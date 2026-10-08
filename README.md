# Open Appointment Receptionist

**An open-source AI receptionist for businesses that take appointments.**

Built from the practical lessons of **Kspa / Kiki** and **Footprint / Karin**, then separated from their private customer data and deployment settings. The goal is simple: help small businesses answer questions, check real availability, and turn a conversation into a confirmed appointment.

[繁體中文](docs/README.zh-TW.md) · [Configuration](docs/configuration.md) · [Phone & Telegram setup](docs/deployment.md) · [Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md)

## Start with your kind of business

The default receptionist uses **services, durations, prices, hours and shared capacity**. It does not require massage-specific terminology.

| Profile | Example services |
| --- | --- |
| `examples/generic.json` | Consultations |
| `examples/salon.json` | Haircuts and coloring |
| `examples/pet-grooming.json` | Pet grooming |
| `examples/repair.json` | Diagnostics and repair appointments |
| `examples/kspa.json` | Swedish, Deep Tissue and Signature massage |
| `examples/footprint.json` | Foot, body, combination and four-hands massage |

Profiles are examples, **not live store listings**. Addresses, hours, staffing and contacts are illustrative. Replace them before accepting customers.

## Try it in five minutes

Python **3.11+** is required. The booking demo is local and needs **no API key**.

```bash
git clone https://github.com/jeff960149-rgb/open-appointment-receptionist.git
cd open-appointment-receptionist
python3 -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# Windows PowerShell: Copy-Item .env.example .env
uvicorn receptionist.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://127.0.0.1:8000**. Choose a weekday and a time during the example business hours, prepare a booking, then confirm it. Each demo session gets its own empty temporary calendar. Demo bookings and callbacks never reach the live calendar or staff notifications.

To try a different business, change `.env`:

```dotenv
BUSINESS_CONFIG=examples/salon.json
```

Restart the server. That profile now controls the calendar, demo and AI instructions.

### Docker

```bash
cp .env.example .env
docker compose up --build
```

The supplied Compose file binds to localhost and persists real bookings in a named volume. Put it behind HTTPS when connecting a phone provider. Configuration changes require a restart.

## What is included

- A local appointment calendar with business hours, date-specific closures, capacity overrides, party size and service buffers.
- Two-stage booking: prepare → read back → customer confirms → recheck and save atomically. Retries of the same confirmation do not create another booking.
- Optional **Twilio → OpenAI Realtime** phone bridge, including interruption handling.
- Simultaneous human phone/SIP ringing, a persistent AI fallback switch, and excluded caller numbers.
- Staff callback requests saved with the caller's number, and Telegram alerts with durable retry storage.
- Telegram capacity commands, such as `今天3人上班`, `明天有2個員工` or `/capacity 2030-01-07 3`.
- An isolated local booking demo and an optional authenticated AI text test.
- Sanitized Kspa and Footprint reference implementations, including the original Smiley integration, LINE/Telegram administration and browser voice testing.
- Regression tests, CI, configuration examples and an MIT license for first-party code.

## AI and phone setup

The software is free to use and modify. **OpenAI, Twilio and hosting are separate services and may charge fees.** No API accounts, keys or model credits are included.

For the AI text test, set your own `OPENAI_API_KEY`, a supported `OPENAI_REALTIME_MODEL`, and an `ADMIN_TOKEN` of at least 24 characters. Generate a token locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `.env`, restart, and enter the same admin token in the page. The token is kept in the page's memory, not its URL or browser storage. AI text goes to OpenAI; phone audio also goes to Twilio and OpenAI. This is **not an offline voice model**.

Follow [deployment.md](docs/deployment.md) to attach your own Twilio number, set forwarding, and configure Telegram. AI fallback starts **off** until you explicitly enable it.

## Scope of v0.1

This is an **early, self-hosted release**, with one business and one server process per deployment.

The generic calendar treats capacity as interchangeable workers, chairs or rooms. It does **not** assign named employees, match employee skills, synchronize Google Calendar, accept payments, or automatically reschedule existing appointments. Rescheduling and customer cancellation requests become staff callbacks; administrators can cancel through the API/CLI.

The generic app's confirmed booking is saved in **its local SQLite calendar**. The Kspa Smiley code is supplied under `reference/`, not silently connected to the generic calendar. Do not run two independent booking systems for the same resources without a synchronization adapter.

Automated checks cover the calendar, authentication, notifications and transport behavior with mocked providers. They do not replace a real end-to-end test using your own Twilio number, model account and staff workflow. No live calls to customers were made for this release.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
python scripts/check_public_files.py
```

```text
receptionist/   Configurable app, calendar, tools, voice bridge, demo
examples/       Six editable business profiles
reference/      Sanitized Kspa and Footprint implementations
tests/          Network-free regression tests for the generic app
scripts/        Admin CLI, Telegram setup and public-file checks
docs/           Setup, architecture and contribution guidance
```

Read [SECURITY.md](SECURITY.md) before exposing the service to the internet. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the separate Twilio SDK license in the reference source.

## Why share it?

Small businesses should not have to start from zero to build an appointment assistant. Kspa and Footprint exposed real problems: asking for duration before answering opening hours, inventing availability, telling someone to call the number they were already calling, and confirming appointments that had not actually been saved. This project turns those lessons into reusable code and welcomes improvements from other types of businesses.
