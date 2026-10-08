import json
from html import escape as html_escape


VOICE_TEST_MODE_INSTRUCTIONS = (
    "BROWSER VOICE TEST MODE: This is a private Footprint staff browser session, "
    "not a phone call. Keep Karin's production dialogue and booking flow unchanged. "
    "Availability and booking tools run against a temporary copy of current Footprint "
    "data; test changes disappear when the session ends and do not notify staff. Do not "
    "mention browser test or sandbox details unless asked."
)


def build_voice_test_session_config(
    *,
    model: str,
    instructions: str,
    voice: str,
    speed: float,
    reasoning_effort: str,
    tools: list[dict],
) -> dict:
    """Build a browser WebRTC session with Footprint's production prompt and tools."""
    session = {
        "type": "realtime",
        "model": model,
        "instructions": f"{instructions}\n{VOICE_TEST_MODE_INSTRUCTIONS}",
        "output_modalities": ["audio"],
        "tools": tools,
        "tool_choice": "auto",
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.7,
                    "silence_duration_ms": 600,
                    "create_response": True,
                    "interrupt_response": True,
                },
            },
            "output": {"voice": voice, "speed": speed},
        },
    }
    if model.startswith("gpt-realtime-2"):
        session["reasoning"] = {"effort": reasoning_effort}
    return session


def render_voice_test_page(model_name: str, greeting: str) -> str:
    return (
        _VOICE_TEST_HTML.replace("__MODEL__", html_escape(model_name))
        .replace("__GREETING_JSON__", json.dumps(greeting))
    )


