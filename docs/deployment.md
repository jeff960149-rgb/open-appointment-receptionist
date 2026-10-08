# Deployment and operations

## One business, one process, persistent disk

Use the root `Dockerfile` or a Python 3.11+ environment. Run **one Uvicorn worker and one replica**. SQLite transactions protect bookings; demo sessions and the notification worker are process-local. Multi-replica deployments need shared database/session coordination and notification leases first.

Mount a persistent disk at `/data`, set `DATA_DIR=/data`, and back up the database using SQLite's backup API (or stop the process before copying). Keep WAL and SHM files with the database during raw backups. In a Docker host bind mount, ensure UID 10001 can write to the directory. The provided Compose named volume initializes permissions automatically.

The root Railway configuration is a convenience template. Supply your own variables and volume; do not connect this repository to an existing production service accidentally. Health check: `GET /health`. No deployment credentials or real service IDs are included.

## Admin access

Generate `ADMIN_TOKEN` with `secrets.token_urlsafe(32)`. Admin endpoints reject absent or short configured tokens. Use HTTPS externally. The AI tool set cannot change staffing, toggle AI, list other customers or cancel existing bookings.

```bash
python scripts/admin.py state
python scripts/admin.py capacity 2030-01-07 3
python scripts/admin.py ai on
python scripts/admin.py ai off
python scripts/admin.py cancel BOOKING_ID
python scripts/admin.py callback-complete CALLBACK_ID
```

The CLI reads `.env`; `ADMIN_BASE_URL` defaults to `http://127.0.0.1:8000`. `state` contains personal customer information. Keep terminal output private.

## Twilio

1. Set `PUBLIC_BASE_URL` to your public HTTPS origin with no path or trailing slash.
2. Set `TWILIO_AUTH_TOKEN` and your own `OPENAI_API_KEY`. Set a Realtime model supported by your account; the example retains the original `gpt-realtime-2.1` model. Availability is account-dependent.
3. In your own Twilio number's incoming voice configuration, use `POST https://YOUR-HOST/incoming-call`.
4. Optionally set comma-separated E.164 `HUMAN_FORWARD_NUMBERS` and registered `HUMAN_SIP_URIS`. `HUMAN_RING_SECONDS` defaults to 20. Twilio decides which answered leg connects; carrier voicemail can count as an answer.
5. Set `HUMAN_ONLY_CALLER_NUMBERS` to exact E.164 numbers that should never reach AI. No previous Kspa/Footprint exclusions are shipped.
6. Set `AI_FALLBACK_ENABLED=true` on first setup, or use the authenticated admin command. A saved toggle overrides the environment default afterward.

Twilio HTTP webhooks and the Media Streams WebSocket validate provider signatures. A short-lived, single-use stream token additionally binds each media connection to the server-known call and caller. Do not rewrite public URLs at your proxy; signature validation uses `PUBLIC_BASE_URL`. Standard Twilio Media Streams uses a `wss://` URL when signing a WebSocket handshake.

If AI is disabled, excluded for that caller, or missing a key, the caller gets an unavailable message after ringing. If a human answered, the call ends normally. A live Realtime failure currently ends the AI stream; carrier-specific voicemail recovery is not implemented.

Test with your own number before opening to customers: normal booking, a full slot, closed hours, incorrect signature, human answer, no answer, excluded caller, AI off, caller interruption and disconnect. The release's transport tests use mocked providers; no claim is made that your account has passed these live tests.

## Telegram

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and a random `TELEGRAM_WEBHOOK_SECRET` of at least 24 characters. Use a private staff chat/group whose members are trusted to change staffing and routing.

```bash
python scripts/setup_telegram.py
```

This registers your bot's webhook at `PUBLIC_BASE_URL/telegram/webhook`. Staff alerts are saved in an outbox with the booking/callback. The server retries unsent alerts every 30 seconds. Delivery is at least once; use the included ID to recognize an occasional duplicate after a crash. Without Telegram configured, records remain in the admin view and alerts stay pending. Staff must actually handle the callback; an AI acknowledgment does not make the phone call.

Commands: `/ai on`, `/ai off`, `/capacity YYYY-MM-DD N`. Supported Chinese shortcuts include `今天3人上班`, `今天有3個員工`, `今天有3個按摩師`, `明天兩人上班`. Date words use the business timezone. This parser is explicit and does not use an AI API; more complex staffing instructions are not supported in the shared core.

## Privacy and retention

The generic app stores booking names, phones, service/time details, callback reasons and notification payloads on your disk. It does not record audio or persist conversation transcripts. Twilio and OpenAI process live audio when enabled; Telegram receives the staff alert's customer details. Configure provider retention/access separately and tell callers about your AI/data use as appropriate for your business.

There is no automatic retention purge in v0.1. Establish your own retention policy and delete old records using a reviewed database maintenance process. Never upload a production database, `.env`, log dump, call transcript or recording to this repository.

Set `ENABLE_DEMO=false` to disable anonymous booking demos on a public deployment. The AI text test always requires admin authentication. Put public services behind a reverse proxy with request-size, connection and rate limits; the demo itself caps active sessions and proposals but is not a general DDoS protection layer.
