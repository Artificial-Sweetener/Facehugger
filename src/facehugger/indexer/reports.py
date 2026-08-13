"""Proof-report construction and durable JSON/Markdown publication."""

import importlib.metadata
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol, cast

from facehugger.hashes import normalize_sha256, sha256_bytes
from facehugger.indexer.rate_limit import RequestMetrics
from facehugger.state import IndexState


class ProofMetricsView(Protocol):
    """Read-only measurements required to build a Phase 0 report."""

    cataloged_repositories: int
    inspected_repositories: int
    eligible_catalog_repositories: int
    skipped_repositories: dict[str, int]
    candidate_files: int
    exact_hash_files: int
    candidate_bytes: int
    exact_hash_bytes: int
    timeout_errors: int
    extension_coverage: dict[str, dict[str, int]]
    local_lookup: dict[str, object] | None
    verification: list[dict[str, object]]
    strategy_measurements: dict[str, list[dict[str, float | int | str]]]
    request_metrics: RequestMetrics


def build_proof_report(
    *,
    metrics: ProofMetricsView,
    state: IndexState,
    root: Path,
    compilation: Mapping[str, Any],
    corpus: tuple[str, ...],
    corpus_composition: Mapping[str, int],
    started: datetime,
    throttle_requests_per_minute: int,
    provided_hash_lookups: list[dict[str, object]] | None,
) -> dict[str, object]:
    """Build the complete review record from bounded proof measurements."""
    counts = state.counts()
    durations = [
        float(item["duration_seconds"])
        for values in metrics.strategy_measurements.values()
        for item in values
    ]
    shard_sizes = cast(list[int], compilation["shard_sizes"])
    estimated_calls = _project_full_catalog_calls(metrics)
    return {
        "start_timestamp": _timestamp(started),
        "end_timestamp": None,
        "package_versions": _package_versions(),
        "proof_corpus": {
            "selected": len(corpus),
            "cap": 5000,
            "composition": dict(corpus_composition),
        },
        "repositories_cataloged": metrics.cataloged_repositories,
        "eligible_catalog_repositories": metrics.eligible_catalog_repositories,
        "repositories_inspected": metrics.inspected_repositories,
        "repositories_skipped": metrics.skipped_repositories,
        "candidate_artifact_paths": metrics.candidate_files,
        "artifact_paths_with_exact_sha256": metrics.exact_hash_files,
        "coverage": {
            "candidate_files_with_sha256": _ratio(
                metrics.exact_hash_files, metrics.candidate_files
            ),
            "candidate_bytes_with_sha256": _ratio(
                metrics.exact_hash_bytes, metrics.candidate_bytes
            ),
            "by_extension": _extension_coverage(metrics.extension_coverage),
        },
        "unique_hashes": counts["unique_hashes"],
        "total_occurrences": counts["occurrences"],
        "hashes_with_multiple_occurrences": _duplicate_hash_count(state),
        "api_requests": {
            "total": metrics.request_metrics.requests,
            "status_counts": metrics.request_metrics.statuses,
            "by_endpoint": metrics.request_metrics.categories,
            "by_strategy": metrics.strategy_measurements,
        },
        "resolver_requests": len(metrics.verification),
        "errors": {
            "429": metrics.request_metrics.statuses.get("429", 0),
            "403": metrics.request_metrics.statuses.get("403", 0),
            "404": metrics.request_metrics.statuses.get("404", 0),
            "5xx": sum(
                count
                for status, count in metrics.request_metrics.statuses.items()
                if status.startswith("5")
            ),
            "timeout": metrics.timeout_errors,
            "retry": None,
        },
        "rate_limits": {
            "rate_limit": metrics.request_metrics.rate_limit,
            "rate_limit_policy": metrics.request_metrics.rate_limit_policy,
            "remaining": metrics.request_metrics.rate_limit_remaining,
        },
        "requests_per_useful_hash": _ratio(
            metrics.request_metrics.requests, counts["unique_hashes"]
        ),
        "repo_inspection_latency_seconds": summarize(durations),
        "sqlite_size_bytes": (root / ".facehugger" / "proof.sqlite").stat().st_size,
        "pages_size_bytes": _site_size(root / "site"),
        "shards": {
            "count": compilation["shard_count"],
            "empty_count": compilation["empty_shard_count"],
            "size_bytes": summarize([float(size) for size in shard_sizes]),
        },
        "local_lookup_latency_seconds": metrics.local_lookup,
        "pages_lookup_latency_seconds": None,
        "cors": None,
        "projections": _projections(
            metrics, estimated_calls, throttle_requests_per_minute, root / "site"
        ),
        "verification_downloads": metrics.verification,
        "provided_hash_lookups": provided_hash_lookups,
        "known_limitations": [
            "The proof index is incomplete and a no-match is not definitive.",
            "Pages measurements are added by the deployment workflow after publication.",
            "The Hub client does not expose a retry counter; retry is reported as null.",
        ],
    }


