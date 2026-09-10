"""Shared subprocess bridge: bounded execution, scrubbing, stream parsing."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

from computeruse.providers.cli_bridge import (
    CliBridgeError,
    CliResult,
    TempPng,
    find_binary,
    iter_jsonl,
    run_cli,
    scrubbed_env,
)


def test_missing_binary_names_the_fix() -> None:
    with pytest.raises(CliBridgeError, match="not on PATH"):
        find_binary("no-such-cli-anywhere")


def test_scrubbed_env_removes_only_the_named_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("HOME", "/tmp")
    env = scrubbed_env(("OPENAI_API_KEY",))
    assert "OPENAI_API_KEY" not in env
    assert env["HOME"] == "/tmp"


def test_run_cli_collects_output() -> None:
    result = run_cli(
        [sys.executable, "-c", "import sys;sys.stdout.write('hi');sys.stderr.write('warn')"],
        None,
        30.0,
        scrub_keys=(),
        cwd=None,
    )
    assert isinstance(result, CliResult)
    assert result.returncode == 0
    assert result.stdout == "hi"
    assert "warn" in result.stderr
    assert result.elapsed_s >= 0.0


def test_run_cli_times_out_and_kills() -> None:
    with pytest.raises(CliBridgeError, match="did not answer within"):
        run_cli(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            "prompt",
            1.0,
            scrub_keys=(),
            cwd=None,
        )


def test_run_cli_scrubs_keys_from_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTERUSE_SPIKE_SECRET", "shh")
    result = run_cli(
        [
            sys.executable,
            "-c",
            "import os;print('leaked' if 'COMPUTERUSE_SPIKE_SECRET' in os.environ else 'clean')",
        ],
        None,
        30.0,
        scrub_keys=("COMPUTERUSE_SPIKE_SECRET",),
        cwd=None,
    )
    assert result.stdout.strip() == "clean"


def test_iter_jsonl_skips_blanks_and_rejects_garbage() -> None:
    events = list(iter_jsonl('{"a": 1}\n\n{"b": 2}\n', source="spike"))
    assert events == [{"a": 1}, {"b": 2}]
    with pytest.raises(CliBridgeError, match="non-JSON line 2"):
        list(iter_jsonl('{"a": 1}\nnope\n', source="spike"))


def test_temp_png_round_trips_and_cleans_up() -> None:
    raw = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000c4944415478d763f80001000500ffff03060008fc022d0000000049454e44ae426082"
    )
    staged = TempPng(base64.b64encode(raw).decode(), source="spike")
    try:
        assert Path(staged.path).read_bytes() == raw
        assert (Path(staged.path).stat().st_mode & 0o777) == 0o600
    finally:
        staged.close()
    assert not Path(staged.path).exists()


def test_temp_png_rejects_non_base64_and_non_png() -> None:
    with pytest.raises(CliBridgeError, match="not valid base64"):
        TempPng("!!!", source="spike")
    with pytest.raises(CliBridgeError, match="must be PNG"):
        TempPng(base64.b64encode(b"just text").decode(), source="spike")


def test_stderr_is_bounded() -> None:
    result = run_cli(
        [sys.executable, "-c", "import sys;sys.stderr.write('x' * 10000)"],
        None,
        30.0,
        scrub_keys=(),
        cwd=None,
    )
    assert len(result.stderr) <= 2000
    assert os.environ.get("PATH") is not None
