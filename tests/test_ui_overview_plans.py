# -*- coding: utf-8 -*-
"""Исполненные планы на «Обзоре» живут в одном месте (план 15, шаг S2, находка F2).

Было два списка: свёрнутый блок «Исполненные планы» (`#ov-closed`, с выбором проекта) и
общий список строк планов (`#plans`), который закрытые не отбрасывал. Владелец видел одни
и те же планы дважды. Сторож держит границу: в общем списке закрытых нет, свёрнутый блок
питается прежним источником (`/api/projects`), второго расчёта не появляется.
"""
from __future__ import annotations

from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin: text.index(end, begin + len(start))]


def test_general_plan_list_drops_closed_plans() -> None:
    """Список строк планов отбрасывает состояние «закрыт» — до отрисовки, а не в CSS."""
    html = page()
    region = block(html, "collectNotify(data.notify);", 'const planBadge')
    assert 'const plans = (data.plans || []).filter((p) => p.state !== "closed");' in region, region


def test_collapsible_block_keeps_its_own_source() -> None:
    """Свёрнутый блок «Исполненные планы» остаётся и питается реестром, не снимком."""
    html = page()
    assert 'id="ov-closed"' in html
    assert "Исполненные планы" in html
    load = block(html, "async function loadProjects(", "// ── глобальная пауза панели")
    assert "renderClosedPlans(lastProjects)" in load, load
    assert "/api/snapshot" not in load, "второго запроса ради закрытых планов быть не должно"


def test_closed_rows_still_have_their_badge_for_the_block() -> None:
    """Бейдж состояния «закрыт» никуда не делся: им пользуется свёрнутый блок."""
    html = page()
    assert 'closed: "ok"' in html
