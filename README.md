# DriveBot MCP server (pilot)

DriveBot is DriveCarClub's AI customer assistant, built on Perfox. Perfox runs the
conversation. This small server gives DriveBot the business logic and data it needs
through these tools:

| Tool | SOP | What it does |
|---|---|---|
| `check_dispatch_status` | SOP 5 | "Has my car been sent?" |
| `get_chauffeur_details` | SOP 6 | "Who is my chauffeur, and which car?" |
| `send_otp` | SOP 5 rule 1 | Website chat: texts a 6-digit code to the customer's number |
| `verify_otp` | SOP 5 rule 1 | Website chat: checks the code, then the number is trusted |
| `use_mobile_number` | | **UAT demos only** (`VERIFICATION_MODE=none`): website chat trusts the typed number, no code (section 5b) |
| `get_fare` | | "How much does a car cost?" Standard rates for a car type, trip type and city (section 6a) |

Nothing else (booking, cancellation, payments) is built yet.

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

- Health check: open http://127.0.0.1:8000/health. You should see `{"ok": true, "mode": "mock", "rate_mode": "mock"}`.
- Test website: open http://127.0.0.1:8000/ (the page in `web/index.html`). Logging in on the
  page needs HTTPS (the login cookie is `Secure`), so try it on Render. `localhost` also works
  in Chrome and Firefox.
- Perfox calls `POST /mcp` with the header `Authorization: Bearer <MCP_API_KEY>`.
- Perfox runs in the cloud, so it needs a public HTTPS address for this server
  (for example a tunnel such as ngrok while testing, or a hosted server for the pilot).
  Set `HOST=0.0.0.0` in `.env` when the server must accept outside connections.

There are two DriveCarClub modes, each `mock` (fake data) or `live` (the real API):

| Setting | Controls | Default |
|---|---|---|
| `DCC_MODE` | The booking tools: `check_dispatch_status`, `get_chauffeur_details` (and "Your pickups" on the test website) | `mock` |
| `DCC_RATE_MODE` | `get_fare` only | Empty = the same as `DCC_MODE` |

So, for example, `DCC_MODE=mock` with `DCC_RATE_MODE=live` gives real fares while booking
lookups stay on mock data. Any `live` mode needs `DCC_USERNAME` and `DCC_PASSWORD`. When
both are live they share one client, so one token serves both APIs. Any value other than
`live` counts as `mock` (with a startup warning). The server logs both modes at startup
(`dcc_modes`), and `/health` shows them as `mode` and `rate_mode`.

The mock data uses the same field names and formats as the real API, so mock and live go
through exactly the same mapping.

### DriveCarClub booking fields

The UAT DespatchedBooking API answers `{"status": "200", "message": "Success", "data": [...]}`.
A top-level `status` other than `"200"` counts as a failure (`bad_response`, so the tool
says `unavailable`). [app/mapper.py](app/mapper.py) reads these fields from each booking:

| We share | Real field | Notes |
|---|---|---|
| Booking reference | `actual_bk_num` | |
| Pickup date and time | `reportingDate` + `reportingTime` | `DD-MM-YYYY` and `HH:MM`, India time. If the date can't be read, the booking is kept (date left empty) and a `booking_date_unreadable` warning is logged. |
| Pickup area | `city_Name`, else `location` | Order set by `PICKUP_AREA_FIELD`. `reportingAddress` is never used. |
| Status | `status` | `Despatched` is shown as `Dispatched` |
| Chauffeur first name | `chauffeurName` | First word only, title case: `RAMESH KUMAR` → `Ramesh` |
| Car model | `carAlloted`, else `carBooked` | |
| Registration number | `carNumber` | Unchanged |
| Car colour | (none) | The API has no colour, so it is left out |
| Chauffeur mobile | `chaufferMobile` (DriveCarClub's spelling) | Hidden while `SHARE_CHAUFFEUR_MOBILE = False` |

Never read or shared: `individualsname`, `company_Name`, `b_Guest_Contact_No`,
`reportingAddress`, `standinginstructions`, `dsNo`. Our older guessed field names are
still accepted after the real ones.

| Setting (`.env`) | What it does |
|---|---|
| `PICKUP_AREA_FIELD` | `city_Name` (default) or `location`: which field is read out as the pickup area. DriveCarClub hasn't confirmed which is right. Any other value is ignored (with a startup warning). |
| `INCLUDE_PAST_BOOKINGS` | **UAT testing only.** `true` also shows pickups before today (the UAT test booking is in the past). Default `false`; a warning is logged at startup when it is `true`. |

`OTP_MODE=mock` (the default) sends no SMS, and the code is always **123456**.
`OTP_MODE=plivo` sends real SMS messages. See section 5 below.

## 4. Mock test numbers

In mock mode, use these as the customer's phone number (the `X-Sa-End-User-Phone` header).
Dates are always worked out from today in India time.

| Number | What happens |
|---|---|
| 919000000001 | One booking today, car sent. Chauffeur RAMESH KUMAR (shared as "Ramesh"), Toyota Innova Crysta, KA01AB1234 |
| 919000000002 | Three bookings: yesterday (Completed, so hidden), today and tomorrow |
| 919000000003 | No bookings |
| 919000000004 | One booking tomorrow, chauffeur not assigned yet |
| 919000000005 | DriveCarClub server error |
| 919000000006 | DriveCarClub does not answer in time (timeout) |

Mock fares (`get_fare`, any phone number or none, any upper/lower case):
Midsize / Local / Gurugram, Midsize / Local / New Delhi, Premium / Local / Gurugram, and
Midsize / Outstation / Gurugram (with an outstation allowance and night stay). Anything
else gives `not_found`.

## 5. OTP on website chat

On website chat anyone can type any number, so DriveBot checks that the number belongs
to the customer before sharing booking details. Phone calls, WhatsApp and SMS don't need
this, because there the number comes from the phone network.

**How it works**

1. The customer asks "Has my car been sent?". `check_dispatch_status` answers
   `verification_needed`, and DriveBot asks for their mobile number.
2. DriveBot calls `send_otp` with the number. The server texts a 6-digit code to it.
3. The customer tells DriveBot the code. DriveBot calls `verify_otp`.
4. If the code is right, the number **the code was sent to** is saved as the
   conversation's verified number. DriveBot then calls the booking tool again.

**Limits (per conversation)**

| Rule | Limit | Outcome when broken |
|---|---|---|
| Codes sent | 3 at most | `too_many_requests` |
| Time between sends | 30 seconds at least | `too_many_requests` |
| How long a code works | 5 minutes | `expired` |
| Wrong codes | 3, then no more codes in this conversation | `locked` |

Sending a new code replaces the old one. These limits are in [app/config.py](app/config.py).

**Testing:** with `OTP_MODE=mock`, no SMS is sent and the code is always `123456`.
The server logs a `mock_otp_on` warning at startup as a reminder. Never use mock mode in
production.

**Settings (in `.env`)**

| Setting | What it is |
|---|---|
| `OTP_MODE` | `mock` (default, testing only) or `plivo` (real SMS) |
| `OTP_SECRET` | Long random secret used to hash the codes. If empty, a new random one is made at each start. |
| `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` | Your Plivo account details |
| `PLIVO_SENDER_ID` | The sender ID or Plivo number the SMS comes from |
| `OTP_SMS_TEXT` | The SMS text. `{code}` is replaced with the code. India needs an approved template, so make it match exactly. |

**Keeping the code safe**

- The code is never stored. Only a hash of it is kept (HMAC-SHA256 with `OTP_SECRET`).
- The code and its hash are never put in a tool result or a log line. Logs show only
  the masked mobile, the outcome and the conversation id.
- If the SMS can't be sent, the tool answers `unavailable` and the server logs
  `alert_perfox_team`.
- SMS sending is in [app/sms.py](app/sms.py), so another provider can be added there later.

## 5b. Verification off for UAT demos (`VERIFICATION_MODE=none`)

For demos on UAT test data, OTP can be switched off completely. **Never in production:**
with it off, anyone can check any number's bookings on website chat.

| `VERIFICATION_MODE` | What happens |
|---|---|
| `otp` (default) | Everything as in section 5. Any value other than `none` counts as `otp` (with a startup warning). |
| `none` | No codes anywhere. Website chat and the test website trust the number the customer types. |

**Safety lock:** with `none`, the server **refuses to start** unless `DCC_BOOKING_URL`
contains `UAT` (any upper/lower case). There is no override. When it does start, it logs the
warning `verification_off`: "Verification is OFF: anyone can check any number's bookings.
UAT demo only."

**Chat, with `none`:**

- `tools/list` hides `send_otp` and `verify_otp` (they answer `not_available` if called anyway)
  and lists `use_mobile_number` instead.
- A booking tool answers `verification_needed` with "Please tell me the mobile number you
  booked with", and tells the AI to call `use_mobile_number`.
- `use_mobile_number(mobile_number)` normalises the number (`invalid_number` if it isn't an
  Indian mobile), saves it as the conversation's verified number (the same place `verify_otp`
  saves it), and signs in the matching website session too (shared login, as with
  `verify_otp`). It answers `ok`; the AI then calls the booking tool again. It never reads the
  number back.
