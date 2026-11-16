"""Per-call records: what was said, how long the caller waited, what it cost, how it ended.

Writes, under logs/:
  calls.jsonl                              one JSON line per finished call (+ one "hangup" line from Vobiz)
  transcripts/<date>/<time>_<call>.txt     the conversation, readable, with timings
Read them back with recent_calls() (used by the phone server's /calls page).
"""

import datetime as dt
import json
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
CALLS_FILE = LOG_DIR / "calls.jsonl"
_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _append(record: dict):
    LOG_DIR.mkdir(exist_ok=True)
    with _lock, CALLS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class CallLog:
    def __init__(self, call_id: str, kind: str = "phone"):
        self.call_id, self.kind = call_id, kind
        self.started = time.time()
        self.lines: list[dict] = []          # {"t": seconds, "who": "caller"/"priya", "text", "lang"/"kind"}
        self.answer_delays: list[float] = []  # caller stopped -> Priya's actual answer starts
        self.first_sound_delays: list[float] = []  # caller stopped -> any sound (incl. "जी।")
        self.events: list[dict] = []
        self.errors = 0

    def _t(self) -> float:
        return round(time.time() - self.started, 2)

    def caller(self, text: str, lang: str | None = None):
        self.lines.append({"t": self._t(), "who": "caller", "text": text, "lang": lang})

    def agent(self, text: str, kind: str = "reply"):
        self.lines.append({"t": self._t(), "who": "priya", "text": text, "kind": kind})

    def event(self, name: str, **data):
        self.events.append({"t": self._t(), "event": name, **data})
        if name == "error":
            self.errors += 1

    def delays(self, first_sound: float, answer: float | None):
        self.first_sound_delays.append(round(first_sound, 2))
        if answer is not None:
            self.answer_delays.append(round(answer, 2))

    @staticmethod
    def _stats(xs: list[float]) -> dict | None:
        if not xs:
            return None
        s = sorted(xs)
        return {"replies": len(s), "fastest": s[0], "typical": s[len(s) // 2], "slowest": s[-1]}

    def finish(self, cost: dict, settings: dict) -> dict:
        duration = round(time.time() - self.started, 1)
        record = {
            "event": "call", "call_id": self.call_id, "kind": self.kind, "ended_at": _now(),
            "duration_s": duration, "turns": sum(1 for l in self.lines if l["who"] == "caller"),
            "answer_delay_s": self._stats(self.answer_delays),
            "first_sound_delay_s": self._stats(self.first_sound_delays),
            "cost_inr": cost, "errors": self.errors, "settings": settings,
            "languages_heard": sorted({l["lang"] for l in self.lines if l.get("lang")}),
            "events": self.events, "transcript": self.lines,
        }
        _append(record)
        self._write_transcript(record)
        return record

    def _write_transcript(self, r: dict):
        day = dt.date.today().isoformat()
        folder = LOG_DIR / "transcripts" / day
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%H%M%S")
        ad, fd = r["answer_delay_s"] or {}, r["first_sound_delay_s"] or {}
        out = [
            f"Call {r['call_id']}  ({r['kind']})  ended {r['ended_at']}",
            f"Duration {r['duration_s']} s · {r['turns']} caller turns · cost ₹{r['cost_inr'].get('total', 0):.2f} "
            f"(≈ ₹{r['cost_inr'].get('per_min', 0):.2f}/min) · errors {r['errors']}",
            f"Answer delay: typical {ad.get('typical', '-')} s, slowest {ad.get('slowest', '-')} s  ·  "
            f"first sound: typical {fd.get('typical', '-')} s",
            f"Voice {r['settings'].get('voice')} · STT {r['settings'].get('stt')} · LLM {r['settings'].get('llm')}",
            "-" * 80,
        ]
        for l in r["transcript"]:
            who = "CALLER" if l["who"] == "caller" else ("Priya*" if l.get("kind") == "ack" else "PRIYA ")
            extra = f"  [{l['lang']}]" if l.get("lang") else ""
            out.append(f"{l['t']:7.1f}s  {who}: {l['text']}{extra}")
        for e in r["events"]:
            out.append(f"{e['t']:7.1f}s  ** {e['event']}: {json.dumps({k: v for k, v in e.items() if k not in ('t', 'event')}, ensure_ascii=False)}")
        out.append("(Priya* = instant acknowledgement)")
        (folder / f"{stamp}_{r['call_id'][:8]}.txt").write_text("\n".join(out) + "\n", encoding="utf-8")


def log_hangup(info: dict):
    """Vobiz's hangup webhook: why the call ended, billed seconds."""
    _append({"event": "hangup", "at": _now(), **info})


def log_dial(call_id: str | None, to: str, status: int, body: str):
    _append({"event": "dial", "at": _now(), "call_id": call_id, "to": to, "vobiz_status": status, "vobiz_body": body[:300]})


def dials_today() -> int:
    """How many outbound calls were placed today (for the daily limit)."""
    if not CALLS_FILE.exists():
        return 0
    today = dt.date.today().isoformat()
    return sum(1 for line in CALLS_FILE.read_text(encoding="utf-8").splitlines()[-5000:]
               if '"event": "dial"' in line and f'"at": "{today}' in line)


def delete_old_logs(days: int) -> int:
    """Personal data (numbers, transcripts) is not kept forever: delete transcript folders older than `days`,
    and drop old lines from calls.jsonl. Returns how many files/lines were removed."""
    if days <= 0 or not LOG_DIR.exists():
        return 0
    cutoff = dt.date.today() - dt.timedelta(days=days)
    removed = 0
    tdir = LOG_DIR / "transcripts"
    if tdir.exists():
        for folder in tdir.iterdir():
            try:
                if folder.is_dir() and dt.date.fromisoformat(folder.name) < cutoff:
                    for f in folder.iterdir():
                        f.unlink()
                        removed += 1
                    folder.rmdir()
            except ValueError:
                continue
    if CALLS_FILE.exists():
        keep = []
        lines = CALLS_FILE.read_text(encoding="utf-8").splitlines()
        for line in lines:
            m = line.split('"at": "', 1)[-1][:10] if '"at": "' in line else line.split('"ended_at": "', 1)[-1][:10]
            try:
                if dt.date.fromisoformat(m) < cutoff:
                    continue
            except ValueError:
                pass
            keep.append(line)
        if len(keep) != len(lines):
            removed += len(lines) - len(keep)
            with _lock:
                CALLS_FILE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return removed


def was_dialed(call_id: str | None) -> bool:
    """True if WE placed this call (outbound) — from the dial records."""
    if not call_id or not CALLS_FILE.exists():
        return False
    for line in CALLS_FILE.read_text(encoding="utf-8").splitlines()[-500:]:
        if '"event": "dial"' in line and call_id in line:
            return True
    return False


def recent_calls(n: int = 20) -> list[dict]:
    """Last n calls, newest first, with Vobiz's hangup info merged in (transcripts left out to keep it short)."""
    if not CALLS_FILE.exists():
        return []
    calls, hangups, dials = [], {}, {}
    for line in CALLS_FILE.read_text(encoding="utf-8").splitlines()[-2000:]:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "call":
            calls.append(r)
        elif r.get("event") == "hangup" and r.get("CallUUID"):
            hangups[r["CallUUID"]] = r
        elif r.get("event") == "dial" and r.get("call_id"):
            dials[r["call_id"]] = r
    out = []
    for r in reversed(calls[-n:]):
        h, d = hangups.get(r["call_id"], {}), dials.get(r["call_id"], {})
        out.append({k: v for k, v in r.items() if k not in ("transcript", "events")} | {
            "to": h.get("To") or d.get("to"), "hangup_cause": h.get("HangupCause"),
            "billed_s": h.get("BillDuration"), "lines": len(r.get("transcript", []))})
    return out
