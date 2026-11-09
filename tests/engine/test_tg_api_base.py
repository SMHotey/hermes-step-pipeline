# -*- coding: utf-8 -*-
"""Прокси Bot API в движке: уведомления идут тем же путём, что и шлюз Hermes."""
from __future__ import annotations

import gate

PIPELINE_KEY = "STEP_PIPELINE_TELEGRAM_API_BASE"
SHARED_KEY = "TELEGRAM_API_BASE_URL"


def test_default_base_without_env(monkeypatch) -> None:
    monkeypatch.delenv(PIPELINE_KEY, raising=False)
    monkeypatch.delenv(SHARED_KEY, raising=False)
    assert gate.tg_api_base() == "https://api.telegram.org"


def test_pipeline_key_wins_over_shared(monkeypatch) -> None:
    monkeypatch.setenv(PIPELINE_KEY, " https://proxy.example/x/ ")
    monkeypatch.setenv(SHARED_KEY, "https://shared.example")
    assert gate.tg_api_base() == "https://proxy.example/x"


def test_shared_hermes_key_is_fallback(monkeypatch) -> None:
    monkeypatch.delenv(PIPELINE_KEY, raising=False)
    monkeypatch.setenv(SHARED_KEY, "https://shared.example/")
    assert gate.tg_api_base() == "https://shared.example"


def test_bot_suffix_is_not_duplicated(monkeypatch) -> None:
    """Шлюз Hermes пишет базу как `…/bot` — движок не должен дать `…/bot/bot<токен>`."""
    monkeypatch.setenv(PIPELINE_KEY, "https://proxy.example/secret/bot/")
    assert gate.tg_api_base() == "https://proxy.example/secret"
