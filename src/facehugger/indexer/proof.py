"""Bounded Phase 0 proof execution and evidence reporting."""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from statistics import mean, median
from typing import Any, cast

import httpx
from huggingface_hub import HfApi, set_client_factory
from huggingface_hub import (
    hf_hub_download as _hf_hub_download,  # pyright: ignore[reportUnknownVariableType]
)
from huggingface_hub.hf_api import ModelInfo

from facehugger.filters import is_candidate_artifact
from facehugger.hashes import normalize_sha256, sha256_bytes
from facehugger.indexer.catalog import select_proof_repos
from facehugger.indexer.metadata_sources import (
    InspectionMeasurement,
    ModelInfoMetadataSource,
    RepoTreeMetadataSource,
)
from facehugger.indexer.rate_limit import RateController, RequestMetrics
from facehugger.models import CatalogRepo, InspectedFile
from facehugger.shard_format import compile_site
from facehugger.state import IndexState

PROOF_REPO_CAP = 5000
STRATEGY_COMPARISON_REPO_CAP = 200
VERIFICATION_FILE_CAP = 3
VERIFICATION_SIZE_CAP = 10 * 1024 * 1024
VERIFICATION_TOTAL_CAP = 50 * 1024 * 1024


@dataclass
class ProofMetrics:
    """Serializable measurements collected by a bounded Phase 0 proof run."""

    started_at: str
    ended_at: str | None = None
    cataloged_repositories: int = 0
    inspected_repositories: int = 0
    skipped_repositories: dict[str, int] = field(default_factory=dict[str, int])
    candidate_files: int = 0
    exact_hash_files: int = 0
    candidate_bytes: int = 0
    exact_hash_bytes: int = 0
    extension_coverage: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    verification: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    strategy_measurements: dict[str, list[dict[str, float | int | str]]] = field(
        default_factory=dict[str, list[dict[str, float | int | str]]]
    )
    request_metrics: RequestMetrics = field(default_factory=RequestMetrics)


def run_proof(
    *,
    root: Path,
    token: str,
    version: str,
    catalog_limit: int | None = None,
) -> dict[str, object]:
    """Run the capped metadata-only proof and write site and review reports."""
    started = datetime.now(UTC)
    metrics = ProofMetrics(started_at=_timestamp(started))
    rate_controller = RateController()
    _configure_hub_http(metrics.request_metrics, rate_controller)
    api = HfApi(token=token, library_name="facehugger", library_version="0.0.0")
    extensions = _extensions(root)
    extras = _extra_repos(root / "fixtures" / "proof-repos-extra.txt")
    corpus = select_proof_repos(
        _catalog(api, extensions, metrics, catalog_limit, rate_controller), extensions, extras
    )
    reports_dir = root / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / "proof-repos.txt").write_text("\n".join(corpus) + "\n", encoding="utf-8")
    state_path = root / ".facehugger" / "proof.sqlite"
    state_path.parent.mkdir(exist_ok=True)
    state = IndexState(state_path)
    try:
        primary = ModelInfoMetadataSource(api)
        fallback = RepoTreeMetadataSource(api)
        comparison_ids = corpus[:STRATEGY_COMPARISON_REPO_CAP]
        for repo_id in comparison_ids:
            _compare_strategies(primary, fallback, repo_id, metrics)
        for repo_id in corpus:
            try:
                inspected, measurement = primary.inspect_repo(repo_id, None)
            except Exception:
                metrics.skipped_repositories["inspection_error"] = (
                    metrics.skipped_repositories.get("inspection_error", 0) + 1
                )
                continue
            candidate_files = tuple(
                item for item in inspected.files if is_candidate_artifact(item.path, extensions)
            )
            state.replace_repo(inspected, candidate_files)
            metrics.inspected_repositories += 1
            metrics.candidate_files += len(candidate_files)
            metrics.exact_hash_files += sum(
                item.content_sha256 is not None for item in candidate_files
            )
            for candidate in candidate_files:
                _record_candidate(metrics, candidate, extensions)
            _append_measurement(metrics, measurement)
        _verify_small_files(api, state, token, metrics, root, extensions)
        generated_at = datetime.now(UTC)
        compilation = compile_site(
            state,
            root / "site",
            version=version,
            generated_at=generated_at,
            catalog_cutoff=generated_at,
            complete=False,
        )
        state.validate()
        report = _report(metrics, state, root, compilation, corpus, started)
    finally:
        state.close()
    metrics.ended_at = _timestamp(datetime.now(UTC))
    report["end_timestamp"] = metrics.ended_at
    _write_reports(reports_dir, report)
    return report


