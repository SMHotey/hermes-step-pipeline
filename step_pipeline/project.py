# -*- coding: utf-8 -*-
"""Работа с проектом-приёмником: поиск корня, чтение конфига, вызов движка.

Плагин НИКОГДА не импортирует движок как модуль: единственный путь исполнения —
`<проект>/.pipeline/*.py` через subprocess. Так у плагина и у cron-джобы один и тот
же код, и обновление движка в проекте не требует переустановки плагина.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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


def hermes_home() -> Path:
    """Home профиля Hermes (не корень Hermes): где живут `projects.json` и `config.yaml`.

    `HERMES_HOME` в процессе, запущенном Hermes, уже указывает на каталог профиля
    (`hermes_cli/profiles.py: resolve_profile_env`), у именованного профиля это
    `…/hermes/profiles/ИМЯ`. Без него — `LOCALAPPDATA/hermes/profile…`, затем `~/.hermes`.
    Импорт `setup_flow` отсюда запрещён (циклический), поэтому блок осознанно продублирован.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "hermes" if local else Path.home() / ".hermes"
    profile = os.environ.get("HERMES_PROFILE")
    if profile and (base / "profiles" / profile).is_dir():
        return base / "profiles" / profile
    return base


def projects_file() -> Path:
    """Реестр проектов панели: env `PIPELINE_PROJECTS_FILE`, иначе `<home профиля>/projects.json`."""
    env = os.environ.get("PIPELINE_PROJECTS_FILE")
    return Path(env).expanduser() if env else hermes_home() / "projects.json"


def _displayed_entries(raw: list) -> list[tuple[int, dict[str, Any], str, Path, str]]:
    """Видимые панели записи реестра: (индекс, запись, id, корень, имя).

    Один разбор на чтение, добавление и удаление: id обязан совпадать во всех трёх
    местах, иначе панель удалит не ту запись (D1).
    """
    out: list[tuple[int, dict[str, Any], str, Path, str]] = []
    taken: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        raw_root = item.get("root") or item.get("path")
        if not raw_root or not str(raw_root).strip():
            continue
        root = Path(str(raw_root)).expanduser().resolve()
        if not root.is_dir():
            continue            # каталога нет — запись нечего показывать (§4.1)
        name = str(item.get("name") or "").strip() or root.name
        base = str(item.get("id") or "").strip() or slug(name)
        project_id, number = base, 2
        while project_id in taken:
            project_id, number = f"{base}-{number}", number + 1
        taken.append(project_id)
        out.append((index, item, project_id, root, name))
    return out


def read_projects() -> list[dict[str, Any]]:
    """Записи реестра: `{"id", "name", "root"}`; битый файл — пустой список.

    Запись без `root` пропускается: угадывать, какой проект имелся в виду, нельзя.
    Неуникальные `id` получают суффикс `-2`, `-3` — по ним панель адресует действия.
    Пути не проверяются: каталог без `.pipeline` показывается как «не установлен».
    """
    try:
        data = json.loads(projects_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    return [{"id": project_id, "name": name, "root": root}
            for _index, _item, project_id, root, name in _displayed_entries(raw)]


def _read_registry_raw() -> tuple[dict[str, Any] | None, list]:
    """Сырой реестр: контейнер (`None` — форма-список) и список записей.

    Нет файла — пустой реестр формы-словаря: добавление создаёт файл. Битый JSON —
    `ValueError`: писатель обязан отказать, а не затереть чужие записи (D2).
    """
    try:
        text = projects_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"projects": []}, []
    except OSError as exc:
        raise ValueError(f"файл реестра не читается: {exc}") from exc
    if not text.strip():
        return {"projects": []}, []
    data = json.loads(text)
    if isinstance(data, list):
        return None, data
    if isinstance(data, dict):
        raw = data.get("projects")
        return data, raw if isinstance(raw, list) else []
    raise ValueError("файл реестра не разбирается: ожидался список или словарь с projects")


def raw_projects() -> list[dict[str, Any]]:
    """Сырые записи файла реестра — для проверок перед записью; битый файл — `ValueError`."""
    _container, raw = _read_registry_raw()
    return [item for item in raw if isinstance(item, dict)]


def _write_registry(container: dict[str, Any] | None, entries: list) -> None:
    """Записать реестр атомарно: временный файл рядом, затем `os.replace` (D1).

    Чужие ключи контейнера сохраняются: панель правит только список `projects`.
    """
    path = projects_file()
    payload = entries if container is None else {**container, "projects": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".projects-", suffix=".tmp",
                                         delete=False) as handle:
            temp = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass


