# -*- coding: utf-8 -*-
"""Метрики цикла плана (план 5, шаг S8): счётчики из файлов проекта.

Число в утверждении — значение, которое тест сам насчитал в фикстуре: ослаблять
проверки («ключ присутствует») нельзя, это и есть предмет шага.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


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

#: Хвост строки «Цикл: …» и подсказки пятой плитки, когда кругов и закрытых шагов нет (13-S5).
NO_ROUNDS_TAIL = ("круги по классу: блокирующих 0, значимых 0, стилистических 0, постановка 0, "
                  "неизвестно 0; медиана круга — мин; с первой попытки 0 из 0")

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


def report_text(verdict: str, sha: str | None = None, extra: str = "",
                constitution: bool = True) -> str:
    """Файл вердикта ревью плана; `plan_sha256:` — метка привязки к версии плана.

    Строка `Конституция: …` обязательна с шага 5-S9: без неё вердикт получает состояние
    `unreferenced` и свежим не считается. `constitution=False` — только тестам про это
    состояние.
    """
    text = "# 1. Ревью плана\n\n## Вердикт шага\n"
    if constitution:
        text += "\nКонституция: п.1, п.7 — нарушений нет\n"
    text += f"\n## ВЕРДИКТ: {verdict}\n"
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
    assert len(tiles) == 5
    assert all(tile["label"] and tile["value"] for tile in tiles)
    # Планов и кругов нет — считать круги на шаг не на что: значение плитки «—».
    assert tiles[4]["label"] == "кругов правок на шаг"
    assert tiles[4]["value"] == "—"

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
        "правок по разведке на план", "ревью плана с первого раза",
        "кругов правок на шаг"]
    # Планы есть, кругов правок нет — считать не на что: «—», а не «0».
    assert tiles[4]["value"] == "—"
    line = stats.render_cycle(data)
    assert "\n" not in line
    assert line.startswith("Цикл:")
    assert "с разведкой 1" in line
    assert "из 3" in line
    # Прежние части строки остаются на своих местах, хвост причин дописан в конец (13-S5).
    assert "кругов на шаг — (правок 0, пересборок 0, закрыто находок 0)" in line
    assert line.endswith("); " + NO_ROUNDS_TAIL)


def test_metrics_counts_unreferenced_review(tmp_path: Path) -> None:
    """Вердикт без строки конституции — `unreferenced`, свежим не считается (§5a, шаг S9)."""
    root = make_project(tmp_path / "project", {"1-plan.md": plan_text("1. План 1")})
    (root / "docs" / "reviews" / "plan-1.md").write_text(
        report_text("APPROVE", sha=plan_sha(root, "1-plan.md"), constitution=False),
        encoding="utf-8")

    data = metrics(root)

    assert data["plan_reviews_unreferenced"] == 1
    assert data["plan_reviews"] == 0


def registry_text(statuses: list[str]) -> str:
    """Реестр находок постановки: шапка `PROMPT_FINDINGS_HEADER`, строка на находку.

    Статус — последняя ячейка: так его читает и движок (`gate.parse_findings`), и
    статистика (R11). ID вида `F-N` обязателен только движку, здесь он держится ради
    правдоподобия фикстуры.
    """
    rows = ["# Реестр находок постановки", "",
            "| ID | Класс | Находка | Что сделать | Статус |",
            "|---|---|---|---|---|"]
    rows += [f"| F-{number} | замысел | находка {number} | что-то | {status} |"
             for number, status in enumerate(statuses, start=1)]
    return "\n".join(rows) + "\n"


def test_metrics_counts_fix_rounds_and_zero_close_share(tmp_path: Path) -> None:
    """Круги по паре «план + шаг» и находки из реестров постановок (R10, R11, §5.4)."""
    root = make_project(
        tmp_path / "project",
        {"1-plan.md": plan_text("1. План 1"), "2-plan.md": plan_text("2. План 2")},
        history=[
            {"action": "create_fix", "plan": 1, "step": "S1", "detail": "карточка правок: t1"},
            {"action": "create_fix", "plan": 1, "step": "S1", "detail": "карточка правок: t2"},
            {"action": "create_prompt_fix", "plan": 1, "step": "S1",
             "detail": "переписать постановку: t3"},
            {"action": "create_prompt_fix", "plan": 1, "step": "S2",
             "detail": "переписать постановку: t4"},
            {"action": "create_prompt_fix", "plan": 1, "step": "S3", "stale": True,
             "detail": "пересборка протухшей постановки: t5"},
            {"action": "create_fix", "plan": 2, "step": "S1", "detail": "карточка правок: t6"},
            # Повтор ревью и стоп — не круги правок: ни бюджет, ни «0 закрытых» не двигают.
            {"action": "create_rereview", "plan": 1, "step": "S1", "detail": "повторное ревью"},
            {"action": "stop", "plan": 1, "step": "S2",
             "detail": "правка постановки не закрыла ни одной находки (круг 2: F-2 — открыты)"},
        ],
    )
    reviews = root / "docs" / "reviews"
    (reviews / "prompt-1-S1-findings.md").write_text(
        registry_text(["закрыто", "закрыто", "закрыто", "открыто"]), encoding="utf-8")
    (reviews / "prompt-1-S2-findings.md").write_text(
        registry_text(["открыто", "не блокирует"]), encoding="utf-8")
    # Реестр без шапки таблицы — строка в errors, шаг «нет данных», а не «ноль закрытых».
    (reviews / "prompt-1-S3-findings.md").write_text("# Реестр\n\nбез таблицы\n", encoding="utf-8")

    data = metrics(root)

    assert data["fix_rounds"] == 3                 # 2 правки S1 плана 1 + 1 правка плана 2
    assert data["prompt_fix_rounds"] == 2          # переписывания S1 и S2, пересборка S3 — нет
    assert data["stale_rebuilds"] == 1
    assert data["steps_with_rounds"] == 3          # (1,S1), (1,S2), (2,S1)
    assert data["rounds_per_step"] == 1.7          # 5 кругов на 3 шага
    assert data["findings_total"] == 6
    assert data["findings_closed"] == 3
    assert data["closed_per_round"] == 0.6         # 3 закрытых на 5 кругов
    assert data["zero_close_steps"] == 1           # S2 плана 1: реестр есть, закрытых ноль
    assert data["zero_close_base"] == 2            # (1,S1) и (1,S2); у (2,S1) реестра нет
    assert data["zero_close_share"] == 0.5
    assert any("prompt-1-S3-findings.md" in line for line in data["errors"])

    plans = {item["plan"]: item for item in data["by_plan"]}
    assert plans["1"]["fix_rounds"] == 2           # только `create_fix`, как ключ §5.4
    assert plans["1"]["stale_rebuilds"] == 1
    assert plans["1"]["findings_closed"] == 3
    assert plans["2"]["fix_rounds"] == 1
    assert plans["2"]["stale_rebuilds"] == 0
    assert plans["2"]["findings_closed"] == 0
    assert "rereview" not in plans["1"]

    tiles = stats.cycle_tiles(data)
    assert tiles[4]["label"] == "кругов правок на шаг"
    assert tiles[4]["value"] == "1.7"
    # Прежний текст подсказки целиком на месте, хвост причин дописан через «; » (13-S5).
    assert tiles[4]["hint"].startswith(
        "правок 3, пересборок 1; закрыто находок 3 (по 0.6 за круг); "
        "ноль закрытых за круг у 1 из 2 шагов; круги по классу: ")
    assert tiles[4]["hint"] == (
        "правок 3, пересборок 1; закрыто находок 3 (по 0.6 за круг); ноль закрытых за круг "
        "у 1 из 2 шагов; круги по классу: блокирующих 0, значимых 0, стилистических 0, "
        "постановка 2, неизвестно 3; медиана круга — мин; с первой попытки 0 из 0")
    line = stats.render_cycle(data)
    assert line.startswith("Цикл:")
    # §5.7: числа про круги дописаны в конец строки, прежние части — на своих местах.
    assert "кругов на шаг 1.7 (правок 3, пересборок 1, закрыто находок 3)" in line
    assert line.endswith("); круги по классу: блокирующих 0, значимых 0, стилистических 0, "
                         "постановка 2, неизвестно 3; медиана круга — мин; с первой попытки "
                         "0 из 0")


def test_tiles_hint_reads_plan_review_stale_not_rebuilds(tmp_path: Path) -> None:
    """Подсказка четвёртой плитки считает вердикты ревью плана (R6), а не пересборки (R10).

    Числа различаются намеренно: `plan_reviews_stale` 1, `plan_reviews_unbound` 1,
    `stale_rebuilds` 5 — подмена класса переменной внутри `cycle_tiles` видна в подсказке,
    а при совпадающих числах прошла бы зелёной.
    """
    root = make_project(
        tmp_path / "project",
        {"1-stale.md": plan_text("1. План 1"), "2-unbound.md": plan_text("2. План 2")},
        history=[{"action": "create_prompt_fix", "plan": 1, "step": f"S{number}", "stale": True}
                 for number in range(1, 6)],
    )
    reviews = root / "docs" / "reviews"
    # 1 — вердикт привязан к чужой версии плана (stale), 2 — строки привязки нет (unbound).
    (reviews / "plan-1.md").write_text(report_text("APPROVE", sha="0" * 64), encoding="utf-8")
    (reviews / "plan-2.md").write_text(report_text("APPROVE"), encoding="utf-8")

    data = metrics(root)

    assert data["plan_reviews_stale"] == 1
    assert data["plan_reviews_unbound"] == 1
    assert data["plan_reviews_unreferenced"] == 0
    assert data["plan_reviews_first"] == 0
    assert data["stale_rebuilds"] == 5

    tiles = stats.cycle_tiles(data)
    assert tiles[3]["label"] == "ревью плана с первого раза"
    assert tiles[3]["hint"] == ("свежих вердиктов 0, непригодных 2 (не привязано к версии 2, "
                                "без ссылки на конституцию 0), ждут 0, без ревью 0")
    # Пересборки — число пятой плитки («кругов правок на шаг»), не подсказки ревью плана.
    assert tiles[4]["hint"].startswith("правок 0, пересборок 5; ")



def round_history() -> list[dict]:
    """Фикстура §4.6 постановки 13-S5: одна и та же история во всех тестах кругов.

    Обязательные поля — `ts`, `plan`, `step`, `action`; причина и флаг `stale` — по смыслу
    строки. `ts` идут не по возрастанию в конце списка нарочно: порядок файла — не гарантия.
    """
    return [
        {"ts": 1000.0, "plan": 1, "step": "S1", "action": "create_step"},
        {"ts": 1100.0, "plan": 1, "step": "S1", "action": "create_fix", "cause": "блокирующая"},
        {"ts": 1700.0, "plan": 1, "step": "S1", "action": "create_review"},
        {"ts": 1800.0, "plan": 1, "step": "S1", "action": "create_prompt_fix"},
        {"ts": 3000.0, "plan": 1, "step": "S1", "action": "create_prompt_review"},
        {"ts": 3100.0, "plan": 1, "step": "S1", "action": "close_step"},
        {"ts": 4000.0, "plan": 1, "step": "S2", "action": "create_fix"},
        {"ts": 4600.0, "plan": 1, "step": "S2", "action": "create_review"},
        {"ts": 5000.0, "plan": 1, "step": "S2", "action": "create_prompt_fix", "stale": True},
        {"ts": 6000.0, "plan": 1, "step": "S2", "action": "close_step"},
        {"ts": 7000.0, "plan": 1, "step": "S3", "action": "close_step"},
        {"ts": 8000.0, "plan": 1, "step": "S4", "action": "create_fix",
         "cause": "стилистическая"},
    ]


def test_metrics_counts_rounds_by_cause(tmp_path: Path) -> None:
    """Круги по классу и `неизвестно`: пять ключей, медиана 10 мин, один круг без ревью (R12)."""
    root = make_project(tmp_path / "project", {"1-plan.md": plan_text("1. План 1")},
                        history=round_history())

    data = metrics(root)

    assert data["round_causes"] == {"блокирующая": 1, "значимая": 0, "стилистическая": 1,
                                    "постановка": 1, "неизвестно": 1}
    assert data["round_minutes_median"] == 10      # значения 10, 20 и 10 минут
    assert data["rounds_open"] == 1                # круг S4 без закрывающего ревью
    steps = {f"{item['plan']}-{item['step']}": item for item in data["rounds_by_step"]}
    assert [f"{item['plan']}-{item['step']}" for item in data["rounds_by_step"]] == [
        "1-S1", "1-S2", "1-S4"]
    assert (steps["1-S1"]["rounds"], steps["1-S1"]["median_min"]) == (2, 15)
    assert steps["1-S1"]["causes"] == {"блокирующая": 1, "значимая": 0,
                                       "стилистическая": 0, "постановка": 1, "неизвестно": 0}
    # Пересборка протухшей постановки (`stale`) кругом не считается: у S2 круг один.
    assert (steps["1-S2"]["rounds"], steps["1-S2"]["median_min"]) == (1, 10)
    assert steps["1-S2"]["causes"]["неизвестно"] == 1
    assert (steps["1-S4"]["rounds"], steps["1-S4"]["median_min"]) == (1, None)
    assert steps["1-S4"]["causes"]["стилистическая"] == 1

    tail = ("круги по классу: блокирующих 1, значимых 0, стилистических 1, постановка 1, "
            "неизвестно 1; медиана круга 10 мин; с первой попытки 1 из 3")
    assert stats.cycle_tiles(data)[4]["hint"].endswith(tail)
    assert stats.render_cycle(data).endswith("; " + tail)


def test_metrics_counts_first_try_share(tmp_path: Path) -> None:
    """Доля шагов с первой попытки: закрыты S1, S2, S3, без кругов только S3 (R12)."""
    root = make_project(tmp_path / "project", {"1-plan.md": plan_text("1. План 1")},
                        history=round_history())

    data = metrics(root)

    assert (data["first_try_steps"], data["first_try_base"]) == (1, 3)
    assert data["first_try_share"] == 0.3
    # Шаг S3 закрыт без кругов: в разбивке на шаг его нет — считать там нечего.
    assert [item["step"] for item in data["rounds_by_step"]] == ["S1", "S2", "S4"]

    empty = tmp_path / "empty"
    empty.mkdir()
    none = metrics(Path(empty))
    assert (none["first_try_steps"], none["first_try_base"], none["first_try_share"]) == (
        0, 0, None)
    assert none["round_causes"] == {"блокирующая": 0, "значимая": 0, "стилистическая": 0,
                                    "постановка": 0, "неизвестно": 0}
    assert none["round_minutes_median"] is None
    assert none["rounds_by_step"] == []


def test_step_round_causes_helper_for_panel(tmp_path: Path) -> None:
    """Хелпер панели: шаг без учёта регистра, неизвестный шаг и пустая история — нули (R12)."""
    root = make_project(tmp_path / "project", {"1-plan.md": plan_text("1. План 1")},
                        history=round_history())
    cfg = proj.read_config(root)

    one = stats.step_round_causes(root, cfg, "1", "s1")
    assert (one["rounds"], one["median_min"], one["open"]) == (2, 15, 0)
    assert one["causes"] == {"блокирующая": 1, "значимая": 0, "стилистическая": 0,
                             "постановка": 1, "неизвестно": 0}
    assert stats.step_round_causes(root, cfg, "1", "S4")["median_min"] is None

    missing = stats.step_round_causes(root, cfg, "9", "S1")
    assert (missing["rounds"], missing["median_min"], missing["open"]) == (0, None, 0)
    assert set(missing["causes"]) == set(stats.ROUND_CAUSES)

    bare = tmp_path / "bare"
    bare.mkdir()
    empty = stats.step_round_causes(bare, None, "1", "S1")
    assert (empty["rounds"], empty["median_min"], empty["open"]) == (0, None, 0)
