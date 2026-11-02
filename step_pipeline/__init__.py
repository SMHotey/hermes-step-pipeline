# -*- coding: utf-8 -*-
"""step-pipeline — конвейер шагов плана как плагин Hermes.

Плагин ничего не вычисляет сам: он ставит в проект движок (`.pipeline/`), будит роли
канбана, показывает состояние в локальном UI и уведомляет в Telegram. Решения
принимает детерминированный страж (`engine/gate.py`), суждение — роль-ревьюер.
"""
from __future__ import annotations

from .cli import build_parser, run
from .prompt_section import SECTION_ID, section_text, skill_gate
from .tools import TOOLS

SECTION_MAX_CHARS = 800


def register(ctx) -> None:
    """Точка входа плагина: CLI `hermes pipeline`, три модели-инструмента, режим планировщика."""
    ctx.register_cli_command(
        name="pipeline",
        help="Пошаговый конвейер: setup, status, run, ui, doctor, notify-test",
        setup_fn=build_parser,
        handler_fn=run,
        description=(
            "Ставит в проект движок конвейера шагов, заводит профили-роли и cron, "
            "показывает визуализацию в локальном UI и шлёт уведомления в Telegram."
        ),
    )
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset="step_pipeline", schema=schema,
                          handler=handler, emoji=emoji)
    # Режим планировщика: текст в промт новой сессии и вето на скилл планирования.
    # Содержимое — функция: состояние проекта читается в момент сборки промта,
    # иначе переключение режима не доехало бы до сессии, начатой после него.
    ctx.register_system_prompt_section(SECTION_ID, section_text, max_chars=SECTION_MAX_CHARS)
    ctx.register_hook("pre_tool_call", skill_gate)
