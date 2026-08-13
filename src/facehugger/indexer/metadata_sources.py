"""Hugging Face metadata adapters with normalized exact-hash semantics."""

from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol

from huggingface_hub import HfApi
from huggingface_hub.hf_api import RepoFile

from facehugger.errors import MetadataError
from facehugger.hashes import sha256_bytes
from facehugger.models import InspectedFile, InspectedRepo


@dataclass(frozen=True)
class InspectionMeasurement:
    """Observability data captured for one metadata inspection."""

    strategy: str
    duration_seconds: float
    file_count: int
    files_with_sha256: int
    files_with_xet_hash: int


class RepoMetadataSource(Protocol):
    """A source of normalized metadata for one public repository revision."""

    strategy_name: str

    def inspect_repo(
        self, repo_id: str, revision: str | None
    ) -> tuple[InspectedRepo, InspectionMeasurement]:
        """Inspect one repository revision without downloading file contents."""
        raise NotImplementedError


class ModelInfoMetadataSource:
    """Inspect files through ``model_info(files_metadata=True)``."""

    strategy_name = "model_info"

    def __init__(self, api: HfApi, timeout_seconds: float = 30.0) -> None:
        """Create a metadata adapter using the supplied Hub API client."""
        self.api = api
        self.timeout_seconds = timeout_seconds

    def inspect_repo(
        self, repo_id: str, revision: str | None
    ) -> tuple[InspectedRepo, InspectionMeasurement]:
        """Read public file metadata from the model-info endpoint."""
        started = perf_counter()
        try:
            info = self.api.model_info(
                repo_id,
                revision=revision,
                files_metadata=True,
                timeout=self.timeout_seconds,
                token=None,
            )
        except Exception as error:
            raise MetadataError(f"Model metadata inspection failed for {repo_id}.") from error
        resolved_revision = info.sha or revision
        if resolved_revision is None:
            raise MetadataError(f"Model metadata did not contain a revision for {repo_id}.")
        files = tuple(_normalize_sibling(item) for item in info.siblings or [])
        return _result(
            self.strategy_name,
            repo_id,
            resolved_revision,
            files,
            isinstance(getattr(info, "gated", False), str),
            started,
        )


class RepoTreeMetadataSource:
    """Inspect files through ``list_repo_tree(recursive=True)``."""

    strategy_name = "repo_tree"

    def __init__(self, api: HfApi) -> None:
        """Create a metadata adapter using the supplied Hub API client."""
        self.api = api

    def inspect_repo(
        self, repo_id: str, revision: str | None
    ) -> tuple[InspectedRepo, InspectionMeasurement]:
        """Read public file metadata from the repository-tree endpoint."""
        started = perf_counter()
        try:
            entries = self.api.list_repo_tree(
                repo_id,
                revision=revision,
                recursive=True,
                expand=False,
                token=None,
            )
            files = tuple(
                _normalize_tree_file(entry) for entry in entries if isinstance(entry, RepoFile)
            )
        except Exception as error:
            raise MetadataError(f"Repository tree inspection failed for {repo_id}.") from error
        if revision is None:
            try:
                resolved_revision = self.api.model_info(repo_id, token=None).sha
            except Exception as error:
                raise MetadataError(
                    f"Repository tree inspection could not resolve a revision for {repo_id}."
                ) from error
        else:
            resolved_revision = revision
        if resolved_revision is None:
            raise MetadataError(f"Repository tree did not contain a revision for {repo_id}.")
        return _result(self.strategy_name, repo_id, resolved_revision, files, False, started)


def _result(
    strategy: str,
    repo_id: str,
    revision: str,
    files: tuple[InspectedFile, ...],
    gated: bool,
    started: float,
) -> tuple[InspectedRepo, InspectionMeasurement]:
    return (
        InspectedRepo(repo_id=repo_id, revision=revision, files=files, gated=gated),
        InspectionMeasurement(
            strategy=strategy,
            duration_seconds=perf_counter() - started,
            file_count=len(files),
            files_with_sha256=sum(file.content_sha256 is not None for file in files),
            files_with_xet_hash=sum(file.xet_hash is not None for file in files),
        ),
    )


def _normalize_sibling(value: object) -> InspectedFile:
    path = getattr(value, "rfilename", None)
    size = getattr(value, "size", None)
    blob_id = getattr(value, "blob_id", None)
    lfs = getattr(value, "lfs", None)
    return _normalize_file(path, size, blob_id, lfs, None)


def _normalize_tree_file(value: RepoFile) -> InspectedFile:
    return _normalize_file(value.path, value.size, value.blob_id, value.lfs, value.xet_hash)


def _normalize_file(
    path: object,
    size: object,
    blob_id: object,
    lfs: object,
    xet_hash: object,
) -> InspectedFile:
    if not isinstance(path, str) or not path:
        raise MetadataError("Hub metadata included an invalid file path.")
    normalized_size = (
        size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
    )
    normalized_blob_id = blob_id if isinstance(blob_id, str) else None
    normalized_xet_hash = xet_hash if isinstance(xet_hash, str) else None
    lfs_sha256 = getattr(lfs, "sha256", None)
    content_sha256 = None
    if isinstance(lfs_sha256, str):
        try:
            content_sha256 = sha256_bytes(lfs_sha256)
        except ValueError as error:
            raise MetadataError(
                f"Hub metadata included an invalid LFS SHA-256 for {path}."
            ) from error
    storage: Literal["lfs", "xet", "git", "unknown"]
    if content_sha256 is not None:
        storage = "lfs"
    elif normalized_xet_hash is not None:
        storage = "xet"
    elif normalized_blob_id is not None:
        storage = "git"
    else:
        storage = "unknown"
    return InspectedFile(
        path, normalized_size, normalized_blob_id, content_sha256, normalized_xet_hash, storage
    )
