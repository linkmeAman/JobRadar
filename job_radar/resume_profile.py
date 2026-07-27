"""Build and cache a Job Radar search profile from the current resume PDF."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROFILE_VERSION = 2
_SKILLS = (
    "python",
    "go",
    "php",
    "javascript",
    "sql",
    "fastapi",
    "django",
    "rest api",
    "websocket",
    "mysql",
    "redis",
    "aws",
    "docker",
    "nginx",
    "linux",
    "systemd",
    "ci/cd",
    "microservices",
    "oauth",
    "jwt",
    "rbac",
    "pydantic",
    "next.js",
)


def _contains_term(text: str, term: str) -> bool:
    if term == "go":
        return bool(re.search(r"\b(go|golang)\b", text))
    return term in text


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_text(text: str) -> str:
    """Repair a few common PDF text-extraction splits before matching skills."""
    normalized = text.lower()
    return normalized.replace("a ws", "aws")


def _extract_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install project requirements to read the resume PDF") from exc

    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _roles_for(skills: list[str], text: str) -> list[str]:
    roles = ["backend engineer", "software engineer"]
    if "python" in skills:
        roles.append("python backend engineer")
    if "go" in skills:
        roles.append("golang backend engineer")
    if "fastapi" in skills or "django" in skills:
        roles.append("api engineer")
    if "aws" in skills or "docker" in skills or "systemd" in skills:
        roles.append("platform engineer")
    if "full-stack" in text or "full stack" in text:
        roles.append("full stack engineer")
    return roles


def build_profile(text: str, source_path: Path, source_hash: str) -> dict[str, Any]:
    """Create a focused role and skills profile from extracted resume text."""
    normalized = _normalize_text(text)
    skills = [skill for skill in _SKILLS if _contains_term(normalized, skill)]
    return {
        "version": PROFILE_VERSION,
        "source_path": str(source_path),
        "source_hash": source_hash,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "roles": _roles_for(skills, normalized),
        "skills": skills,
    }


def load_or_refresh(resume_paths: str | list[str], cache_path: str) -> dict[str, Any]:
    """Return a cached profile, rebuilding it whenever any resume changes."""
    configured_paths = [resume_paths] if isinstance(resume_paths, str) else resume_paths
    sources = [Path(path).expanduser() for path in configured_paths]
    cache = Path(cache_path)
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise RuntimeError(f"Configured resume PDF was not found: {', '.join(missing)}")

    source_hashes = {str(path): _source_hash(path) for path in sources}
    source_hash = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if (
                cached.get("version") == PROFILE_VERSION
                and cached.get("source_hash") == source_hash
            ):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    text = "\n".join(_extract_text(source) for source in sources)
    profile = build_profile(text, sources[0], source_hash)
    profile["source_files"] = [str(source) for source in sources]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return profile
