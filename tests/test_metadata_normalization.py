"""Hugging Face metadata normalization tests."""

from types import SimpleNamespace
from typing import cast

import pytest
from huggingface_hub import HfApi

from facehugger.errors import MetadataError
from facehugger.indexer.metadata_sources import ModelInfoMetadataSource


class FakeApi:
    """Minimal model-info source with captured public response fields."""

    def __init__(self, sibling: object, gated: str | bool = False) -> None:
        self.sibling = sibling
        self.gated = gated

    def model_info(self, *_: object, **__: object) -> object:
        return SimpleNamespace(sha="1" * 40, siblings=[self.sibling], gated=self.gated)


def test_model_info_normalizes_only_lfs_sha256() -> None:
    """Git and Xet storage identifiers never become artifact SHA-256 values."""
    sibling = SimpleNamespace(
        rfilename="model.safetensors",
        size=42,
        blob_id="git-blob-id",
        lfs=SimpleNamespace(sha256="ab" * 32),
    )
    source = ModelInfoMetadataSource(cast(HfApi, FakeApi(sibling)))
    inspected, _ = source.inspect_repo("owner/model", None)
    file = inspected.files[0]
    assert file.content_sha256 == bytes.fromhex("ab" * 32)
    assert file.git_blob_oid == "git-blob-id"
    assert file.xet_hash is None
    assert file.storage == "lfs"


def test_model_info_preserves_the_public_gate_state() -> None:
    """Public gated metadata survives normalization for the lookup contract."""
    sibling = SimpleNamespace(rfilename="model.safetensors", size=42, blob_id=None, lfs=None)
    source = ModelInfoMetadataSource(cast(HfApi, FakeApi(sibling, gated="auto")))
    inspected, _ = source.inspect_repo("owner/model", None)
    assert inspected.gated is True


def test_model_info_rejects_invalid_lfs_sha256() -> None:
    """Malformed LFS metadata cannot enter the exact reverse index."""
    sibling = SimpleNamespace(
        rfilename="model.safetensors",
        size=42,
        blob_id="git-blob-id",
        lfs=SimpleNamespace(sha256="not-a-digest"),
    )
    source = ModelInfoMetadataSource(cast(HfApi, FakeApi(sibling)))
    with pytest.raises(MetadataError):
        source.inspect_repo("owner/model", None)
