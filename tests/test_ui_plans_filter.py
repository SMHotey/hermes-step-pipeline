# -*- coding: utf-8 -*-
"""Фильтр вкладки «Планы» действительно скрывает строки (план 15, шаг S1, находка F1).

Симптом был такой: выбор проекта или состояния считался верно, а на экране ничего не
менялось. Причина — каскад: строки скрываются атрибутом `hidden`, встроенное правило
браузера `[hidden] { display: none }` проигрывает авторскому `.plan-card { display: grid }`,
и элемент остаётся видимым. Сторож держит правило, которое возвращает атрибуту силу, и
следит, чтобы фильтр по-прежнему ставил именно `hidden` (а не, скажем, пустой класс).
"""
from __future__ import annotations

from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def test_hidden_rule_covers_every_kind_of_row() -> None:
    """Правило `[hidden]` есть для карточек, строк и черновиков — все три вида в фильтре."""
    html = page()
    assert ".plan-card[hidden]" in html, "карточка плана без правила скрытия"
    assert ".plan-row[hidden]" in html, "строка плана без правила скрытия"
    assert ".draft-card[hidden]" in html, "карточка черновика без правила скрытия"
    rule = block(html, ".plan-card[hidden]", "}")
    assert "display: none" in rule, rule


def test_filter_still_hides_by_attribute_not_by_class() -> None:
    """Фильтр ставит `row.hidden`: правило CSS работает именно с этим атрибутом."""
    html = page()
    body = block(html, "function filterPlansTab(", "function renderPlansTab(")
    assert "row.hidden = !ok" in body, body
    assert "classList" not in body, "фильтр не должен подменять атрибут классом"


def test_cards_keep_display_grid_so_the_rule_is_necessary() -> None:
    """Проверка причины: у карточки авторский display, иначе правило было бы лишним.

    Если однажды карточки перестанут задавать `display`, правило `[hidden]` станет
    безвредным, но и повод для него исчезнет — тест скажет об этом прямо.
    """
    html = page()
    card = block(html, ".plan-card {", "}")
    assert "display: grid" in card, card
