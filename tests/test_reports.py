"""Proof-report input behavior."""

from pathlib import Path

from facehugger.indexer.reports import (
    group_measurements,
    lookup_provided_hashes,
    summarize_strategy_comparison,
)
from facehugger.models import InspectedFile, InspectedRepo
from facehugger.state import IndexState


def test_optional_hash_file_resolves_normalized_exact_matches(tmp_path: Path) -> None:
    """Owner-provided hashes are reported through the completed reverse index."""
    digest = "ab" * 32
    state = IndexState(tmp_path / "state.sqlite")
    try:
        repo = InspectedRepo(
            "example/model",
            "1" * 40,
            (InspectedFile("model.safetensors", 42, None, bytes.fromhex(digest), None, "lfs"),),
        )
        state.replace_repo(repo, repo.files)
        supplied = tmp_path / "local-hashes.txt"
        supplied.write_text("# comment\n" + digest.upper() + "\n", encoding="utf-8")
        results = lookup_provided_hashes(supplied, state)
    finally:
        state.close()
    assert results is not None
    assert results[0]["digest"] == digest
    assert results[0]["matches"][0]["repo_id"] == "example/model"  # type: ignore[index]


def test_strategy_comparison_selects_the_measured_lowest_request_strategy() -> None:
    """The default adapter selection follows recorded strategy measurements."""
    comparison = summarize_strategy_comparison(
        {
            "model_info": [
                {
                    "duration_seconds": 0.2,
                    "file_count": 1001,
                    "files_with_sha256": 4,
                    "files_with_xet_hash": 2,
                    "http_requests": 1,
                }
            ],
            "repo_tree": [
                {
                    "duration_seconds": 0.1,
                    "file_count": 1001,
                    "files_with_sha256": 4,
                    "files_with_xet_hash": 2,
                    "http_requests": 2,
                }
            ],
        }
    )
    assert comparison["selected_default"] == "model_info"
    assert comparison["strategies"]["model_info"]["repositories_over_1000_files"] == 1  # type: ignore[index]


def test_measurement_grouping_separates_the_production_crawl_from_trial_data() -> None:
    """Crawl metrics can be summarized without contaminating adapter comparison."""
    grouped = group_measurements(
        [
            {
                "strategy": "model_info",
                "duration_seconds": 0.1,
                "file_count": 1,
                "files_with_sha256": 1,
                "files_with_xet_hash": 0,
                "http_requests": 1,
            }
        ]
    )
    assert list(grouped) == ["model_info"]
    assert grouped["model_info"][0]["http_requests"] == 1
