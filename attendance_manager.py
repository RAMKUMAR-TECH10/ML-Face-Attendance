from datetime import datetime, timedelta

class AttendanceManager:
    def __init__(self, db_manager, cooldown_minutes=30):
        self.db_manager = db_manager
        self.cooldown_minutes = cooldown_minutes

    def mark_attendance(self, student_name):
        last_time_str = self.db_manager.get_last_attendance(student_name)
        now = datetime.now()
        
        if last_time_str:
            last_time = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S')
            diff = now - last_time
            if diff < timedelta(minutes=self.cooldown_minutes):
                # print(f"Cooldown active for {student_name}. Skipping.")
                return False, "ALREADY RECORDED", last_time_str
        
        # Determine status: Late if after 9:15 AM
        today_9_15am = now.replace(hour=9, minute=15, second=0, microsecond=0)
        status = 'Late' if now > today_9_15am else 'Present'
        
        try:
            self.db_manager.log_attendance(student_name, status=status)
            return True, "SUCCESS", now.strftime('%Y-%m-%d %H:%M:%S')
        except Exception as e:
            return False, "ERROR", str(e)

