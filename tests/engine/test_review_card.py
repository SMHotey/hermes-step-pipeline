# -*- coding: utf-8 -*-
"""Карточка ревью: строка-образец ссылки на коммит разбирается стражем без ручной правки.

Проверяется не текст шаблона как таковой, а сквозной путь: строка, взятая прямо из
отрендеренной карточки ревью, попадает в отчёт ревьюера и разбирается `verdict_of`
в тот же sha, а при APPROVE sha оказывается в третьей колонке журнала плана.
"""
from __future__ import annotations

import re
from pathlib import Path

import config as config_mod
import gate

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1.md"
SHA = "a1b2c3d"


def card_commit_line(project: Path) -> str:
    """Строка карточки ревью с образцом ссылки на коммит (единственная со «сверять»)."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    body = gate.review_card_body(p, STEP, 1, REPORT_REL, None)
    lines = [ln for ln in body.splitlines() if "коммит" in ln and "сверять" in ln]
    assert len(lines) == 1, f"строк со словами «коммит» и «сверять»: {len(lines)} — {lines}"
    return lines[0]


def write_report(project: Path) -> dict:
    """Отчёт ревьюера с APPROVE, собранный из строки карточки без ручной правки."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    # Образец sha подставляется по угловым скобкам: текст образца не хардкодится.
    line = re.sub(r"<[^>]*>", SHA, card_commit_line(project))
    gate.review_path(p, "S1").write_text("\n".join([
        "## ВЕРДИКТ: APPROVE",
        "",
        line,
        "",
        "Гейты: python -m pytest tests/engine/test_review_card.py → 3 passed",
        "",
    ]), encoding="utf-8")
    return gate.verdict_of(p, "S1")


def test_card_line_has_single_tick_pair(project):
    """Ровно одна пара бэктиков вокруг образца sha и ни одного обратного слэша."""
    line = card_commit_line(project)
    assert line.count("`") == 2, f"бэктиков в строке: {line.count('`')} — {line!r}"
    assert "\\" not in line, f"обратный слэш в строке карточки — {line!r}"


def test_card_line_parsed_to_same_sha(project):
    """Строка из карточки ревью разбирается verdict_of в тот же sha."""
    v = write_report(project)
    assert v["verdict"] == "APPROVE", f"вердикт не распознан: {v}"
    assert v["commit"] == SHA, f"commit из отчёта: {v['commit']!r}"


def test_approve_writes_sha_to_journal(project):
    """При APPROVE в журнал попадает sha, а не прочерк."""
    v = write_report(project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    assert gate.append_journal_row(p, "S1", STEP["title"], v["commit"] or "",
                                  "гейты: 3 passed") is True
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")]
            for ln in Path(p.plan_file).read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("|")]
    journal_rows = [r for r in rows if len(r) >= 5 and r[1].startswith("S1")]
    assert len(journal_rows) == 1, f"строк журнала по S1: {len(journal_rows)}"
    assert journal_rows[0][2] == SHA, f"третья колонка журнала: {journal_rows[0][2]!r}"
