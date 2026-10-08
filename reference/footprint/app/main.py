import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from contextlib import suppress
from datetime import datetime
from html import escape as html_escape
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

import websockets
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from app.line_bot import (
    classify_call_with_gemini,
    handle_admin_text_smart,
    process_schedule_image,
    push_text,
    reply_text,
    verify_line_signature,
)
from app.schedule_parser import parse_schedule
from app.telegram_bot import (
    configure_telegram_webhook, send_telegram_text,
    schedule_image_attachment, process_telegram_schedule_image,
)
from app.voice_test import build_voice_test_session_config, render_voice_test_page
from app.storage import (
    apply_temporary_staff_override,
    append_call_transcript,
    block_caller,
    check_availability,
    clone_database,
    cleanup_expired_data,
    finish_call_session,
    get_appointment_notification_target,
    get_call_transcript,
    get_therapists_in_store,
    init_database,
    is_call_spam_reviewed,
    is_caller_blocked,
    list_appointments,
    list_knowledge_entries,
    list_recent_calls,
    list_schedule,
    replace_schedule,
    reset_daily_schedule_if_due,
    save_appointment_request,
    save_call_spam_review,
    save_unanswered_question,
    save_unverified_appointment_request,
    start_call_session,
    update_appointment_status,
    use_database,
)


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-receptionist")