def _catalog(
    api: HfApi,
    extensions: tuple[str, ...],
    metrics: ProofMetrics,
    catalog_limit: int | None,
    rate_controller: RateController,
) -> Iterable[CatalogRepo]:
    models = api.list_models(
        sort="downloads",
        expand=["sha", "siblings", "lastModified", "private", "gated", "downloads"],
    )
    for info in models:
        if catalog_limit is not None and metrics.cataloged_repositories >= catalog_limit:
            return
        repo = _catalog_record(info)
        metrics.cataloged_repositories += 1
        if repo.private:
            metrics.skipped_repositories["private"] = (
                metrics.skipped_repositories.get("private", 0) + 1
            )
            continue
        if not any(is_candidate_artifact(path, extensions) for path in repo.sibling_paths):
            metrics.skipped_repositories["no_candidate_artifact"] = (
                metrics.skipped_repositories.get("no_candidate_artifact", 0) + 1
            )
            continue
        yield repo


def _catalog_record(info: ModelInfo) -> CatalogRepo:
    """Normalize the minimum safe catalog fields from one Hub listing row."""
    gated = isinstance(info.gated, str)
    return CatalogRepo(
        repo_id=info.id,
        revision=info.sha,
        last_modified=info.last_modified,
        downloads=info.downloads,
        private=bool(info.private),
        gated=gated,
        sibling_paths=tuple(item.rfilename for item in info.siblings or []),
    )


def _compare_strategies(
    first: ModelInfoMetadataSource,
    second: RepoTreeMetadataSource,
    repo_id: str,
    metrics: ProofMetrics,
) -> None:
    for source in (first, second):
        try:
            _, measurement = source.inspect_repo(repo_id, None)
        except Exception:
            metrics.skipped_repositories["strategy_error"] = (
                metrics.skipped_repositories.get("strategy_error", 0) + 1
            )
            continue
        _append_measurement(metrics, measurement)


def _append_measurement(metrics: ProofMetrics, measurement: InspectionMeasurement) -> None:
    metrics.strategy_measurements.setdefault(measurement.strategy, []).append(
        {
            "strategy": measurement.strategy,
            "duration_seconds": measurement.duration_seconds,
            "file_count": measurement.file_count,
            "files_with_sha256": measurement.files_with_sha256,
            "files_with_xet_hash": measurement.files_with_xet_hash,
        }
    )


def _record_candidate(
    metrics: ProofMetrics, candidate: InspectedFile, extensions: tuple[str, ...]
) -> None:
    extension = next(suffix for suffix in extensions if candidate.path.casefold().endswith(suffix))
    coverage = metrics.extension_coverage.setdefault(
        extension,
        {
            "candidate_files": 0,
            "exact_hash_files": 0,
            "candidate_bytes": 0,
            "exact_hash_bytes": 0,
        },
    )
    coverage["candidate_files"] += 1
    metrics.candidate_bytes += candidate.size or 0
    coverage["candidate_bytes"] += candidate.size or 0
    if candidate.content_sha256 is not None:
        coverage["exact_hash_files"] += 1
        metrics.exact_hash_bytes += candidate.size or 0
        coverage["exact_hash_bytes"] += candidate.size or 0


def _verify_small_files(
    api: HfApi,
    state: IndexState,
    token: str,
    metrics: ProofMetrics,
    root: Path,
    extensions: tuple[str, ...],
) -> None:
    verified_bytes = 0
    for digest, occurrences in state.iter_artifacts():
        if len(metrics.verification) == VERIFICATION_FILE_CAP:
            return
        for occurrence in occurrences:
            if not is_candidate_artifact(occurrence.path, extensions):
                continue
            if occurrence.size is None or occurrence.size > VERIFICATION_SIZE_CAP:
                continue
            if verified_bytes + occurrence.size > VERIFICATION_TOTAL_CAP:
                continue
            destination = root / "fixtures" / "downloaded"
            downloaded = Path(
                _hf_hub_download(
                    occurrence.repo_id,
                    occurrence.path,
                    revision=occurrence.revision,
                    token=token,
                    local_dir=destination,
                    library_name="facehugger",
                    library_version="0.0.0",
                )
            )
            calculated = _file_sha256(downloaded)
            result = calculated == digest.hex()
            metrics.verification.append(
                {
                    "repo_id": occurrence.repo_id,
                    "revision": occurrence.revision,
                    "path": occurrence.path,
                    "reported_sha256": digest.hex(),
                    "calculated_sha256": calculated,
                    "size": occurrence.size,
                    "result": result,
                }
            )
            verified_bytes += occurrence.size
            break


