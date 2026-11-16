"""Sarvam Bulbul over HTTP streaming (POST /text-to-speech/stream) for Pipecat, with silence trimming and a cache.

Why: measured from this laptop on 7 Nov 2026, time to first audio for a short Hindi reply —
    WebSocket (Pipecat's SarvamTTSService):   1.8–2.6 s, plus ~3 s to reconnect after every interruption
    REST (Pipecat's SarvamHttpTTSService):    4.6–10.8 s (waits for the whole clip)
    HTTP streaming (this service):            ~0.6–1.5 s (v3; it sometimes spikes when Sarvam is busy), and an
                                              interruption just cancels the request
Sample rate matters: at 24 kHz Sarvam streams slower than real time (choppy audio); at 8 kHz, 2.5x faster.

Silence: Bulbul v3 pads every clip with ~0.2 s of silence at the start and end. That delays the first sound and puts
dead air between turns, so it is trimmed (a few tens of ms are kept so words aren't clipped).

Cache: the same voice saying the same short line (the greeting, "जी।", common answers) is synthesised once and then
played instantly from memory — faster, and those characters aren't billed again.
"""

import array
from collections import OrderedDict
from collections.abc import AsyncGenerator

import aiohttp
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.sarvam._sdk import sdk_headers
from pipecat.services.sarvam.tts import SarvamHttpTTSService

CHUNK_BYTES = 1600          # read size: 100 ms of 8 kHz 16-bit mono
CACHE_MAX_TEXT = 200        # only cache lines up to this many characters
CACHE_MAX_ITEMS = 500
_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
URL = "https://api.sarvam.ai/text-to-speech/stream"

LOUD = 500                  # 16-bit amplitude above which a sample counts as sound
KEEP_BEFORE_S = 0.04        # keep this much silence before the first sound
KEEP_AFTER_S = 0.15        # ...and after the last sound (a natural breath before the caller replies)
HOLD_BACK_S = 0.25          # while streaming, hold this much back so trailing silence can be cut at the end


def build_payload(text, *, model, voice, language, sample_rate, pace, pitch=None, loudness=None, temperature=None):
    p = {"text": text, "target_language_code": language, "speaker": voice, "model": model,
         "speech_sample_rate": sample_rate, "pace": pace if pace is not None else 1.0, "output_audio_codec": "linear16"}
    v4 = str(model).startswith("bulbul:v4")
    if v4 and pitch is not None:
        p["pitch"] = pitch
    if v4 and loudness is not None:
        p["loudness"] = loudness
    if not v4 and temperature is not None:
        p["temperature"] = temperature
    return p


def _key(payload: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in payload.items()))


def is_cached(payload: dict) -> bool:
    return _key(payload) in _CACHE


def _remember(payload: dict, audio: bytes):
    if len(payload["text"]) <= CACHE_MAX_TEXT and audio:
        _CACHE[_key(payload)] = audio
        _CACHE.move_to_end(_key(payload))
        while len(_CACHE) > CACHE_MAX_ITEMS:
            _CACHE.popitem(last=False)


def _samples(buf: bytes) -> array.array:
    a = array.array("h")
    a.frombytes(buf[: len(buf) // 2 * 2])
    return a


def _first_loud(buf: bytes) -> int | None:
    for i, v in enumerate(_samples(buf)):
        if v > LOUD or v < -LOUD:
            return i
    return None


def _last_loud(buf: bytes) -> int | None:
    a = _samples(buf)
    for i in range(len(a) - 1, -1, -1):
        if a[i] > LOUD or a[i] < -LOUD:
            return i
    return None


def trim(buf: bytes, rate: int) -> bytes:
    """Cut leading/trailing silence from a whole clip."""
    first, last = _first_loud(buf), _last_loud(buf)
    if first is None:
        return b""
    start = max(0, first - int(rate * KEEP_BEFORE_S))
    end = min(len(buf) // 2, last + int(rate * KEEP_AFTER_S))
    return buf[start * 2: end * 2]


async def prewarm(api_key: str, text: str, **voice) -> bool:
    """Synthesise a line ahead of time (e.g. the greeting at server start) so calls can play it instantly."""
    payload = build_payload(text, **voice)
    if _key(payload) in _CACHE:
        return True
    async with aiohttp.ClientSession() as s:
        async with s.post(URL, json=payload, headers={"api-subscription-key": api_key}) as r:
            if r.status != 200:
                logger.warning(f"Prewarm failed for [{text[:30]}]: {r.status} {(await r.text())[:150]}")
                return False
            _remember(payload, trim(await r.read(), payload["speech_sample_rate"]))
    return True


class SarvamStreamTTSService(SarvamHttpTTSService):
    """Same settings as SarvamHttpTTSService (model, voice, language, pace...), but streams the audio."""

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        s = self._settings
        rate = self.sample_rate
        payload = build_payload(text, model=s.model, voice=s.voice, language=s.language, sample_rate=rate,
                                pace=s.pace, pitch=s.pitch, loudness=s.loudness, temperature=s.temperature)

        def frame(audio: bytes) -> TTSAudioRawFrame:
            return TTSAudioRawFrame(audio=audio, sample_rate=rate, num_channels=1, context_id=context_id)

        cached = _CACHE.get(_key(payload))
        if cached:
            logger.debug(f"{self}: cached audio for [{text[:40]}]")
            for i in range(0, len(cached), CHUNK_BYTES):
                yield frame(cached[i:i + CHUNK_BYTES])
            return

        headers = {"api-subscription-key": self._api_key, "Content-Type": "application/json", **sdk_headers()}
        hold = int(rate * HOLD_BACK_S) * 2
        try:
            async with self._session.post(URL, json=payload, headers=headers) as response:
                if response.status != 200:
                    yield ErrorFrame(error=f"Sarvam TTS stream error {response.status}: {(await response.text())[:200]}")
                    return
                await self.start_tts_usage_metrics(text)
                pending, whole, started = b"", bytearray(), False
                async for chunk in response.content.iter_any():
                    pending += chunk
                    if not started:  # skip the leading silence
                        first = _first_loud(pending)
                        if first is None:  # still silent: keep only the last 40 ms (cut on a sample boundary)
                            drop = len(pending) // 2 - int(rate * KEEP_BEFORE_S)
                            if drop > 0:
                                pending = pending[drop * 2:]
                            continue
                        pending = pending[max(0, first - int(rate * KEEP_BEFORE_S)) * 2:]
                        started = True
                    if len(pending) > hold:  # play everything except the last bit (may be trailing silence)
                        n = (len(pending) - hold) // 2 * 2
                        whole += pending[:n]
                        yield frame(pending[:n])
                        pending = pending[n:]
                if started and pending:  # end of clip: drop the trailing silence
                    last = _last_loud(pending)
                    if last is not None:
                        tail = pending[: min(len(pending) // 2, last + int(rate * KEEP_AFTER_S)) * 2]
                        whole += tail
                        yield frame(tail)
                _remember(payload, bytes(whole))  # only reached if the line played to the end (not interrupted)
        except Exception as e:
            logger.error(f"{self} streaming error: {e}")
            yield ErrorFrame(error=f"Sarvam TTS stream failed: {e}")