# ─── уведомления панели (план 3 S10): настройки в реестре, а не в pipeline.json ───
# Два `notify` в проекте — разные вещи: `pipeline.json notify` описывает канал Telegram
# (токен, чат, префикс), `projects.json notify` — что и куда шлёт панель. Держим их
# порознь, чтобы правка настроек панели не уезжала в поставку проекта.
NOTIFY_KEY = "notify"

# Девять видов внимания: порядок задаёт и порядок галочек на экране, и порядок в файле.
NOTIFY_KINDS = ("step_started", "step_closed", "changes_requested", "plan_stopped",
                "card_crashed", "card_timed_out", "card_rate_limited", "tick_action",
                "plan_idle")

# Набор по умолчанию при включении проекта: семь «внимательных» видов. `tick_action` и
# `plan_idle` шумят на каждом тике и простое, поэтому включаются только вручную.
NOTIFY_DEFAULT_EVENTS = ("step_started", "step_closed", "changes_requested", "plan_stopped",
                         "card_crashed", "card_timed_out", "card_rate_limited")


def _notify_events(raw) -> list[str]:
    """Набор видов из файла: неизвестные id отбрасываются, порядок — как в `NOTIFY_KINDS`.

    Не список и не кортеж — набор по умолчанию: угадывать смысл чужого значения нельзя.
    """
    if not isinstance(raw, (list, tuple, set)):
        return list(NOTIFY_DEFAULT_EVENTS)
    wanted = {str(item) for item in raw}
    return [kind for kind in NOTIFY_KINDS if kind in wanted]


def read_notify() -> dict[str, Any]:
    """Настройки уведомлений из реестра: глобальный переключатель и записи проектов.

    Битый файл не роняет панель: `error` объясняет причину, дальше — «всё выключено».
    Отсутствие ключа `notify` — это «выключено», а не «включено по умолчанию»:
    разрешение на всплывающие окна даёт пользователь, а не файл.
    """
    out: dict[str, Any] = {"global": False, "projects": {}, "error": ""}
    try:
        container, entries = _read_registry_raw()
    except ValueError as exc:
        out["error"] = f"файл реестра не разбирается: {exc}"
        return out
    if isinstance(container, dict):
        raw = container.get(NOTIFY_KEY)
        out["global"] = bool(raw.get("enabled")) if isinstance(raw, dict) else False
    for _index, item, project_id, _root, _name in _displayed_entries(entries):
        raw = item.get(NOTIFY_KEY)
        enabled = bool(raw.get("enabled")) if isinstance(raw, dict) else False
        events = (_notify_events(raw.get("events"))
                  if isinstance(raw, dict) and "events" in raw
                  else list(NOTIFY_DEFAULT_EVENTS))
        out["projects"][project_id] = {"enabled": enabled, "events": events}
    return out


def write_notify(project_id: str = "", global_enabled: bool | None = None,
                 enabled: bool | None = None, events: list[str] | None = None) -> dict[str, Any]:
    """Записать настройки уведомлений — сразу в файл, атомарно (`_write_registry`).

    Правится только ключ `notify`: чужие ключи записи и контейнера остаются как были,
    иначе панель стирала бы то, чего не понимает. Битая запись — `ValueError`, и тогда
    файл не трогается вовсе: затирать реестр из-за одной опечатки нельзя.
    """
    try:
        container, entries = _read_registry_raw()
    except ValueError as exc:
        # Битый JSON приходит текстом разборщика («Expecting property name…»), а ответ
        # панели обязан называть файл: иначе пользователь ищет ошибку в конвейере.
        message = str(exc)
        raise ValueError(message if "реестр" in message
                         else f"реестр не разбирается: {message}") from exc
    if global_enabled is not None:
        if container is None:
            raise ValueError("реестр в форме списка: глобальный переключатель некуда писать, "
                             "добавь контейнер projects")
        raw = container.get(NOTIFY_KEY)
        notify = dict(raw) if isinstance(raw, dict) else {}
        notify["enabled"] = bool(global_enabled)
        container = {**container, NOTIFY_KEY: notify}
    changed = ""
    if project_id:
        wanted = str(project_id).strip()
        visible = _displayed_entries(entries)
        match = next((item for item in visible if item[2] == wanted), None)
        if match is None:
            available = ", ".join(item[2] for item in visible) or "—"
            raise ValueError(f"неизвестный проект: {wanted}; доступные: {available}")
        index = match[0]
        entry = {**entries[index]}
        raw = entry.get(NOTIFY_KEY)
        notify = dict(raw) if isinstance(raw, dict) else {}
        if enabled is not None:
            notify["enabled"] = bool(enabled)
        elif "enabled" not in notify:
            notify["enabled"] = False
        if events is not None:
            notify["events"] = _notify_events(events)
        else:
            notify["events"] = _notify_events(notify.get("events"))
        entry[NOTIFY_KEY] = notify
        entries = [*entries[:index], entry, *entries[index + 1:]]
        changed = wanted
    if global_enabled is None and not changed:
        raise ValueError("нечего менять: нужен global_enabled или project")
    _write_registry(container, entries)
    state = read_notify()
    result: dict[str, Any] = {"ok": True, "global": state["global"]}
    if changed:
        result["project"] = {"id": changed, **(state["projects"].get(changed) or {})}
    return result


