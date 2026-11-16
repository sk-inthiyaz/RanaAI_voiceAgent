"""RANA R3 voice agent — demo (Method 2: Pipecat + Sarvam STT + LLM + Sarvam Bulbul v3).

Talk to it from the browser:   .venv\\Scripts\\python bot.py   ->  open http://localhost:7860
Prompt: edit agent_prompt.txt (paste any real RANA agent script there).
Keys and choices: .env (copy .env.example).
"""

import os
import random
import re
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252, which crashes on Pipecat's banner and on ₹ / Hindi text in the logs.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
import aiohttp
from pipecat.turns.user_mute import MuteUntilFirstBotCompleteUserMuteStrategy
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.empty_user_turn import EmptyUserTurnConfig
from pipecat.services.tts_service import TextAggregationMode

from pipecat.services.groq.llm import GroqLLMService  # imported up front: ~1.4 s otherwise, on the first call
from pipecat.services.sarvam.llm import SarvamLLMService

from sarvam_stream_tts import SarvamStreamTTSService, build_payload, is_cached, prewarm
from call_log import CallLog
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.stt_service import STTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams

HERE = Path(__file__).parent
load_dotenv(HERE / ".env", override=True)

# Python only loads code when it starts: a server left running keeps the OLD code (this happened on 7 Nov — a test
# call ran without the speed fixes). Every call checks this and warns loudly if a restart is needed.
CODE_FILES = [HERE / "bot.py", HERE / "sarvam_stream_tts.py", HERE / "phone_server.py", HERE / "winloop.py", HERE / "call_log.py"]
CODE_VERSION = max(f.stat().st_mtime for f in CODE_FILES if f.exists())


def code_is_stale() -> bool:
    return max(f.stat().st_mtime for f in CODE_FILES if f.exists()) > CODE_VERSION + 1

# ---- Settings (all from .env, with demo defaults) -------------------------------------------
SARVAM_KEY = os.getenv("SARVAM_API_KEY", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "sarvam").lower()  # sarvam | groq
TTS_MODEL = os.getenv("TTS_MODEL", "bulbul:v4-flash")       # bulbul:v4-flash | bulbul:v3
IS_V4 = TTS_MODEL.startswith("bulbul:v4")
VOICE = os.getenv("TTS_VOICE", "aparna_hi_customer" if IS_V4 else "priya")
TTS_LANG = os.getenv("TTS_LANGUAGE", "hi-IN")                # language the voice starts in
TTS_PACE = float(os.getenv("TTS_PACE", "1.0"))               # Sarvam: 1.0 for conversational agents
TTS_PITCH = float(os.getenv("TTS_PITCH", "0.0"))             # v4 only, -0.5..0.5
TTS_LOUDNESS = float(os.getenv("TTS_LOUDNESS", "1.0"))       # v4 only, 0.1..2.5
TTS_TEMPERATURE = float(os.getenv("TTS_TEMPERATURE", "0.6")) # v3 only

# v4: when the caller switches language, hand over to a native voice of that language (a v4 voice is
# tagged with one language and sounds best in it). Set TTS_NATIVE_VOICES=false to keep one voice all call.
NATIVE_VOICES = IS_V4 and os.getenv("TTS_NATIVE_VOICES", "true").lower() == "true"
VOICE_FOR_LANG = {
    "hi-IN": VOICE,
    "en-IN": os.getenv("TTS_VOICE_EN", VOICE),
    "te-IN": os.getenv("TTS_VOICE_TE", "kavitha_te_conversation"),
    "kn-IN": os.getenv("TTS_VOICE_KN", "chaitra_kn_conversation"),
    "mr-IN": os.getenv("TTS_VOICE_MR", "ishita_mr_conversational"),
    "bn-IN": os.getenv("TTS_VOICE_BN", "roopa_bn_conversational"),
    "gu-IN": os.getenv("TTS_VOICE_GU", "pooja_gu_conversational"),
    # Tamil, Punjabi, Malayalam, Odia: no female conversational v4 voice yet -> the main voice speaks them.
}

# Languages this call may use. Phone speech-to-text mislabels short phrases ("hello" -> Tamil, "हॅलो कोण?" -> Marathi),
# so without a fixed list the agent jumped into Malayalam/Tamil/Marathi on a Telugu caller. First one = start language.
LANG_NAMES = {"hi-IN": "Hindi", "en-IN": "English", "te-IN": "Telugu", "ta-IN": "Tamil", "kn-IN": "Kannada",
              "ml-IN": "Malayalam", "mr-IN": "Marathi", "bn-IN": "Bengali", "gu-IN": "Gujarati", "pa-IN": "Punjabi",
              "or-IN": "Odia"}
