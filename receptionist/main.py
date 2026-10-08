from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from twilio.request_validator import RequestValidator

from .booking import Calendar
from .config import load_business
from .notifications import notification_worker
from .realtime import run_bridge
from .tools import ToolSession

load_dotenv()
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def settings_from_env():
    return dict(openai_key=os.getenv('OPENAI_API_KEY', ''), model=os.getenv('OPENAI_REALTIME_MODEL', 'gpt-realtime-2.1'),
                voice=os.getenv('OPENAI_VOICE','marin'), admin_token=os.getenv('ADMIN_TOKEN',''),
                twilio_token=os.getenv('TWILIO_AUTH_TOKEN',''), public_url=os.getenv('PUBLIC_BASE_URL','').rstrip('/'),
                human_numbers=[x.strip() for x in os.getenv('HUMAN_FORWARD_NUMBERS','').split(',') if x.strip()],
                human_sips=[x.strip() for x in os.getenv('HUMAN_SIP_URIS','').split(',') if x.strip()],
                human_only=set(filter(None, os.getenv('HUMAN_ONLY_CALLER_NUMBERS','').split(','))),
                ai_enabled=os.getenv('AI_FALLBACK_ENABLED','false').lower() in {'true','1','yes'},
                ring_seconds=max(5, min(60,int(os.getenv('HUMAN_RING_SECONDS','20')))),
                max_call_seconds=max(30,min(1800,int(os.getenv('MAX_CALL_SECONDS','600')))),
                telegram_token=os.getenv('TELEGRAM_BOT_TOKEN',''), telegram_chat=os.getenv('TELEGRAM_CHAT_ID',''),
                telegram_secret=os.getenv('TELEGRAM_WEBHOOK_SECRET',''),
                demo_enabled=os.getenv('ENABLE_DEMO','true').lower() in {'true','1','yes'})


