"""GitHub Actions workflow contract tests."""

from pathlib import Path


def test_full_crawl_retries_transient_release_transfer_failures() -> None:
    """Transient GitHub release transfer failures cannot break the resumable crawl chain."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "crawl.yml"
    ).read_text(encoding="utf-8")

    assert "retry_github()" in workflow
    assert "maximum_attempts=5" in workflow
    assert "restore_state()" in workflow
    assert "retry_github restore_state" in workflow
    assert 'retry_github gh release upload full-crawl-state "${assets[@]}" --clobber' in workflow
