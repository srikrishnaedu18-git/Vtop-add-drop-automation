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


# Add more dictionaries here to scrape multiple subjects sequentially!
try:
    COURSES_TO_MONITOR = json.loads(os.getenv("COURSES_TO_MONITOR", "[]"))
except Exception as e:
    print(f"Error parsing COURSES_TO_MONITOR from .env: {e}")
    COURSES_TO_MONITOR = []

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

                    while True:
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
                    while True:
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
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from contextlib import asynccontextmanager
except ImportError:
    pass

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
            return [dict(r) for r in cursor.fetchall()]
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
            return [dict(r) for r in cursor.fetchall()]
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
    status_text = SCRAPER_STATUS["status"]
    last_run = SCRAPER_STATUS["last_run"]
    error = SCRAPER_STATUS["error"]
    
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

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>VTOP Scraper Dashboard</title>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-color: #0d1117;
                --card-bg: rgba(22, 27, 34, 0.85);
                --border-color: #30363d;
                --text-primary: #c9d1d9;
                --text-secondary: #8b949e;
                --accent-color: #58a6ff;
                --danger-color: #da3637;
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
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>FALL SEMESTER ADD/DROP 2026-27</h1>
                <div class="header-right">
                    <span id="refresh-indicator">⟳ Auto-refresh: 10s</span>
                    <span id="status-badge" class="badge {badge_class}">{status_text}</span>
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
                    <div style="font-size:14px;line-height:1.8;">
                        <strong>Mode:</strong> {mode_text}<br>
                        <strong>Interval:</strong> {MONITOR_DELAY_SECONDS}s<br>
                        <strong>REGISTER:</strong> {register_switch}<br>
                        <strong>MODIFY:</strong> {modify_switch}
                    </div>
                </div>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <h2>Live Seat Status <span class="pulse" style="color:#3fb950;font-size:10px;">● LIVE</span></h2>
                <table>
                    <thead><tr><th>Course</th><th>Slot</th><th>Faculty</th><th>Available Seats</th><th>Last Updated</th></tr></thead>
                    <tbody id="seats-tbody">{current_seats_rows}</tbody>
                </table>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <h2>Activity Log (Last 30)</h2>
                <table>
                    <thead><tr><th>Timestamp</th><th>Course</th><th>Slot</th><th>Faculty</th><th>Available</th><th>Changed?</th></tr></thead>
                    <tbody id="logs-tbody">{log_rows}</tbody>
                </table>
            </div>

            <div class="card">
                <h2>Live Console Logs (Terminal)</h2>
                <div id="console-log-container" style="background: #000; color: #00ff00; font-family: monospace; font-size: 13px; padding: 15px; border-radius: 8px; max-height: 250px; overflow-y: auto; border: 1px solid var(--border-color); line-height: 1.6; white-space: pre-wrap; word-break: break-all;">
                    <div style="color: #8b949e;">Waiting for console logs...</div>
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

        function render_seats(seats) {{
            const tbody = document.getElementById('seats-tbody');
            if (!seats || !seats.length) {{
                tbody.innerHTML = "<tr><td colspan='5' style='text-align:center;color:var(--text-secondary)'>No data yet.</td></tr>";
                return;
            }}
            tbody.innerHTML = seats.map(s => `
                <tr>
                    <td class='highlight'>${{s.course_name||'Unknown'}}</td>
                    <td><code>${{s.slot||'-'}}</code></td>
                    <td>${{s.faculty||'-'}}</td>
                    <td class='${{avail_class(s.available)}}'>${{s.available||'-'}}</td>
                    <td style='color:var(--text-secondary);font-size:13px;'>${{s.timestamp||'-'}}</td>
                </tr>`).join('');
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

        async function refresh() {{
            try {{
                const res = await fetch('/api/data?t=' + Date.now());
                const data = await res.json();
                render_status(data.status);
                render_seats(data.seats);
                render_logs(data.logs);
                render_terminal_logs(data.terminal_logs);
            }} catch(e) {{ console.warn('Refresh failed', e); }}
            countdown = REFRESH_INTERVAL / 1000;
        }}

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


@app.get("/api/data")
async def api_data():
    """JSON endpoint for live polling by the dashboard JS."""
    from fastapi.responses import JSONResponse
    current_seats = get_latest_status_by_slot()
    logs = get_recent_transitions()
    return JSONResponse({
        "status": SCRAPER_STATUS,
        "seats": current_seats,
        "logs": logs[:30],
        "terminal_logs": GLOBAL_LOG_BUFFER,
    })

if __name__ == "__main__":
    if os.getenv("PORT") or os.getenv("RUN_WEB"):
        import uvicorn
        port = int(os.getenv("PORT", 8080))
        uvicorn.run("main:app", host="0.0.0.0", port=port)
    else:
        asyncio.run(run())
