"""Resumable full-crawl catalog and incremental-state tests."""

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pytest import MonkeyPatch

from facehugger.indexer.crawl import run_full_crawl
from facehugger.indexer.hub_catalog import (
    CATALOG_URL,
    advance_catalog,
    normalize_catalog_record,
)
from facehugger.indexer.metadata_sources import InspectionMeasurement
from facehugger.models import CatalogRepo, InspectedFile, InspectedRepo
from facehugger.state import IndexState


def test_compilation_failure_keeps_inspection_progress_report_current(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Publication failure reports completed inspections without losing verified records."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "artifacts.toml").write_text("[artifacts]\nextensions = ['.bin']\n")
    (tmp_path / ".facehugger").mkdir()
    state = IndexState(tmp_path / ".facehugger" / "full.sqlite")
    try:
        generation = state.start_catalog_generation()
        state.record_catalog_repo(
            CatalogRepo("owner/model", "1" * 40, None, 1, False, False, ("model.bin",)),
            eligible=True,
            generation=generation,
        )
        state.replace_repo(InspectedRepo("owner/model", "1" * 40, ()), ())
        state.finish_catalog_generation(generation)
    finally:
        state.close()

    def skip_configuration(*args: object) -> None:
        """Avoid process-wide HTTP configuration for the offline fixture."""

    def fail_compilation(*args: object, **kwargs: object) -> None:
        """Represent an enforced publication boundary failure."""
        raise RuntimeError("publication limit")

    monkeypatch.setattr("facehugger.indexer.crawl.configure_hub_http", skip_configuration)
    monkeypatch.setattr("facehugger.indexer.crawl.compile_site", fail_compilation)
    with pytest.raises(RuntimeError, match="publication limit"):
        run_full_crawl(root=tmp_path, token="test", version="failed-publication")
    report = json.loads((tmp_path / "reports" / "full-crawl.json").read_text())
    assert report["indexed_repositories"] == 1
    assert report["pending_repositories"] == 0
    assert report["published"] is False


def test_full_crawl_publishes_after_a_gate_change_since_cataloging(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A successful inspection with newer gate metadata can complete publication."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "artifacts.toml").write_text("[artifacts]\nextensions = ['.bin']\n")
    (tmp_path / ".facehugger").mkdir()
    state = IndexState(tmp_path / ".facehugger" / "full.sqlite")
    try:
        generation = state.start_catalog_generation()
        state.record_catalog_repo(
            CatalogRepo("owner/model", "1" * 40, None, 1, False, False, ("model.bin",)),
            eligible=True,
            generation=generation,
        )
        state.finish_catalog_generation(generation)
    finally:
        state.close()

    class HubApi:
        """Return a gated revision whose catalog observation was ungated."""

        def model_info(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(sha="1" * 40, siblings=[], gated="auto")

    def skip_hub_configuration(*args: object) -> None:
        """Keep the controlled test independent of process-wide HTTP configuration."""

    def create_test_api(token: str) -> HubApi:
        """Return the metadata-only test transport."""
        return HubApi()

    monkeypatch.setattr("facehugger.indexer.crawl.configure_hub_http", skip_hub_configuration)
    monkeypatch.setattr("facehugger.indexer.crawl.create_hub_api", create_test_api)
    progress = run_full_crawl(root=tmp_path, token="test", version="gate-change")

    assert progress.inspections == 1
    assert progress.pending_repositories == 0
    assert progress.published is True
    assert progress.next_invocation_ready is False
    assert (tmp_path / "site-staging" / "api" / "v1" / "manifest.json").is_file()


@pytest.mark.parametrize("split_refresh", [False, True])
def test_legacy_checkpoint_refreshes_despite_failed_compilation(
    monkeypatch: MonkeyPatch, tmp_path: Path, split_refresh: bool
) -> None:
    """New and changed repos join a restored, unpublished catalog without reindexing it."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "artifacts.toml").write_text("[artifacts]\nextensions = ['.bin']\n")
    (tmp_path / ".facehugger").mkdir()
    database = tmp_path / ".facehugger" / "full.sqlite"
    original_digest = bytes.fromhex("ab" * 32)
    state = IndexState(database)
    try:
        state.set_metadata("active_catalog_generation", "1")
        state.set_metadata("catalog_generation_complete", "true")
        for name in ("owner/unchanged", "owner/changed"):
            state.record_catalog_repo(
                CatalogRepo(name, "1" * 40, None, 1, False, False, ("model.bin",)),
                eligible=True,
                generation=1,
            )
            files = (InspectedFile("model.bin", 1, None, original_digest, None, "lfs"),)
            state.replace_repo(InspectedRepo(name, "1" * 40, files), files)
    finally:
        state.close()

    requested: list[tuple[str, str | None]] = []

    class HubApi:
        """Serve metadata only for the new and changed repositories."""

        def model_info(self, repo_id: str, *, revision: str | None, **_: object) -> SimpleNamespace:
            requested.append((repo_id, revision))
            return SimpleNamespace(sha=revision, siblings=[], gated=False)

    times = {"now": 0.0}
    continuation = f"{CATALOG_URL}?cursor=next"

    def catalog_response(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") is None:
            assert request.url.params["sort"] == "createdAt"
            items = (("new", "3"), ("changed", "2"), ("unchanged", "1"))
            if split_refresh:
                times["now"] = 41.0
                return httpx.Response(
                    200,
                    json=[catalog_item(name, revision) for name, revision in items[:2]],
                    headers={"Link": f'<{continuation}>; rel="next"'},
                )
        else:
            assert str(request.url) == continuation
            items = (("unchanged", "1"),)
        return httpx.Response(200, json=[catalog_item(name, revision) for name, revision in items])

    def catalog_item(name: str, revision: str) -> dict[str, object]:
        return {
            "id": f"owner/{name}",
            "sha": revision * 40,
            "siblings": [{"rfilename": "model.bin"}],
        }

    def fail_compilation(*args: object, **kwargs: object) -> None:
        raise RuntimeError("publication limit")

    def skip_configuration(*_: object) -> None:
        """Avoid process-wide configuration in the controlled offline fixture."""

    def test_api(_: str) -> HubApi:
        return HubApi()

    def test_catalog_client(*_: object) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(catalog_response))

    monkeypatch.setattr("facehugger.indexer.crawl.configure_hub_http", skip_configuration)
    monkeypatch.setattr("facehugger.indexer.crawl.create_hub_api", test_api)
    monkeypatch.setattr("facehugger.indexer.crawl.create_catalog_client", test_catalog_client)
    monkeypatch.setattr("facehugger.indexer.crawl.compile_site", fail_compilation)
    monkeypatch.setattr("facehugger.indexer.crawl.monotonic", lambda: times["now"])
    monkeypatch.setattr("facehugger.indexer.hub_catalog.monotonic", lambda: times["now"])
    if split_refresh:
        progress = run_full_crawl(
            root=tmp_path, token="test", version="partial-refresh", time_limit_minutes=1
        )
        assert progress.catalog_complete is False
        assert progress.next_invocation_ready is True
        assert progress.cataloged_repositories == 3
        assert progress.inspections == 2
        state = IndexState(database)
        try:
            assert [match.repo_id for match in state.lookup(original_digest)] == ["owner/unchanged"]
            assert state.get_metadata("catalog_next_url") == continuation
        finally:
            state.close()

    with pytest.raises(RuntimeError, match="publication limit"):
        run_full_crawl(root=tmp_path, token="test", version="failed-publication")

    report = json.loads((tmp_path / "reports" / "full-crawl.json").read_text())
    assert report["generation"] == 2
    assert report["catalog_pages"] == 1
    assert report["cataloged_repositories"] == 3
    assert report["inspections"] == (0 if split_refresh else 2)
    assert report["pending_repositories"] == 0
    assert report["published"] is False
    assert sorted(requested) == [("owner/changed", "2" * 40), ("owner/new", "3" * 40)]
    state = IndexState(database)
    try:
        assert [match.repo_id for match in state.lookup(original_digest)] == ["owner/unchanged"]
    finally:
        state.close()


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

    monkeypatch.setattr("facehugger.indexer.hub_catalog.normalize_catalog_record", catalog_item)
    try:
        monotonic_values = iter((0.0, 1.0))
        monkeypatch.setattr(
            "facehugger.indexer.hub_catalog.monotonic", lambda: next(monotonic_values)
        )
        assert advance_catalog(state, FakeCatalogClient(), (".bin",), generation, 1.0) == (1, False)
        assert (
            state.get_metadata("catalog_next_url")
            == "https://huggingface.co/api/models?cursor=next"
        )
        assert state.catalog_generation_complete() is False
        assert state.lookup(bytes.fromhex("ab" * 32))

        monkeypatch.setattr("facehugger.indexer.hub_catalog.monotonic", lambda: 0.0)
        assert advance_catalog(state, FakeCatalogClient(), (".bin",), generation, float("inf")) == (
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
        monkeypatch.setattr("facehugger.indexer.hub_catalog.sleep", no_sleep)
        generation = state.start_catalog_generation()
        assert advance_catalog(
            state, TimeoutCatalogClient(), (".bin",), generation, float("inf")
        ) == (
            0,
            True,
        )
        assert state.catalog_generation_complete() is False
        assert state.get_metadata("active_catalog_generation") == str(generation)
    finally:
        state.close()


def test_full_crawl_stops_scheduling_new_groups_at_its_deadline(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A crawl deadline leaves work after its bounded in-flight inspection group."""

    class Clock:
        """A deterministic monotonic clock advanced by each fake inspection."""

        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    class FakeMetadataSource:
        """A metadata source that consumes half the invocation budget per inspection."""

        def __init__(self, api: object) -> None:
            del api

        def inspect_repo(
            self, repo_id: str, revision: str | None
        ) -> tuple[InspectedRepo, InspectionMeasurement]:
            clock.value += 30.0
            return (
                InspectedRepo(repo_id, revision or "0" * 40, ()),
                InspectionMeasurement("test", 30.0, 0, 0, 0),
            )

    config = tmp_path / "config"
    config.mkdir()
    (config / "artifacts.toml").write_text("[artifacts]\nextensions = ['.bin']\n", encoding="utf-8")
    state_directory = tmp_path / ".facehugger"
    state_directory.mkdir()
    state = IndexState(state_directory / "full.sqlite")
    try:
        generation = state.start_catalog_generation()
        for index in range(16):
            state.record_catalog_repo(
                CatalogRepo(
                    f"owner/model-{index}",
                    str(index) * 40,
                    None,
                    1,
                    False,
                    False,
                    ("model.bin",),
                ),
                eligible=True,
                generation=generation,
            )
        state.finish_catalog_generation(generation)
    finally:
        state.close()

    clock = Clock()

    def skip_hub_configuration(*values: object) -> None:
        """Avoid installing process-wide HTTP state in the deterministic test."""
        del values

    def fake_hub_api(token: str) -> object:
        """Return an opaque API value consumed only by the fake metadata source."""
        del token
        return object()

    monkeypatch.setattr("facehugger.indexer.crawl.monotonic", clock)
    monkeypatch.setattr("facehugger.indexer.crawl.configure_hub_http", skip_hub_configuration)
    monkeypatch.setattr("facehugger.indexer.crawl.create_hub_api", fake_hub_api)
    monkeypatch.setattr("facehugger.indexer.crawl.ModelInfoMetadataSource", FakeMetadataSource)

    progress = run_full_crawl(
        root=tmp_path,
        token="test-token",
        version="test",
        time_limit_minutes=1,
    )

    assert progress.inspections == 8
    assert progress.pending_repositories == 8
    assert progress.next_invocation_ready is True


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
        assert state.eligible_repository_count() == 1
        assert state.indexed_repository_count() == 1

        changed = CatalogRepo("example/model", "2" * 40, None, 1, False, True, ("model.bin",))
        state.record_catalog_repo(changed, eligible=True, generation=generation)
        pending = state.pending_repositories(1)
        assert pending[0].revision == "2" * 40
        assert pending[0].gated is True
        assert state.eligible_repository_count() == 1
        assert state.indexed_repository_count() == 1
    finally:
        state.close()


def test_pending_inspections_run_concurrently_while_state_writes_remain_serial(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """Multiple outbound inspections can start before the crawler persists their results."""
    active = 0
    maximum_active = 0
    active_lock = Lock()
    concurrent_start = Event()

    class ConcurrentMetadataSource:
        """Metadata source that records whether the crawler issues concurrent reads."""

        def __init__(self, api: object) -> None:
            del api

        def inspect_repo(
            self, repo_id: str, revision: str | None
        ) -> tuple[InspectedRepo, InspectionMeasurement]:
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active >= 2:
                    concurrent_start.set()
            concurrent_start.wait(timeout=2)
            with active_lock:
                active -= 1
            return (
                InspectedRepo(repo_id, revision or "0" * 40, ()),
                InspectionMeasurement("test", 0.0, 0, 0, 0),
            )

    config = tmp_path / "config"
    config.mkdir()
    (config / "artifacts.toml").write_text("[artifacts]\nextensions = ['.bin']\n", encoding="utf-8")
    state_directory = tmp_path / ".facehugger"
    state_directory.mkdir()
    state = IndexState(state_directory / "full.sqlite")
    try:
        generation = state.start_catalog_generation()
        for index in range(2):
            state.record_catalog_repo(
                CatalogRepo(
                    f"owner/model-{index}",
                    str(index) * 40,
                    None,
                    1,
                    False,
                    False,
                    ("model.bin",),
                ),
                eligible=True,
                generation=generation,
            )
        state.finish_catalog_generation(generation)
    finally:
        state.close()

    def skip_hub_configuration(*values: object) -> None:
        """Avoid installing process-wide HTTP state in the deterministic test."""
        del values

    def fake_hub_api(token: str) -> object:
        """Return an opaque API value consumed only by the fake metadata source."""
        del token
        return object()

    monkeypatch.setattr(
        "facehugger.indexer.crawl.ModelInfoMetadataSource", ConcurrentMetadataSource
    )
    monkeypatch.setattr("facehugger.indexer.crawl.configure_hub_http", skip_hub_configuration)
    monkeypatch.setattr("facehugger.indexer.crawl.create_hub_api", fake_hub_api)

    progress = run_full_crawl(
        root=tmp_path,
        token="test-token",
        version="test",
        time_limit_minutes=1,
    )

    assert progress.inspections == 2
    assert maximum_active >= 2
