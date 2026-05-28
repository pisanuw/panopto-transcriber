from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    panopto_host: str
    cookies_browser: str
    cookies_profile: str | None

    canvas_url: str
    canvas_token: str

    download_dir: Path
    transcript_dir: Path

    transcriber_backend: str
    transcriber_model: str
    whisper_cpp_model_path: str
    whisper_cpp_bin: str

    @classmethod
    def load(cls) -> "Config":
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser().resolve()
        transcript_dir = Path(os.getenv("TRANSCRIPT_DIR", "./transcripts")).expanduser().resolve()
        download_dir.mkdir(parents=True, exist_ok=True)
        transcript_dir.mkdir(parents=True, exist_ok=True)

        whisper_model = os.getenv("WHISPER_CPP_MODEL_PATH", "")
        if whisper_model:
            whisper_model = str(Path(whisper_model).expanduser())

        profile = os.getenv("COOKIES_PROFILE") or None

        return cls(
            panopto_host=os.getenv("PANOPTO_HOST", "uw.hosted.panopto.com"),
            cookies_browser=os.getenv("COOKIES_BROWSER", "chrome"),
            cookies_profile=profile,
            canvas_url=os.getenv("CANVAS_URL", "https://canvas.uw.edu"),
            canvas_token=os.getenv("CANVAS_TOKEN", ""),
            download_dir=download_dir,
            transcript_dir=transcript_dir,
            transcriber_backend=os.getenv("TRANSCRIBER_BACKEND", "whisper-cpp"),
            transcriber_model=os.getenv("TRANSCRIBER_MODEL", "base.en"),
            whisper_cpp_model_path=whisper_model,
            whisper_cpp_bin=os.getenv("WHISPER_CPP_BIN", "whisper-cli"),
        )
