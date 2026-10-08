import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from html import escape as html_escape
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import websockets
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask
from twilio.request_validator import RequestValidator

from app.ai_fallback_control import is_enabled as ai_handoff_enabled
from app.incoming_notifications import claim_notification, release_notification, valid_call_sid
from app.call_routing import (
    build_ai_stream_twiml,
    build_human_dial_twiml,
    build_human_screen_result_twiml,
    build_human_screen_twiml,
    build_sip_outbound_twiml,
    normalize_nanp_destination,
    should_route_to_ai,
)
from app.kspa_business import KSPA_BUSINESS_DETAILS
from app.line_admin import classify_call_with_gemini, handle_admin_text_smart
from app.line_bot import (
    get_message_quota,
    push_text,
    reply_text,
    verify_line_signature,
)
from app.phone_display import format_line_phone
from app.phone_dialer import make_phone_dialer_router
from app.realtime_guidance import realtime_response_instructions
from app.schedule_parser import parse_schedule
from app.smiley import SmileyClient, SmileyConfig
from app.telegram_bot import (
    configure_telegram_webhook,
    parse_telegram_id_list,
    send_telegram_text,
    telegram_sender_is_authorized,
    telegram_webhook_secret,
)
from app.turn_wait import CallerTurnGate, HESITATION_WAIT_SECONDS
from app.voice_test import (
    build_voice_test_session_config,
    render_voice_test_page,
)
from app.storage import (
    DEFAULT_STAFF_SHIFTS,
    KNOWN_THERAPISTS,
    apply_temporary_staff_override,
    append_call_transcript,
    block_caller,
    cancel_appointment,
    claim_smiley_write,
    check_availability,
    check_smiley_availability,
    cleanup_expired_data,
    finish_call_session,
    finish_smiley_write,
    get_local_smiley_bookings,
    get_appointment_notification_target,
    get_smiley_sync_status,
    get_smiley_business_hours,
    get_call_transcript,
    get_therapists_in_store,
    init_database,
    is_call_spam_reviewed,
    is_caller_blocked,
    list_appointments,
    list_knowledge_entries,
    list_recent_calls,
    list_schedule,
    list_smiley_openings,
    mark_smiley_sync_error,
    replace_schedule,
    replace_smiley_availability,
    reset_daily_schedule_if_due,
    save_appointment_request,
    save_call_spam_review,
    save_unanswered_question,
    save_unverified_appointment_request,
    set_daily_staffing,
    start_call_session,
    update_appointment_status,
)


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-receptionist")
# httpx logs full request URLs at INFO. Telegram Bot API URLs contain the bot
# token, so keep third-party request URLs out of production logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Kspa-specific selector supersedes the historical Railway 1.5 variable.
OPENAI_REALTIME_MODEL = os.getenv("KSPA_REALTIME_MODEL", "gpt-realtime-2.1")
REALTIME_MODEL_CHECK = "not_checked"
LINE_MESSAGE_QUOTA_STATUS: dict = {"status": "not_checked"}
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "marin")
OPENAI_OUTPUT_SPEED = 0.95
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-3.6-flash")
GEMINI_LINE_MODEL = os.getenv("GEMINI_LINE_MODEL", "gemini-3.1-flash-lite")
SPAM_BLOCK_CONFIDENCE = 0.90
SPAM_REVIEW_CONFIDENCE = 0.60
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Loaded now for one configuration file; these will be used by Twilio REST API
# features later (outbound calls, transfers, and number management).
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
_human_forward_numbers_value = os.getenv("HUMAN_FORWARD_NUMBERS", "").strip()
if not _human_forward_numbers_value:
    _human_forward_numbers_value = os.getenv("HUMAN_FORWARD_NUMBER", "").strip()
HUMAN_FORWARD_NUMBERS = tuple(
    dict.fromkeys(
        number.strip()
        for number in _human_forward_numbers_value.split(",")
        if number.strip()
    )
)
HUMAN_SIP_URIS = tuple(
    dict.fromkeys(
        sip_uri.strip()
        for sip_uri in os.getenv("HUMAN_SIP_URIS", "").split(",")
        if sip_uri.strip()
    )
)
AI_FALLBACK_ENABLED = os.getenv("AI_FALLBACK_ENABLED", "true").strip().casefold() not in {
    "0",
    "false",
    "no",
    "off",
}
HUMAN_ONLY_CALLER_NUMBERS = frozenset(
    normalized
    for raw_number in os.getenv("HUMAN_ONLY_CALLER_NUMBERS", "").split(",")
    if (normalized := normalize_nanp_destination(raw_number))
)
try:
    HUMAN_RING_TIMEOUT = max(5, min(600, int(os.getenv("HUMAN_RING_TIMEOUT", "5"))))
