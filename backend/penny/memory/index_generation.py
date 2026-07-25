from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from promptorium import load_prompt

DEFAULT_MEMORY_INDEX_MODEL = "gemini-3.6-flash"
DEFAULT_MEMORY_INDEX_PROMPT_KEY = "generate-memory-index"
_REQUIRED_HEADINGS = (
    "# Memory Index",
    "## Annotations",
    "## Tax Returns Directory",
)


@dataclass(frozen=True, slots=True)
class MemoryIndexSyncResult:
    """Result metadata for memory index synchronization."""

    updated: bool
    path: Path
    model: str
    reason: str


def generate_memory_index_markdown(
    *,
    memory_dir: Path,
    model: str = DEFAULT_MEMORY_INDEX_MODEL,
    prompt_key: str = DEFAULT_MEMORY_INDEX_PROMPT_KEY,
) -> str:
    """Generate memory/index.md content from the current memory directory."""
    generation_prompt = _load_generation_prompt(prompt_key=prompt_key)
    memory_tree = _build_memory_tree(memory_dir=memory_dir)
    tracked_files = _tracked_memory_files(memory_dir=memory_dir)
    runtime_tax_return_files = _runtime_tax_return_files(memory_dir=memory_dir)

    prompt = generation_prompt
    prompt = prompt.replace("{{MEMORY_TREE}}", memory_tree)
    prompt = prompt.replace("{{TRACKED_MEMORY_FILES}}", _format_lines(tracked_files))
    prompt = prompt.replace(
        "{{RUNTIME_TAX_RETURN_FILES}}", _format_lines(runtime_tax_return_files)
    )

    generated = _normalize_generated_markdown(
        _call_gemini_text(prompt=prompt, model=model)
    )
    _validate_generated_index(generated)
    return f"{generated}\n"


def sync_memory_index(
    *,
    memory_dir: Path,
    model: str = DEFAULT_MEMORY_INDEX_MODEL,
    prompt_key: str = DEFAULT_MEMORY_INDEX_PROMPT_KEY,
    force: bool = False,
) -> MemoryIndexSyncResult:
    """Generate and update memory/index.md only when content has changed."""
    generated = generate_memory_index_markdown(
        memory_dir=memory_dir,
        model=model,
        prompt_key=prompt_key,
    )
    index_path = memory_dir / "index.md"
    existing = index_path.read_text() if index_path.exists() else ""

    if not force and existing == generated:
        return MemoryIndexSyncResult(
            updated=False,
            path=index_path,
            model=model,
            reason="content unchanged",
        )

    index_path.write_text(generated)
    reason = "forced rewrite" if force else "content changed"
    return MemoryIndexSyncResult(
        updated=True,
        path=index_path,
        model=model,
        reason=reason,
    )


def _build_memory_tree(*, memory_dir: Path) -> str:
    if not memory_dir.exists() or not memory_dir.is_dir():
        return f"{memory_dir.name}/"

    lines = [f"{memory_dir.name}/"]
    _append_tree_lines(lines=lines, directory=memory_dir, prefix="")
    return "\n".join(lines)


def _append_tree_lines(*, lines: list[str], directory: Path, prefix: str) -> None:
    entries = sorted(
        (
            entry
            for entry in directory.iterdir()
            if not _should_ignore_from_index(candidate=entry)
        ),
        key=lambda item: (not item.is_dir(), item.name),
    )
    for idx, entry in enumerate(entries):
        is_last = idx == len(entries) - 1
        branch = "`-- " if is_last else "|-- "
        display_name = f"{entry.name}/" if entry.is_dir() else entry.name
        lines.append(f"{prefix}{branch}{display_name}")
        if entry.is_dir():
            child_prefix = f"{prefix}{'    ' if is_last else '|   '}"
            _append_tree_lines(lines=lines, directory=entry, prefix=child_prefix)


def _tracked_memory_files(*, memory_dir: Path) -> list[str]:
    """Return memory file paths relative to memory_dir, excluding .example files."""
    if not memory_dir.exists() or not memory_dir.is_dir():
        return []

    files: list[str] = []
    for candidate in memory_dir.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.name.endswith(".example"):
            continue
        files.append(candidate.relative_to(memory_dir).as_posix())
    return sorted(files)


def _runtime_tax_return_files(*, memory_dir: Path) -> list[str]:
    tax_returns_dir = memory_dir / "tax-returns"
    if not tax_returns_dir.exists() or not tax_returns_dir.is_dir():
        return []

    files: list[str] = []
    for candidate in tax_returns_dir.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.name.endswith(".example"):
            continue
        files.append(candidate.relative_to(memory_dir).as_posix())
    return sorted(files)


def _should_ignore_from_index(*, candidate: Path) -> bool:
    return candidate.is_file() and candidate.name.endswith(".example")


def _format_lines(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {item}" for item in items)


def _validate_generated_index(generated: str) -> None:
    if not generated:
        raise ValueError("Generated memory index is empty")
    missing_headings = [
        heading for heading in _REQUIRED_HEADINGS if heading not in generated
    ]
    if missing_headings:
        raise ValueError(
            "Generated memory index missing required headings: "
            f"{', '.join(missing_headings)}"
        )


def _load_generation_prompt(*, prompt_key: str) -> str:
    prompt_text = str(load_prompt(prompt_key))
    if prompt_text.strip():
        return prompt_text

    fallback_path = Path("prompts") / f"{prompt_key}.md"
    if fallback_path.exists():
        fallback_text = fallback_path.read_text()
        if fallback_text.strip():
            return fallback_text

    raise ValueError(f"Prompt '{prompt_key}' is empty or not found")


def _normalize_generated_markdown(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    return stripped


def _call_gemini_text(*, prompt: str, model: str) -> str:
    """Call Gemini and return plain text output."""
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is required for memory index generation")

    try:
        from google.genai.client import Client
    except ImportError as e:
        raise RuntimeError(
            "google-genai package is required for memory index generation"
        ) from e

    client = Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    payload = _to_jsonable(response)
    fallback_text = _extract_text_from_payload(payload)
    if fallback_text:
        return fallback_text
    return json.dumps(payload, indent=2)


def _to_jsonable(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        dumped = response.model_dump()
        if isinstance(dumped, dict):
            return dumped
    return {"response": str(response)}


def _extract_text_from_payload(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return ""

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        text_chunks: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_text = part.get("text")
            if isinstance(part_text, str) and part_text.strip():
                text_chunks.append(part_text)
        if text_chunks:
            return "\n".join(text_chunks).strip()
    return ""