- Phone, WhatsApp and SMS are unchanged: they always use the number from Perfox. On those
  channels `use_mobile_number` answers `not_needed` and the typed number is never used.
- With `otp`, `use_mobile_number` is not listed and answers `not_available`.

**Test website, with `none`:**

- `GET /api/session` also returns `"verification": "none"` (or `"otp"`).
- `POST /api/login` with `{"mobile": "..."}` signs in straight away (same CSRF check, new cookie,
  and the chat token keeps working, as after `/api/login/verify`). It is `404` with `otp`.
- `POST /api/login/send-code` and `/api/login/verify` are `404` with `none`.
- The login dialog has one step (mobile number, then **Log in**). No code step and no
  "always 123456" hint.

**Switching back:** set `VERIFICATION_MODE=otp` (or remove it) and restart. The OTP code was
never removed, so everything works as before.

### Shared login: test website + chat

The test website and the chat share one login, because they run in the same server:

- **Logged in on the website:** the chat doesn't ask for a code.
- **Logged in through the chat's code:** the website shows "Signed in as ••••0001".
- **Logged out on the website:** the chat needs a code again.

**How it works**

1. Each visitor gets a secret cookie, `dcc_sid`. JavaScript can't read it (HttpOnly), and it is
   only sent over HTTPS. The server keeps a session for it in memory: the verified number (if
   any) and a separate random **chat token**.
2. The page asks `GET /api/session` for the chat token and gives it to Perfox:
   `Perfox('identify', { tenant_session_token: chat_token })`. It does this on every page load
   and after every login or logout.
3. Perfox sends the chat token to our server with every tool call, in the
   `X-Sa-User-Token` header. That's how a tool call finds the website session:
   - Signed in on the website: the chat uses that number. No code is needed.
   - Signed out: `verification_needed`, even if this chat did an OTP earlier.
   - No token, or one we don't know: the normal chat OTP rules (above).
4. A correct code in the chat (`verify_otp`) also signs in the website session for that token.
   The page notices this when the bot replies or when you come back to the tab.
5. Logging out gives the session a **new** chat token. The old one stops working, and the
   page calls `Perfox('shutdown')` so the chat starts fresh.
6. A session that isn't used for **8 hours** is removed.

The website login uses the same code rules and limits as the chat (3 sends, 30 seconds apart,
5 minutes, 3 wrong tries). The page only ever sees `••••0001`, never the full number.