CALL_LANGUAGES = [l.strip() for l in os.getenv("CALL_LANGUAGES", "hi-IN,en-IN,te-IN").split(",") if l.strip() in LANG_NAMES]
CALL_LANGUAGES = CALL_LANGUAGES or ["hi-IN", "en-IN"]

# Longest reply allowed, in sentences — enforced in code (the LLM ignores "keep it short" in the prompt sometimes).
MAX_SENTENCES = int(os.getenv("MAX_SENTENCES", "2"))

# Instant acknowledgement: the moment the caller finishes, play a tiny pre-made "जी।" / "సరే." while the real reply is
# prepared — the caller hears a response in well under a second instead of 2 s of silence.
INSTANT_ACK = os.getenv("INSTANT_ACK", "true").lower() == "true"
ACK_RATE = float(os.getenv("ACK_RATE", "0.4"))  # share of turns that get one (never two turns in a row)
ACKS = {
    "hi-IN": ["जी।", "अच्छा।", "हाँ जी।", "ओके।"],
    "en-IN": ["Okay.", "Right.", "I see.", "Sure."],
    "te-IN": ["సరే.", "అవునా.", "ఓకే.", "అలాగా."],
    "ta-IN": ["சரி.", "ஓகே."], "kn-IN": ["ಸರಿ.", "ಓಕೆ."], "ml-IN": ["ശരി.", "ഓക്കേ."], "mr-IN": ["बरं.", "हो."],
    "bn-IN": ["আচ্ছা.", "ঠিক আছে."], "gu-IN": ["સારું.", "હા."], "pa-IN": ["ਠੀਕ ਹੈ.", "ਅੱਛਾ."], "or-IN": ["ଠିକ୍ ଅଛି."],
}

GREETING = os.getenv(
    "GREETING",
    "నమస్కారం అండి! నేను Priya, RANA AI నుంచి. ఒక్క రెండు నిమిషాలు మాట్లాడొచ్చా?",
)
# When a customer calls OUR number (inbound), Priya answers like a receptionist instead.
INBOUND_GREETING = os.getenv(
    "INBOUND_GREETING",
    "నమస్కారం, RANA AI, నేను Priya. చెప్పండి అండి?",
)
CALL_CONTEXT = {
    "outbound": "You are on a PHONE CALL with a business owner or manager who enquired about RANA AI or agreed to be contacted. YOU called them.",
    "inbound": "You are on a PHONE CALL: the caller phoned RANA AI's number and you answered. THEY called you, "
               "so first find out what they need, then help — never act as if you called them.",
}


def greeting_for(direction: str) -> str:
    return INBOUND_GREETING if direction == "inbound" else GREETING

# Rough ₹ per call-minute, used only for the cost line printed after each test call.
STT_PER_MIN = 0.50          # Sarvam STT ₹30/hour
TTS_PER_10K_CHARS = 20.0 if IS_V4 else 30.0  # Bulbul v4 Flash ₹20, v3 ₹30 per 10,000 characters
LLM_PER_MIN = {"sarvam": 0.30, "groq": 0.10}.get(LLM_PROVIDER, 0.30)
PHONE_PER_MIN = 0.50        # Vobiz estimate — not charged in the browser demo


