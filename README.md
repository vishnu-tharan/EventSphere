# EventSphere – IT4123 ICA 02

A runnable educational multi-agent venue booking and digital ticketing website. The backend is **Python 3.11+ / FastAPI**, with **SQLAlchemy, Pydantic, PostgreSQL on Render, SQLite locally, Jinja/Bootstrap/JavaScript**, five JSON-message agents, simulated payments, QR code entry and timed waitlist offers.

This is project source code and an instructional demo, **not a deployed submission**. You must run, understand, test and deploy it, and include your own screenshots and real links in the report.

## Quickest Windows method

Open the project folder, double-click **`setup_windows.bat`**, then **`run_windows.bat`**. The scripts run the setup and startup commands below.

## Quick start – Windows PowerShell

```powershell
# Open PowerShell or VS Code Terminal in this folder
py -3.11 --version
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python seed.py
python -m uvicorn main:app --reload
```

If `py -3.11` is unavailable but Python 3.11+ is installed as `python`, replace the first two commands with `python --version` and `python -m venv .venv`. If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process Bypass` and retry; this affects only the current PowerShell process.

Open <http://127.0.0.1:8000>. FastAPI API documentation: <http://127.0.0.1:8000/docs>. Stop the server with `Ctrl+C`.

### Demo accounts (LOCAL ONLY)

| Role | Email | Default LOCAL demonstration password |
|---|---|---|
| Admin | `admin@eventsphere.test` | `AdminDemo!2026` |
| Staff | `staff@eventsphere.test` | `StaffDemo!2026` |
| Attendee | `attendee@eventsphere.test` | `StudentDemo!2026` |

The demo passwords are public examples, **not production credentials**. On Render set `DEMO_ADMIN_PASSWORD`, `DEMO_STAFF_PASSWORD`, `DEMO_ATTENDEE_PASSWORD` to new, strong unique passwords before setting `SEED_DEMO=1`. These are credentials for demo-only users; never use real passwords or payment details here. `seed.py` does not overwrite existing users' passwords: change the environment variables **before the database is seeded for the first time**.

## Tests

```powershell
python -m pytest -v
```

There are **26 tests** covering the agents, concurrency, QR issuance and check-in, refunds, waitlist, notifications, venue capacity, agent logs, and permissions. See tests/.

## Files

| File / directory | Purpose |
|---|---|
| `main.py` | FastAPI routes, login, website, CSRF, startup and 30-second sweeper |
| `agents.py` | Five message-driven agents with independent documented rules |
| `models.py` | Relational database tables (persistent state) |
| `database.py` | SQLite local, PostgreSQL production connection |
| `security.py` | PBKDF2 hashed passwords, verification |
| `seed.py` | Create demonstration users, venues and two events |
| `templates/` | HTML pages for attendees, staff, admin |
| `static/style.css` | Responsive design, 375px support |
| `tests/` | Pytest suite |
| `render.yaml` | Render Blueprint (FastAPI app + PostgreSQL) |
| `.env.example` | Sample environment-variable names; not loaded automatically |

## Demonstration flows

1. **Admin:** Login, create/edit venue, create event (draft → publish), confirm conflict is refused, view dashboard/messages, approve hire.
2. **Attendee:** Search/filter events, book Standard or VIP tickets, view per-ticket QR, payment reference, cancellation/refund, join waitlist and private hire.
3. **Staff:** Open `/staff/scanner`, select event, scan phone QR or manually type code; test double scan and wrong event.
4. **Waitlist timer:** Sell out category, join queue, cancel someone's booking, see held seat/notification, wait 5 minutes; background sweep runs every 30 seconds to re-offer.

The demo seed includes `Live QR Check-in Demo` starting around **one hour after first seed**. Book one of its tickets *before* its start time, then the gate staff can scan it (scanner window opens two hours before event). If the demo has become stale, use an admin account to create a **new future event starting in one hour**; you cannot book an event that has already begun.

## Documented rules (must explain in viva)

- **VenueAgent:** Checks ticket totals against current capacity before publishing. Uses a 30-minute gap between venue activities and proposes the earliest suitable slot in each eligible venue, up to three options. It calculates `hours × hourly_rate` and adds 20% on weekends (Asia/Colombo date). Requests above 70% capacity go to admin for approval.
- **BookingAgent:** At most 6 tickets per booking for a published event before its start. A conditional SQL update takes public stock only if enough remains. Booking, waitlist expiry, and cancellation lock the same event while changing its inventory. Payments use a `SIM-...` reference. Refunds are 100% at least 48 hours ahead, 50% from 24 to less than 48 hours, and 0% under 24 hours. Cancelling an event refunds all paid bookings in full.
- **TicketAgent:** `secrets.token_urlsafe(32)` per ticket; gates accept 2h before event start through event end, only for matching event, active ticket, active booking, and not scanned before. A conditional atomic SQL `UPDATE` guarantees single check-in.
- **WaitlistAgent:** First-come-first-served, 5-minute offers with a countdown in Notifications. The scheduler sweeps every 30 seconds, releases expired holds, and offers available inventory to the next eligible entry. An attendee can claim their hold from the event page even when public stock is zero. Cancelling the event closes its queue and releases its holds.
- **NotificationAgent:** Subscribes to broadcast `INFORM` messages and saves user notifications; reacts without being explicitly called by the UI.

Each exchanged message logs sender, receiver, performative, JSON content, conversation ID, and time. The admin dashboard can page through older messages or filter by conversation ID. State persists in PostgreSQL/SQLite tables (`agent_states`, `tickets`, `bookings`, `hire_requests`, `waitlist_entries`, `notifications`). Agents communicate through `MessageBus.send()`.

## Deployment to Render

1. Push this project to a **GitHub repository** without `.venv/`, `.env` or local `.db` files. `.gitignore` handles those.
2. Sign into Render; choose **New → Blueprint**, connect your GitHub repository, and use the included `render.yaml` to provision one web service and one PostgreSQL database. If Render cannot offer the requested free tier, choose an available supported plan/provider; fees and availability may change.
3. In the Render web service's **Environment**, set `SEED_DEMO=1` and three new strong passwords: `DEMO_ADMIN_PASSWORD`, `DEMO_STAFF_PASSWORD`, `DEMO_ATTENDEE_PASSWORD`. `SECRET_KEY` is generated by the Blueprint. Confirm `COOKIE_SECURE=true`, `APP_TIMEZONE=Asia/Colombo`, and that `DATABASE_URL` points to the provisioned PostgreSQL database.
4. Render's start command: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`. Build command: `pip install -r requirements.txt`. Single worker is intentional for this educational embedded scheduler.
5. Open Render's live `https://...onrender.com` URL; verify `/healthz`, login, venue/event creation, booking, QR check-in and `/admin` traces. Camera scanning uses HTTPS (localhost also works). A background worker/celery would be better in larger production systems.
6. Do **not** put a SQLite URL in Render: ephemeral filesystem is not durable. Use the PostgreSQL `DATABASE_URL` linked by the Blueprint.
7. Be mindful of free service sleeps and free Postgres lifetime; open the site and test it **before your viva**.
8. Include the real live URL and GitHub URL, filled-in report and local setup/test instructions in the final PDF.

Render's published FastAPI deployment guide: <https://render.com/docs/deploy-fastapi>.

## Known scope / education constraints

- Email delivery is not implemented: user notifications appear inside the website. The assignment marks email as **optional**.
- Payments and refunds are simulation only: no real card data is collected.
- The browser QR scanner needs `html5-qrcode` loaded from an external CDN and camera permission. **Manual entry fallback** works even if the library fails to load.
- The sample venue image is an external URL entered by an admin; a built-in gradient placeholder is shown if no image is supplied.
- This is a functional educational prototype, not audited production software; use single web worker and do not treat it as a financial system.
- No real live host, GitHub repository or personal screenshots are generated by merely extracting this ZIP. Those require your own accounts and deployment actions.
