"""Typed domain records for lookup and indexing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Occurrence:
    """One repository occurrence of an exact artifact digest."""

    repo_id: str
    path: str
    revision: str
    size: int | None
    storage: str


@dataclass(frozen=True)
class IndexInfo:
    """Metadata describing the index used for a lookup."""

    version: str
    generated_at: datetime
    catalog_cutoff: datetime | None
    complete: bool


@dataclass(frozen=True)
class LookupResult:
    """Exact matches and index context returned by one lookup."""

    algorithm: Literal["sha256"]
    digest: str
    index: IndexInfo
    matches: tuple[Occurrence, ...]


@dataclass(frozen=True)
class InspectedFile:
    """Normalized metadata for one repository file."""

    path: str
    size: int | None
    git_blob_oid: str | None
    content_sha256: bytes | None
    xet_hash: str | None
    storage: Literal["lfs", "xet", "git", "unknown"]


@dataclass(frozen=True)
class InspectedRepo:
    """One repository revision and its normalized file metadata."""

    repo_id: str
    revision: str
    files: tuple[InspectedFile, ...]


@dataclass(frozen=True)
class CatalogRepo:
    """The catalog fields needed to select a public model repository."""

    repo_id: str
    revision: str | None
    last_modified: datetime | None
    downloads: int | None
    private: bool
    gated: bool
    sibling_paths: tuple[str, ...]