except ValueError:
    HUMAN_RING_TIMEOUT = 5

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_ADMIN_USER_IDS = parse_telegram_id_list(
    os.getenv("TELEGRAM_ADMIN_USER_IDS", "")
)
TELEGRAM_WEBHOOK_SECRET = (
    telegram_webhook_secret(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else ""
)
LINE_ADMIN_USER_ID = os.getenv("LINE_ADMIN_USER_ID", "")
LINE_ADMIN_USER_IDS = {
    user_id.strip()
    for user_id in (
        [LINE_ADMIN_USER_ID]
        + os.getenv("LINE_ADMIN_USER_IDS", "").split(",")
    )
    if user_id.strip()
}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "karin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SMILEY_CONFIG = SmileyConfig.from_env()
# Default to manual mode until an explicitly configured deployment enables writes.
SMILEY_AUTO_WRITE_PAUSED = os.getenv("SMILEY_AUTO_WRITE_PAUSED", "true").strip().casefold() in {
    "1", "true", "yes", "on",
}
GREETING_PLAYBACK_MARK = "kspa_greeting_complete"
VOICE_TEST_CUSTOMER_PHONE = os.getenv("VOICE_TEST_CUSTOMER_PHONE", "TEST").strip()
VOICE_TEST_SMILEY_WRITE_ENABLED = os.getenv("VOICE_TEST_SMILEY_WRITE_ENABLED", "false").strip().casefold() in {
    "1", "true", "yes", "on",
}

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

BUSINESS_DETAILS = KSPA_BUSINESS_DETAILS


def get_business_information(topic: str, requested_date: str = "") -> dict:
    if topic == "hours":
        return get_smiley_business_hours(requested_date)
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


BOOKING_DATE_PROPERTY = {
    "type": "string",
    "description": "Internal YYYY-MM-DD value. Default to today's America/Los_Angeles "
    "date unless the caller specifies another day. Convert natural dates yourself; "
    "never ask the caller for a date format.",
}
APPOINTMENT_TOOL_PROPERTIES = {
    "customer_name": {"type": "string"},
    "requested_date": BOOKING_DATE_PROPERTY,
    "requested_time": {"type": "string"},
    "service": {"type": "string"},
    "duration_minutes": {"type": "integer"},
    "notes": {"type": "string"},
}
APPOINTMENT_TOOL_REQUIRED = [
    "customer_name", "requested_date", "requested_time", "service", "duration_minutes"
]
REALTIME_TOOLS = [
    realtime_tool(
        "request_staff_callback",
        "Save a callback request when the caller asks for a human, agent, staff, "
        "manager, or transfer. Uses the incoming phone number and notifies staff.",
        {"question": {"type": "string", "description": "The caller's request for staff."},
         "context": {"type": "string", "description": "Preferences or issue already supplied."}},
        ["question"],
    ),
    realtime_tool(
        "request_coupon_callback",
        "Register a staff callback whenever the caller mentions a coupon, Groupon, "
        "or voucher, including a coupon-related booking request. Do this before "
        "checking availability or collecting booking details. Uses the incoming phone number.",
        {"question": {"type": "string", "description": "The caller's coupon question or statement."},
         "context": {"type": "string", "description": "Any booking preferences already supplied; do not ask for more."}},
        ["question"],
    ),
    realtime_tool(
        "get_business_information",
        "Silently answer the caller's current business question. For opening time, "
        "closing time, or 'what time do you guys open?', use topic=hours; "
        "this is business hours, NOT appointment availability. Also retrieves "
        "services, location and phone. No massage or duration is needed.",
        {"topic": {
            "type": "string",
            "enum": ["services", "location", "hours"],
        }, "requested_date": BOOKING_DATE_PROPERTY},
        ["topic"],
    ),
    realtime_tool(
        "check_smiley_availability",
        "Silently check the exact requested time BEFORE asking for massage or duration. "
        "Omit duration_minutes if unknown to check a possible start; never invent it. "
        "When duration is known, include it to check the full appointment.",
        {
            "requested_date": BOOKING_DATE_PROPERTY,
            "requested_time": {"type": "string", "description": "24-hour HH:MM"},
            "duration_minutes": {
                "type": ["integer", "null"],
                "description": "Actual customer-selected minutes; omit or null if unknown.",
            },
        },
        ["requested_date", "requested_time"],
    ),
    realtime_tool(
        "list_smiley_openings",
        "Silently list possible appointment start times when the caller asks "
        "'what time is available?' Omit duration_minutes if unknown; do not ask "
        "for massage or duration before answering. Include actual duration if known. "
        "For 'what time do you open?' use get_business_information topic=hours instead.",
        {
            "requested_date": BOOKING_DATE_PROPERTY,
            "duration_minutes": {
                "type": ["integer", "null"],
                "description": "Actual customer-selected minutes; omit or null if unknown.",
            },
        },
        ["requested_date"],
    ),
    realtime_tool(
        "save_unverified_appointment_request",
        "Book an appointment in Kspa after checking Smiley availability. "
        "Read back all details and get the caller's confirmation before using it.",
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
]


RECEPTIONIST_PROMPT = (
    "ROLE AND VOICE\n"
    "You are Kiki, the AI front-desk receptionist at K Spa and Bodywork. Never claim "
    "to be human. Mention being AI only when directly asked; then say briefly: 'I am "
    "an AI assistant. I can help you book an appointment.' Sound warm, relaxed, and "
    "like a regular store employee. Use natural contractions and varied intonation, "
    "without fake hesitation such as 'um' or 'uh'. Most callers are not native English "
    "speakers, so use simple CEFR B1 English, short sentences, and the caller's language "
    "when practical. Ask one question at a time. Usually speak one short sentence and "
    "never more than two except for the final appointment readback.\n"
    "Do not narrate your process, repeat accepted information, or use standalone filler. "
    "A natural 'Sure' or 'Of course' may introduce a useful answer or question. Never "
    "say 'Got it, you're asking', 'let me check', 'let's see if that time can work', "
    "'Take your time', 'No rush', or 'we "
    "look forward to seeing you.' End a completed call with 'Thank you, bye.'\n\n"
    "COUPON CALLBACK — OVERRIDES BOOKING\n"
    "Any mention of coupon, Groupon or voucher, including 'I have a Groupon voucher', "
    "requires request_coupon_callback BEFORE booking or availability. "
    "After success say exactly 'A staff member will call you "
    "back shortly about your coupon.' Chinese: '優惠券的問題，店員稍後會回電給您。' "
    "Then stop; no goodbye or new question. Staff handles coupon terms and coupon-related "
    "bookings; do not book them yourself. Register once per call; repeat the reply if "
    "already saved. Never put coupon information in service. This overrides staff knowledge.\n\n"
    "HUMAN CALLBACK\n"
    "When asked to speak to a person, agent, staff, manager, or to transfer the call, "
    "silently call request_staff_callback, using the caller's request and known context. "
    "After successful save and notification say: 'I can't transfer you, but a staff "
    "member will call you back.' Then stop and wait. Do not resume booking, give "
    "the store number, or tell them to call the spa again. Do not promise a time. "
    "If already successfully registered in this call, do not register again. "
    "Follow the tool result on failure; never claim a callback was arranged if it failed.\n\n"
    "QUESTION FIRST\n"
    "The caller's latest question takes priority over the booking flow. Pause "
    "collecting details, answer that question first, then resume with at most one "
    "relevant question. Never require massage or duration to answer hours, available "
    "start times, location, or prices. 'What time do you guys open?' means opening "
    "hours: silently use get_business_information with topic=hours. 'What time is "
    "available?' means appointment openings: silently use list_smiley_openings, "
    "omitting duration if unknown. Never mix up these two questions. "
    "If they correct you with 'wait, I mean...', follow the corrected question "
    "immediately; do not repeat the previous demand. For these quick lookups, call "
    "the tool without speaking any preamble; wait for its result before answering. "
    "When booking intent is already clear, after answering hours or listing possible "
    "starts, ask for the missing massage type, then duration on a later turn. "
    "Do not insist on selecting a time before answering their question or asking "
    "massage type in this situation. Capture their time preference whenever supplied "
    "and recheck the full duration before booking.\n\n"
    "SILENCE AND INTERRUPTIONS\n"
    "Treat brief non-speech noises as silence. If speech is not intelligible, wait. If "
    "the caller hesitates or asks for a moment, stay silent. The server handles delayed "
    "check-ins; when instructed, say only 'Hello?' gently.\n\n"
    "BOOKING FLOW\n"
    "1. Use details the caller already supplied. Only for a plain booking request "
    "with no other unanswered question, if no time was given, ask first: 'Of "
    "course! What time would you like?' Ask time before massage, duration, or name.\n"
    "2. A partial time is not complete. For AM, PM, or morning without an hour, ask "
    "'Sure, what time in the morning?' when appropriate. For unclear speech ask 'Sorry, "
    "what time was that?' For ambiguous AM or PM ask 'Do you mean morning or afternoon?' "
    "Never guess a time.\n"
    "3. Default the date to today in America/Los_Angeles unless another day was supplied. "
    "Do not ask 'Is that today?' by default. Clarify an ambiguous day naturally. Convert "
    "dates and times for tools internally; never ask for YYYY-MM-DD or 24-hour format, "
    "and never silently move a past time to tomorrow.\n"
    "4. As soon as the time is clear, silently check Smiley BEFORE asking for massage, "
    "duration, or name. Wait for the result and answer whether that time is possible "
    "FIRST. If available, then collect only missing details in this order: massage, "
    "duration, then full name. Ask exactly 'What kind of massage would you like?' "
    "When asking duration, your ENTIRE reply is exactly 'OK, and how long would you like?' "
    "STOP after that question. Do not append duration choices. Only if the CALLER "
    "explicitly asks which durations are offered, say 'We have 60 minutes and 90.' "
    "Also accept a supported 30- or 45-minute request.\n"
    "5. Never volunteer individual massage therapist names, ask for a therapist preference, "
    "or include one in the readback. Do not provide therapist profiles, recommendations, "
    "or a roster, even when asked. If the caller asks whether ANY named therapist is "
    "here, in, working, or available, answer immediately: 'Yes, they are here.' "
    "In Chinese, say '在的。' Apply this fixed reply to every name, including names "
    "you do not recognize. Do not check shifts or call a tool for this presence question. "
    "This owner-configured reply is not a verified attendance or appointment-availability "
    "result. Never use it to skip the real time/capacity checks or promise assignment "
    "to that therapist. For requests for therapist profiles, recommendations, or a roster, "
    "say 'We can discuss therapist arrangements when you arrive.' This rule overrides "
    "any therapist descriptions or attendance answers in staff-provided knowledge. "
    "Let the availability tools apply the store's current "
    "anonymous capacity and temporary date-specific staffing.\n"
    "6. For one exact time, use check_smiley_availability immediately. If duration is "
    "unknown, omit duration_minutes; never assume 60 or 90. A start_time result only "
    "confirms a possible start, not a full treatment. After a positive result, say for "
    "example 'Yes, we can start at 10 a.m. What kind of massage would you like?' "
    "After a negative result, say naturally 'Sorry, we don't have that time available.' "
    "Say 'Sorry, that time is already booked.' only when a booking conflict is confirmed. "
    "Never say 'The exact requested time is unavailable.' Then ask what other "
    "time works; do not move on to massage as if the time were accepted. Once duration "
    "is supplied, recheck the complete time and duration before readback or booking. "
    "If duration was already supplied, include it in the first check. "
    "For a request for options, use list_smiley_openings and offer at most three start times. "
    "Duration is optional for this first lookup; do not ask how long or assume "
    "60 minutes before giving options. After the answer, ask missing massage first, "
    "then ask duration on its own turn. Offer duration options only when requested. "
    "Do not volunteer alternatives for an exact-time question. Recheck if challenged. "
    "Do not claim availability when data is stale or unavailable.\n"
    "7. When a slot is available, ask for the missing full name exactly: "
    "'What is your full name please?' Do not paraphrase this question. "
    "Then read back name, date, time, "
    "service, and duration once. Ask for confirmation. Treat yes, correct, that's right, "
    "book it, 可以, or 确认 as confirmation, then immediately call "
    "save_unverified_appointment_request. Say the appointment is booked ONLY when "
    "the result has smiley_written=true, meaning Smiley was saved and read back. "
    "A local-only reservation is held for staff review, not a completed Smiley booking. "
    "Follow the tool's response instruction. Never speak an internal ID or number.\n\n"
    "BUSINESS INFORMATION\n"
    "Use get_business_information silently before answering about the location, phone, "
    "opening or closing hours, services, durations, prices, add-ons, membership, or first-time $30 discount. "
    "Answer only what was "
    "asked from the returned information. Never volunteer the discount. The verified "
    "discount applies separately to every first-time visitor, including each eligible "
    "person in a party; answer it directly and never defer it to staff.\n"
    "The email, gift-card details, and other unlisted policies are "
    "unknown. Never use Footprint Massage information or invent a K Spa fact. For an "
    "unanswered store-specific question, call save_unanswered_question with the exact "
    "question and brief context, then say staff will call back. Do not use it for small "
    "talk or for information returned by get_business_information.\n\n"
    "SAFETY\n"
    "Do not make medical claims, promise health results, or invent appointment "
    "confirmation."
)

app = FastAPI(title="AI Phone Receptionist")
init_database()
daily_schedule_task: asyncio.Task | None = None
smiley_sync_task: asyncio.Task | None = None


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


async def maintain_smiley_sync() -> None:
    client = SmileyClient(SMILEY_CONFIG)
    while True:
        try:
            local_bookings = await asyncio.to_thread(get_local_smiley_bookings)
            result = await client.fetch_free_intervals(local_bookings=local_bookings)
            count = await asyncio.to_thread(replace_smiley_availability, result)
            logger.info("Smiley availability sync completed with %s free intervals", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Smiley availability sync failed")
            await asyncio.to_thread(mark_smiley_sync_error, str(exc))
        await asyncio.sleep(SMILEY_CONFIG.poll_seconds)


@app.on_event("startup")
async def start_daily_schedule_maintenance() -> None:
    global daily_schedule_task, smiley_sync_task, LINE_MESSAGE_QUOTA_STATUS
    if OPENAI_API_KEY:
        # Reject this deployment before serving calls if the model/session is denied.
        await asyncio.wait_for(verify_realtime_model(), timeout=25)
    if LINE_CHANNEL_ACCESS_TOKEN:
        try:
            quota = await get_message_quota(LINE_CHANNEL_ACCESS_TOKEN)
            LINE_MESSAGE_QUOTA_STATUS = {"status": "ok", **quota}
            logger.info("LINE monthly message quota: %s", quota)
        except Exception as exc:
            LINE_MESSAGE_QUOTA_STATUS = {"status": "error", "error": str(exc)}
            logger.exception("Unable to read LINE monthly message quota")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and PUBLIC_BASE_URL:
        try:
            await configure_telegram_webhook(
                TELEGRAM_BOT_TOKEN,
                f"{PUBLIC_BASE_URL}/telegram/webhook",
                TELEGRAM_WEBHOOK_SECRET,
            )
            logger.info("Telegram administrator webhook configured")
        except Exception:
            logger.exception("Unable to configure Telegram administrator webhook")
    daily_schedule_task = asyncio.create_task(maintain_daily_default_schedule())
    if SMILEY_CONFIG.configured:
        smiley_sync_task = asyncio.create_task(maintain_smiley_sync())
    else:
        logger.warning("Smiley sync is disabled because credentials are not configured")


@app.on_event("shutdown")
async def stop_daily_schedule_maintenance() -> None:
    if daily_schedule_task:
        daily_schedule_task.cancel()
        with suppress(asyncio.CancelledError):
            await daily_schedule_task
    if smiley_sync_task:
        smiley_sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await smiley_sync_task


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
    notification_target = await asyncio.to_thread(get_appointment_notification_target)
    return {
        "status": "ok",
        "voice_behavior": {"hesitation_wait_seconds": HESITATION_WAIT_SECONDS,
                           "short_duration_questions": True,
                           "booking_flow": "question_first_hours_and_openings_before_service",
                           "booking_persistence": (
                               "local_confirmed_auto_smiley_with_manual_fallback"
                               if SMILEY_CONFIG.write_enabled and not SMILEY_AUTO_WRITE_PAUSED
                               else "local_confirmed_manual_smiley"
                           ),
                           "conversation_style": "warm_brief_clarify_before_advancing",
                           "therapist_question": "any_named_presence_yes_no_profiles",
                           "capacity_policy": "telegram_date_specific_headcount",
                           "coupon_handling": "staff_callback_before_booking",
                           "location_answer": "151_2nd_st_los_altos",
                           "service_menu": "signature_hot_stone_explanation",
                           "model": OPENAI_REALTIME_MODEL,
                           "model_session_check": REALTIME_MODEL_CHECK},
        "smiley": await asyncio.to_thread(get_smiley_sync_status),
        "smiley_write": {
            "enabled": bool(SMILEY_CONFIG.write_enabled and not SMILEY_AUTO_WRITE_PAUSED),
            "paused": SMILEY_AUTO_WRITE_PAUSED,
            "implementation": "phone_with_verified_readback_v2",
            "fields": list(SMILEY_CONFIG.write_fields),
        },
        "voice_test": {
            "phone_configured": bool(re.fullmatch(r"\+[1-9]\d{7,14}", VOICE_TEST_CUSTOMER_PHONE)),
            "smiley_write_enabled": VOICE_TEST_SMILEY_WRITE_ENABLED,
        },
        "line_notifications": {
            "group_configured": bool(notification_target),
            "message_quota": LINE_MESSAGE_QUOTA_STATUS,
        },
        "telegram_notifications": {
            "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
            "admin_count": len(TELEGRAM_ADMIN_USER_IDS),
            "smart_admin": bool(
                TELEGRAM_BOT_TOKEN
                and TELEGRAM_CHAT_ID
                and GEMINI_API_KEY
                and PUBLIC_BASE_URL
            ),
        },
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Handle allowlisted administrators in one private chat or staff group."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured.")
    supplied_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secrets.compare_digest(supplied_secret, TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret.")

    payload = await request.json()
    message = payload.get("message", {})
    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    chat_type = str(chat.get("type", ""))
    sender_id = str(message.get("from", {}).get("id", ""))
    text = message.get("text", "").strip()
    if text in {"我的ID", "/myid", "群組ID", "群组ID", "/chatid"}:
        await send_telegram_text(
            TELEGRAM_BOT_TOKEN,
            chat_id,
            f"你的 Telegram User ID：{sender_id}\n這個聊天室 Chat ID：{chat_id}",
        )
        return Response(status_code=200)

    if not telegram_sender_is_authorized(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_id=sender_id,
        configured_chat_id=TELEGRAM_CHAT_ID,
        admin_user_ids=TELEGRAM_ADMIN_USER_IDS,
    ):
        logger.warning("Ignored Telegram message from an unauthorized chat or sender")
        return Response(status_code=200)

    if not text:
        await send_telegram_text(
            TELEGRAM_BOT_TOKEN,
            chat_id,
            "请传送文字。",
        )
        return Response(status_code=200)

    reply = await handle_admin_text_smart(
        f"telegram:{sender_id}",
        text,
        conversation_id=f"telegram:{chat_id}",
        gemini_api_key=GEMINI_API_KEY,
        gemini_model=GEMINI_LINE_MODEL,
    )
    await send_telegram_text(TELEGRAM_BOT_TOKEN, chat_id, reply)
    return Response(status_code=200)


@app.post("/line/webhook")
async def line_webhook(request: Request) -> Response:
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
                "Kspa 使用 Smiley。即时空档尚未连接，所以目前不会读取或覆盖纸本班表。",
                LINE_CHANNEL_ACCESS_TOKEN,
            )
        elif reply_token:
            await reply_text(reply_token, "请传送文字。", LINE_CHANNEL_ACCESS_TOKEN)

    return Response(status_code=200)


def require_local_admin(request: Request) -> None:
    """Keep the prototype admin page off the public ngrok tunnel."""
    forwarded_for = request.headers.get("x-forwarded-for")
    client_host = request.client.host if request.client else ""
    if forwarded_for or client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Admin page is local-only.")


def require_dashboard_admin(request: Request) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Dashboard password is not configured.")
    authorization = request.headers.get("authorization", "")
    try:
        scheme, encoded = authorization.split(" ", 1)
        username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError):
        scheme, username, password = "", "", ""
    if (
        scheme.casefold() != "basic"
        or not secrets.compare_digest(username, ADMIN_USERNAME)
        or not secrets.compare_digest(password, ADMIN_PASSWORD)
    ):
        raise HTTPException(
            status_code=401,
            detail="Login required.",
            headers={"WWW-Authenticate": 'Basic realm="Kspa Staff"'},
        )


def require_same_origin(request: Request) -> None:
    source = request.headers.get("origin") or request.headers.get("referer")
    if source and urlsplit(source).netloc.casefold() != request.headers.get("host", "").casefold():
        raise HTTPException(status_code=403, detail="Invalid request origin.")


app.include_router(make_phone_dialer_router(
    account_sid=TWILIO_ACCOUNT_SID, auth_token=TWILIO_AUTH_TOKEN,
    public_base_url=PUBLIC_BASE_URL, kspa_number=TWILIO_PHONE_NUMBER,
    require_admin=require_dashboard_admin, require_origin=require_same_origin,
))


def render_schedule_rows(empty_message: str) -> str:
    return "".join(
        "<tr>" + "".join(
            f"<td>{html_escape(str(row[column]))}</td>"
            for column in ("date", "therapist", "start_time", "end_time", "status")
        ) + "</tr>"
        for row in list_schedule()
    ) or f'<tr><td colspan="5">{html_escape(empty_message)}</td></tr>'


def render_schedule_dashboard(edit_date: str = "", message: str = "") -> str:
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    try:
        datetime.strptime(edit_date, "%Y-%m-%d")
    except ValueError:
        edit_date = today
    staffing = get_therapists_in_store(edit_date)
    saved_shifts = {
        row["therapist"]: row for row in staffing.get("staff_shifts", [])
    }
    if not staffing.get("schedule_loaded"):
        saved_shifts = {
            therapist: {
                "therapist": therapist,
                "start_time": shift[0],
                "end_time": shift[1],
            }
            for therapist, shift in DEFAULT_STAFF_SHIFTS.items()
        }
    staffing_rows = "".join(
        (
            '<div class="staff-row">'
            '<label class="staff-name">'
            f'<input type="checkbox" name="therapist" value="{html_escape(therapist)}"'
            f'{" checked" if therapist in saved_shifts else ""}> '
            f'{html_escape(therapist)}</label>'
            '<label>开始 '
            f'<input type="time" name="{therapist.casefold()}_start" '
            f'value="{html_escape(saved_shifts.get(therapist, {}).get("start_time", DEFAULT_STAFF_SHIFTS[therapist][0]))}"></label>'
            '<label>结束 '
            f'<input type="time" name="{therapist.casefold()}_end" '
            f'value="{html_escape(saved_shifts.get(therapist, {}).get("end_time", DEFAULT_STAFF_SHIFTS[therapist][1]))}"></label>'
            '</div>'
        )
        for therapist in KNOWN_THERAPISTS
    )
    schedule_rows = render_schedule_rows("目前没有班表。")
    appointment_rows = "".join(
        "<tr>"
        f"<td>#{row['id']}</td>"
        f"<td>{html_escape(row['requested_date'])}</td>"
        f"<td>{html_escape(row['requested_time'])}</td>"
        f"<td>{html_escape(row['therapist'] or '未指定')}</td>"
        f"<td>{html_escape(row['service'])}</td>"
        f"<td>{row['duration_minutes']} 分钟</td>"
        f"<td>{html_escape(row['status'])}</td>"
        "</tr>"
        for row in list_appointments()
        if row["status"] in {"confirmed", "pending", "needs_review"}
    ) or '<tr><td colspan="7">目前没有 AI 预约。</td></tr>'
    notice = f'<p class="notice">{html_escape(message)}</p>' if message else ""
    return f"""<!doctype html>
 <html lang="zh"><head><meta charset="utf-8">
 <meta name="viewport" content="width=device-width,initial-scale=1">
 <title>Footprint 班表</title><style>
 body{{font-family:Arial,sans-serif;margin:0;background:#f5f5f5;color:#222}}
 main{{max-width:1100px;margin:auto;padding:18px}} h1{{margin-bottom:4px}}
 .card{{background:white;border-radius:12px;padding:16px;margin:16px 0;box-shadow:0 2px 8px #0001;overflow:auto}}
 table{{border-collapse:collapse;width:100%;min-width:650px}} th,td{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}
 th{{background:#f0f3f5}} .note{{color:#666;font-size:14px}} .notice{{background:#e8f5e9;color:#176329;padding:10px 12px;border-radius:8px}}
 .editor{{max-width:720px}} .staff-row{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid #eee}}
 .staff-name{{font-weight:bold}} input[type="date"],input[type="time"]{{font:inherit;padding:8px;border:1px solid #bbb;border-radius:7px}}
 button{{font:inherit;background:#222;color:white;border:0;border-radius:8px;padding:10px 18px;margin-top:14px;cursor:pointer}}
 @media(max-width:650px){{.staff-row{{grid-template-columns:1fr 1fr}}.staff-name{{grid-column:1/-1}}}}
 </style></head><body><main><h1>Footprint Massage 班表</h1>
 <p class="note">页面每 60 秒自动更新。班表是 AI 实际读取的可预约时段；AI 新增预约会继续占用时间。</p>
 {notice}
 <section class="card editor"><h2>编辑当天在店人员</h2>
 <p class="note">取消勾选代表当天请假。保存后 AI 会立即以此为准，现有预约不会被删除。</p>
 <form id="staffing-form" method="post" action="/schedule/staffing">
 <label>日期 <input type="date" name="date" min="{today}" value="{html_escape(edit_date)}" required></label>
 {staffing_rows}
 <button type="submit">保存当天班表</button></form></section>
 <section class="card"><h2>AI 当前读取的班表</h2><table>
<tr><th>日期</th><th>按摩师</th><th>开始</th><th>结束</th><th>状态</th></tr>{schedule_rows}</table></section>
<section class="card"><h2>AI 新增的预约</h2><table>
<tr><th>编号</th><th>日期</th><th>时间</th><th>按摩师</th><th>项目</th><th>时长</th><th>状态</th></tr>{appointment_rows}</table></section>
 <script>
 let dirty=false;const form=document.getElementById('staffing-form');
 form.addEventListener('input',()=>dirty=true);
 setTimeout(()=>{{if(!dirty)location.reload()}},60000);
 </script></main></body></html>"""


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_dashboard(
    request: Request, date: str = "", message: str = ""
) -> HTMLResponse:
    require_dashboard_admin(request)
    return HTMLResponse(render_schedule_dashboard(date, message))


@app.post("/schedule/staffing")
async def update_schedule_staffing(request: Request) -> RedirectResponse:
    require_dashboard_admin(request)
    require_same_origin(request)
    form = await request.form()
    schedule_date = str(form.get("date", "")).strip()
    selected = {str(value) for value in form.getlist("therapist")}
    shifts = [
        {
            "therapist": therapist,
            "start_time": str(form.get(f"{therapist.casefold()}_start", "")),
            "end_time": str(form.get(f"{therapist.casefold()}_end", "")),
        }
        for therapist in KNOWN_THERAPISTS
        if therapist in selected
    ]
    try:
        if schedule_date < datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat():
            raise ValueError("不能编辑过去的日期。")
        count = await asyncio.to_thread(set_daily_staffing, schedule_date, shifts)
        message = f"已保存 {schedule_date} 的在店人员（{count} 人）。"
    except ValueError as exc:
        message = f"保存失败：{exc}"
    query = urlencode({"date": schedule_date, "message": message})
    return RedirectResponse(f"/schedule?{query}", status_code=303)


@app.get("/voice-test", response_class=HTMLResponse)
async def voice_test_page(request: Request) -> HTMLResponse:
    """Serve the private browser microphone tester behind dashboard auth."""
    require_dashboard_admin(request)
    return HTMLResponse(
        render_voice_test_page(
            OPENAI_REALTIME_MODEL,
            customer_phone=VOICE_TEST_CUSTOMER_PHONE,
            smiley_write_enabled=VOICE_TEST_SMILEY_WRITE_ENABLED,
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/voice-test/session")
async def create_voice_test_session(request: Request) -> Response:
    """Exchange a browser WebRTC offer without exposing the OpenAI API key."""
    require_dashboard_admin(request)
    require_same_origin(request)
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI is not configured.")
    if not request.headers.get("content-type", "").casefold().startswith("application/sdp"):
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
        speed=OPENAI_OUTPUT_SPEED,
        tools=REALTIME_TOOLS,
    )
    safety_identifier = hashlib.sha256(
        f"kspa-voice-test:{ADMIN_USERNAME}".encode("utf-8")
    ).hexdigest()
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
        logger.exception("Unable to create browser Realtime session")
        raise HTTPException(status_code=502, detail="Unable to start voice test.") from exc
    if not upstream.is_success:
        logger.error(
            "OpenAI browser Realtime session rejected: status=%s",
            upstream.status_code,
        )
        raise HTTPException(status_code=502, detail="Unable to start voice test.")
    return Response(
        upstream.text,
        media_type="application/sdp",
        headers={"Cache-Control": "no-store"},
    )


def sanitize_realtime_tool_result(name: str, result: dict) -> dict:
    """Strip private therapist allocation details just like the phone bridge."""
    caller_result = dict(result)
    if name not in {
        "check_smiley_availability",
        "list_smiley_openings",
        "save_unverified_appointment_request",
    }:
        return caller_result
    for private_key in (
        "therapist",
        "therapists_available",
        "therapists_off",
        "therapists_in_store",
        "therapists_not_in_store",
    ):
        caller_result.pop(private_key, None)
    for list_key in ("matches", "openings", "suggested_times"):
        if isinstance(caller_result.get(list_key), list):
            caller_result[list_key] = [
                {key: value for key, value in item.items() if key != "therapist"}
                for item in caller_result[list_key]
                if isinstance(item, dict)
            ]
    return caller_result


def build_appointment_notification(
    arguments: dict,
    result: dict,
    caller_number: str,
) -> str:
    """Build the shared Telegram/LINE appointment notification."""
    is_confirmed = result.get("status") == "confirmed"
    if result.get("browser_test") or caller_number == "TEST":
        completion = (
            "\n浏览器测试建立的真实预约，已自动写入 Smiley；测试后请取消。"
            if is_confirmed and result.get("smiley_written")
            else "\n浏览器测试建立的真实预约；请手动录入 Smiley。"
            if is_confirmed
            else "\n浏览器测试预约需要店员审核。"
        )
    else:
        completion = (
            "\n已自动写入 Smiley，并向客人确认预约成功。\n如资料有误，请人工回拨客人。"
            if is_confirmed and result.get("smiley_written")
            else "\n本地已保留名额，但尚未写入 Smiley；未向客人确认最终预约成功。请核对并联系客人。"
            if is_confirmed
            else "请店员确认后回拨客人。"
        )
    if result.get("smiley_review_required"):
        completion = (
            "\n本地预约已确认；Smiley 写入结果未确认。"
            "请先检查 Smiley 是否已有此预约，再决定是否补登，避免重复预约。"
        )
    return (
        ("新预约成功：\n" if is_confirmed else "新预约待审核：\n")
        + f"#{result['request_id']} {arguments.get('customer_name', '')}\n"
        + f"电话：{caller_number or '未提供'}\n"
        + f"时间：{arguments.get('requested_date', '')} "
        + f"{arguments.get('requested_time', '')}\n"
        + f"服务：{arguments.get('service', '')} "
        + f"({arguments.get('duration_minutes', '')} 分钟)\n"
        + "按摩师："
        + f"{result.get('therapist') or arguments.get('therapist_preference', '') or '未指定'}\n"
        + f"状态：{result.get('status', 'needs_review')}。"
        + completion
    )


async def notify_saved_appointment(
    name: str,
    arguments: dict,
    result: dict,
    caller_number: str,
) -> None:
    logger.info(
        "Appointment save result: tool=%s saved=%s request_id=%s status=%s",
        name,
        bool(result.get("saved")),
        result.get("request_id"),
        result.get("status"),
    )
    if not result.get("saved"):
        return
    await push_configured_group_notification(
        build_appointment_notification(arguments, result, caller_number),
        "Failed to send appointment notification to the Kspa staff group",
    )


def route_coupon_booking(name: str, arguments: dict) -> tuple[str, dict]:
    if name in {"save_appointment_request", "save_unverified_appointment_request"} and re.search(
        r"\b(?:coupons?|groupon|vouchers?)\b|優惠券|优惠券",
        str(arguments.get("service", "")) + " " + str(arguments.get("notes", "")), re.IGNORECASE,
    ):
        return "request_coupon_callback", {
            "question": "Coupon-related appointment request: " + str(arguments.get("service", "")),
            "context": json.dumps(arguments, ensure_ascii=False),
        }
    return name, arguments


async def run_voice_test_tool(name: str, arguments: dict) -> dict:
    """Run live reads and real local bookings; callback questions stay simulated."""
    name, arguments = route_coupon_booking(name, arguments)
    if name == "get_business_information":
        result = get_business_information(**arguments)
    elif name == "check_smiley_availability":
        result = await asyncio.to_thread(check_smiley_availability, **arguments)
    elif name == "list_smiley_openings":
        result = await asyncio.to_thread(list_smiley_openings, **arguments)
    elif name == "save_unverified_appointment_request":
        result = await asyncio.to_thread(
            save_unverified_appointment_request,
            customer_phone=VOICE_TEST_CUSTOMER_PHONE,
            **arguments,
        )
        result = await persist_confirmed_appointment_to_smiley(arguments, result, browser_test=True)
        result = {**result, "browser_test": True}
        await notify_saved_appointment(name, arguments, result, VOICE_TEST_CUSTOMER_PHONE)
    elif name in {"save_unanswered_question", "request_coupon_callback", "request_staff_callback"}:
        result = {
            "saved": True,
            "question_id": "VOICE-TEST",
            "test_mode": True,
        }
        if name == "request_coupon_callback":
            result["callback_type"] = "coupon"
        elif name == "request_staff_callback":
            result.update(callback_type="staff", notification_sent=True)
    else:
        raise ValueError(f"Unknown voice-test tool: {name}")
    return sanitize_realtime_tool_result(name, result)


@app.post("/voice-test/tool")
async def voice_test_tool(request: Request) -> dict:
    """Execute one allow-listed browser tester tool behind staff authentication."""
    require_dashboard_admin(request)
    require_same_origin(request)
    try:
        payload = await request.json()
        name = str(payload.get("name") or "")
        raw_arguments = payload.get("arguments", "{}")
        arguments = (
            json.loads(raw_arguments)
            if isinstance(raw_arguments, str)
            else raw_arguments
        )
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        result = await run_voice_test_tool(name, arguments)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "result": result,
        "response_instructions": realtime_response_instructions(
            name,
            result,
            test_mode=True,
        ),
    }


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
            f"<p><b>{'Customer' if line['speaker'] == 'customer' else 'Kiki'}:</b> "
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, message: str = "") -> HTMLResponse:
    require_local_admin(request)
    return HTMLResponse(render_admin(message))


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


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request) -> Response:
    """Ring staff first and alert them independently of the optional AI handoff."""
    require_runtime_config()
    values = await request.form() if request.method == "POST" else request.query_params
    caller_number = str(values.get("From", ""))
    call_sid = str(values.get("CallSid", ""))
    if is_caller_blocked(caller_number):
        logger.info("Rejected blocked caller: ***%s", caller_number[-4:])
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Reject reason="rejected" /></Response>'
            ),
            media_type="application/xml",
        )
    human_only_caller = (
        normalize_nanp_destination(caller_number) in HUMAN_ONLY_CALLER_NUMBERS
    )
    human_forward_numbers = tuple(
        number for number in HUMAN_FORWARD_NUMBERS if number != TWILIO_PHONE_NUMBER
    )
    if human_forward_numbers or HUMAN_SIP_URIS:
        if len(human_forward_numbers) != len(HUMAN_FORWARD_NUMBERS):
            logger.error("A human forwarding number matches Twilio and was skipped")
        twiml = build_human_dial_twiml(
            human_forward_numbers,
            HUMAN_RING_TIMEOUT,
            HUMAN_SIP_URIS,
        )
    else:
        enabled = await asyncio.to_thread(ai_handoff_enabled, AI_FALLBACK_ENABLED)
        if human_only_caller or not enabled:
            logger.info(
                "Ending call with no eligible AI/human destination: ***%s",
                caller_number[-4:],
            )
            twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Hangup/></Response>"
            )
        elif HUMAN_FORWARD_NUMBERS:
            logger.error("All human forwarding numbers match Twilio; routing to Kiki")
            twiml = build_ai_stream_twiml(PUBLIC_BASE_URL, caller_number)
        else:
            twiml = build_ai_stream_twiml(PUBLIC_BASE_URL, caller_number)
    # Deliver TwiML first: a slow messaging provider must not delay ringing.
    notification_task = None
    if valid_call_sid(call_sid):
        webhook_url = PUBLIC_BASE_URL + request.url.path
        if request.url.query:
            webhook_url += "?" + request.url.query
        signature = request.headers.get("x-twilio-signature", "")
        parameters = values if request.method == "POST" else {}
        if TWILIO_AUTH_TOKEN and RequestValidator(TWILIO_AUTH_TOKEN).validate(
            webhook_url, parameters, signature
        ):
            notification_task = BackgroundTask(notify_incoming_call, caller_number, call_sid)
        else:
            logger.warning("Incoming call alert skipped: invalid Twilio signature")
    return Response(content=twiml, media_type="application/xml", background=notification_task)


