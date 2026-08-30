"""`penny serve` only serves a dist carrying this app's build stamp, and
rebuilds one that's older than its own sources.

Born from a real incident: a gitignored pre-split frontend/dist (Clerk
landing page and all) survived the single-player merge and `penny serve`
happily served it against the no-auth backend. The stamp (``penny-build.json``,
written by vite.config.ts) is how serve tells a dist built by this app from a
stale or foreign one; a second real incident (an `npm install` drift left
uninstalled, an old dist quietly served for weeks) is why an in-family stamp
that predates its own sources triggers an automatic rebuild rather than a
silent pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from penny.cli import (
    _frontend_dist_ok,
    _frontend_dist_stale,
    _frontend_newest_source_mtime,
    _resolve_frontend_dist,
)


def _stamp(dist: Path, value: object) -> None:
    (dist / "penny-build.json").write_text(json.dumps(value), encoding="utf-8")


def _dist(tmp_path: Path, stamp: object | None) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    if stamp is not None:
        _stamp(dist, stamp)
    return dist


def test_stamped_dist_is_accepted(tmp_path: Path):
    dist = _dist(
        tmp_path, {"app": "penny-single-player", "builtAt": "2026-08-01T00:00:00Z"}
    )
    assert _frontend_dist_ok(dist)


def test_unstamped_dist_is_rejected(tmp_path: Path):
    # The incident shape: a real dist with index.html but no stamp.
    assert not _frontend_dist_ok(_dist(tmp_path, stamp=None))


def test_foreign_or_corrupt_stamp_is_rejected(tmp_path: Path):
    assert not _frontend_dist_ok(_dist(tmp_path, {"app": "someone-else"}))
    corrupt = _dist(tmp_path / "c", stamp=None)
    (corrupt / "penny-build.json").write_text("not json", encoding="utf-8")
    assert not _frontend_dist_ok(corrupt)


def _frontend_tree(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    (frontend / "src" / "main.tsx").write_text("// app", encoding="utf-8")
    return frontend


def test_stale_dist_missing_stamp_is_stale(tmp_path: Path):
    frontend = _frontend_tree(tmp_path)
    dist = _dist(tmp_path / "out", stamp=None)
    assert _frontend_dist_stale(dist, frontend)


def test_dist_older_than_source_is_stale(tmp_path: Path):
    frontend = _frontend_tree(tmp_path)
    # A build stamped well before the source tree's mtimes (set by creating
    # the files just above, i.e. "now").
    dist = _dist(
        tmp_path / "out",
        {"app": "penny-single-player", "builtAt": "2000-01-01T00:00:00Z"},
    )
    assert _frontend_dist_stale(dist, frontend)


def test_dist_newer_than_source_is_fresh(tmp_path: Path):
    frontend = _frontend_tree(tmp_path)
    newest = _frontend_newest_source_mtime(frontend)
    from datetime import UTC, datetime, timedelta

    built_at = datetime.fromtimestamp(newest, tz=UTC) + timedelta(seconds=1)
    dist = _dist(
        tmp_path / "out",
        {"app": "penny-single-player", "builtAt": built_at.isoformat()},
    )
    assert not _frontend_dist_stale(dist, frontend)


def test_source_mtime_ignores_node_modules_and_dist(tmp_path: Path):
    frontend = _frontend_tree(tmp_path)
    noisy = frontend / "src" / "node_modules" / "whatever.js"
    noisy.parent.mkdir(parents=True)
    noisy.write_text("ignored", encoding="utf-8")
    # A file under an excluded dir name must not be the reported newest —
    # touch it far in the future and confirm it's not picked up.
    import os

    future = 4102444800  # 2100-01-01, comfortably after any real source file
    os.utime(noisy, (future, future))
    assert _frontend_newest_source_mtime(frontend) < future


def test_resolve_rebuilds_a_stale_default_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The repo-managed default dist is rebuilt in place when stale."""
    frontend = _frontend_tree(tmp_path)

    import penny.cli as cli_module

    rebuilt = {"called": False}

    def fake_rebuild(target: Path) -> bool:
        rebuilt["called"] = True
        assert target == frontend
        dist = frontend / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
        _stamp(dist, {"app": "penny-single-player", "builtAt": "2999-01-01T00:00:00Z"})
        return True

    monkeypatch.setattr(cli_module, "_rebuild_frontend", fake_rebuild)

    result = _resolve_frontend_dist(None, repo_root=tmp_path)
    assert rebuilt["called"]
    assert result == frontend / "dist"


def test_resolve_falls_back_to_api_only_when_rebuild_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _frontend_tree(tmp_path)

    import penny.cli as cli_module

    monkeypatch.setattr(cli_module, "_rebuild_frontend", lambda target: False)

    assert _resolve_frontend_dist(None, repo_root=tmp_path) is None


def test_resolve_leaves_a_fresh_default_dist_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An already-fresh dist is served without touching npm at all."""
    frontend = _frontend_tree(tmp_path)
    newest = _frontend_newest_source_mtime(frontend)
    from datetime import UTC, datetime, timedelta

    built_at = datetime.fromtimestamp(newest, tz=UTC) + timedelta(seconds=1)
    dist = frontend / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    _stamp(dist, {"app": "penny-single-player", "builtAt": built_at.isoformat()})

    import penny.cli as cli_module

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("a fresh dist must not trigger a rebuild")

    monkeypatch.setattr(cli_module, "_rebuild_frontend", fail_if_called)

    assert _resolve_frontend_dist(None, repo_root=tmp_path) == dist


def test_explicit_frontend_dir_is_never_auto_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An explicit --frontend-dir gets a hard error on a bad stamp, not a rebuild."""
    import penny.cli as cli_module

    dist = _dist(tmp_path, stamp=None)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("explicit --frontend-dir must never trigger a rebuild")

    monkeypatch.setattr(cli_module, "_rebuild_frontend", fail_if_called)

    import typer

    with pytest.raises(typer.Exit):
        _resolve_frontend_dist(str(dist))
