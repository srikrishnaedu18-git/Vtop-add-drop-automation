"""
main.py — VIT A&D Portal Automation
Flow:
  1. Login (captcha 1)
  2. Instructions → Start Registration
  3. Progress Info (captcha 2)
  4. Select Discipline Elective → Page 2 → Cyber Security → Proceed
  5. Scrape slot/venue/faculty/available table → print WhatsApp message
"""

import asyncio
import os
import time
import json
import sqlite3

# Set default timezone to IST (Asia/Kolkata)
os.environ['TZ'] = 'Asia/Kolkata'
if hasattr(time, 'tzset'):
    time.tzset()
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ─── Live Log Console Redirection ─────────────────────────────────────────────
GLOBAL_LOG_BUFFER = []

class LiveLogWriter:
    def __init__(self, original_stream, log_buffer):
        self.original_stream = original_stream
        self.log_buffer = log_buffer

    def write(self, message):
        self.original_stream.write(message)
        lines = message.split("\n")
        for line in lines:
            cleaned = line.strip()
            if cleaned:
                ts = datetime.now().strftime("%H:%M:%S")
                self.log_buffer.append({"timestamp": ts, "message": cleaned})
                if len(self.log_buffer) > 100:
                    self.log_buffer.pop(0)

    def flush(self):
        self.original_stream.flush()

import sys
sys.stdout = LiveLogWriter(sys.stdout, GLOBAL_LOG_BUFFER)
sys.stderr = LiveLogWriter(sys.stderr, GLOBAL_LOG_BUFFER)


try:
    from twilio.rest import Client
except ImportError:
    Client = None

from src.captcha_solver import solve_captcha_b64


# ─── CONFIG ───────────────────────────────────────────────────────────────────
USERNAME       = os.getenv("VTOP_USERNAME", "").strip()
PASSWORD       = os.getenv("VTOP_PASSWORD", "").strip()
BASE_URL       = os.getenv("BASE_URL",      "https://vtopreg.vit.ac.in/tablet/")
CHROME_PATH    = os.getenv("CHROME_PATH",   "/usr/bin/google-chrome")
IS_DOCKER      = os.path.exists("/.dockerenv") or os.getenv("PORT") is not None
HEADLESS       = os.getenv("HEADLESS", "true" if IS_DOCKER else "false").lower() == "true"
MAX_RETRIES    = 8
DB_PATH        = os.getenv("DB_PATH", "seats.db").strip()
MONITOR_DELAY_SECONDS = int(os.getenv("MONITOR_DELAY_SECONDS", "30"))
REGISTER       = os.getenv("REGISTER", os.getenv("REGISTER_ENABLED", "false")).lower() == "true"
MODIFY         = os.getenv("MODIFY", os.getenv("MODIFY_ENABLED", "false")).lower() == "true"
CHOSEN_FACULTY = os.getenv("CHOSEN_FACULTY", "").strip()
CHOSEN_SLOT    = os.getenv("CHOSEN_SLOT", "").strip()
PRINT_SCRAPER_DATA = os.getenv("print_scrapper_data_in_terminal", "false").lower() == "true"

SCRAPER_STATUS = {
    "status": "Initializing...",
    "last_run": "Never",
    "error": None
}


def parse_env_list(val):
    if not val:
        return []
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        try:
            return [x.strip() for x in json.loads(val)]
        except Exception:
            pass
    return [x.strip() for x in val.split(",") if x.strip()]

REGISTER_COURSES = parse_env_list(os.getenv("REGISTER_COURSES", ""))
MODIFY_COURSES   = parse_env_list(os.getenv("MODIFY_COURSES", ""))


CONFIG_JSON_PATH = "monitored_courses.json"
COURSES_TO_MONITOR = []

def load_courses_config():
    global COURSES_TO_MONITOR
    if os.path.exists(CONFIG_JSON_PATH):
        try:
            with open(CONFIG_JSON_PATH, "r") as f:
                COURSES_TO_MONITOR = json.load(f)
                print(f"[Config] Loaded {len(COURSES_TO_MONITOR)} courses from {CONFIG_JSON_PATH}")
                return
        except Exception as e:
            print(f"Error loading {CONFIG_JSON_PATH}: {e}")
            
    try:
        COURSES_TO_MONITOR = json.loads(os.getenv("COURSES_TO_MONITOR", "[]"))
        print(f"[Config] Loaded {len(COURSES_TO_MONITOR)} courses from .env")
    except Exception as e:
        print(f"Error parsing COURSES_TO_MONITOR from .env: {e}")
        COURSES_TO_MONITOR = []

load_courses_config()

# Twilio Config
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM        = os.getenv("TWILIO_FROM_NUMBER", "").strip()
MY_PHONE_NUMBER    = os.getenv("MY_PHONE_NUMBER", "").strip()


# ─── Database ─────────────────────────────────────────────────────────────────