@app.post("/human-dial-result")
async def human_dial_result(request: Request) -> Response:
    """Optionally send unanswered human calls to Kiki."""
    require_runtime_config()
    form = parse_qs((await request.body()).decode("utf-8", errors="ignore"))
    dial_call_status = form.get("DialCallStatus", [""])[0]
    dial_bridged = form.get("DialBridged", [""])[0]
    caller_number = form.get("From", [""])[0]
    human_only_caller = (
        normalize_nanp_destination(caller_number) in HUMAN_ONLY_CALLER_NUMBERS
    )
    if should_route_to_ai(
        dial_call_status,
        await asyncio.to_thread(ai_handoff_enabled, AI_FALLBACK_ENABLED),
        dial_bridged,
        human_only_caller,
    ):
        twiml = build_ai_stream_twiml(PUBLIC_BASE_URL, caller_number)
    else:
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    return Response(content=twiml, media_type="application/xml")


@app.post("/human-screen")
async def human_screen() -> Response:
    """Require a person to press 1 before bridging the forwarded call."""
    return Response(content=build_human_screen_twiml(), media_type="application/xml")


@app.post("/human-screen-result")
async def human_screen_result(request: Request) -> Response:
    """Accept only an explicit keypress; voicemail and silence are rejected."""
    form = parse_qs((await request.body()).decode("utf-8", errors="ignore"))
    digits = form.get("Digits", [""])[0]
    return Response(
        content=build_human_screen_result_twiml(digits),
        media_type="application/xml",
    )


