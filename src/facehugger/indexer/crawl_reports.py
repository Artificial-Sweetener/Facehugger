"""Safe full-crawl progress reports and Shields-compatible badge records."""

import json
from pathlib import Path
from typing import Protocol

from facehugger.indexer.rate_limit import RequestMetrics


class CrawlProgressView(Protocol):
    """The progress fields required by full-crawl report publication."""

    @property
    def eligible_repositories(self) -> int:
        """Return the number of cataloged repositories eligible for inspection."""
        raise NotImplementedError

    @property
    def indexed_repositories(self) -> int:
        """Return the number of eligible repositories with current metadata."""
        raise NotImplementedError

    @property
    def pending_repositories(self) -> int:
        """Return the number of eligible repositories awaiting inspection."""
        raise NotImplementedError

    def as_dict(self) -> dict[str, int | bool]:
        """Return the stable JSON-safe crawl progress contract."""
        raise NotImplementedError


def write_full_crawl_report(
    root: Path, progress: CrawlProgressView, metrics: RequestMetrics
) -> None:
    """Write safe progress and badge records without request details or credentials."""
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    payload = {**progress.as_dict(), "requests": metrics.requests, "statuses": metrics.statuses}
    _write_json(reports / "full-crawl.json", payload)
    _write_json(
        reports / "full-crawl-inspected-badge.json",
        _badge(
            "eligible inspected",
            f"{progress.indexed_repositories:,} / {progress.eligible_repositories:,}",
            "007ec6",
        ),
    )
    _write_json(
        reports / "full-crawl-remaining-badge.json",
        _badge(
            "remaining",
            f"{progress.pending_repositories:,}",
            "brightgreen" if progress.pending_repositories == 0 else "orange",
        ),
    )


def _badge(label: str, message: str, color: str) -> dict[str, int | str]:
    """Return one Shields endpoint response record."""
    return {"schemaVersion": 1, "label": label, "message": message, "color": color}


def _write_json(path: Path, value: object) -> None:
    """Write canonical public JSON with a trailing newline."""
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
