# -*- coding: utf-8 -*-
"""Режим планировщика: состояние ПРОЕКТА, а не профиля.

Режим решает, можно ли в проекте писать код или только планировать. Живёт он в файле
`.pipeline/state/plan_mode.json` в корне проекта, потому что в профиле Hermes выключатель
скиллов (`skills.disabled`) глобален для всех проектов сразу — одно «нет» в одном проекте
погасило бы исполнение везде (docs/plans/4-planner.md:26-29).

Умолчание — «включён»: нет файла или нет каталога состояния означает, что режим никто не
выключал. Битое или непригодное содержимое тоже читается как «включён» с одной строкой
предупреждения в stderr: читать состояние будет хук `pre_tool_call` (шаг S4), а исключение
внутри хука ломает сессию раньше, чем человек увидит причину (docs/plans/4-planner.md:93-95).

Запись атомарная: временный файл рядом с целевым и `os.replace`. Тик или панель, убитые
посреди записи, не оставят обрезанный файл, из которого потом нельзя понять состояние.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from . import project as proj
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore

MODE_FILE = "plan_mode.json"
KEY = "plan_mode"


def state_file(project) -> Path:
    """Путь к файлу состояния режима.

    Берётся из `proj.state_dir`, чтобы уважать `paths.state` проекта: иначе в проекте с
    перенастроенным каталогом состояния появился бы второй, и страж с панелью смотрели бы
    в разные файлы. Каталог не создаётся и файл не трогается — это чистое вычисление пути.
    """
    return proj.state_dir(Path(project)) / MODE_FILE


def read_mode(project) -> bool:
    """Прочитать состояние режима; любая непонятная ситуация означает «включён».

    Исключения наружу не выпускаются никогда: функцию зовут из хука инструментов, а упавший
    хук ломает сессию. Предупреждение о непригодном файле — одна строка в stderr, чтобы не
    засорять машиночитаемый вывод (`--json` в S2, снимок панели в S3).
    """
    path = state_file(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError) as exc:
        print(f"предупреждение: режим планировщика не читается: {path}: {exc}; "
              f"считаю включённым", file=sys.stderr)
        return True

    if not isinstance(data, dict) or not isinstance(data.get(KEY), bool):
        print(f"предупреждение: режим планировщика не читается: {path}: "
              f"нет булева поля {KEY}; считаю включённым", file=sys.stderr)
        return True
    return data[KEY]


def write_mode(project, on) -> dict:
    """Записать состояние режима и вернуть полезную нагрузку (её показывают CLI и панель).

    Ошибку записи (`OSError`) не глотаем: выключение режима — явное действие человека, и
    молча «получилось» здесь хуже, чем честная ошибка в терминале. Каталог состояния
    создаётся здесь же: чтение обязано оставаться без побочных эффектов.
    """
    path = state_file(project)
    payload = {KEY: bool(on), "changed_at": datetime.now().isoformat(timespec="seconds")}
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _atomic_write_text(path: Path, text: str) -> None:
    """Записать текст атомарно: временный файл рядом, fsync, `os.replace`.

    Копия структуры `engine/state_io.atomic_write_text` сознательная: плагин и движок
    поставляются по-разному, и плагин никогда не импортирует движок как модуль
    (project.py:1-6, engine/__init__.py:1-6).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