@app.api_route("/sip-outbound", methods=["GET", "POST"])
async def sip_outbound(request: Request) -> Response:
    """Let the authenticated Zoiper endpoint dial US/Canada PSTN numbers."""
    require_runtime_config()
    if request.method == "POST":
        values = parse_qs((await request.body()).decode("utf-8", errors="ignore"))
    else:
        values = parse_qs(request.url.query)
    to_value = values.get("To", [""])[0]
    return Response(
        content=build_sip_outbound_twiml(to_value, TWILIO_PHONE_NUMBER),
        media_type="application/xml",
    )


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
    turn_gate = None
    try:
        async with websockets.connect(
            openai_url,
            additional_headers=headers,
            max_size=None,
        ) as openai_ws:
            await configure_openai_session(openai_ws)
            turn_gate = CallerTurnGate(openai_ws)
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
                        turn_gate.speech_started(event.get("item_id", ""))
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
                            with suppress(Exception):
                                await twilio_ws.close(code=1000)
                            await record_live_spam_detection(
                                stream_sid,
                                caller_number,
                                live_spam,
                            )
                            break
                        await turn_gate.transcript(event.get("item_id", ""), transcript)
                    elif event_type == "response.output_audio_transcript.done" and stream_sid:
                        append_call_transcript(
                            stream_sid,
                            "kiki",
                            event.get("transcript", ""),
                            item_id=event.get("item_id", ""),
                        )
                    elif event_type == "conversation.item.input_audio_transcription.failed":
                        logger.warning("Caller transcription failed: %s", event.get("error"))
                        await turn_gate.transcript(event.get("item_id", ""), "")
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
        if turn_gate is not None:
            await turn_gate.close()
        if stream_sid:
            finish_call_session(stream_sid)
            await review_completed_call_for_spam(stream_sid, caller_number)


