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

    def init_db(self):
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
                    
            conn.commit()

    def add_student(self, student_data, faceDescriptor):
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
            cursor.execute('''
                INSERT INTO students (name, rollno, dept, year, email, contact, faceDescriptor) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                student_data.get('name'),
                rollno,
                student_data.get('dept'),
                student_data.get('year'),
                student_data.get('email'),
                contact,
                faceDescriptor
            ))
            conn.commit()
            return cursor.lastrowid

    def update_student(self, student_id, student_data, faceDescriptor=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Update core fields — cast rollno/contact to strings
            rollno = str(student_data.get('rollno') or '')
            contact = str(student_data.get('contact') or '')
            cursor.execute('''
                UPDATE students 
                SET name = ?, rollno = ?, dept = ?, year = ?, email = ?, contact = ?
                WHERE id = ?
            ''', (
                student_data.get('name'),
                rollno,
                student_data.get('dept'),
                student_data.get('year'),
                student_data.get('email'),
                contact,
                student_id
            ))
            
            # Update descriptor if provided — use .tolist() for clean serialization
            if faceDescriptor is not None:
                if isinstance(faceDescriptor, np.ndarray):
                    faceDescriptor = json.dumps(faceDescriptor.tolist())
                elif isinstance(faceDescriptor, list):
                    faceDescriptor = json.dumps(faceDescriptor)
                cursor.execute("UPDATE students SET faceDescriptor = ? WHERE id = ?", (faceDescriptor, student_id))
            
            conn.commit()
            return cursor.rowcount > 0

    def get_all_students(self, year=None):
        from datetime import datetime, timedelta
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if year:
                cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor FROM students WHERE year = ?", (year,))
            else:
                cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor FROM students")
            rows = cursor.fetchall()
            
            # Recalculate attendance percentages
            # 1. Get calendar exceptions
            exceptions = {}
            try:
                cursor.execute("SELECT date, status FROM calendar_exceptions")
                for d, s in cursor.fetchall():
                    exceptions[d] = s
            except sqlite3.OperationalError:
                pass # Table doesn't exist yet (handled in init_db)
                
            # 2. Get range of dates
            start_date = None
            try:
                cursor.execute("SELECT MIN(date(timestamp)) FROM attendance_log")
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
                    WHERE status IN ('Present', 'Late')
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
                WHERE email = ? OR contact = ?
            ''', (contact_info, contact_info))
            count = cursor.fetchone()[0]
            return count > 0

    def get_student_by_id(self, student_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, rollno, dept, year, email, contact, faceDescriptor FROM students WHERE id = ?", (student_id,))
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

    def log_attendance(self, student_name, status='Present'):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO attendance_log (student_name, status) VALUES (?, ?)", (student_name, status))
            conn.commit()

    def get_last_attendance(self, student_name):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM attendance_log WHERE student_name = ? ORDER BY timestamp DESC LIMIT 1", (student_name,))
            result = cursor.fetchone()
            return result[0] if result else None

    def delete_student(self, student_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM students WHERE id = ?", (student_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_calendar_exception(self, date_str, return_default=False):
        from datetime import datetime
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM calendar_exceptions WHERE date = ?", (date_str,))
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

    def set_calendar_exception(self, date_str, status):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO calendar_exceptions (date, status) VALUES (?, ?)", (date_str, status))
            conn.commit()
