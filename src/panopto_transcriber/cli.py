from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import click
import httpx

from ._progress import fmt_duration

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
    click.echo(f"Plan: up to {len(todo)} course(s) (peers may claim some).")

    total_new = 0
    course_failures: list[tuple[str, str]] = []
    claimed_count = 0
    held_by_peers = 0
    batch_start = time.monotonic()

    for i, entry in enumerate(todo, start=1):
        label = entry.code or entry.name or f"canvas#{entry.canvas_id}"
        subdir = entry.out_dir or _default_course_subdir(entry)
        try:
            transcript_dir = _resolve_subdir(cfg.transcript_dir, subdir)
        except click.BadParameter as e:
            course_failures.append((label, f"bad out_dir {subdir!r}: {e.message}"))
            click.echo(f"\n[{i}/{len(todo)}] {label} — SKIPPED: {e.message}", err=True)
            continue

        claim = try_claim(transcript_dir, stale_after=stale_after)
        if claim is None:
            held_by_peers += 1
            click.echo(f"\n[{i}/{len(todo)}] {label} — held by another worker, skipping")
            continue

        claimed_count += 1
        bar = "=" * 70
        click.echo("")
        click.echo(bar)
        click.echo(f"COURSE [{i}/{len(todo)}]  {label}")
        click.echo(f"  term:        {entry.term or '-'}")
        click.echo(f"  panopto:     {entry.panopto_folder}")
        click.echo(f"  transcripts: {transcript_dir}")
        click.echo(
            f"  progress:    {claimed_count} processed by me, "
            f"{held_by_peers} taken by peers, {i - claimed_count - held_by_peers} other"
        )
        if claimed_count > 1:
            elapsed_now = time.monotonic() - batch_start
            avg_per = elapsed_now / (claimed_count - 1)  # exclude the one just starting
            remaining = len(todo) - i + 1
            eta = remaining * avg_per
            eta_at = (datetime.now() + timedelta(seconds=eta)).strftime("%H:%M")
            click.echo(
                f"  batch ETA:   ~{fmt_duration(eta)} (finish ~{eta_at}); "
                f"avg {fmt_duration(avg_per)}/course over {claimed_count - 1} so far"
            )
        click.echo(bar)

        course_start = time.monotonic()
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
            course_elapsed = time.monotonic() - course_start
            batch_elapsed = time.monotonic() - batch_start
            avg_per_course = batch_elapsed / claimed_count
            remaining = len(todo) - i
            eta = remaining * avg_per_course
            click.echo(
                f"\n[{i}/{len(todo)}] {label} done in {fmt_duration(course_elapsed)} "
                f"({len(results)} new transcript(s)). "
                f"Total: {total_new} transcript(s), "
                f"{fmt_duration(batch_elapsed)} elapsed, "
                f"~{fmt_duration(eta)} remaining."
            )
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

    batch_total = time.monotonic() - batch_start
    click.echo("")
    click.echo("=" * 70)
    click.echo(
        f"BATCH DONE in {fmt_duration(batch_total)}. "
        f"{total_new} new transcript(s) across {claimed_count} course(s) "
        f"processed by this worker; {held_by_peers} held by peers."
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


@main.command("match-orphans-to-calendar")
@click.argument(
    "orphans_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.argument(
    "calendar_ics",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.argument(
    "courses_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--target-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where matched transcripts go (defaults to TRANSCRIPT_DIR from .env).",
)
@click.option(
    "--time-window-minutes",
    type=int,
    default=120,
    show_default=True,
    help="A transcript timestamp matches a class event within ± this many minutes.",
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually move files. Without this, runs as a dry-run report.",
)
@click.option(
    "--print-unmatched",
    is_flag=True,
    default=False,
    help="At the end, print every unmatched session with nearby calendar events "
    "and YAML candidates so you can resolve them by hand.",
)
def match_orphans_cmd(
    orphans_dir: Path,
    calendar_ics: Path,
    courses_yaml: Path,
    target_dir: Path | None,
    time_window_minutes: int,
    apply: bool,
    print_unmatched: bool,
) -> None:
    """Re-file orphan transcripts into the correct course subdir using a
    Google Calendar export.

    For each transcript in ORPHANS_DIR (recursively), parses the recording
    timestamp from the filename, looks up class events on that date in
    CALENDAR_ICS, and matches them to courses in COURSES_YAML.
    """
    from datetime import timedelta
    from .match_calendar import (
        build_course_index, date_to_term_label, expand_class_events,
        parse_filename_datetime, pick_course,
    )

    cfg = Config.load()
    dest_root = (target_dir or cfg.transcript_dir).expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    orphans_dir = orphans_dir.resolve()

    # Collect orphan files (recurse one level under <orphans_dir>/<subdir>/<file>)
    files: list[Path] = []
    for p in orphans_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".txt", ".srt"}:
            files.append(p)
    click.echo(f"Scanning {len(files)} orphan file(s) in {orphans_dir}.")

    # Need calendar spanning earliest..latest transcript date
    timestamps: dict[Path, "datetime"] = {}
    for f in files:
        dt = parse_filename_datetime(f.name)
        if dt is not None:
            timestamps[f] = dt
    if not timestamps:
        click.echo("No transcripts have a parseable yt-dlp date in their filename.")
        return
    min_d = min(dt.date() for dt in timestamps.values())
    max_d = max(dt.date() for dt in timestamps.values()) + timedelta(days=1)
    click.echo(f"Expanding calendar events between {min_d} and {max_d}...")
    events = expand_class_events(calendar_ics, min_d, max_d)
    click.echo(f"  {len(events)} class-like event(s) found.")

    # Bucket by date for fast lookup
    events_by_date: dict[date, list] = {}
    for ev in events:
        events_by_date.setdefault(ev.start.date(), []).append(ev)

    yaml_entries = load_courses_yaml(courses_yaml)
    by_key, by_num_term = build_course_index(yaml_entries)

    # For each transcript GUID, pick one decision (use the .txt's outcome for the pair)
    # but apply the move to both .txt and .srt with the same GUID.
    by_guid: dict[str, list[Path]] = {}
    for f in files:
        m = re.search(r"\[([0-9a-fA-F-]{36})\]", f.name)
        if m:
            by_guid.setdefault(m.group(1).lower(), []).append(f)
        else:
            by_guid.setdefault(f"_no_guid_{f.name}", []).append(f)

    moved = 0
    no_event = 0
    no_match = 0
    ambiguous = 0
    no_section_in_yaml = 0
    reasons: dict[str, int] = {}
    # For --print-unmatched: collect per-orphan details
    unmatched: list[dict] = []

    for guid, paths in by_guid.items():
        # Use the first parseable timestamp for this group (all should be equal).
        dt = None
        for p in paths:
            if p in timestamps:
                dt = timestamps[p]
                break
        if dt is None:
            no_event += 1
            unmatched.append({
                "paths": paths, "dt": None, "reason": "no-parseable-date",
                "nearby": [], "term": None,
            })
            continue

        day_events = events_by_date.get(dt.date(), [])
        window = timedelta(minutes=time_window_minutes)
        nearby = [
            ev for ev in day_events
            if abs((ev.start - dt).total_seconds()) <= window.total_seconds()
        ]
        term = date_to_term_label(dt.date())
        if not nearby:
            no_event += 1
            unmatched.append({
                "paths": paths, "dt": dt, "reason": "no-nearby-event",
                "nearby": [], "term": term,
            })
            continue
        # Closest event wins
        nearby.sort(key=lambda ev: abs((ev.start - dt).total_seconds()))
        course = None
        reason = ""
        for ev in nearby:
            course, reason = pick_course(ev, term, by_key, by_num_term)
            if course:
                break
        reasons[reason] = reasons.get(reason, 0) + 1

        if course is None:
            if reason == "ambiguous":
                ambiguous += 1
            elif reason == "section-not-in-yaml":
                no_section_in_yaml += 1
            else:
                no_match += 1
            unmatched.append({
                "paths": paths, "dt": dt, "reason": reason,
                "nearby": nearby, "term": term,
                "closest_event": nearby[0],
            })
            continue

        subdir = course.out_dir or _default_course_subdir(course)
        target = dest_root / subdir
        if apply:
            target.mkdir(parents=True, exist_ok=True)
            # Same session may appear in several orphan subdirs (cross-listed
            # course folders + multiple historical runs); keep one .txt and
            # one .srt, delete the rest.
            kept_by_ext: dict[str, Path] = {}
            for p in paths:
                ext = p.suffix.lower()
                if ext not in kept_by_ext:
                    kept_by_ext[ext] = p
            for p in paths:
                ext = p.suffix.lower()
                if p is kept_by_ext.get(ext):
                    dest = target / p.name
                    if dest.exists() and dest != p:
                        # Target already has this filename (probably a prior run
                        # of match). Don't overwrite; just drop the orphan.
                        p.unlink()
                    else:
                        p.rename(dest)
                else:
                    p.unlink()
        moved += 1
        if moved <= 10 or moved % 100 == 0:
            sample = nearby[0]
            click.echo(
                f"  [{moved:>4}] {dt.date()} {dt.strftime('%H:%M')} → "
                f"{course.code} ({term}) via '{sample.summary}' [{reason}]"
            )

    action = "moved" if apply else "would move"
    click.echo("")
    click.echo("=" * 70)
    click.echo(
        f"{action.capitalize()} {moved} session(s). "
        f"{no_event} had no nearby class event; "
        f"{ambiguous} ambiguous (multiple sections that term); "
        f"{no_section_in_yaml} matched a section we don't have in YAML; "
        f"{no_match} unmatched."
    )
    click.echo(f"Reason breakdown: {dict(sorted(reasons.items()))}")

    if print_unmatched and unmatched:
        click.echo("")
        click.echo("=" * 70)
        click.echo(f"UNMATCHED DETAIL ({len(unmatched)} session(s)):")
        # Group by reason for easier triage
        unmatched.sort(key=lambda u: (
            u["reason"],
            u["dt"].timestamp() if u["dt"] else 0,
        ))
        current_reason = ""
        for u in unmatched:
            if u["reason"] != current_reason:
                current_reason = u["reason"]
                click.echo("")
                click.echo(f"--- reason: {current_reason} ---")
            # Pick the most informative filename (.txt over .srt) for display
            sample = next(
                (p for p in u["paths"] if p.suffix.lower() == ".txt"),
                u["paths"][0],
            )
            dt_str = u["dt"].strftime("%Y-%m-%d %a %H:%M") if u["dt"] else "(no date)"
            click.echo(f"\n  {sample.name}")
            click.echo(f"    recorded: {dt_str}  ({u['term'] or '-'})")
            click.echo(f"    copies in orphans dir: {len(u['paths'])}")

            if u["reason"] == "ambiguous":
                ev = u["closest_event"]
                opts = by_num_term.get((ev.code_number, u["term"]), [])
                click.echo(
                    f"    calendar event: '{ev.summary}' at {ev.start.strftime('%H:%M')} "
                    f"(no section letter); YAML has {len(opts)} sections that term:"
                )
                for c in opts:
                    click.echo(f"      - {c.code}  (canvas_id={c.canvas_id})")
            elif u["reason"] == "section-not-in-yaml":
                ev = u["closest_event"]
                opts = by_num_term.get((ev.code_number, u["term"]), [])
                click.echo(
                    f"    calendar event: '{ev.summary}' at {ev.start.strftime('%H:%M')} "
                    f"says section {ev.section}; YAML has these sections for "
                    f"CSS {ev.code_number} {u['term']}:"
                )
                for c in opts:
                    click.echo(f"      - {c.code}  (canvas_id={c.canvas_id})")
                if not opts:
                    click.echo("      (none)")
            elif u["reason"] == "no-nearby-event":
                click.echo(
                    "    no class-like event within "
                    f"±{time_window_minutes} min in calendar"
                )
            elif u["reason"] == "no-parseable-date":
                click.echo("    filename has no yt-dlp date stamp")
            elif u["reason"] == "no-course":
                ev = u["closest_event"]
                click.echo(
                    f"    nearby event '{ev.summary}' parsed code={ev.code_number} "
                    f"but no entry for that code in YAML for {u['term']}"
                )


@main.command("verify-transcripts")
@click.argument(
    "courses_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--transcript-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Root transcript directory to check (defaults to TRANSCRIPT_DIR from .env).",
)
@click.option(
    "--move-orphans-to",
    "move_to",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Move orphan transcript files under this quarantine directory, preserving "
    "the per-course subdir structure. Safer than deletion.",
)
@click.option(
    "--delete-orphans",
    is_flag=True,
    default=False,
    help="Permanently delete orphan transcripts. Mutually exclusive with --move-orphans-to.",
)
def verify_transcripts_cmd(
    courses_yaml: Path,
    transcript_dir: Path | None,
    move_to: Path | None,
    delete_orphans: bool,
) -> None:
    """Find transcripts whose session GUID isn't in the course's Panopto folder.

    Detects orphan transcripts left behind by earlier runs where the Panopto
    folder transiently contained sessions that have since been moved out.

    Without --move-orphans-to or --delete-orphans, runs as a dry-run report.
    """
    from .verify import (
        all_files_for_guid,
        collect_transcripts,
        list_folder_sessions,
    )
    from .inventory import load_panopto_cookies

    if move_to and delete_orphans:
        raise click.BadParameter(
            "--move-orphans-to and --delete-orphans are mutually exclusive.",
        )

    cfg = Config.load()
    root = (transcript_dir or cfg.transcript_dir).expanduser().resolve()
    if not root.is_dir():
        raise click.ClickException(f"Transcript root not found: {root}")

    cookies = load_panopto_cookies(
        cfg.cookies_browser, cfg.cookies_profile, cfg.cookies_file
    )
    if not cookies:
        raise click.ClickException(
            "No *.panopto.com cookies. Sign in to Panopto in the configured browser."
        )

    entries = load_courses_yaml(courses_yaml)
    todo = [e for e in entries if e.panopto_folder]
    click.echo(
        f"Checking transcripts in {root} against {len(todo)} folder(s) "
        f"in {courses_yaml}."
    )

    total_files = 0
    total_orphans = 0
    courses_with_orphans = 0
    no_subdir_count = 0
    failed: list[tuple[str, str]] = []

    for i, entry in enumerate(todo, start=1):
        label = entry.code or entry.name or f"canvas#{entry.canvas_id}"
        subdir_name = entry.out_dir or _default_course_subdir(entry)
        course_dir = root / subdir_name

        if not course_dir.is_dir():
            no_subdir_count += 1
            continue

        try:
            folder = list_folder_sessions(cfg.panopto_host, entry.panopto_folder, cookies)
        except Exception as e:  # noqa: BLE001
            failed.append((label, str(e)))
            click.echo(f"[{i}/{len(todo)}] {label} — ERROR fetching folder: {e}", err=True)
            continue

        transcripts, no_guid = collect_transcripts(course_dir)
        total_files += len(transcripts)
        in_folder = folder.all()
        orphans = [t for t in transcripts if t.guid not in in_folder]

        if not orphans and not no_guid:
            click.echo(
                f"[{i}/{len(todo)}] {label} — {len(transcripts)} transcripts, "
                f"all match folder ({folder.active_count} active + "
                f"{folder.archived_count} archived sessions)."
            )
            continue

        courses_with_orphans += 1
        total_orphans += len(orphans)
        click.echo(
            f"[{i}/{len(todo)}] {label}: {len(orphans)} ORPHAN transcript(s) of "
            f"{len(transcripts)} (folder has {folder.active_count} active + "
            f"{folder.archived_count} archived)"
        )
        for orph in orphans[:10]:
            click.echo(f"    - {orph.path.name}")
        if len(orphans) > 10:
            click.echo(f"    ... and {len(orphans) - 10} more")
        if no_guid:
            click.echo(f"    {len(no_guid)} file(s) without a session GUID (left alone)")

        # Mutate disk if asked
        if move_to or delete_orphans:
            for orph in orphans:
                for f in all_files_for_guid(course_dir, orph.guid):
                    if move_to:
                        dest_dir = move_to.expanduser().resolve() / subdir_name
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        f.rename(dest_dir / f.name)
                    elif delete_orphans:
                        f.unlink()

    action = "moved" if move_to else "deleted" if delete_orphans else "found"
    click.echo("")
    click.echo("=" * 70)
    click.echo(
        f"Verified {total_files} transcript(s) across {len(todo) - no_subdir_count} "
        f"course subdir(s). {action} {total_orphans} orphan(s) "
        f"in {courses_with_orphans} course(s)."
    )
    if no_subdir_count:
        click.echo(
            f"{no_subdir_count} course(s) had no transcript subdir on disk "
            "(never run or different out_dir)."
        )
    if failed:
        click.echo(f"{len(failed)} folder lookup(s) failed:")
        for label, err in failed[:5]:
            click.echo(f"  - {label}: {err}")
        if len(failed) > 5:
            click.echo(f"  ... and {len(failed) - 5} more")


@main.command("inventory")
@click.argument(
    "courses_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Also list courses without a panopto_folder set (printed as '-').",
)
@click.option(
    "--skip-archived",
    is_flag=True,
    default=False,
    help="Skip courses whose name starts with 'ARCHIVED:'.",
)
def inventory_cmd(courses_yaml: Path, show_all: bool, skip_archived: bool) -> None:
    """Print active/archived Panopto session counts per course in COURSES_YAML.

    Hits Panopto's `Services/Data.svc/GetSessions` endpoint twice per folder
    (once with `includeArchived=false`, once with `=true`) using the cookies
    yt-dlp would use for downloads. Counts come from the same source the web
    UI uses, so they match what you see when you open the folder in a browser.
    """
    from .inventory import count_folder_sessions, load_panopto_cookies

    cfg = Config.load()
    entries = load_courses_yaml(courses_yaml)
    if skip_archived:
        entries = [
            e for e in entries
            if not (e.name or "").upper().startswith("ARCHIVED")
        ]

    todo = entries if show_all else [e for e in entries if e.panopto_folder]
    if not todo:
        click.echo(
            "No courses to inventory. (Use --all to also list courses without "
            "a panopto_folder set.)"
        )
        return

    cookies = load_panopto_cookies(
        cfg.cookies_browser, cfg.cookies_profile, cfg.cookies_file
    )
    if not cookies:
        raise click.ClickException(
            "No *.panopto.com cookies found. Sign in to Panopto in the "
            f"configured browser/profile ({cfg.cookies_browser} "
            f"{cfg.cookies_profile or 'Default'}), or set COOKIES_FILE on a "
            "headless server."
        )
    click.echo(
        f"Inventorying {len(todo)} course(s) using {len(cookies)} panopto.com cookies."
    )

    header = f"{'CODE':<22} {'TERM':<15} {'ACTIVE':>6} {'ARCHIVED':>8}  NAME"
    click.echo(header)
    click.echo("-" * 100)

    total_active = 0
    total_archived = 0
    failures: list[tuple[str, str]] = []

    for entry in todo:
        code = (entry.code or "")[:22]
        term = (entry.term or "")[:15]
        name = (entry.name or "") if entry.name else ""

        if not entry.panopto_folder:
            click.echo(f"{code:<22} {term:<15} {'-':>6} {'-':>8}  {name}")
            continue

        try:
            counts = count_folder_sessions(
                cfg.panopto_host, entry.panopto_folder, cookies
            )
        except Exception as e:  # noqa: BLE001
            failures.append((entry.code or entry.name or "?", str(e)))
            click.echo(
                f"{code:<22} {term:<15} {'ERR':>6} {'ERR':>8}  {name} — {e}",
                err=True,
            )
            continue

        total_active += counts.active
        total_archived += counts.archived
        click.echo(
            f"{code:<22} {term:<15} {counts.active:>6} {counts.archived:>8}  {name}"
        )

    click.echo("-" * 100)
    click.echo(
        f"{'TOTAL':<22} {'':<15} {total_active:>6} {total_archived:>8}  "
        f"({total_active + total_archived} sessions across {len(todo) - len(failures)} folder(s))"
    )
    if failures:
        click.echo(f"\n{len(failures)} folder(s) failed:")
        for label, err in failures[:10]:
            click.echo(f"  - {label}: {err}")
        if len(failures) > 10:
            click.echo(f"  ... and {len(failures) - 10} more")


@main.command("discover-folders")
@click.argument(
    "courses_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--skip-archived",
    is_flag=True,
    default=False,
    help="Skip courses whose name starts with 'ARCHIVED:'.",
)
@click.option(
    "--headed",
    is_flag=True,
    default=False,
    help="Show the browser window. Useful for first-time SSO/MFA or debugging.",
)
def discover_folders_cmd(
    courses_yaml: Path, skip_archived: bool, headed: bool
) -> None:
    """Auto-populate `panopto_folder` for courses missing it in COURSES_YAML.

    For each entry with an empty `panopto_folder`, queries Canvas for the
    course's Panopto LTI tab and drives a headless Chromium through the LTI
    launch to read the resulting folder GUID. Courses with no Panopto tab
    are skipped silently. The YAML is rewritten in place; comments and key
    order are preserved.

    Requires the `discover` extra:
        uv sync --extra discover
        uv run playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise click.ClickException(
            "Playwright is not installed. Run:\n"
            "  uv sync --extra discover\n"
            "  uv run playwright install chromium"
        ) from e

    from .discover import (
        chrome_cookies_for_playwright,
        extract_folder_id_from_page,
        find_panopto_tab_url,
        update_yaml_in_place,
    )

    cfg = Config.load()
    if not cfg.canvas_token:
        raise click.ClickException("CANVAS_TOKEN is empty — set it in .env first.")

    entries = load_courses_yaml(courses_yaml)
    todo = [
        e for e in entries
        if not e.panopto_folder
        and e.canvas_id
        and not (skip_archived and (e.name or "").upper().startswith("ARCHIVED"))
    ]

    if not todo:
        click.echo("Nothing to discover — all entries already have panopto_folder set.")
        return

    click.echo(
        f"Discovering Panopto folders for {len(todo)} course(s) "
        f"(of {len(entries)} total in {courses_yaml})."
    )

    cookies = chrome_cookies_for_playwright(cfg.cookies_profile or None)
    click.echo(
        f"Loaded {len(cookies)} cookies from {cfg.cookies_browser} "
        f"({cfg.cookies_profile or 'Default'})."
    )

    updates: dict[int, str] = {}
    no_panopto: list[str] = []
    failed: list[tuple[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        context.add_cookies(cookies)

        try:
            for i, entry in enumerate(todo, start=1):
                label = entry.code or entry.name or f"canvas#{entry.canvas_id}"
                prefix = f"[{i}/{len(todo)}]"

                try:
                    launch = find_panopto_tab_url(
                        cfg.canvas_url, cfg.canvas_token, entry.canvas_id
                    )
                except httpx.HTTPStatusError as e:
                    failed.append((label, f"tabs API {e.response.status_code}"))
                    click.echo(f"{prefix} {label} — tabs API failed: {e}", err=True)
                    continue

                if launch is None:
                    no_panopto.append(label)
                    click.echo(f"{prefix} {label} — no Panopto tab, skipping")
                    continue

                page = context.new_page()
                try:
                    page.goto(launch, wait_until="networkidle", timeout=60_000)
                    folder_id = extract_folder_id_from_page(page)
                    if folder_id:
                        updates[entry.canvas_id] = folder_id
                        click.echo(f"{prefix} {label} — {folder_id}")
                    else:
                        failed.append((label, "no folderID in launched page"))
                        click.echo(
                            f"{prefix} {label} — FAILED: no folderID found "
                            f"(re-run with --headed to inspect)",
                            err=True,
                        )
                except Exception as e:  # noqa: BLE001 — keep going on per-page errors
                    failed.append((label, str(e)))
                    click.echo(f"{prefix} {label} — FAILED: {e}", err=True)
                finally:
                    page.close()
        finally:
            browser.close()

    if updates:
        modified = update_yaml_in_place(courses_yaml, updates)
        click.echo(f"\nWrote {modified} new GUID(s) to {courses_yaml}.")
    else:
        click.echo(f"\nNo updates to write.")

    click.echo(
        f"Summary: {len(updates)} discovered, "
        f"{len(no_panopto)} no Panopto tab, {len(failed)} failed."
    )
    if failed:
        for label, err in failed[:10]:
            click.echo(f"  - {label}: {err}")
        if len(failed) > 10:
            click.echo(f"  ... and {len(failed) - 10} more")


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
