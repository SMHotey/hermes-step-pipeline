# -*- coding: utf-8 -*-
"""step-pipeline — конвейер шагов плана как плагин Hermes.

Плагин ничего не вычисляет сам: он ставит в проект движок (`.pipeline/`), будит роли
канбана, показывает состояние в локальном UI и уведомляет в Telegram. Решения
принимает детерминированный страж (`engine/gate.py`), суждение — роль-ревьюер.
"""
from __future__ import annotations

from .cli import build_parser, run
from .tools import TOOLS


def register(ctx) -> None:
    """Точка входа плагина: CLI `hermes pipeline` + три модели-инструмента."""
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
