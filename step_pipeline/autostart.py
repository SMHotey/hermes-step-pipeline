# -*- coding: utf-8 -*-
"""Автозагрузка панели: «запускать панель при входе в систему» из настроек.

Одна механика на платформу, без внешних пакетов:

    Windows   задача планировщика Pipeline_Panel (ONLOGON) → скрытый запуск через
              wscript; сценарии лежат в `<home профиля>/panel/`, лог — там же
    Linux     `~/.config/autostart/hermes-step-pipeline-panel.desktop`
    macOS     `~/Library/LaunchAgents/ru.nousresearch.step-pipeline-panel.plist`

Состояние читается с системы, а не из файла настроек: включили автозагрузку руками —
панель показывает «включено». Ничего не создаётся само: только по действию владельца
из панели. Уже существующая задача или запись не перезаписывается — `enable` говорит
«уже включено», иначе панель затирала бы чужой сценарий запуска.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any, Callable

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import project as proj
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore[no-redef]

TASK_NAME = "Pipeline_Panel"
DESKTOP_NAME = "hermes-step-pipeline-panel.desktop"
PLIST_NAME = "ru.nousresearch.step-pipeline-panel.plist"
CMD_NAME = "panel.cmd"
VBS_NAME = "panel-hidden.vbs"
LOG_NAME = "panel.log"
RETRY_SECONDS = 10

Runner = Callable[..., Any]


def platform_kind() -> str:
    """Платформа автозагрузки: windows, darwin или linux (остальные — как linux)."""
    if os.name == "nt":
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def _run(cmd: list[str]) -> Any:
    """Вызов системной утилиты: без исключений на ненулевом коде (он и есть ответ)."""
    return proj.run(cmd, timeout=60)


def _panel_dir(home: Path | None = None) -> Path:
    """Каталог сценариев автозагрузки Windows — внутри home профиля."""
    return Path(home or proj.hermes_home()) / "panel"


def autostart_file(kind: str | None = None, home: Path | None = None) -> Path:
    """Файл автозапуска для linux/darwin.

    `home` здесь — домашний каталог пользователя, а не профиля Hermes: проводник
    автозапуска читает только `~/.config/autostart` (или `$XDG_CONFIG_HOME`).
    """
    kind = kind or platform_kind()
    user_home = Path(home) if home is not None else Path.home()
    if kind == "darwin":
        return user_home / "Library" / "LaunchAgents" / PLIST_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    return ((Path(base).expanduser() if base else user_home / ".config")
            / "autostart" / DESKTOP_NAME)


def _hermes_command() -> str:
    """Чем поднимать панель: настоящий путь к `hermes`, иначе — имя из PATH."""
    import shutil

    name = proj.hermes_bin()
    return shutil.which(name) or name


def _command_line(project: Path, port: int) -> list[str]:
    """Полная команда автозапуска: `hermes pipeline ui --project ... --port ...`."""
    return [_hermes_command(), "pipeline", "ui", "--project", str(Path(project)),
            "--port", str(port), "--no-open"]


def status(project: Path | None = None, *, kind: str | None = None,
           runner: Runner | None = None, home: Path | None = None) -> dict:
    """Что с автозагрузкой сейчас: платформа, включено ли, чем и где.

    Ошибка чтения не выдаётся за «выключено молча»: `error` объясняет причину, а
    `enabled` в этом случае остаётся False — панель покажет и то, и другое.
    """
    kind = kind or platform_kind()
    out: dict[str, Any] = {"platform": kind, "supported": True, "enabled": False,
                           "mechanism": "", "path": "", "command": "", "detail": "",
                           "error": ""}
    if kind == "windows":
        out["mechanism"] = f"задача планировщика {TASK_NAME} (при входе в систему)"
        out["path"] = TASK_NAME
        runner = runner or _run
        try:
            res = runner(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
        except OSError as exc:
            out["error"] = f"schtasks не запускается: {exc}"
            return out
        out["enabled"] = int(getattr(res, "returncode", 1)) == 0
        out["detail"] = ("задача есть — панель поднимается при входе в систему"
                         if out["enabled"] else "задачи нет — автозагрузка выключена")
        return out
    path = autostart_file(kind, home)
    out["mechanism"] = "запись в автозапуск сеанса"
    out["path"] = str(path)
    out["enabled"] = path.is_file()
    if out["enabled"]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out["error"] = f"файл автозапуска не читается: {exc}"
            text = ""
        out["command"] = _exec_line(text)
        out["detail"] = "файл на месте — панель поднимается при входе в систему"
    else:
        out["detail"] = "файла нет — автозагрузка выключена"
    return out


def _exec_line(text: str) -> str:
    """Строка запуска из .desktop или plist — для подсказки в настройках."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Exec="):
            return stripped[len("Exec="):].strip()
    marker = "<string>"
    if marker in text:
        parts = [part.split("</string>")[0] for part in text.split(marker)[1:]]
        if parts:
            return " ".join(parts)
    return ""


