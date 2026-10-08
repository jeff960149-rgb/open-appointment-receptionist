"""Staff-only browser calls through a separate outbound TwiML application."""
import asyncio
import json
import os
import re
import secrets
import time
from html import escape
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from app import storage

SDK_VERSION = "2.18.4"
LEASE_SECONDS = 360
NO_STORE = {"Cache-Control": "no-store"}


def load_dialer_config() -> dict:
    try:
        config = json.loads(os.getenv("KSPA_PHONE_DIALER_JSON", "{}"))
        return config if isinstance(config, dict) else {}
    except (ValueError, TypeError):
        return {}


def lease(action: str, identity: str = "", call_sid: str = "") -> bool:
    """One persistent, single-use call ticket across tabs and process restarts."""
    now = time.time()
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS phone_test_lease (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            identity TEXT, expires REAL, call_sid TEXT)""")
        row = db.execute("SELECT * FROM phone_test_lease WHERE singleton=1").fetchone()
        if action == "issue":
            if row and row["expires"] > now:
                return False
            db.execute("INSERT OR REPLACE INTO phone_test_lease VALUES (1, ?, ?, '')",
                       (identity, now + LEASE_SECONDS))
        elif action == "consume":
            if not row or row["expires"] <= now or row["identity"] != identity or row["call_sid"]:
                return False
            db.execute("UPDATE phone_test_lease SET call_sid=?, expires=? WHERE singleton=1",
                       (call_sid, now + LEASE_SECONDS))
        elif action == "release":
            if row and row["identity"] == identity and not row["call_sid"]:
                db.execute("DELETE FROM phone_test_lease WHERE singleton=1")
        elif action == "finish":
            if row and row["call_sid"] == call_sid:
                db.execute("DELETE FROM phone_test_lease WHERE singleton=1")
        else:
            raise ValueError("Unknown lease action")
    return True


async def account_busy(account_sid: str, auth_token: str, exclude: str = "") -> bool:
    async with httpx.AsyncClient(
        auth=(account_sid, auth_token), timeout=8,
    ) as client:
        async def check(status):
            response = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json",
                params={"Status": status, "PageSize": 1000},
            )
            response.raise_for_status()
            return any(call["sid"] != exclude for call in response.json()["calls"])
        return any(await asyncio.gather(*(check(s) for s in ("queued", "ringing", "in-progress"))))


def make_phone_dialer_router(*, account_sid, auth_token, public_base_url,
                             kspa_number, require_admin, require_origin, config=None):
    router = APIRouter(prefix="/phone-test")
    cfg = load_dialer_config() if config is None else config
    # The browser's outer call must outlast Kspa's human ringing period.
    # Otherwise it hangs up just before the unanswered call can reach Kiki.
    try:
        human_ring_seconds = max(5, min(600, int(os.getenv("HUMAN_RING_TIMEOUT", "30"))))
    except ValueError:
        human_ring_seconds = 30
    browser_dial_timeout = min(600, max(60, human_ring_seconds + 30))

    def configured():
        if not (account_sid and auth_token and public_base_url.startswith("https://")
                and cfg.get("to_number") == kspa_number
                and cfg.get("from_number") != kspa_number
                and re.fullmatch(r"\+[1-9]\d{7,14}", cfg.get("from_number", ""))
                and cfg.get("api_key_sid") and cfg.get("api_key_secret")
                and cfg.get("app_sid")):
            raise HTTPException(503, "網路撥號尚未設定完成。")

    def staff(request):
        require_admin(request)
        require_origin(request)
        # Browser write actions must carry an explicit same-origin header.
        if request.headers.get("origin") != public_base_url:
            raise HTTPException(403, "Invalid request origin.")

    async def signed_form(request, path):
        configured()
        form = await request.form()
        signature = request.headers.get("x-twilio-signature", "")
        if (request.url.query or not RequestValidator(auth_token).validate(
                public_base_url + "/phone-test/" + path, form, signature)
                or form.get("AccountSid") != account_sid):
            raise HTTPException(403, "Invalid Twilio signature.")
        for key in ("From", "To", "AccountSid", "CallSid", "CallStatus"):
            if len(form.getlist(key)) > 1:
                raise HTTPException(400, "Duplicate call field.")
        return form

    @router.get("", response_class=HTMLResponse)
    async def page(request: Request):
        require_admin(request)
        configured()
        html = (Path(__file__).parent / "phone_dialer.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace("__FROM__", escape(cfg["from_number"]))
                .replace("__TO__", escape(cfg["to_number"])),
            headers=NO_STORE,
        )

    @router.get("/sdk-2.18.4.js")
    async def sdk():
        return FileResponse(Path(__file__).parent / "vendor" / "twilio-voice-2.18.4.min.js",
                            media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @router.post("/token")
    async def token(request: Request):
        staff(request)
        configured()
        body = await request.json()
        if not isinstance(body, dict) or body.get("acknowledge_shared_capacity") is not True:
            raise HTTPException(400, "請先確認這是 Footprint 可暫停接聽的測試時段。")
        try:
            busy = await account_busy(account_sid, auth_token)
        except httpx.HTTPError:
            raise HTTPException(503, "目前無法確認通話狀態，請稍後再試。")
        if busy:
            raise HTTPException(409, "帳戶目前有通話，請結束後再測試。")
        identity = "kspa-test-" + secrets.token_hex(16)
        if not await asyncio.to_thread(lease, "issue", identity):
            raise HTTPException(409, "已有一個測試通話正在準備或進行中，請稍後再試。")
        access = AccessToken(account_sid, cfg["api_key_sid"], cfg["api_key_secret"],
                             identity=identity, ttl=300)
        access.add_grant(VoiceGrant(outgoing_application_sid=cfg["app_sid"],
                                    incoming_allow=False))
        return JSONResponse({"token": access.to_jwt(), "identity": identity,
                             "to": cfg["to_number"]}, headers=NO_STORE)

    @router.post("/release")
    async def release(request: Request):
        staff(request)
        body = await request.json()
        await asyncio.to_thread(lease, "release", str(body.get("identity", "")))
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @router.post("/twiml")
    async def twiml(request: Request):
        form = await signed_form(request, "twiml")
        identity = str(form.get("From", "")).removeprefix("client:")
        call_sid = str(form.get("CallSid", ""))
        if (not re.fullmatch(r"kspa-test-[a-f0-9]{32}", identity)
                or not form.get("From", "").startswith("client:")
                or not re.fullmatch(r"CA[a-fA-F0-9]{32}", call_sid)
                or form.get("To") != cfg["to_number"]):
            raise HTTPException(403, "Only the configured Kspa destination is allowed.")
        if not await asyncio.to_thread(lease, "consume", identity, call_sid):
            raise HTTPException(403, "Expired or already used call ticket.")
        voice = VoiceResponse()
        try:
            busy = await account_busy(account_sid, auth_token, exclude=call_sid)
        except httpx.HTTPError:
            busy = True
        if busy:
            voice.say("Another call is active. Please try again later.")
            voice.hangup()
        else:
            dial = voice.dial(caller_id=cfg["from_number"], timeout=browser_dial_timeout,
                              time_limit=300, answer_on_bridge=True)
            dial.number(cfg["to_number"])
        return Response(str(voice), media_type="application/xml")

    @router.post("/status")
    async def status(request: Request):
        form = await signed_form(request, "status")
        if form.get("CallStatus") in {"completed", "canceled", "failed", "busy", "no-answer"}:
            await asyncio.to_thread(lease, "finish", call_sid=str(form.get("CallSid", "")))
        return Response(status_code=204)

    return router
