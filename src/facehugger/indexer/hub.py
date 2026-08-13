"""Shared Hugging Face client construction and request observability."""

import httpx
from huggingface_hub import HfApi, set_client_factory

from facehugger.indexer.rate_limit import RateController, RequestMetrics


def create_hub_api(token: str) -> HfApi:
    """Create the dedicated Hub client with an attributable project user agent."""
    return HfApi(
        token=token,
        library_name="facehugger",
        library_version="0.0.0",
        user_agent="https://github.com/Artificial-Sweetener/Facehugger",
    )


def configure_hub_http(metrics: RequestMetrics, controller: RateController) -> None:
    """Apply shared pacing and safe request metrics to Hub client traffic."""

    def request_hook(_: httpx.Request) -> None:
        controller.wait()

    def response_hook(response: httpx.Response) -> None:
        metrics.observe(response)
        controller.observe(response)

    set_client_factory(
        lambda: httpx.Client(
            event_hooks={"request": [request_hook], "response": [response_hook]},
            follow_redirects=True,
            timeout=30.0,
        )
    )
