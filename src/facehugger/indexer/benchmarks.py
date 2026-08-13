"""Client-contract latency measurements for generated static indexes."""

import contextlib
import json
import threading
from collections.abc import Generator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, cast

import httpx

from facehugger.client import FacehuggerClient
from facehugger.hashes import normalize_sha256
from facehugger.indexer.reports import summarize

_WARM_LOOKUP_REPETITIONS = 10


def measure_local_lookup(site: Path, digest: str) -> dict[str, object]:
    """Measure cold and warm lookups against the generated static site."""
    with _static_server(site) as base_url:
        return measure_static_lookup(base_url, digest)


def measure_static_lookup(base_url: str, digest: str) -> dict[str, object]:
    """Measure client lookups and CORS headers from one deployed static site."""
    normalized = normalize_sha256(digest)
    with TemporaryDirectory(prefix="facehugger-benchmark-") as cache_root:
        client = FacehuggerClient(base_url=base_url, cache_dir=Path(cache_root))
        cold = _duration(client, normalized)
        warm = [_duration(client, normalized) for _ in range(_WARM_LOOKUP_REPETITIONS)]
    return {
        "cold": summarize([cold]),
        "warm": summarize(warm),
        "cors": _cors_headers(base_url, normalized),
    }


def update_deployment_measurements(
    report_path: Path, pages_url: str, digest: str
) -> dict[str, object]:
    """Add real Pages measurements to an existing proof report and return it."""
    value: object = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Proof report must be a JSON object.")
    report = cast(dict[str, object], value)
    measurement = measure_static_lookup(pages_url, digest)
    report["pages_lookup_latency_seconds"] = {
        "cold": measurement["cold"],
        "warm": measurement["warm"],
    }
    report["cors"] = measurement["cors"]
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _duration(client: FacehuggerClient, digest: str) -> float:
    started = perf_counter()
    client.lookup(digest)
    return perf_counter() - started


def _cors_headers(base_url: str, digest: str) -> dict[str, str | None]:
    response = httpx.get(_url(base_url, "api/v1/manifest.json"), timeout=30.0)
    response.raise_for_status()
    manifest: Any = response.json()
    if not isinstance(manifest, dict):
        raise ValueError("Static manifest must be an object.")
    base_path = cast(dict[str, object], manifest).get("base_path")
    if not isinstance(base_path, str):
        raise ValueError("Static manifest base path is invalid.")
    manifest_mapping = cast(dict[str, object], manifest)
    prefix_length = manifest_mapping.get("prefix_hex_chars")
    if not isinstance(prefix_length, int) or prefix_length < 3:
        raise ValueError("Static manifest shard prefix length is invalid.")
    prefix = digest[:prefix_length]
    shard = httpx.get(_url(base_url, f"{base_path}/{prefix[:2]}/{prefix[2:]}.json"), timeout=30.0)
    shard.raise_for_status()
    return {
        "manifest_access_control_allow_origin": response.headers.get("Access-Control-Allow-Origin"),
        "shard_access_control_allow_origin": shard.headers.get("Access-Control-Allow-Origin"),
    }


def _url(base_url: str, relative_path: str) -> str:
    return base_url.rstrip("/") + "/" + relative_path.lstrip("/")


@contextlib.contextmanager
def _static_server(site: Path) -> Generator[str, None, None]:
    handler = _request_handler(site)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class _QuietRequestHandler(SimpleHTTPRequestHandler):
    """Silence the ephemeral server used for local benchmark measurements."""

    def log_message(self, format: str, *args: object) -> None:
        """Discard request logging from benchmark infrastructure."""


def _request_handler(site: Path) -> type[SimpleHTTPRequestHandler]:
    class StaticRequestHandler(_QuietRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=site, **kwargs)

    return StaticRequestHandler