def init_db():
    """Create the seat_logs table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS seat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                course_name TEXT,
                slot TEXT,
                faculty TEXT,
                available TEXT,
                changed BOOLEAN
            )
        ''')


def check_and_save_db(course_name: str, slots: list) -> bool:
    """
    Saves each scraped slot to seat_logs according to transition rules.
    Returns True if ANY slot underwent a changed state transition (changed=True),
    which signals that we should trigger a WhatsApp alert.
    """
    has_any_change = False
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        for s in slots:
            slot_name = s["slot"]
            faculty = s["faculty"]
            avail = s["available"]
            
            # Fetch the most recent log for this course/slot/faculty
            cursor.execute('''
                SELECT available FROM seat_logs 
                WHERE course_name = ? AND slot = ? AND faculty = ? 
                ORDER BY timestamp DESC LIMIT 1
            ''', (course_name, slot_name, faculty))
            row = cursor.fetchone()
            
            is_number = avail.lower() not in ("full", "0", "-")
            
            if row is None:
                # First time seeing this slot
                cursor.execute('''
                    INSERT INTO seat_logs (timestamp, course_name, slot, faculty, available, changed)
                    VALUES (?, ?, ?, ?, ?, 1)
                ''', (now_str, course_name, slot_name, faculty, avail))
                has_any_change = True
            else:
                last_avail = row[0]
                if avail != last_avail:
                    # Transition occurred
                    cursor.execute('''
                        INSERT INTO seat_logs (timestamp, course_name, slot, faculty, available, changed)
                        VALUES (?, ?, ?, ?, ?, 1)
                    ''', (now_str, course_name, slot_name, faculty, avail))
                    has_any_change = True
                else:
                    # No transition
                    if is_number:
                        # Log it anyway to track history, but with changed=0
                        cursor.execute('''
                            INSERT INTO seat_logs (timestamp, course_name, slot, faculty, available, changed)
                            VALUES (?, ?, ?, ?, ?, 0)
                        ''', (now_str, course_name, slot_name, faculty, avail))
                    else:
                        # "full" with no transition -> skip inserting to prevent bloat
                        pass
                        
    return has_any_change


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def get_captcha_b64(page) -> str:
    img = page.locator("#captcha_id")
    await img.wait_for(state="visible", timeout=15_000)
    return await img.get_attribute("src")


async def dismiss_swal(page) -> bool:
    """Click OK on SweetAlert popup if visible. Returns True if dismissed."""
    swal = page.locator("div.sweet-alert")
    if await swal.count() > 0 and await swal.is_visible():
        ok_btn = swal.locator("button.confirm")
        if await ok_btn.count() > 0:
            await ok_btn.click()
            try:
                await swal.wait_for(state="hidden", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(800)
            print("    [✓] Dismissed swal OK")
            return True
    return False


async def _dump(page, name="page_dump.html"):
    with open(name, "w", encoding="utf-8") as f:
        f.write(await page.content())
    print(f"  [→] Saved {name}")


# ─── Step 1: Login ────────────────────────────────────────────────────────────

async def login(page) -> bool:
    print("\n[STEP 1] Login...")
    await page.goto(BASE_URL, wait_until="domcontentloaded")

    for i in range(1, MAX_RETRIES + 1):
        print(f"  Captcha attempt {i}/{MAX_RETRIES}...")
        cap = solve_captcha_b64(await get_captcha_b64(page))
        print(f"    → {cap}")

        await page.fill("#username", USERNAME)
        await page.fill("#password", PASSWORD)
        await page.fill("#captchaString", cap)
        await page.click("#loginButton")
        await page.wait_for_timeout(3000)

        if await dismiss_swal(page):
            print(f"    [✗] Invalid captcha — retrying...")
            continue

        btn = page.locator("#loginButton")
        if await btn.count() > 0 and await btn.is_visible():
            print(f"    [?] Still on login — refreshing...")
            refresh = page.locator("#refreshCaptchaProcess").first
            if await refresh.count() > 0:
                await refresh.click()
                await page.wait_for_timeout(1800)
            continue

        print("  [✓] Login OK!")
        return True

    print("[✗] Login failed.")
    return False


# ─── Step 2: Instructions → Start Registration ───────────────────────────────

async def pass_instructions(page) -> bool:
    print("\n[STEP 2] Instructions page...")
    try:
        btn = page.locator("form#checkRegistration button[type=submit]")
        await btn.wait_for(state="visible", timeout=30_000)
        print("  [✓] 'Start Registration' visible")
    except PWTimeout:
        if await page.locator("#captchaStringProgInfo").count() > 0:
            print("  [→] Already on Progress Info, skipping.")
            return True
        await _dump(page, "fail_instructions.html")
        return False

    await btn.click()
    print("  [✓] Clicked 'Start Registration'")
    return True


# ─── Step 3: Progress Info → Captcha 2 ───────────────────────────────────────

async def pass_progress_captcha(page) -> bool:
    print("\n[STEP 3] Progress captcha...")
    try:
        await page.wait_for_selector("#captchaStringProgInfo", timeout=20_000)
    except PWTimeout:
        print("  [→] No 2nd captcha needed, skipping.")
        return True

    for i in range(1, MAX_RETRIES + 1):
        print(f"  Captcha attempt {i}/{MAX_RETRIES}...")
        cap = solve_captcha_b64(await get_captcha_b64(page))
        print(f"    → {cap}")

        await page.fill("#captchaStringProgInfo", cap)
        await page.locator("form#conditionProgress button[type=submit]").click()
        await page.wait_for_timeout(3000)

        inp = page.locator("#captchaStringProgInfo")
        if await inp.count() > 0 and await inp.is_visible():
            print(f"    [✗] Rejected — refreshing...")
            await dismiss_swal(page)
            refresh = page.locator("#refreshCaptchaProcess").first
            if await refresh.count() > 0:
                await refresh.click()
                await page.wait_for_timeout(1800)
            continue

        print("  [✓] Progress captcha OK!")
        return True

    print("[✗] Progress captcha failed.")
    return False


# ─── Step 4: Select DE → Page 2 → Cyber Security → Proceed ───────────────────

async def navigate_to_course(page, course_config) -> bool:
    """Navigates to the specified category, switches to page, and finds the keyword."""
    cat = course_config["category"].upper()
    keyword = course_config["keyword"]
    pg_num = str(course_config.get("page", "1"))
    
    if "MODIFY" in cat or "VIEW" in cat:
        # Navigate via the Modify screen
        print(f"\n[STEP 4] Navigating to View/Modify -> '{keyword}'...")
        try:
            # Click the orange View/Modify button on the dashboard
            await page.locator("span:has-text('View / Modify')").click()
            # Wait for the registered courses table to load
            await page.wait_for_selector("#page-wrapper table", timeout=15000)
            print("  [✓] Registered courses table loaded.")
        except Exception as e:
            print(f"  [!] Failed to load registered courses table: {e}")
            await _dump(page, "fail_modify_table.html")
            return False

        # Find the target course row
        print(f"  [→] Looking for '{keyword}'...")
        try:
            # Wait up to 5 seconds for a row containing the keyword to appear
            await page.locator(f"tr:has-text('{keyword}')").first.wait_for(state="visible", timeout=5000)
        except Exception:
            pass

        rows = await page.locator("tr").all()
        target_row = None
        for row in rows:
            text = await row.inner_text()
            if keyword.lower() in text.lower():
                target_row = row
                break

        if not target_row:
            print(f"  [!] Could not find '{keyword}' in registered courses!")
            await _dump(page, "fail_modify_course_not_found.html")
            return False

        # Click the Modify button in that row
        modify_btn = target_row.locator("button:has-text('Modify')")
        if await modify_btn.count() == 0:
            print(f"  [!] Found '{keyword}', but no Modify button exists!")
            return False

        await modify_btn.click()
        print(f"  [✓] Clicked Modify for '{keyword}'")
        
        # Wait for the slot/OTP page to load (it will have the slot table/OTP input)
        try:
            await page.wait_for_selector("#mailOTP", timeout=15000)
            try:
                await page.locator(".blockUI").wait_for(state="hidden", timeout=10000)
            except Exception:
                pass
            print("  [✓] Modify slot page loaded.")
        except Exception as e:
            print(f"  [!] Modify slot page didn't load: {e}")
            swal_dismissed = await dismiss_swal(page)
            if swal_dismissed:
                print("  [!] Detected and dismissed SweetAlert popup on navigation failure.")
            return False
            
        return True

    print(f"\n[STEP 4] Navigating to {cat} -> Page {pg_num} -> '{keyword}'...")
    
    cat_map = {
        "PC": "#registrationOption1",
        "PE": "#registrationOption2",
        "UC": "#registrationOption3",
        "DE": "#registrationOption4"
    }
    
    if cat not in cat_map:
        print(f"  [!] Unknown category: {cat}")
        return False
        
    radio_selector = cat_map[cat]

    
    # Wait for radio buttons
    try:
        await page.wait_for_selector(radio_selector, timeout=20_000)
    except PWTimeout:
        print("  [!] Radio buttons not found")
        await _dump(page, "fail_radios.html")
        return False

    # Click category radio
    await page.locator(radio_selector).click(force=True)
    print(f"  [✓] Selected {cat}")

    # Snapshot before AJAX
    before = await page.locator("#page-wrapper").inner_html()

    # Click Proceed
    await page.locator("button[onclick*='viewRegistrationOption']").click()
    print("  [→] Clicked Proceed...")

    # Wait for #page-wrapper to change
    try:
        await page.wait_for_function(
            "(b) => document.getElementById('page-wrapper')?.innerHTML !== b",
            arg=before, timeout=20_000
        )
    except PWTimeout:
        pass
    await page.wait_for_timeout(1500)

    # ── Navigate to specified Page (client-side) ──
    if pg_num != "1":
        print(f"  [→] Switching to Page {pg_num}...")
        await page.evaluate(f"getResults2('10','{pg_num}','0','NONE','2')")
        await page.wait_for_timeout(1500)

    # ── Find specific course row and click Proceed ──
    print(f"  [→] Looking for '{keyword}' row...")
    
    found = False
    rows = await page.locator("#page-wrapper tbody tr").all()
    for row in rows:
        text = await row.inner_text()
        if keyword.upper() in text.upper():
            proceed_btn = row.locator("button:has-text('Proceed')")
            if await proceed_btn.count() > 0:
                await proceed_btn.click()
                print(f"  [✓] Clicked Proceed for '{keyword}'")
                found = True
                break

    if not found:
        print(f"  [!] '{keyword}' not found on page {pg_num}")
        await _dump(page, "fail_course_not_found.html")
        return False

    # Wait for slot table to load
    await page.wait_for_timeout(3000)
    return True


# ─── Step 5: Scrape slot table → WhatsApp message ────────────────────────────

async def scrape_and_format(page, keyword: str = None) -> str | None:
    print("\n[STEP 5] Scraping slot table...", end="", flush=True)

    # Wait for the slot table (has columns: Slot, Venue, Faculty, Available)
    try:
        await page.wait_for_selector("#page-wrapper table thead", timeout=15_000)
    except PWTimeout:
        print("\n  [!] No table found")
        await _dump(page, "fail_no_table.html")
        return None

    # Extract course info from header table
    course_name = ""
    header_span = page.locator("#page-wrapper table:first-of-type thead tr:not(.w3-blue) td span").first
    if await header_span.count() > 0:
        course_name = (await header_span.inner_text()).strip()

    if not course_name and keyword:
        course_name = keyword

    # Extract slot rows from the second table (the one with Slot/Venue/Faculty/Available)
    slots = []
    all_tables = await page.locator("#page-wrapper table").all()

    for table in all_tables:
        # Check if this table has "Slot" header
        header_text = await table.inner_text()
        if "Slot" not in header_text or "Venue" not in header_text:
            continue

        rows = await table.locator("tbody tr, thead tr").all()
        for row in rows:
            cells = await row.locator("td").all()
            if len(cells) >= 4:
                slot    = (await cells[0].inner_text()).strip()
                venue   = (await cells[1].inner_text()).strip()
                faculty = (await cells[2].inner_text()).strip()
                avail   = (await cells[3].inner_text()).strip()

                # Skip header-like rows
                if slot and venue and faculty and not slot.startswith("Course"):
                    # Skip "Theory Slots" divider rows
                    if "Theory Slots" in slot or "Lab Slots" in slot:
                        continue
                    slots.append({
                        "slot": slot, "venue": venue,
                        "faculty": faculty, "available": avail,
                    })
        if slots:
            break

    if not slots:
        print("\n  [!] No slot rows extracted")
        await _dump(page, "fail_empty_slots.html")
        return None

    # ── Format WhatsApp message ──
    now = datetime.now().strftime("%d-%m %H:%M:%S")
    lines = [
        f"📚 *{course_name or 'Unknown Course'}*",
        f"🕐 Scraped: {now}",
        "",
    ]
    
    avail_count = sum(1 for s in slots if s["available"].lower() not in ("full", "0", "-"))
    
    if avail_count > 0:
        lines.extend([
            "```",
            f"{'SLOT':<10} {'FACULTY':<20} {'STATUS':<8}",
        ])
        for s in slots:
            # ONLY include the row if it's NOT full
            if s["available"].lower() not in ("full", "0", "-"):
                lines.append(
                    f"{s['slot']:<10} {s['faculty']:<20} {s['available']:<8}"
                )
        lines.extend([
            "```",
            f"\n✅ *{avail_count} slot(s) have seats available!*"
        ])
    else:
        lines.append("❌ *All slots are FULL*")
    msg = "\n".join(lines)

    if avail_count == 0:
        print(" all the fac are full")
    else:
        print(" Done.")
        if PRINT_SCRAPER_DATA:
            print("  [Scraper] Available slots:")
            for s in slots:
                if s["available"].lower() not in ("full", "0", "-"):
                    print(f"    Slot: {s['slot']} | Faculty: {s['faculty']} | Available: {s['available']}")

    return msg, avail_count, course_name or "Unknown Course", slots


LAST_HOURLY_SENT = {}

def format_all_slots_msg(course_name: str, slots: list) -> str:
    now = datetime.now().strftime("%d-%m %H:%M:%S")
    lines = [
        f"📚 *{course_name or 'Unknown Course'}* (Hourly Update)",
        f"🕐 Scraped: {now}",
        "",
        "```",
        f"{'SLOT':<10} {'FACULTY':<20} {'STATUS':<8}",
    ]
    for s in slots:
        lines.append(
            f"{s['slot']:<10} {s['faculty']:<20} {s['available']:<8}"
        )
    lines.append("```")
    
    avail_count = sum(1 for s in slots if s["available"].lower() not in ("full", "0", "-"))
    if avail_count > 0:
        lines.append(f"\n✅ *{avail_count} slot(s) have seats available!*")
    else:
        lines.append("\n❌ *All slots are FULL*")
        
    return "\n".join(lines)


def send_whatsapp_alert(msg_text: str):
    """Sends the formatted text via Twilio WhatsApp sandbox."""
    if not Client:
        print("\n[!] Twilio package not installed. Skipping WhatsApp alert.")
        return

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, MY_PHONE_NUMBER]):
        print("\n[!] Twilio credentials missing in .env. Skipping WhatsApp alert.")
        return

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        # Twilio requires 'whatsapp:' prefix
        from_num = f"whatsapp:{TWILIO_FROM}" if not TWILIO_FROM.startswith("whatsapp:") else TWILIO_FROM
        to_num = f"whatsapp:{MY_PHONE_NUMBER}" if not MY_PHONE_NUMBER.startswith("whatsapp:") else MY_PHONE_NUMBER

        message = client.messages.create(
            body=msg_text,
            from_=from_num,
            to=to_num
        )
        print(f"\n[✓] WhatsApp Alert Sent! (Message SID: {message.sid})")
    except Exception as e:
        print(f"\n[✗] Failed to send WhatsApp Alert: {e}")


async def check_and_trigger_registration(page, course_config: dict, course_name: str, slots: list) -> bool:
    """
    Checks if registering/modifying is enabled for this course and if target slot/faculty is available.
    If so, performs the automated registration/modification.
    Returns True if an automated action was successfully triggered (which should stop the script).
    """
    action = course_config.get("action", "").lower()
    
    # Backward compatibility fallback to global toggles if "action" is not specified in JSON
    if not action:
        course_mode_register = False
        course_mode_modify = False
        if REGISTER_COURSES or MODIFY_COURSES:
            for kw in REGISTER_COURSES:
                if kw.lower() in course_name.lower():
                    course_mode_register = True
                    break
            for kw in MODIFY_COURSES:
                if kw.lower() in course_name.lower():
                    course_mode_modify = True
                    break
        else:
            course_mode_register = REGISTER
            course_mode_modify = MODIFY
            
        if course_mode_register:
            action = "register"
        elif course_mode_modify:
            action = "modify"

    if action not in ("register", "modify"):
        return False

    if action == "register" and not REGISTER:
        return False
    if action == "modify" and not MODIFY:
        return False

    # Check if there are ANY available seats in any slot. If none, return False immediately
    has_any_available = any(s["available"].lower() not in ("full", "0", "-") for s in slots)
    if not has_any_available:
        return False

    # Get target faculty and slot pattern from course_config or fall back to global
    target_faculty_pattern = course_config.get("target_faculty", "")
    if not target_faculty_pattern:
        target_faculty_pattern = CHOSEN_FACULTY
        
    target_slot_pattern = course_config.get("target_slot", "")
    if not target_slot_pattern:
        target_slot_pattern = CHOSEN_SLOT

    print(f"\n[Automator] Checking if target slot pattern '{target_slot_pattern}'"
          f" (preferred faculty '{target_faculty_pattern}') is available for '{course_name}'...")

    # Filter all matching slots with available seats
    matching_slots = []
    for s in slots:
        slot_pattern_match = True
        if target_slot_pattern:
            slot_pattern_match = target_slot_pattern.lower() in s["slot"].lower()
            
        if slot_pattern_match:
            avail_str = s["available"].lower()
            if avail_str not in ("full", "0", "-"):
                matching_slots.append(s)

    if not matching_slots:
        print(f"  [i] No available slots match pattern '{target_slot_pattern}'. Continuing monitoring.")
        return False

    # Match ONLY preferred faculty — if a target_faculty is specified, DO NOT fall back to others
    target_slot = None
    if target_faculty_pattern:
        for s in matching_slots:
            if target_faculty_pattern.lower() in s["faculty"].lower():
                target_slot = s
                break

    if not target_slot:
        if target_faculty_pattern:
            # Preferred faculty has no open seats — do NOT fall back, just keep waiting
            print(f"  [i] Preferred faculty '{target_faculty_pattern}' is not available yet. Waiting...")
            return False
        else:
            # No faculty preference set — pick the first available slot
            target_slot = matching_slots[0]

    print(f"\n🚀 [AUTOMATOR TRIGGERED] Selected slot '{target_slot['slot']}' with faculty '{target_slot['faculty']}' ({target_slot['available']} seats)!")

    # 1. Locate and click the slot radio button
    rows = await page.locator("#page-wrapper table tbody tr").all()
    target_row = None
    radio_locator = None

    for r in rows:
        inner_text = await r.inner_text()
        fac_match = target_slot["faculty"].lower() in inner_text.lower()
        slot_match = target_slot["slot"].lower() in inner_text.lower()

        if fac_match and slot_match:
            radio_name = "courseOption" if action == "modify" else "classnbr1"
            radio = r.locator(f"input[type='radio'][name='{radio_name}']")
            if await radio.count() > 0:
                target_row = r
                radio_locator = radio
                break


    if not radio_locator:
        print(f"  [✗] Could not find the radio button for slot '{target_slot['slot']}' with faculty '{target_slot['faculty']}'.")
        await _dump(page, "fail_automator_radio_not_found.html")
        return False

    print(f"  [→] Clicking slot radio button for '{target_slot['faculty']}'...")
    await radio_locator.click()
    await page.wait_for_timeout(1000)

    # Wait for blockUI loader to be hidden (Ajax call filtering/updating state)
    print("  [→] Waiting for AJAX loader/spinner to complete...")
    try:
        await page.locator(".blockUI").wait_for(state="hidden", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)

    # 2. Flow-specific logic
    swal_text = ""
    if action == "register":
        print("  [→] REGISTER mode active. Selecting Regular (RGR) course option...")
        # Select CourseOption RGR (Regular)
        reg_option = page.locator("input[name='CourseOption'][value='RGR']")
        if await reg_option.count() > 0:
            await reg_option.click()
            await page.wait_for_timeout(1000)
            try:
                await page.locator(".blockUI").wait_for(state="hidden", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(500)
        else:
            print("  [i] CourseOption radio button (RGR) not found, proceeding anyway.")

        # Click Register button
        register_btn = page.locator("button:has-text('Register')")
        print("  [→] Clicking Register button...")
        await register_btn.click()
        await page.wait_for_timeout(3000)

        # Handle sweetalert
        swal = page.locator("div.sweet-alert.visible")
        if await swal.count() > 0 and await swal.is_visible():
            swal_text = await swal.inner_text()
            print(f"  [VTOP Result] {swal_text}")
            ok_btn = swal.locator("button.confirm")
            if await ok_btn.count() > 0:
                await ok_btn.click()

        await page.screenshot(path="registration_result.png")

        now_str = datetime.now().strftime("%d-%m %H:%M:%S")
        success_msg = (
            f"🎉 *Course Registration Successful!*\n"
            f"📚 Course: {course_name}\n"
            f"👤 Faculty: {target_slot['faculty']}\n"
            f"⚡ Slot: {target_slot['slot']}\n"
            f"🕐 Completed At: {now_str}\n"
            f"📝 Portal response: {swal_text or 'No SweetAlert detected. Check screenshot.'}"
        )
        send_whatsapp_alert(success_msg)
        print("\n[✓] Automated Registration finished. Terminating script.")
        os._exit(0)

    elif action == "modify":
        print("  [→] MODIFY mode active. Extracting OTP reference prefix...")
        # Extract the Prefix from the DOM
        row = page.locator("tr:has(#mailOTP)")
        spans = await row.locator("span").all()
        screen_prefix = None
        for s in spans:
            txt = await s.inner_text()
            if "-" in txt:
                screen_prefix = txt.replace("-", "").strip()
                break

        if not screen_prefix:
            print("  [✗] Could not locate OTP Reference prefix in the #mailOTP row.")
            await _dump(page, "fail_modify_otp_prefix_not_found.html")
            return False

        print(f"  [SCREEN] OTP Prefix required: {screen_prefix}")
        print("  [→] Fetching OTP from Gmail (polling)...")

        # Import get_vtop_otp dynamically
        from src.fetch_otp import get_vtop_otp
        email_prefix, email_code = get_vtop_otp(max_wait_seconds=120, expected_prefix=screen_prefix)

        if not email_prefix or not email_code:
            print("  [✗] Failed to fetch OTP from Gmail.")
            return False

        print(f"  [✓] Prefixes Match! Filling OTP: {email_code}")
        await page.fill("#mailOTP", email_code)

        # Click the Update button
        update_btn = page.locator("button:has-text('Update')")
        print("  [→] Clicking Update button...")
        await update_btn.click()
        await page.wait_for_timeout(3000)

        # Handle sweetalert
        swal = page.locator("div.sweet-alert.visible")
        if await swal.count() > 0 and await swal.is_visible():
            swal_text = await swal.inner_text()
            print(f"  [VTOP Result] {swal_text}")
            ok_btn = swal.locator("button.confirm")
            if await ok_btn.count() > 0:
                await ok_btn.click()

        await page.screenshot(path="modification_result.png")

        now_str = datetime.now().strftime("%d-%m %H:%M:%S")
        success_msg = (
            f"🎉 *Course Modification Successful!*\n"
            f"📚 Course: {course_name}\n"
            f"👤 Faculty: {target_slot['faculty']}\n"
            f"⚡ Slot: {target_slot['slot']}\n"
            f"🕐 Completed At: {now_str}\n"
            f"📝 Portal response: {swal_text or 'No SweetAlert detected. Check screenshot.'}"
        )
        send_whatsapp_alert(success_msg)
        print("\n[✓] Automated Modification finished. Terminating script.")
        os._exit(0)

    return False


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run():
    if not USERNAME or not PASSWORD:
        print("ERROR: Set VTOP_USERNAME and VTOP_PASSWORD in .env!")
        return

    print(f"[Config] USER={USERNAME} | HEADLESS={HEADLESS} | DELAY={MONITOR_DELAY_SECONDS}s")
    print(f"[Config] Monitoring {len(COURSES_TO_MONITOR)} course(s):")
    for i, c in enumerate(COURSES_TO_MONITOR, 1):
        action = c.get('action') or 'monitor'
        fac = c.get('target_faculty') or 'Any'
        slot = c.get('target_slot') or 'Any'
        print(f"  {i}. {c.get('keyword')} [{c.get('category')}] | Action: {action} | Target: {fac} / {slot}")



    async with async_playwright() as pw:
        chrome_path = CHROME_PATH if CHROME_PATH and os.path.exists(CHROME_PATH) else None
        browser = await pw.chromium.launch(
            executable_path=chrome_path, headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        
        while True:
            SCRAPER_STATUS["status"] = "Active 🚀"
            SCRAPER_STATUS["last_run"] = datetime.now().strftime("%d-%m %H:%M:%S")
            SCRAPER_STATUS["error"] = None
            print("\n" + "═" * 60)
            print("[SESSION START] Starting new browser context...")
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()

            try:
                # ── 1. Full Login Flow ──
                if not await login(page): raise Exception("Login failed")
                if not await pass_instructions(page): raise Exception("Instructions failed")
                if not await pass_progress_captcha(page): raise Exception("Progress failed")

                # ── 2. Inner Continuous Monitoring Loop ──
                if not COURSES_TO_MONITOR:
                    print("ERROR: No courses configured in COURSES_TO_MONITOR in .env")
                    return

                if len(COURSES_TO_MONITOR) == 1:
                    # ── Single Course Optimized Loop (Direct Refresh via Go Back) ──
                    course_config = COURSES_TO_MONITOR[0]
                    keyword = course_config["keyword"]
                    pg_num = str(course_config.get("page", 1))

                    print(f"[Mode] Single course detected. Optimizing refresh logic.")
                    if not await navigate_to_course(page, course_config):
                        raise Exception("Failed to navigate to course. Session likely expired.")

                    current_config_snapshot = json.dumps(COURSES_TO_MONITOR)
                    while True:
                        if json.dumps(COURSES_TO_MONITOR) != current_config_snapshot:
                            print("[Config] Courses configuration changed! Exiting inner loop to reload...")
                            break
                        print(f"\n[--- Monitoring Iteration @ {datetime.now().strftime('%H:%M:%S')} ---]")
                        
                        msg_data = await scrape_and_format(page, keyword)
                        if msg_data:
                            msg, avail_count, course_name, slots = msg_data
                            # Always print extracted data
                            for s in slots:
                                avail_display = s['available']
                                print(f"  Slot: {s['slot']:<10} | Faculty: {s['faculty']:<22} | Available: {avail_display}")

                            init_db()
                            is_changed = check_and_save_db(course_name, slots)

                            current_hour_str = datetime.now().strftime("%Y-%m-%d %H")
                            is_hourly = LAST_HOURLY_SENT.get(course_name) != current_hour_str

                            if is_hourly:
                                LAST_HOURLY_SENT[course_name] = current_hour_str
                                print(f"\n[!] Hourly update trigger for '{course_name}'! Sending full faculty list...")
                                hourly_msg = format_all_slots_msg(course_name, slots)
                                send_whatsapp_alert(hourly_msg)
                            elif is_changed and avail_count > 0:
                                print("\n[!] Availability CHANGED! Triggering WhatsApp API...")
                                send_whatsapp_alert(msg)
                            else:
                                print("\n[i] No change in availability. Not sending WhatsApp.")

                            await check_and_trigger_registration(page, course_config, course_name, slots)
                        else:
                            print("\n[!] Could not extract slot data.")


                        # Click Go Back to return to course list page
                        print("  [→] Clicking Go Back to list page...")
                        await dismiss_swal(page)
                        back_btn = page.locator("button:has-text('Go Back')")
                        await back_btn.wait_for(state="visible", timeout=10_000)
                        await back_btn.click()
                        
                        # Wait for list page (Proceed button to be attached in DOM)
                        await page.wait_for_selector("button:has-text('Proceed')", state="attached", timeout=10_000)
                        
                        # VTOP might default back to Page 1, so if pg_num is not 1, we manually force-switch page
                        if pg_num != "1":
                            print(f"  [→] Forcing switch to Page {pg_num}...")
                            await page.evaluate(f"getResults2('10','{pg_num}','0','NONE','2')")
                            await page.wait_for_timeout(1000)
                        
                        # Now wait for the specific course's Proceed button to be visible
                        target_btn = page.locator(f"tr:has-text('{keyword}') button:has-text('Proceed')")
                        await target_btn.wait_for(state="visible", timeout=10_000)
                        
                        print(f"  [zzz] Sleeping {MONITOR_DELAY_SECONDS} seconds...")
                        SCRAPER_STATUS["status"] = "Sleeping 💤"
                        await asyncio.sleep(MONITOR_DELAY_SECONDS)

                        # Click Proceed on the course again to go back to slot table
                        print(f"  [→] Clicking Proceed for '{keyword}'...")
                        await target_btn.click()

                        # Wait for slot table to load
                        await page.wait_for_timeout(3000)

                else:
                    # ── Multi-Course Sequential Loop (Default behavior) ──
                    current_config_snapshot = json.dumps(COURSES_TO_MONITOR)
                    while True:
                        if json.dumps(COURSES_TO_MONITOR) != current_config_snapshot:
                            print("[Config] Courses configuration changed! Exiting inner loop to reload...")
                            break
                        print(f"\n[--- Monitoring Iteration @ {datetime.now().strftime('%H:%M:%S')} ---]")
                        
                        for course_config in COURSES_TO_MONITOR:
                            print(f"\n[>] Checking {course_config['category']}: {course_config['keyword']}")
                            
                            if not await navigate_to_course(page, course_config):
                                raise Exception("Failed to navigate to course. Session likely expired.")

                            msg_data = await scrape_and_format(page, course_config.get("keyword"))
                            if msg_data:
                                msg, avail_count, course_name, slots = msg_data
                                # Always print extracted data
                                for s in slots:
                                    print(f"  Slot: {s['slot']:<10} | Faculty: {s['faculty']:<22} | Available: {s['available']}")

                                init_db()
                                is_changed = check_and_save_db(course_name, slots)

                                current_hour_str = datetime.now().strftime("%Y-%m-%d %H")
                                is_hourly = LAST_HOURLY_SENT.get(course_name) != current_hour_str

                                if is_hourly:
                                    LAST_HOURLY_SENT[course_name] = current_hour_str
                                    print(f"\n[!] Hourly update trigger for '{course_name}'! Sending full faculty list...")
                                    hourly_msg = format_all_slots_msg(course_name, slots)
                                    send_whatsapp_alert(hourly_msg)
                                elif is_changed and avail_count > 0:
                                    print("\n[!] Availability CHANGED! Triggering WhatsApp API...")
                                    send_whatsapp_alert(msg)
                                else:
                                    print("\n[i] No change in availability. Not sending WhatsApp.")

                                await check_and_trigger_registration(page, course_config, course_name, slots)
                            else:
                                print("\n[!] Could not extract slot data.")


                            # Determine if we need to return to Home dashboard for the next course
                            next_idx = (COURSES_TO_MONITOR.index(course_config) + 1) % len(COURSES_TO_MONITOR)
                            next_course = COURSES_TO_MONITOR[next_idx]
                            next_cat = next_course["category"].upper()
                            need_home = not ("MODIFY" in next_cat or "VIEW" in next_cat)

                            if need_home:
                                print("\n[→] Returning to Home dashboard...")
                                await dismiss_swal(page)
                                try:
                                    await page.locator(".blockUI").wait_for(state="hidden", timeout=10000)
                                except Exception:
                                    pass
                                home_btn = page.locator("#homeIcon")
                                if await home_btn.count() > 0:
                                    await home_btn.click()
                                    await page.wait_for_timeout(2000)
                                else:
                                    raise Exception("Home icon not found. Session must be dead.")
                            else:
                                print(f"\n[→] Skipping Home return (direct navigation to '{next_course['keyword']}' permitted)...")
                                await dismiss_swal(page)

                        print(f"\n[zzz] All courses checked. Sleeping {MONITOR_DELAY_SECONDS} seconds...")
                        SCRAPER_STATUS["status"] = "Sleeping 💤"
                        await asyncio.sleep(MONITOR_DELAY_SECONDS)

            except Exception as e:
                SCRAPER_STATUS["status"] = "Crashed ❌"
                SCRAPER_STATUS["error"] = str(e)
                print(f"\n[!] Session crashed or expired: {e}")
                await page.screenshot(path="error_screenshot.png")
                print("    Restarting a fresh session in 5 seconds...")
                await asyncio.sleep(5)
            finally:
                await context.close()


# ─── FastAPI Web Server (For Render.com) ──────────────────────────────────────

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from contextlib import asynccontextmanager
except ImportError:
    pass

def convert_utc_to_ist(utc_str):
    if not utc_str:
        return utc_str
    import time
    from datetime import datetime, timedelta
    is_utc = (time.timezone == 0)
    if not is_utc:
        return utc_str
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
        dt_ist = dt + timedelta(hours=5, minutes=30)
        return dt_ist.strftime("%Y-%m-%d %H:%M:%S")
    except:
        try:
            dt = datetime.strptime(utc_str, "%d-%m %H:%M:%S")
            dt_ist = dt + timedelta(hours=5, minutes=30)
            return dt_ist.strftime("%d-%m %H:%M:%S")
        except:
            return utc_str

def get_absolute_latest_scraped_time():
    if not os.path.exists(DB_PATH):
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT timestamp FROM seat_logs ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            return convert_utc_to_ist(row[0]) if row else None
    except Exception as e:
        print(f"DB Read Error: {e}")
        return None

def get_dashboard_status():
    db_last_run = get_absolute_latest_scraped_time()
    last_run = db_last_run if db_last_run else SCRAPER_STATUS.get("last_run")
    if not last_run:
        last_run = "-"
        
    status_text = "Active 🚀"
    if SCRAPER_STATUS.get("status") == "Crashed ❌":
        status_text = "Crashed ❌"
        
    error = SCRAPER_STATUS.get("error")
    
    return {
        "status": status_text,
        "last_run": last_run,
        "error": error
    }

def get_latest_status_by_slot():
    if not os.path.exists(DB_PATH):
        return []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT t1.* FROM seat_logs t1
                INNER JOIN (
                    SELECT course_name, slot, faculty, MAX(timestamp) as max_ts
                    FROM seat_logs
                    GROUP BY course_name, slot, faculty
                ) t2 ON t1.course_name = t2.course_name 
                    AND t1.slot = t2.slot 
                    AND t1.faculty = t2.faculty 
                    AND t1.timestamp = t2.max_ts
                ORDER BY t1.course_name ASC, t1.slot ASC
            ''')
            rows = []
            for r in cursor.fetchall():
                d = dict(r)
                if d.get("timestamp"):
                    d["timestamp"] = convert_utc_to_ist(d["timestamp"])
                rows.append(d)
            return rows
    except Exception as e:
        print(f"DB Read Error: {e}")
        return []

def get_recent_transitions():
    if not os.path.exists(DB_PATH):
        return []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM seat_logs ORDER BY timestamp DESC LIMIT 50")
            rows = []
            for r in cursor.fetchall():
                d = dict(r)
                if d.get("timestamp"):
                    d["timestamp"] = convert_utc_to_ist(d["timestamp"])
                rows.append(d)
            return rows
    except Exception as e:
        print(f"DB Read Error: {e}")
        return []

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the monitoring loop in the background
    task = asyncio.create_task(run())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard():
    d_status = get_dashboard_status()
    status_text = d_status["status"]
    last_run = d_status["last_run"]
    error = d_status["error"]
    
    badge_class = "badge-sleeping"
    if "Active" in status_text:
        badge_class = "badge-active"
    elif "Crashed" in status_text or error:
        badge_class = "badge-error"
        
    status_val = status_text
    if error:
        status_val += f" <span style='font-size: 14px; font-weight: normal; color: var(--danger-color);'>({error})</span>"

    # Monitoring Config details
    mode_text = "Multi-Course Sequential" if len(COURSES_TO_MONITOR) > 1 else "Single-Course Optimized"
    register_switch = "ENABLED (True)" if REGISTER else "DISABLED (False)"
    modify_switch = "ENABLED (True)" if MODIFY else "DISABLED (False)"
    subjects_list = "<br>".join([f"• {c['keyword']}" for c in COURSES_TO_MONITOR]) if COURSES_TO_MONITOR else "None"
    
    # Generate latest seat rows
    current_seats = get_latest_status_by_slot()
    current_seats_rows = ""
    if not current_seats:
        current_seats_rows = "<tr><td colspan='5' style='text-align: center; color: var(--text-secondary);'>No data scraped yet.</td></tr>"
    else:
        for s in current_seats:
            course = s.get("course_name") or "Unknown"
            slot = s.get("slot") or "-"
            fac = s.get("faculty") or "-"
            avail = s.get("available") or "-"
            ts = s.get("timestamp") or "-"
            
            avail_style = "color: var(--text-primary);"
            if avail.lower() in ("full", "0", "-"):
                avail_style = "color: var(--danger-color); font-weight: 500;"
            else:
                avail_style = "color: #3fb950; font-weight: 600;"
                
            current_seats_rows += f"""
            <tr>
                <td class="highlight">{course}</td>
                <td><code>{slot}</code></td>
                <td>{fac}</td>
                <td style="{avail_style}">{avail}</td>
                <td style="color: var(--text-secondary); font-size: 13px;">{ts}</td>
            </tr>
            """

    # Generate log rows
    logs = get_recent_transitions()
    log_rows = ""
    if not logs:
        log_rows = "<tr><td colspan='6' style='text-align: center; color: var(--text-secondary);'>No activity logged yet.</td></tr>"
    else:
        for l in logs:
            ts = l.get("timestamp") or "-"
            course = l.get("course_name") or "Unknown"
            slot = l.get("slot") or "-"
            fac = l.get("faculty") or "-"
            avail = l.get("available") or "-"
            changed = l.get("changed") or 0
            
            changed_badge = '<span class="changed-badge">YES 🔔</span>' if changed else '<span style="color: var(--text-secondary);">NO</span>'
            
            avail_style = ""
            if avail.lower() in ("full", "0", "-"):
                avail_style = "color: var(--danger-color);"
            else:
                avail_style = "color: #3fb950; font-weight: 500;"
                
            log_rows += f"""
            <tr>
                <td style="color: var(--text-secondary); font-size: 13px;">{ts}</td>
                <td>{course}</td>
                <td><code>{slot}</code></td>
                <td>{fac}</td>
                <td style="{avail_style}">{avail}</td>
                <td>{changed_badge}</td>
            </tr>
            """

    profile_name = os.getenv("DISPLAY_NAME", "Sri Krishna R")
    if not profile_name:
        profile_name = "Sri Krishna R"
    parts = profile_name.split()
    if len(parts) >= 2:
        profile_initials = (parts[0][0] + parts[-1][0]).upper()
    elif len(parts) == 1:
        profile_initials = parts[0][:2].upper()
    else:
        profile_initials = "SK"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>VTOP Scraper Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <script>
            (function() {{
                const savedTheme = localStorage.getItem('theme');
                if (savedTheme === 'light') {{
                    document.documentElement.classList.add('light-theme');
                }}
            }})();
        </script>
        <style>
            :root {{
                --bg-color: #0d1117;
                --card-bg: rgba(22, 27, 34, 0.85);
                --border-color: #30363d;
                --text-primary: #c9d1d9;
                --text-secondary: #8b949e;
                --accent-color: #58a6ff;
                --danger-color: #da3637;
                --modal-bg: #161b22;
                --input-bg: rgba(0, 0, 0, 0.25);
                --input-color: #ffffff;
                --table-header-bg: #161b22;
            }}
            :root.light-theme {{
                --bg-color: #f6f8fa;
                --card-bg: rgba(255, 255, 255, 0.95);
                --border-color: #d0d7de;
                --text-primary: #24292f;
                --text-secondary: #57606a;
                --accent-color: #0969da;
                --danger-color: #cf222e;
                --modal-bg: #ffffff;
                --input-bg: #ffffff;
                --input-color: #24292f;
                --table-header-bg: #eaeef2;
            }}
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'Plus Jakarta Sans', sans-serif;
                background-color: var(--bg-color);
                color: var(--text-primary);
                margin: 0; padding: 20px;
                display: flex; flex-direction: column; align-items: center;
            }}
            .container {{ width: 100%; max-width: 1200px; }}
            header {{
                display: flex; justify-content: space-between; align-items: center;
                margin-bottom: 30px; border-bottom: 1px solid var(--border-color); padding-bottom: 20px;
            }}
            h1 {{
                margin: 0; font-size: 24px; font-weight: 700;
                background: linear-gradient(45deg, #58a6ff, #bc8cff);
                -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            }}
            .header-right {{ display: flex; align-items: center; gap: 12px; }}
            #refresh-indicator {{
                font-size: 11px; color: var(--text-secondary);
                padding: 4px 8px; border: 1px solid var(--border-color);
                border-radius: 20px;
            }}
            .badge {{ padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
            .badge-active  {{ background: rgba(35,134,54,.2);  color: #3fb950; border: 1px solid rgba(63,185,80,.3); }}
            .badge-sleeping{{ background: rgba(240,139,0,.2);  color: #f08b00; border: 1px solid rgba(240,139,0,.3); }}
            .badge-error   {{ background: rgba(218,54,55,.2);  color: #f85149; border: 1px solid rgba(248,81,73,.3); }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-bottom: 30px; }}
            .card {{ background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; }}
            .card h2 {{ margin-top: 0; font-size: 15px; color: var(--text-secondary); font-weight: 500; margin-bottom: 15px; }}
            .status-val {{ font-size: 26px; font-weight: 700; margin: 8px 0; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ text-align: left; padding: 11px 12px; border-bottom: 1px solid var(--border-color); font-size: 14px; }}
            th {{ color: var(--text-secondary); font-weight: 500; }}
            tr:hover {{ background: rgba(255,255,255,.02); }}
            .avail-open {{ color: #3fb950; font-weight: 600; }}
            .avail-full {{ color: var(--danger-color); font-weight: 500; }}
            .highlight {{ font-weight: 600; color: #58a6ff; }}
            .changed-badge {{ background: rgba(88,166,255,.15); color: #58a6ff; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
            .pulse {{ animation: pulse 2s infinite; }}
            @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.4; }} }}
            code {{ background: rgba(255,255,255,.07); padding: 2px 6px; border-radius: 4px; font-size: 12px; }}

            /* Pipeline Flow layout (AWS Glue/CodePipeline Style) */
            .pipeline-container {{
                display: flex;
                align-items: center;
                gap: 15px;
                overflow-x: auto;
                padding: 20px 10px;
                min-height: 180px;
                scroll-behavior: smooth;
            }}
            .pipeline-card {{
                flex: 0 0 240px;
                background: var(--card-bg);
                border: 1px solid var(--border-color);
                border-radius: 10px;
                padding: 15px;
                position: relative;
                cursor: grab;
                transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s;
                user-select: none;
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            }}
            .pipeline-card:hover {{
                border-color: var(--accent-color);
                transform: translateY(-3px);
                box-shadow: 0 6px 16px rgba(88, 166, 255, 0.15);
            }}
            .pipeline-card:active {{
                cursor: grabbing;
            }}
            .pipeline-card.dragging {{
                opacity: 0.4;
                border: 1px dashed var(--accent-color);
                background: rgba(88, 166, 255, 0.05);
            }}
            .pipeline-card-header {{
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 10px;
            }}
            .pipeline-step-badge {{
                background: rgba(255,255,255,0.06);
                border: 1px solid var(--border-color);
                color: var(--text-secondary);
                font-size: 10px;
                padding: 2px 6px;
                border-radius: 10px;
                font-weight: 600;
            }}
            .pipeline-card-title {{
                font-weight: 700;
                font-size: 14px;
                color: #fff;
                margin-bottom: 8px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }}
            .pipeline-card-details {{
                font-size: 12px;
                color: var(--text-secondary);
                line-height: 1.5;
            }}
            .pipeline-card-details strong {{
                color: var(--text-primary);
            }}
            .pipeline-badge-action {{
                font-size: 10px;
                font-weight: 700;
                padding: 2px 6px;
                border-radius: 4px;
                text-transform: uppercase;
            }}
            .badge-action-modify {{ background: rgba(188, 140, 255, 0.15); color: #bc8cff; border: 1px solid rgba(188, 140, 255, 0.3); }}
            .badge-action-register {{ background: rgba(56, 139, 253, 0.15); color: #58a6ff; border: 1px solid rgba(56, 139, 253, 0.3); }}
            .badge-action-monitor {{ background: rgba(139, 148, 158, 0.15); color: var(--text-secondary); border: 1px solid rgba(139, 148, 158, 0.3); }}

            /* Connectors between pipeline cards */
            .pipeline-arrow {{
                flex: 0 0 32px;
                display: flex;
                align-items: center;
                justify-content: center;
                color: var(--text-secondary);
                font-size: 20px;
                font-weight: bold;
                pointer-events: none;
            }}
            
            /* Dotted Add Card */
            .pipeline-add-card {{
                flex: 0 0 240px;
                border: 2px dashed var(--border-color);
                border-radius: 10px;
                background: transparent;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                height: 130px;
                color: var(--text-secondary);
                cursor: pointer;
                transition: 0.2s;
            }}
            .pipeline-add-card:hover {{
                border-color: var(--accent-color);
                color: var(--accent-color);
                background: rgba(88, 166, 255, 0.03);
            }}
            .pipeline-add-icon {{
                font-size: 32px;
                margin-bottom: 8px;
            }}
            
            /* Modal Overlay Background */
            .modal-backdrop {{
                position: fixed;
                top: 0; left: 0; right: 0; bottom: 0;
                background: rgba(0,0,0,0.75);
                backdrop-filter: blur(4px);
                z-index: 1000;
                display: flex;
                align-items: center;
                justify-content: center;
                animation: fadeIn 0.2s ease-out;
            }}
            .modal-content {{
                background: var(--modal-bg);
                border: 1px solid var(--border-color);
                border-radius: 12px;
                width: 90%;
                max-width: 500px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.5);
                overflow: hidden;
                animation: slideUp 0.2s ease-out;
            }}
            .modal-header {{
                padding: 16px 20px;
                border-bottom: 1px solid var(--border-color);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }}
            .modal-header h3 {{
                margin: 0;
                font-size: 16px;
                font-weight: 600;
            }}
            .slider-btn {{
                background: var(--card-bg);
                border: 1px solid var(--border-color);
                color: var(--text-primary);
                border-radius: 50%;
                width: 32px;
                height: 32px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 14px;
                transition: background 0.2s, color 0.2s;
            }}
            .slider-btn:hover {{
                background: var(--border-color);
                color: var(--text-primary);
            }}
            .close-btn {{
                background: none;
                border: none;
                color: var(--text-secondary);
                font-size: 24px;
                cursor: pointer;
                transition: color 0.2s;
            }}
            .close-btn:hover {{
                color: var(--text-primary);
            }}
            .btn-secondary {{
                background: var(--bg-color);
                border: 1px solid var(--border-color);
                color: var(--text-primary);
                padding: 10px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-weight: 600;
                font-size: 13px;
                transition: background 0.2s;
            }}
            .btn-secondary:hover {{
                background: var(--border-color);
            }}
            .btn-primary {{
                background: var(--accent-color);
                border: 1px solid var(--accent-color);
                color: #fff;
                padding: 10px 20px;
                border-radius: 6px;
                cursor: pointer;
                font-weight: 600;
                font-size: 13px;
                transition: opacity 0.2s;
            }}
            .btn-primary:hover {{
                opacity: 0.9;
            }}
            .btn-danger {{
                background: rgba(218,54,55,0.15);
                border: 1px solid rgba(218,54,55,0.3);
                color: var(--danger-color);
                padding: 10px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-weight: 600;
                font-size: 13px;
                transition: background 0.2s;
            }}
            .btn-danger:hover {{
                background: rgba(218,54,55,0.25);
            }}
            .modal-body {{
                padding: 20px;
            }}
            .form-group {{
                margin-bottom: 16px;
            }}
            .form-group label {{
                display: block;
                font-size: 12px;
                color: var(--text-secondary);
                margin-bottom: 6px;
                font-weight: 500;
            }}
            .form-group input, .form-group select {{
                width: 100%;
                background: var(--input-bg);
                border: 1px solid var(--border-color);
                border-radius: 6px;
                color: var(--input-color);
                padding: 10px 12px;
                font-family: inherit;
                font-size: 14px;
                outline: none;
                transition: border-color 0.2s;
            }}
            select option {{
                background-color: var(--modal-bg) !important;
                color: var(--text-primary) !important;
            }}
            .form-group input:focus, .form-group select:focus {{
                border-color: var(--accent-color);
            }}
            .form-group-row {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 15px;
            }}
            .modal-footer {{
                padding: 16px 20px;
                border-top: 1px solid var(--border-color);
                display: flex;
                justify-content: space-between;
                align-items: center;
                background: var(--bg-color);
            }}
            @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
            @keyframes slideUp {{ from {{ transform: translateY(20px); }} to {{ transform: translateY(0); }} }}

            /* Profile & Environment Modal CSS */
            .profile-trigger {{
                display: flex;
                align-items: center;
                gap: 8px;
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border-color);
                border-radius: 20px;
                padding: 4px 12px 4px 6px;
                cursor: pointer;
                transition: 0.2s;
                user-select: none;
                margin-left: 8px;
            }}
            .profile-trigger:hover {{
                background: rgba(255, 255, 255, 0.1);
                border-color: var(--accent-color);
            }}
            .profile-avatar {{
                width: 24px;
                height: 24px;
                background: linear-gradient(135deg, #58a6ff, #bc8cff);
                color: #fff;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 11px;
                font-weight: 700;
            }}
            .profile-name {{
                font-size: 13px;
                font-weight: 500;
                color: var(--text-primary);
            }}
            
            /* Render-style Env Rows */
            .env-row {{
                display: flex;
                align-items: center;
                gap: 15px;
                margin-bottom: 12px;
                background: rgba(255, 255, 255, 0.01);
                border: 1px solid var(--border-color);
                border-radius: 6px;
                padding: 8px 12px;
            }}
            .env-key {{
                flex: 0 0 200px;
                font-family: monospace;
                font-size: 13px;
                color: var(--text-primary);
                font-weight: 600;
                word-break: break-all;
            }}
            .env-val-container {{
                flex: 1;
                position: relative;
                display: flex;
                align-items: center;
            }}
            .env-val-container input, .env-val-container select {{
                width: 100%;
                background: var(--input-bg) !important;
                border: 1px solid var(--border-color) !important;
                border-radius: 4px !important;
                color: var(--input-color) !important;
                padding: 8px 32px 8px 10px !important;
                font-size: 13px !important;
                font-family: inherit !important;
            }}
            .env-val-container select {{
                padding-right: 10px !important;
            }}
            .env-val-container input:focus {{
                border-color: var(--accent-color) !important;
                outline: none;
            }}
            .password-toggle-btn {{
                position: absolute;
                right: 8px;
                background: none;
                border: none;
                color: var(--text-secondary);
                cursor: pointer;
                font-size: 16px;
                padding: 4px;
                display: flex;
                align-items: center;
                justify-content: center;
            }}
            .password-toggle-btn:hover {{
                color: #fff;
            }}
            
            /* Accordion Help styles */
            .help-toggle {{
                background: none;
                border: none;
                color: var(--accent-color);
                font-size: 12px;
                cursor: pointer;
                padding: 4px 0;
                display: flex;
                align-items: center;
                gap: 4px;
                font-weight: 500;
                outline: none;
            }}
            .help-toggle:hover {{
                text-decoration: underline;
            }}
            .help-content {{
                background: rgba(255, 255, 255, 0.03);
                border-left: 2px solid var(--accent-color);
                padding: 10px 15px;
                margin-top: 8px;
                border-radius: 0 6px 6px 0;
                font-size: 12px;
                color: var(--text-secondary);
                line-height: 1.5;
            }}
            .help-content ol, .help-content ul {{
                margin: 6px 0;
                padding-left: 20px;
            }}
            .help-content li {{
                margin-bottom: 4px;
            }}

            /* Theme Toggle Switch CSS */
            .theme-toggle-btn {{
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid var(--border-color);
                border-radius: 50%;
                width: 34px;
                height: 34px;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                transition: background 0.2s, border-color 0.2s, color 0.2s, transform 0.2s;
                color: var(--text-primary);
                padding: 0;
                outline: none;
                margin-left: 8px;
            }}
            .theme-toggle-btn:hover {{
                background: rgba(255, 255, 255, 0.1);
                border-color: var(--accent-color);
                color: var(--accent-color);
                transform: scale(1.05);
            }}
            .theme-icon {{
                width: 18px;
                height: 18px;
                transition: transform 0.3s ease;
            }}
            
            /* Toggle visibility based on light-theme class on root element */
            .light-theme .sun-icon {{
                display: none;
            }}
            :root:not(.light-theme) .moon-icon {{
                display: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>FALL SEMESTER ADD/DROP 2026-27</h1>
                <div class="header-right">
                    <span id="refresh-indicator">⟳ Auto-refresh: 10s</span>
                    <span id="status-badge" class="badge {badge_class}">{status_text}</span>
                    <button id="theme-toggle-btn" onclick="toggleTheme()" class="theme-toggle-btn" title="Toggle Light/Dark Theme">
                        <svg class="theme-icon sun-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="12" cy="12" r="5"></circle>
                            <line x1="12" y1="1" x2="12" y2="3"></line>
                            <line x1="12" y1="21" x2="12" y2="23"></line>
                            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
                            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
                            <line x1="1" y1="12" x2="3" y2="12"></line>
                            <line x1="21" y1="12" x2="23" y2="12"></line>
                            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
                            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
                        </svg>
                        <svg class="theme-icon moon-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
                        </svg>
                    </button>
                    <div class="profile-trigger" onclick="openEnvModal()">
                        <div class="profile-avatar">{profile_initials}</div>
                        <span class="profile-name">{profile_name}</span>
                    </div>
                </div>
            </header>

            <div class="grid">
                <div class="card">
                    <h2>Scraper Status</h2>
                    <div class="status-val" id="status-val">{status_val}</div>
                    <div style="color:var(--text-secondary);font-size:12px;">Last Run: <span id="last-run">{last_run}</span></div>
                </div>
                <div class="card">
                    <h2>Monitoring Config</h2>
                    <div style="font-size:14px;line-height:1.6;">
                        <strong>Mode:</strong> {mode_text}<br>
                        <strong>Interval:</strong> {MONITOR_DELAY_SECONDS}s<br>
                        <strong>REGISTER:</strong> {register_switch}<br>
                        <strong>MODIFY:</strong> {modify_switch}<br>
                        <div style="margin-top: 8px; border-top: 1px solid var(--border-color); padding-top: 8px;">
                            <strong>Monitored:</strong><br>
                            <span id="monitoring-subjects-list" style="color: var(--accent-color); font-size: 13px;">{subjects_list}</span>
                        </div>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; border-bottom: 1px solid var(--border-color); padding-bottom: 12px;">
                    <h2 style="margin:0; font-size: 16px; color: var(--text-secondary); font-weight: 500;">Monitor Settings (Course Pipeline Flow)</h2>
                    <button id="apply-config-btn" onclick="saveConfigToServer()" style="background: #238636; border: 1px solid #308f40; color: #fff; padding: 8px 20px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; transition: 0.2s;" onmouseover="this.style.background='#2ea44f'" onmouseout="this.style.background='#238636'">Apply & Save Config</button>
                </div>
                
                <div id="pipeline-container" class="pipeline-container">
                    <!-- Populated dynamically via JS -->
                </div>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 15px; border-bottom: 1px solid var(--border-color); padding-bottom: 12px;">
                    <h2 style="margin:0; font-size: 16px; color: var(--text-secondary); font-weight: 500;">Live Seat Status <span class="pulse" style="color:#3fb950;font-size:10px;">● LIVE</span></h2>
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <button onclick="prevCourse()" class="slider-btn" title="Previous Course">&larr;</button>
                        <span id="current-course-title" style="font-weight: 600; color: #58a6ff; font-size: 14px; background: rgba(88,166,255,0.1); padding: 4px 10px; border-radius: 20px;">Loading subject...</span>
                        <button onclick="nextCourse()" class="slider-btn" title="Next Course">&rarr;</button>
                    </div>
                </div>
                <table>
                    <thead><tr><th>Slot</th><th>Faculty</th><th>Available Seats</th><th>Last Updated</th></tr></thead>
                    <tbody id="seats-tbody">
                        <tr><td colspan='4' style='text-align:center;color:var(--text-secondary)'>Loading...</td></tr>
                    </tbody>
                </table>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <h2>Activity Log (Last 100)</h2>
                <div style="max-height: 380px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px;">
                    <table style="margin-top:0; width: 100%;">
                        <thead style="position: sticky; top: 0; background: var(--table-header-bg); z-index: 10; border-bottom: 2px solid var(--border-color);">
                            <tr><th>Timestamp</th><th>Course</th><th>Slot</th><th>Faculty</th><th>Available</th><th>Changed?</th></tr>
                        </thead>
                        <tbody id="logs-tbody">
                            {log_rows}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="card">
                <h2>Live Console Logs (Terminal)</h2>
                <div id="console-log-container" style="background: #000; color: #00ff00; font-family: monospace; font-size: 13px; padding: 15px; border-radius: 8px; max-height: 250px; overflow-y: auto; border: 1px solid var(--border-color); line-height: 1.6; white-space: pre-wrap; word-break: break-all;">
                    <div style="color: #8b949e;">Waiting for console logs...</div>
                </div>
            </div>
        </div>

        <!-- Modal Popup for adding/editing courses -->
        <div id="course-modal" class="modal-backdrop" style="display: none;">
            <div class="modal-content">
                <div class="modal-header">
                    <h3 id="modal-title">Edit Course Step</h3>
                    <button onclick="closeModal()" class="close-btn">&times;</button>
                </div>
                <div class="modal-body">
                    <input type="hidden" id="modal-course-index">
                    
                    <div class="form-group">
                        <label for="modal-keyword">Subject Keyword</label>
                        <input type="text" id="modal-keyword" placeholder="e.g. Cyber Security">
                    </div>
                    
                    <div class="form-group-row">
                        <div class="form-group">
                            <label for="modal-category">Category</label>
                            <input type="text" id="modal-category" placeholder="DE, PC, UC...">
                        </div>
                        <div class="form-group">
                            <label for="modal-page">Page Number</label>
                            <input type="number" id="modal-page" min="1" value="1">
                        </div>
                    </div>
                    
                    <div class="form-group">
                        <label for="modal-action">Target Action</label>
                        <select id="modal-action">
                            <option value="modify">Modify (with OTP)</option>
                            <option value="register">Register (Add)</option>
                            <option value="monitor">Monitor Only</option>
                        </select>
                    </div>
                    
                    <div class="form-group-row">
                        <div class="form-group">
                            <label for="modal-faculty">Faculty Preference (Keyword)</label>
                            <input type="text" id="modal-faculty" placeholder="e.g. PRABHU J (Optional)">
                        </div>
                        <div class="form-group">
                            <label for="modal-slot">Slot Preference (Pattern)</label>
                            <input type="text" id="modal-slot" placeholder="e.g. D1, D1+TD1 (Optional)">
                        </div>
                    </div>
                </div>
                <div class="modal-footer">
                    <button id="modal-delete-btn" onclick="deleteModalCourse()" class="btn-danger">Delete Step</button>
                    <div style="display: flex; gap: 10px;">
                        <button onclick="closeModal()" class="btn-secondary">Cancel</button>
                        <button onclick="saveModalCourse()" class="btn-primary">Save Step</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Modal Popup for Environment Settings -->
        <div id="env-modal" class="modal-backdrop" style="display: none;">
            <div class="modal-content" style="max-width: 650px;">
                <div class="modal-header">
                    <h3 style="display: flex; align-items: center; gap: 8px; margin: 0;">
                        <span style="width: 8px; height: 8px; border-radius: 50%; background: #3fb950; display: inline-block;"></span>
                        System Environment Config ({profile_name})
                    </h3>
                    <button onclick="closeEnvModal()" class="close-btn">&times;</button>
                </div>
                <div class="modal-body" style="max-height: 480px; overflow-y: auto; padding: 20px;">
                    
                    <!-- Personal Details Group -->
                    <div style="margin-bottom: 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <h4 style="margin: 0; font-size: 13px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px;">Personal Details</h4>
                        </div>
                        <div class="env-row">
                            <div class="env-key">DISPLAY_NAME</div>
                            <div class="env-val-container">
                                <input type="text" id="env-display-name" placeholder="Enter Display Name (e.g. Sri Krishna R)">
                            </div>
                        </div>
                    </div>
                    
                    <!-- VTOP Credentials Group -->
                    <div style="margin-bottom: 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <h4 style="margin: 0; font-size: 13px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px;">VTOP Credentials</h4>
                            <button class="help-toggle" onclick="toggleHelp('vtop-help')">ⓘ Setup Help</button>
                        </div>
                        <div id="vtop-help" class="help-content" style="display:none; margin-bottom: 10px;">
                            <p>Enter your VTOP login credentials used for add/drop portal access. <strong>Warning:</strong> Ensure they are entered correctly to prevent captcha solving failures or account locking.</p>
                        </div>
                        
                        <div class="env-row">
                            <div class="env-key">VTOP_USERNAME</div>
                            <div class="env-val-container">
                                <input type="text" id="env-vtop-username" placeholder="Enter VTOP Username">
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">VTOP_PASSWORD</div>
                            <div class="env-val-container">
                                <input type="password" id="env-vtop-password" placeholder="Enter VTOP Password">
                                <button type="button" class="password-toggle-btn" onclick="togglePasswordVisibility('env-vtop-password')">👁</button>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Gmail IMAP Group -->
                    <div style="margin-bottom: 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <h4 style="margin: 0; font-size: 13px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px;">Gmail (OTP Extraction)</h4>
                            <button class="help-toggle" onclick="toggleHelp('gmail-help')">ⓘ Setup Help</button>
                        </div>
                        <div id="gmail-help" class="help-content" style="display:none; margin-bottom: 10px;">
                            <p>To let the script automatically parse and read VTOP OTP emails for slot modifications:</p>
                            <ol>
                                <li>Enable 2-Step Verification on your Gmail account.</li>
                                <li>Go to your Google Account -> Security -> App Passwords.</li>
                                <li>Create an App Password for "Other (Custom Name)" named VTOP Automation and copy the generated 16-character code.</li>
                                <li>Paste it into GMAIL_APP_PASSWORD.</li>
                            </ol>
                        </div>
                        
                        <div class="env-row">
                            <div class="env-key">GMAIL_ADDRESS</div>
                            <div class="env-val-container">
                                <input type="email" id="env-gmail-address" placeholder="Enter Gmail Address">
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">GMAIL_APP_PASSWORD</div>
                            <div class="env-val-container">
                                <input type="password" id="env-gmail-app-password" placeholder="16-character Gmail App Password">
                                <button type="button" class="password-toggle-btn" onclick="togglePasswordVisibility('env-gmail-app-password')">👁</button>
                            </div>
                        </div>
                    </div>
                    
                    <!-- WhatsApp Notifications Group -->
                    <div style="margin-bottom: 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <h4 style="margin: 0; font-size: 13px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px;">WhatsApp (Twilio API)</h4>
                            <button class="help-toggle" onclick="toggleHelp('twilio-help')">ⓘ Setup Help</button>
                        </div>
                        <div id="twilio-help" class="help-content" style="display:none; margin-bottom: 10px;">
                            <p>Configures Twilio API credentials to send real-time available seat alerts straight to your WhatsApp:</p>
                            <ul>
                                <li><strong>TWILIO_ACCOUNT_SID</strong> & <strong>TWILIO_AUTH_TOKEN</strong>: Found on the Twilio Console homepage dashboard.</li>
                                <li><strong>TWILIO_FROM_NUMBER</strong>: The WhatsApp sender number (sandbox default is `whatsapp:+14155238886`).</li>
                                <li><strong>MY_PHONE_NUMBER</strong>: Recipient WhatsApp phone number in country code format (e.g. `whatsapp:+919080014281`).</li>
                            </ul>
                        </div>
                        
                        <div class="env-row">
                            <div class="env-key">TWILIO_ACCOUNT_SID</div>
                            <div class="env-val-container">
                                <input type="text" id="env-twilio-sid" placeholder="ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX">
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">TWILIO_AUTH_TOKEN</div>
                            <div class="env-val-container">
                                <input type="password" id="env-twilio-token" placeholder="Enter Twilio Auth Token">
                                <button type="button" class="password-toggle-btn" onclick="togglePasswordVisibility('env-twilio-token')">👁</button>
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">TWILIO_FROM_NUMBER</div>
                            <div class="env-val-container">
                                <input type="text" id="env-twilio-from" placeholder="whatsapp:+14155238886">
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">MY_PHONE_NUMBER</div>
                            <div class="env-val-container">
                                <input type="text" id="env-twilio-to" placeholder="whatsapp:+91XXXXXXXXXX">
                            </div>
                        </div>
                    </div>
                    
                    <!-- Scraper Operations Group -->
                    <div style="margin-bottom: 10px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <h4 style="margin: 0; font-size: 13px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px;">Automation Settings</h4>
                            <button class="help-toggle" onclick="toggleHelp('scraper-help')">ⓘ Setup Help</button>
                        </div>
                        <div id="scraper-help" class="help-content" style="display:none; margin-bottom: 10px;">
                            <p>Global switches governing scraper speed and execution modes:</p>
                            <ul>
                                <li><strong>MONITOR_DELAY_SECONDS</strong>: Seconds to wait/sleep between scraping rounds (e.g. `5` or `30`).</li>
                                <li><strong>REGISTER</strong>: Enable/Disable automatic registration enrollment when a slot matches your criteria.</li>
                                <li><strong>MODIFY</strong>: Enable/Disable automatic course modification using Gmail OTP verification when a slot opens.</li>
                            </ul>
                        </div>
                        
                        <div class="env-row">
                            <div class="env-key">MONITOR_DELAY_SECONDS</div>
                            <div class="env-val-container">
                                <input type="number" id="env-monitor-delay" min="1">
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">REGISTER</div>
                            <div class="env-val-container">
                                <select id="env-register-enabled">
                                    <option value="true">ENABLED (true)</option>
                                    <option value="false">DISABLED (false)</option>
                                </select>
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">MODIFY</div>
                            <div class="env-val-container">
                                <select id="env-modify-enabled">
                                    <option value="true">ENABLED (true)</option>
                                    <option value="false">DISABLED (false)</option>
                                </select>
                            </div>
                        </div>
                        <div class="env-row">
                            <div class="env-key">PRINT_TERMINAL_DATA</div>
                            <div class="env-val-container">
                                <select id="env-print-terminal">
                                    <option value="true">ENABLED (true)</option>
                                    <option value="false">DISABLED (false)</option>
                                </select>
                            </div>
                        </div>
                    </div>
                    
                </div>
                <div class="modal-footer" style="padding: 16px 20px; border-top: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; background: var(--bg-color);">
                    <div style="color: var(--text-secondary); font-size: 11px;">
                        Modifications are written directly to .env
                    </div>
                    <div style="display: flex; gap: 10px;">
                        <button onclick="closeEnvModal()" class="btn-secondary">Cancel</button>
                        <button onclick="saveEnvToServer()" class="btn-primary" style="background: #238636; border: 1px solid #308f40;">Save Settings</button>
                    </div>
                </div>
            </div>
        </div>

        <script>
        const REFRESH_INTERVAL = 10000;
        let countdown = REFRESH_INTERVAL / 1000;
        const indicator = document.getElementById('refresh-indicator');

        function avail_class(v) {{
            if (!v) return '';
            return ['full','0','-'].includes(v.toLowerCase()) ? 'avail-full' : 'avail-open';
        }}

        let allSeats = [];
        let currentCourseIndex = 0;
        let configCourses = [];
        let isConfigLoaded = false;

        function group_seats_by_course(seats) {{
            const groups = {{}};
            seats.forEach(s => {{
                const c = s.course_name || 'Unknown';
                if (!groups[c]) groups[c] = [];
                groups[c].push(s);
            }});
            return groups;
        }}

        function render_seats(seats) {{
            allSeats = seats || [];
            const groups = group_seats_by_course(allSeats);
            const keys = Object.keys(groups);
            
            const titleEl = document.getElementById('current-course-title');
            const tbody = document.getElementById('seats-tbody');
            
            if (!keys.length) {{
                tbody.innerHTML = "<tr><td colspan='4' style='text-align:center;color:var(--text-secondary)'>No seat data yet.</td></tr>";
                titleEl.textContent = "None";
                return;
            }}
            
            if (currentCourseIndex >= keys.length) {{
                currentCourseIndex = 0;
            }}
            if (currentCourseIndex < 0) {{
                currentCourseIndex = keys.length - 1;
            }}
            
            const activeCourse = keys[currentCourseIndex];
            titleEl.textContent = activeCourse;
            
            const courseSeats = groups[activeCourse];
            tbody.innerHTML = courseSeats.map(s => `
                <tr>
                    <td><code>${{s.slot||'-'}}</code></td>
                    <td>${{s.faculty||'-'}}</td>
                    <td class='${{avail_class(s.available)}}'>${{s.available||'-'}}</td>
                    <td style='color:var(--text-secondary);font-size:13px;'>${{s.timestamp||'-'}}</td>
                </tr>`).join('');
        }}

        window.prevCourse = function() {{
            const groups = group_seats_by_course(allSeats);
            const keys = Object.keys(groups);
            if (!keys.length) return;
            currentCourseIndex = (currentCourseIndex - 1 + keys.length) % keys.length;
            render_seats(allSeats);
        }}

        window.nextCourse = function() {{
            const groups = group_seats_by_course(allSeats);
            const keys = Object.keys(groups);
            if (!keys.length) return;
            currentCourseIndex = (currentCourseIndex + 1) % keys.length;
            render_seats(allSeats);
        }}

        function render_logs(logs) {{
            const tbody = document.getElementById('logs-tbody');
            if (!logs || !logs.length) {{
                tbody.innerHTML = "<tr><td colspan='6' style='text-align:center;color:var(--text-secondary)'>No activity yet.</td></tr>";
                return;
            }}
            tbody.innerHTML = logs.map(l => {{
                const badge = l.changed ? "<span class='changed-badge'>YES 🔔</span>" : "<span style='color:var(--text-secondary)'>NO</span>";
                return `<tr>
                    <td style='color:var(--text-secondary);font-size:13px;'>${{l.timestamp||'-'}}</td>
                    <td>${{l.course_name||'-'}}</td>
                    <td><code>${{l.slot||'-'}}</code></td>
                    <td>${{l.faculty||'-'}}</td>
                    <td class='${{avail_class(l.available)}}'>${{l.available||'-'}}</td>
                    <td>${{badge}}</td>
                </tr>`;
            }}).join('');
        }}

        function render_status(status) {{
            const badge = document.getElementById('status-badge');
            const val   = document.getElementById('status-val');
            const run   = document.getElementById('last-run');
            const s = status.status||'';
            badge.textContent = s;
            badge.className = 'badge ' + (s.includes('Active') ? 'badge-active' : s.includes('Crash') ? 'badge-error' : 'badge-sleeping');
            
            let displayVal = s;
            if (status.error) {{
                displayVal += ` <span style='font-size: 14px; font-weight: normal; color: var(--danger-color);'>(${{status.error}})</span>`;
            }}
            val.innerHTML = displayVal;
            run.textContent = status.last_run||'Never';
        }}

        function render_terminal_logs(logs) {{
            const container = document.getElementById('console-log-container');
            if (!logs || !logs.length) {{
                container.innerHTML = "<div style='color:#8b949e;'>Waiting for console logs...</div>";
                return;
            }}
            container.innerHTML = logs.map(l => {{
                return `<div style="margin-bottom: 4px;"><span style="color:#8b949e;">[${{l.timestamp}}]</span> ${{l.message}}</div>`;
            }}).join('');
            
            // Auto scroll to bottom
            container.scrollTop = container.scrollHeight;
        }}

        let dragSourceIndex = null;

        function render_config_table(courses) {{
            configCourses = courses || [];
            
            // Dynamically update the subjects list in the Monitoring Config card
            const listEl = document.getElementById('monitoring-subjects-list');
            if (listEl) {{
                listEl.innerHTML = configCourses.length 
                    ? configCourses.map(c => `• ${{c.keyword || 'New Subject'}}`).join('<br>')
                    : 'None';
            }}
            
            const container = document.getElementById('pipeline-container');
            if (!container) return;
            
            if (!configCourses.length) {{
                container.innerHTML = `
                    <div class="pipeline-add-card" onclick="openAddModal()">
                        <div class="pipeline-add-icon">+</div>
                        <div>Add First Course</div>
                    </div>
                `;
                return;
            }}
            
            let html = '';
            configCourses.forEach((c, idx) => {{
                const actionBadgeClass = 'badge-action-' + (c.action || 'monitor');
                const actionLabel = c.action === 'modify' ? 'Modify' : c.action === 'register' ? 'Register' : 'Monitor Only';
                
                html += `
                    <div class="pipeline-card" 
                         draggable="true" 
                         data-index="${{idx}}"
                         onclick="openEditModal(${{idx}})"
                         ondragstart="handleDragStart(event, ${{idx}})"
                         ondragover="handleDragOver(event)"
                         ondragenter="handleDragEnter(event)"
                         ondragleave="handleDragLeave(event)"
                         ondrop="handleDrop(event, ${{idx}})"
                         ondragend="handleDragEnd(event)">
                        <div class="pipeline-card-header">
                            <span class="pipeline-step-badge">STEP ${{idx + 1}}</span>
                            <span class="pipeline-badge-action ${{actionBadgeClass}}">${{actionLabel}}</span>
                        </div>
                        <div class="pipeline-card-title">${{c.keyword || 'Unnamed Course'}}</div>
                        <div class="pipeline-card-details">
                            <strong>Category:</strong> ${{c.category || 'DE'}} (Page ${{c.page || 1}})<br>
                            <strong>Faculty:</strong> ${{c.target_faculty || 'Any'}}<br>
                            <strong>Slot:</strong> ${{c.target_slot || 'Any'}}
                        </div>
                    </div>
                `;
                
                // Connection arrow to next card
                html += `<div class="pipeline-arrow">&rarr;</div>`;
            }});
            
            // Final Add Step Card at the end of the train
            html += `
                <div class="pipeline-add-card" onclick="openAddModal()">
                    <div class="pipeline-add-icon">+</div>
                    <div>Add Course Step</div>
                </div>
            `;
            
            container.innerHTML = html;
        }}

        // Drag and Drop Event Handlers
        window.handleDragStart = function(e, idx) {{
            dragSourceIndex = idx;
            e.currentTarget.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', idx);
        }};

        window.handleDragOver = function(e) {{
            if (e.preventDefault) {{
                e.preventDefault(); // Necessary. Allows us to drop.
            }}
            return false;
        }};

        window.handleDragEnter = function(e) {{
            e.currentTarget.style.borderColor = 'var(--accent-color)';
            e.currentTarget.style.background = 'rgba(88, 166, 255, 0.05)';
        }};

        window.handleDragLeave = function(e) {{
            e.currentTarget.style.borderColor = '';
            e.currentTarget.style.background = '';
        }};

        window.handleDrop = function(e, targetIdx) {{
            e.stopPropagation();
            e.preventDefault();
            
            if (dragSourceIndex !== null && dragSourceIndex !== targetIdx) {{
                // Rearrange the items in configCourses array
                const draggedItem = configCourses[dragSourceIndex];
                configCourses.splice(dragSourceIndex, 1);
                configCourses.splice(targetIdx, 0, draggedItem);
                
                // Re-render
                render_config_table(configCourses);
            }}
            return false;
        }};

        window.handleDragEnd = function(e) {{
            e.currentTarget.classList.remove('dragging');
            // Reset styles on all cards
            const cards = document.querySelectorAll('.pipeline-card');
            cards.forEach(c => {{
                c.style.borderColor = '';
                c.style.background = '';
            }});
            dragSourceIndex = null;
        }};

        // Modal Open / Close / Save logic
        window.openEditModal = function(idx) {{
            const course = configCourses[idx];
            if (!course) return;
            
            // Prevent modal click from triggering if we are dragging
            if (dragSourceIndex !== null) return;
            
            document.getElementById('modal-title').textContent = `Edit Step ${{idx + 1}}`;
            document.getElementById('modal-course-index').value = idx;
            document.getElementById('modal-keyword').value = course.keyword || '';
            document.getElementById('modal-category').value = course.category || 'DE';
            document.getElementById('modal-page').value = course.page || 1;
            document.getElementById('modal-action').value = course.action || 'modify';
            document.getElementById('modal-faculty').value = course.target_faculty || '';
            document.getElementById('modal-slot').value = course.target_slot || '';
            
            document.getElementById('modal-delete-btn').style.display = 'block';
            document.getElementById('course-modal').style.display = 'flex';
        }};

        window.openAddModal = function() {{
            document.getElementById('modal-title').textContent = 'Add New Pipeline Step';
            document.getElementById('modal-course-index').value = '-1';
            document.getElementById('modal-keyword').value = '';
            document.getElementById('modal-category').value = 'DE';
            document.getElementById('modal-page').value = 1;
            document.getElementById('modal-action').value = 'modify';
            document.getElementById('modal-faculty').value = '';
            document.getElementById('modal-slot').value = '';
            
            document.getElementById('modal-delete-btn').style.display = 'none';
            document.getElementById('course-modal').style.display = 'flex';
        }};

        window.closeModal = function() {{
            document.getElementById('course-modal').style.display = 'none';
        }};

        window.saveModalCourse = function() {{
            const idx = parseInt(document.getElementById('modal-course-index').value);
            const courseData = {{
                keyword: document.getElementById('modal-keyword').value.trim(),
                category: document.getElementById('modal-category').value.trim() || 'DE',
                page: parseInt(document.getElementById('modal-page').value) || 1,
                action: document.getElementById('modal-action').value,
                target_faculty: document.getElementById('modal-faculty').value.trim(),
                target_slot: document.getElementById('modal-slot').value.trim()
            }};
            
            if (!courseData.keyword) {{
                alert('Please enter a Subject Keyword.');
                return;
            }}
            
            if (idx === -1) {{
                // Add new course to end of array
                configCourses.push(courseData);
            }} else {{
                // Update existing
                configCourses[idx] = courseData;
            }}
            
            render_config_table(configCourses);
            closeModal();
        }};

        window.deleteModalCourse = function() {{
            const idx = parseInt(document.getElementById('modal-course-index').value);
            if (idx >= 0 && idx < configCourses.length) {{
                configCourses.splice(idx, 1);
                render_config_table(configCourses);
            }}
            closeModal();
        }};

        window.saveConfigToServer = async function() {{
            const btn = document.getElementById('apply-config-btn');
            const originalText = btn.textContent;
            const originalBg = btn.style.background;
            const originalBorder = btn.style.borderColor;

            btn.disabled = true;
            btn.textContent = 'Saving Config...';
            btn.style.background = '#6e7681';
            btn.style.borderColor = '#6e7681';

            try {{
                const res = await fetch('/api/config', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify(configCourses)
                }});
                const resData = await res.json();
                if (resData.status === 'success') {{
                    btn.textContent = 'Config Saved!';
                    btn.style.background = '#2ea44f';
                    btn.style.borderColor = '#2ea44f';
                    setTimeout(() => {{
                        btn.textContent = originalText;
                        btn.style.background = originalBg;
                        btn.style.borderColor = originalBorder;
                        btn.disabled = false;
                    }}, 1500);
                    refresh();
                }} else {{
                    alert('Failed to update config: ' + resData.message);
                    btn.textContent = originalText;
                    btn.style.background = originalBg;
                    btn.style.borderColor = originalBorder;
                    btn.disabled = false;
                }}
            }} catch(e) {{
                alert('Error saving configuration: ' + e);
                btn.textContent = originalText;
                btn.style.background = originalBg;
                btn.style.borderColor = originalBorder;
                btn.disabled = false;
            }}
        }}

        async function refresh() {{
            try {{
                const res = await fetch('/api/data?t=' + Date.now());
                const data = await res.json();
                render_status(data.status);
                render_seats(data.seats);
                render_logs(data.logs);
                render_terminal_logs(data.terminal_logs);

                if (!isConfigLoaded && data.courses_config) {{
                    render_config_table(data.courses_config);
                    isConfigLoaded = true;
                }}
            }} catch(e) {{ console.warn('Refresh failed', e); }}
            countdown = REFRESH_INTERVAL / 1000;
        }}

        window.toggleTheme = function() {{
            const isLight = document.documentElement.classList.toggle('light-theme');
            localStorage.setItem('theme', isLight ? 'light' : 'dark');
        }};

        window.openEnvModal = async function() {{
            try {{
                const res = await fetch('/api/env?t=' + Date.now());
                const env = await res.json();
                
                document.getElementById('env-display-name').value = env.DISPLAY_NAME || '';
                document.getElementById('env-vtop-username').value = env.VTOP_USERNAME || '';
                document.getElementById('env-vtop-password').value = env.VTOP_PASSWORD || '';
                document.getElementById('env-gmail-address').value = env.GMAIL_ADDRESS || '';
                document.getElementById('env-gmail-app-password').value = env.GMAIL_APP_PASSWORD || '';
                
                document.getElementById('env-twilio-sid').value = env.TWILIO_ACCOUNT_SID || '';
                document.getElementById('env-twilio-token').value = env.TWILIO_AUTH_TOKEN || '';
                document.getElementById('env-twilio-from').value = env.TWILIO_FROM_NUMBER || '';
                document.getElementById('env-twilio-to').value = env.MY_PHONE_NUMBER || '';
                
                document.getElementById('env-monitor-delay').value = env.MONITOR_DELAY_SECONDS || '30';
                document.getElementById('env-register-enabled').value = env.REGISTER || 'false';
                document.getElementById('env-modify-enabled').value = env.MODIFY || 'false';
                document.getElementById('env-print-terminal').value = env.print_scrapper_data_in_terminal || 'false';
                
                document.getElementById('env-modal').style.display = 'flex';
            }} catch(e) {{
                alert('Error loading environment config: ' + e);
            }}
        }};

        window.closeEnvModal = function() {{
            document.getElementById('env-modal').style.display = 'none';
        }};

        window.toggleHelp = function(id) {{
            const el = document.getElementById(id);
            if (el) {{
                el.style.display = el.style.display === 'none' ? 'block' : 'none';
            }}
        }};

        window.togglePasswordVisibility = function(id) {{
            const input = document.getElementById(id);
            if (input) {{
                input.type = input.type === 'password' ? 'text' : 'password';
            }}
        }};

        window.saveEnvToServer = async function() {{
            const payload = {{
                DISPLAY_NAME: document.getElementById('env-display-name').value.trim(),
                VTOP_USERNAME: document.getElementById('env-vtop-username').value.trim(),
                VTOP_PASSWORD: document.getElementById('env-vtop-password').value.trim(),
                GMAIL_ADDRESS: document.getElementById('env-gmail-address').value.trim(),
                GMAIL_APP_PASSWORD: document.getElementById('env-gmail-app-password').value.trim(),
                TWILIO_ACCOUNT_SID: document.getElementById('env-twilio-sid').value.trim(),
                TWILIO_AUTH_TOKEN: document.getElementById('env-twilio-token').value.trim(),
                TWILIO_FROM_NUMBER: document.getElementById('env-twilio-from').value.trim(),
                MY_PHONE_NUMBER: document.getElementById('env-twilio-to').value.trim(),
                MONITOR_DELAY_SECONDS: document.getElementById('env-monitor-delay').value.trim(),
                REGISTER: document.getElementById('env-register-enabled').value,
                MODIFY: document.getElementById('env-modify-enabled').value,
                print_scrapper_data_in_terminal: document.getElementById('env-print-terminal').value
            }};

            try {{
                const res = await fetch('/api/env', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify(payload)
                }});
                const resData = await res.json();
                if (resData.status === 'success') {{
                    alert('Environment configuration saved! The scraper will automatically reload settings on its next iteration cycle.');
                    closeEnvModal();
                    location.reload();
                }} else {{
                    alert('Failed to save settings: ' + resData.message);
                }}
            }} catch(e) {{
                alert('Error saving environment configuration: ' + e);
            }}
        }};

        setInterval(() => {{
            countdown--;
            indicator.textContent = `⟳ Refreshing in ${{countdown}}s`;
            if (countdown <= 0) refresh();
        }}, 1000);
        </script>
    </body>
    </html>
    """
    return html_content


