# -*- coding: utf-8 -*-
"""Линтер плана и doctor --plan: проверки формата до запуска конвейера."""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from step_pipeline import plan_lint
from step_pipeline import doctor
import cli
import project as proj

BROKEN = """# 2. Кривой план

## Шаги

### S1. Шаг без критерия готовности (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""

JOURNAL_TAIL = """## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
"""


def _line(text: str, needle: str) -> int:
    """Номер строки (1-based) первой строки, содержащей подстроку."""
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    raise AssertionError(f"в тексте нет строки с {needle!r}")


def test_template_plan_is_clean():
    project = helpers.REPO
    path = project / "step_pipeline" / "templates" / "plan-template.md"
    assert plan_lint.lint_file(path, project, plan_lint.settings(project)) == []


def test_broken_plan_reports_missing_dod_and_journal_only_step(project: Path):
    path = project / "docs" / "plans" / "2-broken.md"
    path.write_text(BROKEN, encoding="utf-8")
    result = plan_lint.lint_project(project, "2")
    assert path in result.files
    assert len(result.problems) == 2
    assert result.problems[0].line == _line(BROKEN, "### S1. Шаг без критерия")
    assert "нет критерия" in result.problems[0].text
    assert "Готово" in result.problems[0].text
    assert result.problems[1].line == _line(BROKEN, "| — | S2 |")
    assert "S2" in result.problems[1].text
    assert "только в журнале" in result.problems[1].text
    assert all(p.file == "docs/plans/2-broken.md" for p in result.problems)
    assert str(result.problems[0]).startswith("docs/plans/2-broken.md:")


def test_file_name_and_broken_regex_are_reported(project: Path):
    (project / "docs" / "plans" / "plans.md").write_text(helpers.PLAN, encoding="utf-8")
    result = plan_lint.lint_project(project, "")
    assert len(result.problems) == 1
    assert result.problems[0].line == 0
    assert "номер-слаг" in result.problems[0].text

    cfg = proj.read_config(project)
    cfg["step_id_regex"] = "([a-z"
    proj.write_config(project, cfg)
    broken = plan_lint.lint_project(project, "")
    assert len(broken.problems) == 1
    assert broken.problems[0].line == 0
    assert "не компилируется" in broken.problems[0].text
    assert broken.files == []


def test_index_file_is_not_linted_in_catalog(project: Path):
    """Реестр `00-INDEX.md` — не план: при проверке каталога линтер его пропускает."""
    (project / "docs" / "plans" / "00-INDEX.md").write_text(
        "# 00. Реестр планов\n\n| План | Статус |\n|---|---|\n| 2 | в работе |\n",
        encoding="utf-8")
    (project / "docs" / "plans" / "2-ok.md").write_text(helpers.PLAN, encoding="utf-8")
    result = plan_lint.lint_project(project, "")
    names = [p.name for p in result.files]
    assert "00-INDEX.md" not in names and "2-ok.md" in names
    assert result.problems == []


def test_second_steps_section_is_reported_but_step_titles_are_not(project: Path):
    text = """# 1. Два раздела шагов

## Шаги

### S5. Проверка шага и журнала (0.5 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed
- Готово = проверка пройдена

## Шаги и проверки

### S6. Лишний раздел (0.5 дн)

- Файл: `app/y.py`
- Тесты/гейты: `python -m pytest tests/test_y.py -q` → 1 passed
- Готово = раздел описан

""" + JOURNAL_TAIL + "| — | S5 | — | — | открыт |\n"
    path = project / "docs" / "plans" / "3-sections.md"
    path.write_text(text, encoding="utf-8")
    result = plan_lint.lint_project(project, "3")
    assert len(result.problems) == 1
    assert result.problems[0].line == _line(text, "## Шаги и проверки")
    assert "должен быть один" in result.problems[0].text
    assert not any("нет строк шага" in p.text for p in result.problems)
    assert not any(p.line == _line(text, "### S5.") for p in result.problems)


def test_malformed_step_heading_is_reported(project: Path):
    text = """# 1. Кривой заголовок шага

## Шаги

### S1. Оформленный шаг (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed
- Готово = шаг готов

### S2 Название без точки (1 дн)

- Файл: `app/y.py`
- Тесты/гейты: `python -m pytest tests/test_y.py -q` → 1 passed
- Готово = шаг готов

""" + JOURNAL_TAIL + "| — | S1 | — | — | открыт |\n"
    path = project / "docs" / "plans" / "4-heading.md"
    path.write_text(text, encoding="utf-8")
    result = plan_lint.lint_project(project, "4")
    assert len(result.problems) == 1
    assert result.problems[0].line == _line(text, "### S2 ")
    assert "не по формату" in result.problems[0].text


def test_journal_columns_and_duplicate_steps_are_reported(project: Path):
    text = """# 1. Дубль шага

