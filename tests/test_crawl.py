"""Resumable full-crawl catalog and incremental-state tests."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pytest import MonkeyPatch

from facehugger.indexer.crawl import CATALOG_URL, advance_catalog, normalize_catalog_record
from facehugger.models import CatalogRepo, InspectedFile, InspectedRepo
from facehugger.state import IndexState


def test_catalog_resumes_from_the_hub_continuation_then_reconciles_removals(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A partial catalog keeps its opaque continuation and reconciles only after completion."""
    state = IndexState(tmp_path / "state.sqlite")
    first = CatalogRepo("one/model", "1" * 40, datetime.now(UTC), 1, False, False, ("a.bin",))
    second = CatalogRepo("two/model", "2" * 40, datetime.now(UTC), 1, False, True, ("b.bin",))
    old = InspectedRepo(
        "removed/model",
        "3" * 40,
        (InspectedFile("old.bin", 1, None, bytes.fromhex("ab" * 32), None, "lfs"),),
    )
    state.replace_repo(old, old.files)
    generation = state.start_catalog_generation()
    responses = [
        httpx.Response(
            200,
            json=[{"id": "one/model"}],
            headers={"Link": '<https://huggingface.co/api/models?cursor=next>; rel="next"'},
            request=httpx.Request("GET", CATALOG_URL),
        ),
        httpx.Response(
            200,
            json=[{"id": "two/model"}],
            request=httpx.Request("GET", CATALOG_URL),
        ),
    ]

    class FakeCatalogClient:
        """Two deterministic catalog pages."""

        def get(self, url: str, *, params: Any = None) -> httpx.Response:
            return responses.pop(0)

    values = iter((first, second))

    def catalog_item(_: object) -> CatalogRepo:
        return next(values)

    monkeypatch.setattr("facehugger.indexer.crawl.normalize_catalog_record", catalog_item)
    try:
        assert advance_catalog(state, FakeCatalogClient(), (".bin",), generation, 1) == (1, False)
        assert (
            state.get_metadata("catalog_next_url")
            == "https://huggingface.co/api/models?cursor=next"
        )
        assert state.catalog_generation_complete() is False
        assert state.lookup(bytes.fromhex("ab" * 32))

        assert advance_catalog(state, FakeCatalogClient(), (".bin",), generation, None) == (
            1,
            False,
        )
        assert state.catalog_generation_complete() is True
        assert state.get_metadata("catalog_next_url") is None
        assert not state.lookup(bytes.fromhex("ab" * 32))
        assert [item.repo_id for item in state.pending_repositories(10)] == [
            "one/model",
            "two/model",
        ]
    finally:
        state.close()


def test_catalog_record_normalizes_the_public_hub_page() -> None:
    """Raw catalog JSON retains the fields used for safe incremental selection."""
    repo = normalize_catalog_record(
        {
            "id": "owner/model",
            "sha": "1" * 40,
            "lastModified": "2026-01-01T00:00:00Z",
            "downloads": 42,
            "private": False,
            "gated": "auto",
            "siblings": [{"rfilename": "weights/model.safetensors"}],
        }
    )
    assert repo.repo_id == "owner/model"
    assert repo.gated is True
    assert repo.sibling_paths == ("weights/model.safetensors",)


def test_catalog_timeout_leaves_a_resumable_continuation(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Transient catalog transport failures do not discard prior durable crawler state."""
    state = IndexState(tmp_path / "state.sqlite")

    class TimeoutCatalogClient:
        """A transport that consistently times out after the crawler's bounded retries."""

        def get(self, url: str, *, params: Any = None) -> httpx.Response:
            request = httpx.Request("GET", url)
            raise httpx.ReadTimeout("timed out", request=request)

    def no_sleep(seconds: float) -> None:
        """Keep the transient-failure regression test deterministic and immediate."""
        del seconds

    try:
        monkeypatch.setattr("facehugger.indexer.crawl.sleep", no_sleep)
        generation = state.start_catalog_generation()
        assert advance_catalog(state, TimeoutCatalogClient(), (".bin",), generation, None) == (
            0,
            True,
        )
        assert state.catalog_generation_complete() is False
        assert state.get_metadata("active_catalog_generation") == str(generation)
    finally:
        state.close()


def test_changed_catalog_revision_is_the_only_reinspection_candidate(tmp_path: Path) -> None:
    """Unchanged repositories are skipped while revisions and gate-state changes are queued."""
    state = IndexState(tmp_path / "state.sqlite")
    repo = CatalogRepo("example/model", "1" * 40, None, 1, False, False, ("model.bin",))
    inspected = InspectedRepo("example/model", "1" * 40, ())
    try:
        generation = state.start_catalog_generation()
        state.record_catalog_repo(repo, eligible=True, generation=generation)
        state.replace_repo(inspected, ())
        assert state.pending_repositories(1) == ()

        changed = CatalogRepo("example/model", "2" * 40, None, 1, False, True, ("model.bin",))
        state.record_catalog_repo(changed, eligible=True, generation=generation)
        pending = state.pending_repositories(1)
        assert pending[0].revision == "2" * 40
        assert pending[0].gated is True
    finally:
        state.close()
