"""Typed domain records for lookup and indexing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import quote

HUGGING_FACE_HUB_URL = "https://huggingface.co"


@dataclass(frozen=True)
class Occurrence:
    """One repository occurrence and its locally derived Hub links."""

    repo_id: str
    path: str
    revision: str
    size: int | None
    storage: str
    gated: bool

    @property
    def repository_url(self) -> str:
        """Return the canonical Hub page for this repository without a network request."""
        return f"{HUGGING_FACE_HUB_URL}/{quote(self.repo_id, safe='/')}"

    @property
    def resolver_url(self) -> str:
        """Return the revision-pinned Hub file resolver URL without a network request."""
        return (
            f"{self.repository_url}/resolve/{quote(self.revision, safe='')}/"
            f"{quote(self.path, safe='/')}"
        )

    def as_dict(self) -> dict[str, str | int | bool | None]:
        """Return the complete public occurrence contract for JSON consumers."""
        return {
            "repo_id": self.repo_id,
            "path": self.path,
            "revision": self.revision,
            "size": self.size,
            "storage": self.storage,
            "gated": self.gated,
            "repository_url": self.repository_url,
            "resolver_url": self.resolver_url,
        }


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
    gated: bool = False


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
