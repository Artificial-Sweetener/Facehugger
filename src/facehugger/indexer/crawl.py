"""Resumable full-catalog crawling and staged static-index compilation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from huggingface_hub import HfApi

from facehugger.errors import RepositoryUnavailableError
from facehugger.filters import is_candidate_artifact, load_artifact_extensions
from facehugger.indexer.compiler import compile_site
from facehugger.indexer.crawl_reports import (
    write_full_crawl_report,
    write_full_crawl_status_badge,
)
from facehugger.indexer.hub import configure_hub_http, create_hub_api
from facehugger.indexer.hub_catalog import advance_catalog, create_catalog_client
from facehugger.indexer.metadata_sources import ModelInfoMetadataSource
from facehugger.indexer.rate_limit import RateController, RequestMetrics
from facehugger.models import PendingRepo
from facehugger.state import IndexState

CRAWL_REQUESTS_PER_MINUTE = 180
DEFAULT_CRAWL_TIME_LIMIT_MINUTES = 270
PENDING_REPOSITORY_PAGE_SIZE = 1_000
INSPECTION_WORKERS = 8
MAX_PUBLISHED_SITE_BYTES = 900 * 1024 * 1024


@dataclass(frozen=True)
class CrawlProgress:
    """Durable work completed by one bounded full-crawl invocation."""

    generation: int
    catalog_pages: int
    catalog_complete: bool
    catalog_stalled: bool
    cataloged_repositories: int
    inspections: int
    eligible_repositories: int
    indexed_repositories: int
    pending_repositories: int
    published: bool

    @property
    def next_invocation_ready(self) -> bool:
        """Return whether a completed invocation can immediately continue the crawl."""
        return not self.published and not self.catalog_stalled

    def as_dict(self) -> dict[str, int | bool]:
        """Return a stable JSON-safe progress record."""
        return {
            "generation": self.generation,
            "catalog_pages": self.catalog_pages,
            "catalog_complete": self.catalog_complete,
            "catalog_stalled": self.catalog_stalled,
            "cataloged_repositories": self.cataloged_repositories,
            "inspections": self.inspections,
            "eligible_repositories": self.eligible_repositories,
            "indexed_repositories": self.indexed_repositories,
            "pending_repositories": self.pending_repositories,
            "published": self.published,
            "next_invocation_ready": self.next_invocation_ready,
        }


def run_full_crawl(
    *,
    root: Path,
    token: str,
    version: str,
    time_limit_minutes: int = DEFAULT_CRAWL_TIME_LIMIT_MINUTES,
) -> CrawlProgress:
    """Advance one crash-safe catalog generation and stage a complete index when ready."""
    if time_limit_minutes <= 0:
        raise ValueError("Crawl time limit must be positive.")
    state_path = root / ".facehugger" / "full.sqlite"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = IndexState(state_path)
    metrics = RequestMetrics()
    controller = RateController(CRAWL_REQUESTS_PER_MINUTE)
    started = monotonic()
    time_budget = time_limit_minutes * 60
    deadline = started + time_budget
    configure_hub_http(metrics, controller)
    api = create_hub_api(token)
    extensions = load_artifact_extensions(root / "config" / "artifacts.toml")
    generation = state.start_catalog_generation()
    has_baseline = state.has_completed_catalog()
    catalog_client = create_catalog_client(token, metrics, controller)
    try:
        catalog_page_count = 0
        if not state.catalog_generation_complete():
            catalog_page_count, catalog_stalled = advance_catalog(
                state,
                catalog_client,
                extensions,
                generation,
                started + time_budget * 2 / 3 if has_baseline else deadline,
            )
        else:
            catalog_stalled = False
        inspections = 0
        if has_baseline or state.catalog_generation_complete():
            inspections = _inspect_pending(state, api, extensions, deadline)
        catalog_complete = state.catalog_generation_complete()
        pending = state.pending_repository_count()
        progress = CrawlProgress(
            generation=generation,
            catalog_pages=catalog_page_count,
            catalog_complete=catalog_complete,
            catalog_stalled=catalog_stalled,
            cataloged_repositories=state.cataloged_repository_count(),
            inspections=inspections,
            eligible_repositories=state.eligible_repository_count(),
            indexed_repositories=state.indexed_repository_count(),
            pending_repositories=pending,
            published=False,
        )
        write_full_crawl_report(root, progress, metrics)
        if catalog_complete and not catalog_stalled and pending == 0:
            state.validate()
            _compile_staged_site(state, root, version)
            progress = replace(progress, published=True)
            write_full_crawl_report(root, progress, metrics)
        write_full_crawl_status_badge(root, "idle")
        return progress
    finally:
        catalog_client.close()
        state.close()


def _inspect_pending(
    state: IndexState, api: HfApi, extensions: tuple[str, ...], deadline: float
) -> int:
    """Inspect and atomically replace pending repositories until the invocation deadline."""
    inspected = 0
    cursor: str | None = None
    with ThreadPoolExecutor(max_workers=INSPECTION_WORKERS) as executor:
        source = ModelInfoMetadataSource(api)
        while monotonic() < deadline:
            pending_batch = state.pending_repositories_after(cursor, PENDING_REPOSITORY_PAGE_SIZE)
            if not pending_batch:
                break
            for pending_group in _inspection_groups(pending_batch):
                if monotonic() >= deadline:
                    return inspected
                futures = tuple(
                    (
                        pending,
                        executor.submit(source.inspect_repo, pending.repo_id, pending.revision),
                    )
                    for pending in pending_group
                )
                for pending, future in futures:
                    try:
                        result, _ = future.result()
                    except RepositoryUnavailableError:
                        state.record_repository_unavailable(
                            pending.repo_id, pending.revision, pending.gated
                        )
                        continue
                    except Exception:
                        state.record_inspection_failure(pending.repo_id)
                        continue
                    candidates = tuple(
                        file
                        for file in result.files
                        if is_candidate_artifact(file.path, extensions)
                    )
                    state.replace_repo(result, candidates)
                    inspected += 1
            cursor = pending_batch[-1].repo_id
    return inspected


def _inspection_groups(
    pending_repositories: tuple[PendingRepo, ...],
) -> tuple[tuple[PendingRepo, ...], ...]:
    """Partition pending repository work into bounded concurrent inspection groups."""
    return tuple(
        pending_repositories[index : index + INSPECTION_WORKERS]
        for index in range(0, len(pending_repositories), INSPECTION_WORKERS)
    )


def _compile_staged_site(state: IndexState, root: Path, version: str) -> None:
    """Compile a validated complete index into a new immutable staging directory."""
    staging = root / "site-staging"
    if staging.exists():
        raise RuntimeError(
            "A prior staged site remains; publication must finish before recompiling."
        )
    compile_site(
        state,
        staging,
        version=version,
        generated_at=datetime.now(UTC),
        catalog_cutoff=state.catalog_started_at(),
        complete=True,
    )
    size = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
    if size > MAX_PUBLISHED_SITE_BYTES:
        raise RuntimeError("Complete static index exceeds the configured publication size limit.")