_VOICE_TEST_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Footprint Karin 語音測試</title>
  <style>
    :root { color-scheme:dark; --ink:#f7f1e8; --muted:#aca39a; --accent:#d8a66a; --panel:#171513; --line:#332e29; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(circle at 15% 5%,#35271c 0,transparent 32%),#0d0c0b; color:var(--ink); font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif; }
    main { width:min(900px,calc(100% - 28px)); margin:auto; padding:38px 0 48px; }
    .eyebrow { color:var(--accent); font-size:12px; letter-spacing:.18em; text-transform:uppercase; }
    h1 { margin:8px 0; font-family:Georgia,serif; font-weight:500; font-size:clamp(34px,7vw,62px); line-height:1; }
    .sub { color:var(--muted); max-width:680px; line-height:1.6; margin:0 0 24px; }
    .panel { background:color-mix(in srgb,var(--panel) 94%,transparent); border:1px solid var(--line); border-radius:22px; padding:22px; box-shadow:0 24px 70px #0008; }
    .status-row { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }
    .status { display:flex; align-items:center; gap:10px; font-weight:650; }
    .dot { width:10px; height:10px; border-radius:50%; background:#716b65; box-shadow:0 0 0 5px #ffffff0a; }
    .dot.live { background:#74d99f; box-shadow:0 0 0 5px #74d99f20,0 0 20px #74d99f55; }
    .model { color:var(--muted); font-size:13px; }
    .controls,.composer { display:flex; gap:10px; margin:22px 0; flex-wrap:wrap; }
    button,input { border:1px solid var(--line); border-radius:999px; padding:12px 19px; font:600 15px inherit; }
    button { appearance:none; cursor:pointer; color:var(--ink); background:#24211e; }
    button.primary { background:var(--accent); color:#21170d; border-color:var(--accent); }
    button:disabled,input:disabled { opacity:.45; cursor:not-allowed; }
    .composer input { flex:1; min-width:210px; color:var(--ink); background:#11100f; font-weight:400; }
    .notice { border-left:3px solid var(--accent); background:#dcae720d; padding:12px 14px; color:#d9d0c7; line-height:1.5; font-size:14px; }
    .transcript { margin-top:18px; min-height:260px; max-height:48vh; overflow:auto; border-top:1px solid var(--line); padding-top:16px; }
    .empty { color:#756f69; text-align:center; padding:72px 14px; }
    .turn { display:grid; grid-template-columns:72px 1fr; gap:12px; padding:10px 0; border-bottom:1px solid #ffffff0a; line-height:1.55; }
    .speaker { color:var(--accent); font-size:12px; font-weight:750; letter-spacing:.06em; text-transform:uppercase; padding-top:4px; }
    .turn.you .speaker { color:#8fb9e7; }.error { color:#ff9d91; white-space:pre-wrap; } audio { display:none; }
    @media(max-width:560px) { main{padding-top:24px}.panel{padding:17px}.turn{grid-template-columns:58px 1fr}button{flex:1} }
  </style>
</head>
<body><main>
  <div class="eyebrow">Footprint staff tool</div>
  <h1>Karin 語音測試</h1>
  <p class="sub">不用打電話，直接使用這台裝置的麥克風和線上 Karin 對話，也可以在下方輸入文字。適合測試回答、語氣、插話與預約流程。</p>
  <section class="panel">
    <div class="status-row">
      <div class="status"><span id="dot" class="dot"></span><span id="status">尚未連線</span></div>
      <div class="model">__MODEL__ · Footprint production prompt</div>
    </div>
    <div class="controls">
      <button id="start" class="primary">開始語音測試</button>
      <button id="mute" disabled>麥克風靜音</button>
      <button id="stop" disabled>結束</button>
    </div>
    <div class="notice"><strong>安全沙盒：</strong>使用目前 Footprint 班表的臨時副本。測試預約、待處理問題不會寫入正式資料庫，也不會傳送 LINE 或 Telegram 通知。</div>
    <div id="transcript" class="transcript"><div class="empty">連線後，Karin 會先打招呼。</div></div>
    <form id="composer" class="composer"><input id="message" placeholder="也可以輸入客人的話…" disabled><button id="send" disabled>傳送文字</button></form>
    <audio id="remote" autoplay></audio>
  </section>
</main>
<script>
const startButton=document.getElementById('start'),stopButton=document.getElementById('stop'),muteButton=document.getElementById('mute'),sendButton=document.getElementById('send'),messageInput=document.getElementById('message'),statusText=document.getElementById('status'),dot=document.getElementById('dot'),transcript=document.getElementById('transcript'),remoteAudio=document.getElementById('remote');
const greeting=__GREETING_JSON__;let pc=null,dc=null,micStream=null,sessionId='',muted=false;
function setStatus(text,live=false){statusText.textContent=text;dot.classList.toggle('live',live)}
function appendTurn(speaker,text,kind=''){if(!text)return;const empty=transcript.querySelector('.empty');if(empty)empty.remove();const row=document.createElement('div');row.className='turn '+kind;const who=document.createElement('div');who.className='speaker';who.textContent=speaker;const body=document.createElement('div');body.textContent=text;row.append(who,body);transcript.append(row);transcript.scrollTop=transcript.scrollHeight}
function appendError(message){appendTurn('Error',message,'error')}
async function handleTool(event){try{const response=await fetch('/test-chat/tool',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Footprint-Test-Session':sessionId},body:JSON.stringify({name:event.name,arguments:event.arguments||'{}'})});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'Tool request failed');dc.send(JSON.stringify({type:'conversation.item.create',item:{type:'function_call_output',call_id:event.call_id,output:JSON.stringify(payload.result)}}));dc.send(JSON.stringify({type:'response.create',response:{instructions:payload.response_instructions}}))}catch(error){appendError(error.message)}}
function handleEvent(message){const event=JSON.parse(message.data);if(event.type==='conversation.item.input_audio_transcription.completed')appendTurn('You',event.transcript,'you');else if(event.type==='response.output_audio_transcript.done')appendTurn('Karin',event.transcript);else if(event.type==='response.function_call_arguments.done')handleTool(event);else if(event.type==='error')appendError(event.error?.message||'Realtime error')}
async function start(){startButton.disabled=true;setStatus('正在取得麥克風…');try{pc=new RTCPeerConnection();pc.ontrack=(event)=>{remoteAudio.srcObject=event.streams[0]};pc.onconnectionstatechange=()=>{if(pc&&['failed','disconnected'].includes(pc.connectionState))setStatus('連線中斷')};micStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});pc.addTrack(micStream.getAudioTracks()[0],micStream);dc=pc.createDataChannel('oai-events');dc.addEventListener('message',handleEvent);dc.addEventListener('open',()=>{setStatus('測試中',true);stopButton.disabled=false;muteButton.disabled=false;messageInput.disabled=false;sendButton.disabled=false;dc.send(JSON.stringify({type:'response.create',response:{instructions:`Say exactly: "${greeting}"`}}))});const offer=await pc.createOffer();await pc.setLocalDescription(offer);const response=await fetch('/test-chat/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/sdp'},body:offer.sdp});const answer=await response.text();if(!response.ok)throw new Error(answer||'Unable to start session');sessionId=response.headers.get('X-Footprint-Test-Session')||'';if(!sessionId)throw new Error('Missing test session');await pc.setRemoteDescription({type:'answer',sdp:answer})}catch(error){appendError(error.message);await stop();setStatus('無法連線')}}
async function stop(){if(dc)dc.close();if(pc)pc.close();if(micStream)micStream.getTracks().forEach(track=>track.stop());if(sessionId){fetch('/test-chat/end',{method:'POST',credentials:'same-origin',keepalive:true,headers:{'X-Footprint-Test-Session':sessionId}}).catch(()=>{})}dc=null;pc=null;micStream=null;sessionId='';muted=false;startButton.disabled=false;stopButton.disabled=true;muteButton.disabled=true;messageInput.disabled=true;sendButton.disabled=true;muteButton.textContent='麥克風靜音';dot.classList.remove('live');if(statusText.textContent!=='無法連線')setStatus('已結束')}
function toggleMute(){if(!micStream)return;muted=!muted;micStream.getAudioTracks().forEach(track=>track.enabled=!muted);muteButton.textContent=muted?'取消靜音':'麥克風靜音';setStatus(muted?'已靜音':'測試中',!muted)}
document.getElementById('composer').addEventListener('submit',(event)=>{event.preventDefault();const text=messageInput.value.trim();if(!text||!dc||dc.readyState!=='open')return;appendTurn('You',text,'you');dc.send(JSON.stringify({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text}]}}));dc.send(JSON.stringify({type:'response.create'}));messageInput.value='' });
startButton.addEventListener('click',start);stopButton.addEventListener('click',stop);muteButton.addEventListener('click',toggleMute);window.addEventListener('beforeunload',stop);
</script></body></html>"""
