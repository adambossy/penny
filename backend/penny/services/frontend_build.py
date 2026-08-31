"""Keep the frontend `penny serve` mounts in sync with its own sources.

Born from two real incidents. First: a gitignored pre-split `frontend/dist`
(Clerk landing page and all) survived the single-player merge, and `serve`
happily served it against the no-auth backend. Second: a `dist` built weeks
earlier was served silently — stale UI source, and an `@adambossy/agent-ui`
in `node_modules` pinned behind what `package.json` asked for — with no
signal anything was wrong.

`npm run build` writes a stamp (``penny-build.json``, via `vite.config.ts`)
into the dist; this module verifies it (rejecting a foreign/unstamped dist)
and, for the repo-managed default, rebuilds automatically when the stamp
predates the sources that feed it.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess

import typer

_NO_FRONTEND_MESSAGE = (
    "No built frontend found — serving the API only. "
    "Build it with `npm run build` in frontend/."
)


def _expected_app_id() -> str:
    """The app identity every build stamps (``frontend/app-id.json``).

    The single source both ``vite.config.ts`` and this verifier read, so the
    two languages can't drift apart on what "built by this app" means.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    path = repo_root / "frontend" / "app-id.json"
    return json.loads(path.read_text(encoding="utf-8"))["app"]


def _build_stamp(dist: Path) -> dict | None:
    """This app's build stamp (``penny-build.json``), or ``None`` if absent/corrupt/foreign."""
    try:
        stamp = json.loads((dist / "penny-build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stamp, dict) or stamp.get("app") != _expected_app_id():
        return None
    return stamp


def _dist_ok(dist: Path) -> bool:
    """True when ``dist`` carries this app's build stamp."""
    return _build_stamp(dist) is not None


# Inputs that determine what a build actually produces. `node_modules`
# itself is deliberately excluded — any dependency version that matters is
# already reflected in `package.json` / `package-lock.json`, so hashing the
# lockfiles is enough to catch an `npm install` drift without walking every
# installed package.
_SOURCE_DIRS = ("src", "packages")
_SOURCE_FILES = ("package.json", "package-lock.json", "vite.config.ts")
_SOURCE_EXCLUDE_DIRS = {"node_modules", "dist", ".git"}


def _newest_source_mtime(frontend_dir: Path) -> float:
    """The newest mtime among files that feed the build.

    Compared against the dist's stamped ``builtAt`` to tell a build that
    predates a later source or dependency change — the mtime approach costs
    a stat-tree walk, not a hash, and a `git pull` or `npm install` already
    bumps mtimes on every file it touches, so it catches exactly the drift
    that matters (uncommitted edits included).

    Walks with ``os.walk`` rather than ``Path.rglob`` so an excluded
    directory (a nested ``node_modules`` under a workspace package, say) is
    pruned before descending into it, not stat'd and then discarded.
    """
    newest = 0.0
    for name in _SOURCE_FILES:
        path = frontend_dir / name
        if path.exists():
            newest = max(newest, path.stat().st_mtime)
    for dirname in _SOURCE_DIRS:
        root = frontend_dir / dirname
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SOURCE_EXCLUDE_DIRS]
            for filename in filenames:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, filename)))
    return newest


def _dist_stale(dist: Path, frontend_dir: Path) -> bool:
    """True when ``dist`` is missing, foreign/unstamped, or older than its sources."""
    stamp = _build_stamp(dist)
    if stamp is None:
        return True
    try:
        built_at = datetime.fromisoformat(str(stamp["builtAt"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return True
    return _newest_source_mtime(frontend_dir) > built_at.timestamp()


def _rebuild(frontend_dir: Path) -> bool:
    """Run ``npm install && npm run build``; True on success.

    Output streams straight to the console (no capture) — a build failure's
    detail belongs in the terminal, not swallowed into an exception message.
    """
    npm = shutil.which("npm")
    if npm is None:
        typer.echo("npm not found on PATH — cannot rebuild the frontend.", err=True)
        return False
    for args in (["install"], ["run", "build"]):
        result = subprocess.run([npm, *args], cwd=frontend_dir)  # noqa: S603 - npm found via PATH, args are our own literals
        if result.returncode != 0:
            typer.echo(f"`npm {' '.join(args)}` failed — see output above.", err=True)
            return False
    return True


def resolve_frontend_dist(
    explicit_dir: str | None, *, repo_root: Path | None = None
) -> Path | None:
    """The dist to serve, rebuilding the repo's default one when it's stale.

    An explicit ``--frontend-dir`` is the caller's own choice: it must exist
    and carry the stamp, but is never rebuilt (there is no source tree this
    command can assume for an arbitrary path) — a bad one is a hard error,
    not a silent downgrade. The repo-managed default (``frontend/dist``) is
    instead kept fresh automatically: missing, foreign, or older than its own
    sources triggers ``npm install && npm run build`` before serving.

    ``repo_root`` defaults to this checkout's root; overridable for tests.
    """
    if explicit_dir is not None:
        static = Path(explicit_dir).expanduser()
        if not static.exists():
            typer.echo(f"Frontend dir not found: {static}", err=True)
            raise typer.Exit(1)
        if not _dist_ok(static):
            typer.echo(
                f"{static} has no penny-build.json stamp — it was not built "
                "by this app's `npm run build` (a stale pre-split or foreign "
                "build). Rebuild it, or pass a freshly built dist.",
                err=True,
            )
            raise typer.Exit(1)
        return static

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
    frontend_dir = repo_root / "frontend"
    candidate = frontend_dir / "dist"
    if not frontend_dir.exists():
        # No source tree to build from at all — the repo-managed default
        # can't be a stamped dist without it (`candidate` lives under it).
        typer.echo(_NO_FRONTEND_MESSAGE)
        return None

    if not candidate.exists() or _dist_stale(candidate, frontend_dir):
        typer.echo(
            "Frontend is missing or stale — rebuilding "
            "(`npm install && npm run build` in frontend/)…"
        )
        if not _rebuild(frontend_dir):
            typer.echo(
                "Rebuild failed — serving the API only. Fix the error above "
                "and re-run `penny serve`, or build manually.",
                err=True,
            )
            return None

    if candidate.exists() and _dist_ok(candidate):
        return candidate
    typer.echo(_NO_FRONTEND_MESSAGE)
    return None
