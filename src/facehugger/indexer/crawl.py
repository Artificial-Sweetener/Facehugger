"""Resumable full-catalog crawling and staged static-index compilation."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Protocol, cast

import httpx
from huggingface_hub import HfApi

from facehugger.filters import is_candidate_artifact, load_artifact_extensions
from facehugger.indexer.crawl_reports import (
    write_full_crawl_report,
    write_full_crawl_status_badge,
)
from facehugger.indexer.hub import configure_hub_http, create_hub_api
from facehugger.indexer.metadata_sources import ModelInfoMetadataSource
from facehugger.indexer.rate_limit import RateController, RequestMetrics
from facehugger.models import CatalogRepo
from facehugger.shard_format import compile_site
from facehugger.state import IndexState

CATALOG_URL = "https://huggingface.co/api/models"
CATALOG_PAGE_SIZE = 100
CRAWL_REQUESTS_PER_MINUTE = 100
DEFAULT_CRAWL_TIME_LIMIT_MINUTES = 270
PENDING_REPOSITORY_PAGE_SIZE = 1_000
MAX_PUBLISHED_SITE_BYTES = 900 * 1024 * 1024
CATALOG_RETRY_ATTEMPTS = 3


class CatalogClient(Protocol):
    """Public-model catalog transport required by the resumable crawler."""

    def get(self, url: str, *, params: Any = None) -> httpx.Response:
        """Return one response for the requested public catalog page."""
        raise NotImplementedError


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
    deadline = monotonic() + time_limit_minutes * 60
    configure_hub_http(metrics, controller)
    api = create_hub_api(token)
    extensions = load_artifact_extensions(root / "config" / "artifacts.toml")
    generation = state.start_catalog_generation()
    catalog_client = _catalog_client(token, metrics, controller)
    try:
        catalog_page_count = 0
        if not state.catalog_generation_complete():
            catalog_page_count, catalog_stalled = advance_catalog(
                state,
                catalog_client,
                extensions,
                generation,
                deadline,
            )
        else:
            catalog_stalled = False
        inspections = 0
        if state.catalog_generation_complete() and not catalog_stalled:
            inspections = _inspect_pending(state, api, extensions, deadline)
        catalog_complete = state.catalog_generation_complete()
        pending = state.pending_repository_count()
        published = False
        if catalog_complete and not catalog_stalled and pending == 0:
            state.validate()
            _compile_staged_site(state, root, version)
            state.complete_catalog_generation()
            published = True
        progress = CrawlProgress(
            generation=generation,
            catalog_pages=catalog_page_count,
            catalog_complete=catalog_complete,
            catalog_stalled=catalog_stalled,
            cataloged_repositories=_cataloged_count(state, generation),
            inspections=inspections,
            eligible_repositories=state.eligible_repository_count(),
            indexed_repositories=state.indexed_repository_count(),
            pending_repositories=pending,
            published=published,
        )
        write_full_crawl_report(root, progress, metrics)
        write_full_crawl_status_badge(root, "idle")
        return progress
    finally:
        catalog_client.close()
        state.close()


def _catalog_client(
    token: str, metrics: RequestMetrics, controller: RateController
) -> httpx.Client:
    """Create the direct paginated catalog client with safe request accounting."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "facehugger/0.0.0 (+https://github.com/Artificial-Sweetener/Facehugger)",
    }

    def request_hook(_: httpx.Request) -> None:
        controller.wait()

    def response_hook(response: httpx.Response) -> None:
        metrics.observe(response)
        controller.observe(response)

    return httpx.Client(
        headers=headers,
        event_hooks={"request": [request_hook], "response": [response_hook]},
        follow_redirects=True,
        timeout=30.0,
    )


def advance_catalog(
    state: IndexState,
    client: CatalogClient,
    extensions: tuple[str, ...],
    generation: int,
    deadline: float,
) -> tuple[int, bool]:
    """Persist catalog pages until exhausted or the invocation deadline is reached."""
    next_url = state.get_metadata("catalog_next_url")
    pages = 0
    while monotonic() < deadline:
        response = _catalog_response(
            client,
            CATALOG_URL if next_url is None else next_url,
            _catalog_params() if next_url is None else None,
        )
        if response is None:
            return pages, True
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Model catalog returned an invalid page.")
        observations = tuple(
            (repo, _eligible(repo, extensions))
            for repo in (normalize_catalog_record(item) for item in cast(list[object], payload))
        )
        state.record_catalog_repositories(observations, generation=generation)
        pages += 1
        next_url = _next_url(response)
        if next_url is None:
            state.finish_catalog_generation(generation)
            return pages, False
        state.set_metadata("catalog_next_url", next_url)
    return pages, False


