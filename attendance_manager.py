from datetime import datetime, timedelta

class AttendanceManager:
    def __init__(self, db_manager, cooldown_minutes=30):
        self.db_manager = db_manager
        self.cooldown_minutes = cooldown_minutes

    def mark_attendance(self, student_name):
        last_time_str = self.db_manager.get_last_attendance(student_name)
        now = datetime.now()
        today_date_str = now.strftime('%Y-%m-%d')
        
        if last_time_str:
            try:
                # Convert UTC stored timestamp to local time for correct calendar checking
                last_time_utc = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S')
                local_offset = datetime.now() - datetime.utcnow()
                last_time_local = last_time_utc + local_offset
                
                if last_time_local.strftime('%Y-%m-%d') == today_date_str:
                    return False, "ALREADY RECORDED", last_time_str
            except Exception:
                # Fallback to cooldown check if timestamp parsing fails
                last_time = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S')
                diff = now - last_time
                if diff < timedelta(minutes=self.cooldown_minutes):
                    return False, "ALREADY RECORDED", last_time_str
        
        # Determine status: Late if after 9:15 AM local time
        today_9_15am = now.replace(hour=9, minute=15, second=0, microsecond=0)
        status = 'Late' if now > today_9_15am else 'Present'
        
        try:
            # Save UTC string in database
            timestamp_utc_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            self.db_manager.log_attendance(student_name, status=status, timestamp_str=timestamp_utc_str)
            return True, "SUCCESS", timestamp_utc_str
        except Exception as e:
            return False, "ERROR", str(e)

