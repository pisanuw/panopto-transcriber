# panopto-transcriber

Download Panopto-hosted lecture recordings and transcribe them locally with Whisper.

## How auth works (no Panopto API needed)

UW-IT does not hand out Panopto OAuth API credentials, so we use a workaround: **`yt-dlp` reads your browser's existing SSO cookies** and uses them to download videos. As long as you're signed into Panopto in Chrome (or Safari/Firefox), downloads just work. The session expires occasionally — re-log-in in your browser and try again.

On machines without a browser (headless servers, CI), set `COOKIES_FILE` to a Netscape-format cookies file exported from a desktop run — see [Headless servers](#headless-servers-no-browser) below.

The Canvas REST API works with a personal access token (`CANVAS_TOKEN` in `.env`), so course enumeration and Panopto-folder discovery use that.

## What it does

1. yt-dlp downloads the MP4 (or HLS-muxed MP4) using your browser cookies (or a cookies file)
2. Whisper transcribes it locally — `whisper.cpp` (default, faster) or `openai-whisper` (Python)
3. Works for single sessions, whole course folders, or a streaming download → transcribe → delete loop for disk-constrained machines
4. Auto-discovers Panopto folders for every Canvas course you're in (via Playwright), and the multi-course pipeline coordinates across up to ~10 machines via shared-FS lockfiles

### Recommended workflow

Most users want all their Canvas courses transcribed end-to-end:

```bash
uv run panopto-transcriber list-courses --out courses.yml          # one-time per quarter
uv run panopto-transcriber discover-folders courses.yml --skip-archived  # fills in folder GUIDs
uv run panopto-transcriber run-courses courses.yml                 # download → transcribe → delete each
```

Steps 1 and 2 take seconds-to-minutes; step 3 is the long-running pipeline. See [List your Canvas courses](#list-your-canvas-courses) for details on each.

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

The mapping is the bottleneck. Two ways to fill it in:

**Auto-discover (recommended)** — drive Chromium through Canvas's LTI launch for each course and read the Panopto folder GUID off the resulting page. Courses without a Panopto tab are silently skipped:

```bash
uv sync --extra discover                     # one-time: pulls Playwright
uv run playwright install chromium           # one-time: pulls the browser binary
uv run panopto-transcriber discover-folders courses.yml
# Helpful flags:
#   --skip-archived  → ignore "ARCHIVED: …" courses
#   --headed         → show the browser (debug or first-time MFA)
```

The script edits `courses.yml` in place; comments and key order are preserved. Re-running is safe: entries that already have `panopto_folder` set are left alone.

**Manual** — open each course's "Panopto Course Videos" tab, copy the folder URL/GUID, paste it into `panopto_folder:` yourself.

#### Sanity check: how many videos per course?

Before kicking off a long batch, see how many sessions each course folder actually has — active vs. archived:

```bash
uv run panopto-transcriber inventory courses.yml                  # default: only courses with panopto_folder set
uv run panopto-transcriber inventory courses.yml --skip-archived  # ignore "ARCHIVED:" courses
uv run panopto-transcriber inventory courses.yml --all            # include entries without a folder, as "-"
```

Output looks like:

```
CODE                   TERM            ACTIVE ARCHIVED  NAME
----------------------------------------------------------------------
CSS 343 D              Winter 2026         15        0  CSS 343 D Wi 26: Data Structures…
CSS 430 A              Winter 2026         12        2  CSS 430 A Wi 26: Operating Systems
ARCHIVED: CSS 422 B    Autumn 2024         17        0  ARCHIVED: CSS 422 B Au 24: …
----------------------------------------------------------------------
TOTAL                                      44        2  (46 sessions across 3 folder(s))
```

Counts come from Panopto's `Services/Data.svc/GetSessions` endpoint using your existing browser cookies — same as the web UI, so they always match what you'd see by clicking around.

Optional per-entry `out_dir: "..."` overrides the transcript subdir (default: `<code>_<term>` slugified, e.g., `css_143_d_winter_2026`).

Then process every course that has a `panopto_folder` set:

```bash
uv run panopto-transcriber run-courses courses.yml
# or, to keep media files instead of deleting after transcribing:
uv run panopto-transcriber run-courses courses.yml --keep-media
```

This loops the streaming download → transcribe → delete pipeline over each course, writing transcripts to `<TRANSCRIPT_DIR>/<subdir>/`. Per-course failures (typo'd GUID, no access) are logged and the batch continues to the next course; cookie/auth expiry aborts the whole batch so you don't burn through every course with bad credentials.

While running, you'll see a banner per course, a per-session progress line from `run_folder_streaming` (`[3/15] done in 3m21s. ETA: 42m08s …`), a per-course footer with the rolling batch ETA, and a final BATCH DONE summary:

```
======================================================================
COURSE [3/11]  CSS 343 A
  term:        Autumn 2025
  panopto:     1f45b165-7819-…-b6b8-b2c601266e47
  transcripts: /Users/.../transcripts/css_343_a_autumn_2025
  progress:    2 processed by me, 0 taken by peers, 0 other
  batch ETA:   ~32m12s (finish ~14:47); avg 16m06s/course over 2 so far
======================================================================
```

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

### Export auth tokens for a headless server

`dump-tokens` writes the `.tokens/` helper files (see [Headless servers](#headless-servers-no-browser)) without downloading anything. Run it on a machine that is signed in to Panopto in its browser:

```bash
uv run panopto-transcriber dump-tokens
# → writes .tokens/panopto_cookies.txt (Netscape), .tokens/panopto.txt, .tokens/canvas.txt
```

The `download`/`run`/`run-folder`/`run-courses` commands already do this automatically before each run, so you only need `dump-tokens` when preparing cookies to copy to a server.

## Housekeeping

Two commands help keep a large transcript tree tidy after many runs. Both are **dry-run by default** — they only report until you pass an explicit apply/move/delete flag.

### Find stranded transcripts (`verify-transcripts`)

Over time a Panopto folder can lose sessions it once held (course re-orgs, template cleanup). Their transcripts stay on disk as "orphans". `verify-transcripts` lists, per course in `courses.yml`, every transcript whose embedded session GUID is no longer in the folder:

```bash
uv run panopto-transcriber verify-transcripts courses.yml                       # dry-run report
uv run panopto-transcriber verify-transcripts courses.yml --move-orphans-to quarantine/  # quarantine them
uv run panopto-transcriber verify-transcripts courses.yml --delete-orphans      # permanently delete
```

`--move-orphans-to` (safer) and `--delete-orphans` are mutually exclusive.

### Re-file misplaced transcripts by calendar (`match-orphans-to-calendar`)

If transcripts ended up in the wrong (or no) course subdir, this re-files them using a Google Calendar `.ics` export: it parses the recording timestamp from each filename, finds the class event on that day, and maps it to a course in `courses.yml`.

```bash
# dry-run report (no files moved):
uv run panopto-transcriber match-orphans-to-calendar orphans/ calendar.ics courses.yml
# actually move the matched files:
uv run panopto-transcriber match-orphans-to-calendar orphans/ calendar.ics courses.yml --apply
# show every unmatched file with nearby events so you can resolve by hand:
uv run panopto-transcriber match-orphans-to-calendar orphans/ calendar.ics courses.yml --print-unmatched
```

Useful flags: `--target-dir` (where matched files go; defaults to `TRANSCRIPT_DIR`) and `--time-window-minutes` (how close a recording must be to a calendar event to match; default 120). When the calendar event omits the section letter, a match only succeeds if exactly one section with that number ran that quarter; otherwise the file is left alone and reported as ambiguous.

## Troubleshooting

- **`Panopto download failed — your browser session may have expired`** — open Panopto in your browser, sign in, retry.
- **`could not find chrome cookies database`** — either your Chrome profile path is non-default (set `COOKIES_PROFILE` to the profile folder name, e.g., `Profile 1`), or you're on a machine with no browser at all (see [Headless servers](#headless-servers-no-browser)).
- **`This video is only available for registered users`** — `COOKIES_PROFILE` points at a Chrome profile that isn't signed in to Panopto. yt-dlp's Panopto extractor doesn't follow SSO redirects, so 0 `*.panopto.com` cookies in the chosen profile means the download fails even though the video plays in your browser. Open `https://uw.hosted.panopto.com` in the configured profile, sign in, retry — or change `COOKIES_PROFILE` to whichever profile actually has Panopto cookies (often just blank/`Default`).
- **`discover-folders` fails / hangs on every course** — most commonly the cookies extracted from Chrome don't carry over to Playwright cleanly (SameSite issues, MFA prompt). Re-run with `--headed` once; complete any SSO/MFA in the visible window; the session cookies set during that run carry through the rest of the loop. If a specific course consistently fails ("no folderID in launched page"), open its Panopto tab manually in Canvas and paste the GUID into `courses.yml` by hand.
- **`std::filesystem::__cxx11::directory_iterator` linker error when building whisper.cpp** — your system GCC is older than 9.x and `libstdc++fs` isn't linked. Either build with a newer GCC (`module load gcc/11`) or pass `cmake -B build -DCMAKE_EXE_LINKER_FLAGS="-lstdc++fs" -DCMAKE_SHARED_LINKER_FLAGS="-lstdc++fs"`. Alternatively skip whisper.cpp and use `TRANSCRIBER_BACKEND=openai-whisper`.
- **Safari: `permission denied`** — give Terminal/your IDE Full Disk Access (see above).
- **No audio in output** — yt-dlp downloaded an audio-less HLS variant. Already mitigated by `format: bestvideo*+bestaudio/best`; if you still hit it, run `yt-dlp -F <url>` to inspect available formats.

## Layout

```
src/panopto_transcriber/
├── cli.py                  # click-based entry point (all subcommands)
├── config.py               # env loading
├── downloader.py           # yt-dlp wrapper: single session or whole folder
├── batch.py                # transcribe a directory; streaming download→transcribe→delete loop
├── tokens.py               # dump Canvas token + Panopto cookies to .tokens/
├── canvas.py               # minimal Canvas REST client (list courses for now)
├── claim.py                # cross-machine course-level lockfile coordination
├── discover.py             # auto-fill panopto_folder via Playwright (LTI launch)
├── inventory.py            # count active/archived sessions per folder (Panopto Data.svc)
├── match_calendar.py       # match-orphans-to-calendar: re-file transcripts via an .ics export
├── verify.py               # verify-transcripts: detect orphan transcripts not in the folder
├── _progress.py            # human-readable duration/progress formatting
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
- [x] Auto-discover Panopto folder GUID per Canvas course (`discover-folders` via Playwright LTI launch)
- [ ] Optional: download Panopto auto-captions as a "free" transcription source
