"""Use the Selector event loop on Windows.

uvicorn (and Python) default to the Proactor event loop on Windows. Measured on 7 Nov 2026, Proactor delivered
Sarvam's streamed voice audio in big lumps 2.4–2.9 s after the request instead of piece by piece — first sound at
2.4 s instead of 0.6 s with the Selector loop. Linux servers (production) are not affected.
"""

import asyncio
import sys

IS_WINDOWS = sys.platform == "win32"
UVICORN_LOOP = "asyncio:SelectorEventLoop" if IS_WINDOWS else "auto"


def selector_loop_factory(use_subprocess: bool = False):
    """For uvicorn's LOOP_FACTORIES table (it calls this, then calls the result to make the loop)."""
    return asyncio.SelectorEventLoop if IS_WINDOWS else None


def use_selector_loop_in_uvicorn():
    """Make every uvicorn.run(...) in this process (e.g. Pipecat's dev runner) use the Selector loop."""
    if IS_WINDOWS:
        import uvicorn.config

        uvicorn.config.LOOP_FACTORIES["auto"] = "winloop:selector_loop_factory"
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