def _catalog_params() -> dict[str, object]:
    """Return the complete deterministic public model catalog request."""
    return {
        "sort": "lastModified",
        "direction": "-1",
        "limit": CATALOG_PAGE_SIZE,
        "expand": ["sha", "siblings", "lastModified", "private", "gated", "downloads"],
    }


def _catalog_response(
    client: CatalogClient, url: str, params: dict[str, object] | None
) -> httpx.Response | None:
    """Return one catalog page, retrying only transient Hub failures within this invocation."""
    for attempt in range(CATALOG_RETRY_ATTEMPTS):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {429, 500, 502, 503, 504}:
                raise
        except httpx.TransportError:
            pass
        if attempt + 1 < CATALOG_RETRY_ATTEMPTS:
            sleep(2**attempt)
    return None


def normalize_catalog_record(value: object) -> CatalogRepo:
    """Normalize one public REST catalog object through the common typed contract."""
    if not isinstance(value, dict):
        raise RuntimeError("Model catalog included an invalid repository.")
    mapping = cast(dict[str, Any], value)
    repo_id = mapping.get("id")
    if not isinstance(repo_id, str) or not repo_id:
        raise RuntimeError("Model catalog included an invalid repository identifier.")
    revision = mapping.get("sha")
    if revision is not None and not isinstance(revision, str):
        raise RuntimeError("Model catalog included an invalid revision.")
    raw_modified = mapping.get("lastModified")
    if raw_modified is None:
        modified = None
    elif isinstance(raw_modified, str):
        try:
            modified = datetime.fromisoformat(raw_modified.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError("Model catalog included an invalid modification time.") from error
    else:
        raise RuntimeError("Model catalog included an invalid modification time.")
    raw_downloads = mapping.get("downloads")
    downloads = (
        raw_downloads
        if isinstance(raw_downloads, int) and not isinstance(raw_downloads, bool)
        else None
    )
    raw_siblings = cast(list[object], mapping.get("siblings", []))
    sibling_paths = _sibling_paths(raw_siblings)
    return CatalogRepo(
        repo_id=repo_id,
        revision=revision,
        last_modified=modified,
        downloads=downloads,
        private=bool(mapping.get("private")),
        gated=isinstance(mapping.get("gated"), str),
        sibling_paths=sibling_paths,
    )


def _next_url(response: httpx.Response) -> str | None:
    """Extract the opaque public continuation URL supplied by the Hub."""
    next_link = response.links.get("next")
    if next_link is None:
        return None
    url = next_link.get("url")
    return url if isinstance(url, str) and url.startswith(CATALOG_URL) else None


def _sibling_paths(values: list[object]) -> tuple[str, ...]:
    """Extract valid candidate paths from one untrusted catalog siblings field."""
    paths: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        sibling = cast(dict[str, object], value)
        path = sibling.get("rfilename")
        if isinstance(path, str):
            paths.append(path)
    return tuple(paths)


def _eligible(repo: CatalogRepo, extensions: tuple[str, ...]) -> bool:
    """Return whether a public catalog entry has a supported candidate artifact path."""
    return not repo.private and any(
        is_candidate_artifact(path, extensions) for path in repo.sibling_paths
    )


def _inspect_pending(
    state: IndexState, api: HfApi, extensions: tuple[str, ...], deadline: float
) -> int:
    """Inspect and atomically replace pending repositories until the invocation deadline."""
    source = ModelInfoMetadataSource(api)
    inspected = 0
    cursor: str | None = None
    while monotonic() < deadline:
        pending_batch = state.pending_repositories_after(cursor, PENDING_REPOSITORY_PAGE_SIZE)
        if not pending_batch:
            break
        for pending in pending_batch:
            if monotonic() >= deadline:
                return inspected
            try:
                result, _ = source.inspect_repo(pending.repo_id, pending.revision)
            except Exception:
                state.record_inspection_failure(pending.repo_id)
                continue
            candidates = tuple(
                file for file in result.files if is_candidate_artifact(file.path, extensions)
            )
            state.replace_repo(result, candidates)
            inspected += 1
        cursor = pending_batch[-1].repo_id
    return inspected


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
        catalog_cutoff=datetime.now(UTC),
        complete=True,
    )
    size = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
    if size > MAX_PUBLISHED_SITE_BYTES:
        raise RuntimeError("Complete static index exceeds the configured publication size limit.")


def _cataloged_count(state: IndexState, generation: int) -> int:
    """Return the number of records observed in this active catalog generation."""
    return int(
        state.connection.execute(
            "SELECT COUNT(*) FROM repos WHERE catalog_generation = ?", (generation,)
        ).fetchone()[0]
    )