## Шаги

### S1. Первый шаг (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed
- Готово = шаг готов

### S1. Дубль шага (0.5 дн)

- Файл: `app/y.py`
- Тесты/гейты: `python -m pytest tests/test_y.py -q` → 1 passed
- Готово = дубль описан

""" + JOURNAL_TAIL + """| — | S1 | — | открыт |
| — | S1 | — | — | открыт |
"""
    path = project / "docs" / "plans" / "5-duplicate.md"
    path.write_text(text, encoding="utf-8")
    result = plan_lint.lint_project(project, "5")
    assert len(result.problems) == 2
    columns = [p for p in result.problems if "колонок" in p.text]
    duplicates = [p for p in result.problems if "повторяется в разделе шагов" in p.text]
    assert len(columns) == 1
    assert columns[0].line == _line(text, "| — | S1 | — | открыт |")
    assert len(duplicates) == 1
    assert duplicates[0].line == _line(text, "### S1. Дубль шага")


def test_step_requires_file_and_nonempty_gates(project: Path):
    text = """# 1. Шаг без файла и с пустыми гейтами

## Шаги

### S1. Шаг без файла (1 дн)

- Тесты/гейты: —
- Готово = шаг описан

""" + JOURNAL_TAIL + "| — | S1 | — | — | открыт |\n"
    path = project / "docs" / "plans" / "6-empty.md"
    path.write_text(text, encoding="utf-8")
    result = plan_lint.lint_project(project, "6")
    assert len(result.problems) == 2
    head = _line(text, "### S1. Шаг без файла")
    assert all(p.line == head for p in result.problems)
    texts = " | ".join(p.text for p in result.problems)
    assert "нет пункта" in texts
    assert "Файл" in texts
    assert "гейты пустые" in texts


def test_problem_carries_step_for_panel_navigation(project: Path):
    """Находка знает свой шаг: по нему панель даёт переход к шагу (шаг 3-S5)."""
    (project / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")
    broken = plan_lint.lint_project(project, "2").problems
    assert sorted(problem.step for problem in broken) == ["S1", "S2"]

    # Строка журнала без колонок тоже знает шаг: он лежит во второй колонке.
    text = """# 3. Короткий журнал

## Шаги

### S1. Шаг с нормальным описанием (0.5 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed
- Готово = шаг описан

""" + JOURNAL_TAIL + "| — | S1 | — | открыт |\n"
    (project / "docs" / "plans" / "3-short.md").write_text(text, encoding="utf-8")
    short = plan_lint.lint_project(project, "3").problems
    columns = [problem for problem in short if "колонок" in problem.text]
    assert len(columns) == 1 and columns[0].step == "S1"

    # Находки уровня файла шага не знают: переходить по ним некуда.
    (project / "docs" / "plans" / "plans.md").write_text(helpers.PLAN, encoding="utf-8")
    file_level = [problem for problem in plan_lint.lint_project(project, "").problems
                  if "номер-слаг" in problem.text]
    assert len(file_level) == 1 and file_level[0].step == ""


def test_doctor_plan_flag_reports_lint_problems_and_exits_1(project: Path,
                                                           monkeypatch, capsys,
                                                           tmp_path: Path):
    monkeypatch.setattr(cli.proj, "run_hermes", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="План: страж шага (конвейер)", stderr=""))
    monkeypatch.setattr(cli.proj, "profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(cli.snap, "snapshot", lambda *a, **k: {})
    db = tmp_path / "kanban.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    (project / ".pipeline" / "cron.json").write_text(
        '{"driver": "demo", "name": "План: страж шага (конвейер)"}', encoding="utf-8")
    (project / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")

    code = cli.cmd_doctor(argparse.Namespace(project=str(project), plan="2"))
    out = capsys.readouterr().out
    assert code == 1
    assert "ИТОГ: ЕСТЬ ПРОБЛЕМЫ" in out
    assert f"docs/plans/2-broken.md:{_line(BROKEN, '### S1. Шаг без критерия')}" in out
    assert f"docs/plans/2-broken.md:{_line(BROKEN, '| — | S2 |')}" in out

    code_all = cli.cmd_doctor(argparse.Namespace(project=str(project), plan=""))
    out_all = capsys.readouterr().out
    assert code_all == 1
    assert sum(1 for line in out_all.splitlines() if "✗ план" in line) == 2


def test_setup_tail_warns_about_plan_problems(tmp_path: Path, monkeypatch, capsys):
    import setup_flow

    monkeypatch.setattr(setup_flow.proj, "run_hermes", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    target = tmp_path / "shop"
    (target / "docs" / "plans").mkdir(parents=True)
    (target / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")
    code = setup_flow.run_setup(argparse.Namespace(
        project=str(target), board="shop", driver="shop", prompter="prompter",
        reviewer="reviewer", telegram_token="", telegram_chat="", no_telegram=True,
        no_test=True, no_profiles=True, no_cron=True, yes=True, force=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "линтер планов: проблем 2" in out
    assert "нет критерия «Готово =»" in out
    assert "только в журнале" in out


# ── doctor как источник данных отчёта. Перенесено из tests/test_doctor.py по ревью
# 3-S5: файл вне белого списка §3 постановки, тесты же ложатся в разрешённый файл. ──

BROKEN_DOCTOR = """# 2. Кривой план

