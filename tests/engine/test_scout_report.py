# -*- coding: utf-8 -*-
"""Гейт артефакта разведки: шаблон, пустые поля, вердикты и вывод в план."""
from __future__ import annotations

from pathlib import Path

import pytest

import scout_report as sr

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "docs" / "research" / "TEMPLATE.md"

SAMPLE = """# Разведка: план 1 «Демо» — готовые решения и практики

Дата: 2026-11-06. Объект: цикл план → разведка. Метод: GitHub API.

## Кандидаты

| Кандидат | Ссылка | Звёзды | Дата коммита | Лицензия | Что даёт | Стоимость, дн | Вердикт |
|---|---|---|---|---|---|---|---|
| demo/spec-kit | https://github.com/demo/spec-kit | 1200 | 2026-10-30 | MIT | конституция и чек-листы | 2 | берём |
| demo/agent-factory | https://github.com/demo/agent-factory | 300 | 2026-09-01 | Apache-2.0 | фабрика ролей | 3-5 | своё |

## Итог

Решение: go — берём практики spec-kit
"""

ARTIFACT = "docs/research/1-prior-art.md"


def codes(text: str) -> dict[str, str]:
    return {code: level for level, code, _msg in sr.check_text(text).problems}


def test_template_header_matches_columns():
    text = TEMPLATE.read_text(encoding="utf-8")
    header = next(line for line in text.splitlines() if line.startswith("| Кандидат |"))
    assert header == "| " + " | ".join(sr.COLUMNS) + " |"
    assert len(sr.COLUMNS) == 8


def test_sample_report_is_accepted():
    report = sr.check_text(SAMPLE)
    assert report.accepted is True
    assert set(codes(SAMPLE).values()) == {"PASS"}
    assert len(report.candidates) == 2
    assert report.candidates[0].name == "demo/spec-kit"
    assert report.candidates[0].verdict == "берём"
    assert report.candidates[0].last_commit == "2026-10-30"
    assert report.decision == "go"
    assert report.decision_note == "берём практики spec-kit"


def test_empty_cells_are_rejected():
    text = SAMPLE.replace("| MIT |", "|  |").replace("| берём |", "| — |")
    report = sr.check_text(text)
    assert codes(text)["R2"] == "FAIL"
    assert report.accepted is False
    errors = "\n".join(report.errors)
    assert "Лицензия" in errors
    assert "Вердикт" in errors
    assert "нарушений: 2" in errors
    with pytest.raises(ValueError):
        sr.summary_for_plan(report, ARTIFACT)


def test_unknown_verdict_is_rejected():
    text = SAMPLE.replace("| берём |", "| возьмём |")
    report = sr.check_text(text)
    assert codes(text)["R3"] == "FAIL"
    assert "форкаем" in "\n".join(report.errors)


def test_bad_commit_date_is_rejected():
    text = SAMPLE.replace("| 2026-10-30 |", "| 30.10.2026 |")
    report = sr.check_text(text)
    assert codes(text)["R4"] == "FAIL"
    assert "ГГГГ-ММ-ДД" in "\n".join(report.errors)


def test_missing_or_unknown_decision_is_rejected():
    without = SAMPLE.replace("Решение: go — берём практики spec-kit\n", "")
    assert codes(without)["R5"] == "FAIL"
    text = SAMPLE.replace("Решение: go", "Решение: может быть")
    report = sr.check_text(text)
    assert codes(text)["R5"] == "FAIL"
    assert "needs-clarification" in "\n".join(report.errors)


def test_summary_for_plan_renders_section():
    block = sr.summary_for_plan(sr.check_text(SAMPLE), ARTIFACT)
    assert block.startswith("## Разведка")
    assert f"Артефакт: `{ARTIFACT}`" in block
    assert "| " + " | ".join(sr.COLUMNS) + " |" in block
    assert block.count("| demo/") == 2
    assert "Решение: go — берём практики spec-kit" in block
    lowered = block.lower()
    assert "андидат" in lowered
    assert "иценз" in lowered
    assert "prior-art" in lowered
    assert "решение" in lowered
