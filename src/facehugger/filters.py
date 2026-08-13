"""Version-controlled model artifact candidate rules."""

import tomllib
from pathlib import Path
from typing import cast


def load_artifact_extensions(path: Path) -> tuple[str, ...]:
    """Load and validate artifact suffixes from a TOML configuration file."""
    raw_config = cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))
    artifacts = raw_config.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Artifact configuration must contain an [artifacts] table.")
    extensions = cast(dict[str, object], artifacts).get("extensions")
    if not isinstance(extensions, list):
        raise ValueError("Artifact extensions must be a list of strings.")
    raw_extensions = cast(list[object], extensions)
    if not all(isinstance(item, str) for item in raw_extensions):
        raise ValueError("Artifact extensions must be a list of strings.")
    normalized = tuple(
        sorted({extension.casefold() for extension in cast(list[str], raw_extensions)})
    )
    if not normalized or any(not extension.startswith(".") for extension in normalized):
        raise ValueError("Artifact extensions must be non-empty dot-prefixed suffixes.")
    return normalized


def is_candidate_artifact(path: str, extensions: tuple[str, ...]) -> bool:
    """Return whether a repository path has a configured artifact suffix."""
    return any(path.casefold().endswith(extension) for extension in extensions)
