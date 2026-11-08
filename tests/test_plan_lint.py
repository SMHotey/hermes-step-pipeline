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
from step_pipeline import plan_accept
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


# ── План действующего формата: раздел «Разведка» (§5a, шаг 5-S1). ──────────────

NEW_FORMAT = helpers.PLAN.replace(
    "# 1. Демо-план конвейера",
    "# 1. Демо-план конвейера\n\nКонституция: docs/CONSTITUTION.md", 1)
"""Тот же демо-план, но со строкой `Конституция:` в шапке — формат действует."""

EMPTY_RESEARCH = "## Разведка\n\nСекция без единого поля.\n"

FULL_RESEARCH = """## Разведка

Артефакт: `docs/research/6-prior-art.md`

| Кандидат | Ссылка | Лицензия | Вердикт |
|---|---|---|---|
| spec-kit | https://example.com | MIT | берём |

Решение: go — готовый каркас, своё не пишем.
"""


def test_research_missing_named_for_new_format_plan(project: Path):
    """Л18: план с `Конституция:` в шапке обязан иметь `## Разведка` — находка называет раздел."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(NEW_FORMAT, encoding="utf-8")

    problems = plan_lint.lint_project(project, "6").problems
    assert len(problems) == 1
    assert "Разведка" in problems[0].text
    assert problems[0].step == ""
    assert problems[0].line == 1  # строка заголовка плана — зафиксировано в §11.1


def test_legacy_plan_without_research_is_tolerated(project: Path):
    """Тот же план без строки `Конституция:` — legacy: линтер молчит, счётчики не растут."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(helpers.PLAN, encoding="utf-8")

    assert plan_lint.lint_project(project, "6").problems == []


def test_research_section_names_missing_fields(project: Path):
    """Л19: раздел без полей — находка на каждое отсутствующее поле, каждая называет поле."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(NEW_FORMAT.replace("## Шаги", EMPTY_RESEARCH + "\n## Шаги", 1),
                    encoding="utf-8")

    problems = plan_lint.lint_project(project, "6").problems
    texts = " | ".join(problem.text for problem in problems)
    assert problems
    for field in ("ссылки на артефакт", "кандидатов", "лицензий", "решения/вердикта"):
        assert field in texts

    path.write_text(NEW_FORMAT.replace("## Шаги", FULL_RESEARCH + "\n## Шаги", 1),
                    encoding="utf-8")
    assert plan_lint.lint_project(project, "6").problems == []


def test_draft_without_research_fails_acceptance(project: Path):
    """Утверждение (`--accept`) не пропускает черновик действующего формата без разведки."""
    folder = project / "docs" / "plans" / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    draft = folder / "7-demo.md"
    draft.write_text(NEW_FORMAT, encoding="utf-8")

    errors = plan_accept.check_draft(project, draft)
    assert errors
    assert "Разведка" in " ".join(errors)

    draft.write_text(helpers.PLAN, encoding="utf-8")
    assert plan_accept.check_draft(project, draft) == []


def test_repo_plans_lint_clean():
    """Планы 1-4 (legacy) и план 5 (с разделом) — без находок: линтер существующие не ломает."""
    result = plan_lint.lint_project(helpers.REPO)

    assert result.problems == []
    assert len(result.files) >= 5


def test_repo_plans_warn_only_legacy():
    """Планы 1-4 (legacy) и план 5 (действующего формата) — линтер существующие не ломает.

    `problems` пусто у всех; предупреждение Л20 — о каждом legacy-плане, у плана 5 его нет:
    конституционная строка есть, и предупреждать не о чем.
    """
    result = plan_lint.lint_project(helpers.REPO)

    assert result.problems == []
    assert len(result.files) >= 5
    assert len(result.warnings) == 4
    names = sorted(Path(warning.file).name for warning in result.warnings)
    assert names == ["1-stabilnost.md", "2-upravlenie.md", "3-panel.md", "4-planner.md"]
    assert all("Конституция" in warning.text for warning in result.warnings)


RESEARCH_STEP = "### S1. Разведка рынка (1 дн)"
"""Заголовок шага со словом «разведка» — ловушка наивного поиска раздела."""


def test_step_heading_is_not_research_section(project: Path):
    """Заголовок шага разделом не считается: у плана действующего формата одна Л18, не Л19."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(NEW_FORMAT.replace("### S1. Таблица счетов (1 дн)", RESEARCH_STEP, 1),
                    encoding="utf-8")

    problems = plan_lint.lint_project(project, "6").problems
    assert len(problems) == 1
    assert "нет раздела «Разведка»" in problems[0].text
    assert problems[0].step == ""


