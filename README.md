# DriveBot MCP server (pilot)

DriveBot is DriveCarClub's AI customer assistant, built on Perfox. Perfox runs the
conversation. This small server gives DriveBot the business logic and data it needs
through two tools:

| Tool | SOP | What it answers |
|---|---|---|
| `check_dispatch_status` | SOP 5 | "Has my car been sent?" |
| `get_chauffeur_details` | SOP 6 | "Who is my chauffeur, and which car?" |

Nothing else (OTP, booking, cancellation, payments) is built yet.

---

## 1. Setup (once)

This project uses [uv](https://docs.astral.sh/uv/) to manage Python and the packages.
uv creates the virtual environment (`.venv`) for you, and it can download Python too,
so you don't need Python installed separately.

**Install uv (once per computer)**

- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- Mac / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`

**Set up the project**

Windows (PowerShell):

```powershell
cd DriveBot
uv sync
copy .env.example .env
```

Mac / Linux:

```bash
cd DriveBot
uv sync
cp .env.example .env
```

`uv sync` installs the exact package versions from `uv.lock` into `.venv`.

Then open `.env` and fill in the values. Each setting is explained in the file.
Never commit `.env`. It holds secrets and is already in `.gitignore`.

You don't need to "activate" anything: `uv run ...` always uses the project's `.venv`.

**Adding or upgrading a package later:** `uv add <package>==<version>` updates
`pyproject.toml` and `uv.lock` together.

## 2. Run the tests

```
uv run python -m tests.run_tests
```

Each check prints `PASS` or `FAIL`, with a total at the end. The tests use mock data
and a small fake DriveCarClub server, so they need no internet or passwords.

## 3. Run the server

```
uv run python -m app.server
```

- Health check: open http://127.0.0.1:8000/health. You should see `{"ok": true, "mode": "mock"}`.
- Perfox calls `POST /mcp` with the header `Authorization: Bearer <MCP_API_KEY>`.
- Perfox runs in the cloud, so it needs a public HTTPS address for this server
  (for example a tunnel such as ngrok while testing, or a hosted server for the pilot).
  Set `HOST=0.0.0.0` in `.env` when the server must accept outside connections.

`DCC_MODE=mock` (the default) uses fake data. `DCC_MODE=live` calls the real DriveCarClub
API and needs `DCC_USERNAME` and `DCC_PASSWORD`.

## 4. Mock test numbers

In mock mode, use these as the customer's phone number (the `X-Sa-End-User-Phone` header).
Dates are always worked out from today in India time.

| Number | What happens |
|---|---|
| 919000000001 | One booking today, car sent. Chauffeur Ramesh Kumar, white Toyota Innova Crysta, KA 01 AB 1234 |
| 919000000002 | Three bookings: yesterday (Completed, so hidden), today and tomorrow |
| 919000000003 | No bookings |
| 919000000004 | One booking tomorrow, chauffeur not assigned yet |
| 919000000005 | DriveCarClub server error |
| 919000000006 | DriveCarClub does not answer in time (timeout) |

## 5. Safety rules (enforced in code, not left to the AI)

1. **Trusted number only.** The customer's mobile is never a tool argument. It comes
   only from the `X-Sa-End-User-Phone` header Perfox sends.
   - Phone call, WhatsApp, SMS: trusted.
   - Website chat: trusted only after an OTP check (not built yet). Until then the tool
     returns `verification_needed`. `TRUST_WEB_PHONE=true` skips this for development only.
   - No number, or any other channel: `unavailable`.
2. **Own bookings only.** `get_chauffeur_details` fetches the caller's bookings again and
   checks the booking reference belongs to them. If not, it says `not_found` and reveals nothing.
3. **Allowlisted fields only.** Only the fields built in [app/mapper.py](app/mapper.py) leave the server:
   - Dispatch status: booking reference, pickup date, time and area (never the full address),
     status, and whether a chauffeur is allocated. No chauffeur details.
   - Chauffeur details: booking reference, pickup date and time, chauffeur **first name only**,
     car model, colour and registration number.
   - Never shared: surname, addresses, drop location, other drivers' details. The chauffeur's
     mobile stays hidden while `SHARE_CHAUFFEUR_MOBILE = False` in [app/config.py](app/config.py).
4. **Today and upcoming only** (India time). Completed, closed and cancelled bookings are
   hidden, and the earliest pickup comes first.
5. **"Allocated"** means the booking has both a chauffeur name and a registration number.
6. **Never guess.** If DriveCarClub fails (sign-in fails, 401 twice, 5xx, network error or
   timeout) the tool returns `unavailable`, DriveBot gives the support number, and the server
   logs an `alert_perfox_team` line.
7. **Careful logging.** One JSON line per tool call, with the mobile masked (`91******3210`).
   Full numbers, tokens and passwords are never logged.

Every tool result has an `outcome` (a fixed code) and a `say` field with the SOP wording
for DriveBot to use.

## 6. Project layout

| File | What it does |
|---|---|
| `app/server.py` | FastAPI app: `/mcp` (JSON-RPC) and `/health`, API-key check |
| `app/tools.py` | The two tools, their descriptions, and the SOP wording |
| `app/identity.py` | Trusted-number rules, phone formatting, per-conversation sessions |
| `app/mapper.py` | `FIELD_MAP` and the field allowlists. **Update this when the real sample arrives.** |
| `app/drivecarclub.py` | Live DriveCarClub client: token caching, one retry on 401, timeouts |
| `app/mock.py` | Fake data for the test numbers |
| `app/config.py` | All settings (read from `.env`) |
| `app/log.py`, `app/errors.py` | JSON logging and the "DriveCarClub unavailable" error |
| `tests/run_tests.py` | All tests |
| `pyproject.toml`, `uv.lock` | Package list and exact locked versions (used by uv) |

## 7. Still waiting on DriveCarClub IT

These are guesses in the code today. Each one is easy to change when the answer arrives.

| Item | What the code assumes now | Where to change it |
|---|---|---|
| Sample DespatchedBooking response (one and several bookings) | Field names like `BookingId`, `PickupDateTime`, `DriverName`, `CarNo`; a list, or a list inside `data`/`Data`/`bookings`/`result` | `FIELD_MAP` and `LIST_KEYS` in `app/mapper.py` |
| Status values (sent, arrived, completed, cancelled...) | Statuses containing "complete", "close" or "cancel" are hidden | `FINISHED_STATUS_WORDS` in `app/mapper.py` |
| Does the API return past trips? | We filter out anything before today ourselves | `app/mapper.py` |
| Token API: method, response format, token field name | POST; plain string or JSON with `access_token`/`token`/`Token`/`AccessToken`/`data.token` | `app/drivecarclub.py` |
| Token expiry | `expires_in` if given, otherwise `DCC_TOKEN_TTL_SECONDS` (3000) | `.env` |
| Header used to send the token | `Authorization: Bearer <token>` | `DCC_TOKEN_HEADER`, `DCC_TOKEN_PREFIX` in `.env` |
| Error codes and messages (for example, is "no bookings" a 404?) | Anything except 200 counts as a failure | `app/drivecarclub.py` |
| Production URLs, credentials and rate limits | UAT URLs | `DCC_TOKEN_URL`, `DCC_BOOKING_URL` in `.env` |
| Share the chauffeur's mobile? (SOP 6 open item 2) | No | `SHARE_CHAUFFEUR_MOBILE` in `app/config.py` |
| Customer support number | Placeholder text | `SUPPORT_NUMBER` in `.env` |

## 8. Good to know

- Sessions (per Perfox conversation) are kept in memory, so restarting the server clears
  them. That's fine for the pilot. The future OTP step will store `verified_mobile` there.
- The server is built with FastAPI and runs on uvicorn, which is production-grade.
  While the server is running, FastAPI's automatic API page is at http://127.0.0.1:8000/docs.
- Run only **one** uvicorn worker process. Sessions and the cached DriveCarClub token live
  in memory, so several worker processes would each keep their own copy.
- The tests use FastAPI's `TestClient`, which needs the dev-only package `httpx2`.
  `uv sync` installs it automatically.
