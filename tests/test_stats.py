# -*- coding: utf-8 -*-
"""Метрики цикла плана (план 5, шаг S8): счётчики из файлов проекта.

Число в утверждении — значение, которое тест сам насчитал в фикстуре: ослаблять
проверки («ключ присутствует») нельзя, это и есть предмет шага.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
import project as proj
from step_pipeline import stats

CONFIG = {
    "project": "demo",
    "board": "demo",
    "paths": {"plans": "docs/plans", "reviews": "docs/reviews",
              "research": "docs/research", "state": ".pipeline/state"},
    "markers": {"journal": "журнал", "steps": "шаг"},
    "step_id_regex": r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)",
}

CONSTITUTION = "Конституция: docs/CONSTITUTION.md\n"

JOURNAL = """## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


def plan_text(title: str, *, constitution: bool = True, research: bool = False,
              edits: int | None = None) -> str:
    """Текст плана: шапка, необязательные `Конституция:` и `## Разведка`, шаги, журнал."""
    text = f"# {title}\n\n"
    if constitution:
        text += CONSTITUTION + "\n"
    if research:
        text += "## Разведка\n\nСмотрели готовые решения.\n\n"
    text += "## Шаги\n\n### S1. Первый шаг (1 дн)\n\n- Файл: `app/x.py`\n\n"
    if edits is not None:
        text += f"Правки по разведке: {edits}\n\n"
    return text + JOURNAL


def artifact_text(rows: list[str], columns: int = 8) -> str:
    """Артефакт разведки: раздел `## Кандидаты` и таблица нужного числа колонок.

    Вердикт — всегда последняя ячейка строки: так его читает R4.
    """
    head = ["Кандидат", "Ссылка", "Звёзды", "Дата коммита", "Лицензия",
            "Что даёт", "Стоимость, дн", "Вердикт"]
    lines = ["# 1. Разведка: готовые решения", "", "## Кандидаты", "",
             "| " + " | ".join(head[:columns - 1] + [head[-1]]) + " |",
             "|" + "---|" * columns]
    for number, verdict in enumerate(rows, start=1):
        values = [f"кандидат-{number}", f"https://example.com/{number}", "10",
                  "2024-01-01", "MIT", "польза", "2"]
        cells = values[:columns - 1] + [verdict]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Итог", "", "Решение: go", ""]
    return "\n".join(lines)


