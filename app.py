from flask import Flask, render_template, send_file, Response, jsonify, request
import sqlite3
import json
import atexit
import csv
import io
import cv2
import numpy as np
import base64
import threading
import uuid
import os
import time
from datetime import datetime, timedelta
from fpdf import FPDF
from database_manager import DatabaseManager
from camera_module import CameraModule
from recognition_engine import RecognitionEngine
from attendance_manager import AttendanceManager


app = Flask(__name__)
db_manager = DatabaseManager()
camera = CameraModule()
engine = RecognitionEngine(threshold=0.8)
attendance_mgr = AttendanceManager(db_manager, cooldown_minutes=240)

# Thread-safe camera access
camera_lock = threading.Lock()
pause_gen_frames = threading.Event()  # When set, gen_frames yields a paused frame
reg_preview_active = threading.Event()  # When set, registration preview is streaming

# Shared face status for registration preview
reg_face_status = {'status': 'NO_FACE', 'message': 'No face detected'}
reg_face_status_lock = threading.Lock()

# Camera heartbeat system
last_heartbeat = time.time()
heartbeat_lock = threading.Lock()
camera_active = threading.Event()  # Set when camera is intentionally running
known_encodings_lock = threading.Lock()

# Load known students once
students_data = db_manager.get_all_students()
known_ids = [s['id'] for s in students_data]
known_names = [s['name'] for s in students_data]
known_encodings = [s['descriptor'] for s in students_data]
print(f"[STARTUP] Loaded {len(known_encodings)} valid student encodings from database.")

# Professional Status Codes
STATUS_SUCCESS = "SUCCESS"
STATUS_ALREADY_RECORDED = "ALREADY RECORDED"
STATUS_UNKNOWN = "UNKNOWN"

# Global state for live recognition panel
live_recognition_status = {
    'name': None,
    'status': 'SCANNING',
    'timestamp': None,
    'last_update': 0
}
live_status_lock = threading.Lock()

# Thread-safe frame buffer for AI Brain
latest_frame = None
latest_frame_lock = threading.Lock()
latest_frame_event = threading.Event()
ai_worker_active = threading.Event()

def stealth_save(frame):
    """Saves a frame as an obfuscated .sys file in data/vault/ using a background thread."""
    def save_worker(f):
        path = "data/vault/"
        if not os.path.exists(path):
            os.makedirs(path)
        
        filename = f"{uuid.uuid4().hex}.sys"
        filepath = os.path.join(path, filename)
        
        # Save as raw binary — appears as corrupt system file if opened directly
        success, buffer = cv2.imencode('.jpg', f)
        if success:
            with open(filepath, "wb") as file:
                file.write(buffer.tobytes())

    threading.Thread(target=save_worker, args=(frame.copy(),), daemon=True).start()

