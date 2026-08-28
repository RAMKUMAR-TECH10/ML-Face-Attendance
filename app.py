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
                    index, distance = engine.compare_faces(known_encodings, face_encoding)

                    with live_status_lock:
                        if index is not None:
                            name = known_names[index]
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
                index, distance = engine.compare_faces(known_encodings, face_encoding)
                
                with live_status_lock:
                    if index is not None:
                        name = known_names[index]
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

@app.route('/download_pdf')
def export_pdf():
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    dept = request.args.get('dept')
    year = request.args.get('year')
    
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        
        query = '''
            SELECT al.timestamp, al.student_name, al.status
            FROM attendance_log al
        '''
        conditions = []
        params = []
        
        if dept or year:
            query = '''
                SELECT al.timestamp, al.student_name, al.status
                FROM attendance_log al
                JOIN students s ON al.student_name = s.name
            '''
            if dept:
                conditions.append("s.dept = ?")
                params.append(dept)
            if year:
                conditions.append("s.year = ?")
                params.append(year)
        
        if from_date:
            conditions.append("date(al.timestamp) >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("date(al.timestamp) <= ?")
            params.append(to_date)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY al.timestamp DESC"
        
        cursor.execute(query, params)
        records = cursor.fetchall()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Attendance Report", ln=True, align='C')
    pdf.ln(5)
    
    pdf.set_font("Arial", '', 10)
    if from_date or to_date:
        date_range = f"Date Range: {from_date or 'Start'} to {to_date or 'End'}"
        pdf.cell(0, 8, date_range, ln=True, align='C')
        pdf.ln(2)
    if dept:
        pdf.cell(0, 8, f"Department: {dept}", ln=True, align='C')
        pdf.ln(2)
    if year:
        pdf.cell(0, 8, f"Year: {year}", ln=True, align='C')
        pdf.ln(2)
    pdf.ln(5)
    
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(40, 10, "Timestamp", 1)
    pdf.cell(60, 10, "Student Name", 1)
    pdf.cell(40, 10, "Status", 1)
    pdf.ln()
    
    pdf.set_font("Arial", '', 10)
    for row in records:
        pdf.cell(40, 10, str(row[0]), 1)
        pdf.cell(60, 10, str(row[1]), 1)
        pdf.cell(40, 10, str(row[2]), 1)
        pdf.ln()
    
    pdf_bytes = pdf.output(dest='S').encode('latin-1')
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
    
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT timestamp, student_name, status FROM attendance_log"
        conditions = []
        params = []
        
        if from_date:
            conditions.append("date(timestamp) >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("date(timestamp) <= ?")
            params.append(to_date)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT 50"
        
        cursor.execute(query, params)
        records = cursor.fetchall()
    return jsonify([{'timestamp': r[0], 'student_name': r[1], 'status': r[2]} for r in records])

@app.route('/api/stats')
def api_stats():
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM students")
        total = cursor.fetchone()[0]
        
        # Determine the start of today to query attendance efficiently
        cursor.execute("SELECT COUNT(DISTINCT student_name) FROM attendance_log WHERE date(timestamp) = date('now')")
        present = cursor.fetchone()[0]
        
    absent = total - present
    return jsonify({'total': total, 'present': present, 'absent': absent})

@app.route('/api/students')
def api_students():
    students_list = db_manager.get_all_students()
    
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
        if known_encodings:
            index, distance = engine.compare_faces(known_encodings, encoding, threshold=0.6)
            if index is not None:
                existing_name = known_names[index]
                return jsonify({'error': f'Biometric Match Found: You are already registered as {existing_name}.'}), 400
        
        # Save to DB
        student_id = db_manager.add_student(data, encoding)
        
        # Update in-memory ML lists so recognition works without restart
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
            'dept': request.form.get('dept'),
            'year': request.form.get('year'),
            'email': request.form.get('email'),
            'contact': str(request.form.get('contact') or '')
        }
        
        # 1. Duplicate Contact Check
        contact_info = student_data['email']
        if db_manager.check_duplicate_contact(contact_info):
            return jsonify({'error': 'Duplicate Contact: This email or phone number is already registered.'}), 400
            
        # 2. Biometric Anti-Duplicate Check
        if known_encodings:
            index, distance = engine.compare_faces(known_encodings, encoding, threshold=0.6)
            if index is not None:
                existing_name = known_names[index]
                return jsonify({'error': f'Biometric Match Found: You are already registered as {existing_name}.'}), 400
        
        # Save to DB as JSON-string (handled by DatabaseManager.add_student)
        student_id = db_manager.add_student(student_data, encoding)
        
        # Update in-memory lists
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
            'dept': request.form.get('dept'),
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
