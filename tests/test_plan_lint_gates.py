# -*- coding: utf-8 -*-
"""A6-A8: пред-гейт гейтов плана — команда, которая на дереве не выполнится (шаг 7-S5).

Девять тестов, по одному на случай плана (строки 202-204): каталог вывода создан и не
создан, файл в дереве, новый файл в «Файл:», нет каталога для команды, битый `cd`,
команда без путей, юникодный путь, шаг без блока «Гейты» и деградация без модуля.

Проверяется пара «код + путь плана» (`gate_codes`), а не подстроки `! A6`: у проекта из
`install_project` предупреждение про cron есть всегда, а строка деградации сама
начинается с `A6–A8`. Плагин и алиасы модулей поднимает `import helpers`, CLI зовётся в
процессе — как в `tests/test_plan_check_cycle.py`.
"""
from __future__ import annotations

import argparse
import re

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
import cli
from step_pipeline import doctor

PLAN_HEAD = """# 2. Демо-план гейтов

Конституция: docs/CONSTITUTION.md

## Разведка

Артефакт: `docs/research/2-prior-art.md`. Лицензии: MIT. Кандидаты: demo/spec-kit.
Решение: go — берём практики demo/spec-kit.
Правки по разведке: 1

## Шаги

### S1. Шаг с гейтом (1 дн)

{step}

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

#: Группа захвата обязательна: `findall` без неё отдаёт полное совпадение `A6 docs/plans/`.
GATE_CODE_RE = re.compile(r"\b(A[678]) docs/plans/")


def gate_codes(text: str) -> list[str]:
    """Коды A6-A8 в выводе или в warnings: только строки находок."""
    return GATE_CODE_RE.findall(text)


def step(gate: str, files: str = "`tests/test_x.py`",
         ready: str = "гейт отработал") -> str:
    """Тестовый шаг: три обязательных пункта, команда гейта — в обратных кавычках."""
    return "\n".join([f"- Файл: {files}", f"- Тесты/гейты: `{gate}`", f"- Готово = {ready}"])


def write_draft(project, step_text: str, plan: str = "2"):
    """Черновик плана в `docs/plans/drafts/<plan>-demo.md` текстом `PLAN_HEAD.format(step=…)`.

    Помощник свой, а не из `tests/test_plan_check_cycle.py`: тот пишет `helpers.PLAN` и
    аргумента `step` не имеет.
    """
    folder = project / "docs" / "plans" / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{plan}-demo.md"
    path.write_text(PLAN_HEAD.format(step=step_text), encoding="utf-8")
    return path


def plan_check(project, target: str = "2") -> int:
    """Вызов CLI в процессе: код возврата и печать — как у `hermes pipeline plan --check`."""
    return cli.cmd_plan(argparse.Namespace(project=str(project), check=target))


@pytest.fixture()
def quiet_snapshot(monkeypatch):
    """Снимок движка не поднимается: `collect` берёт пустой словарь, как в test_plan_lint."""
    monkeypatch.setattr(doctor.snap, "snapshot", lambda *a, **k: {})


def test_output_dir_exists_silent(project, capsys, quiet_snapshot):
    """Каталог вывода создан в проекте — находок нет, предупреждений в ИТОГе тоже."""
    (project / "logs").mkdir()
    draft = write_draft(project, step("python -m pytest -q > logs/pytest.log"))

    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert "ИТОГ: ошибок нет" in out
    assert ", предупреждений" not in out
    assert gate_codes(out) == []

    warning_lines = doctor.collect(project, str(draft)).warnings
    assert gate_codes("\n".join(warning_lines)) == []
    assert warning_lines                     # cron не заводился — предупреждение есть всегда


def test_output_dir_missing_found(project, capsys, quiet_snapshot):
    """A6: каталога нет и шаг его не создаёт; `mkdir` в теле шага находку снимает."""
    command = "python -m pytest -q > logs/pytest.log"
    write_draft(project, step(command))

    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A6"]
    assert "каталог вывода не создан: `logs/`" in out

    write_draft(project, step(command) + "\n- Работа: mkdir -p logs")
    plan_check(project)
    assert gate_codes(capsys.readouterr().out) == []

    write_draft(project, step(command) + "\n- Работа: mkdir -p logs2")
    plan_check(project)
    assert gate_codes(capsys.readouterr().out) == ["A6"]

    write_draft(project, step(command) + "\n- Работа: mkdir -p logs/sub")
    plan_check(project)
    assert gate_codes(capsys.readouterr().out) == []

    write_draft(project, step("python -m pytest -q 1>logs/pytest.log"))
    plan_check(project)
    assert gate_codes(capsys.readouterr().out) == ["A6"]


def test_input_file_exists_silent(project, capsys, quiet_snapshot):
    """Путь гейта есть в дереве — молчание."""
    (project / "tests").mkdir()
    (project / "tests" / "test_x.py").write_text("", encoding="utf-8")
    write_draft(project, step("python -m pytest tests/test_x.py -q"))

    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []


def test_declared_new_file_silent(project, capsys, quiet_snapshot):
    """A7 молчит на пути, объявленном в «Файл:»: файл создаёт сам шаг."""
    write_draft(project, step("python -m pytest app/new.py -q",
                              files="`tests/test_x.py`, `app/new.py`"))

    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []


def test_command_dir_missing_found(project, capsys, quiet_snapshot):
    """A7: каталога команды нет; после объявления пути в «Файл:» — молчание (D6)."""
    write_draft(project, step("python -m pytest build/tests -q"))
    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A7"]
    assert "путь не существует и не объявлен: `build/tests`" in out

    write_draft(project, step("python -m pytest build/tests/ -q",
                              files="`tests/test_x.py`, `build/tests/test_x.py`"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []


def test_broken_cd_found(project, capsys, quiet_snapshot):
    """A8: `cd sub` при отсутствующем каталоге; объявленный каталог молчит (D6)."""
    write_draft(project, step("cd sub && python -m pytest -q"))
    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A8"]
    assert "cd в несуществующий каталог: `sub`" in out

    write_draft(project, step("cd tests && python -m pytest -q"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []


def test_command_without_paths_silent(project, capsys, quiet_snapshot):
    """В первом прогоне путей нет, во втором `2>/dev/null` абсолютен — молчание (D4, D11)."""
    write_draft(project, step("python -m pytest -q"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []

    write_draft(project, step("python -m pytest -q 2>/dev/null"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []


def test_unicode_path_intact(project, capsys, quiet_snapshot):
    """Юникодный путь печатается целиком и остаётся предупреждением, а не ошибкой."""
    write_draft(project, step("python -m pytest docs/research/2-разведка-ё-prior-art.md -q"))

    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A7"]
    assert ("путь не существует и не объявлен: "
            "`docs/research/2-разведка-ё-prior-art.md`") in out


def test_no_gates_block_degrades(project, capsys, monkeypatch, quiet_snapshot):
    """Шаг без блока «Гейты» и отсутствие модуля analyze — строки, а не трейсбек."""
    two_bullets = "\n".join(["- Файл: `tests/test_x.py`", "- Готово = гейт отработал"])
    draft = write_draft(project, two_bullets)

    plan_check(project)
    out = capsys.readouterr().out
    assert "шаг S1: нет пункта «Гейты:»" in out
    assert gate_codes(out) == []

    write_draft(project, step("python -m pytest -q"))
    monkeypatch.setattr(doctor, "analyze", None)
    phrase = doctor.ANALYZE_UNAVAILABLE
    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert phrase in out
    assert gate_codes(out) == []

    assert phrase in doctor.collect(project, str(draft)).warnings
