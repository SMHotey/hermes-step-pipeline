# -*- coding: utf-8 -*-
"""Разбор пункта «Прогон»: прогон шага как данные (§5 формата плана, шаг 9-S1).

Шесть тестов: три вида ожидания (`exit`, `содержит`, `regex`), хвосты `таймаут`/`slow`,
пустой и битый пункт (прогоном не считаются, разбор шага не падает) и то, что пункт
не попадает в гейты, не съедает цель шага и доезжает до машинного пакета `--json`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config as config_mod
import next_step
from engine_kit import make_project


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Проект-приёмник как после `hermes pipeline setup` (движок, конфиг, план 1)."""
    return make_project(tmp_path / "proj")


BASE = [
    "- Файл: `app/x.py`",
    "- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed",
    "- Готово = прогон шага прочитан как данные",
]


def plan_text(body: list[str]) -> str:
    """План 2 из одного шага: шапка, раздел шагов и журнал — иначе шага движок не увидит."""
    lines = ["# 2. Прогоны шага", "",
             "Цель: прогон шага читается как данные, а не как проза.", "",
             "## Шаги", "", "### S1. Проверка прогонами (1 дн)", "", *body, "",
             "## Журнал выполнения", "",
             "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
             "|---|---|---|---|---|",
             "| — | S1 | — | — | открыт |", ""]
    return "\n".join(lines) + "\n"


def write_plan(project: Path, body: list[str]) -> Path:
    path = Path(project) / "docs" / "plans" / "2-runs.md"
    path.write_text(plan_text(body), encoding="utf-8")
    return path


def steps_of(project: Path, body: list[str]) -> list[next_step.Step]:
    cfg = config_mod.load(project)
    return next_step.plan_steps(cfg, write_plan(project, body))


def test_exit_expectation(project: Path):
    """`→ exit 0`: вид ожидания `exit`, значение — код возврата строкой."""
    step = steps_of(project, [*BASE, "- Прогон: `python -m pytest -q` → exit 0"])[0]

    assert len(step.runs) == 1
    run = step.runs[0]
    assert (run.command, run.expect, run.value) == ("python -m pytest -q", "exit", "0")


def test_contains_expectation(project: Path):
    """`→ содержит "passed"`: подстрока без кавычек, вид ожидания `contains`."""
    step = steps_of(project,
                    [*BASE, '- Прогон: `python -m pytest` → содержит "passed"'])[0]

    assert [(run.expect, run.value) for run in step.runs] == [("contains", "passed")]


def test_regex_expectation(project: Path):
    """`→ regex "\\d+ passed"`: выражение сохраняется дословно, без кавычек."""
    step = steps_of(project,
                    [*BASE, '- Прогон: `python -m pytest` → regex "\\d+ passed"'])[0]

    assert [(run.expect, run.value) for run in step.runs] == [("regex", "\\d+ passed")]


def test_tails_and_defaults(project: Path):
    """Хвосты `таймаут 900` и `slow` читаются; без хвостов — умолчания None и False."""
    step = steps_of(project, [*BASE,
                              "- Прогон: `python -m pytest` → exit 0; таймаут 900; slow",
                              "- Прогон: `python -m pytest -q` → exit 0"])[0]

    with_tails, plain = step.runs
    assert (with_tails.timeout, with_tails.slow) == (900, True)
    assert (plain.timeout, plain.slow) == (None, False)


def test_empty_and_broken_bullet_are_not_runs(project: Path):
    """Пустой пункт и команда без кавычек прогоном не считаются; шаг разобран целиком."""
    step = steps_of(project, [*BASE, "- Прогон:", "- Прогон: pytest без кавычек"])[0]

    assert step.runs == []
    assert step.id == "S1"
    assert step.dod == "прогон шага прочитан как данные"


def test_runs_in_contract_and_goal_kept(project: Path):
    """Пункт не гейт и не цель: `runs` в пакете, строка «Прогон» в `gates` не попадает."""
    cfg = config_mod.load(project)
    path = write_plan(project, [
        "- Прогоны: сначала точечные, полный — один раз перед коммитом",
        "- Прогон: `python -m pytest tests/test_x.py -q` → exit 0",
        *BASE,
    ])
    step = next_step.plan_steps(cfg, path)[0]

    assert not step.goal.startswith("сначала точечные"), step.goal
    assert step.goal == "Проверка прогонами"
    assert not any("Прогон" in gate for gate in step.gates), step.gates

    pkg = next_step.build_package(cfg, "2")
    runs = pkg["next"]["runs"]
    assert len(runs) == 1
    assert set(runs[0]) == {"command", "expect", "value", "timeout", "slow"}
    assert runs[0]["command"] == "python -m pytest tests/test_x.py -q"
    assert runs[0]["expect"] == "exit"
