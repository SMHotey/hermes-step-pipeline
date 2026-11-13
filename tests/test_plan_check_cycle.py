# -*- coding: utf-8 -*-
"""Автор плана видит требования стража P1-P4: `plan --check` и `doctor --plan` (S0 плана 7)."""
from __future__ import annotations

import argparse

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
import cli
from step_pipeline import doctor  # алиаса `doctor` в tests/helpers.py:26-27 нет

RESEARCH = """## Разведка

Артефакт: `docs/research/{plan}-prior-art.md`. Лицензии: MIT. Кандидаты: demo/spec-kit.
Решение: go — берём практики demo/spec-kit.

{mark}"""


def write_draft(project, plan="2", *, mark="Правки по разведке: 1\n", artifact=None,
                research=True):
    """Черновик в `docs/plans/drafts`, имя из аргумента plan: helpers.PLAN плюс (по умолчанию)
    раздел «Разведка» перед шагами; `research=False` — ровно helpers.PLAN, без вставки."""
    folder = project / "docs" / "plans" / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    text = helpers.PLAN
    if research:
        block = RESEARCH.format(plan=plan, mark=mark)
        if artifact:  # юникод-ссылка для теста 6
            block = block.replace(f"docs/research/{plan}-prior-art.md", artifact)
        text = text.replace("## Шаги", block + "\n## Шаги", 1)
    path = folder / f"{plan}-demo.md"
    path.write_text(text, encoding="utf-8")
    return path


def plan_check(project, target):
    """Вызов CLI в процессе: код возврата и печать — как у `hermes pipeline plan --check`."""
    return cli.cmd_plan(argparse.Namespace(project=str(project), check=target))


@pytest.fixture()
def quiet_snapshot(monkeypatch):
    """Снимок движка не поднимается: `collect` берёт пустой словарь, как в test_plan_lint."""
    monkeypatch.setattr(doctor.snap, "snapshot", lambda *a, **k: {})


def test_plan_check_shows_p3_fail(project, capsys, quiet_snapshot):
    """Проваленное требование P3 видно и в `plan --check`, и в `doctor --plan`."""
    draft = write_draft(project, mark="")

    plan_check(project, "2")
    out = capsys.readouterr().out
    assert "Требования стража (P1–P4):" in out
    assert ("✗ P3: нет отметки «правок по разведке: N» — "
            "правка по разведке не оформлена") in out

    report = doctor.collect(project, str(draft))
    assert any(problem.startswith("план P3: нет отметки") for problem in report.problems)
    assert "✗ план P3:" in doctor.format_report(report)


def test_plan_check_exit_code_follows_p_fail(project, capsys):
    """Код возврата `plan --check` следует за требованиями стража, а не только за линтером."""
    write_draft(project, mark="")
    assert plan_check(project, "2") == 1
    capsys.readouterr()

    write_draft(project)
    assert plan_check(project, "2") == 0
    assert "ИТОГ: ошибок нет" in capsys.readouterr().out


def test_plan_check_passes_p1_p2_p4(project, capsys, quiet_snapshot):
    """Чистый черновик: три PASS дословно, номер для P4 — из имени файла, а не из заголовка."""
    draft = write_draft(project)

    assert plan_check(project, "2") == 0
    out = capsys.readouterr().out
    assert "✓ P1: раздел «Разведка» на месте по якорю ## Разведка" in out
    assert "✓ P2: кандидатов «берём» без шага интеграции нет" in out
    assert "✓ P4: раздел ссылается на свой артефакт разведки docs/research/2-prior-art.md" in out

    notes = doctor.collect(project, str(draft)).notes
    assert any("план P1: раздел «Разведка» на месте" in note for note in notes)
    assert any("план P4: раздел ссылается на свой артефакт разведки"
               " docs/research/2-prior-art.md" in note for note in notes)


def test_plan_without_research_section_fails_p1(project, capsys):
    """План без раздела «Разведка» — P1 FAIL и ненулевой код возврата."""
    draft = write_draft(project, research=False)
    assert draft.read_text(encoding="utf-8") == helpers.PLAN

    assert plan_check(project, "2") == 1
    out = capsys.readouterr().out
    assert ("✗ P1: нет раздела «Разведка» — план без ссылки на разведку "
            "не идёт на ревью") in out


def test_missing_cycle_module_degrades(project, capsys, monkeypatch, quiet_snapshot):
    """Без модуля plan_cycle обе команды деградируют строкой, но не падают."""
    draft = write_draft(project)
    monkeypatch.setattr(doctor, "plan_cycle", None)
    phrase = "P1–P4 не проверены: модуль plan_cycle не найден"

    assert plan_check(project, "2") == 0
    out = capsys.readouterr().out
    assert f"! {phrase}" in out
    assert "Требования стража" not in out

    report = doctor.collect(project, str(draft))
    assert phrase in report.warnings
    assert not any(problem.startswith("план P") for problem in report.problems)


def test_unicode_plan_output_is_intact(project, capsys, quiet_snapshot):
    """Юникодная ссылка печатается целиком: P4 видит чужой артефакт своей строкой."""
    draft = write_draft(project, artifact="docs/research/2-разведка-ё-prior-art.md")
    expected = ("✗ P4: раздел ссылается на docs/research/2-разведка-ё-prior-art.md, "
                "а артефакт плана — docs/research/2-prior-art.md")

    assert plan_check(project, "2") == 1
    out = capsys.readouterr().out
    assert expected in out

    assert any(problem == "план " + expected[len("✗ "):]
               for problem in doctor.collect(project, str(draft)).problems)
