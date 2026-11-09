# -*- coding: utf-8 -*-
"""Карточки правок: находку закрывают классом, а не одной процитированной строкой.

Причина кругов `promptfix → promptreview`: ревьюер читает постановку целиком, а карточка
перечисляла находки по строкам, и правка круга N рождала находку круга N+1 в соседних
тестах и разделах. Проверяется, что требование свипа класса и полной сверки документа
есть и в карточке переписывания постановки, и в карточке правок шага, а блокирующие
находки по-прежнему попадают в тело дословно.
"""
from __future__ import annotations

from pathlib import Path

import config as config_mod
import gate

STEP = {"id": "S1", "epoch": "", "title": "Мастер первого запуска", "days": 1.0,
        "owner": "", "goal": "g", "files": [], "gates": ["python -m pytest tests/test_x.py -q"],
        "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1-prompt.md"
FINDING = ("[блокирующая] docs/prompts/2026-11-09_1-S1_prompt.md:534-570 — "
           "тест 10 не изолирует `profile_dir`")


def stand(project: Path) -> tuple[gate.Paths, str]:
    """Пути плана 1 и текст карточки переписывания постановки (круг 2)."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    (Path(p.repo) / "docs" / "prompts" / "2026-11-09_1-S1_prompt.md").write_text(
        "recon_commit: abc1234\n\n# Постановка\n", encoding="utf-8")
    (Path(p.repo) / REPORT_REL).write_text("\n".join([
        "recon_commit: abc1234",
        "",
        "## ВЕРДИКТ: REQUEST_CHANGES",
        "",
        f"- {FINDING}",
        "",
        "## Рекомендации владельцу",
        "",
        "- поправить изоляцию в тестах 10 и 13",
        "",
    ]), encoding="utf-8")
    return p, gate.prompt_fix_card_body(p, STEP, 2, REPORT_REL)


def test_prompt_fix_card_demands_class_sweep(project):
    """Карточка переписывания требует закрыть класс и перечитать весь документ."""
    p, body = stand(project)
    assert "СВИП КЛАССА" in body, body
    assert "`search_files` все места" in body, "нет требования искать тот же класс в других местах"
    assert "где ещё нашёл" in body, "нет требования отчитаться о найденных соседних местах"
    assert "документ целиком" in body, "нет требования полной сверки, а не только диффа"
    assert "abc1234" in body, "не подставлен recon_commit постановки"
    assert "не изолирует" in body, "блокирующая находка не попала в карточку дословно"


def test_step_fix_card_demands_class_check(project):
    """Карточка правок шага требует проверить соседние вызовы, а не только строку отчёта."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    body = gate.fix_card_body(p, STEP, 1, "docs/reviews/1-S1.md")
    assert "Находка — это класс" in body, body
    assert "соседние вызовы" in body, "не сказано, где искать тот же дефект"
