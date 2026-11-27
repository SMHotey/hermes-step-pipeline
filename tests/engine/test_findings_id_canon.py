# -*- coding: utf-8 -*-
"""Идентификатор находки: `F-1` читается как `F1` (случай 27.11.2026).

План 36 DoorSystem встал инцидентом «идентификатор не вида F<число>: 'F-1'» на
строке 8 реестра постановки S5: автор постановки написал номер по привычке от
`Q-1`, а разбор находок ждал слитную запись. Конвейер простоял с 01:00 до вечера,
потому что стоп-флаг инцидента никто не снимает автоматически.

Правило: разделитель после префикса — украшение записи, а не часть номера.
Канон один (`F1`), и по нему же идёт дедуп: `F-1` рядом с `F1` — повтор, а не
вторая находка.
"""
from __future__ import annotations

import sys

import pytest

from engine_kit import ENGINE

sys.path.insert(0, str(ENGINE))

import gate  # noqa: E402 - разбор реестра принадлежит движку
import keeper_heal  # noqa: E402

HEADER = "| " + " | ".join(gate.FINDINGS_HEADER) + " |"
SEP = "|" + "---|" * len(gate.FINDINGS_HEADER)


def registry(*rows: str) -> str:
    """Текст реестра находок шага: шапка, разделитель и строки как есть."""
    return "\n".join([HEADER, SEP, *rows]) + "\n"


def row(fid: str, status: str = "открыто") -> str:
    return f"| {fid} | S5 | замысел | находка | сделать | {status} |"


def test_hyphenated_id_is_read_as_canonical() -> None:
    """`F-1` — тот же номер, что `F1`, и в реестре он канонизируется."""
    parsed = gate.parse_findings(registry(row("F-1")), "prompt-36-S5-findings.md")

    assert [item["id"] for item in parsed] == ["F1"]
    assert [item["status"] for item in parsed] == ["открыто"]


def test_solid_id_still_reads() -> None:
    """Слитная запись работает как раньше."""
    parsed = gate.parse_findings(registry(row("F2")), "reg.md")

    assert [item["id"] for item in parsed] == ["F2"]


def test_hyphen_and_solid_same_number_is_duplicate() -> None:
    """`F-1` и `F1` в одном реестре — повтор номера, а не две находки."""
    with pytest.raises(gate.GateError, match="повторяется"):
        gate.parse_findings(registry(row("F-1"), row("F1")), "reg.md")


def test_question_ids_keep_their_separator() -> None:
    """У вопросов разделитель штатный: канонизация не портит `Q-3`."""
    header = "| " + " | ".join(gate.QUESTIONS_HEADER) + " |"
    sep = "|" + "---|" * len(gate.QUESTIONS_HEADER)
    body = "| Q-3 | S5 | вопрос | варианты | рекомендация | открыт | — |\n"
    parsed = gate.parse_findings(f"{header}\n{sep}\n{body}", "questions.md",
                                 gate.QUESTIONS_HEADER, gate.QUESTIONS_KEYS,
                                 **gate._question_rules())

    assert [item["id"] for item in parsed] == ["Q-3"]


def test_keeper_reads_hyphenated_id_and_writes_canonical(tmp_path) -> None:
    """Смотритель читает ту же запись: иначе починка не нашла бы строку реестра."""
    path = tmp_path / "findings.md"
    path.write_text(registry(row("F-4")), encoding="utf-8")

    rows = keeper_heal.parse_registry(path.read_text(encoding="utf-8"))

    assert [item["id"] for item in rows] == ["F4"]
    assert [item["number"] for item in rows] == [4]
