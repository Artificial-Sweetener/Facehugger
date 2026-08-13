"""Deterministic public model corpus selection."""

import heapq
from collections.abc import Iterable
from hashlib import sha256

from facehugger.filters import is_candidate_artifact
from facehugger.models import CatalogRepo

PROOF_REPO_CAP = 5000


def select_proof_repos(
    catalog: Iterable[CatalogRepo],
    extensions: tuple[str, ...],
    extra_repos: Iterable[str] = (),
    *,
    popular_limit: int = 2000,
    random_limit: int = 2000,
    targeted_limit: int = 1000,
) -> tuple[str, ...]:
    """Build a reproducible, capped Phase 0 corpus from public model metadata."""
    popular: list[tuple[int, str]] = []
    random: list[tuple[int, str]] = []
    targeted: list[tuple[int, int, str]] = []
    for repo in catalog:
        if repo.private or not any(
            is_candidate_artifact(path, extensions) for path in repo.sibling_paths
        ):
            continue
        _retain_pair(popular, (repo.downloads or 0, repo.repo_id), popular_limit)
        rank = _stable_rank(repo.repo_id)
        _retain_pair(random, (-rank, repo.repo_id), random_limit)
        _retain_triple(
            targeted,
            (_format_diversity_score(repo, extensions), -rank, repo.repo_id),
            targeted_limit,
        )
    selected = {repo_id for _, repo_id in popular}
    _append_unique(selected, (repo_id for _, repo_id in sorted(random, reverse=True)), random_limit)
    _append_unique(
        selected,
        (repo_id for _, _, repo_id in sorted(targeted, reverse=True)),
        targeted_limit,
    )
    _append_unique(selected, extra_repos, PROOF_REPO_CAP - len(selected))
    return tuple(sorted(selected))[:5000]


def _append_unique(selected: set[str], candidates: Iterable[str], limit: int) -> None:
    if limit <= 0:
        return
    added = 0
    for candidate in candidates:
        if candidate not in selected:
            selected.add(candidate)
            added += 1
            if added == limit:
                return


def _stable_rank(repo_id: str) -> int:
    return int.from_bytes(sha256(repo_id.encode("utf-8")).digest(), byteorder="big")


def _retain_pair(heap: list[tuple[int, str]], value: tuple[int, str], limit: int) -> None:
    if limit <= 0:
        return
    if len(heap) < limit:
        heapq.heappush(heap, value)
    elif value > heap[0]:
        heapq.heapreplace(heap, value)


def _retain_triple(
    heap: list[tuple[int, int, str]], value: tuple[int, int, str], limit: int
) -> None:
    if limit <= 0:
        return
    if len(heap) < limit:
        heapq.heappush(heap, value)
    elif value > heap[0]:
        heapq.heapreplace(heap, value)


def _format_diversity_score(repo: CatalogRepo, extensions: tuple[str, ...]) -> int:
    return sum(is_candidate_artifact(path, extensions) for path in repo.sibling_paths)
