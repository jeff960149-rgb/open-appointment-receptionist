from html import escape as html_escape


VOICE_TEST_MODE_INSTRUCTIONS = (
    "BROWSER VOICE TEST MODE: This is a private staff browser session, not a phone "
    "call. Keep the same Kiki dialogue and booking flow. Business and availability "
    "tools use live data. A confirmed appointment is real: it occupies a local slot "
    "and notifies staff using the server-configured test phone. The server handles "
    "the configured Smiley write. Do not ask for a phone number. Unanswered-question saves "
    "remain simulated. Do not mention browser test details unless asked."
)


def build_voice_test_session_config(
    *,
    model: str,
    instructions: str,
    voice: str,
    speed: float,
    tools: list[dict],
) -> dict:
    """Build a browser-friendly Realtime session using the production prompt/tools."""
    return {
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
                    "silence_duration_ms": 1200,
                    "create_response": True,
                    "interrupt_response": True,
                },
            },
            "output": {"voice": voice, "speed": speed},
        },
    }


def render_voice_test_page(
    model_name: str, *, customer_phone: str = "TEST", smiley_write_enabled: bool = False
) -> str:
    model = html_escape(model_name)
    mode = (
        "預約將嘗試自動寫入 Smiley，並設定不發送簡訊。"
        if smiley_write_enabled else "目前由員工手動登錄 Smiley。"
    )
    return (_VOICE_TEST_HTML.replace("__MODEL__", model)
            .replace("__TEST_PHONE__", html_escape(customer_phone))
            .replace("__SMILEY_MODE__", mode))


