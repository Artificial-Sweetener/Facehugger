"""Proof-report input behavior."""

from pathlib import Path

from facehugger.indexer.reports import lookup_provided_hashes
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