def make_llm():
    if LLM_PROVIDER == "groq":
        return GroqLLMService(
            api_key=os.environ["GROQ_API_KEY"],
            settings=GroqLLMService.Settings(model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")),
        )
    return SarvamLLMService(
        api_key=SARVAM_KEY,
        settings=SarvamLLMService.Settings(model=os.getenv("SARVAM_LLM_MODEL", "sarvam-105b-conversations")),
    )


def voice_for(lang: str) -> str:
    return VOICE_FOR_LANG.get(lang, VOICE) if NATIVE_VOICES else VOICE


def voice_params(lang: str, sample_rate: int) -> dict:
    """The exact voice settings a line is synthesised with (must match the TTS service, for the cache)."""
    from pipecat.services.sarvam.tts import language_to_sarvam_language

    return dict(model=TTS_MODEL, voice=voice_for(lang), language=language_to_sarvam_language(Language(lang)),
                sample_rate=sample_rate, pace=TTS_PACE, pitch=TTS_PITCH if IS_V4 else None,
                loudness=TTS_LOUDNESS if IS_V4 else None, temperature=None if IS_V4 else TTS_TEMPERATURE)


async def prewarm_greeting(sample_rate: int) -> bool:
    """Make the greeting's (and the instant acknowledgements') audio once at server start, so calls play it instantly."""
    import asyncio

    jobs = [prewarm(SARVAM_KEY, GREETING, **voice_params(CALL_LANGUAGES[0], sample_rate)),
            prewarm(SARVAM_KEY, INBOUND_GREETING, **voice_params(CALL_LANGUAGES[0], sample_rate))]
    if INSTANT_ACK:
        jobs += [prewarm(SARVAM_KEY, a, **voice_params(l, sample_rate)) for l in CALL_LANGUAGES for a in ACKS.get(l, [])]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    return results[0] is True


def build_prompt(direction: str = "outbound") -> str:
    """agent_prompt.txt with this call's direction and allowed languages filled in."""
    names = [LANG_NAMES[l] for l in CALL_LANGUAGES]
    return (
        (HERE / "agent_prompt.txt").read_text(encoding="utf-8")
        .replace("{CALL_CONTEXT}", CALL_CONTEXT.get(direction, CALL_CONTEXT["outbound"]))
        .replace("{CALL_LANGUAGES}", ", ".join(names))
        .replace("{FIRST_LANGUAGE}", names[0])
        .replace("{MAX_SENTENCES}", str(MAX_SENTENCES))
    )


def _bare(word: str) -> str:
    return word.strip(" ,.।!?…").lower()


# Words that count as an acknowledgement at the start of a reply (to avoid "సరే. సరే, ..." doubles).
ACK_WORDS = {_bare(a) for opts in ACKS.values() for a in opts} | {
    "ठीक है", "हाँ", "जी हाँ", "ok", "okay", "ओके", "सरे", "हा", "అలాగే", "ఓకే", "హా", "sure", "right", "great"}


class InstantAck(FrameProcessor):
    """When the caller's turn ends, sometimes play a pre-made short acknowledgement (cached = instant).

    A person doesn't say "जी।" before every sentence — doing it every turn was itself a robot tell (7 Nov test).
    So: only on ACK_RATE of turns, never two turns in a row, and never the same word twice in a row.
    """

    def __init__(self, voice_lang: "VoiceLanguage", sample_rate: int):
        super().__init__()
        self.voice_lang, self.rate = voice_lang, sample_rate
        self.last_text, self.played_last_turn, self.played_this_turn = "", False, False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if INSTANT_ACK and isinstance(frame, LLMContextFrame) and not frame.speculation:
            self.played_this_turn = False
            lang = self.voice_lang.current
            options = [a for a in ACKS.get(lang, []) if a != self.last_text]
            if options and not self.played_last_turn and random.random() < ACK_RATE:
                text = random.choice(options)
                if is_cached(build_payload(text, **voice_params(lang, self.rate))):
                    await self.push_frame(TTSSpeakFrame(text, append_to_context=False), direction)
                    self.last_text, self.played_this_turn = text, True
                    if getattr(self, "log", None):
                        self.log.agent(text, "ack")
            self.played_last_turn = self.played_this_turn
        await self.push_frame(frame, direction)


class ReplyLimiter(FrameProcessor):
    """Cuts a reply after MAX_SENTENCES sentences (long monologues kill phone calls)."""

    ENDS = "?!।."

    def __init__(self, max_sentences: int):
        super().__init__()
        self.max, self.count, self.cut, self.prev = max_sentences, 0, False, ""

    def reset(self):
        self.count, self.cut, self.prev = 0, False, ""

    def clip(self, text: str) -> str | None:
        """The part of this streamed piece of the reply to keep, or None to drop it."""
        if self.cut:
            return None
        for i, ch in enumerate(text):
            before = text[i - 1] if i else self.prev
            after = text[i + 1] if i + 1 < len(text) else ""
            not_end = ch == "." and (after.isdigit() or after == ".")  # "1.5" is a decimal, "..." is a pause
            if ch in self.ENDS and not not_end and before not in self.ENDS:  # "?!" counts once
                self.count += 1
                if self.count >= self.max:
                    self.cut = True
                    if any(c.isalnum() for c in text[i + 1:]):
                        logger.info(f"Reply cut after {self.max} sentences")
                        return text[: i + 1]
                    return text
        self.prev = text[-1:] or self.prev
        return text

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self.reset()
        elif isinstance(frame, LLMTextFrame) and self.max > 0:
            kept = self.clip(frame.text or "")
            if kept is None:
                return  # drop the rest of this reply
            frame.text = kept
        await self.push_frame(frame, direction)


class ReplyBuffer(FrameProcessor):
    """Sends the whole reply to the voice as ONE piece, so it is spoken as one continuous clip.

    Sentence-by-sentence synthesis left a 1–4 s gap between sentences on calls (each sentence was a new request that
    only started after the previous one finished) — the "robot reading lines" effect. The LLM writes a 2-sentence
    reply in ~0.3–0.8 s, so waiting for all of it costs little and gives natural, connected intonation.
    """

    END_MARK = "<<END>>"

    def __init__(self, ack: "InstantAck | None" = None, on_goodbye=None):
        super().__init__()
        self.parts: list[str] = []
        self.ack, self.on_goodbye = ack, on_goodbye

    def finish(self, text: str) -> tuple[str, bool]:
        """Whole reply -> (what to speak, is it the goodbye?)."""
        goodbye = self.END_MARK in text or "<<END" in text
        text = text.replace(self.END_MARK, "").replace("<<END", "").replace(">>", "").strip()
        # An acknowledgement was just played: drop the reply's own opener if it's another one ("సరే, ..." after "సరే.").
        if self.ack and self.ack.played_this_turn:
            m = re.match(r"\s*([^,।.!?]{1,15})[,।.!?]+\s*", text)
            if m and _bare(m.group(1)) in ACK_WORDS and len(text) > m.end() + 3:
                text = text[m.end():]
        limiter = ReplyLimiter(MAX_SENTENCES)
        limiter.reset()
        text = limiter.clip(text) or text
        found = script_language(text, None)
        if found and found[1] and found[0] not in ("hi-IN", "mr-IN"):
            text = text.replace("।", ".")  # Hindi full stop in a Telugu/Tamil reply
        if goodbye and "?" in text:  # never hang up right after asking something ("ఇంకేమైనా అడగాలా?")
            logger.info("Goodbye marker ignored: the reply asks a question")
            goodbye = False
        return text.strip(), goodbye

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (LLMFullResponseStartFrame, InterruptionFrame)):
            self.parts = []
        elif isinstance(frame, LLMTextFrame):
            self.parts.append(frame.text or "")
            return  # held until the reply is complete
        elif isinstance(frame, LLMFullResponseEndFrame):
            text, goodbye = self.finish("".join(self.parts))
            self.parts = []
            if text:
                await self.push_frame(LLMTextFrame(text), direction)
                if getattr(self, "log", None):
                    self.log.agent(text)
            await self.push_frame(frame, direction)
            if goodbye and self.on_goodbye:
                logger.info("Goodbye said — ending the call after it plays")
                if getattr(self, "log", None):
                    self.log.event("goodbye_hangup")
                await self.on_goodbye()
            return
        await self.push_frame(frame, direction)