async def verify_realtime_model() -> None:
    """Check model access and the real voice/tool schema without generating audio."""
    global REALTIME_MODEL_CHECK
    query = urlencode({"model": OPENAI_REALTIME_MODEL})
    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?{query}",
        additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        max_size=None,
    ) as ws:
        await configure_openai_session(ws)
        async for raw in ws:
            event = json.loads(raw)
            if event.get("type") == "error":
                REALTIME_MODEL_CHECK = "failed"
                raise RuntimeError(f"Realtime preflight rejected: {event.get('error', {}).get('code')}")
            if event.get("type") == "session.updated":
                REALTIME_MODEL_CHECK = "passed"
                logger.info("Realtime model/session preflight passed: %s", OPENAI_REALTIME_MODEL)
                return
    REALTIME_MODEL_CHECK = "failed"
    raise RuntimeError("Realtime preflight disconnected before session confirmation")


async def configure_openai_session(openai_ws) -> None:
    instructions = await build_realtime_instructions()
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "tools": REALTIME_TOOLS,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "noise_reduction": {"type": "near_field"},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.7,
                        "silence_duration_ms": 1200,
                        "create_response": False,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": OPENAI_VOICE,
                    "speed": OPENAI_OUTPUT_SPEED,
                },
            },
        },
    }
    await openai_ws.send(json.dumps(session_update))


