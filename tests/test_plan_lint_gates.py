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


def step_raw(gates: str, files: str = "`tests/test_x.py`",
             ready: str = "гейт отработал", work: str = "") -> str:
    """Шаг, у которого пункт гейтов задан как есть: несколько команд и флагов в строке.

    `step` заворачивает команду в обратные кавычки целиком — для строки с несколькими
    фрагментами это дало бы один «команду»-франкенштейн, поэтому такие случаи пишутся
    напрямую (D12: правила смотрят на каждый фрагмент отдельно).
    """
    lines = [f"- Файл: {files}", f"- Тесты/гейты: {gates}", f"- Готово = {ready}"]
    if work:
        lines.insert(2, f"- Работа: {work}")
    return "\n".join(lines)


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


def test_duplicate_path_reported_once(project, capsys, quiet_snapshot):
    """Дедуп (D12, п.2): один путь дважды на строке шага — одна находка, не две.

    Живой случай плана 34: `dist/index.html` встречался в строке четыре раза, и каждая
    копия была отдельной строкой A7 — предупреждений становилось вчетверо больше правды.
    """
    gates = ("`grep -o 'data-section=' dist/index.html | wc -l` → 6; "
             "`grep -o 'data-reveal=' dist/index.html | wc -l` → 6")
    write_draft(project, step_raw(gates))

    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A7"]
    assert out.count("путь не существует и не объявлен: `dist/index.html`") == 1


def test_non_path_tokens_silent(project, capsys, quiet_snapshot):
    """Токен-не-путь (D12, п.1): флаг, `--ключ=значение`, подстановка, адрес пакета.

    Флаг сам по себе путём не является, значение флага с `=` — не файл дерева,
    `${HOME}/dist` — подстановка, `@scope/pkg@ver` — спецификация npm.
    """
    gates = ("`node s.mjs --screenshot=docs/shots/a.png`; `${HOME}/dist`; "
             "`@scope/pkg@1.2.3`; `python -m pytest --no-color -q`")
    write_draft(project, step_raw(gates))

    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []


def test_cd_sets_step_directory(project, capsys, quiet_snapshot):
    """Рабочий каталог шага (D12, п.3): `cd landing` переносится на следующую команду.

    План 34 пишет `node scripts/build.mjs` там, где скрипт лежит в `landing/scripts/`, и
    помечает «там же, из `landing`». От корня проекта такого файла нет.
    """
    (project / "landing" / "scripts").mkdir(parents=True)
    (project / "landing" / "scripts" / "build.mjs").write_text("", encoding="utf-8")
    gates = "`cd landing && node scripts/build.mjs` → 0 ошибок"

    # объявлен как `landing/scripts/...` — молчание
    write_draft(project, step_raw(gates, files="`landing/scripts/build.mjs`"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []

    # без объявления путь находится только прочтением от каталога шага
    write_draft(project, step_raw(gates))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []

    # а без `cd` тот же путь не находится ни в одном прочтении — A7
    write_draft(project, step_raw("`node scripts/build.mjs` → 0 ошибок"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == ["A7"]


def test_ready_mention_declares_path(project, capsys, quiet_snapshot):
    """Объявления шире «Файл:» (D12, п.5): «Готово» объявляет путь наравне с «Файл:».

    План 34 проверяет сборку `dist/index.html` гейтом, а объявляет её в «Готово =
    страница собирается в dist/index.html»: файла до шага нет, и A7 тут ложен.
    """
    gates = "`cd landing && grep -o data-section dist/index.html | wc -l` → 6"
    ready = "страница собирается в dist/index.html"
    (project / "landing").mkdir()

    write_draft(project, step_raw(gates, ready=ready))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []

    # без упоминания в «Готово» тот же гейт — A7
    write_draft(project, step_raw(gates))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == ["A7"]


def test_glob_declaration_covers_prefix(project, capsys, quiet_snapshot):
    """Глоб-хвост объявляет свой каталог (D12, п.5): `docs/x/**` покрывает `../docs/x`."""
    (project / "landing").mkdir()
    gates = "`cd landing && node s.mjs --dir ../docs/x` → 0 расхождений"

    write_draft(project, step_raw(gates, files="`docs/x/**`"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []

    write_draft(project, step_raw(gates, files="`tests/test_x.py`"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == ["A7"]


def test_work_mention_declares_path(project, capsys, quiet_snapshot):
    """Путь, объявленный только в «Работа», находкой не становится (D12, п.5)."""
    (project / "tests").mkdir()
    gates = "`python -m pytest build/tests -q` → 3 passed"

    write_draft(project, step_raw(gates, work="тесты переезжают в build/tests"))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == []

    write_draft(project, step_raw(gates))
    assert plan_check(project) == 0
    assert gate_codes(capsys.readouterr().out) == ["A7"]


def test_real_missing_test_still_found(project, capsys, quiet_snapshot):
    """Правила не глушат настоящую находку: отсутствующего теста ровно одна A7."""
    (project / "tests").mkdir()
    write_draft(project, step_raw("`python -m pytest tests/нет_такого.py -q` → 3 passed"))

    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A7"]
    assert "путь не существует и не объявлен: `tests/нет_такого.py`" in out


def test_broken_cd_still_single_finding(project, capsys, quiet_snapshot):
    """`cd` в несуществующий каталог — по-прежнему одна A8 (D12, п.3)."""
    write_draft(project, step_raw("`cd нет_такого && ls` → 1 файл"))

    assert plan_check(project) == 0
    out = capsys.readouterr().out
    assert gate_codes(out) == ["A8"]
    assert "cd в несуществующий каталог: `нет_такого`" in out


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
