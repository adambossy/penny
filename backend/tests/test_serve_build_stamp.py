"""`penny serve` only serves a dist carrying this app's build stamp.

Born from a real incident: a gitignored pre-split frontend/dist (Clerk
landing page and all) survived the single-player merge and `penny serve`
happily served it against the no-auth backend. The stamp
(``penny-build.json``, written by vite.config.ts) is how serve tells a dist
built by this app from a stale or foreign one.
"""

from __future__ import annotations

import json
from pathlib import Path

from penny.cli import _frontend_dist_ok


def _dist(tmp_path: Path, stamp: object | None) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    if stamp is not None:
        (dist / "penny-build.json").write_text(json.dumps(stamp), encoding="utf-8")
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