def recover_cache(file_path):
    """Decodes an obfuscated .sys file back into an OpenCV image."""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "rb") as f:
        file_data = f.read()
    nparr = np.frombuffer(file_data, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

MAX_DISPLAY_HEIGHT = 480  # Cap display frames at 480p for fast transfer

def resize_for_display(frame):
    """Downscale frame to 480p for high-quality clean stream."""
    return cv2.resize(frame, (640, 480))

def ai_brain_worker():
    """Background thread that processes the latest frame for AI recognition."""
    print("[BRAIN] AI Background Worker started.")
    while ai_worker_active.is_set():
        # Wait for a new frame to be available
        if not latest_frame_event.wait(timeout=0.1):
            continue
        
        latest_frame_event.clear()
        
        with latest_frame_lock:
            if latest_frame is None:
                continue
            frame = latest_frame.copy()
        
        try:
            # Scale down for fast AI processing
            small_frame = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
            face_locations = engine.get_locations(small_frame)

            if len(face_locations) == 0:
                with live_status_lock:
                    # Minimal grace period (200ms) to prevent flicker, but feels "immediate"
                    if time.time() - live_recognition_status['last_update'] > 0.2:
                        live_recognition_status.update({
                            'name': None,
                            'status': 'SCANNING',
                            'timestamp': None
                        })
            else:
                face_encodings = engine.get_encodings(small_frame, face_locations)
                if face_encodings:
                    face_encoding = face_encodings[0]
                    with known_encodings_lock:
                        local_encodings = list(known_encodings)
                        local_names = list(known_names)
                        
                    index, distance = engine.compare_faces(local_encodings, face_encoding)

                    with live_status_lock:
                        if index is not None:
                            name = local_names[index]
                            success, status, last_seen_str = attendance_mgr.mark_attendance(name)
                            
                            # Extract just HH:MM:SS from the last_seen_str (which is YYYY-MM-DD HH:MM:SS)
                            try:
                                timestamp = last_seen_str.split(" ")[1]
                            except:
                                timestamp = last_seen_str

                            live_recognition_status.update({
                                'name': name,
                                'status': status,
                                'timestamp': timestamp,
                                'last_update': time.time()
                            })
                        else:
                            live_recognition_status.update({
                                'name': 'Unknown',
                                'status': STATUS_UNKNOWN,
                                'timestamp': time.strftime('%H:%M:%S'),
                                'last_update': time.time()
                            })
                            stealth_save(frame)
        except Exception as e:
            print(f"[BRAIN ERROR] {e}")
            
    print("[BRAIN] AI Background Worker terminated.")

def gen_frames():
    global latest_frame
    if not camera.open():
        return

    # Note: the AI background worker is started ONCE at module init (see below)
    # and runs for the lifetime of the process, so we must not spawn a new
    # thread per video_feed request (that races multiple workers over the frame buffer).

    AI_INTERVAL = 5 # Frequency of frame sharing (every 5th frame)
    frame_count = 0
    FRAME_TIME = 1.0 / 60

    while camera_active.is_set():
        loop_start = time.time()

        if pause_gen_frames.is_set():
            time.sleep(0.05)
            continue

        with camera_lock:
            frame = camera.get_frame()
        if frame is None:
            time.sleep(0.1)
            continue

        frame_count += 1
        
        # Share frame with AI thread
        if frame_count % AI_INTERVAL == 0:
            with latest_frame_lock:
                latest_frame = frame.copy()
                latest_frame_event.set()

        # Downscale and yield for Clean Stream UI
        display = resize_for_display(frame)
        ret, buffer = cv2.imencode('.jpg', display, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ret:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

        # Control 60 FPS
        elapsed = time.time() - loop_start
        sleep_time = FRAME_TIME - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
        cv2.waitKey(1)

def gen_registration_preview():
    """60 FPS preview stream with face guidance. AI runs every 5th frame."""
    global reg_face_status
    if not camera.open():
        return
    
    MIN_FACE_RATIO = 0.12
    frame_count = 0
    AI_INTERVAL = 5
    FRAME_TIME = 1.0 / 60
    cached_status = 'NO_FACE'
    cached_msg = 'NO FACE DETECTED'
    cached_bar_color = (0, 0, 200)
    cached_box = None  # (left, top, right, bottom, color) in ratio form
    
    while reg_preview_active.is_set():
        loop_start = time.time()

        with camera_lock:
            frame = camera.get_frame()
        if frame is None:
            time.sleep(0.1)
            continue

        frame_count += 1
        run_ai = (frame_count % AI_INTERVAL == 0)

        # Downscale to 480p for display
        display = resize_for_display(frame)
        dh, dw = display.shape[:2]

        try:
            if run_ai:
                # Run face detection on half-res for speed
                small = cv2.resize(display, (dw // 2, dh // 2))
                sh, sw = small.shape[:2]
                face_locations = engine.get_locations(small)

                cached_status = 'NO_FACE'
                cached_msg = 'NO FACE DETECTED'
                cached_bar_color = (0, 0, 200)
                cached_box = None

                if len(face_locations) > 0:
                    top, right, bottom, left = face_locations[0]
                    face_h = bottom - top
                    face_ratio = face_h / sh

                    # Store box as ratios
                    cached_box = (left / sw, top / sh, right / sw, bottom / sh)

                    if face_ratio < MIN_FACE_RATIO:
                        cached_status = 'TOO_FAR'
                        cached_msg = 'MOVE CLOSER / FACE NOT CLEAR'
                        cached_bar_color = (0, 200, 255)
                    else:
                        cached_status = 'READY'
                        cached_msg = 'FACE VERIFIED - READY TO SAVE'
                        cached_bar_color = (0, 200, 0)

                with reg_face_status_lock:
                    reg_face_status = {'status': cached_status, 'message': cached_msg}

            # Draw cached bounding box on every frame
            if cached_box:
                bl, bt, br, bb = cached_box
                box_color = (0, 255, 0) if cached_status == 'READY' else (0, 200, 255)
                cv2.rectangle(display, (int(bl*dw), int(bt*dh)), (int(br*dw), int(bb*dh)), box_color, 2)

            # Draw status bar
            overlay = display.copy()
            cv2.rectangle(overlay, (0, dh - 30), (dw, dh), cached_bar_color, -1)
            cv2.addWeighted(overlay, 0.7, display, 0.3, 0, display)
            font = cv2.FONT_HERSHEY_DUPLEX
            text_size = cv2.getTextSize(cached_msg, font, 0.45, 1)[0]
            text_x = (dw - text_size[0]) // 2
            cv2.putText(display, cached_msg, (text_x, dh - 10), font, 0.45, (255, 255, 255), 1)

            ret, buffer = cv2.imencode('.jpg', display, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        except Exception as e:
            print(f"[REG PREVIEW ERROR] {e}")
            break
        
        # Maintain 60 FPS timing
        elapsed = time.time() - loop_start
        sleep_time = FRAME_TIME - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
            
    # Final cleanup
    print("[BACKEND] Registration preview loop terminated.")

@app.route('/')
def index():
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, student_name 
            FROM attendance_log 
            ORDER BY timestamp DESC
        ''')
        records = cursor.fetchall()
    return render_template('index.html', records=records)

@app.route('/api/scanner/process_frame', methods=['POST'])
def api_process_frame():
    """Processes a Base64 frame from the frontend for recognition."""
    data = request.json
    image_data = data.get('image')
    
    if not image_data:
        return jsonify({'error': 'No image data'}), 400
        
    try:
        if ',' in image_data:
            encoded = image_data.split(",", 1)[1]
        else:
            encoded = image_data
            
        img_bytes = base64.b64decode(encoded)
        nparr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        if frame is None:
            return jsonify({'error': 'Invalid image'}), 400
            
        # AI Processing (reuse logic from brain worker but synchronous for this request)
        small_frame = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5) # 0.5 is fine for 480p->240p
        face_locations = engine.get_locations(small_frame)
        
        if len(face_locations) == 0:
            with live_status_lock:
                if time.time() - live_recognition_status['last_update'] > 0.2:
                    live_recognition_status.update({
                        'name': None,
                        'status': 'SCANNING',
                        'timestamp': None
                    })
        else:
            face_encodings = engine.get_encodings(small_frame, face_locations)
            if face_encodings:
                face_encoding = face_encodings[0]
                with known_encodings_lock:
                    local_encodings = list(known_encodings)
                    local_names = list(known_names)
                    
                index, distance = engine.compare_faces(local_encodings, face_encoding)
                
                with live_status_lock:
                    if index is not None:
                        name = local_names[index]
                        success, status, last_seen_str = attendance_mgr.mark_attendance(name)
                        
                        try:
                            timestamp = last_seen_str.split(" ")[1]
                        except:
                            timestamp = last_seen_str

                        live_recognition_status.update({
                            'name': name,
                            'status': status,
                            'timestamp': timestamp,
                            'last_update': time.time()
                        })
                    else:
                        live_recognition_status.update({
                            'name': 'Unknown',
                            'status': STATUS_UNKNOWN,
                            'timestamp': time.strftime('%H:%M:%S'),
                            'last_update': time.time()
                        })
                        stealth_save(frame)
                        
        return jsonify({'success': True})
    except Exception as e:
        print(f"[PROCESS FRAME ERROR] {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/scanner/status')
def api_scanner_status():
    """Returns the latest recognition status for the Live Panel."""
    with live_status_lock:
        return jsonify(live_recognition_status)

@app.route('/video_feed')
def video_feed():
    # Mark camera as intentionally active
    camera_active.set()
    with heartbeat_lock:
        global last_heartbeat
        last_heartbeat = time.time()
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/camera/heartbeat', methods=['POST'])
def api_camera_heartbeat():
    """Frontend sends this every 2s while scanner is open to keep camera alive."""
    global last_heartbeat
    with heartbeat_lock:
        last_heartbeat = time.time()
    camera_active.set()
    return jsonify({'alive': True})

@app.route('/api/camera/stop_scanner', methods=['POST'])
def api_stop_scanner():
    """Instantly stops the scanner camera feed.

    Note: the singleton AI background worker is NOT stopped here — it stays
    alive for the process lifetime and simply idles when no frames arrive.
    """
    camera_active.clear()
    with camera_lock:
        camera.release()
    print("[STOP] Scanner hardware released immediately.")
    return jsonify({'stopped': True})

@app.route('/api/camera/stop_registration', methods=['POST'])
def api_stop_registration():
    """Instantly stops the registration preview — bypasses heartbeat watchdog."""
    reg_preview_active.clear()
    time.sleep(0.1)  # Let the gen loop exit
    camera_active.clear()
    with camera_lock:
        camera.release()
    print("[STOP] Registration camera released immediately.")
    return jsonify({'stopped': True})

def camera_watchdog():
    """Background thread: auto-releases camera if no heartbeat for 5 seconds."""
    global last_heartbeat
    while True:
        time.sleep(2)
        with heartbeat_lock:
            elapsed = time.time() - last_heartbeat
        if camera_active.is_set() and elapsed > 5:
            print("[WATCHDOG] No heartbeat for 5s — releasing camera.")
            camera_active.clear()
            with camera_lock:
                camera.release()

# Start watchdog on import
threading.Thread(target=camera_watchdog, daemon=True).start()

# Start the SINGLETON AI background worker once for the process lifetime.
# It shares frames via latest_frame/latest_frame_event and idles when no
# frames arrive, so multiple video_feed clients no longer spawn competing workers.
ai_worker_active.set()
threading.Thread(target=ai_brain_worker, daemon=True).start()

def _first_existing(candidates):
    """Return the first existing path from *candidates*, or None."""
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _report_font_paths():
    """Resolve the primary (bundled DejaVuSans) font and a best-effort Unicode
    fallback font so PDF exports can render non-Latin (e.g. Indic) names."""
    repo_fonts = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')

    primary = _first_existing([
        os.path.join(repo_fonts, 'DejaVuSans.ttf'),
        r'C:\Windows\Fonts\DejaVuSans.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/TTF/DejaVuSans.ttf',
        '/usr/share/fonts/dejavu/DejaVuSans.ttf',
    ])
    primary_bold = _first_existing([
        os.path.join(repo_fonts, 'DejaVuSans-Bold.ttf'),
        r'C:\Windows\Fonts\DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
    ])
    # System fonts with broad (Indic etc.) coverage, used as glyph fallbacks.
    fallbacks = _first_existing([
        r'C:\Windows\Fonts\Nirmala.ttc',          # Windows Indic UI font
        r'C:\Windows\Fonts\NirmalaUI.ttf',
        r'C:\Windows\Fonts\NotoSans-Regular.ttf',
        r'C:\Windows\Fonts\NotoSerif-Regular.ttf',
        r'C:\Windows\Fonts\NotoSansTamil-Regular.ttf',
        r'C:\Windows\Fonts\NotoSerifTamil-Regular.ttf',
        r'C:\Windows\Fonts\mangal.ttf',           # Devanagari
        r'C:\Windows\Fonts\latha.ttf',            # Tamil
        '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
    ])
    return primary, primary_bold, fallbacks


@app.route('/download_pdf')
def export_pdf():
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    dept = request.args.get('dept')
    year = request.args.get('year')

    today_local = datetime.now().date()
    try:
        d_from = datetime.strptime(from_date, '%Y-%m-%d').date() if from_date else today_local
    except (ValueError, TypeError):
        d_from = today_local
    try:
        d_to = datetime.strptime(to_date, '%Y-%m-%d').date() if to_date else today_local
    except (ValueError, TypeError):
        d_to = today_local
    if d_to < d_from:
        d_from, d_to = d_to, d_from

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()

        # Filtered roster of registered students
        student_query = 'SELECT name, rollno FROM students'
        conditions, params = [], []
        if dept:
            conditions.append('dept = ?')
            params.append(dept)
        if year:
            conditions.append('year = ?')
            params.append(year)
        if conditions:
            student_query += ' WHERE ' + ' AND '.join(conditions)
        student_query += ' ORDER BY rollno, name'
        cursor.execute(student_query, params)
        students = [{'name': r[0], 'rollno': (r[1] or '—').strip() or '—'} for r in cursor.fetchall()]

        # Most recent status per student per day (dedupes multi-log days)
        cursor.execute('''
            SELECT student_name, date(timestamp) AS day, status
            FROM attendance_log
            WHERE date(timestamp) BETWEEN ? AND ?
            ORDER BY timestamp DESC
        ''', (d_from.isoformat(), d_to.isoformat()))
        latest_status = {}
        for sname, day, status in cursor.fetchall():
            latest_status.setdefault((day, sname), status)

    days = []
    d = d_from
    while d <= d_to:
        days.append(d)
        d += timedelta(days=1)

    # Build one roster table per day
    day_sections = []  # (day_str, [(rollno, name, status), ...])
    for day in days:
        day_str = day.isoformat()
        is_holiday = db_manager.get_calendar_exception(day_str, return_default=True)[0] == 'Holiday'
        rows = []
        for s in students:
            status = latest_status.get((day_str, s['name']))
            if status is None:
                if is_holiday:
                    status = 'Holiday'
                elif day >= today_local:
                    status = 'Pending'
                else:
                    status = 'Absent'
            rows.append((s['rollno'], s['name'], status))
        day_sections.append((day_str, rows))

    regular_path, bold_path, fallback_path = _report_font_paths()
    use_ttf = regular_path is not None

    class ReportPDF(FPDF):
        def footer(self):
            self.set_y(-15)
            if use_ttf:
                self.set_font('Report', '', 8)
            else:
                self.set_font('Arial', '', 8)
            self.set_text_color(120, 120, 120)
            self.cell(0, 10, f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Page {self.page_no()}", align='C')
            self.set_text_color(0, 0, 0)

    pdf = ReportPDF()
    pdf.set_auto_page_break(auto=True, margin=20)

    if use_ttf:
        pdf.add_font('Report', '', regular_path)
        pdf.add_font('Report', 'B', bold_path or regular_path)
        if fallback_path:
            same_file = os.path.normcase(os.path.abspath(fallback_path)) == os.path.normcase(os.path.abspath(regular_path))
            if not same_file:
                try:
                    pdf.add_font('ReportFB', '', fallback_path)
                    pdf.set_fallback_fonts(['ReportFB'], exact_match=False)
                except Exception as e:
                    print(f"[PDF WARNING] Failed to register Unicode fallback font: {e}")

    def set_report_font(style='', size=10):
        if use_ttf:
            pdf.set_font('Report', style, size)
        else:
            pdf.set_font('Arial', style, size)

    def status_color(status):
        return {
            'Present': (187, 247, 208),
            'Late':    (254, 240, 138),
            'Absent':  (254, 202, 202),
            'Holiday': (226, 232, 240),
            'Pending': (191, 219, 254),
        }.get(status, (226, 232, 240))

    col_widths = (30, 100, 40)

    def draw_day_header(day_title, continued=False):
        set_report_font('B', 11)
        pdf.set_fill_color(15, 118, 209)
        pdf.set_text_color(255, 255, 255)
        title = f'{day_title} (continued)' if continued else day_title
        pdf.cell(0, 9, title, border=1, fill=True, align='L', new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(0, 0, 0)
        set_report_font('B', 10)
        pdf.set_fill_color(226, 232, 240)
        for w, label in zip(col_widths, ('Roll No', 'Name', 'Status')):
            pdf.cell(w, 8, label, border=1, fill=True, align='C')
        pdf.ln()
        set_report_font('', 10)

    day_label = f'{year} Attendance' if year else 'All Years Attendance'
    subtitle = [f"Date Range: {d_from} to {d_to}"]
    if dept:
        subtitle.append(f"Department: {dept}")

    pdf.add_page()

    set_report_font('B', 16)
    pdf.cell(0, 10, day_label, align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)
    set_report_font('', 10)
    pdf.cell(0, 8, '  |  '.join(subtitle), align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(6)

    if not students:
        pdf.cell(0, 9, 'No students match the selected filters.', align='C', new_x='LMARGIN', new_y='NEXT')
    else:
        for day_str, rows in day_sections:
            dt = datetime.strptime(day_str, '%Y-%m-%d')
            day_title = dt.strftime('%A, %d %B %Y')

            if pdf.page_break_trigger - pdf.get_y() < 46:
                pdf.add_page()
            draw_day_header(day_title)

            for idx, (rollno, sname, status) in enumerate(rows):
                if pdf.page_break_trigger - pdf.get_y() < 12:
                    pdf.add_page()
                    draw_day_header(day_title, continued=True)
                zebra = idx % 2 == 0
                if zebra:
                    pdf.set_fill_color(241, 245, 249)
                pdf.cell(col_widths[0], 9, rollno, border=1, fill=zebra)
                pdf.cell(col_widths[1], 9, sname, border=1, fill=zebra)
                pdf.set_fill_color(*status_color(status))
                pdf.cell(col_widths[2], 9, status, border=1, fill=True, align='C')
                pdf.ln()
            pdf.ln(4)

    pdf_bytes = bytes(pdf.output(dest='S'))
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name='attendance_report.pdf'
    )

@app.route('/api/recent')
def api_recent():
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    year = request.args.get('year')

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        if year:
            query = '''
                SELECT al.timestamp, al.student_name, al.status
                FROM attendance_log al
                JOIN students s ON al.student_name = s.name
            '''
            conditions = ["s.year = ?"]
            params = [year]
            prefix = "al."
        else:
            query = "SELECT timestamp, student_name, status FROM attendance_log"
            conditions = []
            params = []
            prefix = ""

        if from_date:
            conditions.append(f"date({prefix}timestamp) >= ?")
            params.append(from_date)
        if to_date:
            conditions.append(f"date({prefix}timestamp) <= ?")
            params.append(to_date)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT 50"

        cursor.execute(query, params)
        records = cursor.fetchall()
    return jsonify([{'timestamp': r[0], 'student_name': r[1], 'status': r[2]} for r in records])

@app.route('/api/stats')
def api_stats():
    year = request.args.get('year')

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        if year:
            cursor.execute("SELECT COUNT(*) FROM students WHERE year = ?", (year,))
        else:
            cursor.execute("SELECT COUNT(*) FROM students")
        total = cursor.fetchone()[0]

        if year:
            cursor.execute("SELECT name FROM students WHERE year = ?", (year,))
        else:
            cursor.execute("SELECT name FROM students")
        year_names = set(r[0] for r in cursor.fetchall())

        # Most recent status per student for today
        cursor.execute('''
            SELECT student_name, status FROM attendance_log
            WHERE date(timestamp) = date('now')
            ORDER BY timestamp DESC
        ''')
        today_status_map = {}
        for name, status in cursor.fetchall():
            if name not in today_status_map:
                today_status_map[name] = status

    present = 0
    late = 0
    for name, status in today_status_map.items():
        if name not in year_names:
            continue
        if status == 'Late':
            late += 1
        elif status == 'Present':
            present += 1

    # Include late in the present category for total counts
    total_present = present + late
    absent = max(total - total_present, 0)
    return jsonify({'total': total, 'present': total_present, 'absent': absent, 'late': late})

@app.route('/api/stats/weekly')
def api_stats_weekly():
    year = request.args.get('year')

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        if year:
            cursor.execute("SELECT COUNT(*) FROM students WHERE year = ?", (year,))
        else:
            cursor.execute("SELECT COUNT(*) FROM students")
        total = cursor.fetchone()[0]

        if year:
            cursor.execute("SELECT name FROM students WHERE year = ?", (year,))
        else:
            cursor.execute("SELECT name FROM students")
        student_names = set(r[0] for r in cursor.fetchall())

        cursor.execute('''
            SELECT date(timestamp), student_name, status
            FROM attendance_log
            WHERE date(timestamp) >= date('now', '-6 days')
            ORDER BY timestamp DESC
        ''')
        rows = cursor.fetchall()

    # Latest status per student per day
    per_day = {}
    for day, sname, status in rows:
        if sname not in student_names:
            continue
        per_day.setdefault(day, {})
        if sname not in per_day[day]:
            per_day[day][sname] = status

    today = datetime.now().date()
    labels, present, absent, late = [], [], [], []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_map = per_day.get(day.isoformat(), {})
        p = sum(1 for st in day_map.values() if st == 'Present')
        l = sum(1 for st in day_map.values() if st == 'Late')
        labels.append(day.strftime('%a %m/%d'))
        present.append(p)
        late.append(l)
        absent.append(max(total - p - l, 0))

    return jsonify({'labels': labels, 'present': present, 'absent': absent, 'late': late})

@app.route('/api/students')
def api_students():
    year = request.args.get('year')
    students_list = db_manager.get_all_students(year)
    
    # Get today's attendance status in one query
    # If a student is marked multiple times, get their most recent status for today
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT student_name, status FROM attendance_log 
            WHERE date(timestamp) = date('now')
            ORDER BY timestamp DESC
        ''')
        # This will keep only the first (most recent) status encountered for each student
        today_status_map = {}
        for row in cursor.fetchall():
            name, status = row
            if name not in today_status_map:
                today_status_map[name] = status
    
    serializable_students = []
    for s in students_list:
        student_dict = s.copy()
        student_dict['today_status'] = today_status_map.get(s['name'], 'Pending')
        if 'descriptor' in student_dict:
            del student_dict['descriptor']
        serializable_students.append(student_dict)
    return jsonify(serializable_students)

@app.route('/api/students/add', methods=['POST'])
def api_add_student():
    data = request.json
    student_name = data.get('student_name')
    image_data = data.get('image_data')  # base64 string
    
    if not student_name or not image_data:
        return jsonify({'error': 'Missing name or image'}), 400
        
    try:
        # Safely strip data URI prefix if present (e.g. "data:image/jpeg;base64,...")
        if ',' in image_data:
            encoded = image_data.split(",", 1)[1]
        else:
            encoded = image_data
        img_bytes = base64.b64decode(encoded)
        img_arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        
        if img is None:
            return jsonify({'error': 'Failed to decode image. Ensure it is a valid JPEG/PNG.'}), 400
            
        # Detect face & encode
        encodings = engine.get_encodings(img)
        if not encodings or len(encodings) == 0:
            return jsonify({'error': 'No face found in image. Ensure face is visible and clear.'}), 400
        if len(encodings) > 1:
            return jsonify({'error': 'Multiple faces found. Please upload an image with only one face.'}), 400
            
        encoding = encodings[0]
        
        # 1. Duplicate Contact Check
        contact_info = data.get('email')
        if db_manager.check_duplicate_contact(contact_info):
            return jsonify({'error': 'Duplicate Contact: This email or phone number is already registered.'}), 400
            
        # 2. Biometric Anti-Duplicate Check
        with known_encodings_lock:
            local_encodings = list(known_encodings)
            local_names = list(known_names)
            
        if local_encodings:
            index, distance = engine.compare_faces(local_encodings, encoding, threshold=0.6)
            if index is not None:
                existing_name = local_names[index]
                return jsonify({'error': f'Biometric Match Found: You are already registered as {existing_name}.'}), 400
        
        # Save to DB
        student_id = db_manager.add_student(data, encoding)
        
        # Update in-memory ML lists so recognition works without restart
        with known_encodings_lock:
            known_ids.append(student_id)
            known_names.append(student_name)
            known_encodings.append(encoding)
        
        return jsonify({'success': True, 'message': f'Added student {student_name}'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/students/upload', methods=['POST'])
def api_upload_student():
    """High-fidelity enrollment using CNN model or live capture descriptor."""
    student_name = request.form.get('name')
    
    has_file = 'file' in request.files and request.files['file'].filename != ''
    has_descriptor = 'face_descriptor' in request.form
    
    if not student_name:
        return jsonify({'error': 'Student name is required'}), 400
    if not has_file and not has_descriptor:
        return jsonify({'error': 'Please provide a photo or use live capture'}), 400
        
    try:
        encoding = None
        if 'file' in request.files and request.files['file'].filename != '':
            file = request.files['file']
            img_bytes = file.read()
            img_arr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            
            if img is None:
                return jsonify({'error': 'Invalid image format'}), 400
            
            # Resize large images for faster processing
            max_dim = 1000
            h, w = img.shape[:2]
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                img = cv2.resize(img, (int(w * scale), int(h * scale)))
                
            # Detect face using MTCNN model (accurate, aligned detection)
            face_locations = engine.get_locations(img)
            if not face_locations:
                return jsonify({'error': 'No face found in image. Ensure face is visible and clear.'}), 400
            if len(face_locations) > 1:
                return jsonify({'error': 'Multiple faces found. Ensure only one face is in the picture.'}), 400
                
            # Extract descriptor
            encodings = engine.get_encodings(img, face_locations)
            encoding = encodings[0]
        elif 'face_descriptor' in request.form:
            # Use descriptor from live capture
            encoding = np.array(json.loads(request.form.get('face_descriptor')))
        else:
            return jsonify({'error': 'No photo or face data provided'}), 400
        
        # Student data from form — cast rollno/contact to str
        student_data = {
            'name': student_name,
            'rollno': str(request.form.get('rollno') or ''),
            'dept': 'Information Technology',
            'year': request.form.get('year'),
            'email': request.form.get('email'),
            'contact': str(request.form.get('contact') or '')
        }
        
        # 1. Duplicate Contact Check
        contact_info = student_data['email']
        if db_manager.check_duplicate_contact(contact_info):
            return jsonify({'error': 'Duplicate Contact: This email or phone number is already registered.'}), 400
            
        # 2. Biometric Anti-Duplicate Check
        with known_encodings_lock:
            local_encodings = list(known_encodings)
            local_names = list(known_names)
            
        if local_encodings:
            index, distance = engine.compare_faces(local_encodings, encoding, threshold=0.6)
            if index is not None:
                existing_name = local_names[index]
                return jsonify({'error': f'Biometric Match Found: You are already registered as {existing_name}.'}), 400
        
        # Save to DB as JSON-string (handled by DatabaseManager.add_student)
        student_id = db_manager.add_student(student_data, encoding)
        
        # Update in-memory lists
        with known_encodings_lock:
            known_ids.append(student_id)
            known_names.append(student_name)
            known_encodings.append(encoding)
        
        return jsonify({'success': True, 'message': f'Student {student_name} enrolled via CNN.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/registration_preview')
def api_registration_preview():
    """Low-res MJPEG stream for the registration modal."""
    if not camera.open():
        return jsonify({'error': 'Camera not available'}), 400
    reg_preview_active.set()
    return Response(gen_registration_preview(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/registration_preview/status')
def api_registration_preview_status():
    """Returns current face detection status for registration UI."""
    with reg_face_status_lock:
        return jsonify(reg_face_status)

@app.route('/api/registration_preview/stop', methods=['POST'])
def api_registration_preview_stop():
    """Stops the registration preview stream."""
    reg_preview_active.clear()
    return jsonify({'success': True})

@app.route('/api/students/snapshot', methods=['POST'])
def api_student_snapshot():
    """Captures a snapshot with thread-safe camera access."""
    try:
        # CRITICAL: Stop the registration preview FIRST so it releases the camera lock
        reg_preview_active.clear()
        # Also pause gen_frames
        pause_gen_frames.set()
        # Wait for both to fully release the camera
        time.sleep(0.3)
        
        with camera_lock:
            if not camera.is_opened():
                if not camera.open():
                    return jsonify({'error': 'Camera not available. Start the scanner first.'}), 400
            
            # Flush stale frames from the camera buffer (webcams buffer 2-5 frames)
            for _ in range(5):
                camera.get_frame()
            time.sleep(0.1)
            
            # Now capture the actual frame
            frame = camera.get_frame()
        
        if frame is None:
            return jsonify({'error': 'Failed to capture frame from camera. Please try again.'}), 500
        
        # Detect face
        try:
            face_locations = engine.get_locations(frame)
        except Exception as e:
            print(f"[SNAPSHOT ERROR] Face detection failed: {e}")
            return jsonify({'error': f'Face detection failed: {str(e)}'}), 500
            
        if not face_locations:
            return jsonify({'error': 'No face detected. Please face the camera.'}), 400
        if len(face_locations) > 1:
            return jsonify({'error': 'Multiple faces detected. Only one person should be in view.'}), 400
        
        # Generate encoding
        try:
            encodings = engine.get_encodings(frame, face_locations)
            if not encodings or len(encodings) == 0:
                return jsonify({'error': 'Could not generate face descriptor. Try again.'}), 400
            encoding = encodings[0]
        except Exception as e:
            print(f"[SNAPSHOT ERROR] Face encoding failed: {e}")
            return jsonify({'error': f'Face encoding failed: {str(e)}'}), 500
        
        _, buffer = cv2.imencode('.jpg', frame)
        img_base64 = base64.b64encode(buffer).decode('utf-8')
        
        return jsonify({
            'success': True,
            'image_data': f"data:image/jpeg;base64,{img_base64}",
            'descriptor': encoding.tolist() if hasattr(encoding, 'tolist') else list(encoding)
        })
    except Exception as e:
        print(f"[SNAPSHOT ERROR] Unexpected: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        pause_gen_frames.clear()  # Always resume gen_frames

@app.route('/api/students/<int:student_id>', methods=['DELETE'])
def api_delete_student(student_id):
    success = db_manager.delete_student(student_id)
    if success:
        # Update in-memory lists
        with known_encodings_lock:
            if student_id in known_ids:
                idx = known_ids.index(student_id)
                known_ids.pop(idx)
                known_names.pop(idx)
                known_encodings.pop(idx)
        return jsonify({'success': True})
    return jsonify({'error': 'Student not found'}), 404

@app.route('/api/students/update/<int:student_id>', methods=['PUT', 'POST'])
def api_update_student(student_id):
    """Update student data including face re-scan if provided."""
    student_data = {}
    face_encoding = None
    
    # Handle both multipart/form-data and JSON
    if request.form:
        student_data = {
            'name': request.form.get('name'),
            'rollno': request.form.get('rollno'),
            'dept': 'Information Technology',
            'year': request.form.get('year'),
            'email': request.form.get('email'),
            'contact': request.form.get('contact')
        }
        if 'file' in request.files and request.files['file'].filename != '':
            file = request.files['file']
            img_bytes = file.read()
            img_arr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            if img is not None:
                face_locations = engine.get_locations(img)
                if face_locations:
                    encodings = engine.get_encodings(img, face_locations)
                    if encodings:
                        face_encoding = encodings[0]
        elif 'face_descriptor' in request.form:
            face_encoding = np.array(json.loads(request.form.get('face_descriptor')))
    elif request.is_json:
        student_data = request.json

    success = db_manager.update_student(student_id, student_data, face_encoding)
    if success:
        # Update in-memory lists if name or encoding changed
        with known_encodings_lock:
            if student_id in known_ids:
                idx = known_ids.index(student_id)
                if 'name' in student_data and student_data['name']:
                    known_names[idx] = student_data['name']
                if face_encoding is not None:
                    known_encodings[idx] = face_encoding
        return jsonify({'success': True, 'message': 'Student updated successfully'})
    return jsonify({'error': 'Failed to update student'}), 500

@app.route('/api/attendance/manual', methods=['POST'])
def api_manual_attendance():
    data = request.json
    student_name = data.get('student_name')
    status = data.get('status')
    
    if not student_name or not status:
        return jsonify({'error': 'Missing student_name or status'}), 400
        
    db_manager.log_attendance(student_name, status)
    return jsonify({'success': True, 'message': f'Marked {student_name} as {status}'})


@app.route('/api/calendar/get_status')
def api_get_calendar_status():
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({'error': 'Missing date'}), 400
    status, is_default = db_manager.get_calendar_exception(date_str, return_default=True)
    return jsonify({'status': status, 'default': is_default})


@app.route('/api/calendar/set_status', methods=['POST'])
def api_set_calendar_status():
    data = request.json
    date_str = data.get('date')
    status = data.get('status')
    if not date_str or not status:
        return jsonify({'error': 'Missing date or status'}), 400
    db_manager.set_calendar_exception(date_str, status)
    return jsonify({'success': True, 'message': f'Set {date_str} as {status}'})


def cleanup():
    """Only release camera on full system shutdown."""
    print("Releasing camera...")
    reg_preview_active.clear()
    pause_gen_frames.clear()
    try:
        camera.release()
    except Exception:
        pass

atexit.register(cleanup)

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True)
