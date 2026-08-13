"""Bounded proof runtime configuration tests."""

from typing import Any

from pytest import MonkeyPatch

from facehugger.indexer.hub import create_hub_api


def test_hub_client_identifies_the_project(monkeypatch: MonkeyPatch) -> None:
    """Hub traffic carries the dedicated project's descriptive user agent."""
    captured: dict[str, Any] = {}

    def fake_api(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("facehugger.indexer.hub.HfApi", fake_api)
    create_hub_api("test-token")
    assert captured["token"] == "test-token"
    assert captured["user_agent"] == "https://github.com/Artificial-Sweetener/Facehugger"
