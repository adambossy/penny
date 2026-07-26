"""The eval version stamp: what defined the agent's decision space for a run.

Stamped on every ``eval_runs`` row so the accuracy trend can attribute a change to
a cause (a model swap, a prompt edit, a taxonomy rename, a rules change) rather
than conflating it with data drift. Every field is best-effort — a missing source
yields ``None``, never an exception, so a stamp never breaks a run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from penny.config import categorizer_model

# backend/penny/eval/version.py -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PROMPTS_META = _BACKEND_ROOT / ".prompts" / "_meta.json"
_TAXONOMY_YAML = _BACKEND_ROOT / "configs" / "taxonomy.yaml"

_CATEGORIZER_PROMPT_KEY = "categorize-transaction-agent"


def _prompt_version(key: str) -> str | None:
    """``last_version`` for a prompt key from the promptorium manifest."""
    try:
        meta = json.loads(_PROMPTS_META.read_text(encoding="utf-8"))
        entry = (meta.get("prompts") or meta).get(key) or {}
        version = entry.get("last_version")
        return str(version) if version is not None else None
    except Exception:
        return None


def _file_hash(path: Path) -> str | None:
    """Short content hash of a file (a stable version handle for config files)."""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest[:12]
    except Exception:
        return None


def _rules_hash() -> str | None:
    """Short hash of the active merchant rules text."""
    try:
        from penny.services import get_rules_loader

        loader = get_rules_loader()
        rules = (loader.load() if loader else "") or ""
        return hashlib.sha256(rules.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return None


def _git_sha() -> str | None:
    """Short git HEAD sha of the running code (best-effort)."""
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=str(_BACKEND_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = out.stdout.strip()
        return sha or None
    except Exception:
        return None


def version_stamp() -> dict[str, str | None]:
    """Return the version stamp for the current eval run.

    Keys match the ``eval_runs`` version columns: ``model``, ``prompt_version``,
    ``harness_sha``, ``taxonomy_version``, ``rules_version``.
    """
    return {
        "model": categorizer_model(),
        "prompt_version": _prompt_version(_CATEGORIZER_PROMPT_KEY),
        "harness_sha": _git_sha(),
        "taxonomy_version": _file_hash(_TAXONOMY_YAML),
        "rules_version": _rules_hash(),
    }