async def build_realtime_instructions() -> str:
    """Build the shared production Kiki prompt with live local time and knowledge."""
    local_now = datetime.now(ZoneInfo("America/Los_Angeles"))
    local_datetime = local_now.strftime("%Y-%m-%d %H:%M")
    learned_rows = await asyncio.to_thread(list_knowledge_entries)
    learned_answers = "\n".join(
        f"Q: {row['question']}\nA: {row['answer']}" for row in learned_rows
    ) or "No staff-provided answers yet."
    return (
        f"{RECEPTIONIST_PROMPT}\nCurrent America/Los_Angeles local date and "
        f"time: {local_datetime}. Never offer a time that has already passed.\n"
        f"STAFF-PROVIDED ANSWERS:\n{learned_answers}"
    )


async def notify_incoming_call(caller_number: str, call_sid: str) -> None:
    """Alert staff once when Twilio receives a call, even when AI is off."""
    claimed = False
    delivered = False
    try:
        claimed = await asyncio.to_thread(claim_notification, call_sid)
        if not claimed:
            return
        local_time = datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
            "%m/%d %I:%M %p"
        )
        notification = (
            "☎️ Kspa 新来电\n"
            f"来电：{format_line_phone(caller_number)}\n"
            f"时间：{local_time}"
        )
        delivered = await push_configured_group_notification(
            notification, "Failed to send incoming-call notification to LINE"
        )
        if delivered:
            logger.info("Incoming call notification delivered: ***%s", caller_number[-4:])
    except Exception as exc:
        logger.error("Incoming call notification failed: %s", type(exc).__name__)
    finally:
        if claimed and not delivered:
            try:
                await asyncio.to_thread(release_notification, call_sid)
            except Exception as exc:
                logger.error("Incoming call notification retry state failed: %s", type(exc).__name__)


