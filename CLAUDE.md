# CLAUDE.md

Guidance for working in this repository.

## What this is

A single-author Python CLI that downloads Panopto-hosted lecture recordings
(via the user's browser SSO cookies, using `yt-dlp` — there is no Panopto API)
and transcribes them locally with Whisper (`whisper.cpp` or `openai-whisper`).
It also enumerates Canvas courses, auto-discovers Panopto folder GUIDs, and
coordinates batch runs across multiple machines over a shared filesystem.

Entry point: the `panopto-transcriber` console script → `cli.main` (a Click
group). See the README for end-user usage.

## Environment & commands

Uses [`uv`](https://docs.astral.sh/uv/). Python 3.11+.

```bash
uv sync --dev            # install base deps + dev tools (ruff, mypy, pytest)
uv run pytest -q         # run the test suite
uv run ruff check .      # lint
uv run mypy              # typecheck (config in pyproject.toml)
uv run panopto-transcriber --help
```

Optional extras (heavy; not needed for tests/lint/type):
`uv sync --extra openai-whisper` and `uv sync --extra discover` (Playwright).

CI (`.github/workflows/ci.yml`) runs lint + typecheck + test on 3.11/3.12,
builds the wheel/sdist, and runs a gitleaks secret scan, on every push and PR.
Keep all of these green.

## Module map

- `cli.py` — all Click subcommands; thin glue over the modules below.
- `config.py` — `Config.load()` reads `.env`; the single source of settings.
- `downloader.py` — `yt-dlp` wrapper (single session / whole folder).
- `batch.py` — transcribe a directory; streaming download→transcribe→delete.
- `canvas.py` — minimal Canvas REST client + `courses.yml` parsing.
- `discover.py` — fill `panopto_folder` GUIDs by driving the Canvas `/tabs`
  LTI launch through Playwright.
- `claim.py` — cross-machine course-level lockfiles (`O_CREAT|O_EXCL` over NFS).
- `inventory.py` — count active/archived sessions per folder.
- `match_calendar.py` / `verify.py` — transcript housekeeping commands.
- `transcribers/` — pluggable Whisper backends behind a `Transcriber` protocol.

## Conventions

- **Logging, not print.** Operational modules log via `logging.getLogger(__name__)`.
  Logging is configured once in `cli.main`. Use `logger.info` for progress and
  `logger.error` only for actual failures — do not use `error` for summaries.
- **Path safety.** Any user-supplied output subdir goes through
  `cli._resolve_subdir`, which rejects absolute paths and `..`/symlink escapes.
  Keep it that way; it is covered by `tests/test_resolve_subdir.py`.
- **No secrets or institutional data in git.** `.env`, `.tokens/`, `*.asc`, and
  the generated `courses.yml` (real Canvas IDs + Panopto GUIDs) are gitignored.
  Commit `courses.example.yml` instead. The gitleaks CI job guards this.
- **Tests.** Add/extend tests under `tests/` for any change to the path guard,
  the claim/locking logic, the filename date parsers, or the YAML rewriter.
- **Commits.** Use Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`,
  `chore:`) with a one-line scope, e.g. `fix(downloader): ...`.
