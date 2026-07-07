# ⚡ VIT VTOP Course Add/Drop Automation Script

An advanced, high-performance automation and monitoring tool for the **VIT VTOP Add/Drop Portal 2026-27**, built using Python, Playwright, and SQLite.

---

## 🌟 Key Features

*   **Automated Login & CAPTCHA Solver**: Bypasses VTOP login captchas using client-side image processing and base64 solving logic.
*   **Fast-Refresh Single-Course Loop**: When monitoring a single course, the script skips loading the entire homepage dashboard. It performs a local "Go Back ➡️ Proceed" refresh to save time and prevent timeouts.
*   **1-Click Auto-Registration**: Set `REGISTER=true`, select a `CHOSEN_FACULTY` and `CHOSEN_SLOT`, and the script will automatically select the matching row and click **Register** as soon as a seat opens.
*   **1-Click Auto-Modification (with OTP)**: Set `MODIFY=true` to update an existing course. The script automatically reads the required 3-letter OTP prefix, polls Gmail, extracts the VTOP OTP, inputs it, and clicks **Update**.
*   **Relational Seat Change Logging**: Stores seat metrics per-faculty in an SQLite database (`seat_logs`). Transition rules ensure duplicate "Full" states are suppressed, logging only real changes or numerical seat heartbeats.
*   **WhatsApp Instant Notifications**: Integrates with Twilio API to push real-time alerts showing only the available slots when seat availability shifts.

---

## 🛠️ Setup Instructions

### 1. Prerequisites
*   Python 3.8+
*   Google Chrome installed locally

### 2. Installation
Clone the repository and install dependencies inside a virtual environment:

```bash
# Clone the repository
git clone <repository-url>
cd vtop-add-drop-automation

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 3. Configuration
Copy the environment variables template and configure it with your credentials:

```bash
cp .env.example .env
```

Open `.env` and fill out:
*   `VTOP_USERNAME` & `VTOP_PASSWORD`
*   `GMAIL_ADDRESS` & `GMAIL_APP_PASSWORD` (if using course modification/OTP features)
*   `TWILIO` credentials & phone numbers (for WhatsApp alerts)
*   `CHOSEN_FACULTY` & `CHOSEN_SLOT` (to automate actions on a specific slot)

---

## 🚀 Running the Script

### Start Monitoring Loop
```bash
python main.py
```

### View DB Seat History
```bash
python show_db.py
```

---

## ⚠️ Security & Safety Warning

> [!WARNING]
> Automatically submitting forms on university portals can violate terms of service. Use this script responsibly. By default, `REGISTER` and `MODIFY` are set to `false` in `.env` to prevent accidental submissions. Always dry-run and verify browser behavior.

---

## 🤝 Acknowledgements & Credits

*   **CAPTCHA Solving Logic**: Credits to the **viboot** Chrome extension developers for the base CAPTCHA image solver model coefficients and solving algorithm adapted inside this script.