def normalize_us_phone_number(value: str) -> str:
    """Return a comparable 10-digit US number, or an empty string."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_REALTIME_REASONING_EFFORT = os.getenv(
    "OPENAI_REALTIME_REASONING_EFFORT", "low"
)
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "marin")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-3.6-flash")
GEMINI_LINE_MODEL = os.getenv("GEMINI_LINE_MODEL", "gemini-3.1-flash-lite")
SPAM_BLOCK_CONFIDENCE = 0.90
SPAM_REVIEW_CONFIDENCE = 0.60
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
AI_BYPASS_NUMBERS = {
    normalized
    for raw_number in re.split(r"[,;\s]+", os.getenv("AI_BYPASS_NUMBERS", ""))
    if (normalized := normalize_us_phone_number(raw_number))
}

# Loaded now for one configuration file; these will be used by Twilio REST API
# features later (outbound calls, transfers, and number management).
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ADMIN_CHAT_IDS = {
    item.strip()
    for item in os.getenv("TELEGRAM_ADMIN_CHAT_IDS", TELEGRAM_CHAT_ID).split(",")
    if item.strip()
}
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TEST_CHAT_PASSWORD = (
    os.getenv("TEST_CHAT_PASSWORD") or os.getenv("ADMIN_PASSWORD", "")
).strip()
TEST_CHAT_COOKIE_NAME = "footprint_test_chat"
TEST_CHAT_SESSION_SECONDS = 8 * 60 * 60
TEST_CHAT_SANDBOX_SECONDS = 2 * 60 * 60
TEST_CHAT_SANDBOXES: dict[str, dict] = {}


def _configured_telegram_admin_chat_ids() -> set[str]:
    return TELEGRAM_ADMIN_CHAT_IDS or ({TELEGRAM_CHAT_ID} if TELEGRAM_CHAT_ID else set())


def _configured_telegram_notification_chat_ids() -> set[str]:
    """Prefer authorized Telegram groups for proactive Footprint alerts."""
    group_ids = {
        chat_id
        for chat_id in _configured_telegram_admin_chat_ids()
        if chat_id.startswith("-")
    }
    if group_ids:
        return group_ids
    return {TELEGRAM_CHAT_ID} if TELEGRAM_CHAT_ID else set()


LINE_ADMIN_USER_ID = os.getenv("LINE_ADMIN_USER_ID", "")
LINE_ADMIN_USER_IDS = {
    user_id.strip()
    for user_id in (
        [LINE_ADMIN_USER_ID]
        + os.getenv("LINE_ADMIN_USER_IDS", "").split(",")
    )
    if user_id.strip()
}
GREETING_PLAYBACK_MARK = "footprint_greeting_complete"
INITIAL_GREETING = (
    "Hello, this is Footprint Massage. How can I help you today?"
)
AI_IDENTITY_RESPONSE = (
    "I can make an appointment for you. Our staff can call you back later if you "
    "prefer."
)
DEFAULT_CALLER_NAME = "Phone caller"

LIVE_ROBOCALL_PATTERNS = (
    r"\bpress\s+(?:1|one|2|two|3|three)\b",
    r"\bthis is (?:an? )?(?:automated|recorded|pre[- ]?recorded) (?:call|message)\b",
    r"\bautomated (?:call|message|system)\b",
    r"\bdo not hang up\b",
    r"\bstay on the line\b",
    r"\bfinal notice\b",
)
LIVE_SALES_INTENT_PATTERNS = (
    r"\b(?:i am|i'm|we are|we're) calling (?:today )?(?:to|about|regarding)\b",
    r"\bcalling (?:today )?(?:to offer|about|regarding)\b",
    r"\b(?:we|our company) (?:can|would like to|want to) (?:help|offer|provide|sell)\b",
    r"\b(?:special|promotional|limited time) offer\b",
)
LIVE_SALES_TOPIC_PATTERNS = (
    r"\b(?:marketing|advertising|seo|lead generation|google listing)\b",
    r"\b(?:website design|web design|online presence|social media management)\b",
    r"\b(?:merchant services|payment processing|credit card processing)\b",
    r"\b(?:business loan|business funding|working capital)\b",
    r"\b(?:extended warranty|debt relief|solar panels?|insurance quote)\b",
)

PRICE_SERVICE_QUESTION = (
    "Which massage price would you like—foot, full body, combination, or four hands?"
)
PRICE_DETAILS = {
    "deep_tissue_full_body": {
        "name": "Deep Tissue Full Body Massage",
        "rates": {30: 50, 45: 60, 60: 70, 90: 110, 120: 140},
    },
    "combination": {
        "name": "Combination Massage",
        "rates": {30: 50, 45: 60, 60: 70, 90: 110, 120: 140},
    },
    "four_hands": {
        "name": "Four Hands Massage",
        "rates": {30: 100, 45: 120, 60: 140, 90: 220, 120: 280},
    },
    "foot": {
        "name": "Foot Massage",
        "rates": {30: 40, 60: 50, 80: 70, 100: 100, 120: 120},
    },
}
BUSINESS_DETAILS = {
    "add_ons": (
        "Hot stones for legs $4.99; hot stones for feet $4.99; hot stones for "
        "back $9.99; walk back $9.99; strong pressure $9.99; foot bath bomb "
        "$4.99; herbs foot soak $9.99."
    ),
    "gift_cards": (
        "Foot Massage gift card: 60 minutes $50 or 80 minutes $70. Body Massage "
        "gift card: 60 minutes $70 or 90 minutes $110. Gift cards have no expiration "
        "date, can be used by anyone, and are collected after one use."
    ),
    "memberships": (
        "Body membership $700: buy 10 visits and get 1 free. Foot membership $500: "
        "buy 10 visits and get 1 free. Combo membership $600: 5 body visits and "
        "5 foot visits, then 1 free visit. Membership cards are permanent, "
        "shareable, and renewable."
    ),
}


def get_business_information(
    topic: str,
    service: str = "",
    duration_minutes: int | None = None,
) -> dict[str, object]:
    if topic == "prices":
        if not service:
            return {
                "topic": topic,
                "needs_service": True,
                "question": PRICE_SERVICE_QUESTION,
            }
        price_details = PRICE_DETAILS.get(service)
        if not price_details:
            return {
                "topic": topic,
                "needs_service": True,
                "error": "Unknown massage service.",
                "question": PRICE_SERVICE_QUESTION,
            }
        service_name = str(price_details["name"])
        rates = price_details["rates"]
        if duration_minutes is not None:
            price = rates.get(duration_minutes)
            if price is None:
                durations = ", ".join(str(value) for value in rates)
                return {
                    "topic": topic,
                    "service": service_name,
                    "needs_duration": True,
                    "available_durations_minutes": list(rates),
                    "question": (
                        f"We don't list a {duration_minutes}-minute {service_name}. "
                        f"Which duration would you like: {durations} minutes?"
                    ),
                }
            return {
                "topic": topic,
                "service": service_name,
                "duration_minutes": duration_minutes,
                "price_dollars": price,
                "information": f"{service_name}: {duration_minutes} minutes is ${price}.",
            }
        rate_text = "; ".join(
            f"{duration} minutes ${rates[duration]}"
            for duration in (60,) if duration in rates
        )
        return {
            "topic": topic,
            "service": service_name,
            "information": f"{service_name}: {rate_text}.",
        }
    information = BUSINESS_DETAILS.get(topic)
    if not information:
        return {"error": "Unknown business-information topic."}
    return {"topic": topic, "information": information}


def realtime_tool(
    name: str, description: str, properties: dict, required: list[str]
) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


APPOINTMENT_TOOL_PROPERTIES = {
    "requested_date": {"type": "string"},
    "requested_time": {"type": "string"},
    "service": {"type": "string"},
    "duration_minutes": {"type": "integer"},
    "party_size": {
        "type": "integer",
        "enum": [1, 2],
        "description": "One person, or two people receiving massages at the same time.",
    },
    "therapist_preference": {"type": "string"},
    "notes": {"type": "string"},
}
APPOINTMENT_TOOL_REQUIRED = [
    "requested_date", "requested_time", "service", "duration_minutes"
]
REALTIME_TOOLS = [
    realtime_tool(
        "check_walk_in_availability",
        "For walk-in / walk in / can I come now / 現在可以直接來嗎, check NOW using "
        "the server's live Los Angeles clock. Overrides booking: do not ask what "
        "time. Pass known duration, party size and requested therapist only. Unknown "
        "duration checks a short session, not a reservation. For an explicitly "
        "later visit use check_availability instead.",
        {
            "duration_minutes": {"type": "integer"},
            "party_size": {"type": "integer", "enum": [1, 2]},
            "therapist_preference": {"type": "string"},
        },
        [],
    ),
    realtime_tool(
        "get_business_information",
        "Retrieve one requested store detail. For prices, do not call for a broad "
        "rates question until the caller names a massage service; then include that "
        "service and the duration if known. Never retrieve every service's prices.",
        {
            "topic": {
                "type": "string",
                "enum": ["prices", "add_ons", "gift_cards", "memberships"],
            },
            "service": {
                "type": "string",
                "enum": [
                    "deep_tissue_full_body", "foot", "combination", "four_hands"
                ],
                "description": "Required for prices after the caller names a service.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "Include only when the caller names a duration.",
            },
        },
        ["topic"],
    ),
    realtime_tool(
        "check_availability",
        "Check the uploaded therapist schedule before discussing a specific "
        "appointment time. Dates must be YYYY-MM-DD and times HH:MM in "
        "America/Los_Angeles local time. Omit duration for a preliminary start-time "
        "check; recheck with the caller's actual duration before booking.",
        {
            "requested_date": {"type": "string"},
            "requested_time": {"type": "string"},
            "duration_minutes": {"type": "integer"},
            "party_size": {
                "type": "integer",
                "enum": [1, 2],
                "description": "Number of people receiving massages at the same time.",
            },
            "therapist_preference": {"type": "string"},
        },
        ["requested_date", "requested_time"],
    ),
    realtime_tool(
        "get_therapists_in_store",
        "Use the confirmed schedule to identify which therapists are in the store "
        "on a specific date. Call this before answering who is working or whether "
        "a requested therapist is present.",
        {
            "requested_date": {
                "type": "string", "description": "Date in YYYY-MM-DD format."
            },
            "requested_time": {
                "type": "string",
                "description": "Time in HH:MM. Include it when the caller asks who "
                "is present now or at a particular time.",
            },
        },
        ["requested_date"],
    ),
    realtime_tool(
        "save_appointment_request",
        "Book a CONFIRMED appointment only after availability was checked, all "
        "details were read back, and the caller clearly confirmed them.",
        APPOINTMENT_TOOL_PROPERTIES,
        APPOINTMENT_TOOL_REQUIRED,
    ),
    realtime_tool(
        "save_unverified_appointment_request",
        "Save a NEEDS_REVIEW callback request only when check_availability returned "
        "schedule_loaded=false. Use the caller's preferred time; do not claim it is "
        "available or confirmed. Read back all details and get the caller's "
        "confirmation first.",
        APPOINTMENT_TOOL_PROPERTIES,
        APPOINTMENT_TOOL_REQUIRED,
    ),
    realtime_tool(
        "save_unanswered_question",
        "Save a caller's store-specific question only when the approved business "
        "information and staff-provided answers do not contain a reliable answer. "
        "Never guess.",
        {"question": {"type": "string"}, "context": {"type": "string"}},
        ["question"],
    ),
    realtime_tool(
        "request_staff_callback",
        "Use immediately when the caller asks to speak with a person, a human, or "
        "a staff member. This records the caller's phone number for staff callback.",
        {},
        [],
    ),
]


RECEPTIONIST_PROMPT = (
    "You are Karin, the AI receptionist for Footprint Massage. Never claim or imply "
    "that you are human or a real person. Do not volunteer that you are AI in the "
    "opening or ordinary conversation. If asked whether you are AI, human, a real "
    "person, a robot, or a machine, say exactly in English: '"
    + AI_IDENTITY_RESPONSE
    + "' Do not add an explanation. If the caller asks for a person, human, or staff "
    "member, immediately use request_staff_callback. After it is saved, say staff will "
    "call back; continue appointment questions only if the caller wants to. "
    "STYLE: Be an efficient, concise receptionist. Answer directly, then stop. "
    "Use one sentence, usually 3-12 words; add only the next missing question. "
    "One question at a time; never re-ask known details. Prices, addresses and the "
    "required booking readback may be longer for accuracy. No small talk, praise, "
    "filler except the waiting line below. "
    "VOICE: Polite, not rushed; natural rhythm and a normal conversational pace. "
    "Use simple CEFR B1-B2 English or the caller's language; say 'open time', not "
    "'availability'. Never speak internal tool fields. "
    "Wait silently through noise, unclear speech, hesitation or requests to wait. "
    "WALK-IN NOW: Use check_walk_in_availability. Otherwise, BOOKING: "
    "When a caller wants an appointment but has not given a "
    "time, say ONLY 'Sure, what time?' Then stop and listen. A date alone (e.g. "
    "September 21) is not a time. Never invent a time, check availability, or say "
    "a time works until the caller gives one. Default the "
    "appointment date to today in America/Los_Angeles unless another day is given; never ask 'Today?' "
    "or say 'If you mean today'. If the "
    "caller later specifies another day, update the date and recheck that date before "
    "booking. Infer unstated AM/PM from business hours: 1-9 means PM, 10-11 means "
    "AM, and 12 means noon. At 1 PM, '2' means 2 PM. "
    "Respect explicit AM/PM. Never ask 'AM or PM?' when only one interpretation "
    "fits opening hours. Use check_availability before rejecting a time as past; "
    "its live Los Angeles clock is authoritative. Do not silently change the date. "
    "Once a time is supplied, check it, omitting unknown duration (preliminary only). "
    "After a time works, ask 'What kind of massage would you like?' only if unknown, "
    "then 'How long?' only if unknown. Remember 'body massage'; never ask it again. "
    "Recheck the actual duration before final readback; never invent a duration. "
    "For couples/two people, clarify simultaneous massages only if unclear. "
    "Pass party_size=2 to check_availability "
    "and the save tool; two different therapists must be simultaneously available. "
    "Never say you can only book one appointment at a time. If one person was already "
    "checked, recheck with party_size=2 before confirming. Automated two-person "
    "bookings require the same service and duration; otherwise arrange staff follow-up. "
    "Include 'for two people' in the final readback. Before a potentially slow lookup, "
    "say 'OK, give me one second.' once, then call immediately. Skip for instant "
    "answers; never repeat during that lookup or after its result. "
    "Never proactively name, recommend, list, or ask the caller to choose therapists. "
    "Mention staff only when asked; otherwise pass an empty therapist_preference. "
    "MALE THERAPISTS: We have none. If asked for a male therapist (男按摩師), say "
    "only 'Sorry, we don't have male massage therapists.' No lookup, callback, or "
    "alternatives for this question. This does not restrict male customers. "
    "PRICES: For broad rates/prices without a service, ask ONLY '"
    + PRICE_SERVICE_QUESTION
    + "' Stop; do not call a tool, quote a price/range, or say you're checking "
    "prices. Once named, call "
    "get_business_information. Answer only that service; if duration is known, give "
    "one price. Otherwise quote only the 60-minute rate; other lengths "
    "only on request. Never invent a 90-minute rate. Never list other services. "
    "Do not repeat accepted details or list services/times unless asked. "
    "End with 'Thank you, bye.' "
    "\n\n"
    "BUSINESS:\n"
    "Address: 123 Example Street, Example City.\n"
    "Business phone: 202-555-0123.\n"
    "Email: owner@example.com.\n"
    "Hours: Open daily from 10:00 AM to 10:00 PM.\n"
    "Staff defaults: Staff B and Staff C 10 AM-10 PM; Staff A 7 PM-10 PM. "
    "Daily staff edits override defaults. Former Staff has left; never book her. "
    "A new blank daily sheet opens those shifts; written service times are occupied.\n"
    "Online booking is available through the Footprint Massage website.\n"
    "\n"
    "OPERATING RULES:\n"
    "Never make medical claims or promise results. The latest appointment start is "
    "9:30 PM; never offer or accept a later start. Do not invent policies, therapist "
    "availability, or appointment confirmation. Use get_business_information before "
    "answering a specific price, add-ons, gift cards, or memberships. Always use "
    "check_availability for a time. When asked who is working, who is present, or for "
    "a specific therapist, use get_therapists_in_store for that date/time; answer "
    "only who was asked about. If staffing_overrides says 请假, say that therapist is off. "
    "Without a loaded schedule, do not guess staffing; "
    "arrange follow-up. If unavailable, immediately offer the first suggested_times "
    "entry: 'Sorry, that time doesn't work. How about [time]?' No names or extra options. "
    "If none, say 'Sorry, no later times are open that day.' Never invent times. "
    "Without a loaded schedule, do not suggest another time. "
    "Say 'I can't confirm that time. Our staff can call you back.' Then collect and "
    "read back the preferred details; after confirmation use "
    "save_unverified_appointment_request. "
    "Never ask for the caller's name. Use the incoming phone number to identify the "
    "appointment. Before saving, read back once only the date, time, service, duration, "
    "and a therapist only if requested. Ask 'Is that right?' Replies including yes, correct, book "
    "it, 可以, or 确认 authorize immediate use of the appropriate save tool. "
    "Never say an appointment is booked unless save_appointment_request returns "
    "saved=true, status=confirmed, and a request_id. Then say it is booked in one short "
    "sentence, without a confirmation number or staff-confirmation language. If needed, "
    "offer staff follow-up. For an unanswered store-specific question, use "
    "save_unanswered_question with the exact question and brief context, then say staff "
    "will call back with the answer; never use it for ordinary small talk. "
    "A request for somebody/human overrides booking questions: call "
    "request_staff_callback immediately, not 'if you like', and do not hang up first."
)

app = FastAPI(title="AI Phone Receptionist")
init_database()
daily_schedule_task: asyncio.Task | None = None


async def maintain_daily_default_schedule() -> None:
    while True:
        try:
            result = await asyncio.to_thread(reset_daily_schedule_if_due)
            await asyncio.to_thread(
                apply_temporary_staff_override,
                datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat(),
            )
            if result.get("reset"):
                logger.info(
                    "Created daily default schedule for %s with %s rows",
                    result.get("date"),
                    result.get("rows"),
                )
            cleanup = await asyncio.to_thread(cleanup_expired_data)
            if cleanup.get("cleaned"):
                logger.info(
                    "Retention cleanup removed appointments=%s availability=%s "
                    "staff_shifts=%s staged_uploads=%s",
                    cleanup.get("appointments"),
                    cleanup.get("availability"),
                    cleanup.get("staff_shifts"),
                    cleanup.get("staged_uploads"),
                )
        except Exception:
            logger.exception("Daily default schedule maintenance failed")
        await asyncio.sleep(60)


@app.on_event("startup")
async def start_daily_schedule_maintenance() -> None:
    global daily_schedule_task
    if (
        TELEGRAM_BOT_TOKEN
        and _configured_telegram_admin_chat_ids()
        and TELEGRAM_WEBHOOK_SECRET
        and PUBLIC_BASE_URL
    ):
        try:
            await configure_telegram_webhook(
                TELEGRAM_BOT_TOKEN,
                f"{PUBLIC_BASE_URL}/telegram/webhook",
                TELEGRAM_WEBHOOK_SECRET,
            )
            logger.info("Footprint Telegram administrator webhook configured")
        except Exception:
            logger.exception("Unable to configure Footprint Telegram webhook")
    daily_schedule_task = asyncio.create_task(maintain_daily_default_schedule())


@app.on_event("shutdown")
async def stop_daily_schedule_maintenance() -> None:
    if daily_schedule_task:
        daily_schedule_task.cancel()
        with suppress(asyncio.CancelledError):
            await daily_schedule_task
    for session_id in list(TEST_CHAT_SANDBOXES):
        close_test_chat_sandbox(session_id)


def require_runtime_config() -> None:
    missing = []
    for name, value in (
        ("OPENAI_API_KEY", OPENAI_API_KEY),
        ("PUBLIC_BASE_URL", PUBLIC_BASE_URL),
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER),
    ):
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


@app.get("/")
async def health() -> dict:
    return {
        "status": "ok",
        "telegram": {
            "configured": bool(
                TELEGRAM_BOT_TOKEN
                and _configured_telegram_admin_chat_ids()
                and TELEGRAM_WEBHOOK_SECRET
            ),
            "smart_admin": bool(
                TELEGRAM_BOT_TOKEN
                and TELEGRAM_ADMIN_CHAT_IDS
                and TELEGRAM_WEBHOOK_SECRET
                and GEMINI_API_KEY
            ),
        },
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Accept commands only from Footprint's authenticated administrator chat."""
    if not (
        TELEGRAM_BOT_TOKEN
        and _configured_telegram_admin_chat_ids()
        and TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=503, detail="Telegram bot is not configured.")
    supplied_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secrets.compare_digest(supplied_secret, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret.")

    payload = await request.json()
    message = payload.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    authorized_chat_ids = _configured_telegram_admin_chat_ids()
    if chat_id not in authorized_chat_ids:
        logger.warning("Ignored Telegram command from an unauthorized chat")
        return Response(status_code=200)

    attachment = schedule_image_attachment(message)
    if attachment:
        tasks = BackgroundTasks()
        tasks.add_task(
            process_telegram_schedule_image, TELEGRAM_BOT_TOKEN, chat_id, attachment,
            gemini_api_key=GEMINI_API_KEY, gemini_model=GEMINI_LINE_MODEL,
            openai_api_key=OPENAI_API_KEY, vision_model=OPENAI_VISION_MODEL,
        )
        await send_telegram_text(TELEGRAM_BOT_TOKEN, chat_id, "收到，正在读表；确认后才会更新班表。")
        return Response(status_code=200, background=tasks)

    text = str(message.get("text", "")).strip()
    if not text:
        await send_telegram_text(
            TELEGRAM_BOT_TOKEN, chat_id, "请传送文字或班表照片（JPG/PNG）。"
        )
        return Response(status_code=200)

    if any(phrase in text for phrase in (
        "設定預約通知群", "设定预约通知群", "设置预约通知群",
        "取消預約通知群", "取消预约通知群",
        "查看預約通知群", "查看预约通知群",
    )):
        reply = "LINE 通知群设置请在 LINE 里操作；Telegram 通知只发送到指定 Chat ID。"
    else:
        reply = await handle_admin_text_smart(
            f"telegram:{chat_id}",
            text,
            conversation_id=f"telegram:{chat_id}",
            gemini_api_key=GEMINI_API_KEY,
            gemini_model=GEMINI_LINE_MODEL,
            allow_notification_group_commands=False,
        )
    await send_telegram_text(TELEGRAM_BOT_TOKEN, chat_id, reply)
    return Response(status_code=200)


@app.post("/line/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN or not LINE_ADMIN_USER_IDS:
        logger.error("LINE configuration is incomplete")
        raise HTTPException(status_code=503, detail="LINE bot is not configured.")

    body = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not verify_line_signature(body, signature, LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=400, detail="Invalid LINE signature.")

    payload = json.loads(body or b"{}")
    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        source = event.get("source", {})
        user_id = source.get("userId", "")
        conversation_id = (
            source.get("groupId") or source.get("roomId") or user_id
        )
        reply_token = event.get("replyToken", "")
        message = event.get("message", {})
        message_type = message.get("type")
        message_text = message.get("text", "").strip() if message_type == "text" else ""

        if message_text.casefold() in {"我的id", "my id"}:
            if reply_token:
                await reply_text(
                    reply_token,
                    f"你的 LINE User ID 是：\n{user_id}",
                    LINE_CHANNEL_ACCESS_TOKEN,
                )
            continue

        if user_id not in LINE_ADMIN_USER_IDS:
            if reply_token:
                await reply_text(
                    reply_token,
                    "这个机器人仅供店内管理员使用。若要申请权限，请输入：我的ID",
                    LINE_CHANNEL_ACCESS_TOKEN,
                )
            continue

        if message_type == "text" and reply_token:
            await reply_text(
                reply_token,
                await handle_admin_text_smart(
                    user_id,
                    message.get("text", ""),
                    conversation_id=conversation_id,
                    gemini_api_key=GEMINI_API_KEY,
                    gemini_model=GEMINI_LINE_MODEL,
                ),
                LINE_CHANNEL_ACCESS_TOKEN,
            )
        elif message_type == "image" and reply_token:
            await reply_text(
                reply_token,
                "收到表格照片，正在判断是可预约班表还是已完成服务纪录。完成后我会传回预览，不会直接覆盖正式班表。",
                LINE_CHANNEL_ACCESS_TOKEN,
            )
            background_tasks.add_task(
                process_schedule_image,
                conversation_id=conversation_id,
                message_id=message.get("id", ""),
                line_access_token=LINE_CHANNEL_ACCESS_TOKEN,
                openai_api_key=OPENAI_API_KEY,
                vision_model=OPENAI_VISION_MODEL,
                gemini_api_key=GEMINI_API_KEY,
                gemini_model=GEMINI_VISION_MODEL,
            )
        elif reply_token:
            await reply_text(reply_token, "请传送文字或班表照片。", LINE_CHANNEL_ACCESS_TOKEN)

    return Response(status_code=200)


def is_local_admin_connection(headers, client_host: str) -> bool:
    """Return true only for a direct loopback connection without a proxy."""
    return (
        not headers.get("x-forwarded-for")
        and client_host in {"127.0.0.1", "::1"}
    )


def _test_chat_session_token(now: int | None = None) -> str:
    if not TEST_CHAT_PASSWORD:
        return ""
    issued_at = int(time.time()) if now is None else now
    signature = hmac.new(
        TEST_CHAT_PASSWORD.encode("utf-8"),
        f"footprint-test-chat-session-v1:{issued_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{signature}"


def _valid_test_chat_session_token(token: str) -> bool:
    try:
        issued_at_text, supplied_signature = token.split(".", 1)
        issued_at = int(issued_at_text)
    except (TypeError, ValueError):
        return False
    age = int(time.time()) - issued_at
    if age < 0 or age > TEST_CHAT_SESSION_SECONDS:
        return False
    expected = _test_chat_session_token(issued_at).split(".", 1)[1]
    return secrets.compare_digest(supplied_signature, expected)


def is_test_chat_authorized(headers, client_host: str) -> bool:
    if is_local_admin_connection(headers, client_host):
        return True
    if not TEST_CHAT_PASSWORD:
        return False
    cookies = SimpleCookie()
    with suppress(Exception):
        cookies.load(headers.get("cookie", ""))
    supplied = cookies.get(TEST_CHAT_COOKIE_NAME)
    return bool(supplied and _valid_test_chat_session_token(supplied.value))


def require_local_admin(request: Request) -> None:
    """Keep the prototype admin page off public tunnels and reverse proxies."""
    client_host = request.client.host if request.client else ""
    if not is_local_admin_connection(request.headers, client_host):
        raise HTTPException(status_code=403, detail="Admin page is local-only.")


def require_test_chat_request(request: Request, *, same_origin: bool = False) -> None:
    client_host = request.client.host if request.client else ""
    if not is_test_chat_authorized(request.headers, client_host):
        raise HTTPException(status_code=401, detail="Test chat login required.")
    if same_origin:
        source = request.headers.get("origin") or request.headers.get("referer")
        if (
            not source
            or urlsplit(source).netloc.casefold()
            != request.headers.get("host", "").casefold()
        ):
            raise HTTPException(status_code=403, detail="Invalid request origin.")


def cleanup_test_chat_sandboxes() -> None:
    cutoff = time.monotonic() - TEST_CHAT_SANDBOX_SECONDS
    expired = [
        session_id
        for session_id, session in TEST_CHAT_SANDBOXES.items()
        if session["created_at"] < cutoff
    ]
    for session_id in expired:
        close_test_chat_sandbox(session_id)


def create_test_chat_sandbox() -> str:
    cleanup_test_chat_sandboxes()
    temporary_directory = tempfile.TemporaryDirectory(prefix="footprint-voice-test-")
    database_path = Path(temporary_directory.name) / "receptionist-test.db"
    try:
        clone_database(database_path)
    except Exception:
        temporary_directory.cleanup()
        raise
    session_id = secrets.token_urlsafe(32)
    TEST_CHAT_SANDBOXES[session_id] = {
        "temporary_directory": temporary_directory,
        "database_path": database_path,
        "created_at": time.monotonic(),
    }
    return session_id


def get_test_chat_sandbox(session_id: str) -> Path:
    cleanup_test_chat_sandboxes()
    session = TEST_CHAT_SANDBOXES.get(session_id)
    if not session:
        raise ValueError("Voice test session expired. Please start again.")
    return session["database_path"]


def close_test_chat_sandbox(session_id: str) -> None:
    session = TEST_CHAT_SANDBOXES.pop(session_id, None)
    if session:
        session["temporary_directory"].cleanup()


def render_schedule_rows(empty_message: str) -> str:
    return "".join(
        "<tr>" + "".join(
            f"<td>{html_escape(str(row[column]))}</td>"
            for column in ("date", "therapist", "start_time", "end_time", "status")
        ) + "</tr>"
        for row in list_schedule()
    ) or f'<tr><td colspan="5">{html_escape(empty_message)}</td></tr>'


def render_admin(message: str = "") -> str:
    schedule_rows = render_schedule_rows("No schedule uploaded yet.")

    appointment_rows = "".join(
        "<tr>"
        f"<td>{row['id']}</td>"
        f"<td>{html_escape(row['customer_name'])}</td>"
        f"<td>{html_escape(row['customer_phone'] or '')}</td>"
        f"<td>{html_escape(row['requested_date'])}</td>"
        f"<td>{html_escape(row['requested_time'])}</td>"
        f"<td>{html_escape(row['service'])} ({row['duration_minutes']} min)</td>"
        f"<td>{html_escape(row['therapist'] or '')}</td>"
        f"<td>{html_escape(row['status'])}</td>"
        "<td>"
        f'<form method="post" action="/admin/appointments/{row["id"]}/status">'
        '<button name="status" value="confirmed">Confirm</button> '
        '<button name="status" value="rejected">Reject</button>'
        "</form></td></tr>"
        for row in list_appointments()
    ) or '<tr><td colspan="9">No appointment requests yet.</td></tr>'

    call_rows = "".join(
        "<details><summary>"
        f"{html_escape(row['started_at'][:16].replace('T', ' '))} | "
        f"caller ***{html_escape((row['caller_phone'] or '')[-4:] or 'unknown')} | "
        f"{row['transcript_count']} lines"
        "</summary><div class=\"transcript\">"
        + "".join(
            f"<p><b>{'Customer' if line['speaker'] == 'customer' else 'Karin'}:</b> "
            f"{html_escape(line['text'])}</p>"
            for line in get_call_transcript(row["stream_sid"])
        )
        + "</div></details>"
        for row in list_recent_calls()
    ) or "<p>No call transcripts yet.</p>"

    notice = f'<p class="notice">{html_escape(message)}</p>' if message else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Footprint Massage Admin</title>
<style>
body{{font-family:Arial,sans-serif;margin:32px;max-width:1200px}}
table{{border-collapse:collapse;width:100%;margin-bottom:32px}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left}}
th{{background:#f2f2f2}} .notice{{background:#e8f5e9;padding:12px}}
input,button{{padding:8px;margin:4px}} code{{background:#f4f4f4;padding:2px 5px}}
details{{border:1px solid #ccc;padding:10px;margin:8px 0}} .transcript{{padding-left:16px}}
</style></head><body>
<h1>Footprint Massage</h1>{notice}
<p><a href="/admin/test-chat">Open local voice and text booking test</a></p>
<h2>Upload therapist schedule</h2>
<p>Accepted: <code>.xlsx</code> or <code>.csv</code>. Required columns:
<code>date</code>, <code>therapist</code>, <code>start_time</code>,
<code>end_time</code>. Optional: <code>status</code>.</p>
<form method="post" action="/admin/schedule" enctype="multipart/form-data">
<input type="file" name="file" accept=".xlsx,.csv" required>
<button type="submit">Replace Schedule</button></form>
<h2>Current schedule</h2>
<table><tr><th>Date</th><th>Therapist</th><th>Start</th><th>End</th><th>Status</th></tr>
{schedule_rows}</table>
<h2>Appointment requests</h2>
<table><tr><th>ID</th><th>Name</th><th>Phone</th><th>Date</th><th>Time</th>
<th>Service</th><th>Therapist</th><th>Status</th><th>Action</th></tr>
{appointment_rows}</table>
<h2>Recent call transcripts (48-hour retention)</h2>
{call_rows}
</body></html>"""


def render_test_chat() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Footprint Text Booking Test</title>
<style>
*{box-sizing:border-box} body{font-family:Arial,sans-serif;margin:0;background:#f5f2ed;color:#29241f}
main{max-width:820px;margin:0 auto;padding:24px} .top{display:flex;align-items:center;justify-content:space-between;gap:16px}
a{color:#6d4c35} .notice{background:#fff4d6;border:1px solid #e6c66b;padding:12px;border-radius:10px;line-height:1.45}
.settings{display:flex;gap:10px;margin:16px 0}.settings input{flex:1;min-width:0}
#chat{height:52vh;min-height:360px;overflow-y:auto;background:white;border:1px solid #d8d0c7;border-radius:14px;padding:16px}
.message{max-width:82%;margin:10px 0;padding:10px 13px;border-radius:14px;white-space:pre-wrap;line-height:1.4}
.assistant{background:#eee8e1}.customer{background:#6d4c35;color:white;margin-left:auto}.system{max-width:100%;background:#eef3f7;color:#425466;font-size:13px}
.composer{display:flex;gap:10px;margin-top:12px}.composer input{flex:1}.composer input,.settings input,button{padding:12px;border:1px solid #b9afa5;border-radius:9px;font:inherit}
button{background:#6d4c35;color:white;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}#status{font-size:13px;color:#665e57}
</style></head><body><main>
<div class="top"><div><h1>Text booking test</h1><div id="status">Connecting…</div></div></div>
<p class="notice"><b>Isolated test.</b> This uses Footprint's current Realtime model, receptionist prompt, availability checks, and booking tools against a temporary copy of the current database. Test bookings disappear when the conversation ends and staff notifications are suppressed. Audio, speech recognition, interruption, and voice still require a real call.</p>
<div class="settings"><input id="phone" value="+12025550123" aria-label="Test caller phone"><button id="reset" type="button">New conversation</button></div>
<div id="chat" aria-live="polite"></div>
<form id="composer" class="composer"><input id="message" autocomplete="off" placeholder="Type as the customer…" disabled><button id="send" disabled>Send</button></form>
<script>
const chat=document.querySelector('#chat'), message=document.querySelector('#message'), send=document.querySelector('#send'), statusEl=document.querySelector('#status'), phone=document.querySelector('#phone');
let ws, assistantBubble=null;
function addMessage(kind,text){const el=document.createElement('div');el.className='message '+kind;el.textContent=text;chat.appendChild(el);chat.scrollTop=chat.scrollHeight;return el}
function setReady(ready,label){message.disabled=!ready;send.disabled=!ready;statusEl.textContent=label;if(ready)message.focus()}
function connect(){if(ws)ws.close();chat.replaceChildren();assistantBubble=null;setReady(false,'Connecting…');const scheme=location.protocol==='https:'?'wss':'ws';const endpoint=location.pathname.startsWith('/admin/')?'/admin/test-chat/ws':'/test-chat/ws';ws=new WebSocket(`${scheme}://${location.host}${endpoint}`);
ws.onopen=()=>{ws.send(JSON.stringify({type:'start',caller_number:phone.value}));setReady(false,'Karin is greeting…')};
ws.onmessage=(raw)=>{const event=JSON.parse(raw.data);if(event.type==='ready'){return}if(event.type==='assistant_delta'){if(!assistantBubble)assistantBubble=addMessage('assistant','');assistantBubble.textContent+=event.delta;chat.scrollTop=chat.scrollHeight}
else if(event.type==='assistant_done'){if(!assistantBubble)assistantBubble=addMessage('assistant',event.text||'');else if(event.text)assistantBubble.textContent=event.text;assistantBubble=null;setReady(true,'Ready — isolated temporary data')}
else if(event.type==='tool'){addMessage('system',`Tool: ${event.name}`);setReady(false,'Checking Footprint data…')}
else if(event.type==='error'){addMessage('system',event.message||'Connection error');setReady(false,'Error')}};
ws.onclose=()=>{if(statusEl.textContent!=='Error')setReady(false,'Disconnected')};ws.onerror=()=>setReady(false,'Error')}
document.querySelector('#composer').addEventListener('submit',(event)=>{event.preventDefault();const text=message.value.trim();if(!text||!ws||ws.readyState!==WebSocket.OPEN)return;addMessage('customer',text);ws.send(JSON.stringify({type:'message',text}));message.value='';assistantBubble=null;setReady(false,'Karin is replying…')});
document.querySelector('#reset').addEventListener('click',connect);connect();
</script></main></body></html>"""


def render_test_chat_login(message: str = "") -> str:
    notice = f'<p class="error">{html_escape(message)}</p>' if message else ""
    configured = bool(TEST_CHAT_PASSWORD)
    disabled = "" if configured else " disabled"
    setup = "" if configured else (
        '<p class="error">TEST_CHAT_PASSWORD is not configured on this service.</p>'
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Footprint Voice and Text Booking Test</title><style>
body{{font-family:Arial,sans-serif;background:#f5f2ed;color:#29241f;margin:0}}
main{{max-width:430px;margin:12vh auto;background:white;padding:28px;border-radius:14px}}
input,button{{box-sizing:border-box;width:100%;padding:12px;margin-top:10px;font:inherit;border-radius:8px;border:1px solid #b9afa5}}
button{{background:#6d4c35;color:white}}.error{{color:#9c2f20}}</style></head><body><main>
<h1>Footprint test chat</h1><p>Enter the Footprint admin/test password.</p>
{setup}{notice}<form method="post" action="/test-chat/login">
<input type="password" name="password" autocomplete="current-password" required{disabled}>
<button type="submit"{disabled}>Open test chat</button></form>
</main></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, message: str = "") -> HTMLResponse:
    require_local_admin(request)
    return HTMLResponse(render_admin(message))


@app.get("/admin/test-chat", response_class=HTMLResponse)
async def test_chat_page(request: Request) -> HTMLResponse:
    require_local_admin(request)
    return HTMLResponse(
        render_voice_test_page(OPENAI_REALTIME_MODEL, INITIAL_GREETING),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/test-chat", response_class=HTMLResponse)
async def public_test_chat_page(request: Request) -> HTMLResponse:
    client_host = request.client.host if request.client else ""
    if is_test_chat_authorized(request.headers, client_host):
        return HTMLResponse(
            render_voice_test_page(OPENAI_REALTIME_MODEL, INITIAL_GREETING),
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(render_test_chat_login())


@app.post("/test-chat/login", response_class=HTMLResponse)
async def public_test_chat_login(
    request: Request,
    password: str = Form(...),
) -> Response:
    if not TEST_CHAT_PASSWORD or not secrets.compare_digest(password, TEST_CHAT_PASSWORD):
        await asyncio.sleep(1)
        return HTMLResponse(render_test_chat_login("Incorrect password."), status_code=401)
    response = RedirectResponse("/test-chat", status_code=303)
    response.set_cookie(
        TEST_CHAT_COOKIE_NAME,
        _test_chat_session_token(),
        max_age=TEST_CHAT_SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response


@app.post("/test-chat/session")
async def create_voice_test_session(request: Request) -> Response:
    """Exchange a browser WebRTC offer without exposing the OpenAI API key."""
    require_test_chat_request(request, same_origin=True)
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI is not configured.")
    if not request.headers.get("content-type", "").casefold().startswith(
        "application/sdp"
    ):
        raise HTTPException(status_code=415, detail="Expected an SDP offer.")

    offer_bytes = await request.body()
    if not offer_bytes or len(offer_bytes) > 200_000:
        raise HTTPException(status_code=400, detail="Invalid SDP offer.")
    try:
        offer_sdp = offer_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid SDP offer.") from exc

    session_config = build_voice_test_session_config(
        model=OPENAI_REALTIME_MODEL,
        instructions=await build_realtime_instructions(),
        voice=OPENAI_VOICE,
        speed=1.0,
        reasoning_effort=OPENAI_REALTIME_REASONING_EFFORT,
        tools=REALTIME_TOOLS,
    )
    safety_identifier = hashlib.sha256(b"footprint-browser-voice-test").hexdigest()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            upstream = await client.post(
                "https://api.openai.com/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "OpenAI-Safety-Identifier": safety_identifier,
                },
                files={
                    "sdp": (None, offer_sdp, "application/sdp"),
                    "session": (
                        None,
                        json.dumps(session_config),
                        "application/json",
                    ),
                },
            )
    except httpx.HTTPError as exc:
        logger.exception("Unable to create Footprint browser Realtime session")
        raise HTTPException(status_code=502, detail="Unable to start voice test.") from exc
    if not upstream.is_success:
        logger.error(
            "OpenAI Footprint browser Realtime session rejected: status=%s",
            upstream.status_code,
        )
        raise HTTPException(status_code=502, detail="Unable to start voice test.")

    session_id = create_test_chat_sandbox()
    return Response(
        upstream.text,
        media_type="application/sdp",
        headers={
            "Cache-Control": "no-store",
            "X-Footprint-Test-Session": session_id,
        },
    )


class VoiceTestToolCollector:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, payload: str) -> None:
        self.messages.append(payload)


async def run_voice_test_tool(
    session_id: str,
    name: str,
    arguments: dict,
) -> dict:
    """Execute the production tool flow within one isolated browser session."""
    database_path = get_test_chat_sandbox(session_id)
    allowed_tools = {tool["name"] for tool in REALTIME_TOOLS}
    if name not in allowed_tools:
        raise ValueError(f"Unknown voice-test tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    collector = VoiceTestToolCollector()
    event = {
        "name": name,
        "call_id": "browser-voice-test",
        "arguments": json.dumps(arguments),
    }
    with use_database(database_path):
        await handle_tool_call(
            event,
            collector,
            "VOICE-TEST",
            notify_staff=False,
        )
    function_output = json.loads(collector.messages[0])
    response_create = json.loads(collector.messages[1])
    return {
        "result": json.loads(function_output["item"]["output"]),
        "response_instructions": response_create["response"]["instructions"],
    }


@app.post("/test-chat/tool")
async def voice_test_tool(request: Request) -> dict:
    """Run one allow-listed receptionist tool against the session sandbox."""
    require_test_chat_request(request, same_origin=True)
    session_id = request.headers.get("x-footprint-test-session", "")
    try:
        payload = await request.json()
        name = str(payload.get("name") or "")
        raw_arguments = payload.get("arguments", "{}")
        arguments = (
            json.loads(raw_arguments)
            if isinstance(raw_arguments, str)
            else raw_arguments
        )
        result = await run_voice_test_tool(session_id, name, arguments)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post("/test-chat/end", status_code=204)
async def end_voice_test_session(request: Request) -> Response:
    require_test_chat_request(request, same_origin=True)
    close_test_chat_sandbox(
        request.headers.get("x-footprint-test-session", "")
    )
    return Response(status_code=204)


@app.post("/admin/schedule")
async def upload_schedule(request: Request, file: UploadFile = File(...)) -> RedirectResponse:
    require_local_admin(request)
    try:
        rows = parse_schedule(file.filename or "", await file.read())
        count = replace_schedule(rows)
        message = f"Uploaded {count} schedule rows."
    except ValueError as exc:
        message = f"Upload failed: {exc}"
    return RedirectResponse(f"/admin?{urlencode({'message': message})}", status_code=303)


@app.post("/admin/appointments/{appointment_id}/status")
async def appointment_status(
    request: Request,
    appointment_id: int,
    status: str = Form(...),
) -> RedirectResponse:
    require_local_admin(request)
    update_appointment_status(appointment_id, status)
    return RedirectResponse("/admin?message=Appointment+updated.", status_code=303)


@app.websocket("/test-chat/ws")
@app.websocket("/admin/test-chat/ws")
async def test_chat_socket(browser_ws: WebSocket) -> None:
    """Run the real receptionist prompt and tools in an isolated text session."""
    client_host = browser_ws.client.host if browser_ws.client else ""
    is_admin_path = browser_ws.url.path.startswith("/admin/")
    authorized = (
        is_local_admin_connection(browser_ws.headers, client_host)
        if is_admin_path
        else is_test_chat_authorized(browser_ws.headers, client_host)
    )
    if not authorized:
        await browser_ws.close(code=1008)
        return

    await browser_ws.accept()
    if not OPENAI_API_KEY:
        await browser_ws.send_json(
            {"type": "error", "message": "OPENAI_API_KEY is missing locally."}
        )
        await browser_ws.close(code=1011)
        return

    query = urlencode({"model": OPENAI_REALTIME_MODEL})
    openai_url = f"wss://api.openai.com/v1/realtime?{query}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    caller_number = "+12025550123"

    try:
        with tempfile.TemporaryDirectory(prefix="footprint-text-test-") as sandbox_dir:
            sandbox_path = Path(sandbox_dir) / "receptionist-test.db"
            clone_database(sandbox_path)
            with use_database(sandbox_path):
                async with websockets.connect(
                    openai_url,
                    additional_headers=headers,
                    max_size=None,
                ) as openai_ws:
                    await configure_openai_session(openai_ws, text_only=True)

                    async def browser_to_openai() -> None:
                        nonlocal caller_number
                        while True:
                            incoming = json.loads(await browser_ws.receive_text())
                            event_type = incoming.get("type")
                            if event_type == "start":
                                supplied_number = str(
                                    incoming.get("caller_number", "")
                                ).strip()
                                caller_number = supplied_number or caller_number
                                await browser_ws.send_json({"type": "ready"})
                                await ask_openai_to_greet(openai_ws)
                            elif event_type == "message":
                                text = str(incoming.get("text", "")).strip()
                                if not text:
                                    continue
                                await openai_ws.send(
                                    json.dumps(
                                        {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "message",
                                                "role": "user",
                                                "content": [
                                                    {"type": "input_text", "text": text}
                                                ],
                                            },
                                        }
                                    )
                                )
                                await openai_ws.send(
                                    json.dumps({"type": "response.create"})
                                )

                    async def openai_to_browser() -> None:
                        async for raw_message in openai_ws:
                            event = json.loads(raw_message)
                            event_type = event.get("type")
                            if event_type == "response.output_text.delta":
                                await browser_ws.send_json(
                                    {
                                        "type": "assistant_delta",
                                        "delta": event.get("delta", ""),
                                    }
                                )
                            elif event_type == "response.output_text.done":
                                await browser_ws.send_json(
                                    {
                                        "type": "assistant_done",
                                        "text": event.get("text", ""),
                                    }
                                )
                            elif event_type == "response.function_call_arguments.done":
                                await browser_ws.send_json(
                                    {"type": "tool", "name": event.get("name", "")}
                                )
                                await handle_tool_call(
                                    event,
                                    openai_ws,
                                    caller_number,
                                    notify_staff=False,
                                )
                            elif event_type == "error":
                                error = event.get("error", {})
                                logger.error("OpenAI text test error: %s", error)
                                await browser_ws.send_json(
                                    {
                                        "type": "error",
                                        "message": error.get(
                                            "message", "OpenAI Realtime error."
                                        ),
                                    }
                                )

                    receiver = asyncio.create_task(browser_to_openai())
                    sender = asyncio.create_task(openai_to_browser())
                    done, pending = await asyncio.wait(
                        {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with suppress(asyncio.CancelledError):
                            await task
                    for task in done:
                        task.result()
    except WebSocketDisconnect:
        logger.info("Local text booking test disconnected")
    except Exception:
        logger.exception("Local text booking test failed")
        with suppress(Exception):
            await browser_ws.send_json(
                {"type": "error", "message": "Text booking test connection failed."}
            )
            await browser_ws.close(code=1011)


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request) -> Response:
    """Return TwiML that connects the call to our bidirectional WebSocket."""
    caller_number = ""
    if request.method == "POST":
        form = parse_qs((await request.body()).decode("utf-8", errors="ignore"))
        caller_number = form.get("From", [""])[0]
    normalized_caller = normalize_us_phone_number(caller_number)
    if normalized_caller and normalized_caller in AI_BYPASS_NUMBERS:
        logger.info("Skipped AI for configured caller: ***%s", normalized_caller[-4:])
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Reject reason="rejected" /></Response>'
            ),
            media_type="application/xml",
        )
    if is_caller_blocked(caller_number):
        logger.info("Rejected blocked caller: ***%s", caller_number[-4:])
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Reject reason="rejected" /></Response>'
            ),
            media_type="application/xml",
        )
    require_runtime_config()
    websocket_url = PUBLIC_BASE_URL.replace("https://", "wss://", 1).replace(
        "http://", "ws://", 1
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={quoteattr(websocket_url + '/media-stream')}>"
        f"<Parameter name=\"caller_number\" value={quoteattr(caller_number)} />"
        "</Stream>"
        "</Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket) -> None:
    """Bridge Twilio's μ-law audio stream to an OpenAI Realtime session."""
    await twilio_ws.accept()

    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY is missing")
        await twilio_ws.close(code=1011)
        return

    query = urlencode({"model": OPENAI_REALTIME_MODEL})
    openai_url = f"wss://api.openai.com/v1/realtime?{query}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    stream_sid: str | None = None
    caller_number = ""
    greeting_playback_done = asyncio.Event()
    greeting_mark_sent = False
    try:
        async with websockets.connect(
            openai_url,
            additional_headers=headers,
            max_size=None,
        ) as openai_ws:
            await configure_openai_session(openai_ws)
            async def twilio_to_openai() -> None:
                nonlocal stream_sid, caller_number
                try:
                    while True:
                        message = json.loads(await twilio_ws.receive_text())
                        event = message.get("event")

                        if event == "start":
                            stream_sid = message["start"]["streamSid"]
                            caller_number = message["start"].get("customParameters", {}).get(
                                "caller_number", ""
                            )
                            start_call_session(stream_sid, caller_number)
                            logger.info("Twilio stream started: %s", stream_sid)
                            await ask_openai_to_greet(openai_ws)
                            await notify_incoming_call(caller_number)
                        elif event == "media":
                            if not greeting_playback_done.is_set():
                                continue
                            await openai_ws.send(
                                json.dumps(
                                    {
                                        "type": "input_audio_buffer.append",
                                        "audio": message["media"]["payload"],
                                    }
                                )
                            )
                        elif (
                            event == "mark"
                            and message.get("mark", {}).get("name")
                            == GREETING_PLAYBACK_MARK
                        ):
                            greeting_playback_done.set()
                            logger.info("Greeting playback completed; caller audio enabled")
                        elif event == "stop":
                            logger.info("Twilio stream stopped")
                            break
                except WebSocketDisconnect:
                    logger.info("Caller disconnected")

            async def openai_to_twilio() -> None:
                nonlocal greeting_mark_sent
                async for raw_message in openai_ws:
                    event = json.loads(raw_message)
                    event_type = event.get("type")

                    if event_type == "response.output_audio.delta" and stream_sid:
                        await twilio_ws.send_json(
                            {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": event["delta"]},
                            }
                        )
                    elif event_type == "input_audio_buffer.speech_started" and stream_sid:
                        # Remove audio Twilio has buffered when the caller interrupts.
                        await twilio_ws.send_json(
                            {"event": "clear", "streamSid": stream_sid}
                        )
                    elif (
                        event_type == "response.done"
                        and stream_sid
                        and not greeting_mark_sent
                        and event.get("response", {}).get("status") == "completed"
                    ):
                        # Twilio echoes this mark only after all queued greeting audio
                        # has actually played on the call. Keep caller audio muted until then.
                        await twilio_ws.send_json(
                            {
                                "event": "mark",
                                "streamSid": stream_sid,
                                "mark": {"name": GREETING_PLAYBACK_MARK},
                            }
                        )
                        greeting_mark_sent = True
                    elif event_type == "response.function_call_arguments.done":
                        await handle_tool_call(event, openai_ws, caller_number)
                    elif (
                        event_type == "conversation.item.input_audio_transcription.completed"
                        and stream_sid
                    ):
                        transcript = event.get("transcript", "")
                        append_call_transcript(
                            stream_sid,
                            "customer",
                            transcript,
                            item_id=event.get("item_id", ""),
                        )
                        live_spam = detect_live_spam(transcript)
                        if live_spam:
                            logger.info(
                                "Ending live %s call from ***%s: %s",
                                live_spam["category"],
                                caller_number[-4:],
                                live_spam["reason"],
                            )
                            # Closing the only TwiML <Connect><Stream> ends the call
                            # before OpenAI is asked to generate another reply.
                            with suppress(Exception):
                                await twilio_ws.close(code=1000)
                            await record_live_spam_detection(
                                stream_sid,
                                caller_number,
                                live_spam,
                            )
                            break
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                    elif event_type == "response.output_audio_transcript.done" and stream_sid:
                        append_call_transcript(
                            stream_sid,
                            "karin",
                            event.get("transcript", ""),
                            item_id=event.get("item_id", ""),
                        )
                    elif event_type == "conversation.item.input_audio_transcription.failed":
                        logger.warning("Caller transcription failed: %s", event.get("error"))
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                    elif event_type == "error":
                        logger.error("OpenAI Realtime error: %s", event.get("error"))

            receiver = asyncio.create_task(twilio_to_openai())
            sender = asyncio.create_task(openai_to_twilio())
            done, pending = await asyncio.wait(
                {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
    except Exception:
        logger.exception("Call bridge failed")
        with suppress(Exception):
            await twilio_ws.close(code=1011)
    finally:
        if stream_sid:
            finish_call_session(stream_sid)
            await notify_completed_call_transcript(stream_sid, caller_number)
            await review_completed_call_for_spam(stream_sid, caller_number)


async def build_realtime_instructions() -> str:
    local_now = datetime.now(ZoneInfo("America/Los_Angeles"))
    local_datetime = local_now.strftime("%Y-%m-%d %H:%M")
    learned_rows = await asyncio.to_thread(list_knowledge_entries)
    # Remove only identical Q/A pairs; preserve distinct or conflicting staff answers.
    # Do not mutate the stored knowledge or truncate business facts to save tokens.
    answers = dict.fromkeys(
        (row["question"].strip(), row["answer"].strip()) for row in learned_rows
    )
    learned_answers = "\n".join(f"Q: {question}\nA: {answer}" for question, answer in answers)
    knowledge = f"\nSTAFF-PROVIDED ANSWERS:\n{learned_answers}" if learned_answers else ""
    return (
        f"{RECEPTIONIST_PROMPT}{knowledge}\nCurrent America/Los_Angeles local date and "
        f"time: {local_datetime}. Never offer a time that has already passed."
    )


async def configure_openai_session(openai_ws, *, text_only: bool = False) -> None:
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": await build_realtime_instructions(),
            "output_modalities": ["text"] if text_only else ["audio"],
            "tools": REALTIME_TOOLS,
            "tool_choice": "auto",
        },
    }
    if not text_only:
        session_update["session"]["audio"] = {
            "input": {
                "format": {"type": "audio/pcmu"},
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.7,
                    "silence_duration_ms": 600,
                    # Screen the caller's completed utterance before spending
                    # output tokens on the next AI reply.
                    "create_response": False,
                    "interrupt_response": True,
                },
            },
            "output": {
                "format": {"type": "audio/pcmu"},
                "voice": OPENAI_VOICE,
                "speed": 1.0,
            },
        }
    if OPENAI_REALTIME_MODEL.startswith("gpt-realtime-2"):
        session_update["session"]["reasoning"] = {
            "effort": OPENAI_REALTIME_REASONING_EFFORT,
        }
    await openai_ws.send(json.dumps(session_update))


async def notify_incoming_call(caller_number: str) -> None:
    """Notify staff as soon as a phone call reaches the AI receptionist."""
    local_time = datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
        "%m/%d %I:%M %p"
    )
    notification = (
        "☎️ AI 接到新来电\n"
        f"来电：{caller_number or '未知'}\n"
        f"时间：{local_time}\n"
        "通话结束后可直接传电话末四码查看对话"
    )
    await push_configured_group_notification(
        notification,
        "Failed to send incoming-call notification to LINE",
        notification_disabled=True,
    )


async def push_line_notification(
    target_ids: list[str],
    notification: str,
    error_message: str,
    *,
    notification_disabled: bool = False,
) -> None:
    for target_id in target_ids:
        try:
            await push_text(
                target_id,
                notification,
                LINE_CHANNEL_ACCESS_TOKEN,
                notification_disabled=notification_disabled,
            )
        except Exception:
            logger.exception(error_message)


async def push_configured_group_notification(
    notification: str,
    error_message: str,
    *,
    notification_disabled: bool = False,
) -> None:
    """Send Footprint alerts independently to Telegram and the existing LINE group."""
    if TELEGRAM_BOT_TOKEN:
        for chat_id in sorted(_configured_telegram_notification_chat_ids()):
            try:
                await send_telegram_text(
                    TELEGRAM_BOT_TOKEN,
                    chat_id,
                    notification,
                )
            except Exception:
                logger.exception("Failed to send Footprint notification to Telegram")
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("LINE notification skipped because the access token is missing")
        return
    group_id = await asyncio.to_thread(get_appointment_notification_target)
    if not group_id:
        logger.warning("LINE group is not configured; proactive notification skipped")
        return
    await push_line_notification(
        [group_id],
        notification,
        error_message,
        notification_disabled=notification_disabled,
    )


async def notify_completed_call_transcript(
    stream_sid: str,
    caller_number: str,
) -> None:
    """Send the complete, ordered call transcript to Footprint's Telegram chat."""
    notification_chat_ids = _configured_telegram_notification_chat_ids()
    if not TELEGRAM_BOT_TOKEN or not notification_chat_ids:
        return
    rows = await asyncio.to_thread(get_call_transcript, stream_sid)
    local_time = datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
        "%m/%d %I:%M %p"
    )
    transcript = "\n".join(
        f"{'客人' if row['speaker'] == 'customer' else 'Karin'}：{row['text']}"
        for row in rows
    ) or "（没有可读的对话文字）"
    notification = (
        "📞 通话已结束\n"
        f"来电：{caller_number or '未知'}\n"
        f"时间：{local_time}\n\n"
        f"完整对话：\n{transcript}"
    )
    for chat_id in sorted(notification_chat_ids):
        try:
            await send_telegram_text(
                TELEGRAM_BOT_TOKEN,
                chat_id,
                notification,
            )
        except Exception:
            logger.exception("Failed to send completed Footprint transcript to Telegram")


def detect_live_spam(transcript: str) -> dict[str, str] | None:
    """Detect only explicit spam phrases safe enough for an immediate hangup."""
    normalized = " ".join(transcript.casefold().split())
    if not normalized:
        return None
    for pattern in LIVE_ROBOCALL_PATTERNS:
        if re.search(pattern, normalized):
            return {
                "category": "robocall",
                "reason": "Explicit automated or press-key call language.",
            }
    has_sales_intent = any(
        re.search(pattern, normalized) for pattern in LIVE_SALES_INTENT_PATTERNS
    )
    has_sales_topic = any(
        re.search(pattern, normalized) for pattern in LIVE_SALES_TOPIC_PATTERNS
    )
    if has_sales_intent and has_sales_topic:
        return {
            "category": "sales",
            "reason": "Explicit unsolicited sales pitch detected during the call.",
        }
    return None


async def record_live_spam_detection(
    stream_sid: str,
    caller_number: str,
    detection: dict[str, str],
) -> None:
    """Persist, block, and notify after a live spam call has been disconnected."""
    category = detection["category"]
    reason = detection["reason"]
    confidence = 1.0
    await asyncio.to_thread(
        save_call_spam_review,
        stream_sid,
        category,
        confidence,
        reason,
        "blocked",
    )
    blocked = await asyncio.to_thread(
        block_caller,
        caller_number,
        reason,
        category,
        confidence,
        stream_sid,
    )
    if not blocked:
        logger.warning("Unable to block live spam caller with unusable phone number")
        return
    await push_configured_group_notification(
        "🚫 已挂断并封锁广告／机器人来电\n"
        f"号码：{caller_number}\n"
        f"类别：{category}\n"
        f"原因：{reason}\n"
        f"如需恢复，可直接说：解除封锁 {caller_number[-4:]}",
        "Failed to send live spam notification to LINE",
    )


def format_appointment_line_notification(
    result: dict,
    arguments: dict,
    caller_number: str,
) -> str:
    """Format Footprint booking alerts as a compact LINE-friendly card."""
    is_confirmed = result.get("status") == "confirmed"
    party_size = int(result.get("party_size") or arguments.get("party_size") or 1)
    request_ids = result.get("request_ids") or [result["request_id"]]
    request_id_text = "、".join(f"#{request_id}" for request_id in request_ids)
    therapists = result.get("therapists") or []
    therapist_text = (
        "、".join(str(therapist) for therapist in therapists)
        or result.get("therapist")
        or arguments.get("therapist_preference", "")
        or "未指定"
    )
    party_line = f"人数：{party_size}人\n" if party_size > 1 else ""
    return (
        ("新预约成功：\n" if is_confirmed else "新预约待审核：\n")
        + f"{request_id_text}\n"
        f"电话：{caller_number or '未提供'}\n"
        f"时间：{arguments.get('requested_date', '')} "
        f"{arguments.get('requested_time', '')}\n"
        f"服务：{arguments.get('service', '')} "
        f"({arguments.get('duration_minutes', '')} 分钟)\n"
        + party_line
        + f"按摩师：{therapist_text}\n"
        f"状态：{result.get('status', 'needs_review')}。\n\n"
        + ("如资料有误，请人工回拨客人。" if is_confirmed else "请店员确认后回拨客人。")
    )


async def review_completed_call_for_spam(
    stream_sid: str,
    caller_number: str,
) -> None:
    if (
        not GEMINI_API_KEY
        or not caller_number
        or await asyncio.to_thread(is_call_spam_reviewed, stream_sid)
    ):
        return
    rows = await asyncio.to_thread(get_call_transcript, stream_sid)
    customer_lines = [
        row["text"].strip()
        for row in rows
        if row["speaker"] == "customer" and row["text"].strip()
    ]
    if not customer_lines:
        await asyncio.to_thread(
            save_call_spam_review,
            stream_sid,
            "unclear",
            0,
            "No intelligible caller speech.",
            "none",
        )
        return
    transcript = "\n".join(
        f"{'Caller' if row['speaker'] == 'customer' else 'Receptionist'}: {row['text']}"
        for row in rows
    )
    try:
        result = await classify_call_with_gemini(
            transcript,
            GEMINI_API_KEY,
            GEMINI_LINE_MODEL,
        )
    except Exception:
        logger.exception("Gemini call-spam classification failed")
        return

    category = result["category"]
    confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    reason = str(result.get("reason", "")).strip()[:300] or "No reason provided."
    block_worthy = category in {"sales", "scam", "robocall"}
    action = (
        "blocked"
        if block_worthy and confidence >= SPAM_BLOCK_CONFIDENCE
        else "review"
        if block_worthy and confidence >= SPAM_REVIEW_CONFIDENCE
        else "none"
    )
    saved = await asyncio.to_thread(
        save_call_spam_review,
        stream_sid,
        category,
        confidence,
        reason,
        action,
    )
    if not saved:
        return

    if action == "blocked":
        blocked = await asyncio.to_thread(
            block_caller,
            caller_number,
            reason,
            category,
            confidence,
            stream_sid,
        )
        if not blocked:
            logger.warning("Unable to block caller with unusable phone number")
            return
        notification = (
            "🚫 已自动封锁推销来电\n"
            f"号码：{caller_number}\n"
            f"类别：{category}（{round(confidence * 100)}%）\n"
            f"原因：{reason}\n"
            f"如需恢复，可直接说：解除封锁 {caller_number[-4:]}"
        )
    elif action == "review":
        notification = (
            "⚠️ 疑似推销来电，尚未封锁\n"
            f"号码：{caller_number}\n"
            f"类别：{category}（{round(confidence * 100)}%）\n"
            f"原因：{reason}"
        )
    else:
        return

    await push_configured_group_notification(
        notification,
        "Failed to send spam-review notification to LINE",
    )


async def handle_tool_call(
    event: dict,
    openai_ws,
    caller_number: str,
    *,
    notify_staff: bool = True,
) -> None:
    name = event.get("name", "")
    call_id = event.get("call_id", "")
    try:
        arguments = json.loads(event.get("arguments") or "{}")
        if name == "check_walk_in_availability":
            # Schedules have minute precision; do not use a stale model timestamp
            # or reject this current minute merely because seconds have elapsed.
            now = datetime.now(ZoneInfo("America/Los_Angeles")).replace(second=0, microsecond=0)
            result = await asyncio.to_thread(
                check_availability, requested_date=now.date().isoformat(),
                requested_time=now.strftime("%H:%M"), now_local=now, **arguments,
            )
        elif name == "check_availability":
            result = await asyncio.to_thread(check_availability, **arguments)
        elif name == "get_business_information":
            result = get_business_information(**arguments)
        elif name == "get_therapists_in_store":
            result = await asyncio.to_thread(get_therapists_in_store, **arguments)
        elif name == "save_appointment_request":
            result = await asyncio.to_thread(
                save_appointment_request,
                customer_name=DEFAULT_CALLER_NAME,
                customer_phone=caller_number,
                **arguments,
            )
        elif name == "save_unverified_appointment_request":
            result = await asyncio.to_thread(
                save_unverified_appointment_request,
                customer_name=DEFAULT_CALLER_NAME,
                customer_phone=caller_number,
                **arguments,
            )
        elif name == "save_unanswered_question":
            result = await asyncio.to_thread(
                save_unanswered_question,
                caller_phone=caller_number,
                **arguments,
            )
        elif name == "request_staff_callback":
            result = await asyncio.to_thread(
                save_unanswered_question,
                caller_phone=caller_number,
                question="Caller requests a staff callback.",
                context="Requested during the AI phone call.",
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Tool call failed: %s", exc)
        result = {"error": str(exc)}

    if name == "save_unanswered_question":
        logger.info(
            "Unanswered question save result: saved=%s question_id=%s",
            bool(result.get("saved")),
            result.get("question_id"),
        )
        if result.get("saved") and notify_staff:
            notification = (
                "AI 无法回答的问题：\n"
                f"#{result['question_id']} {arguments.get('question', '')}\n"
                f"来电：{caller_number or '未知'}\n"
                f"背景：{arguments.get('context', '') or '无'}\n\n"
                f"回复：回答 {result['question_id']} 正确答案"
            )
            await push_configured_group_notification(
                notification,
                "Failed to send unanswered-question notification",
            )

    if name == "request_staff_callback":
        logger.info(
            "Staff callback save result: saved=%s question_id=%s",
            bool(result.get("saved")),
            result.get("question_id"),
        )
        if result.get("saved") and notify_staff:
            notification = (
                "真人回电请求：\n"
                f"电话：{caller_number or '未知'}\n"
                "请店员回电。"
            )
            await push_configured_group_notification(
                notification,
                "Failed to send staff-callback notification",
            )

    if name in {"save_appointment_request", "save_unverified_appointment_request"}:
        logger.info(
            "Appointment save result: tool=%s saved=%s request_id=%s status=%s",
            name,
            bool(result.get("saved")),
            result.get("request_id"),
            result.get("status"),
        )
        if result.get("saved") and notify_staff:
            notification = format_appointment_line_notification(
                result,
                arguments,
                caller_number,
            )
            await push_configured_group_notification(
                notification,
                "Failed to send appointment notification to the LINE group",
            )

    if name == "get_business_information":
        if result.get("needs_service"):
            response_instructions = (
                f"Ask exactly: '{PRICE_SERVICE_QUESTION}' Then stop. Do not mention "
                "any price, price range, or unrelated service."
            )
        elif result.get("needs_duration"):
            response_instructions = (
                "Ask the returned question in one short sentence. Mention only the "
                "selected service and its available durations; do not list prices or "
                "other services."
            )
        elif result.get("topic") == "prices":
            response_instructions = (
                "Answer only the selected massage service using the returned information. "
                "Give only the requested duration's price, or the returned 60-minute "
                "rate if no duration was requested. A default quote does not select a booking duration. "
                "No preamble, extra durations, "
                "or other services. Then stop; if a duration question is needed, "
                "ask only 'How long?' Never restate the service or add examples."
            )
        else:
            response_instructions = (
                "Answer the caller's exact question using only the returned information. "
                "Use one short sentence; do not read unrelated prices or policies."
            )
    elif name == "check_walk_in_availability":
        response_instructions = (
            "Answer the walk-in inquiry only; no booking questions, staff names, "
            "or reservation claims. Do not save an appointment. "
        )
        if result.get("error") or not result.get("schedule_loaded"):
            response_instructions += "Say only: 'I can't confirm walk-ins right now. Our staff can call you back.'"
        elif result.get("available"):
            response_instructions += "Say only: 'Yes, you can walk in.'"
        elif result.get("suggested_times"):
            response_instructions += (
                "Say only: 'Sorry, not right now. How about [time]?' "
                "Use the first suggested_times start_time in natural AM/PM format."
            )
        else:
            response_instructions += "Say only: 'Sorry, no walk-in times are left today.'"
    elif name == "check_availability":
        response_instructions = (
            "Use simple conversational English, or the caller's language. "
            "Never proactively name or recommend therapists, ask the caller to choose "
            "one. Never read internal schedule status or tool fields aloud. "
            "No checking announcements or filler. "
        )
        if result.get("error"):
            response_instructions += (
                "The check failed. Do not claim availability. Ask only for the missing "
                "or invalid detail if the error identifies one; otherwise offer staff follow-up."
            )
        elif not result.get("schedule_loaded"):
            response_instructions += (
                "Say briefly: 'I can't confirm that time. Our staff can call you back.' "
                "Do not suggest another time or claim a booking. Follow the unverified "
                "request flow if the caller wants to leave their details."
            )
        elif result.get("available") and result.get("preliminary"):
            response_instructions += (
                "Say 'Yes, that time works!' Then ask 'What kind of massage would you like?' "
                "only if unknown; otherwise ask 'How long?' if duration is unknown. "
                "This checks only a starting time for a short session, not the final "
                "duration. Recheck the actual duration before readback or saving. "
                "Never repeat supplied service details or claim a confirmed booking."
            )
        elif result.get("available"):
            response_instructions += (
                "The requested time works for the checked duration. If booking details "
                "are complete, give ONE short final readback of date, time, massage "
                "type and duration and ask 'Is that right?' If party_size is two, say "
                "'for two people' in that readback. Include a therapist only "
                "if the caller explicitly requested one. Otherwise say 'Yes, that time "
                "works.' and ask only for the next missing detail. Do not say booked "
                "until the save tool succeeds."
            )
        else:
            suggestions = result.get("suggested_times") or []
            if suggestions:
                response_instructions += (
                    "Say only 'Sorry, that time doesn't work. How about [time]?' "
                    "Use the first suggested_times start_time in natural AM/PM format. "
                    "Wait for acceptance, then check that time and ask only the next "
                    "missing detail. Never list staff or additional times."
                )
            else:
                response_instructions += "Say only 'Sorry, no later times are open that day.'"
    elif name == "save_unanswered_question":
        if result.get("saved") and result.get("question_id"):
            response_instructions = (
                "Briefly tell the caller you do not want to give incorrect information. "
                "Say a staff member will call back with the answer. Do not mention internal "
                "tools or the LINE bot."
            )
        else:
            response_instructions = (
                "Say you do not have a reliable answer and a staff member can follow up."
            )
    elif name == "request_staff_callback":
        if result.get("saved"):
            response_instructions = (
                "Say exactly: 'A staff member will call you back.'"
            )
        else:
            response_instructions = (
                "Say a staff member can call back. Do not claim the request was saved."
            )
    elif name in {"save_appointment_request", "save_unverified_appointment_request"}:
        if (
            name == "save_appointment_request"
            and result.get("saved")
            and result.get("status") == "confirmed"
            and result.get("request_id")
        ):
            success_sentence = (
                "Your appointments are booked."
                if result.get("party_size") == 2
                else "Your appointment is booked."
            )
            response_instructions = (
                f"Say exactly one short sentence: '{success_sentence}' "
                "Never mention the confirmation number. Do not add a welcome, farewell, repeated "
                "appointment details, or any staff-confirmation language."
            )
        elif (
            name == "save_unverified_appointment_request"
            and result.get("saved")
            and result.get("request_id")
        ):
            response_instructions = (
                "Say 'I've saved your request. Our staff will call you back.' "
                "Do not say it is booked or mention request numbers or internal status."
            )
        else:
            response_instructions = (
                "Clearly tell the caller the appointment request was NOT saved. Explain "
                "the reason from the tool result and continue helping. Do not claim success."
            )
    else:
        response_instructions = (
            "Explain the tool result briefly in the caller's language. "
            "Do not volunteer alternative times or therapist recommendations. "
            "For get_therapists_in_store, answer only the caller's explicit staffing "
            "question in one short sentence, using returned facts and, when "
            "relevant, that the requested therapist is not in the store. If "
            "staffing_overrides marks that therapist off with note 请假, say that she "
            "is off that day."
        )

    await openai_ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                },
            }
        )
    )
    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Be concise and direct. No acknowledgement prefixes or process narration. "
                        + response_instructions
                    )
                },
            }
        )
    )


async def ask_openai_to_greet(openai_ws) -> None:
    # Have the receptionist speak first when the call connects.
    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "instructions": f'Say exactly: "{INITIAL_GREETING}"'
                },
            }
        )
    )
