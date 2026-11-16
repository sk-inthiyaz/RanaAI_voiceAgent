"""RANA R3 — real phone calls through Vobiz, using the same agent as the browser demo (bot.py).

How a call works:
  1. We ask Vobiz to call a number (POST /Account/{id}/Call/) with answer_url = PUBLIC_URL/answer/<secret>.
  2. When the person picks up, Vobiz fetches that answer_url; we reply with XML <Stream> pointing at our WebSocket.
  3. Vobiz streams the caller's audio (8 kHz μ-law) to /ws/<secret>; Pipecat runs STT -> LLM -> Bulbul voice
     and streams the reply audio back (playAudio). Barge-in sends clearAudio.
  4. When the call ends, Vobiz closes the WebSocket and posts to /hangup/<secret>.

Run:
  .venv\\Scripts\\python phone_server.py                      # start the server (port 8765)
  .venv\\Scripts\\python phone_server.py call 9876543210      # place a test call (server must be running)

Needs in .env: VOBIZ_AUTH_ID, VOBIZ_AUTH_TOKEN, VOBIZ_FROM_NUMBER, PUBLIC_URL (https tunnel to port 8765), CALL_SECRET.
"""

import asyncio
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)  # keep the console readable
import json
import time
import os
import re
import secrets
import sys
from xml.sax.saxutils import escape

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from loguru import logger
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat_vobiz import VobizFrameSerializer, parse_vobiz_start

import bot  # loads .env, the agent, and the UTF-8 console fix
from call_log import delete_old_logs, dials_today, log_dial, log_hangup, recent_calls, was_dialed

