"""whisper.cpp backend: subprocess to the `whisper-cli` binary.

Install:  brew install whisper-cpp
Model:    download a ggml-*.bin file (e.g. ggml-base.en.bin) and pass its path.

whisper.cpp expects 16kHz mono WAV. We pipe through ffmpeg first so it can
ingest MP4, MP3, M4A, etc.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import TranscriptionResult


class WhisperCppTranscriber:
    name = "whisper-cpp"

    def __init__(self, model: str, model_path: str, bin_path: str = "whisper-cli") -> None:
        if not model_path:
            raise ValueError(
                "whisper.cpp needs WHISPER_CPP_MODEL_PATH set to a ggml-*.bin file."
            )
        if not Path(model_path).exists():
            raise FileNotFoundError(f"whisper.cpp model not found at {model_path}")
        if shutil.which(bin_path) is None:
            raise FileNotFoundError(
                f"whisper.cpp binary {bin_path!r} not on PATH. Try `brew install whisper-cpp`."
            )
        if shutil.which("ffmpeg") is None:
            raise FileNotFoundError("ffmpeg not on PATH. Try `brew install ffmpeg`.")
        self.model = model  # informational; the actual selection is model_path
        self.model_path = model_path
        self.bin_path = bin_path

    def transcribe(self, media_path: Path, out_dir: Path) -> TranscriptionResult:
        stem = media_path.stem
        out_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / f"{stem}.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(media_path),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                    str(wav_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            out_prefix = out_dir / stem
            subprocess.run(
                [
                    self.bin_path,
                    "-m", self.model_path,
                    "-f", str(wav_path),
                    "-otxt", "-osrt",
                    "-of", str(out_prefix),
                ],
                check=True,
            )

        return TranscriptionResult(
            text_path=out_dir / f"{stem}.txt",
            srt_path=out_dir / f"{stem}.srt",
        )