class TurnTimer(FrameProcessor):
    """Per reply: caller stopped talking -> first sound (may be the instant "जी।") and -> the actual answer."""

    def __init__(self, ack: "InstantAck", call_log: CallLog):
        super().__init__()
        self.ack, self.log = ack, call_log
        self.stopped_at, self.first_sound = None, None
        self.waits: list[float] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStoppedSpeakingFrame):
            self.stopped_at, self.first_sound = time.time(), None
        elif isinstance(frame, BotStartedSpeakingFrame) and self.stopped_at:
            wait = time.time() - self.stopped_at
            if self.first_sound is None and self.ack.played_this_turn:
                self.first_sound = wait  # that was the "जी।" — the answer comes next
            else:
                first = self.first_sound if self.first_sound is not None else wait
                self.waits.append(wait)
                self.log.delays(first, wait)
                logger.info(f"REPLY DELAY: first sound {first:.2f}s, answer started {wait:.2f}s after the caller stopped")
                self.stopped_at = None
        await self.push_frame(frame, direction)


class CostMeter(FrameProcessor):
    """Counts the characters sent to the voice (TTS is billed per character)."""

    def __init__(self):
        super().__init__()
        self.tts_chars = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (LLMTextFrame, TTSSpeakFrame)):
            self.tts_chars += len(frame.text or "")
        await self.push_frame(frame, direction)