def write_proof_reports(directory: Path, report: Mapping[str, object]) -> None:
    """Write deterministic JSON and readable Markdown forms of one report."""
    encoded = json.dumps(report, indent=2, sort_keys=True)
    (directory / "proof.json").write_text(encoded + "\n", encoding="utf-8")
    (directory / "proof.md").write_text(
        "# Facehugger Phase 0 proof\n\n```json\n" + encoded + "\n```\n", encoding="utf-8"
    )


def lookup_provided_hashes(path: Path, state: IndexState) -> list[dict[str, object]] | None:
    """Resolve optional owner-supplied hashes against the completed proof state."""
    if not path.exists():
        return None
    results: list[dict[str, object]] = []
    for raw_value in path.read_text(encoding="utf-8").splitlines():
        value = raw_value.strip()
        if value and not value.startswith("#"):
            digest = normalize_sha256(value)
            results.append(
                {
                    "digest": digest,
                    "matches": [item.__dict__ for item in state.lookup(sha256_bytes(digest))],
                }
            )
    return results


def _extension_coverage(
    values: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int | float | None]]:
    return {
        extension: {
            **counts,
            "file_coverage": _ratio(counts["exact_hash_files"], counts["candidate_files"]),
            "byte_coverage": _ratio(counts["exact_hash_bytes"], counts["candidate_bytes"]),
        }
        for extension, counts in values.items()
    }


def _project_full_catalog_calls(metrics: ProofMetricsView) -> float | None:
    if not metrics.inspected_repositories:
        return None
    catalog_requests = metrics.request_metrics.categories.get("model_catalog", 0)
    inspection_requests = metrics.request_metrics.categories.get("model_info", 0)
    per_repo = inspection_requests / metrics.inspected_repositories
    return catalog_requests + metrics.eligible_catalog_repositories * per_repo


def _projections(
    metrics: ProofMetricsView,
    estimated_calls: float | None,
    throttle_requests_per_minute: int,
    site: Path,
) -> dict[str, object]:
    estimated_pages_size = None
    if metrics.inspected_repositories:
        estimated_pages_size = _site_size(site) * (
            metrics.eligible_catalog_repositories / metrics.inspected_repositories
        )
    return {
        "full_catalog_api_calls": estimated_calls,
        "full_crawl_days_at_default_throttle": None
        if estimated_calls is None
        else estimated_calls / throttle_requests_per_minute / 60 / 24,
        "complete_pages_size_bytes": estimated_pages_size,
        "method": "Linear estimate from the complete catalog inventory and bounded proof sample.",
    }


def _site_size(site: Path) -> int:
    return sum(path.stat().st_size for path in site.rglob("*") if path.is_file())


def _duplicate_hash_count(state: IndexState) -> int:
    return sum(len(occurrences) > 1 for _, occurrences in state.iter_artifacts())


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return None if not denominator else numerator / denominator


def summarize(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    if not ordered:
        return {"mean": None, "median": None, "p95": None, "max": None}
    index = min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))
    return {
        "mean": mean(ordered),
        "median": median(ordered),
        "p95": ordered[index],
        "max": ordered[-1],
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _package_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in ("facehugger", "huggingface-hub", "httpx")
    }
