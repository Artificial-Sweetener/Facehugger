"""Hub request metrics and rate-limit response tests."""

from datetime import UTC, datetime

import httpx

from facehugger.indexer.rate_limit import RateController, RequestMetrics


def test_request_metrics_classify_tree_metadata_requests() -> None:
    """Request metrics expose endpoint categories without retaining request URLs."""
    metrics = RequestMetrics()
    request = httpx.Request("GET", "https://huggingface.co/api/models/owner/model/tree/main")
    metrics.observe(httpx.Response(200, request=request))
    assert metrics.categories == {"repo_tree": 1}
    assert metrics.statuses == {"200": 1}


def test_rate_controller_honors_retry_after() -> None:
    """A 429 response moves the controller's pause deadline into the future."""
    controller = RateController()
    request = httpx.Request("GET", "https://huggingface.co/api/models")
    controller.observe(httpx.Response(429, headers={"Retry-After": "1"}, request=request))
    assert controller.paused_until is not None
    assert controller.paused_until > datetime.now(UTC)
