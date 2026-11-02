# -*- coding: utf-8 -*-
"""Секция системного промта `step-pipeline-planning`: текст зависит от режима проекта.

Секция — не «справка», а единственное место, где агент узнаёт, разрешено ли в этом
проекте планировать. Поэтому проверяется не наличие строк, а смысл: при включённом
режиме есть триггеры и требование загрузить скилл, при выключенном — прямой запрет,
без проекта — секции нет вовсе.
"""
from __future__ import annotations

from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
from step_pipeline import plan_mode, prompt_section


class FakeCtx:
    """Минимальный `ctx` Hermes: запоминает, что именно зарегистрировал плагин."""

    def __init__(self) -> None:
        self.cli = []
        self.tools = []
        self.sections = []
        self.hooks = []

    def register_cli_command(self, **kwargs) -> None:
        self.cli.append(kwargs)

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_system_prompt_section(self, section_id, content, **kwargs) -> None:
        self.sections.append((section_id, content, kwargs))

    def register_hook(self, hook_name, callback) -> None:
        self.hooks.append((hook_name, callback))


def test_section_on_lists_triggers_and_requires_skill(project: Path):
    """Режим включён: названы фразы-триггеры и требование сначала загрузить скилл."""
    plan_mode.write_mode(project, True)

    text = prompt_section.build_section(project)

    assert text is not None
    assert "ВКЛЮЧЁН" in text
    for phrase in ("составь план", "спланируй", "план работ"):
        assert phrase in text
    assert prompt_section.SKILL_NAME in text
    assert "загрузи" in text.lower()
    assert "запрещено" not in text


def test_section_off_forbids_planning_and_skill(project: Path):
    """Режим выключен: прямой запрет планирования и загрузки скилла плюс команда включения."""
    plan_mode.write_mode(project, False)

    text = prompt_section.build_section(project)

    assert text is not None
    assert "ВЫКЛЮЧЕН" in text
    assert "запрещено" in text
    assert prompt_section.SKILL_NAME in text
    assert "hermes pipeline plan-mode on" in text
    assert "составь план" not in text  # триггеры в выключенном режиме не рекламируются


def test_section_without_project_is_none():
    """Нет проекта — нет утверждения о режиме: секция молчит, а не угадывает."""
    assert prompt_section.build_section(None) is None
    assert prompt_section.section_text({}) is None
    assert prompt_section.section_text(None) is None


def test_register_wires_section_and_hook(project: Path, monkeypatch):
    """`register` заводит секцию с ленивым содержимым и хук `pre_tool_call`."""
    import step_pipeline

    plan_mode.write_mode(project, False)
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    ctx = FakeCtx()

    step_pipeline.register(ctx)

    assert [name for name, _cb, _kw in ctx.sections] == [prompt_section.SECTION_ID]
    section_id, content, kwargs = ctx.sections[0]
    assert callable(content)  # состояние читается при сборке промта, а не при импорте
    assert kwargs["max_chars"] >= len(prompt_section.OFF_TEXT)
    assert [name for name, _cb in ctx.hooks] == ["pre_tool_call"]
    assert ctx.hooks[0][1] is prompt_section.skill_gate
    # Содержимое секции для сессии в этом проекте — запрет, а не текст включённого режима.
    rendered = content({"cwd": str(project), "session_id": "s1"})
    assert isinstance(rendered, str)
    assert "запрещено" in rendered