def read_env_file():
    env_data = {}
    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.split("#", 1)[0].strip()  # remove inline comment
                    env_data[k] = v
    return env_data

def update_env_file(updates: dict):
    env_path = ".env"
    if not os.path.exists(env_path):
        with open(env_path, "w", encoding="utf-8") as f:
            for k, v in updates.items():
                f.write(f"{k}={v}\n")
        return

    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        
        if "=" in line:
            parts = line.split("=", 1)
            key = parts[0].strip()
            if key in updates:
                val_part = parts[1]
                comment = ""
                if "#" in val_part:
                    val_sub, comment_sub = val_part.split("#", 1)
                    comment = "  # " + comment_sub.strip()
                
                new_value = updates[key]
                new_lines.append(f"{key}={new_value}{comment}\n")
                updated_keys.add(key)
                continue
        
        new_lines.append(line)

    for k, v in updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


@app.get("/api/env")
async def get_env_config():
    from fastapi.responses import JSONResponse
    env_file_data = read_env_file()
    
    keys_to_read = [
        "DISPLAY_NAME", "VTOP_USERNAME", "VTOP_PASSWORD",
        "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "MY_PHONE_NUMBER",
        "MONITOR_DELAY_SECONDS", "REGISTER", "MODIFY", "print_scrapper_data_in_terminal"
    ]
    
    res = {}
    for k in keys_to_read:
        val = env_file_data.get(k)
        if val is None:
            if k == "TWILIO_FROM_NUMBER":
                val = env_file_data.get("TWILIO_FROM_NUMBER", os.getenv("TWILIO_FROM_NUMBER", os.getenv("TWILIO_FROM", "")))
            else:
                val = os.getenv(k, "")
        res[k] = val.strip() if val else ""
        
    return JSONResponse(res)


