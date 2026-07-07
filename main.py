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
import json
import sqlite3
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from twilio.rest import Client
except ImportError:
    Client = None

from captcha_solver import solve_captcha_b64

# ─── CONFIG ───────────────────────────────────────────────────────────────────
USERNAME       = os.getenv("VTOP_USERNAME", "").strip()
PASSWORD       = os.getenv("VTOP_PASSWORD", "").strip()
BASE_URL       = os.getenv("BASE_URL",      "https://vtopreg.vit.ac.in/tablet/")
CHROME_PATH    = os.getenv("CHROME_PATH",   "/usr/bin/google-chrome")
HEADLESS       = os.getenv("HEADLESS", "false").lower() == "true"
MAX_RETRIES    = 8
DB_PATH        = os.getenv("DB_PATH", "seats.db").strip()
MONITOR_DELAY_SECONDS = int(os.getenv("MONITOR_DELAY_SECONDS", "30"))

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
    """Create the scrapes table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scrapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                changed BOOLEAN,
                course_name TEXT,
                slots_json TEXT
            )
        ''')


def check_and_save_db(course_name: str, slots: list) -> bool:
    """
    Checks if the scraped slots match the last changed state in the DB.
    Inserts a new heartbeat row (changed=0) if identical.
    Inserts a new data row (changed=1) if different.
    Returns True if changed, False otherwise.
    """
    current_json = json.dumps(slots, sort_keys=True)
    
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # Get the JSON of the last time this course's slots actually changed
        cursor.execute('''
            SELECT slots_json FROM scrapes 
            WHERE changed = 1 AND course_name = ? 
            ORDER BY timestamp DESC LIMIT 1
        ''', (course_name,))
        row = cursor.fetchone()
        
        if row and row[0] == current_json:
            # Data is identical to the last known state -> insert heartbeat
            cursor.execute('''
                INSERT INTO scrapes (changed, course_name, slots_json)
                VALUES (0, ?, NULL)
            ''', (course_name,))
            return False
        else:
            # Data is new or changed -> save the full json
            cursor.execute('''
                INSERT INTO scrapes (changed, course_name, slots_json)
                VALUES (1, ?, ?)
            ''', (course_name, current_json))
            return True


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def get_captcha_b64(page) -> str:
    img = page.locator("#captcha_id")
    await img.wait_for(state="visible", timeout=15_000)
    return await img.get_attribute("src")


async def dismiss_swal(page) -> bool:
    """Click OK on SweetAlert popup if visible. Returns True if dismissed."""
    swal = page.locator("div.sweet-alert.visible")
    if await swal.count() > 0 and await swal.is_visible():
        ok_btn = swal.locator("button.confirm")
        if await ok_btn.count() > 0:
            await ok_btn.click()
            await page.wait_for_selector("div.sweet-alert.visible", state="hidden", timeout=5000)
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
    pg_num = str(course_config["page"])
    
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

async def scrape_and_format(page) -> str | None:
    print("\n[STEP 5] Scraping slot table...")

    # Wait for the slot table (has columns: Slot, Venue, Faculty, Available)
    try:
        await page.wait_for_selector("#page-wrapper table thead", timeout=15_000)
    except PWTimeout:
        print("  [!] No table found")
        await _dump(page, "fail_no_table.html")
        return None

    # Extract course info from header table
    course_name = ""
    header_span = page.locator("#page-wrapper table:first-of-type thead tr:not(.w3-blue) td span").first
    if await header_span.count() > 0:
        course_name = (await header_span.inner_text()).strip()

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
        print("  [!] No slot rows extracted")
        await _dump(page, "fail_empty_slots.html")
        return None

    # ── Format WhatsApp message ──
    now = datetime.now().strftime("%d-%b-%Y %I:%M %p")
    lines = [
        f"📚 *{course_name or 'Unknown Course'}*",
        f"🕐 Scraped: {now}",
        "",
        "```",
        f"{'SLOT':<10} {'FACULTY':<20} {'STATUS':<8}",
        f"{'─'*10} {'─'*20} {'─'*8}",
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
        lines.append(f"\n❌ *All slots are FULL*")

    msg = "\n".join(lines)
    return msg, avail_count, course_name or "Unknown Course", slots


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


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run():
    if not USERNAME or not PASSWORD:
        print("ERROR: Set VTOP_USERNAME and VTOP_PASSWORD in .env!")
        return

    print(f"[Config] USER={USERNAME} | HEADLESS={HEADLESS} | DELAY={MONITOR_DELAY_SECONDS}s")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROME_PATH, headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        
        while True:
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
                while True:
                    print(f"\n[--- Monitoring Iteration @ {datetime.now().strftime('%H:%M:%S')} ---]")
                    
                    for course_config in COURSES_TO_MONITOR:
                        print(f"\n[>] Checking {course_config['category']}: {course_config['keyword']}")
                        
                        if not await navigate_to_course(page, course_config):
                            raise Exception("Failed to navigate to course. Session likely expired.")

                        msg, avail_count, course_name, slots = await scrape_and_format(page)
                        if msg:
                            print("\n" + "═" * 60)
                            print("Extracted Data:\n")
                            print(msg)
                            print("\n" + "═" * 60)

                            init_db()
                            is_changed = check_and_save_db(course_name, slots)

                            if is_changed:
                                print("\n[!] Data CHANGED since last run! Triggering WhatsApp API...")
                                send_whatsapp_alert(msg)
                            else:
                                print("\n[i] Data is IDENTICAL to the last run. Stored heartbeat in DB. Not sending WhatsApp spam.")
                        else:
                            print("\n[!] Could not extract slot data.")

                        print("\n[→] Returning to Home dashboard...")
                        home_btn = page.locator("#homeIcon")
                        if await home_btn.count() > 0:
                            await home_btn.click()
                            # Wait for dashboard to load before starting next course
                            await page.wait_for_timeout(2000)
                        else:
                            raise Exception("Home icon not found. Session must be dead.")

                    print(f"\n[zzz] All courses checked. Sleeping {MONITOR_DELAY_SECONDS} seconds...")
                    await asyncio.sleep(MONITOR_DELAY_SECONDS)

            except Exception as e:
                print(f"\n[!] Session crashed or expired: {e}")
                await page.screenshot(path="error_screenshot.png")
                print("    Restarting a fresh session in 5 seconds...")
                await asyncio.sleep(5)
            finally:
                await context.close()


if __name__ == "__main__":
    asyncio.run(run())
