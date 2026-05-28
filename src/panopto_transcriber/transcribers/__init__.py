from __future__ import annotations

from pathlib import Path

from .base import Transcriber, TranscriptionResult


def get_transcriber(backend: str, model: str, **kwargs: object) -> Transcriber:
    if backend == "whisper-cpp":
        from .whisper_cpp import WhisperCppTranscriber

        return WhisperCppTranscriber(model=model, **kwargs)  # type: ignore[arg-type]
    if backend == "openai-whisper":
        from .openai_whisper import OpenAIWhisperTranscriber

        return OpenAIWhisperTranscriber(model=model)
    raise ValueError(
        f"Unknown transcriber backend: {backend!r}. Use 'whisper-cpp' or 'openai-whisper'."
    )


__all__ = ["Transcriber", "TranscriptionResult", "get_transcriber", "Path"]
