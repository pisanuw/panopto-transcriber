# panopto-transcriber

Download Panopto-hosted lecture recordings and transcribe them locally with Whisper.

## How auth works (no Panopto API needed)

UW-IT does not hand out Panopto OAuth API credentials, so we use a workaround: **`yt-dlp` reads your browser's existing SSO cookies** and uses them to download videos. As long as you're signed into Panopto in Chrome (or Safari/Firefox), downloads just work. The session expires occasionally — re-log-in in your browser and try again.

On machines without a browser (headless servers, CI), set `COOKIES_FILE` to a Netscape-format cookies file exported from a desktop run — see [Headless servers](#headless-servers-no-browser) below.

The Canvas REST API does work with a personal access token (`CANVAS_TOKEN` in `.env`), so course discovery (next milestone) will use that.

## What it does

1. yt-dlp downloads the MP4 (or HLS-muxed MP4) using your browser cookies (or a cookies file)
2. Whisper transcribes it locally — `whisper.cpp` (default, faster) or `openai-whisper` (Python)
3. Works for single sessions, whole course folders, or a streaming download → transcribe → delete loop for disk-constrained machines

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
# edit .env: set COOKIES_BROWSER if not Chrome, set WHISPER_CPP_MODEL_PATH,
# optionally CANVAS_TOKEN (for upcoming course discovery),
# optionally COOKIES_FILE for headless servers (see below).
```

### Make sure your browser cookies work

1. Open Chrome (or whichever browser you set in `COOKIES_BROWSER`)
2. Sign in to `https://uw.hosted.panopto.com/` — make sure you can play a video
3. **Safari only:** in System Settings → Privacy & Security → Full Disk Access, enable Terminal (or your IDE)

### Headless servers (no browser)

If the machine that runs `panopto-transcriber` has no browser (a Linux server, CI), you can't read cookies from a browser there. Workflow:

1. **On your desktop** (signed into Panopto in your browser), run any download command — e.g. `uv run panopto-transcriber download <session>`. This writes `.tokens/panopto_cookies.txt` (Netscape format, yt-dlp-compatible) alongside a few other helpers under `.tokens/`.
2. **Copy that file to the server**, e.g. `scp .tokens/panopto_cookies.txt server:~/panopto_cookies.txt`.
3. **On the server**, set `COOKIES_FILE=~/panopto_cookies.txt` in `.env`. When `COOKIES_FILE` is set, `COOKIES_BROWSER` and `COOKIES_PROFILE` are ignored.
4. Re-export and re-copy when your Panopto session expires.

The `.tokens/` directory is gitignored and contains:

| File | Contents |
|---|---|
| `canvas.txt` | The `CANVAS_TOKEN` value from `.env`, copyable into curl etc. |
| `panopto_cookies.txt` | Netscape-format cookies for `yt-dlp` / `curl -b` |
| `panopto.txt` | `Cookie: name=value; ...` header string |

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

### List your Canvas courses

To pick which courses to process, dump a list of the courses your `CANVAS_TOKEN` can see:

```bash
uv run panopto-transcriber list-courses                  # active enrollments (default)
uv run panopto-transcriber list-courses --state all      # everything past + present

# Also write a starter YAML you can fill in with Panopto folder GUIDs:
uv run panopto-transcriber list-courses --out courses.yml
```

`courses.yml` will look like:

```yaml
courses:
  - canvas_id: 1234567
    name: "CSE 143 — Computer Programming II"
    code: "CSE 143"
    term: "Spring 2026"
    panopto_folder: ""  # TODO  ← paste the folder GUID or URL here
```

The mapping is a manual step today: open each course's "Panopto Course Videos" tab, copy the folder URL/GUID, paste it in. Optional per-entry `out_dir: "..."` overrides the transcript subdir (default: `<code>_<term>` slugified, e.g., `css_143_d_winter_2026`).

Then process every course that has a `panopto_folder` set:

```bash
uv run panopto-transcriber run-courses courses.yml
# or, to keep media files instead of deleting after transcribing:
uv run panopto-transcriber run-courses courses.yml --keep-media
```

This loops the streaming download → transcribe → delete pipeline over each course, writing transcripts to `<TRANSCRIPT_DIR>/<subdir>/`. Per-course failures (typo'd GUID, no access) are logged and the batch continues to the next course; cookie/auth expiry aborts the whole batch so you don't burn through every course with bad credentials.

#### Running on multiple machines in parallel

If `TRANSCRIPT_DIR` lives on shared storage (NFS-mounted home dir on the UW CSS lab, etc.), you can run `run-courses` on up to ~10 machines pointed at the same `courses.yml` and they'll split the work without overlapping:

```bash
# on each machine — same command, same courses.yml:
uv run panopto-transcriber run-courses courses.yml
```

How it works:
- For each course, the worker tries to atomically create `<TRANSCRIPT_DIR>/<subdir>/.claim` (via `O_CREAT|O_EXCL`, which NFSv3/v4 honor). The winner owns that course; the loser skips it and moves on to the next.
- A background thread touches the claim's mtime every 60s (`--heartbeat`) so peers can tell the worker is alive.
- If a worker crashes mid-course, its claim file gets stale. Any other worker reaching that course after `--stale-after` seconds (default 30 min) reclaims and re-processes it. Already-transcribed sessions inside the course are skipped (transcript-exists check), so reclaim is cheap.

Constraints to know about:
- Coordination is per-course, not per-session. With 10 machines and 5 courses, 5 machines sit idle.
- Each machine needs its own `DOWNLOAD_DIR` (set in its `.env`), or use `/tmp/...` locally — otherwise yt-dlp's archive file gets contested.
- The same Panopto cookies file works for all machines; just `scp` it once. If your Panopto session expires, every machine hits the auth-failure abort, so refresh cookies and re-run.

Tuning knobs:
```bash
# More aggressive stale-reclaim if courses are short:
uv run panopto-transcriber run-courses courses.yml --stale-after 600 --heartbeat 30
# Keep media files (e.g., for debugging or re-transcription):
uv run panopto-transcriber run-courses courses.yml --keep-media
```

To force a re-run of a course another worker thinks is in progress, delete its `.claim` file:
```bash
rm transcripts/<subdir>/.claim
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

# streaming mode: download → transcribe → delete each media file before
# moving on (one video on disk at a time). Useful on disk-constrained servers:
uv run panopto-transcriber run-folder "<folder-url-or-guid>" --delete-after

# organize transcripts per course under TRANSCRIPT_DIR (relative path; no '..'):
uv run panopto-transcriber run-folder "<folder-url-or-guid>" --delete-after --out-dir cse143
# → transcripts land in <TRANSCRIPT_DIR>/cse143/
```

Reruns are idempotent: yt-dlp tracks completed downloads in `<DOWNLOAD_DIR>/.yt-dlp-archive.txt`, and transcription skips any media that already has a `.txt` in `TRANSCRIPT_DIR`. Safe to re-run after new lectures are posted. With `--delete-after`, the archive still records each session, so a re-run won't re-download anything that's already been transcribed.

## Troubleshooting

- **`Panopto download failed — your browser session may have expired`** — open Panopto in your browser, sign in, retry.
- **`could not find chrome cookies database`** — either your Chrome profile path is non-default (set `COOKIES_PROFILE` to the profile folder name, e.g., `Profile 1`), or you're on a machine with no browser at all (see [Headless servers](#headless-servers-no-browser)).
- **`std::filesystem::__cxx11::directory_iterator` linker error when building whisper.cpp** — your system GCC is older than 9.x and `libstdc++fs` isn't linked. Either build with a newer GCC (`module load gcc/11`) or pass `cmake -B build -DCMAKE_EXE_LINKER_FLAGS="-lstdc++fs" -DCMAKE_SHARED_LINKER_FLAGS="-lstdc++fs"`. Alternatively skip whisper.cpp and use `TRANSCRIBER_BACKEND=openai-whisper`.
- **Safari: `permission denied`** — give Terminal/your IDE Full Disk Access (see above).
- **No audio in output** — yt-dlp downloaded an audio-less HLS variant. Already mitigated by `format: bestvideo*+bestaudio/best`; if you still hit it, run `yt-dlp -F <url>` to inspect available formats.

## Layout

```
src/panopto_transcriber/
├── cli.py                  # click-based entry point
├── config.py               # env loading
├── downloader.py           # yt-dlp wrapper: single session or whole folder
├── batch.py                # transcribe a directory; streaming download→transcribe→delete loop
├── tokens.py               # dump Canvas token + Panopto cookies to .tokens/
├── canvas.py               # minimal Canvas REST client (list courses for now)
├── claim.py                # cross-machine course-level lockfile coordination
└── transcribers/
    ├── base.py             # Transcriber protocol
    ├── whisper_cpp.py      # subprocess to whisper-cli
    └── openai_whisper.py   # import whisper
```

## Roadmap

- [x] Batch download + transcribe an entire Panopto folder
- [x] Streaming mode (`--delete-after`) for disk-constrained machines
- [x] Canvas client: list courses + emit a starter `courses.yml`
- [x] `panopto-transcriber run-courses courses.yml` — loop `run-folder --delete-after` over every entry
- [x] Multi-machine parallel execution via shared-FS lockfiles (up to ~10 workers)
- [ ] Auto-discover Panopto folder GUID per Canvas course (so `courses.yml` doesn't need manual entry)
- [ ] Optional: download Panopto auto-captions as a "free" transcription source
