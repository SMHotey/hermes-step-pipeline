# -*- coding: utf-8 -*-
"""Находки ревью плана на вкладке «Шаг»: текст, а не только счётчик (0.34.0).

Живой случай (17.11.2026): план 1 проекта self-analyzer не прошёл ревью, панель показала
«5 находок» — и ни одной находки. Править было нечего, владелец пошёл искать текст в файлах.
Тесты держат страницу: функцию вывода строк реестра, её вызов в блоке «Ревью плана» и поля,
без которых находка нечитаема (ID, шаг, класс, статус, текст, «что сделать»).

Движка JS в проекте нет: сторож читает текст страницы, как соседние тесты вкладки «Шаг».
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402,F401  (загружает плагин и алиасы модулей)

PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    """Кусок текста от `start` до ближайшего `end` после него."""
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def test_step_tab_has_plan_findings_placeholder():
    """Место под находки реестра есть на вкладке «Шаг», внутри блока «Ревью плана»."""
    html = page()
    start = html.index('id="tab-step"')
    assert 'id="plan-review-findings"' in html[start:]


def test_registry_rows_are_rendered_with_identity_and_action():
    """Строка реестра рисуется с ID, шагом, классом и «что сделать»: без них не починить."""
    body = block(page(), "function planFindingsHtml(", "function renderPlanReview(")
    for field in ("row.id", "row.step", "row.cls", "row.status", "row.text", "row.action"):
        assert field in body, field
    assert "что сделать" in body


def test_open_findings_are_counted_and_lead():
    """Открытые считаются отдельно и идут первыми: вердикт держат именно они."""
    body = block(page(), "function planFindingsHtml(", "function renderPlanReview(")
    assert "pf.open" in body and "pf.blocking" in body
    assert "находки реестра" in body


def test_plan_review_calls_the_findings_renderer():
    """Блок «Ревью плана» зовёт вывод строк: иначе функция есть, а находок не видно."""
    body = block(page(), "function renderPlanReview(", "function renderStep(")
    assert "planFindingsHtml(data.plan_findings)" in body
    assert "находки отчёта:" in body


def test_empty_registry_shows_note_not_silence():
    """Нет реестра — подпись; пустой блок читался бы как «находок нет»."""
    body = block(page(), "function planFindingsHtml(", "function renderPlanReview(")
    assert "pf.note" in body
