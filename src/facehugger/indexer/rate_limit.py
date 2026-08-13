"""Conservative response-header-aware Hub request pacing."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import sleep

import httpx


@dataclass
class RequestMetrics:
    """Aggregate HTTP metrics without retaining sensitive request data."""

    requests: int = 0
    statuses: dict[str, int] = field(default_factory=dict[str, int])
    categories: dict[str, int] = field(default_factory=dict[str, int])
    rate_limit: str | None = None
    rate_limit_policy: str | None = None
    rate_limit_remaining: str | None = None

    def observe(self, response: httpx.Response) -> None:
        """Record one response's safe aggregate fields."""
        self.requests += 1
        status = str(response.status_code)
        self.statuses[status] = self.statuses.get(status, 0) + 1
        category = _request_category(response.request.url.path)
        self.categories[category] = self.categories.get(category, 0) + 1
        self.rate_limit = response.headers.get("RateLimit", self.rate_limit)
        self.rate_limit_policy = response.headers.get("RateLimit-Policy", self.rate_limit_policy)
        self.rate_limit_remaining = response.headers.get(
            "RateLimit-Remaining", self.rate_limit_remaining
        )


def _request_category(path: str) -> str:
    if "/tree/" in path:
        return "repo_tree"
    if path.startswith("/api/models/"):
        return "model_info"
    if path.startswith("/api/models"):
        return "model_catalog"
    if "/resolve/" in path:
        return "resolver"
    return "other"


class RateController:
    """Apply a small fixed request interval and honor parsed reset delays."""

    def __init__(self, requests_per_minute: int = 100) -> None:
        """Create a controller that never exceeds the configured sustained rate."""
        if requests_per_minute <= 0:
            raise ValueError("Request rate must be positive.")
        self.interval_seconds = 60 / requests_per_minute
        self._next_request = datetime.now(UTC)
        self._paused_until: datetime | None = None

    def wait(self) -> None:
        """Wait until both the sustained-rate and any server reset deadline allow a request."""
        now = datetime.now(UTC)
        target = max((self._next_request, self._paused_until or now))
        delay = (target - now).total_seconds()
        if delay > 0:
            sleep(delay)
        self._next_request = datetime.now(UTC) + timedelta(seconds=self.interval_seconds)

    def observe(self, response: httpx.Response) -> None:
        """Pause after a rate-limit response when the server exposes a reset duration."""
        if response.status_code != 429:
            return
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None and retry_after.isdigit():
            self._paused_until = datetime.now(UTC) + timedelta(seconds=int(retry_after))

    @property
    def paused_until(self) -> datetime | None:
        """Return the current server-requested pause deadline, if any."""
        return self._paused_until
