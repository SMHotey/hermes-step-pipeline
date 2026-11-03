# -*- coding: utf-8 -*-
"""Тестовое сообщение уважает префикс проекта (иначе владелец не отличит конвейеры)."""
from __future__ import annotations

from step_pipeline import telegram


def test_message_without_prefix() -> None:
    assert telegram.test_message("") == "🧪 Тест уведомлений конвейера."


def test_message_with_prefix() -> None:
    assert telegram.test_message("Есенин: ") == "Есенин: 🧪 Тест уведомлений конвейера."
