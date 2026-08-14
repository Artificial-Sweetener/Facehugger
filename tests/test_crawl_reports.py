"""Full-crawl progress report and badge tests."""

import json
from pathlib import Path

from facehugger.indexer.crawl import CrawlProgress
from facehugger.indexer.crawl_reports import write_full_crawl_report, write_full_crawl_status_badge
from facehugger.indexer.rate_limit import RequestMetrics


def test_full_crawl_report_exposes_matching_inspection_progress_badges(tmp_path: Path) -> None:
    """The public badges use the same eligible-repository denominator as the progress report."""
    progress = CrawlProgress(
        generation=1,
        catalog_pages=0,
        catalog_complete=True,
        catalog_stalled=False,
        cataloged_repositories=100,
        inspections=10,
        eligible_repositories=80,
        indexed_repositories=25,
        pending_repositories=55,
        published=False,
    )
    metrics = RequestMetrics(requests=10, statuses={"200": 10})

    write_full_crawl_report(tmp_path, progress, metrics)

    report = json.loads((tmp_path / "reports" / "full-crawl.json").read_text(encoding="utf-8"))
    inspected = json.loads(
        (tmp_path / "reports" / "full-crawl-inspected-badge.json").read_text(encoding="utf-8")
    )
    remaining = json.loads(
        (tmp_path / "reports" / "full-crawl-remaining-badge.json").read_text(encoding="utf-8")
    )
    assert report["eligible_repositories"] == 80
    assert report["indexed_repositories"] == 25
    assert inspected == {
        "color": "007ec6",
        "label": "eligible inspected",
        "message": "25 / 80",
        "schemaVersion": 1,
    }
    assert remaining == {
        "color": "orange",
        "label": "remaining",
        "message": "55",
        "schemaVersion": 1,
    }


def test_full_crawl_status_badge_represents_an_active_crawl(tmp_path: Path) -> None:
    """The workflow can publish an explicit running state before inspection begins."""
    write_full_crawl_status_badge(tmp_path, "running")

    status = json.loads(
        (tmp_path / "reports" / "full-crawl-status-badge.json").read_text(encoding="utf-8")
    )

    assert status == {
        "color": "007ec6",
        "label": "crawl",
        "message": "running",
        "schemaVersion": 1,
    }
