# -*- coding: utf-8 -*-
"""Режим планировщика в системном промте и вето на скилл планирования.

Две точки, обе — про ОДНО состояние проекта (`.pipeline/state/plan_mode.json`,
`plan_mode.read_mode`):

* секция системного промта `step-pipeline-planning` — агент с первой секунды сессии
  знает, можно ли в этом проекте планировать, и какими словами его об этом просят;
* хук `pre_tool_call` — при выключенном режиме загрузка скилла
  `pipeline-plan-authoring` блокируется с причиной, а не «мягко не рекомендуется».

Почему состояние проекта, а не профиля: выключатель скиллов Hermes
(`skills.disabled`) глобален для всех проектов сразу (docs/plans/4-planner.md:26-29).

Хук живёт на горячем пути каждого вызова инструмента, поэтому здесь нет ни сети, ни
subprocess'ов: одно чтение маленького JSON и выход. Любое исключение внутри хука
глушится и уходит одной строкой в stderr — упавший хук ломает сессию раньше, чем
человек увидит причину, а вето на один скилл такой цены не стоит.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    from . import plan_mode
    from . import project as proj
except ImportError:  # pragma: no cover
    import plan_mode  # type: ignore
    import project as proj  # type: ignore

SECTION_ID = "step-pipeline-planning"
SKILL_NAME = "pipeline-plan-authoring"
TRIGGERS = ("составь план", "спланируй", "план работ")
BLOCK_REASON = "режим планирования выключен, включить — hermes pipeline plan-mode on"

ON_TEXT = """РЕЖИМ ПЛАНИРОВЩИКА ВКЛЮЧЁН.
Фразы «составь план», «спланируй», «план работ» — запрос на планирование: сначала
загрузи скилл pipeline-plan-authoring (skill_view) и работай по его протоколу, а не
по памяти."""

OFF_TEXT = """РЕЖИМ ПЛАНИРОВЩИКА ВЫКЛЮЧЕН.
Планирование в этом проекте запрещено: не составляй планы и списки шагов, не
предлагай разбиение работы, не загружай скилл pipeline-plan-authoring — загрузку
блокирует хук плагина. Включить режим: hermes pipeline plan-mode on"""


def build_section(project) -> str | None:
    """Текст секции промта для *project*; `None` — секции в этом промте нет.

    Нет проекта — нет и утверждения о режиме: секция молчит, а не угадывает.
    """
    if project is None:
        return None
    return ON_TEXT if plan_mode.read_mode(project) else OFF_TEXT


def section_text(session_info: Mapping[str, Any] | None) -> str | None:
    """Содержимое секции для Hermes: проект берётся из `cwd` новой сессии.

    Hermes зовёт это при сборке промта и передаёт read-only словарь с `cwd`
    (`agent/system_prompt.py:_plugin_session_info`). Ошибка внутри не должна валить
    сборку промта — секция просто не появится.
    """
    try:
        info = session_info if isinstance(session_info, Mapping) else {}
        raw = str(info.get("cwd") or "").strip()
        return build_section(find_project(raw) if raw else None)
    except Exception as exc:  # pragma: no cover - защита от чужих сбоев
        print(f"предупреждение: секция режима планировщика не собрана: {exc}", file=sys.stderr)
        return None


def find_project(start) -> Path | None:
    """Корень проекта от *start* вверх или `None`, если конвейер не установлен."""
    if not start:
        return None
    try:
        return proj.find_project(Path(start))
    except (proj.ProjectNotFound, OSError):
        return None


def current_project() -> Path | None:
    """Проект текущей сессии для хука: настроенный cwd Hermes, затем cwd процесса.

    В `pre_tool_call` путь сессии не передаётся, поэтому cwd достаётся тем же
    резолвером, что и у контекстных файлов. Резолвера может не быть (плагин обязан
    работать без установленного Hermes) — тогда `TERMINAL_CWD` и `os.getcwd()`.
    """
    for start in (_session_cwd(), Path.cwd()):
        found = find_project(start)
        if found is not None:
            return found
    return None


def _session_cwd() -> Path | None:
    """Настроенный cwd сессии из Hermes, иначе `TERMINAL_CWD`; `None` — не знаем."""
    try:
        from agent.runtime_cwd import resolve_context_cwd

        resolved = resolve_context_cwd()
        if resolved:
            return Path(resolved)
    except Exception:
        pass
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    return Path(raw) if raw else None


def skill_gate(*, tool_name: str = "", args: Any = None, **_kwargs) -> dict | None:
    """`pre_tool_call`: не дать загрузить скилл планирования при выключенном режиме.

    Вмешивается только в `skill_view` с `pipeline-plan-authoring`: остальные вызовы
    инструментов и остальные скиллы проходят без единого чтения с диска.
    """
    try:
        if tool_name != "skill_view":
            return None
        params = args if isinstance(args, dict) else {}
        if params.get("name") != SKILL_NAME:
            return None
        project = current_project()
        if project is None or plan_mode.read_mode(project):
            return None
        return {"action": "block", "message": BLOCK_REASON}
    except Exception as exc:
        print(f"предупреждение: вето на скилл планирования не сработало: {exc}", file=sys.stderr)
        return None
