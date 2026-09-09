"""Hub request metrics and rate-limit response tests."""

from datetime import UTC, datetime
from threading import Barrier, Thread

import httpx
from pytest import MonkeyPatch

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


def test_rate_controller_reserves_distinct_slots_for_concurrent_requests(
    monkeypatch: MonkeyPatch,
) -> None:
    """Concurrent callers share one sustained-rate schedule instead of bursting together."""
    controller = RateController(requests_per_minute=600)
    start = Barrier(4)
    delays: list[float] = []

    def record_delay(delay: float) -> None:
        """Capture reserved waits without delaying the deterministic test."""
        delays.append(delay)

    def wait_for_slot() -> None:
        """Start one controller request alongside the other workers."""
        start.wait()
        controller.wait()

    monkeypatch.setattr("facehugger.indexer.rate_limit.sleep", record_delay)
    workers = [Thread(target=wait_for_slot) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert len(delays) == 3
    assert max(delays) >= 0.25