**Website API** (in [app/website.py](app/website.py))

| Endpoint | What it does |
|---|---|
| `GET /api/session` | Signed in or not, the masked number, the chat token, and `verification` (`otp` or `none`) |
| `POST /api/login/send-code` | `{"mobile": "..."}`: texts a code (`404` with `VERIFICATION_MODE=none`) |
| `POST /api/login/verify` | `{"code": "..."}`: checks it and signs in (and changes the cookie) (`404` with `none`) |
| `POST /api/login` | `{"mobile": "..."}`: signs in with no code. **`VERIFICATION_MODE=none` only** (section 5b), otherwise `404` |
| `POST /api/logout` | Signs out and makes a new chat token |
| `GET /api/my-pickups` | Today's and upcoming pickups, with safe fields only. `401` when signed out. |

The `POST` endpoints only accept requests whose `Origin` header is our own site, so other
websites can't post with a customer's cookie (CSRF protection).

**Perfox setup**

- In Perfox Studio > Web Chat > Install, set **Allowed origins** to the Render URL:
  `https://drivebot-dczp.onrender.com`. Otherwise the chat widget won't load on the page.
- Leave **Require HMAC identity verification** off, and don't send `external_id`.
- In `web/index.html`, only the first part of the embed code (the small `window.Perfox` queue)
  is pasted at the marked spot. The page loads `widget.js` itself, **after** it has called
  `identify`, because the widget reads the user only once, when its chat starts. If you get new
  embed code, update `WIDGET_SRC` and `WIDGET_SITE` in the page's script.
- After a logout the page calls `shutdown` and then `identify` with the new token, straight
  after each other, so the chat restarts with the new token.
- Open the browser console with "Verbose" logs on to see `[drivebot]` lines: session loaded,
  identify called, widget script loading, bubble shown, widget ready.

## 6. Safety rules (enforced in code, not left to the AI)