_VOICE_TEST_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Kspa Kiki 語音測試</title>
  <style>
    :root { color-scheme: dark; --ink:#f6f1e8; --muted:#aaa29a; --accent:#dcae72; --panel:#171614; --line:#302c28; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(circle at 15% 5%,#34271d 0,transparent 32%),#0d0c0b; color:var(--ink); font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif; }
    main { width:min(900px,calc(100% - 28px)); margin:auto; padding:38px 0 48px; }
    .eyebrow { color:var(--accent); font-size:12px; letter-spacing:.18em; text-transform:uppercase; }
    h1 { margin:8px 0 8px; font-family:Georgia,serif; font-weight:500; font-size:clamp(34px,7vw,62px); line-height:1; }
    .sub { color:var(--muted); max-width:650px; line-height:1.6; margin:0 0 24px; }
    .panel { background:color-mix(in srgb,var(--panel) 94%,transparent); border:1px solid var(--line); border-radius:22px; padding:22px; box-shadow:0 24px 70px #0008; }
    .status-row { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }
    .status { display:flex; align-items:center; gap:10px; font-weight:650; }
    .dot { width:10px; height:10px; border-radius:50%; background:#716b65; box-shadow:0 0 0 5px #ffffff0a; }
    .dot.live { background:#74d99f; box-shadow:0 0 0 5px #74d99f20,0 0 20px #74d99f55; }
    .model { color:var(--muted); font-size:13px; }
    .controls { display:flex; gap:10px; margin:22px 0; flex-wrap:wrap; }
    button { appearance:none; border:1px solid var(--line); border-radius:999px; padding:12px 19px; font:600 15px inherit; cursor:pointer; color:var(--ink); background:#24211e; }
    button.primary { background:var(--accent); color:#21170d; border-color:var(--accent); }
    button:disabled { opacity:.45; cursor:not-allowed; }
    .notice { border-left:3px solid var(--accent); background:#dcae720d; padding:12px 14px; color:#d9d0c7; line-height:1.5; font-size:14px; }
    .transcript { margin-top:18px; min-height:260px; max-height:48vh; overflow:auto; border-top:1px solid var(--line); padding-top:16px; }
    .empty { color:#756f69; text-align:center; padding:72px 14px; }
    .turn { display:grid; grid-template-columns:72px 1fr; gap:12px; padding:10px 0; border-bottom:1px solid #ffffff0a; line-height:1.55; }
    .speaker { color:var(--accent); font-size:12px; font-weight:750; letter-spacing:.06em; text-transform:uppercase; padding-top:4px; }
    .turn.you .speaker { color:#8fb9e7; }
    .error { color:#ff9d91; white-space:pre-wrap; }
    audio { display:none; }
    @media(max-width:560px) { main{padding-top:24px}.panel{padding:17px}.turn{grid-template-columns:58px 1fr}button{flex:1} }
  </style>
</head>
<body>
<main>
  <div class="eyebrow">K Spa staff tool</div>
  <h1>Kiki 語音測試</h1>
  <p class="sub">不用電話，直接用這台裝置的麥克風與線上 Kiki 對話。適合測試回答、語氣、插話與預約流程。</p>
  <p><a href="/phone-test" style="color:var(--accent)">用 Footprint 號碼撥給 Kspa，測試正式電話流程 →</a></p>
  <section class="panel">
    <div class="status-row">
      <div class="status"><span id="dot" class="dot"></span><span id="status">尚未連線</span></div>
      <div class="model">__MODEL__ · production prompt</div>
    </div>
    <div class="controls">
      <button id="start" class="primary">開始語音測試</button>
      <button id="mute" disabled>麥克風靜音</button>
      <button id="stop" disabled>結束</button>
    </div>
    <div class="notice"><strong>真實預約測試：</strong>預約電話：<strong>__TEST_PHONE__</strong>。完成預約會占用實際名額並通知 Telegram。__SMILEY_MODE__ 測試後請取消本地與 Smiley 預約。回電問題仍不會儲存。</div>
    <div id="transcript" class="transcript"><div class="empty">連線後，Kiki 會先打招呼。</div></div>
    <audio id="remote" autoplay></audio>
  </section>
</main>
<script>
const startButton=document.getElementById('start');
const stopButton=document.getElementById('stop');
const muteButton=document.getElementById('mute');
const statusText=document.getElementById('status');
const dot=document.getElementById('dot');
const transcript=document.getElementById('transcript');
const remoteAudio=document.getElementById('remote');
let pc=null,dc=null,micStream=null,muted=false;

function setStatus(text,live=false){statusText.textContent=text;dot.classList.toggle('live',live)}
function appendTurn(speaker,text,kind=''){
  if(!text)return;
  const empty=transcript.querySelector('.empty');if(empty)empty.remove();
  const row=document.createElement('div');row.className='turn '+kind;
  const who=document.createElement('div');who.className='speaker';who.textContent=speaker;
  const body=document.createElement('div');body.textContent=text;
  row.append(who,body);transcript.append(row);transcript.scrollTop=transcript.scrollHeight;
}
function appendError(message){appendTurn('Error',message,'error')}

async function handleTool(event){
  try{
    const response=await fetch('/voice-test/tool',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:event.name,arguments:event.arguments||'{}'})});
    const payload=await response.json();
    if(!response.ok)throw new Error(payload.detail||'Tool request failed');
    if(event.name==='save_unverified_appointment_request' && payload.result?.saved){
      const result=payload.result;
      appendTurn('預約結果',result.smiley_written?'已建立本地預約並寫入 Smiley。':result.smiley_review_required?'本地預約已建立；Smiley 寫入未確認，請先查看 Smiley，避免重複補登。':result.status==='confirmed'?'已建立本地預約；尚未寫入 Smiley。':'預約已記錄，等待員工確認。');
    }
    dc.send(JSON.stringify({type:'conversation.item.create',item:{type:'function_call_output',call_id:event.call_id,output:JSON.stringify(payload.result)}}));
    dc.send(JSON.stringify({type:'response.create',response:{instructions:payload.response_instructions}}));
  }catch(error){appendError(error.message)}
}
function handleEvent(message){
  const event=JSON.parse(message.data);
  if(event.type==='conversation.item.input_audio_transcription.completed')appendTurn('You',event.transcript,'you');
  else if(event.type==='response.output_audio_transcript.done')appendTurn('Kiki',event.transcript);
  else if(event.type==='response.function_call_arguments.done')handleTool(event);
  else if(event.type==='error')appendError(event.error?.message||'Realtime error');
}

async function start(){
  startButton.disabled=true;setStatus('正在取得麥克風…');
  try{
    pc=new RTCPeerConnection();
    pc.ontrack=(event)=>{remoteAudio.srcObject=event.streams[0]};
    pc.onconnectionstatechange=()=>{if(pc&&['failed','disconnected'].includes(pc.connectionState))setStatus('連線中斷')};
    micStream=await navigator.mediaDevices.getUserMedia({audio:true});
    pc.addTrack(micStream.getAudioTracks()[0],micStream);
    dc=pc.createDataChannel('oai-events');
    dc.addEventListener('message',handleEvent);
    dc.addEventListener('open',()=>{
      setStatus('測試中',true);stopButton.disabled=false;muteButton.disabled=false;
      dc.send(JSON.stringify({type:'response.create',response:{instructions:'Say exactly: "Hello, here\'s Kspa, how can I help you today?"'}}));
    });
    const offer=await pc.createOffer();await pc.setLocalDescription(offer);
    const response=await fetch('/voice-test/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/sdp'},body:offer.sdp});
    const answer=await response.text();if(!response.ok)throw new Error(answer||'Unable to start session');
    await pc.setRemoteDescription({type:'answer',sdp:answer});
  }catch(error){appendError(error.message);stop();setStatus('無法連線')}
}
function stop(){
  if(dc)dc.close();if(pc)pc.close();if(micStream)micStream.getTracks().forEach(track=>track.stop());
  dc=null;pc=null;micStream=null;muted=false;startButton.disabled=false;stopButton.disabled=true;muteButton.disabled=true;muteButton.textContent='麥克風靜音';dot.classList.remove('live');
  if(statusText.textContent!=='無法連線')setStatus('已結束');
}
function toggleMute(){
  if(!micStream)return;muted=!muted;micStream.getAudioTracks().forEach(track=>track.enabled=!muted);muteButton.textContent=muted?'取消靜音':'麥克風靜音';setStatus(muted?'已靜音':'測試中',!muted);
}
startButton.addEventListener('click',start);stopButton.addEventListener('click',stop);muteButton.addEventListener('click',toggleMute);window.addEventListener('beforeunload',stop);
</script>
</body></html>"""
