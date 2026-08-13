"""Static client request and offline-cache tests."""

from pathlib import Path

import httpx
from pytest import MonkeyPatch

from facehugger.client import FacehuggerClient

_DIGEST = "abc" + "0" * 61


def test_client_fetches_only_manifest_and_derived_shard_then_uses_cache(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A cold lookup uses two static URLs and a warm offline lookup uses none."""
    requests: list[str] = []
    manifest = {
        "schema_version": 1,
        "algorithm": "sha256",
        "index_version": "proof",
        "generated_at": "2026-01-01T00:00:00Z",
        "catalog_cutoff": None,
        "complete": False,
        "prefix_hex_chars": 3,
        "record_format": "facehugger-json-shard-v1",
        "base_path": "api/v1/index/proof/sha256",
    }
    shard = {
        "v": 1,
        "p": "abc",
        "r": [["0" * 61, [["example/model", "model.safetensors", "1" * 40, 42, "lfs"]]]],
    }

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        requests.append(url)
        data = manifest if url.endswith("manifest.json") else shard
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr("facehugger.client.httpx.get", fake_get)
    client = FacehuggerClient(base_url="https://example.test/", cache_dir=tmp_path)
    result = client.lookup(_DIGEST)
    assert result.matches[0].repo_id == "example/model"
    assert requests == [
        "https://example.test/api/v1/manifest.json",
        "https://example.test/api/v1/index/proof/sha256/ab/c.json",
    ]

    offline = FacehuggerClient(base_url="https://example.test/", cache_dir=tmp_path, offline=True)
    assert offline.lookup(_DIGEST).matches == result.matches
    assert len(requests) == 2
