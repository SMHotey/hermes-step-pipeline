# -*- coding: utf-8 -*-
"""Doctor без печати: CLI и панель проверок собирают один и тот же отчёт (шаг 3-S5)."""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from step_pipeline import doctor
import cli

BROKEN = """# 2. Кривой план

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


def _line(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    raise AssertionError(f"в тексте нет строки с {needle!r}")


def test_collect_prints_nothing_and_keeps_report_data(clean_env: Path, capsys):
    """Сбор отчёта — не печать: панель получает те же данные, терминал молчит."""
    (clean_env / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")
    report = doctor.collect(clean_env, "2")
    out = capsys.readouterr().out
    assert out == ""
    assert report.lint is not None and len(report.lint.problems) == 1
    assert any("нет критерия «Готово =»" in problem for problem in report.problems)
    assert any("решение стража" in note for note in report.notes)


def test_cli_doctor_prints_exactly_the_collected_report(clean_env: Path, capsys,
                                                        monkeypatch):
    """CLI печатает `format_report(collect(...))`: панель и терминал не расходятся."""
    (clean_env / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")
    monkeypatch.setattr(cli, "doctor", doctor)
    expected = doctor.format_report(doctor.collect(clean_env, "2"))

    code = cli.cmd_doctor(argparse.Namespace(project=str(clean_env), plan="2"))
    out = capsys.readouterr().out
    assert code == 1
    assert out.rstrip("\n") == expected
    assert "ИТОГ: ЕСТЬ ПРОБЛЕМЫ" in out
    assert f"docs/plans/2-broken.md:{_line(BROKEN, '### S1. Шаг без критерия')}" in out
    assert "  · проект " in out and "  ✗ план " in out


def test_without_lint_the_report_has_no_plan_problems(clean_env: Path):
    """Панель зовёт линтер отдельно: doctor с `with_lint=False` находок формата не даёт."""
    (clean_env / "docs" / "plans" / "2-broken.md").write_text(BROKEN, encoding="utf-8")
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
    (root / "docs" / "plans" / "7-real.md").write_text(BROKEN.replace("# 2.", "# 7."),
                                                       encoding="utf-8")

    report = doctor.collect(root, "", preview=True)
    text = " | ".join(report.problems)
    assert "конвейер не установлен" in text
    assert not (root / ".pipeline").exists()
    assert not (root / "pipeline.json").exists()