def _report(
    metrics: ProofMetrics,
    state: IndexState,
    root: Path,
    compilation: dict[str, Any],
    corpus: tuple[str, ...],
    started: datetime,
) -> dict[str, object]:
    counts = state.counts()
    durations = [
        float(item["duration_seconds"])
        for values in metrics.strategy_measurements.values()
        for item in values
    ]
    shard_sizes = cast(list[int], compilation["shard_sizes"])
    report: dict[str, object] = {
        "start_timestamp": _timestamp(started),
        "end_timestamp": None,
        "package_versions": _package_versions(),
        "proof_corpus": {"selected": len(corpus), "cap": PROOF_REPO_CAP},
        "repositories_cataloged": metrics.cataloged_repositories,
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
            "by_extension": metrics.extension_coverage,
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
            "timeout": 0,
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
        "repo_inspection_latency_seconds": _summarize(durations),
        "sqlite_size_bytes": (root / ".facehugger" / "proof.sqlite").stat().st_size,
        "pages_size_bytes": sum(
            path.stat().st_size for path in (root / "site").rglob("*") if path.is_file()
        ),
        "shards": {
            "count": compilation["shard_count"],
            "empty_count": compilation["empty_shard_count"],
            "size_bytes": _summarize([float(size) for size in shard_sizes]),
        },
        "local_lookup_latency_seconds": None,
        "pages_lookup_latency_seconds": None,
        "cors": None,
        "projections": {
            "full_catalog_api_calls": None,
            "full_crawl_days": None,
            "pages_size_bytes": None,
        },
        "verification_downloads": metrics.verification,
        "provided_hash_lookups": _provided_hash_lookups(
            root / "fixtures" / "local-hashes.txt", state
        ),
        "known_limitations": [
            "The proof index is incomplete and a no-match is not definitive.",
            "Pages deployment measurements are populated only after the proof workflow deploys.",
        ],
    }
    return report


def _write_reports(directory: Path, report: dict[str, object]) -> None:
    (directory / "proof.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = "\n".join(
        [
            "# Facehugger Phase 0 proof",
            "",
            "```json",
            json.dumps(report, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    (directory / "proof.md").write_text(markdown, encoding="utf-8")


def _configure_hub_http(metrics: RequestMetrics, rate_controller: RateController) -> None:
    def request_hook(_: httpx.Request) -> None:
        rate_controller.wait()

    def response_hook(response: httpx.Response) -> None:
        metrics.observe(response)
        rate_controller.observe(response)

    set_client_factory(
        lambda: httpx.Client(
            event_hooks={"request": [request_hook], "response": [response_hook]},
            follow_redirects=True,
            timeout=30.0,
        )
    )


def _extensions(root: Path) -> tuple[str, ...]:
    from facehugger.filters import load_artifact_extensions

    return load_artifact_extensions(root / "config" / "artifacts.toml")


def _extra_repos(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def _provided_hash_lookups(path: Path, state: IndexState) -> list[dict[str, object]] | None:
    if not path.exists():
        return None
    results: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        digest = normalize_sha256(value)
        results.append(
            {
                "digest": digest,
                "matches": [
                    occurrence.__dict__ for occurrence in state.lookup(sha256_bytes(digest))
                ],
            }
        )
    return results


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duplicate_hash_count(state: IndexState) -> int:
    return sum(len(occurrences) > 1 for _, occurrences in state.iter_artifacts())


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return None if not denominator else numerator / denominator


def _summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))
    return {
        "mean": mean(values),
        "median": median(values),
        "p95": ordered[index],
        "max": ordered[-1],
    }


def _package_versions() -> dict[str, str]:
    import importlib.metadata

    return {
        name: importlib.metadata.version(name)
        for name in ("facehugger", "huggingface-hub", "httpx")
    }
