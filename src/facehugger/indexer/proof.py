"""Bounded Phase 0 proof execution and evidence collection."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
from huggingface_hub import HfApi, set_client_factory
from huggingface_hub import (
    hf_hub_download as _hf_hub_download,  # pyright: ignore[reportUnknownVariableType]
)
from huggingface_hub.hf_api import ModelInfo

from facehugger.errors import ProofStopError
from facehugger.filters import is_candidate_artifact, load_artifact_extensions
from facehugger.indexer.benchmarks import measure_local_lookup
from facehugger.indexer.catalog import ProofCorpus, select_proof_corpus
from facehugger.indexer.metadata_sources import (
    InspectionMeasurement,
    ModelInfoMetadataSource,
    RepoTreeMetadataSource,
)
from facehugger.indexer.rate_limit import RateController, RequestMetrics
from facehugger.indexer.reports import (
    build_proof_report,
    lookup_provided_hashes,
    write_proof_reports,
)
from facehugger.models import CatalogRepo, InspectedFile
from facehugger.shard_format import compile_site
from facehugger.state import IndexState

PROOF_REPO_CAP = 5000
STRATEGY_COMPARISON_REPO_CAP = 200
VERIFICATION_FILE_CAP = 3
VERIFICATION_SIZE_CAP = 10 * 1024 * 1024
VERIFICATION_TOTAL_CAP = 50 * 1024 * 1024
THROTTLE_REQUESTS_PER_MINUTE = 100


@dataclass
class ProofMetrics:
    """Serializable aggregate measurements collected by one proof run."""

    cataloged_repositories: int = 0
    eligible_catalog_repositories: int = 0
    inspected_repositories: int = 0
    skipped_repositories: dict[str, int] = field(default_factory=dict[str, int])
    candidate_files: int = 0
    exact_hash_files: int = 0
    candidate_bytes: int = 0
    exact_hash_bytes: int = 0
    timeout_errors: int = 0
    extension_coverage: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    local_lookup: dict[str, object] | None = None
    verification: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    strategy_measurements: dict[str, list[dict[str, float | int | str]]] = field(
        default_factory=dict[str, list[dict[str, float | int | str]]]
    )
    request_metrics: RequestMetrics = field(default_factory=RequestMetrics)


def run_proof(
    *, root: Path, token: str, version: str, catalog_limit: int | None = None
) -> dict[str, object]:
    """Run the capped metadata proof and write a review-ready report."""
    started = datetime.now(UTC)
    metrics = ProofMetrics()
    controller = RateController(THROTTLE_REQUESTS_PER_MINUTE)
    _configure_hub_http(metrics.request_metrics, controller)
    api = HfApi(token=token, library_name="facehugger", library_version="0.0.0")
    extensions = load_artifact_extensions(root / "config" / "artifacts.toml")
    corpus = select_proof_corpus(
        _catalog(api, extensions, metrics, catalog_limit),
        extensions,
        _extra_repos(root / "fixtures" / "proof-repos-extra.txt"),
    )
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "proof-repos.txt").write_text(
        "\n".join(corpus.repositories) + "\n", encoding="utf-8"
    )
    state_path = root / ".facehugger" / "proof.sqlite"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = IndexState(state_path)
    try:
        _inspect_corpus(api, state, corpus, extensions, metrics)
        state.validate()
        compilation = _compile_site(state, root, version)
        _measure_local_lookup(state, root, metrics)
        stop_error: ProofStopError | None = None
        try:
            _verify_small_files(state, metrics, root=root, token=token, extensions=extensions)
        except ProofStopError as error:
            stop_error = error
        report = build_proof_report(
            metrics=metrics,
            state=state,
            root=root,
            compilation=compilation,
            corpus=corpus.repositories,
            corpus_composition=corpus.composition,
            started=started,
            throttle_requests_per_minute=THROTTLE_REQUESTS_PER_MINUTE,
            provided_hash_lookups=lookup_provided_hashes(
                root / "fixtures" / "local-hashes.txt", state
            ),
        )
    finally:
        state.close()
    report["end_timestamp"] = _timestamp(datetime.now(UTC))
    write_proof_reports(reports, report)
    if stop_error is not None:
        raise stop_error
    return report


def _catalog(
    api: HfApi,
    extensions: tuple[str, ...],
    metrics: ProofMetrics,
    catalog_limit: int | None,
) -> Iterable[CatalogRepo]:
    for info in api.list_models(
        sort="downloads",
        expand=["sha", "siblings", "lastModified", "private", "gated", "downloads"],
    ):
        if catalog_limit is not None and metrics.cataloged_repositories >= catalog_limit:
            return
        repo = _catalog_record(info)
        metrics.cataloged_repositories += 1
        if repo.private:
            _skip(metrics, "private")
            continue
        if not any(is_candidate_artifact(path, extensions) for path in repo.sibling_paths):
            _skip(metrics, "no_candidate_artifact")
            continue
        metrics.eligible_catalog_repositories += 1
        yield repo


def _catalog_record(info: ModelInfo) -> CatalogRepo:
    """Normalize the safe catalog fields from one Hub listing row."""
    return CatalogRepo(
        repo_id=info.id,
        revision=info.sha,
        last_modified=info.last_modified,
        downloads=info.downloads,
        private=bool(info.private),
        gated=isinstance(info.gated, str),
        sibling_paths=tuple(item.rfilename for item in info.siblings or []),
    )


def _inspect_corpus(
    api: HfApi,
    state: IndexState,
    corpus: ProofCorpus,
    extensions: tuple[str, ...],
    metrics: ProofMetrics,
) -> None:
    """Compare adapters and persist primary metadata for the selected corpus."""
    primary = ModelInfoMetadataSource(api)
    fallback = RepoTreeMetadataSource(api)
    for repo_id in corpus.repositories[:STRATEGY_COMPARISON_REPO_CAP]:
        _compare_strategies(primary, fallback, repo_id, metrics)
    for repo_id in corpus.repositories:
        requests_before = metrics.request_metrics.requests
        try:
            inspected, measurement = primary.inspect_repo(repo_id, None)
        except Exception as error:
            _record_error(metrics, error)
            _skip(metrics, "inspection_error")
            continue
        candidates = tuple(
            file for file in inspected.files if is_candidate_artifact(file.path, extensions)
        )
        state.replace_repo(inspected, candidates)
        metrics.inspected_repositories += 1
        metrics.candidate_files += len(candidates)
        metrics.exact_hash_files += sum(file.content_sha256 is not None for file in candidates)
        for candidate in candidates:
            _record_candidate(metrics, candidate, extensions)
        _append_measurement(
            metrics, measurement, metrics.request_metrics.requests - requests_before
        )


def _compare_strategies(
    primary: ModelInfoMetadataSource,
    fallback: RepoTreeMetadataSource,
    repo_id: str,
    metrics: ProofMetrics,
) -> None:
    for source in (primary, fallback):
        requests_before = metrics.request_metrics.requests
        try:
            _, measurement = source.inspect_repo(repo_id, None)
        except Exception as error:
            _record_error(metrics, error)
            _skip(metrics, "strategy_error")
            continue
        _append_measurement(
            metrics, measurement, metrics.request_metrics.requests - requests_before
        )


def _append_measurement(
    metrics: ProofMetrics, measurement: InspectionMeasurement, http_requests: int
) -> None:
    metrics.strategy_measurements.setdefault(measurement.strategy, []).append(
        {
            "strategy": measurement.strategy,
            "duration_seconds": measurement.duration_seconds,
            "file_count": measurement.file_count,
            "files_with_sha256": measurement.files_with_sha256,
            "files_with_xet_hash": measurement.files_with_xet_hash,
            "http_requests": http_requests,
        }
    )


def _record_candidate(
    metrics: ProofMetrics, candidate: InspectedFile, extensions: tuple[str, ...]
) -> None:
    extension = next(suffix for suffix in extensions if candidate.path.casefold().endswith(suffix))
    coverage = metrics.extension_coverage.setdefault(
        extension,
        {"candidate_files": 0, "exact_hash_files": 0, "candidate_bytes": 0, "exact_hash_bytes": 0},
    )
    coverage["candidate_files"] += 1
    coverage["candidate_bytes"] += candidate.size or 0
    metrics.candidate_bytes += candidate.size or 0
    if candidate.content_sha256 is not None:
        coverage["exact_hash_files"] += 1
        coverage["exact_hash_bytes"] += candidate.size or 0
        metrics.exact_hash_bytes += candidate.size or 0


def _verify_small_files(
    state: IndexState,
    metrics: ProofMetrics,
    *,
    root: Path,
    token: str,
    extensions: tuple[str, ...],
) -> None:
    verified_bytes = 0
    for digest, occurrences in state.iter_artifacts():
        if len(metrics.verification) >= VERIFICATION_FILE_CAP:
            break
        for occurrence in occurrences:
            if not _eligible_verification_file(
                occurrence.size, verified_bytes
            ) or not is_candidate_artifact(occurrence.path, extensions):
                continue
            try:
                downloaded = Path(
                    _hf_hub_download(
                        occurrence.repo_id,
                        occurrence.path,
                        revision=occurrence.revision,
                        token=token,
                        local_dir=root / "fixtures" / "downloaded",
                        library_name="facehugger",
                        library_version="0.0.0",
                    )
                )
                calculated = _file_sha256(downloaded)
            except Exception as error:
                _record_error(metrics, error)
                _skip(metrics, "verification_error")
                continue
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
            if not result:
                raise ProofStopError(
                    "Verification metadata did not match downloaded artifact bytes."
                )
            verified_bytes += occurrence.size or 0
            break
    if len(metrics.verification) < VERIFICATION_FILE_CAP:
        raise ProofStopError("Proof could not verify three eligible artifact files.")


def _eligible_verification_file(size: int | None, verified_bytes: int) -> bool:
    return (
        size is not None
        and size <= VERIFICATION_SIZE_CAP
        and verified_bytes + size <= VERIFICATION_TOTAL_CAP
    )


def _compile_site(state: IndexState, root: Path, version: str) -> dict[str, object]:
    now = datetime.now(UTC)
    return compile_site(
        state, root / "site", version=version, generated_at=now, catalog_cutoff=now, complete=False
    )


def _measure_local_lookup(state: IndexState, root: Path, metrics: ProofMetrics) -> None:
    first = next(state.iter_artifacts(), None)
    if first is None:
        raise ProofStopError("Proof index did not contain an exact SHA-256 artifact.")
    measurement = measure_local_lookup(root / "site", first[0].hex())
    metrics.local_lookup = measurement


def _extra_repos(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skip(metrics: ProofMetrics, reason: str) -> None:
    metrics.skipped_repositories[reason] = metrics.skipped_repositories.get(reason, 0) + 1


def _record_error(metrics: ProofMetrics, error: Exception) -> None:
    cause = error.__cause__
    if isinstance(error, httpx.TimeoutException) or isinstance(cause, httpx.TimeoutException):
        metrics.timeout_errors += 1


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _configure_hub_http(metrics: RequestMetrics, controller: RateController) -> None:
    def request_hook(_: httpx.Request) -> None:
        controller.wait()

    def response_hook(response: httpx.Response) -> None:
        metrics.observe(response)
        controller.observe(response)

    set_client_factory(
        lambda: httpx.Client(
            event_hooks={"request": [request_hook], "response": [response_hook]},
            follow_redirects=True,
            timeout=30.0,
        )
    )
