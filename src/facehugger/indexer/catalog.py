"""Deterministic public model corpus selection."""

import heapq
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from facehugger.filters import is_candidate_artifact
from facehugger.models import CatalogRepo

PROOF_REPO_CAP = 5000


@dataclass(frozen=True)
class ProofCorpus:
    """A reproducible repository selection and its mutually exclusive composition."""

    repositories: tuple[str, ...]
    composition: dict[str, int]


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
    return select_proof_corpus(
        catalog,
        extensions,
        extra_repos,
        popular_limit=popular_limit,
        random_limit=random_limit,
        targeted_limit=targeted_limit,
    ).repositories


def select_proof_corpus(
    catalog: Iterable[CatalogRepo],
    extensions: tuple[str, ...],
    extra_repos: Iterable[str] = (),
    *,
    popular_limit: int = 2000,
    random_limit: int = 2000,
    targeted_limit: int = 1000,
) -> ProofCorpus:
    """Select the bounded corpus and record the source of every selected repository."""
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
    selected: set[str] = set()
    composition = {
        "popular": _append_unique(selected, (repo_id for _, repo_id in popular), popular_limit),
        "random": _append_unique(
            selected, (repo_id for _, repo_id in sorted(random, reverse=True)), random_limit
        ),
        "targeted": _append_unique(
            selected,
            (repo_id for _, _, repo_id in sorted(targeted, reverse=True)),
            targeted_limit,
        ),
    }
    composition["extra"] = _append_unique(selected, extra_repos, PROOF_REPO_CAP - len(selected))
    return ProofCorpus(tuple(sorted(selected)), composition)


def _append_unique(selected: set[str], candidates: Iterable[str], limit: int) -> int:
    if limit <= 0:
        return 0
    added = 0
    for candidate in candidates:
        if candidate not in selected:
            selected.add(candidate)
            added += 1
            if added == limit:
                return added
    return added


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
