"""Optional OpenAI Realtime transport. No credentials or network calls at import."""
import asyncio
import json
from contextlib import suppress
from urllib.parse import urlencode

import websockets

from .tools import TOOLS, instructions


def session_update(session, voice, audio):
    result = dict(type='realtime', instructions=instructions(session.calendar, demo=session.demo),
                  tools=TOOLS, tool_choice='auto', output_modalities=['audio' if audio else 'text'])
    if audio:
        result['audio'] = dict(
            input=dict(format={'type':'audio/pcmu'}, noise_reduction={'type':'near_field'},
                       turn_detection=dict(type='server_vad', silence_duration_ms=600, threshold=0.6,
                                           create_response=True, interrupt_response=True)),
            output=dict(format={'type':'audio/pcmu'}, voice=voice))
    return dict(type='session.update', session=result)


async def run_bridge(client, session, settings, *, stream_sid=None):
    audio = stream_sid is not None
    url = 'wss://api.openai.com/v1/realtime?' + urlencode({'model':settings['model']})
    async with websockets.connect(url, additional_headers={'Authorization':'Bearer '+settings['openai_key']},
                                  open_timeout=15, max_size=2**20) as upstream:
        await upstream.send(json.dumps(session_update(session, settings['voice'], audio)))
        # Wait for acceptance before greeting or forwarding customer audio.
        async with asyncio.timeout(15):
            async for raw in upstream:
                event = json.loads(raw)
                if event.get('type') == 'error':
                    raise RuntimeError('Realtime session rejected')
                if event.get('type') == 'session.updated':
                    break
        if audio:
            greeting = f"Hello, this is {session.calendar.business.name}'s AI receptionist. How can I help you today?"
            await upstream.send(json.dumps(dict(type='response.create', response={'instructions':'Say exactly: '+greeting})))
        else:
            await client.send_json({'type':'ready'})
        latest_timestamp = 0
        response_start = None
        last_item = None
        async def receive():
            nonlocal latest_timestamp
            while True:
                message = await client.receive_json()
                if audio:
                    if message.get('event') == 'stop':
                        return
                    if message.get('event') == 'media':
                        latest_timestamp = int(message['media'].get('timestamp', 0))
                        await upstream.send(json.dumps({'type':'input_audio_buffer.append', 'audio':message['media']['payload']}))
                else:
                    text = str(message.get('text', '')).strip()
                    if not text or len(text) > 2000:
                        await client.send_json({'type':'error', 'message':'Use 1–2000 characters.'})
                        continue
                    await upstream.send(json.dumps({'type':'conversation.item.create','item':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}}))
                    await upstream.send(json.dumps({'type':'response.create'}))
        async def send():
            nonlocal response_start, last_item
            async for raw in upstream:
                event = json.loads(raw)
                kind = event.get('type')
                if kind == 'response.output_audio.delta' and audio:
                    if event.get('item_id') != last_item:
                        last_item, response_start = event.get('item_id'), latest_timestamp
                    await client.send_json({'event':'media','streamSid':stream_sid,'media':{'payload':event['delta']}})
                elif kind == 'input_audio_buffer.speech_started' and audio:
                    await client.send_json({'event':'clear','streamSid':stream_sid})
                    if last_item is not None and response_start is not None:
                        await upstream.send(json.dumps({'type':'conversation.item.truncate','item_id':last_item,'content_index':0,'audio_end_ms':max(0,latest_timestamp-response_start)}))
                        last_item, response_start = None, None
                elif kind == 'response.output_text.delta' and not audio:
                    await client.send_json({'type':'delta','text':event['delta']})
                elif kind == 'response.done':
                    calls = [item for item in event.get('response', {}).get('output', []) if item.get('type') == 'function_call']
                    for call in calls:
                        result = await asyncio.to_thread(session.event_result, call)
                        await upstream.send(json.dumps({'type':'conversation.item.create','item':{
                            'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(result)}}))
                    if calls:
                        await upstream.send(json.dumps({'type':'response.create'}))
                    elif not audio:
                        await client.send_json({'type':'done'})
                elif kind == 'error':
                    # Never echo upstream errors, headers or caller transcripts to logs.
                    raise RuntimeError('Realtime provider error')
        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        try:
            done, _ = await asyncio.wait(tasks, timeout=settings['max_call_seconds'], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
