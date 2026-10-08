from datetime import datetime, timezone
from pathlib import Path
import json

from fastapi.testclient import TestClient
import httpx
import pytest
from twilio.request_validator import RequestValidator

from receptionist.booking import Calendar
from receptionist.main import create_app, settings_from_env
from receptionist.notifications import deliver_pending
from receptionist.realtime import session_update
from receptionist.tools import ToolSession

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def app(tmp_path,monkeypatch):
    monkeypatch.setattr(Calendar,'now',staticmethod(lambda:datetime(2030,1,7,8,tzinfo=timezone.utc)))
    # Never read real credentials from the caller's environment in tests.
    settings=dict(openai_key='',model='test-model',voice='marin',admin_token='a'*32,
        twilio_token='local-test-secret',public_url='https://example.com',human_numbers=[],human_sips=[],
        human_only={'+12025550124'},ai_enabled=True,ring_seconds=20,max_call_seconds=60,
        telegram_token='',telegram_chat='-12345',telegram_secret='b'*32,demo_enabled=True)
    return create_app(ROOT/'examples/generic.json',tmp_path,settings)


def signed(client,path,form):
    signature=RequestValidator('local-test-secret').compute_signature('https://example.com'+path,form)
    return client.post(path,data=form,headers={'x-twilio-signature':signature})


def test_no_admin_access_with_blank_or_incorrect_token(app):
    with TestClient(app) as c:
        assert c.get('/api/admin/state').status_code==401
        assert c.post('/api/admin/ai',json={'enabled':True}).status_code==401
        assert c.get('/api/admin/state',headers={'Authorization':'Bearer '+'a'*32}).status_code==200
        app.state.settings['admin_token']=''
        assert c.get('/api/admin/state',headers={'Authorization':'Bearer '}).status_code==401


def test_demo_cannot_book_in_real_database(app):
    with TestClient(app) as c:
        assert c.post('/api/demo/session',headers={'origin':'https://evil.example'}).status_code==403
        token=c.post('/api/demo/session').json()['token']
        h={'X-Demo-Session':token}
        p=c.post('/api/demo/tool',headers=h,json={'name':'prepare_booking','arguments':{
            'day':'2030-01-07','time':'10:00','service_id':'consultation','duration':60,'name':'Demo'}}).json()
        result=c.post('/api/demo/tool',headers=h,json={'name':'confirm_booking','arguments':{'confirmation_token':p['confirmation_token'],'confirmed':True}}).json()
        assert result['confirmed'] and result['demo']
        with app.state.calendar.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_twilio_rejects_unsigned_and_disables_missing_ai_key(app):
    with TestClient(app) as c:
        assert c.post('/incoming-call',data={'From':'+12025550123'}).status_code==403
        r=signed(c,'/incoming-call',{'From':'+12025550123','CallSid':'CA'+'0'*32})
        assert '<Hangup' in r.text and '<Stream' not in r.text


def test_parallel_human_ring_and_exclusions(app):
    app.state.settings.update(human_numbers=['+12025550125'],human_sips=['sip:desk@example.com'],openai_key='test-key')
    with TestClient(app) as c:
        r=signed(c,'/incoming-call',{'From':'+12025550123','CallSid':'CA'+'0'*32})
        assert '<Number>' in r.text and '<Sip>' in r.text and 'timeout="20"' in r.text
        r=signed(c,'/human-dial-result',{'From':'+12025550124','CallSid':'CA'+'0'*32,'DialCallStatus':'no-answer'})
        assert '<Stream' not in r.text
        r=signed(c,'/human-dial-result',{'From':'+12025550123','CallSid':'CA'+'0'*32,'DialCallStatus':'no-answer'})
        assert 'session_token' in r.text and '<Stream' in r.text
        r=signed(c,'/human-dial-result',{'From':'+12025550123','DialCallStatus':'completed'})
        assert '<Stream' not in r.text


def test_telegram_auth_and_capacity_save(app):
    with TestClient(app) as c:
        payload={'message':{'chat':{'id':-12345},'text':'今天有3個員工'}}
        assert c.post('/telegram/webhook',json=payload).status_code==403
        h={'X-Telegram-Bot-Api-Secret-Token':'b'*32}
        payload['message']['text']='/capacity 2030-01-07 3'
        r=c.post('/telegram/webhook',json=payload,headers=h)
        assert r.status_code==200 and '= 3' in r.json()['text']
        with app.state.calendar.connect() as db:
            assert db.execute('SELECT capacity FROM overrides').fetchone()[0]==3
        payload['message']['chat']['id']=999
        assert c.post('/telegram/webhook',json=payload,headers=h).status_code==403


def test_realtime_session_uses_profile_and_protected_tools(app):
    session=ToolSession(app.state.calendar,'test','+12025550123')
    value=session_update(session,'marin',True)['session']
    assert value['audio']['input']['format']['type']=='audio/pcmu'
    assert app.state.calendar.business.name in value['instructions']
    names={t['name'] for t in value['tools']}
    assert 'confirm_booking' in names and 'cancel' not in names


def test_ai_toggle_is_persistent_and_type_checked(app):
    with TestClient(app) as c:
        h={'Authorization':'Bearer '+'a'*32}
        assert c.post('/api/admin/ai',headers=h,json={'enabled':'false'}).status_code==400
        assert c.post('/api/admin/ai',headers=h,json={'enabled':False}).json()=={'enabled':False}
        assert c.get('/api/admin/state',headers=h).json()['ai_enabled'] is False


def test_notification_failures_remain_pending(app,monkeypatch):
    import asyncio
    app.state.calendar.callback('test','+12025550123','Agent please')
    async def fail(self,*a,**kw):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(httpx.AsyncClient,'post',fail)
    assert asyncio.run(deliver_pending(app.state.calendar,'test','test'))==0
    with app.state.calendar.connect() as db:
        row=db.execute('SELECT sent,attempts FROM outbox').fetchone()
        assert row['sent']==0 and row['attempts']==1


def test_invalid_websocket_cannot_connect_to_provider(app):
    from starlette.websockets import WebSocketDisconnect
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/media-stream'): pass
        with c.websocket_connect('/test-chat') as ws:
            ws.send_json({'token':'wrong'})
            with pytest.raises(WebSocketDisconnect): ws.receive_json()


def test_demo_page_and_health(app):
    with TestClient(app) as c:
        assert c.get('/health').json()['status']=='ok'
        assert 'Open Appointment Receptionist' in c.get('/').text
        assert c.get('/api/business').json()['services']