def make_project(root: Path, plans: dict[str, str], *,
                 artifacts: dict[str, str] | None = None,
                 history: list[dict | str] | None = None) -> Path:
    """Проект-фикстура: `pipeline.json`, планы, артефакты разведки, история тиков."""
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "reviews").mkdir(parents=True)
    (root / "docs" / "research").mkdir(parents=True)
    (root / "pipeline.json").write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    for name, text in plans.items():
        (root / "docs" / "plans" / name).write_text(text, encoding="utf-8")
    for name, text in (artifacts or {}).items():
        (root / "docs" / "research" / name).write_text(text, encoding="utf-8")
    if history is not None:
        state = root / ".pipeline" / "state"
        state.mkdir(parents=True)
        lines = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                 for item in history]
        (state / "history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def metrics(root: Path) -> dict:
    """Метрики проекта с его настоящим `pipeline.json`."""
    return stats.cycle_metrics(root, proj.read_config(root))


def plan_sha(root: Path, name: str) -> str:
    """sha256 сырых байтов файла плана — как его считает R6."""
    return hashlib.sha256((root / "docs" / "plans" / name).read_bytes()).hexdigest()


def report_text(verdict: str, sha: str | None = None, extra: str = "") -> str:
    """Файл вердикта ревью плана; `plan_sha256:` — метка привязки к версии плана."""
    text = f"# 1. Ревью плана\n\n## Вердикт шага\n\n## ВЕРДИКТ: {verdict}\n"
    if sha is not None:
        text += f"\nplan_sha256: {sha}\n"
    return text + extra


def test_metrics_counts_plans_with_research(tmp_path: Path) -> None:
    """План без строки шага в журнале планом не считается (R1)."""
    root = make_project(
        tmp_path / "project",
        {
            "1-full.md": plan_text("1. Полный план", research=True),
            "2-research.md": plan_text("2. Только раздел", research=True),
            "3-plain.md": plan_text("3. Без разведки"),
            "notes.md": "# Заметки\n\nБез номера в имени — не план.\n",
            "00-Заметки.md": "# 0. Заметки\n\n## Шаги\n\n### S1. Шаг (1 дн)\n\n"
                             "## Журнал выполнения\n\n| Дата | Шаг | Коммиты | "
                             "Прогоны и доказательства | Статус |\n|---|---|---|---|---|\n",
        },
        artifacts={"1-prior-art.md": artifact_text(["берём"] * 5)},
    )

    data = metrics(root)

    assert data["plans_total"] == 3
    assert data["plans_with_research"] == 1
    assert data["research_gap"] == ["2"]
    assert data["errors"] == []


def test_metrics_counts_verdicts_without_solution(tmp_path: Path) -> None:
    """Вердикты читаются только из раздела «Кандидаты» таблицы артефакта (R4)."""
    first = artifact_text(["берём", "форкаем", "своё", "не берём: дорого", ""], columns=8)
    # Вторая таблица в том же файле: раздел «Что берём» вердикты не добавляет.
    first += ("\n## Что берём\n\n| Кандидат | Ссылка | Звёзды | Дата коммита | Лицензия "
              "| Что даёт | Стоимость, дн | Вердикт |\n|---|---|---|---|---|---|---|---|\n"
              "| лишний | https://example.com/x | 1 | 2024-01-01 | MIT | — | 1 | своё |\n")
    root = make_project(
        tmp_path / "project",
        {"1-eight.md": plan_text("1. Восемь колонок", research=True),
         "2-five.md": plan_text("2. Пять колонок", research=True)},
        artifacts={"1-prior-art.md": first,
                   "2-prior-art.md": artifact_text(
                       ["берём практики", "не берём: у нас своё"], columns=5)},
    )

    data = metrics(root)

    assert data["candidates_total"] == 7
    assert data["candidates_with_solution"] == 3
    assert data["candidates_own"] == 3
    assert data["candidates_unknown"] == 1


def test_metrics_counts_edits_per_plan(tmp_path: Path) -> None:
    """Отметка «Правки по разведке: N» без артефакта не считается (R5)."""
    root = make_project(
        tmp_path / "project",
        {"1-three.md": plan_text("1. Три правки", research=True, edits=3),
         "2-one.md": plan_text("2. Одна правка", research=True, edits=1),
         "3-none.md": plan_text("3. Без отметки", research=True),
         "4-mark.md": plan_text("4. Отметка без разведки", edits=7)},
        artifacts={"1-prior-art.md": artifact_text(["берём"]),
                   "2-prior-art.md": artifact_text(["своё"]),
                   "3-prior-art.md": artifact_text(["форкаем"])},
    )

    data = metrics(root)

    assert data["edits_total"] == 4
    assert data["edits_plans"] == 2
    assert data["edits_per_plan"] == 2.0
    assert [item["edits"] for item in data["by_plan"]] == [3, 1, None, None]


def test_metrics_counts_plan_review_first_time(tmp_path: Path) -> None:
    """Состояния ревью плана: fresh/stale/unbound/pending/missing/invalid/skipped (R6)."""
    plans = {f"{number}-plan.md": plan_text(f"{number}. План {number}")
             for number in range(1, 7)}
    plans["7-old.md"] = plan_text("7. План старого формата", constitution=False)
    root = make_project(
        tmp_path / "project",
        plans,
        history=[
            {"action": "create_plan_review", "plan": 1},
            {"action": "create_plan_review", "plan": 2},
            {"action": "create_step", "plan": 2},
            {"action": "create_plan_review", "plan": 2},
        ],
    )
    # Отчёты пишутся после планов: метка свежего вердикта — настоящий sha256 файла (R6).
    reviews = root / "docs" / "reviews"
    (reviews / "plan-1.md").write_text(
        report_text("APPROVE", sha=plan_sha(root, "1-plan.md")), encoding="utf-8")
    (reviews / "plan-2.md").write_text(
        report_text("APPROVE", sha=plan_sha(root, "2-plan.md")), encoding="utf-8")
    # 3 — вердикт привязан к чужой версии плана
    (reviews / "plan-3.md").write_text(
        report_text("APPROVE", sha="0" * 64), encoding="utf-8")
    # 4 — ждёт правок
    (reviews / "plan-4.md").write_text(
        report_text("REQUEST_CHANGES", sha="0" * 64), encoding="utf-8")
    # 5 — отчёта нет (missing)
    # 6 — два разных вердикта в одном отчёте: неоднозначно (invalid)
    (reviews / "plan-6.md").write_text(
        report_text("APPROVE", extra="\n## ВЕРДИКТ: REQUEST_CHANGES\n"), encoding="utf-8")

    data = metrics(root)

    assert data["plan_reviews"] == 2
    assert data["plan_reviews_first"] == 1
    assert data["plan_reviews_stale"] == 1
    assert data["plan_reviews_pending"] == 1
    assert data["plan_reviews_skipped"] == 1
    states = {item["plan"]: item["review"] for item in data["by_plan"]}
    assert states == {"1": "fresh", "2": "fresh", "3": "stale", "4": "pending",
                      "5": "missing", "6": "invalid", "7": "skipped"}
    rounds = {item["plan"]: item["rounds"] for item in data["by_plan"]}
    assert rounds["1"] == 1
    assert rounds["2"] == 2


def test_metrics_tiles_and_empty_project(tmp_path: Path) -> None:
    """Пустой каталог — нули без ошибок; плитки и строка отчёта собираются из чисел."""
    empty = str(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    data = metrics(Path(empty))

    assert data["plans_total"] == 0
    assert data["plans_with_research"] == 0
    assert data["candidates_total"] == 0
    assert data["edits_total"] == 0
    assert data["edits_per_plan"] is None
    assert data["by_plan"] == []
    assert data["errors"] == []
    tiles = stats.cycle_tiles(data)
    assert len(tiles) == 4
    assert all(tile["label"] and tile["value"] for tile in tiles)

    root = make_project(
        tmp_path / "project",
        {"1-full.md": plan_text("1. Полный план", research=True),
         "2-research.md": plan_text("2. Только раздел", research=True),
         "3-plain.md": plan_text("3. Без разведки")},
        artifacts={"1-prior-art.md": artifact_text(
            ["берём", "берём", "своё", "не берём: дорого", "своё"])},
    )
    data = metrics(root)
    tiles = stats.cycle_tiles(data)
    assert [tile["label"] for tile in tiles] == [
        "планов с разведкой", "вердиктов без готового решения",
        "правок по разведке на план", "ревью плана с первого раза"]
    line = stats.render_cycle(data)
    assert "\n" not in line
    assert line.startswith("Цикл:")
    assert "с разведкой 1" in line
    assert "из 3" in line
