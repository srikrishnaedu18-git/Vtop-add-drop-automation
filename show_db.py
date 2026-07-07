import sqlite3
import json
import os
from datetime import datetime

DB_PATH = "seats.db"

def format_timestamp(ts_str):
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d-%b %Y %I:%M:%S %p")
    except:
        return ts_str

def main():
    if not os.path.exists(DB_PATH):
        print(f"No database found at '{DB_PATH}'. Run the main monitoring script first to populate it.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scrapes'")
        if not cursor.fetchone():
            print("Table 'scrapes' does not exist in the database yet.")
            return

        cursor.execute("""
            SELECT id, timestamp, changed, course_name, slots_json 
            FROM scrapes 
            ORDER BY timestamp DESC 
            LIMIT 40
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

    print("\n" + "=" * 100)
    print(f"{'ID':<4} | {'TIMESTAMP':<24} | {'CHANGE?':<7} | {'COURSE':<35} | {'SLOTS SUMMARY':<20}")
    print("-" * 100)

    for row in rows:
        db_id, timestamp, changed, course_name, slots_json = row
        
        changed_str = "YES 🔔" if changed else "NO (hb)"
        
        # Parse slots summary
        summary = "-"
        if slots_json:
            try:
                slots = json.loads(slots_json)
                parts = []
                for s in slots:
                    avail = s.get("available", "0")
                    parts.append(f"{s.get('slot')}:{avail}")
                summary = ", ".join(parts)
            except:
                summary = "Error parsing slots"
        
        # Truncate summary if too long
        if len(summary) > 40:
            summary = summary[:37] + "..."

        course_display = course_name or "Unknown"
        if len(course_display) > 35:
            course_display = course_display[:32] + "..."

        print(f"{db_id:<4} | {format_timestamp(timestamp):<24} | {changed_str:<7} | {course_display:<35} | {summary}")
    print("=" * 100 + "\n")

if __name__ == "__main__":
    main()