VOBIZ_API = os.getenv("VOBIZ_API_BASE", "https://api.vobiz.ai/api/v1").rstrip("/")
AUTH_ID = os.getenv("VOBIZ_AUTH_ID", "")
AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")
FROM_NUMBER = os.getenv("VOBIZ_FROM_NUMBER", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
SECRET = os.getenv("CALL_SECRET", "")          # only in Vobiz webhook URLs (answer / hangup / audio stream)
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")   # separate: /call and /calls. Empty = those web endpoints are OFF.
MAX_ACTIVE_CALLS = int(os.getenv("MAX_ACTIVE_CALLS", "5"))
MAX_DIALS_PER_DAY = int(os.getenv("MAX_DIALS_PER_DAY", "200"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
# Only Indian mobile numbers (91 + 10 digits starting 6-9): blocks international / premium-rate toll fraud.
ALLOWED_TO = re.compile(os.getenv("ALLOWED_TO_REGEX", r"^91[6-9]\d{9}$"))
PORT = int(os.getenv("PHONE_PORT", "8765"))
HOST = os.getenv("PHONE_HOST", "0.0.0.0")
PHONE_RATE = 8000  # Vobiz μ-law is 8 kHz

# ---- Logs -------------------------------------------------------------------------------------------------------
#   console                         INFO: one line per important step (readable while you watch)
#   logs/phone_<date>.log           DEBUG: everything Pipecat does (for diagnosing), kept 14 days
#   logs/errors_<date>.log          WARNING+: only problems, kept 30 days
#   logs/calls.jsonl + transcripts/ one record + one readable transcript per call (see call_log.py)
LOGS = bot.HERE / "logs"
logger.remove()
logger.add(sys.stderr, level=os.getenv("CONSOLE_LOG_LEVEL", "INFO"),
           format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}")
logger.add(str(LOGS / "phone_{time:YYYY-MM-DD}.log"), level="DEBUG", encoding="utf-8", rotation="00:00",
           retention="14 days", enqueue=True)
logger.add(str(LOGS / "errors_{time:YYYY-MM-DD}.log"), level="WARNING", encoding="utf-8", rotation="00:00",
           retention="30 days", enqueue=True)

STARTED_AT = time.time()
ACTIVE_CALLS: set[str] = set()

app = FastAPI(title="RANA R3 phone server")


@app.on_event("startup")
async def warm_up():
    import datetime

    logger.info(f"Agent code version {datetime.datetime.fromtimestamp(bot.CODE_VERSION):%d %b %H:%M} · "
                f"languages {', '.join(bot.CALL_LANGUAGES)} · max {bot.MAX_SENTENCES} sentences · instant ack {bot.INSTANT_ACK}")
    removed = delete_old_logs(LOG_RETENTION_DAYS)
    if removed:
        logger.info(f"Deleted {removed} transcript/log files older than {LOG_RETENTION_DAYS} days")
    if SECRET and ADMIN_SECRET and secrets.compare_digest(SECRET, ADMIN_SECRET):
        logger.warning("ADMIN_SECRET must differ from CALL_SECRET — admin web endpoints disabled")
    ok = await bot.prewarm_greeting(PHONE_RATE)
    logger.info(f"Greeting audio {'ready — calls will start speaking instantly' if ok else 'NOT cached (will be made per call)'}")


def _check(secret: str):
    if not SECRET or not secrets.compare_digest(secret, SECRET):
        raise HTTPException(status_code=404)


def _check_admin(secret: str):
    if (not ADMIN_SECRET or secrets.compare_digest(ADMIN_SECRET, SECRET or "\0")
            or not secrets.compare_digest(secret, ADMIN_SECRET)):
        raise HTTPException(status_code=404)


def _digits(number: str) -> str:
    """Indian 10-digit numbers get 91 in front; Vobiz takes E.164 digits."""
    d = re.sub(r"\D", "", number or "")
    return "91" + d if len(d) == 10 else d


@app.get("/")
async def health():
    missing = [k for k, v in {"VOBIZ_AUTH_ID": AUTH_ID, "VOBIZ_AUTH_TOKEN": AUTH_TOKEN,
                              "VOBIZ_FROM_NUMBER": FROM_NUMBER, "PUBLIC_URL": PUBLIC_URL,
                              "CALL_SECRET": SECRET}.items() if not v]
    import datetime

    return {"ok": not missing and not bot.code_is_stale(), "missing": missing,
            "restart_needed": bot.code_is_stale(),
            "code_version": datetime.datetime.fromtimestamp(bot.CODE_VERSION).strftime("%d %b %H:%M"),
            "uptime_min": round((time.time() - STARTED_AT) / 60, 1), "active_calls": len(ACTIVE_CALLS),
            "tts": f"Sarvam {bot.TTS_MODEL} / {bot.voice_for(bot.CALL_LANGUAGES[0])} @ pace {bot.TTS_PACE}",
            "stt": "Sarvam saaras:v4", "llm": f"{bot.LLM_PROVIDER} / {os.getenv('SARVAM_LLM_MODEL', 'sarvam-105b-conversations')}",
            "languages": bot.CALL_LANGUAGES, "max_sentences": bot.MAX_SENTENCES,
            "ack_rate": bot.ACK_RATE if bot.INSTANT_ACK else 0}


@app.get("/calls/{secret}")
async def calls(secret: str, n: int = 20):
    """The last n calls: delays, cost, how they ended. Full transcripts are in logs/transcripts/."""
    _check_admin(secret)
    return recent_calls(max(1, min(n, 200)))


@app.api_route("/answer/{secret}", methods=["GET", "POST"])
async def answer(secret: str, request: Request):
    """Vobiz asks what to do when the call is answered: stream the audio to our WebSocket."""
    _check(secret)
    # Inbound (a customer called our number) or outbound (we dialled)? Vobiz sends Direction; our dial records
    # are the backup. Form or JSON body, depending on the request.
    info: dict = dict(request.query_params)
    try:
        if "json" in request.headers.get("content-type", ""):
            info |= await request.json()
        elif request.method == "POST":
            info |= dict(await request.form())
    except Exception:
        pass
    vobiz_dir = str(info.get("Direction", "")).lower()
    outbound = vobiz_dir.startswith("out") or (not vobiz_dir and was_dialed(info.get("CallUUID")))
    direction = "out" if outbound else "in"
    logger.info(f"Call answered — {'OUTBOUND (we called)' if outbound else 'INBOUND (customer called us)'} · "
                f"from {info.get('From', '?')} to {info.get('To', '?')} · Vobiz Direction={vobiz_dir or 'n/a'}")
    ws_url = PUBLIC_URL.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/{SECRET}/{direction}"
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
        f'  <Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">'
        f"{escape(ws_url)}</Stream>\n</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.api_route("/hangup/{secret}", methods=["GET", "POST"])
async def hangup(secret: str, request: Request):
    _check(secret)
    form = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    keep = {k: form.get(k) for k in ("CallUUID", "To", "From", "Duration", "BillDuration", "HangupCause",
                                     "HangupCauseName", "HangupSource", "CallStatus") if form.get(k)}
    logger.info(f"Vobiz: call ended {keep}")
    log_hangup(keep)
    return {"ok": True}


@app.websocket("/ws/{secret}")
@app.websocket("/ws/{secret}/{direction}")
async def media(websocket: WebSocket, secret: str, direction: str = "out"):
    if not SECRET or not secrets.compare_digest(secret, SECRET):
        await websocket.close(code=1008)
        return
    if len(ACTIVE_CALLS) >= MAX_ACTIVE_CALLS:
        logger.warning(f"Refused a call stream: already {len(ACTIVE_CALLS)} active (MAX_ACTIVE_CALLS={MAX_ACTIVE_CALLS})")
        await websocket.close(code=1013)
        return
    await websocket.accept()
    start = await parse_vobiz_start(websocket)
    logger.info(f"Vobiz stream started: call {start['call_id']} ({start['encoding']} @ {start['sample_rate']} Hz)")

    serializer = VobizFrameSerializer(
        stream_id=start["stream_id"],
        call_id=start["call_id"],
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        params=VobizFrameSerializer.InputParams(
            encoding=start["encoding"],
            vobiz_sample_rate=start["sample_rate"],
            # Hang the call up via Vobiz's API when the agent ends it (needs credentials; local tests run without).
            auto_hang_up=bool(AUTH_ID and AUTH_TOKEN and start["call_id"]),
        ),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False,
                                      serializer=serializer),
    )
    call_id = start["call_id"] or start["stream_id"]
    ACTIVE_CALLS.add(call_id)
    try:
        await bot.run_agent(transport, phone_rate=start["sample_rate"], label=f"call {call_id[:8]}", call_id=call_id,
                            direction="inbound" if direction == "in" else "outbound")
    except Exception:
        logger.exception(f"Call {call_id} crashed")
        raise
    finally:
        ACTIVE_CALLS.discard(call_id)


async def place_call(to: str) -> dict:
    """Ask Vobiz to call `to` from our number; the answer URL connects it to the agent."""
    if not ALLOWED_TO.match(_digits(to)):
        logger.warning(f"Blocked dial to {_digits(to)}: not an allowed number (Indian mobiles only)")
        return {"status": 403, "body": "Number not allowed: only Indian mobile numbers (10 digits, starting 6-9)",
                "call_id": None}
    if dials_today() >= MAX_DIALS_PER_DAY:
        logger.warning(f"Blocked dial: daily limit of {MAX_DIALS_PER_DAY} calls reached")
        return {"status": 429, "body": f"Daily call limit ({MAX_DIALS_PER_DAY}) reached", "call_id": None}
    body = {
        "from": _digits(FROM_NUMBER),
        "to": _digits(to),
        "answer_url": f"{PUBLIC_URL}/answer/{SECRET}",
        "answer_method": "POST",
        "hangup_url": f"{PUBLIC_URL}/hangup/{SECRET}",
        "hangup_method": "POST",
    }
    headers = {"X-Auth-ID": AUTH_ID, "X-Auth-Token": AUTH_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{VOBIZ_API}/Account/{AUTH_ID}/Call/", json=body, headers=headers,
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            text = await r.text()
    call_id = None
    try:
        call_id = json.loads(text).get("request_uuid")
    except (ValueError, AttributeError):
        pass
    log_dial(call_id, _digits(to), r.status, text)
    return {"status": r.status, "body": text[:500], "call_id": call_id}


@app.post("/call/{secret}")
async def call(secret: str, request: Request):
    """POST {"to": "9876543210"} to place an outbound call."""
    _check_admin(secret)
    try:
        to = str((await request.json()).get("to", ""))
    except Exception:
        raise HTTPException(status_code=400, detail='Send JSON: {"to": "98XXXXXXXX"}')
    res = await place_call(to)
    logger.info(f"Call to {to}: Vobiz {res['status']} {res['body']}")
    return JSONResponse(res, status_code=200 if res["status"] < 300 else 502)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "call":
        missing = [k for k, v in {"VOBIZ_AUTH_ID": AUTH_ID, "VOBIZ_AUTH_TOKEN": AUTH_TOKEN,
                                  "VOBIZ_FROM_NUMBER": FROM_NUMBER, "PUBLIC_URL": PUBLIC_URL,
                                  "CALL_SECRET": SECRET}.items() if not v]
        if missing:
            sys.exit(f"Missing in .env: {', '.join(missing)}")
        res = asyncio.run(place_call(sys.argv[2]))
        print(f"Vobiz answered {res['status']}: {res['body']}")
        return
    if not SECRET:
        sys.exit("Set CALL_SECRET in .env first (any long random text).")
    logger.info(f"Phone server on http://localhost:{PORT}  ·  public URL: {PUBLIC_URL or '(PUBLIC_URL not set)'}")
    # Windows: the default (Proactor) loop delayed Sarvam's streamed voice by ~2 s. See winloop.py.
    from winloop import UVICORN_LOOP

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", loop=UVICORN_LOOP)


if __name__ == "__main__":
    main()