## Шаги

### S1. Шаг без критерия готовности (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


@pytest.fixture()
def clean_env(project: Path, monkeypatch, tmp_path: Path) -> Path:
    """Проект с подменёнными внешними источниками: cron, профили, снимок, борда."""
    monkeypatch.setattr(cli.proj, "run_hermes", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="План: страж шага (конвейер)", stderr=""))
    monkeypatch.setattr(cli.proj, "profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(cli.snap, "snapshot", lambda *a, **k: {})
    db = tmp_path / "kanban.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    (project / ".pipeline" / "cron.json").write_text(
        '{"driver": "demo", "name": "План: страж шага (конвейер)"}', encoding="utf-8")
    return project


def test_collect_prints_nothing_and_keeps_report_data(clean_env: Path, capsys):
    """Сбор отчёта — не печать: панель получает те же данные, терминал молчит."""
    (clean_env / "docs" / "plans" / "2-broken.md").write_text(BROKEN_DOCTOR, encoding="utf-8")
    report = doctor.collect(clean_env, "2")
    out = capsys.readouterr().out
    assert out == ""
    assert report.lint is not None and len(report.lint.problems) == 1
    assert any("нет критерия «Готово =»" in problem for problem in report.problems)
    assert any("решение стража" in note for note in report.notes)


def test_cli_doctor_prints_exactly_the_collected_report(clean_env: Path, capsys,
                                                        monkeypatch):
    """CLI печатает `format_report(collect(...))`: панель и терминал не расходятся."""
    (clean_env / "docs" / "plans" / "2-broken.md").write_text(BROKEN_DOCTOR, encoding="utf-8")
    monkeypatch.setattr(cli, "doctor", doctor)
    expected = doctor.format_report(doctor.collect(clean_env, "2"))

    code = cli.cmd_doctor(argparse.Namespace(project=str(clean_env), plan="2"))
    out = capsys.readouterr().out
    assert code == 1
    assert out.rstrip("\n") == expected
    assert "ИТОГ: ЕСТЬ ПРОБЛЕМЫ" in out
    assert f"docs/plans/2-broken.md:{_line(BROKEN_DOCTOR, '### S1. Шаг без критерия')}" in out
    assert "  · проект " in out and "  ✗ план " in out


def test_without_lint_the_report_has_no_plan_problems(clean_env: Path):
    """Панель зовёт линтер отдельно: doctor с `with_lint=False` находок формата не даёт."""
    (clean_env / "docs" / "plans" / "2-broken.md").write_text(BROKEN_DOCTOR, encoding="utf-8")
    with_lint = doctor.collect(clean_env, "")
    without = doctor.collect(clean_env, "", with_lint=False)
    assert without.lint is None
    assert not any(problem.startswith("план ") for problem in without.problems)
    assert any(problem.startswith("план ") for problem in with_lint.problems)
    # Остальная диагностика на месте: линтер — только один из источников.
    assert without.notes and "файлов планов: 2" in " ".join(without.notes)


def test_preview_reports_missing_engine_and_writes_nothing(tmp_path: Path, monkeypatch):
    """Предпросмотр: проверки читают проект, но ничего в нём не создают."""
    monkeypatch.setattr(cli.proj, "run_hermes", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(cli.proj, "profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(cli.snap, "snapshot", lambda *a, **k: {})
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(tmp_path / "empty.db"))
    root = tmp_path / "bare"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "7-real.md").write_text(BROKEN_DOCTOR.replace("# 2.", "# 7."),
                                                       encoding="utf-8")

    report = doctor.collect(root, "", preview=True)
    text = " | ".join(report.problems)
    assert "конвейер не установлен" in text
    assert not (root / ".pipeline").exists()
    assert not (root / "pipeline.json").exists()
