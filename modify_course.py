import os
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from dotenv import load_dotenv

# Import functions from our other scripts
from captcha_solver import solve_captcha_b64
from fetch_otp import get_vtop_otp

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
USERNAME      = os.getenv("VTOP_USERNAME", "").strip()
PASSWORD      = os.getenv("VTOP_PASSWORD", "").strip()
BASE_URL      = os.getenv("BASE_URL", "https://vtopreg.vit.ac.in/tablet/")
CHROME_PATH   = os.getenv("CHROME_PATH", "/usr/bin/google-chrome")
HEADLESS      = os.getenv("HEADLESS", "false").lower() == "true"
MAX_RETRIES   = 8
TARGET_COURSE = "Software Industrialization"

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

async def run_modify_flow():
    print(f"[Config] USER={USERNAME} | HEADLESS={HEADLESS}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        
        if not await login(page):
            print("Login failed completely.")
            await browser.close()
            return

        if not await pass_instructions(page):
            print("Instructions stage failed.")
            await browser.close()
            return

        if not await pass_progress_captcha(page):
            print("Progress Captcha failed.")
            await browser.close()
            return
            
        print("\n[STEP 4] Navigating to View/Modify...")
        
        # Click the orange View/Modify button on the dashboard
        # This button triggers modifySlots() JS call, but let's click it directly
        await page.locator("span:has-text('View / Modify')").click()
        
        # Wait for the registered courses table to load
        await page.wait_for_selector("#page-wrapper table", timeout=15000)
        print("  [✓] Registered courses table loaded.")
        
        # Find the target course row
        print(f"  [→] Looking for '{TARGET_COURSE}'...")
        rows = await page.locator("#page-wrapper tbody tr").all()
        target_row = None
        
        for row in rows:
            text = await row.inner_text()
            if TARGET_COURSE.lower() in text.lower():
                target_row = row
                break
                
        if not target_row:
            print(f"  [!] Could not find '{TARGET_COURSE}' in registered courses!")
            await _dump(page, "fail_modify_course_not_found.html")
            await browser.close()
            return
            
        # Click the Modify button in that row
        modify_btn = target_row.locator("button:has-text('Modify')")
        if await modify_btn.count() == 0:
            print(f"  [!] Found '{TARGET_COURSE}', but no Modify button exists!")
            await browser.close()
            return
            
        await modify_btn.click()
        print(f"  [✓] Clicked Modify for '{TARGET_COURSE}'")
        
        print("\n[STEP 5] Handling OTP Screen...")
        # Wait for the OTP input to appear on the new page
        await page.wait_for_selector("#mailOTP", timeout=15000)
        
        # Extract the Prefix from the DOM
        # Inside the row containing #mailOTP, there is a span containing "PREFIX - "
        row = page.locator("tr:has(#mailOTP)")
        spans = await row.locator("span").all()
        screen_prefix = None
        for s in spans:
            txt = await s.inner_text()
            if "-" in txt:
                screen_prefix = txt.replace("-", "").strip()
                break
                
        if not screen_prefix:
            print("  [!] Could not locate OTP Reference prefix in the #mailOTP row.")
            await browser.close()
            return
            
        print(f"\n==========================================")
        print(f"  [SCREEN] OTP Prefix required: {screen_prefix}")
        print(f"==========================================")
        
        print("\n[→] Fetching OTP from Gmail...")
        
        # Poll Gmail for the OTP, matching the expected prefix
        email_prefix, email_code = get_vtop_otp(max_wait_seconds=120, expected_prefix=screen_prefix)
        
        if not email_prefix:
            print("\n[!] Failed to fetch OTP from Gmail.")
            await browser.close()
            return
            
        print(f"\n==========================================")
        print(f"  [GMAIL] OTP Prefix received:  {email_prefix}")
        print(f"  [GMAIL] OTP Code received:    {email_code}")
        print(f"==========================================")
        
        if screen_prefix == email_prefix:
            print("\n[✓] SUCCESS! The prefixes MATCH perfectly.")
            print("  [i] Skipping auto-fill as requested for testing.")
        else:
            print("\n[!] WARNING! The prefixes DO NOT MATCH.")
            print(f"    Screen wants '{screen_prefix}' but Email provided '{email_prefix}'.")
            print("    This usually means the email is an old OTP. Wait for the new one.")
            
        # Keep browser open for a bit so you can see it
        await page.wait_for_timeout(10000)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_modify_flow())