@app.post("/api/env")
async def save_env_config(request: Request):
    global USERNAME, PASSWORD, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, MY_PHONE_NUMBER
    global MONITOR_DELAY_SECONDS, REGISTER, MODIFY, PRINT_SCRAPER_DATA
    from fastapi.responses import JSONResponse
    try:
        data = await request.json()
        updates = {}
        keys = [
            "DISPLAY_NAME", "VTOP_USERNAME", "VTOP_PASSWORD",
            "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "MY_PHONE_NUMBER",
            "MONITOR_DELAY_SECONDS", "REGISTER", "MODIFY", "print_scrapper_data_in_terminal"
        ]
        
        for k in keys:
            if k in data:
                updates[k] = str(data[k]).strip()
        
        update_env_file(updates)
        
        if "VTOP_USERNAME" in updates:
            USERNAME = updates["VTOP_USERNAME"]
        if "VTOP_PASSWORD" in updates:
            PASSWORD = updates["VTOP_PASSWORD"]
        if "TWILIO_ACCOUNT_SID" in updates:
            TWILIO_ACCOUNT_SID = updates["TWILIO_ACCOUNT_SID"]
        if "TWILIO_AUTH_TOKEN" in updates:
            TWILIO_AUTH_TOKEN = updates["TWILIO_AUTH_TOKEN"]
        if "TWILIO_FROM_NUMBER" in updates:
            TWILIO_FROM = updates["TWILIO_FROM_NUMBER"]
        if "MY_PHONE_NUMBER" in updates:
            MY_PHONE_NUMBER = updates["MY_PHONE_NUMBER"]
            
        if "MONITOR_DELAY_SECONDS" in updates:
            try:
                MONITOR_DELAY_SECONDS = int(updates["MONITOR_DELAY_SECONDS"])
            except:
                pass
        if "REGISTER" in updates:
            REGISTER = updates["REGISTER"].lower() == "true"
        if "MODIFY" in updates:
            MODIFY = updates["MODIFY"].lower() == "true"
        if "print_scrapper_data_in_terminal" in updates:
            PRINT_SCRAPER_DATA = updates["print_scrapper_data_in_terminal"].lower() == "true"
            
        for k, v in updates.items():
            os.environ[k] = v
            
        try:
            import src.fetch_otp
            if "GMAIL_ADDRESS" in updates:
                src.fetch_otp.GMAIL_ADDRESS = updates["GMAIL_ADDRESS"]
            if "GMAIL_APP_PASSWORD" in updates:
                src.fetch_otp.GMAIL_APP_PASSWORD = updates["GMAIL_APP_PASSWORD"]
        except Exception as e:
            print(f"Error updating fetch_otp module: {e}")
            
        print(f"[Config] Dynamic environment variables updated & written to .env")
        return JSONResponse({"status": "success", "message": "Environment settings updated successfully!"})
    except Exception as e:
        print(f"Error saving environment config: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/data")
