"""Smoke tests: the CLI imports, builds its command tree, and every command
exposes `--help` without crashing. Cheap insurance against a broken decorator
or a bad import taking the whole tool down.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from panopto_transcriber.cli import main

EXPECTED_COMMANDS = {
    "dump-tokens",
    "download",
    "transcribe",
    "run",
    "download-folder",
    "transcribe-dir",
    "run-folder",
    "run-courses",
    "match-orphans-to-calendar",
    "verify-transcripts",
    "inventory",
    "discover-folders",
    "list-courses",
}


def test_top_level_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0


def test_all_expected_commands_registered() -> None:
    registered = set(main.commands)
    missing = EXPECTED_COMMANDS - registered
    assert not missing, f"commands missing from CLI: {sorted(missing)}"


def test_dump_tokens_command_exists() -> None:
    # Regression for the README/hint that referenced a non-existent command.
    assert "dump-tokens" in main.commands


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_each_command_help(command: str) -> None:
    result = CliRunner().invoke(main, [command, "--help"])
    assert result.exit_code == 0, result.output
