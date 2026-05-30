from __future__ import annotations

import re
from pathlib import Path

import click
import httpx

from .batch import run_folder_streaming, transcribe_directory
from .canvas import CourseEntry, list_courses, load_courses_yaml
from .claim import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_STALE_SECONDS,
    Heartbeat,
    release,
    try_claim,
)
from .config import Config
from .downloader import download_folder, download_session
from .tokens import save_canvas_token, save_panopto_cookies
from .transcribers import get_transcriber


def _dump_tokens(cfg: Config) -> None:
    """Persist Canvas token and Panopto browser cookies to `.tokens/` for manual reuse.

    Skips browser-cookie extraction when `COOKIES_FILE` is set, since on a
    headless server there is no browser to read from.
    """
    canvas_path = save_canvas_token(cfg.canvas_token)
    if canvas_path:
        click.echo(f"Canvas token saved: {canvas_path}")
    if cfg.cookies_file:
        click.echo(f"Using Panopto cookies file: {cfg.cookies_file}")
        return
    try:
        panopto_path = save_panopto_cookies(
            cfg.cookies_browser, cfg.cookies_profile, cfg.panopto_host
        )
    except Exception as e:
        click.echo(f"Could not extract Panopto cookies: {e}", err=True)
        return
    if panopto_path:
        click.echo(f"Panopto cookies saved: {panopto_path}")
    else:
        profile = cfg.cookies_profile or "Default"
        # yt-dlp's Panopto extractor calls /Pages/Viewer/DeliveryInfo.aspx
        # directly and needs a `.panopto.com` session cookie — it does NOT
        # follow SSO redirects the way a browser does. So zero panopto.com
        # cookies in the chosen profile = download will fail with "only
        # available for registered users". Surface this clearly.
        click.echo(
            f"WARNING: no *.panopto.com cookies in {cfg.cookies_browser} "
            f"({profile}). yt-dlp can't follow SSO redirects, so this download "
            "will almost certainly fail.\n"
            f"  Fix: open {cfg.cookies_browser} ({profile}) → "
            f"https://{cfg.panopto_host} → sign in → play any video, then retry.\n"
            f"  Or: switch COOKIES_PROFILE in .env to a profile already signed in to Panopto.",
            err=True,
        )


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _default_course_subdir(entry: CourseEntry) -> str:
    parts = []
    if entry.code:
        parts.append(_slugify(entry.code))
    if entry.term:
        parts.append(_slugify(entry.term))
    return "_".join(p for p in parts if p) or f"course_{entry.canvas_id or 'unknown'}"


def _resolve_subdir(base: Path, sub: str | None) -> Path:
    """Resolve `<base>/<sub>`, refusing absolute paths or any path that escapes `base`."""
    if not sub:
        return base
    p = Path(sub)
    if p.is_absolute() or ".." in p.parts:
        raise click.BadParameter(
            f"must be a relative path with no '..' segments, got {sub!r}",
            param_hint="--out-dir",
        )
    target = (base / p).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as e:
        raise click.BadParameter(
            f"{sub!r} resolves outside {base}", param_hint="--out-dir"
        ) from e
    target.mkdir(parents=True, exist_ok=True)
    return target


def _make_transcriber(cfg: Config, backend: str | None, model: str | None):
    backend = backend or cfg.transcriber_backend
    model = model or cfg.transcriber_model
    if backend == "whisper-cpp":
        return get_transcriber(
            backend,
            model,
            model_path=cfg.whisper_cpp_model_path,
            bin_path=cfg.whisper_cpp_bin,
        )
    return get_transcriber(backend, model)


_BACKEND = click.option(
    "--backend",
    type=click.Choice(["whisper-cpp", "openai-whisper"]),
    default=None,
    help="Override TRANSCRIBER_BACKEND from .env.",
)
_MODEL = click.option("--model", default=None, help="Override TRANSCRIBER_MODEL from .env.")


@click.group()
def main() -> None:
    """Download Panopto recordings and transcribe them locally."""


# ---- single-session commands ------------------------------------------------

@main.command()
@click.argument("session_or_url")
def download(session_or_url: str) -> None:
    """Download one Panopto session by ID or viewer URL."""
    cfg = Config.load()
    _dump_tokens(cfg)
    out = download_session(
        session_or_url,
        cfg.download_dir,
        panopto_host=cfg.panopto_host,
        cookies_browser=cfg.cookies_browser,
        cookies_profile=cfg.cookies_profile,
        cookies_file=cfg.cookies_file,
    )
    click.echo(f"Saved: {out}")