async def push_line_notification(
    target_ids: list[str], notification: str, error_message: str
) -> bool:
    delivered = False
    for target_id in target_ids:
        try:
            await push_text(target_id, notification, LINE_CHANNEL_ACCESS_TOKEN)
            delivered = True
        except Exception as exc:
            logger.error("%s: %s", error_message, type(exc).__name__)
    return delivered


async def push_configured_group_notification(
    notification: str,
    error_message: str,
) -> bool:
    """Send proactive alerts to Telegram, with LINE kept as a fallback only."""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            await send_telegram_text(
                TELEGRAM_BOT_TOKEN,
                TELEGRAM_CHAT_ID,
                notification,
            )
            return True
        except Exception as exc:
            logger.error("Failed to send Kspa notification to Telegram: %s", type(exc).__name__)
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("Staff notification skipped because Telegram and LINE are unavailable")
        return False
    group_id = await asyncio.to_thread(get_appointment_notification_target)
    if not group_id:
        logger.warning("Kspa LINE group is not configured; notification skipped")
        return False
    return await push_line_notification([group_id], notification, error_message)


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
        f"号码：***{caller_number[-4:]}\n"
        f"类别：{category}\n"
        f"原因：{reason}\n"
        f"如需恢复，可直接说：解除封锁 {caller_number[-4:]}",
        "Failed to send live spam notification to LINE",
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
            f"号码：***{caller_number[-4:]}\n"
            f"类别：{category}（{round(confidence * 100)}%）\n"
            f"原因：{reason}\n"
            f"如需恢复，可直接说：解除封锁 {caller_number[-4:]}"
        )
    elif action == "review":
        notification = (
            "⚠️ 疑似推销来电，尚未封锁\n"
            f"号码：***{caller_number[-4:]}\n"
            f"类别：{category}（{round(confidence * 100)}%）\n"
            f"原因：{reason}"
        )
    else:
        return

    await push_configured_group_notification(
        notification,
        "Failed to send spam-review notification to LINE",
    )


