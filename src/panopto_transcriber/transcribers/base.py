from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class TranscriptionResult:
    text_path: Path
    srt_path: Path | None


class Transcriber(Protocol):
    name: str

    def transcribe(self, media_path: Path, out_dir: Path) -> TranscriptionResult: ...