1. **Trusted number only.** The booking tools never take the mobile number as an argument.
   - Phone call, WhatsApp, SMS: the `X-Sa-End-User-Phone` header Perfox sends is trusted.
   - Website chat: trusted only after OTP in the chat or a login on the test website
     (section 5). Until then the booking tools return
     `verification_needed`. `TRUST_WEB_PHONE=true` skips this for development only, and
     `VERIFICATION_MODE=none` for UAT demos only (section 5b; it won't start outside UAT).
   - `send_otp` is the only tool that takes a typed number. It only sends a code to that
     number and returns no booking data.
   - No number, or any other channel: `unavailable`.
2. **Own bookings only.** `get_chauffeur_details` fetches the caller's bookings again and
   checks the booking reference belongs to them. If not, it says `not_found` and reveals nothing.
3. **Allowlisted fields only.** Only the fields built in [app/mapper.py](app/mapper.py) leave the server:
   - Dispatch status: booking reference, pickup date, time and area (never the full address),
     status, and whether a chauffeur is allocated. No chauffeur details.
   - Chauffeur details: booking reference, pickup date and time, chauffeur **first name only**,
     car model, colour (only if DriveCarClub sends one; the UAT API doesn't) and registration number.
   - Never shared: surname, the customer's name, company and guest number, addresses,
     standing instructions, other drivers' details. The chauffeur's
     mobile stays hidden while `SHARE_CHAUFFEUR_MOBILE = False` in [app/config.py](app/config.py).
4. **Today and upcoming only** (India time, from `reportingDate`). Completed, closed and
   cancelled bookings are hidden, and the earliest pickup comes first. Only
   `INCLUDE_PAST_BOOKINGS=true` (UAT testing) shows past pickups.
5. **"Allocated"** means the booking has both a chauffeur name and a registration number.
6. **Never guess.** If DriveCarClub fails (sign-in fails, 401 twice, 5xx, network error or
   timeout) the tool returns `unavailable`, DriveBot points to customer support, and the server
   logs an `alert_perfox_team` line.
7. **No support number on the server.** The customer support number lives only in the
   Perfox knowledge base. Where a tool's `say` sends the customer to support, it contains
   `[share the customer support number from the knowledge base]`, and every tool description
   tells the AI to fill that in from the knowledge base. No tool result or log line has a
   support phone number. The test website just says "Please call our customer support team."
8. **Careful logging.** One JSON line per tool call, with the mobile masked (`91******3210`).
   Full numbers, tokens and passwords are never logged.

Every tool result has an `outcome` (a fixed code) and a `say` field with the SOP wording
for DriveBot to use.

## 6a. Fares: `get_fare`

`get_fare(car_category, service_type, city_name)` calls DriveCarClub's **GetRate** API
(`DCC_RATE_URL`) with the same token as DespatchedBooking (same caching, one retry on 401,
same timeout). It sends `{"CarCategory": ..., "ServiceType": ..., "CityName": ...}` and gets
back `{"status": "200", "message": "Success", "data": [packages]}`.

- **No identity needed.** Fares are not personal data, so it works on every channel,
  including website chat without OTP.
- **All three inputs are required.** The tool description tells the AI to ask the customer
  for any missing one and never to guess, especially the city.
- **Input checks.** Letters, spaces and hyphens only, at most 40 characters. Anything else
  gives `invalid_input`, and DriveCarClub is not called. If an allow-list setting is filled,
  the value must be on it (any upper/lower case).
- **Live or mock.** `DCC_RATE_MODE` chooses, separately from the booking tools (section 3).
- **Cache.** Each (car category, service type, city) answer that found fares is kept for
  1 hour, in any upper/lower case. `not_found` and failures are not cached, so they are
  asked again next time.

| Outcome | When | Result |
|---|---|---|
| `found` | DriveCarClub has rates | `packages`: `package_name`, `service_type`, `car_category`, `city`, `rate_inr`, `extra_km_rate_inr`, `extra_hour_rate_inr`, plus `outstation_allowance_inr` and `night_stay_inr` only when above 0. Amounts are numbers. `say` quotes them exactly as standard rates, followed by `FARE_NOTE`. |
| `not_found` | Empty `data` list | `say` explains no rate was found and points to customer support. Never a guessed fare. |
| `invalid_input` | Missing, not plain text, too long, or not on an allow-list | `say` asks for the detail again |
| `unavailable` | API error, `status` not `"200"`, timeout, or rates we can't read | `say` points to customer support; `alert_perfox_team` is logged |

The log line has the tool, the three inputs (only those that passed the checks), the
`source` of the fares (`live`, `cache` or `mock`; empty when the inputs were refused), the
outcome and the duration. No phone number is involved.

| Setting (`.env`) | What it does |
|---|---|
| `DCC_RATE_MODE` | `mock` or `live` for `get_fare` only. Empty = the same as `DCC_MODE`. |
| `DCC_RATE_URL` | The GetRate URL (default: the UAT URL) |
| `FARE_CAR_CATEGORIES`, `FARE_SERVICE_TYPES`, `FARE_CITIES` | Optional allow-lists, comma-separated (for example `Midsize, Premium`). Empty = no check, because DriveCarClub hasn't given us the lists yet. |
| `FARE_NOTE` | Added to every fare answer. Default: "Tolls, parking and taxes may be extra. For contract rates or an exact quote, please call our customer support team." |

## 7. Project layout

| File | What it does |
|---|---|
| `app/server.py` | FastAPI app: `/mcp` (JSON-RPC) and `/health`, API-key check |
| `app/website.py` | The test website: serves `web/`, and the login and pickups API |
| `app/browser_sessions.py` | Website sessions (cookie, chat token, 8-hour expiry) |
| `web/index.html` | The test page: log in, your pickups, the Perfox chat |
| `app/tools.py` | The four tools, their descriptions, and the SOP wording |
| `app/otp.py` | OTP rules: making, hashing and checking codes, and the limits |
| `app/sms.py` | Sending the SMS (mock or Plivo) |
| `app/identity.py` | Trusted-number rules, phone formatting, per-conversation sessions |
| `app/mapper.py` | `FIELD_MAP` (real DriveCarClub field names), date parsing, filtering and the field allowlists |
| `app/drivecarclub.py` | Live DriveCarClub client: token caching, one retry on 401, timeouts |
| `app/fares.py` | `get_fare` input checks, GetRate field mapping, and the 1-hour fare cache |
| `app/mock.py` | Fake data for the test numbers, and fake fares |
| `app/config.py` | All settings (read from `.env`) |
| `app/log.py`, `app/errors.py` | JSON logging, and the "DriveCarClub unavailable" and "SMS unavailable" errors |
| `tests/run_tests.py` | All tests |
| `pyproject.toml`, `uv.lock` | Package list and exact locked versions (used by uv) |

## 8. Still waiting on DriveCarClub IT

These are guesses in the code today. Each one is easy to change when the answer arrives.

| Item | What the code assumes now | Where to change it |
|---|---|---|
| Pickup area: `city_Name` or `location`? | `city_Name` | `PICKUP_AREA_FIELD` in `.env` |
| Sample with several bookings, and with no bookings (is it `data: []`, `data: null` or another `status`?) | `status` `"200"` with a `data` list; anything else is `bad_response` | `unwrap_response_list` in `app/mapper.py` |
| Fare lists: car categories, service types and cities | No check (any plain text is sent to GetRate) | `FARE_CAR_CATEGORIES`, `FARE_SERVICE_TYPES`, `FARE_CITIES` in `.env` |
| GetRate production URL | UAT URL | `DCC_RATE_URL` in `.env` |
| Status values besides `Despatched` (arrived, completed, cancelled...) | Statuses containing "complete", "close" or "cancel" are hidden | `FINISHED_STATUS_WORDS` in `app/mapper.py` |
| Does the API return past trips? | We filter out anything before today ourselves | `app/mapper.py` |
| Token API: method, response format, token field name | POST; plain string or JSON with `access_token`/`token`/`Token`/`AccessToken`/`data.token` | `app/drivecarclub.py` |
| Token expiry | `expires_in` if given, otherwise `DCC_TOKEN_TTL_SECONDS` (3000) | `.env` |
| Header used to send the token | `Authorization: Bearer <token>` | `DCC_TOKEN_HEADER`, `DCC_TOKEN_PREFIX` in `.env` |
| Error codes and messages (for example, is "no bookings" a 404?) | Anything except 200 counts as a failure | `app/drivecarclub.py` |
| Production URLs, credentials and rate limits | UAT URLs | `DCC_TOKEN_URL`, `DCC_BOOKING_URL` in `.env` |
| Share the chauffeur's mobile? (SOP 6 open item 2) | No | `SHARE_CHAUFFEUR_MOBILE` in `app/config.py` |
| Approved SMS template and sender ID for the OTP | Text from the brief | `OTP_SMS_TEXT`, `PLIVO_SENDER_ID` in `.env` |

## 9. Good to know

- Sessions (per Perfox conversation) are kept in memory, so restarting the server clears
  them. That's fine for the pilot. The OTP data and `verified_mobile` are kept there, so a
  restart means website customers verify again. Website logins are in memory too, so a
  restart signs everyone out of the test website.
- The server is built with FastAPI and runs on uvicorn, which is production-grade.
  FastAPI's automatic API page is at http://127.0.0.1:8000/docs, but only when
  `ENABLE_DOCS=true` in `.env`. It is off by default, because the server is public on Render.
- Run only **one** uvicorn worker process. Sessions and the cached DriveCarClub token live
  in memory, so several worker processes would each keep their own copy.
- The tests use FastAPI's `TestClient`, which needs the dev-only package `httpx2`.
  `uv sync` installs it automatically.
