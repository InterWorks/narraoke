"""Tests for the espeak-ng data-path length workaround.

espeak-ng stores its data path in a fixed 160-byte buffer. A longer path is
silently truncated, fails the directory check that follows, and the lookup
falls through to the compiled-in build default — a GitHub Actions runner path
baked into the wheel. The user sees a missing-file error naming a directory
that never existed on their machine.

Measured against espeakng-loader 0.2.4 by calling espeak_Initialize directly
with synthetic paths: 159 characters initialises, 160 fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import tts_engine  # noqa: E402


def test_limit_matches_the_measured_boundary() -> None:
    """159 works and 160 fails, so the limit is 160."""
    assert tts_engine._ESPEAK_PATH_LIMIT == 160


def test_short_path_is_not_flagged(monkeypatch) -> None:
    """The common case must cost nothing."""
    fake = type("L", (), {"get_data_path": staticmethod(lambda: "/short/path")})
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake)
    too_long, path = tts_engine._espeak_data_path_is_too_long()
    assert not too_long
    assert path == "/short/path"


def test_long_path_is_flagged(monkeypatch) -> None:
    long_path = "/" + "x" * 200
    fake = type("L", (), {"get_data_path": staticmethod(lambda: long_path)})
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake)
    too_long, path = tts_engine._espeak_data_path_is_too_long()
    assert too_long
    assert len(path) >= tts_engine._ESPEAK_PATH_LIMIT


def test_boundary_is_exclusive_at_159(monkeypatch) -> None:
    """159 characters must NOT trigger the shim; 160 must."""
    for length, expected in ((159, False), (160, True)):
        path = "/" + "x" * (length - 1)
        assert len(path) == length
        fake = type("L", (), {"get_data_path": staticmethod(lambda p=path: p)})
        monkeypatch.setitem(sys.modules, "espeakng_loader", fake)
        too_long, _ = tts_engine._espeak_data_path_is_too_long()
        assert too_long is expected, f"{length} chars should be {expected}"


def test_missing_loader_is_not_an_error(monkeypatch) -> None:
    """espeakng_loader is a transitive dep; absence must not raise."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "espeakng_loader":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    too_long, path = tts_engine._espeak_data_path_is_too_long()
    assert not too_long
    assert path == ""


def test_shim_is_a_no_op_for_a_short_path(monkeypatch, tmp_path) -> None:
    """No copy, no side effects, when the packaged path already fits."""
    fake = type("L", (), {"get_data_path": staticmethod(lambda: "/short")})
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    tts_engine._shim_espeak_data_path()
    assert not (tmp_path / "narraoke").exists(), "should not have copied anything"


def test_shim_copies_rather_than_symlinks(monkeypatch, tmp_path) -> None:
    """A symlink would not work: phonemizer resolves the path it is given,
    which expands straight back to the long original."""
    source = tmp_path / ("d" * 120) / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_text("data", encoding="utf-8")
    assert len(str(source)) >= tts_engine._ESPEAK_PATH_LIMIT

    fake = type("L", (), {"get_data_path": staticmethod(lambda: str(source))})
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake)
    cache = tmp_path / "c"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setitem(sys.modules, "phonemizer", None)  # skip the real call

    tts_engine._shim_espeak_data_path()
    target = cache / "narraoke" / "espeak" / "espeak-ng-data"
    assert (target / "phontab").is_file()
    assert not target.is_symlink(), "must be a real copy, not a symlink"


def test_shim_warns_when_the_cache_is_also_too_long(monkeypatch, tmp_path, capsys) -> None:
    """Nothing can be done from inside the process; say so plainly rather
    than failing later with an unrelated-looking error."""
    source = tmp_path / ("d" * 120) / "espeak-ng-data"
    source.mkdir(parents=True)
    fake = type("L", (), {"get_data_path": staticmethod(lambda: str(source))})
    monkeypatch.setitem(sys.modules, "espeakng_loader", fake)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ("c" * 200)))

    tts_engine._shim_espeak_data_path()
    out = capsys.readouterr().out
    assert "shorter path" in out or "Move the project" in out
