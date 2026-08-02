"""GET /api/config: the UI asks which model is answering.

The composer used to hardcode its model label, which quietly lied the moment
``PENNY_AGENT_MODEL`` named anything else. These pin the contract the UI now
depends on: the *configured* model, and a label that degrades to the raw id
rather than guessing.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

import penny.api.main as main


def test_config_reports_the_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENNY_AGENT_MODEL", "moonshotai/kimi-k3")

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    assert r.status_code == 200
    assert r.json()["model"] == {"id": "moonshotai/kimi-k3", "label": "Kimi K3"}


def test_config_defaults_to_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PENNY_AGENT_MODEL", raising=False)

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    assert r.json()["model"] == {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash"}


def test_unknown_model_falls_back_to_its_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accurate unfamiliar id beats a pretty wrong one."""
    monkeypatch.setenv("PENNY_AGENT_MODEL", "some-vendor/experimental-9")

    with TestClient(main.app) as client:
        r = client.get("/api/config")

    model = r.json()["model"]
    assert model["id"] == "some-vendor/experimental-9"
    assert model["label"] == "some-vendor/experimental-9"
