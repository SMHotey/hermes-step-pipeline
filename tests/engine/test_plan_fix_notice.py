# -*- coding: utf-8 -*-
"""Уведомление о круге правок плана: находки идут текстом, а не только счётом.

Замечание владельца 0.34.0: в боте приходило «правим план кругом 1 (карточка t_…)» без
единой находки, поэтому чинить было нечего. Тесты держат две вещи: открытые находки попадают
в сообщение строками (ID, шаг, суть, что сделать), а бюджет сообщения режется с явной
пометкой, а не молча.
"""
from __future__ import annotations

import gate

HEAD = "план 1: ревью вернуло REQUEST_CHANGES — правим план кругом 1"

ROWS = [
    {"id": "F1", "step": "S1", "cls": "замысел", "status": "открыто",
     "text": "В шаге нет стоп-сигнала", "action": "Добавить `Риск: high` и стоп"},
    {"id": "F2", "step": "S2", "cls": "двусмысленность", "status": "открыто",
     "text": "Файл-источник не назван", "action": "Назвать `bot/config.py`"},
    {"id": "F3", "step": "S3", "cls": "стиль", "status": "не блокирует",
     "text": "Лишняя строка", "action": "Убрать"},
]


def test_notice_lists_open_findings_with_what_to_do():
    """Каждая открытая находка — отдельной строкой: ID, шаг, суть, что сделать."""
    text = gate.plan_fix_notice("1", "t_123", "self-analyzer-prompter", 1, ROWS)
    assert text.startswith(HEAD)
    assert "t_123" in text and "self-analyzer-prompter" in text
    assert "открыто находок: 2" in text
    assert "F1 (S1): В шаге нет стоп-сигнала → Добавить `Риск: high` и стоп" in text
    assert "F2 (S2): Файл-источник не назван → Назвать `bot/config.py`" in text


def test_non_blocking_findings_stay_out_of_the_notice():
    """Неблокирующая находка вердикт не держит — в сообщении её нет, чтобы не путать."""
    text = gate.plan_fix_notice("1", "t_123", "p", 1, ROWS)
    assert "F3" not in text


def test_notice_without_registry_keeps_the_old_line():
    """Реестра нет — сообщение остаётся прежним: одна строка без выдуманных находок."""
    assert gate.plan_fix_notice("1", "t_9", "p", 2, []) == (
        "план 1: ревью вернуло REQUEST_CHANGES — правим план кругом 2 (карточка t_9, "
        "профиль p); после правки ревью повторится")


def test_notice_trims_long_text_and_counts_the_rest():
    """Длинные тексты режутся, а невлезшие строки называются числом — потолок Telegram."""
    rows = [{"id": f"F{n}", "step": "S1", "status": "открыто",
             "text": "очень длинная находка " * 40, "action": "длинное действие " * 20}
            for n in range(1, 30)]
    text = gate.plan_fix_notice("1", "t_1", "p", 1, rows)
    assert len(text) <= gate.NOTICE_TOTAL_CAP + 400
    assert "…и ещё" in text
    assert "…" in text


def test_notice_text_fits_telegram_limit_for_a_dozen_findings():
    """Двенадцать обычных находок — в лимит Telegram (4096) без обрезки хвоста."""
    rows = [{"id": f"F{n}", "step": "S1", "status": "открыто",
             "text": "короткая находка", "action": "короткое действие"} for n in range(1, 13)]
    text = gate.plan_fix_notice("1", "t_1", "p", 1, rows)
    assert len(text) < 4096
    assert "F12" in text
    assert "…и ещё" not in text
