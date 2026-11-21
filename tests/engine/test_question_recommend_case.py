# -*- coding: utf-8 -*-
"""Буква рекомендации читается в любом регистре (живой Q-1 живого проекта: «Рекомендация» с большой).

`parse_question_line` ищет маркер `рекомендаци\\w*` с `re.I`, а `QUESTION_RECOMMEND_RE`
был написан строчными без флага: маркер находился, хвост отрезался, буква из хвоста не
читалась — рекомендация молча терялась. Следствие живое: автовыбор по таймауту
(`ANSWER_TIMEOUT_DEFAULT`) не срабатывал, вопрос стоял до ручного ответа, а в
`docs/reviews/*-questions.md` колонка «Рекомендация» оставалась пустой при написанной
автором рекомендации.
"""
from __future__ import annotations

import pytest

import gate

QUESTION_TEXT = "поднять ли лимит карточек до трёх"

#: Хвост строки вопроса (как его отделяет `parse_question_line`) и ожидаемая буква.
CASES = [
    (" — рекомендация A", "A"),
    (" — Рекомендация A", "A"),
    (" — Рекомендация: B.", "B"),
    (". Рекомендация A.", "A"),
    (" — рекомендации нет", ""),
]


@pytest.mark.parametrize("tail, expected", CASES)
def test_recommend_letter_any_case(tail: str, expected: str) -> None:
    line = f"Q1: {QUESTION_TEXT}{tail}"
    parsed = gate.parse_question_line(line)
    assert parsed is not None, line
    assert parsed["recommend"] == expected, line
    # Хвост с рекомендацией не уезжает ни в текст вопроса, ни в варианты.
    assert "екомендац" not in parsed["text"], line
    assert parsed["options"] == "", line
    assert parsed["text"].startswith(QUESTION_TEXT), line
