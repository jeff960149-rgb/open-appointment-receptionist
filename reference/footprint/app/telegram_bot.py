import logging
import asyncio
import re

import httpx


logger = logging.getLogger("ai-receptionist.telegram")
# Telegram Bot API credentials are embedded in the request path. HTTPX logs
# complete request URLs at INFO, so suppress transport-level URL logging.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_image_locks: dict[str, asyncio.Lock] = {}


def schedule_image_attachment(message: dict) -> dict | None:
    photos = message.get("photo") or []
    if photos:
        return max(photos, key=lambda item: item.get("width", 0) * item.get("height", 0))
    document = message.get("document") or {}
    if document.get("mime_type") in {"image/jpeg", "image/png"}:
        return document
    return None


async def download_schedule_image(bot_token: str, attachment: dict) -> tuple[bytes, str]:
    if attachment.get("file_size", 0) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds 10 MB")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile",
            json={"file_id": attachment["file_id"]},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise ValueError("Telegram file unavailable")
        file = data["result"]
        path = file.get("file_path", "")
        if (not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path
                or path.startswith("/") or file.get("file_size", 0) > MAX_IMAGE_BYTES):
            raise ValueError("Invalid Telegram image")
        chunks = bytearray()
        async with client.stream("GET", f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{path}") as image:
            image.raise_for_status()
            async for chunk in image.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_IMAGE_BYTES:
                    raise ValueError("Image exceeds 10 MB")
    content = bytes(chunks)
    if content.startswith(b"\xff\xd8\xff"):
        return content, "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return content, "image/png"
    raise ValueError("Please send a JPG or PNG image")


async def process_telegram_schedule_image(
    bot_token: str, chat_id: str, attachment: dict, *,
    gemini_api_key: str, gemini_model: str, openai_api_key: str, vision_model: str,
) -> None:
    from app import line_bot, storage
    from app.schedule_parser import normalize_schedule_records

    owner = f"telegram:{chat_id}"
    async with _image_locks.setdefault(owner, asyncio.Lock()):
        # A failed new upload must not leave an older preview confirmable.
        storage.discard_staged_schedule(owner)
        try:
            content, mime = await download_schedule_image(bot_token, attachment)
            if gemini_api_key:
                try:
                    extracted = await line_bot.extract_schedule_with_gemini(
                        content, mime, gemini_api_key, gemini_model
                    )
                except Exception:
                    if not openai_api_key:
                        raise
                    extracted = await line_bot.extract_schedule_from_image(
                        content, mime, openai_api_key, vision_model
                    )
            elif openai_api_key:
                extracted = await line_bot.extract_schedule_from_image(
                    content, mime, openai_api_key, vision_model
                )
            else:
                raise ValueError("Image recognition is not configured")
            if extracted.get("document_type") == "service_log":
                date = extracted.get("document_date", "")
                records = extracted.get("service_records", [])
                with storage.connect() as connection:
                    staff_rows = storage._staff_shifts(connection, date)
                    manual = storage._manual_staffing_exists(connection, date)
                shifts = {r["therapist"]: (r["start_time"], r["end_time"])
                          for r in staff_rows}
                if not staff_rows and not manual:
                    shifts = dict(storage.DEFAULT_STAFF_SHIFTS)
                # For historical sheets there may be no stored shift. Never infer
                # a full working day from a single handwritten booking.
                missing = sorted({r["employee"].strip() for r in records
                                  if r["employee"].strip().casefold()
                                  not in {name.casefold() for name in shifts}})
                if missing:
                    preview = line_bot.format_service_log_preview(date, records, [], extracted.get("warnings", []))
                    preview = preview.split("\n根据以上占用时段")[0]
                    await send_telegram_text(bot_token, chat_id, preview +
                        "\n尚未更新班表。缺少以下人员当天班次：" + ", ".join(missing) +
                        "。请先设定完整上下班时间，再重传照片。")
                    return
                rows = line_bot.derive_availability_from_bookings(date, records, shifts=shifts)
                preview = line_bot.format_service_log_preview(date, records, rows, extracted.get("warnings", []))
            else:
                rows = normalize_schedule_records(extracted.get("rows", []))
                preview = line_bot.format_schedule_preview(rows, extracted.get("warnings", []))
            if not rows:
                raise ValueError("No confirmable schedule rows")
            if any(row["therapist"] not in storage.KNOWN_THERAPISTS for row in rows):
                raise ValueError("Unknown or departed therapist")
            storage.stage_schedule(owner, rows)
            await send_telegram_text(bot_token, chat_id, preview)
        except Exception as exc:
            storage.discard_staged_schedule(owner)
            # Exception messages/tracebacks may contain Telegram token-bearing URLs.
            logger.warning("Telegram schedule image failed: %s", type(exc).__name__)
            await send_telegram_text(bot_token, chat_id,
                "照片未更新班表。请传清晰的 JPG/PNG（10 MB 内），确认日期和上下班时间后重试。")


def split_telegram_text(text: str) -> list[str]:
    """Return every character in ordered Telegram-sized messages."""
    if not text:
        return [""]
    return [
        text[index:index + TELEGRAM_MESSAGE_LIMIT]
        for index in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ]


async def configure_telegram_webhook(
    bot_token: str,
    webhook_url: str,
    secret_token: str,
) -> None:
    """Register Footprint's authenticated administrator webhook."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret_token,
                "allowed_updates": ["message"],
            },
        )
    if response.is_error:
        logger.error("Telegram webhook setup failed: status=%s", response.status_code)
    response.raise_for_status()


async def send_telegram_text(bot_token: str, chat_id: str, text: str) -> None:
    """Send all text to Footprint's configured chat without logging secrets/content."""
    async with httpx.AsyncClient(timeout=30) as client:
        for part in split_telegram_text(text):
            response = await client.post(
                f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": part},
            )
            if response.is_error:
                logger.error(
                    "Telegram API request failed: status=%s", response.status_code
                )
            response.raise_for_status()