def _write(path: Path, text: str, *, crlf: bool = False) -> None:
    """Записать сценарий автозагрузки: UTF-8, каталог создаётся.

    `newline=""` обязателен: без него `write_text` переводит `\\n` в `\\r\\n` поверх
    наших `\\r\\n`, и в `.cmd` появляются пустые строки между командами.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.replace("\r\n", "\n").replace("\n", "\r\n") if crlf else text
    path.write_text(body, encoding="utf-8", newline="")


def _cmd_text(project: Path, port: int, home: Path) -> str:
    """Сценарий Windows: цикл перезапуска, лог рядом, окна нет (его гасит vbs)."""
    log = _panel_dir(home) / LOG_NAME
    cmd = " ".join(f'"{part}"' for part in _command_line(project, port))
    return (
        "@echo off\n"
        "rem Панель конвейера шагов: автозагрузка. Файл создан панелью (настройки).\n"
        "rem Задача планировщика запускает его при входе в систему через wscript — без окна.\n"
        "chcp 65001 >nul\n"
        f'set "HERMES_HOME={home}"\n'
        f'cd /d "{Path(project)}"\n'
        ":loop\n"
        f"{cmd} >> \"{log}\" 2>&1\n"
        f'echo [%DATE% %TIME%] панель завершилась, перезапуск через {RETRY_SECONDS} с >> "{log}"\n'
        f"timeout /t {RETRY_SECONDS} /nobreak >nul\n"
        "goto loop\n"
    )


def _vbs_text(cmd_path: Path) -> str:
    """Скрытый запуск: окно консоли не появляется, задача ждёт цикл перезапуска."""
    return (
        "' Скрытый запуск панели конвейера шагов (создано панелью).\n"
        "Option Explicit\n"
        "Dim shell, cmd\n"
        'Set shell = CreateObject("WScript.Shell")\n'
        f'cmd = "{cmd_path}"\n'
        'shell.Run "cmd.exe /c """ & cmd & """", 0, True\n'
    )


def _desktop_text(project: Path, port: int) -> str:
    """Запись freedesktop: сеанс пользователя поднимает панель без терминала."""
    args = " ".join(shlex.quote(part) for part in _command_line(project, port))
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Панель конвейера шагов\n"
        f"Comment=Конвейер шагов: пульт на {project}\n"
        f"Exec={args}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _plist_text(project: Path, port: int) -> str:
    """LaunchAgent: панель поднимается при входе пользователя в систему."""
    args = "".join(f"        <string>{part}</string>\n"
                   for part in _command_line(project, port))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>ru.nousresearch.step-pipeline-panel</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args}"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def enable(project: Path, port: int, *, kind: str | None = None,
           runner: Runner | None = None, home: Path | None = None) -> dict:
    """Включить автозагрузку панели для проекта-хоста и порта.

    Уже включённую автозагрузку не перезаписываем: «включено» — это ответ, а не повод
    затереть существующую задачу или запись автозапуска своим сценарием.
    """
    kind = kind or platform_kind()
    port = int(port or 0)
    if port <= 0:
        return {"ok": False, "status": status(project, kind=kind, runner=runner, home=home),
                "error": "панель слушает случайный порт: автозагрузке нужен --port"}
    current = status(project, kind=kind, runner=runner, home=home)
    if current.get("enabled"):
        return {"ok": True, "already": True, "status": current,
                "detail": "автозагрузка уже включена: " + (current.get("mechanism") or "")}
    if kind == "windows":
        return _enable_windows(project, port, current, runner or _run, home)
    path = autostart_file(kind, home)
    try:
        text = _plist_text(project, port) if kind == "darwin" else _desktop_text(project, port)
        _write(path, text)
    except OSError as exc:
        return {"ok": False, "status": status(project, kind=kind, runner=runner, home=home),
                "error": f"файл автозапуска не записан: {exc}"}
    fresh = status(project, kind=kind, runner=runner, home=home)
    return {"ok": bool(fresh.get("enabled")), "status": fresh,
            "detail": f"автозагрузка включена: {path}"}


def _enable_windows(project: Path, port: int, current: dict, runner: Runner,
                    home: Path | None) -> dict:
    """Задача планировщика ONLOGON + скрытый запуск: файлы в home профиля."""
    home_path = Path(home or proj.hermes_home())
    folder = _panel_dir(home_path)
    cmd_path = folder / CMD_NAME
    vbs_path = folder / VBS_NAME
    try:
        _write(cmd_path, _cmd_text(project, port, home_path), crlf=True)
        _write(vbs_path, _vbs_text(cmd_path), crlf=True)
    except OSError as exc:
        return {"ok": False, "status": current, "error": f"сценарии автозагрузки не записаны: {exc}"}
    command = f'wscript.exe "{vbs_path}"'
    try:
        res = runner(["schtasks", "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME,
                      "/TR", command, "/RL", "LIMITED", "/F"])
    except OSError as exc:
        return {"ok": False, "status": current, "error": f"schtasks не запускается: {exc}"}
    if int(getattr(res, "returncode", 1)) != 0:
        text = ((getattr(res, "stderr", "") or "") + (getattr(res, "stdout", "") or "")).strip()
        return {"ok": False, "status": current,
                "error": text or f"schtasks завершился с кодом {res.returncode}"}
    fresh = status(project, kind="windows", runner=runner, home=home)
    return {"ok": bool(fresh.get("enabled")), "status": fresh,
            "detail": f"автозагрузка включена: задача {TASK_NAME} → {cmd_path}"}


def disable(*, kind: str | None = None, runner: Runner | None = None,
            home: Path | None = None) -> dict:
    """Выключить автозагрузку: снять задачу планировщика или удалить запись сеанса."""
    kind = kind or platform_kind()
    if kind == "windows":
        runner = runner or _run
        notes: list[str] = []
        for cmd in (["schtasks", "/End", "/TN", TASK_NAME],
                    ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]):
            try:
                res = runner(cmd)
            except OSError as exc:
                notes.append(f"schtasks не запускается: {exc}")
                break
            if int(getattr(res, "returncode", 1)) != 0:
                # «Нет такой задачи» — не ошибка: выключать уже нечего. Код и текст
                # утилиты кладём в подсказку, а решение принимает свежий статус.
                text = ((getattr(res, "stderr", "") or "")
                        + (getattr(res, "stdout", "") or "")).strip()
                notes.append(f"schtasks {cmd[1].lower()}: {text or res.returncode}")
        fresh = status(kind="windows", runner=runner, home=home)
        result: dict[str, Any] = {"ok": not fresh.get("enabled"), "status": fresh,
                                  "detail": "автозагрузка выключена" if not fresh.get("enabled")
                                            else "задача планировщика осталась"}
        if not fresh.get("enabled"):
            return result
        result["error"] = "; ".join(notes) or "задачу не удалось снять"
        return result
    path = autostart_file(kind, home)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "status": status(kind=kind, runner=runner, home=home),
                "error": f"файл автозапуска не удалён: {exc}"}
    fresh = status(kind=kind, runner=runner, home=home)
    return {"ok": not fresh.get("enabled"), "status": fresh,
            "detail": f"автозагрузка выключена: {path}"}
