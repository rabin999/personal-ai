"""Both wired STT adapters must satisfy the STT port (incl. `preload`).

`api/app.py` calls `pipeline.stt.preload()` at startup on whatever adapter `stt_engine`
selects. Only `FasterWhisperSTT` implemented it, so once the default became "grok" every
startup raised `AttributeError` into a best-effort `except Exception` — and silently took
`llm.preload()` (the local embedder) with it.
"""

import inspect

from adapters.stt.grok import GrokSTT
from ports.stt import STT


def test_every_wired_stt_adapter_implements_the_port() -> None:
    from adapters.stt.faster_whisper import FasterWhisperSTT

    for adapter in (GrokSTT, FasterWhisperSTT):
        for name in ("transcribe_stream", "preload"):
            assert callable(getattr(adapter, name, None)), (
                f"{adapter.__name__} does not implement STT.{name}() — "
                "api/app.py calls it at startup on the wired adapter"
            )


def test_preload_is_declared_on_the_port() -> None:
    assert callable(getattr(STT, "preload", None))
    assert inspect.signature(STT.preload).parameters.keys() == {"self"}


def test_grok_preload_is_a_no_op_and_does_not_raise() -> None:
    from config.settings import Settings

    GrokSTT(Settings()).preload()  # a remote endpoint has nothing to warm
