# -*- coding: utf-8 -*-
"""Хук `pre_tool_call`: вето на загрузку скилла планирования при выключенном режиме.

Проверяется ровно то, чем хук опасен: он стоит на пути КАЖДОГО вызова инструмента,
поэтому обязан молчать во всех случаях, кроме одного, и не ронять сессию, когда
состояние проекта прочитать нельзя.
"""
from __future__ import annotations

from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
from step_pipeline import plan_mode, prompt_section


def _no_ambient_project(monkeypatch) -> None:
    """Убрать подсказки окружения: проект ищется только от cwd теста."""
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)


def test_blocks_planning_skill_when_mode_off(project: Path, monkeypatch):
    """Режим выключен — загрузка скилла блокируется с причиной и командой включения."""
    _no_ambient_project(monkeypatch)
    monkeypatch.chdir(project)
    plan_mode.write_mode(project, False)

    result = prompt_section.skill_gate(
        tool_name="skill_view", args={"name": prompt_section.SKILL_NAME})

    assert result is not None
    assert result["action"] == "block"
    assert "режим планирования выключен" in result["message"]
    assert "hermes pipeline plan-mode on" in result["message"]


def test_passes_planning_skill_when_mode_on(project: Path, monkeypatch):
    """Режим включён — хук не вмешивается: скилл загружается как обычно."""
    _no_ambient_project(monkeypatch)
    monkeypatch.chdir(project)
    plan_mode.write_mode(project, True)

    assert prompt_section.skill_gate(
        tool_name="skill_view", args={"name": prompt_section.SKILL_NAME}) is None


def test_silent_without_project(tmp_path: Path, monkeypatch):
    """Конвейер в проекте не стоит — хук молчит: чужой проект он не запирает."""
    _no_ambient_project(monkeypatch)
    workdir = tmp_path / "no-pipeline"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert prompt_section.find_project(workdir) is None
    assert prompt_section.current_project() is None
    assert prompt_section.skill_gate(
        tool_name="skill_view", args={"name": prompt_section.SKILL_NAME}) is None


def test_ignores_other_tools_and_survives_errors(project: Path, monkeypatch, capsys):
    """Чужие вызовы не трогает, а сбой чтения состояния уходит в stderr и не блокирует."""
    _no_ambient_project(monkeypatch)
    monkeypatch.chdir(project)
    plan_mode.write_mode(project, False)

    # Другой инструмент и другой скилл проходят без обращения к состоянию проекта.
    assert prompt_section.skill_gate(tool_name="skills_list", args={}) is None
    assert prompt_section.skill_gate(
        tool_name="skill_view", args={"name": "pipeline-reviewer"}) is None
    assert prompt_section.skill_gate(tool_name="skill_view", args=None) is None

    def boom(_project):
        raise RuntimeError("состояние недоступно")

    monkeypatch.setattr(plan_mode, "read_mode", boom)

    assert prompt_section.skill_gate(
        tool_name="skill_view", args={"name": prompt_section.SKILL_NAME}) is None
    err = capsys.readouterr().err
    assert "вето на скилл планирования не сработало" in err
    assert "состояние недоступно" in err
