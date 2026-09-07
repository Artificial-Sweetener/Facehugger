"""GitHub Actions workflow contract tests."""

from pathlib import Path


def test_full_crawl_restores_verified_immutable_checkpoints() -> None:
    """A corrupt mutable state release cannot discard more than one retained crawl batch."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "crawl.yml"
    ).read_text(encoding="utf-8")

    assert "retry_github()" in workflow
    assert "maximum_attempts=5" in workflow
    assert "restore_state()" in workflow
    assert 'retry_github restore_state "$checkpoint_tag"' in workflow
    assert "checkpoint_seen=false" in workflow
    assert "full-crawl-checkpoint-" in workflow
    assert "checkpoint_is_verified()" in workflow
    assert "PRAGMA integrity_check;" in workflow
    assert 'gh release create "$CHECKPOINT_TAG" --prerelease' in workflow
    assert "tail --lines=+9" in workflow
    assert "id: package_state" in workflow
    assert "zstd --quiet --test .facehugger/full.sqlite.zst" in workflow
    assert "steps.checkpoint.outputs.published == 'true'" in workflow
    assert 'retry_github gh release upload full-crawl-state "${assets[@]}" --clobber' in workflow
