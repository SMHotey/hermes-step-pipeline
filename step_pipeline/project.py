# -*- coding: utf-8 -*-
"""Работа с проектом-приёмником: поиск корня, чтение конфига, вызов движка.

Плагин НИКОГДА не импортирует движок как модуль: единственный путь исполнения —
`<проект>/.pipeline/*.py` через subprocess. Так у плагина и у cron-джобы один и тот
же код, и обновление движка в проекте не требует переустановки плагина.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
ENGINE_SRC = PLUGIN_DIR / "engine"
ROLES_SRC = PLUGIN_DIR / "roles"
TEMPLATES_SRC = PLUGIN_DIR / "templates"
DEFAULTS_FILE = TEMPLATES_SRC / "pipeline.json.example"

MARKER = "pipeline.json"


class ProjectNotFound(RuntimeError):
    """Проект без установленного конвейера."""


def find_project(start: Path | str | None = None) -> Path:
    """Корень проекта: вверх от *start* (по умолчанию — cwd) до `pipeline.json`."""
    env_root = os.environ.get("PIPELINE_REPO")
    if env_root and (Path(env_root) / MARKER).is_file():
        return Path(env_root).resolve()
    here = Path(start or Path.cwd()).expanduser().resolve()
    if here.is_file():
        here = here.parent
    for candidate in (here, *here.parents):
        if (candidate / MARKER).is_file():
            return candidate
    raise ProjectNotFound(
        f"не нашёл {MARKER} ни в {here}, ни выше. Установи конвейер: `hermes pipeline setup`")


def read_config(project: Path) -> dict[str, Any]:
    """`pipeline.json` проекта как словарь (пустой, если файл битый)."""
    path = Path(project) / MARKER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_config(project: Path, data: dict[str, Any]) -> None:
    path = Path(project) / MARKER
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def engine(project: Path, name: str, engine_dir: Path | None = None) -> Path:
    """Путь к модулю движка: сначала установленный в проекте, затем движок плагина.

    Второй путь — режим предпросмотра: читаем проект, в котором конвейера ещё нет.
    """
    path = Path(project) / ".pipeline" / name
    if path.is_file():
        return path
    if engine_dir is not None and (Path(engine_dir) / name).is_file():
        return Path(engine_dir) / name
    raise ProjectNotFound(
        f"нет {path} — конвейер не установлен в этот проект "
        f"(поставь: `hermes pipeline setup --project \"{Path(project)}\"`)")


def python_exe(project: Path) -> str:
    """Интерпретатор для движка: venv проекта, иначе текущий."""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe",
                "venv/bin/python"):
        candidate = Path(project) / rel
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def run(cmd: list[str], *, cwd: str | Path | None = None, timeout: int = 180,
        env: dict[str, str] | None = None, text: bool = True) -> subprocess.CompletedProcess:
    """Запустить внешнюю команду: по умолчанию UTF-8 с errors="replace".

    text=False — байтовый вывод: нужен там, где содержимое не текст (tasklist).
    """
    kwargs: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "capture_output": True,
                              "timeout": timeout, "env": env}
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd, **kwargs)


def run_engine(project: Path, name: str, *args: str, timeout: int = 180,
               engine_dir: Path | None = None,
               extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Запустить модуль движка проекта и вернуть результат процесса."""
    cmd = [python_exe(project), str(engine(project, name, engine_dir)), *args]
    env = {**os.environ, "PIPELINE_REPO": str(project), **(extra_env or {})}
    return run(cmd, cwd=project, timeout=timeout, env=env)


def engine_json(project: Path, name: str, *args: str, timeout: int = 180,
                engine_dir: Path | None = None,
                extra_env: dict[str, str] | None = None) -> Any:
    """Запустить движок с `--json` и разобрать ответ; при ошибке — исключение с текстом."""
    res = run_engine(project, name, *args, "--json", timeout=timeout,
                     engine_dir=engine_dir, extra_env=extra_env)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "").strip() or f"{name} вышел с {res.returncode}")
    text = (res.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # перед JSON движок мог напечатать служебную строку — берём от первой скобки
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if not starts:
            raise
        return json.loads(text[min(starts):])


def board_db(project: Path, board: str | None = None) -> Path:
    """Путь к sqlite борды канбана (env важнее конфига: так работает и тест, и прод)."""
    env = os.environ.get("PIPELINE_KANBAN_DB")
    if env and not board:
        return Path(env)
    board = board or read_config(project).get("board") or "default"
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "hermes" if local else Path.home() / ".hermes"
    return base / "kanban" / "boards" / board / "kanban.db"


def state_dir(project: Path) -> Path:
    cfg = read_config(project)
    rel = ((cfg.get("paths") or {}).get("state")) or ".pipeline/state"
    return (Path(project) / rel).resolve()


def profile_dir(profile: str) -> Path:
    """Каталог профиля Hermes (для .env и scripts/)."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "hermes" if local else Path.home() / ".hermes"
    return base / "profiles" / profile


def hermes_bin() -> str:
    """"hermes" из PATH; нужен для `kanban`, `cron`, `profile`."""
    return os.environ.get("HERMES_BIN") or "hermes"


def run_hermes(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return run([hermes_bin(), *args], timeout=timeout)


def setup_stdio() -> None:
    """UTF-8 в stdout/stderr плагина: Windows-консоль по умолчанию cp1251."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def slug(name: str) -> str:
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "-"
                      for ch in name.lower()).strip("-")
    return cleaned or "project"
