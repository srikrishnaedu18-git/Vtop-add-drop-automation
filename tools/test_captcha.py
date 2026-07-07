"""
test_captcha.py
---------------
Fetches the live CAPTCHA from the VTOP portal (no login required),
solves it with captcha_solver.py, and saves the raw image into
captcha_samples/ with a timestamp so you can visually verify accuracy.

Usage:
    python3 test_captcha.py [--count N]   # capture N samples (default 1)

After running, open the saved .jpg files and compare against the
printed solver output. Match = ✅ working. Mismatch = ❌ needs tuning.
"""

import sys
import os
import asyncio
import base64
import argparse
from datetime import datetime
from playwright.async_api import async_playwright

# Add parent directory to sys.path to find src/ package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load .env if python-dotenv is installed (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.captcha_solver import solve_captcha_b64


BASE_URL    = os.getenv("BASE_URL",    "https://vtopreg.vit.ac.in/tablet/")
CHROME_PATH = os.getenv("CHROME_PATH", "/usr/bin/google-chrome")
SAMPLES_DIR = os.getenv("CAPTCHA_SAMPLES_DIR", "captcha_samples")

# Ensure the samples directory exists
os.makedirs(SAMPLES_DIR, exist_ok=True)


async def capture_and_solve(page, index: int = 0) -> tuple[str, str]:
    """
    Grab one CAPTCHA from the portal page, save it to captcha_samples/,
    and return (filename, solved_text).
    """
    # Refresh captcha between captures (if a refresh button is present)
    if index > 0:
        refresh_btn = page.locator("#refreshCaptchaProcess").first
        if await refresh_btn.count() > 0:
            await refresh_btn.click()
            await page.wait_for_timeout(1500)

    await page.wait_for_selector("#captcha_id", timeout=10000)
    src = await page.locator("#captcha_id").get_attribute("src")

    # Decode and save with timestamp
    b64_data = src.split(",", 1)[1]
    raw = base64.b64decode(b64_data)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SAMPLES_DIR, f"captcha_{timestamp}.jpg")
    with open(filename, "wb") as f:
        f.write(raw)

    # Solve
    result = solve_captcha_b64(src)
    return filename, result


async def run(count: int):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page()

        print(f"[*] Loading portal: {BASE_URL}")
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        print(f"[*] Capturing {count} sample(s) → {SAMPLES_DIR}/\n")
        for i in range(count):
            filename, solved = await capture_and_solve(page, index=i)
            rel = os.path.relpath(filename)
            print(f"  [{i+1}/{count}]  Saved : {rel}")
            print(f"         Solved: '{solved}'")
            print(f"         → Open the image and verify ✅ / ❌\n")

        await browser.close()

    print(f"Done. Images are in '{SAMPLES_DIR}/' — git-ignored, local only.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capture & solve VTOP CAPTCHA samples")
    parser.add_argument(
        "--count", type=int, default=1,
        help="Number of CAPTCHA samples to capture (default: 1)"
    )
    args = parser.parse_args()
    asyncio.run(run(args.count))