# Unicode script of the reply -> the language the voice must speak it in.
SCRIPT_LANGS = [
    (0x0980, 0x09FF, "bn-IN"),  # Bengali
    (0x0A00, 0x0A7F, "pa-IN"),  # Gurmukhi (Punjabi)
    (0x0A80, 0x0AFF, "gu-IN"),  # Gujarati
    (0x0B00, 0x0B7F, "or-IN"),  # Odia
    (0x0B80, 0x0BFF, "ta-IN"),  # Tamil
    (0x0C00, 0x0C7F, "te-IN"),  # Telugu
    (0x0C80, 0x0CFF, "kn-IN"),  # Kannada
    (0x0D00, 0x0D7F, "ml-IN"),  # Malayalam
]


def script_language(text: str, caller_lang: str | None) -> tuple[str, bool] | None:
    """(voice language, found an Indian script?) for this text, or None if it has no letters yet."""
    for ch in text:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:  # Devanagari: Hindi, or Marathi if that's what the caller speaks
            return ("mr-IN" if (caller_lang or "").startswith("mr") else "hi-IN"), True
        for lo, hi, lang in SCRIPT_LANGS:
            if lo <= cp <= hi:
                return lang, True
    if any(ch.isalpha() for ch in text):  # Roman letters only: English (or Hinglish typed in English)
        return ("en-IN" if (caller_lang or "").startswith("en") else "hi-IN"), False
    return None


class CallerLanguage(FrameProcessor):
    """Remembers the language speech-to-text detected in the caller's last sentence (and logs what they said)."""

    def __init__(self, call_log: CallLog | None = None):
        super().__init__()
        self.lang: str | None = None
        self.log = call_log

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            lang = str(getattr(frame.language, "value", frame.language)) if frame.language else None
            if lang in CALL_LANGUAGES:  # ignore mislabels like "hello" -> Tamil
                self.lang = lang
            if self.log and (frame.text or "").strip():
                self.log.caller(frame.text.strip(), lang)
        await self.push_frame(frame, direction)


# Caller explicitly asks for a language ("speak in English", "ఇంగ్లీష్ లో మాట్లాడండి", "हिंदी में बोलिए").
LANG_REQUEST_NAMES = {
    "en-IN": ("english", "ఇంగ్లీష్", "ఇంగ్లిష్", "इंग्लिश", "अंग्रेज़ी", "अंग्रेजी"),
    "hi-IN": ("hindi", "హిందీ", "హింది", "हिंदी", "हिन्दी"),
    "te-IN": ("telugu", "తెలుగు", "तेलुगु", "तेलगु"),
}
LANG_REQUEST_VERBS = ("speak", "talk", "tell", "explain", "మాట్లాడ", "చెప్ప", "बात", "बोल", "बताइए", "बताओ")


def requested_language(user_text: str) -> str | None:
    """The language the caller explicitly asked for, if any (the last one named wins)."""
    low = user_text.lower()
    if not any(v in low for v in LANG_REQUEST_VERBS):
        return None
    best, pos = None, -1
    for lang, names in LANG_REQUEST_NAMES.items():
        for n in names:
            i = low.rfind(n)
            if i > pos and lang in CALL_LANGUAGES:
                best, pos = lang, i
    return best


def language_hint(user_text: str, current: str) -> str:
    """Reply language for this turn: an explicit request ("speak in English") wins; otherwise the SCRIPT of the
    caller's words (Telugu letters -> Telugu, Devanagari -> Hindi, 3+ English words -> English). Short Roman bits
    ("ok", "hello", "4 hours") keep the current language."""
    asked = requested_language(user_text)
    if asked:
        return asked
    found = script_language(user_text, None)
    words = len(user_text.split())
    if found and found[1] and found[0] in CALL_LANGUAGES and (words >= 2 or found[0] == current):
        return found[0]  # a single "हाँ" / "సరే" doesn't switch the language
    if found and not found[1] and "en-IN" in CALL_LANGUAGES and words >= 3:
        return "en-IN"
    return current


def add_language_hint(messages: list, current: str) -> tuple[list, str]:
    """Append 'reply only in X' to the newest caller message (the LLM drifted back to Hindi on Telugu callers)."""
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            if "[Reply ONLY in" in m["content"]:
                return messages, current
            lang = language_hint(m["content"], current)
            name = LANG_NAMES[lang]
            m["content"] += (f"\n\n[Reply ONLY in {name}" + ("" if lang == "en-IN" else f", in {name} script") +
                             ". Never mix in another Indian language.]")
            return messages, lang
    return messages, current


