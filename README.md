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
python tools/show_db.py
```


## 🖥️ Web Interface & Live Dashboard

The application features a lightweight, high-performance web interface built with FastAPI that acts as a real-time command center:

*   **Interactive Course Configuration**: Update subjects to monitor directly from the web dashboard. Add new courses, set categories, page numbers, target slots/faculties, and choose actions (`modify` / `register` / `monitor`). Saving settings updates the memory configuration instantly and persists them to `monitored_courses.json`.
*   **Live Seats Subject Slider**: Paginate through seat availability of different courses gracefully using the Left (`←`) and Right (`→`) slider controls.
*   **Persistent & Dynamic Status**: Scraper status (Active/Sleeping/Crashed) and latest run timestamps render dynamically in real-time in your browser (utilizing cache-busting request parameters).
*   **Scrollable Activity Log**: View up to 100 historical seat transition changes inside a neat, scrollable component with sticky table headers.
*   **Live Console stream**: Stream stdout terminal output directly inside the dashboard.
*   **Timezone Localization**: All timestamps on the dashboard, SQLite database logs, and notifications are formatted in Indian Standard Time (IST - Asia/Kolkata).


## ⚙️ Automated Workflows (Registration vs. Modification)

The automation engine supports two distinct workflows running simultaneously. You can configure target actions, faculties, and slots on a per-course level directly within the `COURSES_TO_MONITOR` JSON array:

### Configuration Parameters per Course:
*   `action`: `"register"`, `"modify"`, or `"monitor"` (omit or leave blank to only monitor).
*   `target_faculty`: Faculty keyword to match (e.g. `"PRABHU J"`). If empty `""`, matches *any* faculty.
*   `target_slot`: Slot pattern to match (e.g. `"1"` to match any slot containing "1" like `D1`, `D1+TD1`, `G1+TG1`).
*   `category` & `page`: Category type (e.g. `DE`, `PC`, `UC`, or `View/Modify` / `Modify`) and page numbers.

### Terminal Output Settings:
*   `print_scrapper_data_in_terminal`: Set to `true` in `.env` to output real-time scraped slot statistics directly to your console.
    *   If all faculties/slots are full: prints `all the fac are full`.
    *   Otherwise: prints each available slot name and its remaining seat count.

### Heuristic Matcher:

The script filters open slots matching the `target_slot` pattern.
1. If the preferred `target_faculty` is available on any of the matching slots, it is selected.
2. If the preferred faculty is **not** available, the script automatically falls back to select **any** available faculty on a matching slot.

---

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