def create_app(config_path=None, data_dir=None, settings=None):
    config = load_business(config_path or os.getenv('BUSINESS_CONFIG', str(ROOT/'examples/generic.json')))
    cfg = settings if settings is not None else settings_from_env()
    calendar = Calendar(Path(data_dir or os.getenv('DATA_DIR','data'))/'appointments.sqlite3', config)
    demos = {}

    @asynccontextmanager
    async def lifespan(app):
        worker = asyncio.create_task(notification_worker(calendar, cfg['telegram_token'], cfg['telegram_chat']))
        yield
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        for value in demos.values():
            value[1].cleanup()

    app = FastAPI(title='Open Appointment Receptionist', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.calendar, app.state.settings = calendar, cfg

    def admin(request):
        expected = cfg['admin_token']
        provided = request.headers.get('authorization','').removeprefix('Bearer ')
        if len(expected) < 24 or not hmac.compare_digest(provided.encode(), expected.encode()):
            raise HTTPException(401, 'Set ADMIN_TOKEN (at least 24 characters) and send it as a Bearer token')

    def same_origin(request):
        origin = request.headers.get('origin')
        allowed = cfg['public_url'] or str(request.base_url).rstrip('/')
        if origin and origin.rstrip('/') != allowed:
            raise HTTPException(403, 'Origin not allowed')

    async def body(request):
        if int(request.headers.get('content-length','0')) > 16384:
            raise HTTPException(413, 'Request too large')
        raw = await request.body()
        if len(raw) > 16384:
            raise HTTPException(413, 'Request too large')
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise HTTPException(400, 'Expected a JSON object')

    async def twilio(request):
        if not cfg['twilio_token'] or not cfg['public_url'].startswith('https://'):
            raise HTTPException(503, 'Telephony not configured')
        form = await request.form()
        url = cfg['public_url'] + request.url.path
        if request.url.query:
            url += '?' + request.url.query
        if not RequestValidator(cfg['twilio_token']).validate(url, form, request.headers.get('x-twilio-signature','')):
            raise HTTPException(403, 'Invalid provider signature')
        return form

    def xml(root):
        return Response(tostring(root, encoding='unicode'), media_type='application/xml')

    def ai_response(form):
        root = Element('Response')
        enabled = calendar.setting('ai_enabled', cfg['ai_enabled'])
        if not enabled or form.get('From','') in cfg['human_only'] or not cfg['openai_key']:
            SubElement(root,'Say').text = 'Sorry, no one is available to answer right now. Please try again later.'
            SubElement(root,'Hangup')
            return xml(root)
        token = calendar.issue_stream(form.get('CallSid',''), form.get('From',''))
        stream = SubElement(SubElement(root,'Connect'),'Stream', url=cfg['public_url'].replace('https://','wss://',1)+'/media-stream')
        SubElement(stream,'Parameter',name='session_token',value=token)
        return xml(root)

    def demo_session(request):
        token = request.headers.get('x-demo-session','')
        item = demos.get(token)
        if not item or item[2] < calendar.now().timestamp():
            raise HTTPException(401,'Start a new demo session')
        return item[0]

    @app.get('/health')
    async def health():
        return dict(status='ok', version='0.1.0')

    @app.get('/')
    async def home():
        return FileResponse(ROOT/'receptionist/static/index.html')

    @app.get('/api/business')
    async def business():
        return config.model_dump(mode='json')

    @app.post('/api/demo/session')
    async def start_demo(request: Request):
        same_origin(request)
        if not cfg['demo_enabled']:
            raise HTTPException(404)
        now = calendar.now().timestamp()
        for token, item in list(demos.items()):
            if item[2] < now:
                item[1].cleanup()
                del demos[token]
        if len(demos) >= 30:
            raise HTTPException(429,'Demo capacity reached; retry later')
        temp = tempfile.TemporaryDirectory(prefix='appointment-demo-')
        token = secrets.token_urlsafe(32)
        session = ToolSession(Calendar(Path(temp.name)/'demo.sqlite3',config),token,'+12025550123',demo=True)
        demos[token] = (session,temp,now+1800)
        return dict(token=token, expires_in=1800, demo=True)

    @app.post('/api/demo/tool')
    async def demo_tool(request: Request):
        same_origin(request)
        session = demo_session(request)
        payload = await body(request)
        with session.calendar.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0]
        if count >= 100:
            raise HTTPException(429, 'Demo limit reached')
        return await asyncio.to_thread(session.execute, payload.get('name'), payload.get('arguments',{}))

    @app.get('/api/admin/state')
    async def state(request: Request):
        admin(request)
        with calendar.connect() as db:
            bookings = [dict(r) for r in db.execute('SELECT id,details,status FROM bookings ORDER BY start DESC LIMIT 200')]
            callbacks = [dict(r) for r in db.execute('SELECT * FROM callbacks ORDER BY created DESC LIMIT 200')]
            pending = db.execute('SELECT COUNT(*) FROM outbox WHERE sent=0').fetchone()[0]
        return dict(ai_enabled=calendar.setting('ai_enabled',cfg['ai_enabled']), bookings=bookings, callbacks=callbacks, pending_alerts=pending)

    @app.post('/api/admin/capacity')
    async def capacity(request: Request):
        admin(request)
        value = await body(request)
        try:
            return calendar.set_capacity(value['day'],value['capacity'])
        except (KeyError,TypeError,ValueError) as exc:
            raise HTTPException(400,str(exc))

    @app.post('/api/admin/ai')
    async def ai_switch(request: Request):
        admin(request)
        value = (await body(request)).get('enabled')
        if type(value) is not bool:
            raise HTTPException(400,'enabled must be a boolean')
        return dict(enabled=calendar.setting('ai_enabled',False,value))

    @app.post('/api/admin/cancel/{booking_id}')
    async def cancel(booking_id: str, request: Request):
        admin(request)
        return calendar.cancel(booking_id)

    @app.post('/api/admin/callback/{callback_id}/complete')
    async def complete_callback(callback_id: str, request: Request):
        admin(request)
        with calendar.connect(write=True) as db:
            changed=db.execute("UPDATE callbacks SET status='completed' WHERE id=?", (callback_id,)).rowcount
        return dict(completed=changed==1)

    @app.post('/incoming-call')
    async def incoming(request: Request):
        form = await twilio(request)
        if cfg['human_numbers'] or cfg['human_sips']:
            root = Element('Response')
            dial = SubElement(root,'Dial',answerOnBridge='true',timeout=str(cfg['ring_seconds']),action=cfg['public_url']+'/human-dial-result',method='POST')
            for number in cfg['human_numbers']:
                if re.fullmatch(r'\+[1-9]\d{7,14}',number):
                    SubElement(dial,'Number').text=number
            for sip in cfg['human_sips']:
                if re.fullmatch(r'sips?:[^\s<>]+@[^\s<>]+',sip):
                    SubElement(dial,'Sip').text=sip
            return xml(root)
        return ai_response(form)

    @app.post('/human-dial-result')
    async def after_ring(request: Request):
        form = await twilio(request)
        if form.get('DialCallStatus') == 'completed' or form.get('DialBridged') == 'true':
            root=Element('Response'); SubElement(root,'Hangup'); return xml(root)
        return ai_response(form)

    @app.post('/telegram/webhook')
    async def telegram(request: Request):
        secret=cfg['telegram_secret']
        if len(secret)<24 or not hmac.compare_digest(request.headers.get('x-telegram-bot-api-secret-token','').encode(),secret.encode()):
            raise HTTPException(403)
        payload=await body(request)
        message=payload.get('message',{})
        if not cfg['telegram_chat'] or str(message.get('chat',{}).get('id','')) != cfg['telegram_chat']:
            raise HTTPException(403)
        text=message.get('text','').strip()
        reply='Commands: /capacity YYYY-MM-DD N; /ai on; /ai off. Or: 今天3人上班 / 明天有2個員工'
        capacity_match=re.fullmatch(r'/capacity (\d{4}-\d{2}-\d{2}) (\d{1,3})',text)
        natural=re.fullmatch(r'(今天|明天)(?:有)?\s*(\d{1,3}|[一二兩两三四五六七八九十])\s*(?:個|个)?\s*(?:按摩師|按摩师|員工|员工|人)(?:上班)?',text)
        if natural:
            day=(datetime.now(ZoneInfo(config.timezone)).date()+timedelta(days=natural[1]=='明天')).isoformat()
            count=int(natural[2]) if natural[2].isdigit() else {'一':1,'二':2,'兩':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[natural[2]]
        elif capacity_match:
            day,count=capacity_match[1],int(capacity_match[2])
        else:
            day,count=None,None
        try:
            if day:
                calendar.set_capacity(day,count)
                reply=f'Saved capacity: {day} = {count}. Existing appointments were not cancelled.'
            elif text in {'/ai on','/ai off'}:
                calendar.setting('ai_enabled',False,text=='/ai on')
                reply='AI fallback '+('ON' if text=='/ai on' else 'OFF')
        except ValueError:
            reply='Invalid date or capacity (0–100).'
        # Telegram accepts a Bot API method directly in webhook response.
        return dict(method='sendMessage',chat_id=cfg['telegram_chat'],text=reply)

    @app.websocket('/media-stream')
    async def media(ws: WebSocket):
        public=cfg['public_url'].replace('https://','wss://',1)+'/media-stream'
        if not cfg['twilio_token'] or not RequestValidator(cfg['twilio_token']).validate(public,{},ws.headers.get('x-twilio-signature','')):
            await ws.close(code=1008); return
        await ws.accept()
        try:
            async with asyncio.timeout(10):
                while True:
                    event=await ws.receive_json()
                    if event.get('event')=='start': break
            start=event['start']
            value=calendar.consume_stream(start.get('customParameters',{}).get('session_token',''),start.get('callSid',''))
            if not value or not cfg['openai_key']:
                await ws.close(code=1008); return
            session=ToolSession(calendar,value['call_id'],value['phone'])
            await run_bridge(ws,session,cfg,stream_sid=start['streamSid'])
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.warning('Voice bridge ended with an error; inspect provider dashboards without exposing credentials')
        finally:
            with suppress(Exception): await ws.close()

    @app.websocket('/test-chat')
    async def test_chat(ws: WebSocket):
        origin=ws.headers.get('origin','')
        expected=cfg['public_url'] or f"{ws.url.scheme.replace('ws','http')}://{ws.url.netloc}"
        if origin and origin.rstrip('/')!=expected:
            await ws.close(code=1008); return
        await ws.accept()
        try:
            async with asyncio.timeout(10): auth=await ws.receive_json()
            if len(cfg['admin_token'])<24 or not hmac.compare_digest(str(auth.get('token','')).encode(),cfg['admin_token'].encode()):
                await ws.close(code=1008); return
            if not cfg['openai_key']:
                await ws.send_json({'type':'error','message':'OPENAI_API_KEY is not configured.'}); return
            with tempfile.TemporaryDirectory(prefix='appointment-chat-') as temp:
                session=ToolSession(Calendar(Path(temp)/'demo.sqlite3',config),secrets.token_hex(16),'+12025550123',demo=True)
                await run_bridge(ws,session,cfg)
        except WebSocketDisconnect:
            pass
        except Exception:
            with suppress(Exception): await ws.send_json({'type':'error','message':'Chat ended. Check model access and configuration.'})
        finally:
            with suppress(Exception): await ws.close()
    return app


app = create_app()
