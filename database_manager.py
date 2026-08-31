import json
import sqlite3
import numpy as np
import os

class DatabaseManager:
    def __init__(self, db_path="data/logs/attendance.db"):
        self.db_path = db_path
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def _log_sync_event(self, cursor, event_type, table_name, record_uuid, payload_dict):
        cursor.execute('''
            INSERT INTO sync_queue (event_type, table_name, record_uuid, payload) 
            VALUES (?, ?, ?, ?)
        ''', (event_type, table_name, record_uuid, json.dumps(payload_dict)))

    def init_db(self):
        import uuid
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Students table with expanded schema
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    rollno TEXT,
                    dept TEXT,
                    year TEXT,
                    email TEXT,
                    contact TEXT,
                    faceDescriptor TEXT NOT NULL
                )
            ''')
            # Attendance log table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attendance_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_name TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'Present'
                )
            ''')
            # Calendar Exceptions table (for Holidays/Working days)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS calendar_exceptions (
                    date TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                )
            ''')
            
            # Migration: add missing columns if they don't exist
            columns = [
                ('rollno', 'TEXT'),
                ('dept', 'TEXT'),
                ('year', 'TEXT'),
                ('email', 'TEXT'),
                ('contact', 'TEXT')
            ]
            for col_name, col_type in columns:
                try:
                    cursor.execute(f"ALTER TABLE students ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass # Column already exists
            
            # Sync Metadata migrations for students
            sync_columns_students = [
                ('uuid', 'TEXT'),
                ('updated_at', 'TEXT'),
                ('is_deleted', 'INTEGER DEFAULT 0')
            ]
            for col_name, col_type in sync_columns_students:
                try:
                    cursor.execute(f"ALTER TABLE students ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass

            # Sync Metadata migrations for attendance logs
            sync_columns_attendance = [
                ('uuid', 'TEXT'),
                ('origin_node_id', 'TEXT'),
                ('is_deleted', 'INTEGER DEFAULT 0')
            ]
            for col_name, col_type in sync_columns_attendance:
                try:
                    cursor.execute(f"ALTER TABLE attendance_log ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass

            # Sync Metadata migrations for calendar exceptions
            sync_columns_calendar = [
                ('uuid', 'TEXT'),
                ('updated_at', 'TEXT'),
                ('is_deleted', 'INTEGER DEFAULT 0')
            ]
            for col_name, col_type in sync_columns_calendar:
                try:
                    cursor.execute(f"ALTER TABLE calendar_exceptions ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError:
                    pass

            # Sync Queue table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    record_uuid TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

            # Backfill UUIDs and timestamps for existing student records
            cursor.execute("SELECT id FROM students WHERE uuid IS NULL OR uuid = ''")
            for (row_id,) in cursor.fetchall():
                new_uuid = str(uuid.uuid4())
                cursor.execute("UPDATE students SET uuid = ?, updated_at = datetime('now') WHERE id = ?", (new_uuid, row_id))

            # Backfill UUIDs and node ids for existing attendance logs
            cursor.execute("SELECT id FROM attendance_log WHERE uuid IS NULL OR uuid = ''")
            for (row_id,) in cursor.fetchall():
                new_uuid = str(uuid.uuid4())
                cursor.execute("UPDATE attendance_log SET uuid = ?, origin_node_id = 'local' WHERE id = ?", (new_uuid, row_id))

            # Backfill UUIDs and timestamps for existing calendar exceptions
            cursor.execute("SELECT date FROM calendar_exceptions WHERE uuid IS NULL OR uuid = ''")
            for (date_val,) in cursor.fetchall():
                new_uuid = str(uuid.uuid4())
                cursor.execute("UPDATE calendar_exceptions SET uuid = ?, updated_at = datetime('now') WHERE date = ?", (new_uuid, date_val))
            conn.commit()

    def add_student(self, student_data, faceDescriptor, sync_mode=False):
        import uuid
        from datetime import datetime
        # Convert descriptor to JSON string — use .tolist() for clean float serialization
        if isinstance(faceDescriptor, np.ndarray):
            faceDescriptor = json.dumps(faceDescriptor.tolist())
        elif isinstance(faceDescriptor, list):
            faceDescriptor = json.dumps(faceDescriptor)
            
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Cast rollno and contact to strings to prevent SQLite type mismatches
            rollno = str(student_data.get('rollno') or '')
            contact = str(student_data.get('contact') or '')
            student_uuid = student_data.get('uuid') or str(uuid.uuid4())
            updated_at = student_data.get('updated_at') if (sync_mode and student_data.get('updated_at')) else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                INSERT INTO students (name, rollno, dept, year, email, contact, faceDescriptor, uuid, updated_at, is_deleted) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ''', (
                student_data.get('name'),
                rollno,
                student_data.get('dept'),
                student_data.get('year'),
                student_data.get('email'),
                contact,
                faceDescriptor,
                student_uuid,
                updated_at
            ))
            
            # Log sync event
            if not sync_mode:
                self._log_sync_event(cursor, 'INSERT', 'students', student_uuid, {
                    'name': student_data.get('name'),
                    'rollno': rollno,
                    'dept': student_data.get('dept'),
                    'year': student_data.get('year'),
                    'email': student_data.get('email'),
                    'contact': contact,
                    'faceDescriptor': faceDescriptor,
                    'updated_at': updated_at,
                    'is_deleted': 0
                })
            
            conn.commit()
            return cursor.lastrowid

    def update_student(self, student_id, student_data, faceDescriptor=None, sync_mode=False):
        from datetime import datetime
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Fetch UUID and existing descriptor first
            cursor.execute("SELECT uuid, faceDescriptor FROM students WHERE id = ?", (student_id,))
            row = cursor.fetchone()
            if not row:
                return False
            student_uuid, existing_descriptor = row
            
            rollno = str(student_data.get('rollno') or '')
            contact = str(student_data.get('contact') or '')
            updated_at = student_data.get('updated_at') if student_data.get('updated_at') else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                UPDATE students 
                SET name = ?, rollno = ?, dept = ?, year = ?, email = ?, contact = ?, updated_at = ?
                WHERE id = ?
            ''', (
                student_data.get('name'),
                rollno,
                student_data.get('dept'),
                student_data.get('year'),
                student_data.get('email'),
                contact,
                updated_at,
                student_id
            ))
            
            final_descriptor = existing_descriptor
            # Update descriptor if provided — use .tolist() for clean serialization
            if faceDescriptor is not None:
                if isinstance(faceDescriptor, np.ndarray):
                    faceDescriptor = json.dumps(faceDescriptor.tolist())
                elif isinstance(faceDescriptor, list):
                    faceDescriptor = json.dumps(faceDescriptor)
                cursor.execute("UPDATE students SET faceDescriptor = ? , updated_at = ? WHERE id = ?", (
                    faceDescriptor, 
                    updated_at, 
                    student_id
                ))
                final_descriptor = faceDescriptor
            
            # Log sync event
            if not sync_mode:
                self._log_sync_event(cursor, 'UPDATE', 'students', student_uuid, {
                    'name': student_data.get('name'),
                    'rollno': rollno,
                    'dept': student_data.get('dept'),
                    'year': student_data.get('year'),
                    'email': student_data.get('email'),
                    'contact': contact,
                    'faceDescriptor': final_descriptor,
                    'updated_at': updated_at,
                'is_deleted': 0
            })
            
            conn.commit()
            return cursor.rowcount > 0

    def get_all_students(self, year=None):
        from datetime import datetime, timedelta
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if year:
                cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor FROM students WHERE year = ? AND is_deleted = 0", (year,))
            else:
                cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor FROM students WHERE is_deleted = 0")
            rows = cursor.fetchall()
            
            # Recalculate attendance percentages
            # 1. Get calendar exceptions
            exceptions = {}
            try:
                cursor.execute("SELECT date, status FROM calendar_exceptions WHERE is_deleted = 0")
                for d, s in cursor.fetchall():
                    exceptions[d] = s
            except sqlite3.OperationalError:
                pass # Table doesn't exist yet (handled in init_db)
                
            # 2. Get range of dates
            start_date = None
            try:
                cursor.execute("SELECT MIN(date(timestamp)) FROM attendance_log WHERE is_deleted = 0")
                min_log_date_str = cursor.fetchone()[0]
            except:
                min_log_date_str = None
                
            min_exception_date_str = min(exceptions.keys()) if exceptions else None
            today_local = datetime.now().date()
            
            for date_str in (min_log_date_str, min_exception_date_str):
                if date_str:
                    try:
                        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
                        if start_date is None or d < start_date:
                            start_date = d
                    except ValueError:
                        pass
                        
            if start_date is None or start_date > today_local:
                start_date = today_local
                
            # 3. Generate all working days
            working_days = set()
            curr = start_date
            while curr <= today_local:
                date_str = curr.isoformat()
                if date_str in exceptions:
                    if exceptions[date_str] == 'Working':
                        working_days.add(date_str)
                else:
                    if curr.weekday() != 6: # 6 is Sunday
                        working_days.add(date_str)
                curr += timedelta(days=1)
                
            total_working = len(working_days)
            
            # 4. Get present days count per student
            student_present_counts = {}
            if total_working > 0:
                cursor.execute('''
                    SELECT student_name, date(timestamp) 
                    FROM attendance_log 
                    WHERE status IN ('Present', 'Late') AND is_deleted = 0
                ''')
                student_present_days = {}
                for name, date_str in cursor.fetchall():
                    if date_str in working_days:
                        student_present_days.setdefault(name, set()).add(date_str)
                for name, dates in student_present_days.items():
                    student_present_counts[name] = len(dates)
            
            results = []
            for row in rows:
                sid, name, rollno, dept, year, email, contact, descriptor_str = row
                if not descriptor_str or descriptor_str.strip() == '':
                    print(f"[DB WARNING] Student '{name}' (id={sid}) has empty descriptor, skipping.")
                    continue
                try:
                    descriptor_list = json.loads(descriptor_str)
                    descriptor = np.array(descriptor_list, dtype=np.float64)
                except:
                    print(f"[DB WARNING] Student '{name}' (id={sid}) has corrupt face descriptor, skipping.")
                    continue
                # Skip students with empty or wrong-dimension descriptors
                if descriptor.ndim != 1 or descriptor.shape[0] not in (128, 512):
                    print(f"[DB WARNING] Student '{name}' (id={sid}) has invalid descriptor shape {descriptor.shape}, skipping.")
                    continue
                
                # Compute attendance percentage
                present_count = student_present_counts.get(name, 0)
                pct = 100 if total_working == 0 else int(round((present_count / total_working) * 100))
                
                results.append({
                    'id': sid,
                    'name': name,
                    'rollno': rollno,
                    'dept': dept,
                    'year': year,
                    'email': email,
                    'contact': contact,
                    'descriptor': descriptor,
                    'attendance_pct': pct
                })
            return results

    def check_duplicate_contact(self, contact_info):
        """Checks if the given email or phone already exists in the database."""
        if not contact_info:
            return False
            
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM students 
                WHERE (email = ? OR contact = ?) AND is_deleted = 0
            ''', (contact_info, contact_info))
            count = cursor.fetchone()[0]
            return count > 0

    def get_student_by_id(self, student_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor FROM students WHERE id = ? AND is_deleted = 0", (student_id,))
            row = cursor.fetchone()
            if row:
                sid, name, rollno, dept, year, email, contact, descriptor_str = row
                return {
                    'id': sid,
                    'name': name,
                    'rollno': rollno,
                    'dept': dept,
                    'year': year,
                    'email': email,
                    'contact': contact,
                    'descriptor_str': descriptor_str
                }
            return None

    def log_attendance(self, student_name, status='Present', log_uuid=None, origin_node_id='local', timestamp_str=None, sync_mode=False):
        import uuid
        from datetime import datetime
        if not log_uuid:
            log_uuid = str(uuid.uuid4())
        if not timestamp_str:
            timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO attendance_log (student_name, status, uuid, origin_node_id, timestamp, is_deleted) 
                VALUES (?, ?, ?, ?, ?, 0)
            ''', (student_name, status, log_uuid, origin_node_id, timestamp_str))
            
            if not sync_mode:
                self._log_sync_event(cursor, 'INSERT', 'attendance_log', log_uuid, {
                    'student_name': student_name,
                    'status': status,
                    'timestamp': timestamp_str,
                    'origin_node_id': origin_node_id,
                    'is_deleted': 0
                })
            conn.commit()

    def get_last_attendance(self, student_name):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM attendance_log WHERE student_name = ? AND is_deleted = 0 ORDER BY timestamp DESC LIMIT 1", (student_name,))
            result = cursor.fetchone()
            return result[0] if result else None

    def delete_student(self, student_id, sync_mode=False):
        from datetime import datetime
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT uuid FROM students WHERE id = ?", (student_id,))
            row = cursor.fetchone()
            if not row:
                return False
            student_uuid = row[0]
            
            updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                UPDATE students 
                SET is_deleted = 1, updated_at = ? 
                WHERE id = ?
            ''', (updated_at, student_id))
            
            if not sync_mode:
                self._log_sync_event(cursor, 'DELETE', 'students', student_uuid, {
                    'updated_at': updated_at,
                    'is_deleted': 1
                })
            conn.commit()
            return cursor.rowcount > 0

    def get_calendar_exception(self, date_str, return_default=False):
        from datetime import datetime
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM calendar_exceptions WHERE date = ? AND is_deleted = 0", (date_str,))
            row = cursor.fetchone()
            if row:
                return (row[0], False) if return_default else row[0]
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                default_status = "Holiday" if dt.weekday() == 6 else "Working"
                return (default_status, True) if return_default else default_status
            except:
                default_status = "Working"
                return (default_status, True) if return_default else default_status

    def set_calendar_exception(self, date_str, status, exception_uuid=None, sync_mode=False):
        import uuid
        from datetime import datetime
        if not exception_uuid:
            exception_uuid = str(uuid.uuid4())
        updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO calendar_exceptions (date, status, uuid, updated_at, is_deleted) 
                VALUES (?, ?, ?, ?, 0)
            ''', (date_str, status, exception_uuid, updated_at))
            
            if not sync_mode:
                self._log_sync_event(cursor, 'INSERT', 'calendar_exceptions', exception_uuid, {
                    'date': date_str,
                    'status': status,
                    'updated_at': updated_at,
                    'is_deleted': 0
                })
            conn.commit()

    def get_student_by_uuid(self, student_uuid):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor, is_deleted, updated_at FROM students WHERE uuid = ?", (student_uuid,))
            row = cursor.fetchone()
            if row:
                sid, name, rollno, dept, year, email, contact, descriptor_str, is_deleted, updated_at = row
                return {
                    'id': sid,
                    'name': name,
                    'rollno': rollno,
                    'dept': dept,
                    'year': year,
                    'email': email,
                    'contact': contact,
                    'descriptor_str': descriptor_str,
                    'is_deleted': is_deleted,
                    'updated_at': updated_at
                }
            return None

    def update_student_by_uuid(self, student_uuid, student_data, faceDescriptor=None, updated_at=None, is_deleted=0):
        from datetime import datetime
        if not updated_at:
            updated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if is_deleted == 1 or student_data.get('is_deleted', 0) == 1:
                cursor.execute('''
                    UPDATE students 
                    SET is_deleted = 1, updated_at = ? 
                    WHERE uuid = ?
                ''', (updated_at, student_uuid))
                conn.commit()
                return True
            rollno = str(student_data.get('rollno') or '')
            contact = str(student_data.get('contact') or '')
            cursor.execute('''
                UPDATE students 
                SET name = ?, rollno = ?, dept = ?, year = ?, email = ?, contact = ?, updated_at = ?, is_deleted = ?
                WHERE uuid = ?
            ''', (
                student_data.get('name'),
                rollno,
                student_data.get('dept'),
                student_data.get('year'),
                student_data.get('email'),
                contact,
                updated_at,
                is_deleted,
                student_uuid
            ))
            if faceDescriptor is not None:
                if isinstance(faceDescriptor, np.ndarray):
                    faceDescriptor = json.dumps(faceDescriptor.tolist())
                elif isinstance(faceDescriptor, list):
                    faceDescriptor = json.dumps(faceDescriptor)
                cursor.execute("UPDATE students SET faceDescriptor = ?, updated_at = ? WHERE uuid = ?", (
                    faceDescriptor, 
                    updated_at, 
                    student_uuid
                ))
            conn.commit()
            return cursor.rowcount > 0

    def get_sync_events_since(self, last_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, event_type, table_name, record_uuid, payload, created_at 
                FROM sync_queue 
                WHERE id > ? 
                ORDER BY id ASC
            ''', (last_id,))
            rows = cursor.fetchall()
            events = []
            for row in rows:
                events.append({
                    'id': row[0],
                    'event_type': row[1],
                    'table_name': row[2],
                    'record_uuid': row[3],
                    'payload': json.loads(row[4]),
                    'created_at': row[5]
                })
            return events

    def clear_sync_events_up_to(self, event_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sync_queue WHERE id <= ?", (event_id,))
            conn.commit()
