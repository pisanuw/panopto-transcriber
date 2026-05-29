# panopto-transcriber

Download Panopto-hosted lecture recordings and transcribe them locally with Whisper.

## How auth works (no Panopto API needed)

UW-IT does not hand out Panopto OAuth API credentials, so we use a workaround: **`yt-dlp` reads your browser's existing SSO cookies** and uses them to download videos. As long as you're signed into Panopto in Chrome (or Safari/Firefox), downloads just work. The session expires occasionally — re-log-in in your browser and try again.

The Canvas REST API does work with a personal access token, so course discovery (next milestone) will use that.

## Status: vertical slice

This first cut works for **one known Panopto session at a time**:

1. yt-dlp downloads the MP4 (or HLS-muxed MP4) using your browser cookies
2. Whisper transcribes it locally — `whisper.cpp` (default, faster) or `openai-whisper` (Python)

## Install

Requires Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and `ffmpeg`.

```bash
brew install uv ffmpeg
# pick at least one transcription backend:
brew install whisper-cpp         # for the whisper-cpp backend (recommended)
# and/or `uv sync --extra openai-whisper` below for the Python backend (~2GB)

git clone <this repo>
cd panopto-transcriber
uv sync                          # base deps (includes yt-dlp)
uv sync --extra openai-whisper   # optional Python whisper backend
```

### Whisper.cpp model file

`whisper.cpp` needs a `ggml-*.bin` model file:

```bash
mkdir -p models/whisper
curl -L -o models/whisper/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

## Configure

```bash
cp .env.example .env
# edit .env: set COOKIES_BROWSER if not Chrome, set WHISPER_CPP_MODEL_PATH
```

### Make sure your browser cookies work

1. Open Chrome (or whichever browser you set in `COOKIES_BROWSER`)
2. Sign in to `https://uw.hosted.panopto.com/` — make sure you can play a video
3. **Safari only:** in System Settings → Privacy & Security → Full Disk Access, enable Terminal (or your IDE)

## Usage

### Download one session

Grab a session ID from a viewer URL (`...Viewer.aspx?id=<SESSION_ID>`) — or paste the whole URL.

```bash
uv run panopto-transcriber download 12345678-abcd-1234-abcd-1234567890ab
# or
uv run panopto-transcriber download "https://uw.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=12345678-..."
```

### Transcribe a local file

```bash
uv run panopto-transcriber transcribe ~/Downloads/lecture.mp4
# override backend or model:
uv run panopto-transcriber transcribe ~/Downloads/lecture.mp4 \
  --backend openai-whisper --model small.en
```

### End-to-end (single session)

```bash
uv run panopto-transcriber run 12345678-abcd-1234-abcd-1234567890ab
```

### Whole course (batch)

Find the Panopto **folder** URL for the course once: in Canvas, click "Panopto Course Videos" in the left nav, then the "Open in Panopto" arrow at top right of the embedded view. The resulting URL looks like:

```
https://uw.hosted.panopto.com/Panopto/Pages/Sessions/List.aspx?folderID=<FOLDER_GUID>
```

Pass either the folder GUID or the whole URL:

```bash
# download every session in the folder (skips already-downloaded):
uv run panopto-transcriber download-folder "<folder-url-or-guid>"

# transcribe every media file in DOWNLOAD_DIR (skips already-transcribed):
uv run panopto-transcriber transcribe-dir

# both, in one command:
uv run panopto-transcriber run-folder "<folder-url-or-guid>"
```

Reruns are idempotent: yt-dlp tracks completed downloads in `<DOWNLOAD_DIR>/.yt-dlp-archive.txt`, and transcription skips any media that already has a `.txt` in `TRANSCRIPT_DIR`. Safe to re-run after new lectures are posted.

## Troubleshooting

- **`Panopto download failed — your browser session may have expired`** — open Panopto in your browser, sign in, retry.
- **`could not find chrome cookies database`** — your Chrome profile path is non-default. Set `COOKIES_PROFILE` to the profile folder name (e.g., `Profile 1`).
- **Running on a headless server (no browser installed)** — run any download command on your desktop first; this writes `.tokens/panopto_cookies.txt` (Netscape format). Copy that file to the server and set `COOKIES_FILE=/path/to/panopto_cookies.txt` in the server's `.env`. yt-dlp will use the file instead of the browser. Re-export when the session expires.
- **Safari: `permission denied`** — give Terminal/your IDE Full Disk Access (see above).
- **No audio in output** — yt-dlp downloaded an audio-less HLS variant. Already mitigated by `format: bestvideo*+bestaudio/best`; if you still hit it, run `yt-dlp -F <url>` to inspect available formats.

## Layout

```
src/panopto_transcriber/
├── cli.py                  # click-based entry point
├── config.py               # env loading
├── downloader.py           # yt-dlp wrapper: single session or whole folder
├── batch.py                # iterate a directory, transcribe each file, skip done
└── transcribers/
    ├── base.py             # Transcriber protocol
    ├── whisper_cpp.py      # subprocess to whisper-cli
    └── openai_whisper.py   # import whisper
```

## Roadmap

- [x] Batch download + transcribe an entire Panopto folder
- [ ] Canvas client: given a Canvas course ID, auto-discover the Panopto folder
- [ ] `panopto-transcriber run-course <canvas-course-id>` as a thin wrapper
- [ ] Optional: download Panopto auto-captions as a "free" transcription source
