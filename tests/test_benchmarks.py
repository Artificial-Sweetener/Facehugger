"""Client-contract measurements over a generated static index."""

from datetime import UTC, datetime
from pathlib import Path

from facehugger.indexer.benchmarks import measure_local_lookup
from facehugger.models import InspectedFile, InspectedRepo
from facehugger.shard_format import compile_site
from facehugger.state import IndexState


def test_local_measurement_exercises_the_static_client_contract(tmp_path: Path) -> None:
    """Cold and warm measurements use a real generated manifest and shard."""
    digest = bytes.fromhex("abc" + "0" * 61)
    state = IndexState(tmp_path / "state.sqlite")
    try:
        repo = InspectedRepo(
            "example/model",
            "1" * 40,
            (InspectedFile("model.safetensors", 42, None, digest, None, "lfs"),),
        )
        state.replace_repo(repo, repo.files)
        compile_site(
            state,
            tmp_path / "site",
            version="proof",
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            catalog_cutoff=None,
            complete=False,
        )
        measurement = measure_local_lookup(tmp_path / "site", digest.hex())
    finally:
        state.close()
    assert measurement["cold"]["max"] is not None  # type: ignore[index]
    assert measurement["warm"]["p95"] is not None  # type: ignore[index]