def test_legacy_plan_with_research_step_is_tolerated(project: Path):
    """Тот же шаг в legacy-плане (без `Конституция:`): ни Л18, ни Л19 — шаг остаётся шагом."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(helpers.PLAN.replace("### S1. Таблица счетов (1 дн)", RESEARCH_STEP, 1),
                    encoding="utf-8")

    assert plan_lint.lint_project(project, "6").problems == []


def test_legacy_plan_with_research_section_is_tolerated(project: Path):
    """Раздел `## Разведка` в legacy-плане линтер не проверяет: ни Л18, ни Л19 (§5a)."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(helpers.PLAN.replace("## Шаги", EMPTY_RESEARCH + "\n## Шаги", 1),
                    encoding="utf-8")

    assert plan_lint.lint_project(project, "6").problems == []


def test_research_section_after_steps_is_found(project: Path):
    """Раздел после раздела шагов — всё ещё раздел: пустой даёт четыре Л19."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(
        NEW_FORMAT.replace("## Журнал выполнения", EMPTY_RESEARCH + "\n## Журнал выполнения", 1),
        encoding="utf-8")

    texts = " | ".join(problem.text for problem in plan_lint.lint_project(project, "6").problems)
    for field in ("ссылки на артефакт", "кандидатов", "лицензий", "решения/вердикта"):
        assert field in texts


# ── Л20: строка `Конституция:` в шапке плана (§5a, шаг 5-S9). ─────────────────
#
# Л20 — предупреждение, а не проблема: legacy-планы 1-4 заведены до конституции, и
# утверждение из-за отсутствия строки не блокируется (решение D1 шага S1).

def test_legacy_plan_gets_constitution_warning(project: Path):
    """У legacy-плана находка на `# `-заголовке и только в канале предупреждений."""
    result = plan_lint.lint_project(project, "1")

    assert result.problems == []
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "Конституция" in warning.text
    assert warning.file == "docs/plans/1-demo.md"
    assert warning.line == 1                      # строка заголовка плана (1-based)
    assert warning.step == ""                     # находка уровня файла, не шага


def test_plan_with_constitution_line_has_no_warning(project: Path):
    """Строка в шапке закрывает Л20: предупреждать не о чем (Л18/Л19 — свой канал)."""
    path = project / "docs" / "plans" / "6-demo.md"
    path.write_text(NEW_FORMAT, encoding="utf-8")

    result = plan_lint.lint_project(project, "6")

    assert result.warnings == []


def test_constitution_warning_does_not_block_accept(project: Path):
    """Черновик без строки конституции утверждается: предупреждение не ошибка."""
    folder = project / "docs" / "plans" / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    draft = folder / "7-demo.md"
    draft.write_text(helpers.PLAN, encoding="utf-8")

    assert plan_accept.check_draft(project, draft) == []
    assert plan_lint.lint_project(project, str(draft)).warnings


def test_doctor_shows_constitution_warning(project: Path):
    """Доктор печатает Л20 строкой «!» и зелёный итог не переворачивает."""
    report = doctor.collect(project)

    assert any("Конституция" in warning for warning in report.warnings)
    assert not any("Конституция" in problem for problem in report.problems)
    lines = doctor.format_report(report).splitlines()
    assert any(line.lstrip().startswith("!") and "Конституция" in line for line in lines)
    # Итог и код возврата считают только problems: предупреждение установку не краснит.
    assert doctor.format_report(doctor.Report(warnings=list(report.warnings))).splitlines()[-1] \
        == "ИТОГ: OK"
