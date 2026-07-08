# ⚡ VIT VTOP Course Add/Drop Automation Script

An advanced, high-performance, and feature-rich automation and monitoring tool for the **VIT VTOP Add/Drop Portal 2026-27**, built using Python, Playwright, SQLite, and a FastAPI dashboard.

---

## 🌟 Key Features & Capabilities

### 🤖 Automation & CAPTCHA Bypassing
*   **Intelligent CAPTCHA Solver**: Integrates a client-side base64 image-processing solver that automatically decrypts and solves VTOP login and progress CAPTCHAs, handling session timeouts without human intervention.
*   **Fast-Refresh Single-Course Loop**: Optimization that bypasses VTOP homepage dashboard reloading when monitoring a single course. It executes a local "Go Back ➡️ Proceed" loop to check seats up to 5x faster.
*   **Self-Healing Sessions**: Automatic detection of session timeouts or portal crashes, spawning a fresh browser context, re-logging in, and resuming monitoring.

### ⚙️ Automated Workflows
*   **1-Click Auto-Registration**: Set `REGISTER=true`, and the scraper will automatically navigate, select, and enroll in your specified course, faculty, and slot as soon as a seat opens.
*   **1-Click Auto-Modification (with Gmail OTP)**: Set `MODIFY=true` to handle swapping courses. The engine automatically navigates to the View/Modify menu, checks seat availability, polls Gmail via IMAP, extracts the 6-digit OTP, fills it, and submits the update.
*   **Heuristic Matcher**: Intelligent selection algorithm that:
    1. Prioritizes the exact combination of preferred faculty and slot.
    2. Falls back to select **any** available faculty matching the target slot if the preferred faculty is full.

### 🔒 Privacy & "Blur Mode" Anonymization
*   **Public Stream Protection**: A built-in "Blur Mode" (LinkedIn Anonymize) designed specifically for public screen sharing or video recording.
*   **Dynamic Client Redaction**: Automatically blurs sensitive elements in the UI—such as faculty names and personal phone numbers.
*   **Sanitized Console Logs**: Redacts phone numbers (matching `+91XXXXXXXXXX`) and target faculty names in real-time within the live terminal log viewer.

### 🖥️ Live Web Dashboard (FastAPI)
*   **Interactive Config Editor**: A dedicated profile settings modal where you can edit Display Name, Privacy settings, VTOP credentials, Gmail credentials, Twilio API credentials, and Scraper parameters in real-time. Settings are persisted directly back to the local `.env` file.
*   **Live Seat Status Slider**: Interactive cards that display real-time seat availability across all configured courses. Paginate through subjects using the intuitive Left (`←`) and Right (`→`) controls.
*   **Live Console Log Stream**: Polls stdout/stderr in real-time and streams terminal log outputs directly onto the dashboard with dynamic Blur Mode filters.
*   **IST Timezone Alignment**: Formatted in Indian Standard Time (IST - Asia/Kolkata) across dashboard counters, SQLite logs, and alerts.

### 📊 Logs & Database Analytics
*   **Relational SQLite DB Logging**: Persists seat status checks and status changes in a local SQLite database (`seats.db`).
*   **State-Transition Filtering**: Implements database optimization to suppress duplicate "Full" states, logging only numerical seat changes or actual availability transitions.
*   **Interactive Log Filters**: Filters dashboard activity logs dynamically based on the active course selected in the Seat Status slider.

### 💬 Real-Time Notifications
*   **Twilio WhatsApp alerts**: Transmits instant notifications showing real-time availability changes.
*   **Global Alert Gate**: Exposed toggles (`WHATSAPP_ENABLED`) in the environment settings to turn notifications on or off globally.

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
Copy the environment variables template and configure it:

```bash
cp .env.example .env
```

Open `.env` and fill out your configuration parameters:
*   `VTOP_USERNAME` & `VTOP_PASSWORD`
*   `GMAIL_ADDRESS` & `GMAIL_APP_PASSWORD` (for Gmail OTP auto-retrieval)
*   `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `MY_PHONE_NUMBER` (for WhatsApp alerts)
*   `COURSES_TO_MONITOR` (JSON list of courses, see section below)

---

## 🚀 Running the Script

### Start Monitoring Loop & Web Dashboard
```bash
python main.py
```

### View DB Seat History
```bash
python tools/show_db.py
```

---

## ⚙️ Automated Workflows (Registration vs. Modification)

The automation engine supports two distinct workflows running simultaneously. You can configure target actions, faculties, and slots on a per-course level directly within the `COURSES_TO_MONITOR` JSON array:

### Course Parameters:
*   `action`: `"register"`, `"modify"`, or `"monitor"` (omit or leave blank to only monitor).
*   `target_faculty`: Faculty keyword to match (e.g. `"PRABHU J"`). If empty `""`, matches *any* faculty.
*   `target_slot`: Slot pattern to match (e.g. `"1"` to match any slot containing "1" like `D1`, `D1+TD1`, `G1+TG1`).
*   `category` & `page`: Category type (e.g. `DE`, `PC`, `UC`, or `View/Modify` / `Modify`) and page numbers.

### Example Configuration:
```ini
COURSES_TO_MONITOR='[
  {
    "category": "DE",
    "keyword": "Cyber Security",
    "page": 2,
    "action": "register",
    "target_faculty": "",
    "target_slot": "1"
  },
  {
    "category": "View/Modify",
    "keyword": "Software Industrialization",
    "page": 1,
    "action": "modify",
    "target_faculty": "PRABHU J",
    "target_slot": "1"
  }
]'
```

This configuration executes the following loop:
1. **Registration Flow for Cyber Security**: Navigates to DE Page 2 ➡️ Scrapes slots. Looks for **any slot containing "1"** (e.g., `D1`, `D1+TD1`). If open, it registers (prioritizing the global `CHOSEN_FACULTY` if configured, otherwise taking any available faculty).
2. **Modification Flow for Software Industrialization**: Clicks **View / Modify** dashboard button ➡️ Clicks **Modify** in the matching course row ➡️ Looks for **any slot containing "1"** (e.g., `G1+TG1`). If open, it modifies using Gmail OTP (prioritizing `PRABHU J` if open, otherwise taking any available faculty).
3. **Loop**: Clicks Home ➡️ Sleep ➡️ Repeat.

---

## ⚠️ Security & Safety Warning

> [!WARNING]
> Automatically submitting forms on university portals can violate terms of service. Use this script responsibly. By default, `REGISTER` and `MODIFY` are set to `false` in `.env` to prevent accidental submissions. Always dry-run and verify browser behavior.

---

## 🤝 Acknowledgements & Credits

*   **CAPTCHA Solving Logic**: Credits to the **viboot** Chrome extension developers for the base CAPTCHA image solver model coefficients and solving algorithm adapted inside this script.
