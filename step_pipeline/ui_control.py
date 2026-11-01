# -*- coding: utf-8 -*-
"""Фоновый запуск UI: отдельный процесс, который живёт после закрытия терминала.

Нужен, чтобы пульт конвейера можно было открыть в браузере в любой момент:
`hermes pipeline ui --detach` поднимает сервер, ждёт ответа /api/health и выходит,
оставляя PID в `.pipeline/ui.json`. `hermes pipeline ui --stop` гасит его.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from . import project as proj
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore

PID_FILE = "ui.json"
HEALTH_PATH = "/api/health"


def pid_path(project: Path) -> Path:
    return Path(project) / ".pipeline" / PID_FILE


def is_up(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{HEALTH_PATH}", timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def read_pid(project: Path) -> dict:
    path = pid_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def start(project: Path, port: int, host: str = "127.0.0.1",
          wait_seconds: float = 20.0, preview: bool = False,
          board: str | None = None) -> dict:
    """Поднять UI отдельным процессом. Возвращает {ok, url, pid, port, error}."""
    if is_up(port, host):
        old = read_pid(project)
        return {"ok": True, "url": f"http://{host}:{port}/", "pid": old.get("pid"),
                "port": port, "already": True}
    log = Path(project) / ".pipeline" / "ui.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [proj.hermes_bin(), "pipeline", "ui", "--project", str(project),
           "--port", str(port), "--host", host, "--no-open"]
    if preview:
        cmd.append("--preview")
    if board:
        cmd += ["--board", board]
    kwargs: dict = {"cwd": str(project), "stdout": open(log, "ab"), "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return {"ok": False, "error": f"не смог запустить: {exc}", "port": port}
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_up(port, host):
            pid_path(project).write_text(json.dumps(
                {"pid": child.pid, "port": port, "host": host, "url": f"http://{host}:{port}/",
                 "log": str(log)}, ensure_ascii=False), encoding="utf-8")
            return {"ok": True, "url": f"http://{host}:{port}/", "pid": child.pid,
                    "port": port}
        if child.poll() is not None:
            tail = ""
            try:
                tail = log.read_text(encoding="utf-8", errors="replace")[-400:]
            except OSError:
                pass
            return {"ok": False, "error": f"процесс завершился (код {child.returncode}). {tail}",
                    "port": port}
        time.sleep(0.5)
    return {"ok": False, "error": f"UI не ответил за {int(wait_seconds)} с — смотри {log}",
            "port": port, "pid": child.pid}


def stop(project: Path) -> dict:
    """Погасить фоновый UI по PID из `.pipeline/ui.json`."""
    info = read_pid(project)
    pid = int(info.get("pid") or 0)
    if not pid:
        return {"ok": False, "error": "нет .pipeline/ui.json — UI не запускался через --detach"}
    if not _alive(pid):
        pid_path(project).unlink(missing_ok=True)
        return {"ok": True, "detail": f"процесс {pid} уже не работал, метка удалена"}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"не смог остановить {pid}: {exc}"}
    pid_path(project).unlink(missing_ok=True)
    return {"ok": True, "detail": f"UI остановлен (pid {pid})"}


if __name__ == "__main__":  # ручная проверка
    root = proj.find_project(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(start(root, int(sys.argv[2]) if len(sys.argv) > 2 else 8770),
                     ensure_ascii=False))