def add_project(root: Path | str, name: str = "") -> dict[str, Any]:
    """Добавить проект в реестр панели: запись `{"id", "name", "root"}` (D1).

    id вычисляется по правилам `read_projects` и уникален среди всех сырых записей —
    включая те, что панель не показывает (несуществующий каталог). Битый файл —
    `ValueError`, вызывающий отдаёт 400 и файл не трогает.
    """
    container, entries = _read_registry_raw()
    target = Path(root).expanduser().resolve()
    label = str(name or "").strip() or target.name
    taken = {project_id for _i, _e, project_id, _r, _n in _displayed_entries(entries)}
    for item in entries:                    # id из невидимых записей тоже занят
        if isinstance(item, dict):
            explicit = str(item.get("id") or "").strip()
            if explicit:
                taken.add(explicit)
    base = slug(label)
    project_id, number = base, 2
    while project_id in taken:
        project_id, number = f"{base}-{number}", number + 1
    entry = {"id": project_id, "name": label, "root": str(target)}
    _write_registry(container, [*entries, entry])
    return entry


def remove_project(project_id: str) -> bool:
    """Убрать запись реестра по id панели; False — такой записи нет (D1).

    Каталог проекта не читается и не меняется: панель отключает проект, а не удаляет
    его с диска (C2). Записи, которых панель не показывает, не удаляются: их id
    никому не адресуем.
    """
    wanted = str(project_id or "").strip()
    if not wanted:
        return False
    container, entries = _read_registry_raw()
    for index, _item, current_id, _root, _name in _displayed_entries(entries):
        if current_id == wanted:
            _write_registry(container, [item for position, item in enumerate(entries)
                                        if position != index])
            return True
    return False


def _yaml_scalar(value: str) -> str:
    """Значение YAML-строки: без кавычек и хвостового комментария."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def _yaml_project_root(text: str) -> str:
    """`project_root` из блока `plugins … step_pipeline` без PyYAML (в CI его нет).

    Разбор по отступам, а не по фиксированной схеме: Hermes кладёт значения схемы
    плагина в `config.yaml` вложенными словарями, и точная глубина — его дело.
    """
    stack: list[tuple[int, str]] = []      # (отступ, ключ) открытых блоков
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(line) - len(line.lstrip())
        match = re.match(r"^([^:#]+):(.*)$", stripped)
        if not match:
            continue
        name = match.group(1).strip().strip("'\"")
        value = match.group(2).strip()
        while stack and stack[-1][0] >= lead:
            stack.pop()
        if name == "project_root":
            path = [key for _indent, key in stack]
            if "plugins" in path and any("step_pipeline" in key for key in path):
                return _yaml_scalar(value)
            continue
        if not value:                      # `ключ:` — открывается вложенный блок
            stack.append((lead, name))
    return ""


def _config_yaml_candidates() -> list[Path]:
    """`config.yaml` в home профиля — там Hermes хранит значения схемы плагина."""
    return [hermes_home() / "config.yaml"]


def configured_project_root() -> Path | None:
    """Проект по умолчанию: env `PIPELINE_PROJECT_ROOT`, затем ключ схемы плагина.

    Ключ `project_root` объявлен в `config_schema`, поэтому его значение живёт
    в `config.yaml` home профиля (`plugins.entries.step_pipeline.settings`). Нечитаемый
    файл, отсутствие ключа и несуществующий каталог дают `None` — тогда работает поиск
    вверх от cwd, как раньше.
    """
    env_root = os.environ.get("PIPELINE_PROJECT_ROOT")
    if env_root and env_root.strip():
        # env — решение владельца: ключ из config.yaml его не подменяет, но
        # несуществующий каталог всё равно даёт `None` (проекта по умолчанию нет).
        path = Path(env_root.strip()).expanduser()
        return path.resolve() if path.is_dir() else None
    raw = ""
    for path in _config_yaml_candidates():
        try:
            raw = _yaml_project_root(path.read_text(encoding="utf-8"))
        except OSError:
            raw = ""
        if raw.strip():
            break
    if not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    return path.resolve() if path.is_dir() else None


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
