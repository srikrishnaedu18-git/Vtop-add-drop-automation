import sqlite3
import os
from datetime import datetime

# Resolve seats.db relative to the project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "seats.db")


def format_timestamp(ts_str):
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d-%m %H:%M:%S")
    except:
        return ts_str

def main():
    if not os.path.exists(DB_PATH):
        print(f"No database found at '{DB_PATH}'. Run the main monitoring script first to populate it.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seat_logs'")
        if not cursor.fetchone():
            print("Table 'seat_logs' does not exist in the database yet.")
            return

        cursor.execute("""
            SELECT id, timestamp, course_name, slot, faculty, available, changed 
            FROM seat_logs 
            ORDER BY timestamp DESC 
            LIMIT 60
        """)
        rows = cursor.fetchall()
    except Exception as e:
        print(f"Error querying database: {e}")
        return
    finally:
        conn.close()

    if not rows:
        print("Database is currently empty.")
        return

    print("\n" + "=" * 115)
    print(f"{'ID':<6} | {'TIMESTAMP':<24} | {'COURSE':<25} | {'SLOT':<8} | {'FACULTY':<25} | {'AVAILABLE':<9} | {'CHANGED?':<8}")
    print("-" * 115)

    for row in rows:
        db_id, timestamp, course_name, slot, faculty, available, changed = row
        
        changed_str = "YES 🔔" if changed else "NO"
        
        course_display = course_name or "Unknown"
        if len(course_display) > 25:
            course_display = course_display[:22] + "..."

        faculty_display = faculty or "Unknown"
        if len(faculty_display) > 25:
            faculty_display = faculty_display[:22] + "..."

        print(f"{db_id:<6} | {format_timestamp(timestamp):<24} | {course_display:<25} | {slot:<8} | {faculty_display:<25} | {available:<9} | {changed_str:<8}")
    print("=" * 115 + "\n")

if __name__ == "__main__":
    main()
