"""openai-whisper backend: the Python `whisper` package.

Install:  uv sync --extra openai-whisper   (pulls openai-whisper + torch)
"""
from __future__ import annotations

from pathlib import Path

from .base import TranscriptionResult


class OpenAIWhisperTranscriber:
    name = "openai-whisper"

    def __init__(self, model: str) -> None:
        try:
            import whisper  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "openai-whisper not installed. Run: uv sync --extra openai-whisper"
            ) from e
        self.model_name = model
        self._model = None  # lazy load

    def _load(self):
        if self._model is None:
            import whisper

            self._model = whisper.load_model(self.model_name)
        return self._model

    def transcribe(self, media_path: Path, out_dir: Path) -> TranscriptionResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        model = self._load()
        result = model.transcribe(str(media_path), verbose=False)

        stem = media_path.stem
        text_path = out_dir / f"{stem}.txt"
        srt_path = out_dir / f"{stem}.srt"

        text_path.write_text(result["text"].strip() + "\n")
        srt_path.write_text(_segments_to_srt(result.get("segments", [])))

        return TranscriptionResult(text_path=text_path, srt_path=srt_path)


def _segments_to_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_ts(seg['start'])} --> {_ts(seg['end'])}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines)


def _ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