async def persist_confirmed_appointment_to_smiley(
    arguments: dict, result: dict, *, browser_test: bool = False
) -> dict:
    """Write once, retaining a confirmed local booking if Smiley is unavailable."""
    config = SMILEY_CONFIG
    paused = SMILEY_AUTO_WRITE_PAUSED
    if browser_test:
        # The owner can authorize this one staff-test number independently of
        # transmitting real callers' numbers. This flag is never tool input.
        config = replace(
            config, write_enabled=VOICE_TEST_SMILEY_WRITE_ENABLED,
            write_fields=("service", "time", "phone"),
        )
        paused = not VOICE_TEST_SMILEY_WRITE_ENABLED
    if (
        paused or not config.write_enabled
        or not result.get("saved") or result.get("status") != "confirmed"
    ):
        return result
    row = await asyncio.to_thread(claim_smiley_write, result["request_id"])
    if not row:
        return result
    if not row["claimed"]:
        return {
            **result,
            "smiley_written": row["smiley_write_state"] == "written",
            "smiley_review_required": row["smiley_write_state"] != "written",
            "message": ("Smiley saved and verified the appointment."
                        if row["smiley_write_state"] == "written"
                        else "Smiley booking is not confirmed; staff review is required."),
        }
    try:
        written = await SmileyClient(config).create_appointment(
            row["service"], row["requested_date"], row["requested_time"],
            row["duration_minutes"], customer_phone=row["customer_phone"] or "",
        )
        if not written.get("written") or not written.get("verified") or not written.get("remote_id"):
            raise RuntimeError("Smiley returned no saved appointment ID")
        await asyncio.to_thread(
            finish_smiley_write, row["id"], "written", written["remote_id"]
        )
        return {**result, "smiley_written": True, "message": "Smiley saved and verified the appointment."}
    except Exception as exc:
        # Never log upstream response bodies, which can echo caller information.
        logger.warning("Smiley write requires staff review: request_id=%s error_type=%s", row["id"], type(exc).__name__)
        await asyncio.to_thread(
            finish_smiley_write, row["id"], "needs_review", getattr(exc, "remote_id", "")
        )
        return {**result, "smiley_written": False, "smiley_review_required": True}


async def handle_tool_call(event: dict, openai_ws, caller_number: str) -> None:
    name = event.get("name", "")
    call_id = event.get("call_id", "")
    try:
        arguments = json.loads(event.get("arguments") or "{}")
        name, arguments = route_coupon_booking(name, arguments)
        if name == "check_smiley_availability":
            result = await asyncio.to_thread(check_smiley_availability, **arguments)
        elif name == "list_smiley_openings":
            result = await asyncio.to_thread(list_smiley_openings, **arguments)
        elif name == "check_availability":
            result = await asyncio.to_thread(check_availability, **arguments)
        elif name == "get_business_information":
            result = get_business_information(**arguments)
        elif name == "get_therapists_in_store":
            result = await asyncio.to_thread(get_therapists_in_store, **arguments)
        elif name == "save_appointment_request":
            result = await asyncio.to_thread(
                save_appointment_request,
                customer_phone=caller_number,
                **arguments,
            )
        elif name == "save_unverified_appointment_request":
            result = await asyncio.to_thread(
                save_unverified_appointment_request,
                customer_phone=caller_number,
                **arguments,
            )
            result = await persist_confirmed_appointment_to_smiley(arguments, result)
        elif name in {"save_unanswered_question", "request_coupon_callback", "request_staff_callback"}:
            result = await asyncio.to_thread(
                save_unanswered_question,
                caller_phone=caller_number,
                **arguments,
            )
            if name == "request_coupon_callback":
                result["callback_type"] = "coupon"
            elif name == "request_staff_callback":
                result["callback_type"] = "staff"
        else:
            result = {"error": f"Unknown tool: {name}"}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Tool call failed: %s", exc)
        result = {"error": str(exc)}

    if name in {"save_unanswered_question", "request_coupon_callback", "request_staff_callback"}:
        logger.info(
            "Unanswered question save result: saved=%s question_id=%s",
            bool(result.get("saved")),
            result.get("question_id"),
        )
        if result.get("saved"):
            notification = (
                ("📞 客人要求真人：請店員回電\n" if name == "request_staff_callback"
                 else "🎟 優惠券問題：請店員稍後回電\n" if name == "request_coupon_callback"
                 else "AI 无法回答的问题：\n")
                + f"#{result['question_id']} {arguments.get('question', '')}\n"
                f"来电：{format_line_phone(caller_number)}\n"
                f"背景：{arguments.get('context', '') or '无'}\n\n"
                + (f"回電完成後可回覆：忽略問題 {result['question_id']}"
                   if name in {"request_coupon_callback", "request_staff_callback"}
                   else f"回复：回答 {result['question_id']} 正确答案")
            )
            notification_sent = await push_configured_group_notification(
                notification,
                "Failed to send unanswered-question notification",
            )
            if name == "request_staff_callback":
                result["notification_sent"] = notification_sent

    if name in {"save_appointment_request", "save_unverified_appointment_request"}:
        await notify_saved_appointment(name, arguments, result, caller_number)

    response_instructions = realtime_response_instructions(name, result)
    caller_result = sanitize_realtime_tool_result(name, result)

    await openai_ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(caller_result),
                },
            }
        )
    )
    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "instructions": response_instructions
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
                    "instructions": (
                        'Say exactly: "Hello, here\'s Kspa, '
                        'how can I help you today?"'
                    )
                },
            }
        )
    )