async def api_data():
    """JSON endpoint for live polling by the dashboard JS."""
    from fastapi.responses import JSONResponse
    current_seats = get_latest_status_by_slot()
    logs = get_recent_transitions()
    return JSONResponse({
        "status": get_dashboard_status(),
        "seats": current_seats,
        "logs": logs[:100],
        "terminal_logs": GLOBAL_LOG_BUFFER,
        "courses_config": COURSES_TO_MONITOR,
    })

@app.post("/api/config")
async def save_config(request: Request):
    global COURSES_TO_MONITOR
    try:
        data = await request.json()
        if not isinstance(data, list):
            return JSONResponse({"status": "error", "message": "Config must be a list of courses"}, status_code=400)
        
        # Save to JSON config file
        with open(CONFIG_JSON_PATH, "w") as f:
            json.dump(data, f, indent=4)
        
        # Update in-memory configuration immediately
        COURSES_TO_MONITOR = data
        print(f"[Config] Dynamic configuration updated via dashboard. Total courses: {len(COURSES_TO_MONITOR)}")
        return JSONResponse({"status": "success", "message": "Config updated successfully"})
    except Exception as e:
        print(f"Error saving dynamic config: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

if __name__ == "__main__":
    if os.getenv("PORT") or os.getenv("RUN_WEB"):
        import uvicorn
        port = int(os.getenv("PORT", 8080))
        uvicorn.run("main:app", host="0.0.0.0", port=port)
    else:
        asyncio.run(run())
