from __future__ import annotations

from pathlib import Path

import click

from .batch import transcribe_directory
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
        profile = cfg.cookies_profile or "default"
        click.echo(
            f"No Panopto cookies found in {cfg.cookies_browser} ({profile}). "
            "Sign in to Panopto in that browser/profile.",
            err=True,
        )


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
@_BACKEND
@_MODEL
def run_folder_cmd(folder_or_url: str, backend: str | None, model: str | None) -> None:
    """Download all sessions in a Panopto folder, then transcribe them all."""
    cfg = Config.load()
    _dump_tokens(cfg)
    click.echo(f"Downloading folder to {cfg.download_dir}...")
    download_folder(
        folder_or_url,
        cfg.download_dir,
        panopto_host=cfg.panopto_host,
        cookies_browser=cfg.cookies_browser,
        cookies_profile=cfg.cookies_profile,
        cookies_file=cfg.cookies_file,
    )
    t = _make_transcriber(cfg, backend, model)
    click.echo(f"Transcribing all media in {cfg.download_dir} with {t.name}...")
    results = transcribe_directory(cfg.download_dir, t, cfg.transcript_dir)
    click.echo(f"Done. {len(results)} new transcript(s) in {cfg.transcript_dir}")


if __name__ == "__main__":
    main()
