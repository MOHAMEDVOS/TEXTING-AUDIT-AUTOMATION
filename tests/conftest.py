"""Shared pytest fixtures for KeyPoolManager tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ai.providers.base import AIProvider


class FakeProvider(AIProvider):
    """Test double for AIProvider — records calls, returns configurable output."""

    def __init__(self, api_key: str, model: str = "fake-model"):
        self._api_key = api_key
        self._model = model
        self.calls: list[tuple] = []
        self.response: str | Exception = '{"ok": true}'

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, system_prompt, user_content, *, max_tokens=1200, temperature=0.1) -> str:
        self.calls.append((system_prompt, user_content, max_tokens, temperature))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def fake_provider_factory():
    """Factory that returns a new FakeProvider per call."""
    created: list[FakeProvider] = []

    def _make(api_key: str = "fake-key", model: str = "fake-model") -> FakeProvider:
        p = FakeProvider(api_key, model)
        created.append(p)
        return p

    _make.created = created
    return _make


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch) -> Path:
    """Create tmp config dir with groq_keys.json and agent_keys.json; patch PROJECT_ROOT."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "groq_keys.json").write_text("[]")
    (config_dir / "agent_keys.json").write_text("{}")

    # Patch the module-level path constants in ai.analyzer
    import ai.analyzer as analyzer_mod
    monkeypatch.setattr(analyzer_mod, "_AGENT_KEYS_FILE", config_dir / "agent_keys.json")
    monkeypatch.setattr(analyzer_mod, "_GROQ_KEYS_FILE", config_dir / "groq_keys.json")

    return config_dir


@pytest.fixture
def frozen_time(monkeypatch):
    """Patch time.monotonic() to return a controllable value."""
    current = [1000.0]

    def fake_monotonic():
        return current[0]

    def advance(seconds: float):
        current[0] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    class Clock:
        @staticmethod
        def advance(seconds: float):
            current[0] += seconds

        @staticmethod
        def now() -> float:
            return current[0]

    return Clock()
