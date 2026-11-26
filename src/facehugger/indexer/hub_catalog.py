"""Resumable traversal of the public Hugging Face model catalog."""

from datetime import datetime
from time import monotonic, sleep
from typing import Any, Protocol, cast

import httpx

from facehugger.filters import is_candidate_artifact
from facehugger.indexer.rate_limit import RateController, RequestMetrics
from facehugger.models import CatalogRepo
from facehugger.state import IndexState

CATALOG_URL = "https://huggingface.co/api/models"
"""Hugging Face Hub's public model catalog endpoint."""

_PAGE_SIZE = 100
_RETRY_ATTEMPTS = 3


class CatalogClient(Protocol):
    """Public-model catalog transport required by the resumable crawler."""

    def get(self, url: str, *, params: Any = None) -> httpx.Response:
        """Return one response for the requested public catalog page."""
        raise NotImplementedError


def create_catalog_client(
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
        next_url = _next_url(response)
        state.record_catalog_page(observations, generation=generation, next_url=next_url)
        pages += 1
        if next_url is None:
            state.finish_catalog_generation(generation)
            return pages, False
    return pages, False


def _catalog_params() -> dict[str, object]:
    """Return the complete deterministic public model catalog request."""
    return {
        "sort": "createdAt",
        "direction": "-1",
        "limit": _PAGE_SIZE,
        "expand": ["sha", "siblings", "lastModified", "private", "gated", "downloads"],
    }


def _catalog_response(
    client: CatalogClient, url: str, params: dict[str, object] | None
) -> httpx.Response | None:
    """Return one catalog page, retrying only transient Hub failures within this invocation."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {429, 500, 502, 503, 504}:
                raise
        except httpx.TransportError:
            pass
        if attempt + 1 < _RETRY_ATTEMPTS:
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
    raw_siblings = mapping.get("siblings", [])
    if not isinstance(raw_siblings, list):
        raise RuntimeError("Model catalog included an invalid siblings field.")
    sibling_paths = _sibling_paths(cast(list[object], raw_siblings))
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
    if url is None:
        raise RuntimeError("Model catalog returned an invalid continuation.")
    parsed = httpx.URL(url)
    if parsed.copy_with(query=None) != httpx.URL(CATALOG_URL):
        raise RuntimeError("Model catalog returned an invalid continuation.")
    return url


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
