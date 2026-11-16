# RANA R3 voice agent — demo

Method 2 pipeline: **Sarvam speech-to-text → LLM (Sarvam or Groq) → Sarvam Bulbul v3 voice**, run by Pipecat.
This demo runs in the browser on your laptop. No phone line, Vobiz or server is needed yet.

## Run the demo

1. Copy `.env.example` to `.env` and paste your Sarvam API key (the `sk_...` key from dashboard.sarvam.ai → API Keys).
2. In a terminal, from this folder:
   ```
   .venv\Scripts\python bot.py
   ```
   First time on a new machine: `uv venv --python 3.11 .venv` then
   `uv pip install --python .venv "pipecat-ai[sarvam,silero,webrtc,groq,runner]" python-dotenv`
3. Open **http://localhost:7860** in Chrome, click **Connect**, allow the microphone, and talk.
4. When you disconnect, the terminal prints a **CALL COST ESTIMATE** line (₹ per minute for that call).

## Real phone calls (Vobiz)

Same agent, over a real phone line. Outbound only: this does not change any number's settings, so R1 keeps working.

1. In `.env`, fill in `VOBIZ_AUTH_ID`, `VOBIZ_AUTH_TOKEN` (Vobiz console, or the same values as in Vercel) and
   `VOBIZ_FROM_NUMBER` (a number on that Vobiz account). `CALL_SECRET` is already filled in.
2. Give your laptop a public https address. Install cloudflared once (`winget install Cloudflare.cloudflared`), then in a terminal:
   ```
   cloudflared tunnel --url http://localhost:8765
   ```
   Copy the `https://….trycloudflare.com` address it prints into `PUBLIC_URL` in `.env`. It changes every time you restart cloudflared.
3. Start the phone server (second terminal): `.venv\Scripts\python phone_server.py`
   Check http://localhost:8765 shows `"ok": true`.
4. Call your phone (third terminal): `.venv\Scripts\python phone_server.py call 98XXXXXXXX`
5. Answer it and talk to Priya. When you hang up, the server terminal prints the CALL COST ESTIMATE, including the phone line.

**Restart the phone server after ANY change** (code, prompt-independent settings in `.env`): Python only loads code at
start. http://localhost:8765 shows `"restart_needed": true` and every call logs a warning if the code is newer than the server.

Each call logs `REPLY DELAY` lines (how long the caller waited) and a cost line to `logs/phone_<date>.log`.

## Things to change for the demo

| What | Where |
|---|---|
| What the agent knows and how it talks | `agent_prompt.txt` (paste a real RANA agent script) |
| First line it says | `GREETING` in `.env` |
| Voice (priya, shubh, kavya, rahul…) | `TTS_VOICE` in `.env` |
| Voice language (hi-IN, en-IN, ta-IN, te-IN…) | `TTS_LANGUAGE` in `.env` |
| Cheaper LLM | `LLM_PROVIDER=groq` + `GROQ_API_KEY` in `.env` |
| Languages allowed on a call (first = start) | `CALL_LANGUAGES=hi-IN,en-IN,te-IN` |
| Longest reply | `MAX_SENTENCES=2` (enforced in code) |
| Instant "जी।" while the reply is prepared | `INSTANT_ACK=true` |
| Max silence before answering when unsure | `TURN_MAX_SILENCE=1.0` |

## Cost per minute (estimate)

| Part | ₹/min |
|---|---|
| Speech-to-text (Sarvam) | 0.50 |
| Voice: Bulbul v4 Flash (v3) | ~1.00 (~1.50) |
| LLM: Sarvam 105B conversations / Groq Llama 8B | ~0.30 / ~0.10 |
| Phone line (Vobiz, later) | ~0.50 |
| **Total on a phone call, v4** | **~₹2.1–2.3** |

## Sounding human (from Sarvam's v4 guide)

- Hindi words in Hindi script, English words in English: "आपका order confirm हो गया है", not "Aapka order confirm ho gaya hai". Romanised Hindi lowers voice quality.
- Short sentences, normal punctuation, few "…". Pace 1.0 for conversation.
- Use the browser's **Metrics** tab to see how long each step takes per reply.
