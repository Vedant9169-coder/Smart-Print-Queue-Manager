# Smart Campus Print Queue Manager

A runnable, simulation-first Operating Systems project built with Python, Flask, SQLite, HTML, CSS, and JavaScript.

## What it demonstrates

- Job queues and process-like job states
- FCFS, Shortest Job First (SJF), Priority, and Priority + Aging scheduling
- Multi-printer allocation
- Thread-safe critical sections (`threading.RLock`)
- Producer-consumer style queue processing
- Printer failure simulation and re-queuing
- PDF upload and page-count extraction
- Student and administrator roles
- Waiting-time / turnaround-time analytics
- Live simulated printing progress

> This is a classroom simulation. It does not send documents to physical printers. Printer status and print duration are simulated so OS scheduling behavior is easy to demonstrate.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.10 or newer
- A browser

## Run on Windows (PowerShell)

1. Extract the ZIP.
2. Open PowerShell in the extracted `CampusPrintManager` folder.
3. Create a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, run this for the current terminal only:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1
   ```

4. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

5. Start the app:

   ```powershell
   python app.py
   ```

6. Open `http://127.0.0.1:5000` in your browser.

The SQLite database and uploads folder are created automatically on first run.

## Demo accounts

- Administrator: `admin` / `admin123`
- Student: `student` / `student123`

These are demo credentials only. Change them before deploying the app anywhere beyond your own machine. The development secret key must also be replaced for deployment.

## Suggested demo

1. Log in as `student`.
2. Submit a valid PDF, choose copies and priority, and observe the job state.
3. Open a second browser/incognito window and log in as `admin`.
4. Switch scheduling policies on the admin dashboard.
5. Toggle a printer offline and back online.
6. Watch the live queue, printer cards, and progress bars.
7. Open Analytics to inspect completed-job waiting and turnaround times.
8. Submit several jobs with different page counts/priorities to compare policies.

## Project structure

```text
CampusPrintManager/
├── app.py
├── requirements.txt
├── README.md
├── instance/                 # Created at runtime; SQLite DB
├── uploads/                  # Created at runtime; uploaded PDFs
├── templates/
│   ├── base.html
│   ├── index.html
│   ├── login.html
│   ├── student_dashboard.html
│   ├── admin_dashboard.html
│   ├── analytics.html
│   └── error.html
├── static/
│   ├── css/style.css
│   └── js/dashboard.js
└── tests/
    └── test_scheduler.py
```

## Scheduling policies

- **FCFS**: oldest waiting job first.
- **SJF**: smallest page-count × copies first (a simple duration estimate).
- **Priority**: URGENT, then HIGH, then NORMAL.
- **Priority + Aging**: waiting jobs gain effective priority over time to reduce starvation.

Jobs only go to printers compatible with the requested color mode. The current demo simulates print progress using a simple pages-per-minute model; real printers have additional setup, duplex, color, and device-specific delays.

## Safety and limitations

- Upload limit: 20 MB; maximum 500 pages per PDF.
- Only PDFs are accepted.
- Password-protected or malformed PDFs are rejected.
- This is a local educational demo, not production-ready campus software.
- The demo uses simple session authentication and seeded accounts; it does not include account registration, password reset, campus SSO, CSRF protection, antivirus scanning, or real printer integration.
- For a real deployment, add CSRF protection, HTTPS, stronger secret management, rate limits, safe file retention/deletion, campus authentication, and robust printer integration.
- SQLite is appropriate for a local demo. A multi-server deployment would need a server database and a dedicated job worker.