class LanguageHint(FrameProcessor):
    def __init__(self, start_lang: str, caller: "CallerLanguage | None" = None):
        super().__init__()
        self.current = start_lang
        self.caller = caller

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and not frame.speculation:
            msgs, self.current = add_language_hint(frame.context.get_messages(), self.current)
            frame.context.set_messages(msgs)
            if self.caller:
                # STT is locked to one language, so its label is always te-IN. Tell the voice what we decided,
                # so an English reply is spoken with the English voice (not read as Hindi).
                self.caller.lang = self.current
        await self.push_frame(frame, direction)


class VoiceLanguage(FrameProcessor):
    """Switches the Bulbul voice to the language each reply is written in (Telugu reply -> te-IN, etc.)."""

    def __init__(self, caller: CallerLanguage, start_lang: str):
        super().__init__()
        self.caller = caller
        self.current = start_lang
        self.voice = VOICE
        self.decided = False  # language picked for this reply
        self.native = False   # ...from an Indian script (Roman-only guesses can still be corrected)

    async def _switch(self, lang: str, direction: FrameDirection):
        if lang not in CALL_LANGUAGES:
            logger.warning(f"Reply looks like {lang}, which is not allowed on this call — keeping {self.current}")
            return
        voice = voice_for(lang)
        if lang == self.current and voice == self.voice:
            return
        logger.info(f"Voice {self.current}/{self.voice} -> {lang}/{voice}")
        self.current, self.voice = lang, voice
        await self.push_frame(
            TTSUpdateSettingsFrame(delta=SarvamStreamTTSService.Settings(language=Language(lang), voice=voice)),
            direction,
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self.decided = self.native = False
        elif isinstance(frame, (LLMTextFrame, TTSSpeakFrame)):
            found = script_language(frame.text or "", self.caller.lang)
            if found:
                lang, native = found
                # First letters pick the language; the first Indian script in a reply overrides a Roman-only guess
                # (so "Okay, आपका…" is spoken in Hindi). Once an Indian script decides, English words don't flip it.
                if not self.decided or (native and not self.native):
                    await self._switch(lang, direction)
                    self.decided, self.native = True, native
        await self.push_frame(frame, direction)


class FastStartSarvamSTT(SarvamSTTService):
    """Sarvam STT that connects in the background: the stock service blocked the whole pipeline ~1 s on its
    WebSocket connect, so the greeting started 2 s after pick-up. Audio before it connects is dropped, which is fine:
    the caller is muted during the greeting anyway."""

    async def setup(self, setup):
        import asyncio

        await STTService.setup(self, setup)  # skip SarvamSTTService.setup's blocking connect

        async def connect():
            try:
                await self._connect()
            except Exception as e:
                logger.error(f"Speech-to-text connect failed: {e}")

        self._bg_connect = asyncio.create_task(connect())


transport_params = {
    "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


async def bot(runner_args: RunnerArguments):
    """Browser demo (Pipecat dev runner, WebRTC)."""
    transport = await create_transport(runner_args, transport_params)
    await run_agent(transport, handle_sigint=runner_args.handle_sigint)


async def run_agent(transport, *, handle_sigint: bool = False, phone_rate: int | None = None, label: str = "",
                    call_id: str | None = None, direction: str = "outbound"):
    """The voice agent itself. Shared by the browser demo (bot) and phone calls (phone_server.py).

    phone_rate: the phone line's audio sample rate (8000 for Vobiz μ-law); None for the browser.
    call_id: Vobiz call id (phone) — used to name this call's record in logs/.
    """
    call_log = CallLog(call_id or f"web-{int(time.time())}", kind="phone" if phone_rate else "browser")
    if not SARVAM_KEY:
        raise RuntimeError("SARVAM_API_KEY is missing — copy .env.example to .env and add your key.")
    if code_is_stale():
        logger.warning("!!! The agent's code changed after this server started — this call uses the OLD code. "
                       "Stop the server (Ctrl+C) and start it again. !!!")
    start_lang = CALL_LANGUAGES[0]

    # saaras:v4. STT_LANGUAGE (e.g. te-IN) locks recognition to one language — on phone audio, auto-detect misheard
    # Telugu as Tamil/Gujarati. Empty = auto-detect.
    stt_lang = os.getenv("STT_LANGUAGE", "").strip()
    # "codemix": English words come back in English letters, Indian words in their own script. With the plain
    # "transcribe" mode locked to te-IN, English speech came back in Telugu letters ("కెన్ యు స్పీక్ ఇన్ ఇంగ్లీష్"),
    # so the agent never noticed the caller had switched to English (16 Nov).
    stt_mode = os.getenv("STT_MODE", "codemix").strip() or None
    stt = FastStartSarvamSTT(api_key=SARVAM_KEY, mode=stt_mode,
                             settings=SarvamSTTService.Settings(language=Language(stt_lang)) if stt_lang else None)
    llm = make_llm()
    voice_settings = (
        dict(pitch=TTS_PITCH, loudness=TTS_LOUDNESS) if IS_V4 else dict(temperature=TTS_TEMPERATURE)
    )
    http = aiohttp.ClientSession()
    # HTTP streaming voice: first audio in ~1 s (the WebSocket version took 2–3 s, plus 3 s per interruption).
    # Sample rate matters: at 24 kHz Sarvam streams SLOWER than real time (1.5x -> choppy "cut-cut" audio on calls);
    # at 8 kHz it streams 2.5x faster than real time. A phone line is 8 kHz anyway, so phone calls ask for 8 kHz.
    tts = SarvamStreamTTSService(
        api_key=SARVAM_KEY,
        aiohttp_session=http,
        sample_rate=phone_rate or 16000,
        settings=SarvamStreamTTSService.Settings(
            model=TTS_MODEL,
            voice=voice_for(start_lang),
            language=Language(start_lang),
            pace=TTS_PACE,
            **voice_settings,
        ),
        # ReplyBuffer hands over each reply as one piece; TOKEN mode speaks it as-is (no re-splitting into sentences).
        text_aggregation_mode=TextAggregationMode.TOKEN,
        # Pipecat gives up on a reply whose audio hasn't started after this long (default 3 s) and logs an error.
        stop_frame_timeout_s=8.0,
    )
    logger.info(f"Agent: voice {TTS_MODEL} / {voice_for(start_lang)} · languages {', '.join(CALL_LANGUAGES)} · "
                f"max {MAX_SENTENCES} sentences · instant ack {INSTANT_ACK} · HTTP-stream voice")
    meter = CostMeter()
    caller_lang = CallerLanguage(call_log)
    voice_lang = VoiceLanguage(caller_lang, start_lang)
    voice_lang.voice = voice_for(start_lang)
    ack = InstantAck(voice_lang, phone_rate or 16000)
    lang_hint = LanguageHint(start_lang, caller_lang)
    ack.log = call_log
    async def hang_up():
        # Queued behind the goodbye: the pipeline plays it out, then ends; the Vobiz serializer hangs up the call.
        await task.queue_frame(EndFrame())

    buffer = ReplyBuffer(ack, on_goodbye=hang_up)
    buffer.log = call_log
    timer = TurnTimer(ack, call_log)

    context = LLMContext(messages=[{"role": "system", "content": build_prompt(direction)}])
    greeting = greeting_for(direction)
    call_log.event("direction", direction=direction)
    # Silero hears speech; Pipecat's default smart-turn model decides when the caller has FINISHED.
    # The greeting can't be interrupted (people say "hello?" the moment they pick up, which used to cut it off).
    # Smart Turn is trained mostly on English: on Hindi/Telugu phone audio it says "not finished" on most turns, so
    # the answer waits for TURN_MAX_SILENCE of silence (was 3 s, then 1 s; 0.7 s ≈ a person's pause before replying).
    turn_wait = float(os.getenv("TURN_MAX_SILENCE", "1.0"))
    user_params = LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(confidence=0.75, start_secs=0.3) if phone_rate else None),
        user_turn_strategies=UserTurnStrategies(stop=[TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=turn_wait)))]),
        user_mute_strategies=[MuteUntilFirstBotCompleteUserMuteStrategy()],
        # The caller spoke but speech-to-text returned nothing (on 7 Nov this left the caller in silence for 16 s).
        # Never stay silent: ask them to repeat. And give up waiting for a transcript after 2.5 s instead of 5 s.
        empty_user_turn=EmptyUserTurnConfig(
            idle_prompt="The caller just said something on the phone, but it could not be recognised. In your current "
                        "language, briefly ask them to repeat (one short sentence, e.g. 'माफ़ कीजिए, फिर से बोलिए?').",
            max_consecutive_recoveries=2,
        ),
        user_turn_stop_timeout=2.5,
    )
    aggregators = LLMContextAggregatorPair(context, user_params=user_params)

    pipeline = Pipeline(
        [
            transport.input(),       # caller's audio in
            stt,                     # speech -> text (Sarvam saaras:v4, auto-detects language)
            caller_lang,             # remember which language the caller is speaking
            aggregators.user(),      # add caller's words to the conversation
            lang_hint,               # 'reply only in Telugu' etc., from the caller's script
            ack,                     # instant "जी।" while the reply is prepared
            llm,                     # decide the reply
            buffer,                  # whole reply -> one voice request; max sentences; goodbye -> hang up
            meter,                   # count characters going to the voice
            voice_lang,              # switch the voice to the reply's language (allowed languages only)
            tts,                     # text -> voice (Sarvam Bulbul, HTTP streaming)
            timer,                   # log how long the caller waited
            transport.output(),      # audio back to caller
            aggregators.assistant(), # remember what the agent said
        ]
    )
    # Metrics: the browser's "Metrics" tab shows how long each step (speech-to-text, LLM, voice) takes per reply.
    params = PipelineParams(enable_metrics=True)
    if phone_rate:  # phone audio is 8 kHz both ways
        params = PipelineParams(enable_metrics=True, audio_in_sample_rate=phone_rate, audio_out_sample_rate=phone_rate)
    task = PipelineTask(pipeline, params=params)
    started = {"t": 0.0}

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        started["t"] = time.time()
        logger.info(f"{label or 'Caller'} connected — speaking greeting")
        call_log.agent(greeting, "greeting")
        await task.queue_frames([TTSSpeakFrame(greeting)])

    @task.event_handler("on_pipeline_error")
    async def on_error(_task, frame):
        call_log.event("error", error=str(getattr(frame, "error", frame))[:200])

    wrapped = {"done": False}

    def wrap_up():
        """Cost + delays + transcript, written once however the call ends (caller hung up, goodbye, error)."""
        if wrapped["done"]:
            return
        wrapped["done"] = True
        mins = max((time.time() - started["t"]) / 60, 1 / 60) if started["t"] else 0
        stt_cost = mins * STT_PER_MIN
        tts_cost = meter.tts_chars / 10_000 * TTS_PER_10K_CHARS
        llm_cost = mins * LLM_PER_MIN
        phone_cost = mins * PHONE_PER_MIN if phone_rate else 0.0
        total = stt_cost + tts_cost + llm_cost + phone_cost
        if timer.waits:
            w = sorted(timer.waits)
            logger.info(f"REPLY DELAYS {label} — {len(w)} answers: fastest {w[0]:.2f}s, "
                        f"typical {w[len(w) // 2]:.2f}s, slowest {w[-1]:.2f}s")
        logger.info(
            f"CALL COST ESTIMATE {label} — {mins:.2f} min | STT ₹{stt_cost:.2f} + voice ₹{tts_cost:.2f} "
            f"({meter.tts_chars} chars) + LLM ₹{llm_cost:.2f}"
            + (f" + phone line ₹{phone_cost:.2f}" if phone_rate else "")
            + f" = ₹{total:.2f} (≈ ₹{total / mins if mins else 0:.2f}/min"
            + ("" if phone_rate else f"; add ~₹{PHONE_PER_MIN:.2f}/min on a real phone line")
            + ")"
        )
        record = call_log.finish(
            cost={"stt": round(stt_cost, 2), "voice": round(tts_cost, 2), "voice_chars": meter.tts_chars,
                  "llm": round(llm_cost, 2), "phone": round(phone_cost, 2), "total": round(total, 2),
                  "per_min": round(total / mins, 2) if mins else 0},
            settings={"voice": f"{TTS_MODEL} / {voice_for(start_lang)} @ pace {TTS_PACE}", "stt": "sarvam saaras:v4",
                      "llm": f"{LLM_PROVIDER} / {os.getenv('SARVAM_LLM_MODEL', 'sarvam-105b-conversations') if LLM_PROVIDER == 'sarvam' else os.getenv('GROQ_MODEL')}",
                      "languages": CALL_LANGUAGES, "ack_rate": ACK_RATE if INSTANT_ACK else 0,
                      "turn_max_silence": turn_wait, "code_version": CODE_VERSION},
        )
        logger.info(f"CALL RECORD saved: logs/calls.jsonl + logs/transcripts/ ({record['turns']} turns, "
                    f"{record['errors']} errors)")

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        call_log.event("caller_disconnected")
        wrap_up()
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=handle_sigint).run(task)
    finally:
        wrap_up()
        await http.close()


if __name__ == "__main__":
    from pipecat.runner.run import main

    from winloop import use_selector_loop_in_uvicorn

    use_selector_loop_in_uvicorn()  # Windows: the default loop delayed streamed voice audio by ~2 s
    main()
