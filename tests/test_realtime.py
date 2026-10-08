"""Transport contract tests; never contact an AI or phone provider."""
import asyncio
import json
from pathlib import Path

from receptionist.booking import Calendar
from receptionist.config import load_business
from receptionist.realtime import run_bridge
from receptionist.tools import ToolSession


def test_batched_tools_complete_before_next_response(tmp_path,monkeypatch):
    sent=[]
    received=[]
    class Upstream:
        def __init__(self): self.pass_number=0
        async def send(self,data): sent.append(json.loads(data))
        def __aiter__(self):
            self.pass_number+=1
            async def iterator():
                if self.pass_number==1:
                    yield json.dumps({'type':'session.updated'})
                else:
                    yield json.dumps({'type':'response.done','response':{'output':[
                        {'type':'function_call','call_id':'a','name':'business_information','arguments':'{}'},
                        {'type':'function_call','call_id':'b','name':'business_information','arguments':'{}'}]}})
                    yield json.dumps({'type':'response.output_text.delta','delta':'Example business'})
                    yield json.dumps({'type':'response.done','response':{'output':[]}})
            return iterator()
    upstream=Upstream()
    class Connection:
        async def __aenter__(self):return upstream
        async def __aexit__(self,*args):pass
    class Client:
        async def send_json(self,data):received.append(data)
        async def receive_json(self):await asyncio.Event().wait()
    monkeypatch.setattr('receptionist.realtime.websockets.connect',lambda *a,**kw:Connection())
    config=load_business(Path(__file__).resolve().parents[1]/'examples/generic.json')
    session=ToolSession(Calendar(tmp_path/'test.db',config),'test',demo=True)
    asyncio.run(run_bridge(Client(),session,dict(model='test',openai_key='test',voice='marin',max_call_seconds=1)))
    kinds=[x['type'] for x in sent]
    assert kinds==['session.update','conversation.item.create','conversation.item.create','response.create']
    assert [x['type'] for x in received]==['ready','delta','done']


def test_voice_interruption_clears_phone_audio(tmp_path,monkeypatch):
    sent=[];received=[]
    class Upstream:
        def __init__(self):self.pass_number=0
        async def send(self,data):sent.append(json.loads(data))
        def __aiter__(self):
            self.pass_number+=1
            async def iterator():
                if self.pass_number==1:yield json.dumps({'type':'session.updated'})
                else:
                    yield json.dumps({'type':'response.output_audio.delta','item_id':'voice-1','delta':'AA=='})
                    yield json.dumps({'type':'input_audio_buffer.speech_started'})
            return iterator()
    upstream=Upstream()
    class Connection:
        async def __aenter__(self):return upstream
        async def __aexit__(self,*args):pass
    class Client:
        async def send_json(self,value):received.append(value)
        async def receive_json(self):await asyncio.Event().wait()
    monkeypatch.setattr('receptionist.realtime.websockets.connect',lambda *a,**kw:Connection())
    config=load_business(Path(__file__).resolve().parents[1]/'examples/generic.json')
    session=ToolSession(Calendar(tmp_path/'test.db',config),'test')
    asyncio.run(run_bridge(Client(),session,dict(model='test',openai_key='test',voice='marin',max_call_seconds=1),stream_sid='test-stream'))
    assert [x['event'] for x in received]==['media','clear']
    assert sent[-1]['type']=='conversation.item.truncate'
    assert sent[-1]['item_id']=='voice-1'
