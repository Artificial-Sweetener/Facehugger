"""GitHub Actions workflow contract tests."""

from pathlib import Path


def test_full_crawl_retries_transient_release_upload_failures() -> None:
    """A transient GitHub release outage cannot break the resumable crawl chain."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "crawl.yml"
    ).read_text(encoding="utf-8")

    assert "retry_github()" in workflow
    assert "maximum_attempts=5" in workflow
    assert 'retry_github gh release upload full-crawl-state "${assets[@]}" --clobber' in workflow