@main.command()
@click.argument("media_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_BACKEND
@_MODEL
def transcribe(media_path: Path, backend: str | None, model: str | None) -> None:
    """Transcribe a single local media file."""
    cfg = Config.load()
    t = _make_transcriber(cfg, backend, model)
    click.echo(f"Transcribing with {t.name} (model={model or cfg.transcriber_model})...")
    result = t.transcribe(media_path, cfg.transcript_dir)
    click.echo(f"Text: {result.text_path}")
    if result.srt_path:
        click.echo(f"SRT:  {result.srt_path}")


@main.command()
@click.argument("session_or_url")
@_BACKEND
@_MODEL
def run(session_or_url: str, backend: str | None, model: str | None) -> None:
    """Download one session and transcribe it end-to-end."""
    cfg = Config.load()
    _dump_tokens(cfg)
    media = download_session(
        session_or_url,
        cfg.download_dir,
        panopto_host=cfg.panopto_host,
        cookies_browser=cfg.cookies_browser,
        cookies_profile=cfg.cookies_profile,
        cookies_file=cfg.cookies_file,
    )
    t = _make_transcriber(cfg, backend, model)
    click.echo(f"Transcribing with {t.name}...")
    result = t.transcribe(media, cfg.transcript_dir)
    click.echo(f"Text: {result.text_path}")
    if result.srt_path:
        click.echo(f"SRT:  {result.srt_path}")


# ---- batch / folder commands ------------------------------------------------

@main.command("download-folder")
@click.argument("folder_or_url")
def download_folder_cmd(folder_or_url: str) -> None:
    """Download every session in a Panopto course folder.

    FOLDER_OR_URL is either a folder GUID or a full Sessions/List.aspx URL.
    Already-downloaded sessions are skipped (yt-dlp archive in DOWNLOAD_DIR).
    """
    cfg = Config.load()
    _dump_tokens(cfg)
    paths = download_folder(
        folder_or_url,
        cfg.download_dir,
        panopto_host=cfg.panopto_host,
        cookies_browser=cfg.cookies_browser,
        cookies_profile=cfg.cookies_profile,
        cookies_file=cfg.cookies_file,
    )
    click.echo(f"Downloaded {len(paths)} new file(s) to {cfg.download_dir}")


@main.command("transcribe-dir")
@click.argument(
    "in_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=False,
)
@_BACKEND
@_MODEL
def transcribe_dir_cmd(in_dir: Path | None, backend: str | None, model: str | None) -> None:
    """Transcribe every media file in IN_DIR (defaults to DOWNLOAD_DIR).

    Files that already have a .txt next to them in TRANSCRIPT_DIR are skipped.
    """
    cfg = Config.load()
    target = in_dir or cfg.download_dir
    t = _make_transcriber(cfg, backend, model)
    click.echo(f"Transcribing {target} with {t.name}...")
    results = transcribe_directory(target, t, cfg.transcript_dir)
    click.echo(f"Done. {len(results)} new transcript(s) in {cfg.transcript_dir}")


@main.command("run-folder")
@click.argument("folder_or_url")
@click.option(
    "--delete-after",
    is_flag=True,
    default=False,
    help="Stream one session at a time: download → transcribe → delete the "
    "media file before moving on. Useful on disk-constrained machines.",
)
@click.option(
    "--out-dir",
    "out_dir",
    default=None,
    metavar="SUBDIR",
    help="Write transcripts under <TRANSCRIPT_DIR>/<SUBDIR>/ instead of <TRANSCRIPT_DIR>/. "
    "Relative path only; handy for organizing transcripts per course (e.g. --out-dir cse143).",
)
@_BACKEND
@_MODEL
def run_folder_cmd(
    folder_or_url: str,
    delete_after: bool,
    out_dir: str | None,
    backend: str | None,
    model: str | None,
) -> None:
    """Download all sessions in a Panopto folder, then transcribe them all.

    With ``--delete-after``, switches to a streaming flow that downloads,
    transcribes, and deletes each media file one at a time.
    """
    cfg = Config.load()
    _dump_tokens(cfg)
    t = _make_transcriber(cfg, backend, model)
    transcript_dir = _resolve_subdir(cfg.transcript_dir, out_dir)

    if delete_after:
        click.echo(
            f"Streaming folder through {t.name}: "
            f"download → transcribe → delete, one session at a time. "
            f"Transcripts → {transcript_dir}"
        )
        results = run_folder_streaming(
            folder_or_url,
            cfg.download_dir,
            transcript_dir,
            t,
            panopto_host=cfg.panopto_host,
            cookies_browser=cfg.cookies_browser,
            cookies_profile=cfg.cookies_profile,
            cookies_file=cfg.cookies_file,
            delete_media=True,
        )
        click.echo(f"Done. {len(results)} new transcript(s) in {transcript_dir}")
        return

    click.echo(f"Downloading folder to {cfg.download_dir}...")
    download_folder(
        folder_or_url,
        cfg.download_dir,
        panopto_host=cfg.panopto_host,
        cookies_browser=cfg.cookies_browser,
        cookies_profile=cfg.cookies_profile,
        cookies_file=cfg.cookies_file,
    )
    click.echo(f"Transcribing all media in {cfg.download_dir} with {t.name}. Transcripts → {transcript_dir}")
    results = transcribe_directory(cfg.download_dir, t, transcript_dir)
    click.echo(f"Done. {len(results)} new transcript(s) in {transcript_dir}")


@main.command("run-courses")
@click.argument(
    "courses_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--keep-media",
    is_flag=True,
    default=False,
    help="Keep downloaded media files after transcription. Default: delete "
    "each file after its transcript is written (good for disk-constrained machines).",
)
@click.option(
    "--stale-after",
    type=int,
    default=DEFAULT_STALE_SECONDS,
    show_default=True,
    metavar="SECONDS",
    help="Reclaim a peer worker's lock if its heartbeat is older than this. "
    "Lower = faster recovery from crashed workers; higher = safer for slow courses.",
)
@click.option(
    "--heartbeat",
    type=int,
    default=DEFAULT_HEARTBEAT_SECONDS,
    show_default=True,
    metavar="SECONDS",
    help="How often the lock's mtime is touched while a course is being processed.",
)
@_BACKEND
@_MODEL
def run_courses_cmd(
    courses_yaml: Path,
    keep_media: bool,
    stale_after: int,
    heartbeat: int,
    backend: str | None,
    model: str | None,
) -> None:
    """Process every course in COURSES_YAML that has `panopto_folder` set.

    Loops the streaming download → transcribe → (delete) pipeline over each
    course. Transcripts land in a per-course subdir under TRANSCRIPT_DIR,
    auto-derived from the entry's `code` + `term` unless the entry specifies
    its own `out_dir`.

    Safe to run on multiple machines that share TRANSCRIPT_DIR (e.g. NFS):
    each course is claimed via a `.claim` file in its transcript subdir, so
    no two workers process the same course. Crashed workers leave a stale
    lock that's reclaimed after ``--stale-after`` seconds.
    """
    cfg = Config.load()
    _dump_tokens(cfg)
    t = _make_transcriber(cfg, backend, model)

    entries = load_courses_yaml(courses_yaml)
    todo = [e for e in entries if e.panopto_folder]
    skipped = len(entries) - len(todo)
    click.echo(
        f"{courses_yaml}: {len(entries)} course(s); "
        f"{len(todo)} with panopto_folder set, {skipped} blank (skipped)."
    )
    if not todo:
        click.echo("Nothing to do. Fill in `panopto_folder:` for at least one course.")
        return

    total_new = 0
    course_failures: list[tuple[str, str]] = []
    claimed_count = 0
    held_by_peers = 0

    for i, entry in enumerate(todo, start=1):
        label = entry.code or entry.name or f"canvas#{entry.canvas_id}"
        subdir = entry.out_dir or _default_course_subdir(entry)
        try:
            transcript_dir = _resolve_subdir(cfg.transcript_dir, subdir)
        except click.BadParameter as e:
            course_failures.append((label, f"bad out_dir {subdir!r}: {e.message}"))
            click.echo(f"\n=== [{i}/{len(todo)}] {label} — SKIPPED: {e.message}", err=True)
            continue

        claim = try_claim(transcript_dir, stale_after=stale_after)
        if claim is None:
            held_by_peers += 1
            click.echo(
                f"\n=== [{i}/{len(todo)}] {label} — held by another worker, skipping"
            )
            continue

        claimed_count += 1
        click.echo(
            f"\n=== [{i}/{len(todo)}] {label} → "
            f"{transcript_dir.relative_to(cfg.transcript_dir.parent)} (claimed) ==="
        )
        try:
            with Heartbeat(claim, interval=heartbeat):
                results = run_folder_streaming(
                    entry.panopto_folder,
                    cfg.download_dir,
                    transcript_dir,
                    t,
                    panopto_host=cfg.panopto_host,
                    cookies_browser=cfg.cookies_browser,
                    cookies_profile=cfg.cookies_profile,
                    cookies_file=cfg.cookies_file,
                    delete_media=not keep_media,
                )
            total_new += len(results)
        except RuntimeError as e:
            # Auth/cookie expiry is unrecoverable across courses — abort the batch.
            msg = str(e)
            if "expired" in msg.lower() or "cookies" in msg.lower():
                release(claim)
                click.echo(f"\nAUTH FAILURE on {label}: {msg}", err=True)
                click.echo("Aborting remaining courses — fix cookies and re-run.", err=True)
                raise
            course_failures.append((label, msg))
            click.echo(f"FAILED ({label}): {msg}", err=True)
        except Exception as e:  # noqa: BLE001 — one bad folder shouldn't sink the batch
            course_failures.append((label, str(e)))
            click.echo(f"FAILED ({label}): {e}", err=True)
        finally:
            release(claim)

    click.echo("")
    click.echo(
        f"Done. {total_new} new transcript(s) across {claimed_count} course(s) "
        f"this worker processed; {held_by_peers} held by peers."
        + (f" {len(course_failures)} failed:" if course_failures else "")
    )
    for label, err in course_failures:
        click.echo(f"  - {label}: {err}")


# ---- Canvas course discovery ------------------------------------------------


def _yaml_str(s: str | None) -> str:
    """Quote a YAML scalar safely (small subset; we don't want a YAML dep)."""
    if s is None:
        return "null"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@main.command("list-courses")
@click.option(
    "--state",
    default="active",
    show_default=True,
    help="Canvas enrollment_state filter: active | invited_or_pending | completed | all.",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write a starter courses.yml here, with one entry per course "
    "ready for you to fill in `panopto_folder`.",
)
def list_courses_cmd(state: str, out: Path | None) -> None:
    """List Canvas courses for the user behind CANVAS_TOKEN.

    Prints a table to stdout. With --out, also writes a YAML stub for later
    mapping each course to its Panopto folder.
    """
    cfg = Config.load()
    if not cfg.canvas_token:
        raise click.ClickException("CANVAS_TOKEN is empty — set it in .env first.")

    enrollment_state = None if state == "all" else state
    try:
        courses = list_courses(
            cfg.canvas_url, cfg.canvas_token, enrollment_state=enrollment_state
        )
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"Canvas returned {e.response.status_code} from {e.request.url}. "
            "Check CANVAS_URL and CANVAS_TOKEN."
        ) from e

    if not courses:
        click.echo(f"No courses returned (state={state}).")
        return

    courses_sorted = sorted(courses, key=lambda c: ((c.term or ""), c.course_code, c.name))
    click.echo(f"{'ID':<10} {'CODE':<22} {'TERM':<22} NAME")
    click.echo("-" * 100)
    for c in courses_sorted:
        click.echo(
            f"{c.id:<10} {c.course_code[:22]:<22} {(c.term or '')[:22]:<22} {c.name}"
        )
    click.echo(f"\nTotal: {len(courses)} course(s)")

    if out:
        lines = [
            "# Generated by `panopto-transcriber list-courses --out`.",
            "# Fill in `panopto_folder` (GUID or full Sessions/List.aspx URL) for",
            "# each course you want to process, then point the future run-courses",
            "# command at this file.",
            "courses:",
        ]
        for c in courses_sorted:
            lines += [
                f"  - canvas_id: {c.id}",
                f"    name: {_yaml_str(c.name)}",
                f"    code: {_yaml_str(c.course_code)}",
                f"    term: {_yaml_str(c.term)}",
                f"    panopto_folder: \"\"  # TODO",
            ]
        out.write_text("\n".join(lines) + "\n")
        click.echo(f"Wrote starter mapping to {out}")


if __name__ == "__main__":
    main()
