# -*- coding: utf-8 -*-
"""Атомарная запись файлов состояния и блокировка тика.

Зачем: два одновременных тика портят журнал плана, состояние и отложенный коммит.
Отсюда две вещи:

1. `atomic_write_text` — любая запись идёт во временный файл В ТОМ ЖЕ каталоге,
   затем `os.replace` подменяет целевой файл. Kill посреди записи оставляет старый
   файл целым, а не обрезанным по половине байтов (обрезанный `plan_1_state.json`
   читается как `{}`: теряются `pending_commit` и `hold_steps`).
2. `acquire`/`release`/`locked` — блокировка каталога состояния одним файлом
   `tick.lock`. Второй тик видит живую блокировку и не делает ничего; протухшая
   (по возрасту, чужому хосту или мёртвому PID) снимается и создаётся заново.

Примитив блокировки — `os.open(..., O_CREAT | O_EXCL)` без `msvcrt`/`fcntl`:
кроссплатформенно, только stdlib и не держит открытый дескриптор на время тика.

Блокировку берёт только точка входа (`gate.main`); писатели состояния (`save_state`,
`write_stop`, `_write_offset`) её не берут — иначе тик самоблокируется на своей же
записи.
"""
from __future__ import annotations

import io
import json
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LOCK_NAME = "tick.lock"
STALE_AFTER = 1800
WAIT = float(os.environ.get("PIPELINE_LOCK_WAIT") or 5)

STEP = 0.1                       # шаг ожидания блокировки, секунды
STILL_ACTIVE = 259               # GetExitCodeProcess: процесс жив
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ───────────────────────────── атомарная запись ─────────────────────────────

def atomic_write_text(path: Path, text: str) -> None:
    """Записать текст атомарно: временный файл рядом, fsync, `os.replace`.

    Целевой файл либо старый целиком, либо новый целиком — обрезанным он не бывает
    даже после kill посреди записи. Временный файл при ошибке удаляется.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        # io.open, а не встроенный open: запись обязана проходить через один и тот же
        # слой (тесты двигают io.open и проверяют, что обрыв не портит целевой файл).
        with io.open(tmp, "w", encoding="utf-8") as fh:
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


def save_json(path: Path, data) -> None:
    """Записать JSON тем же форматом, что раньше (`ensure_ascii=False`, отступ 2)."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# ───────────────────────────── блокировка ─────────────────────────────

@dataclass
class Lock:
    """Своя взятая блокировка: путь файла и записанная полезная нагрузка."""

    path: Path
    payload: dict


def _payload(what: str) -> dict:
    return {"pid": os.getpid(), "host": socket.gethostname(),
            "at": time.strftime("%d.%m.%Y %H:%M:%S"), "at_ts": time.time(),
            "what": what}


def holder(state_dir: Path) -> dict | None:
    """Полезная нагрузка чужой блокировки (для сообщений «тик идёт (pid …, с …)»)."""
    path = Path(state_dir) / LOCK_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def acquire(state_dir: Path, what: str = "tick", wait: float | None = None,
            stale_after: float | None = None) -> Lock | None:
    """Взять блокировку каталога состояния. `None` — блокировка живая и чужая."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / LOCK_NAME
    wait = WAIT if wait is None else float(wait)
    stale_after = STALE_AFTER if stale_after is None else float(stale_after)
    deadline = time.monotonic() + max(0.0, wait)
    took_over = False
    while True:
        payload = _payload(what)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            current = holder(state_dir)
            # Нечитаемый файл блокировки — тоже протухший: иначе тик встанет навсегда.
            if took_over:
                return None          # гонку за протухшую блокировку проиграли
            if current is None or _stale(current, stale_after):
                took_over = True
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(STEP, max(0.0, deadline - time.monotonic())))
            continue
        try:
            os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        except BaseException:
            os.close(fd)
            try:
                path.unlink()
            except OSError:
                pass
            raise
        os.close(fd)
        return Lock(path=path, payload=payload)


def release(lock: Lock | None) -> None:
    """Снять ТОЛЬКО свою блокировку: сверить pid и host, чужую не трогать."""
    if lock is None:
        return
    path = Path(lock.path)
    current = holder(path.parent)
    if not current:
        return
    if int(current.get("pid") or 0) != int(lock.payload.get("pid") or 0):
        return
    if str(current.get("host") or "") != str(lock.payload.get("host") or ""):
        return
    try:
        path.unlink()
    except OSError:
        pass


@contextmanager
def locked(state_dir: Path, what: str = "tick", wait: float | None = None,
           stale_after: float | None = None):
    """`acquire` на входе, `release` в `finally`; `None` внутри — блокировка чужая."""
    lock = acquire(state_dir, what=what, wait=wait, stale_after=stale_after)
    try:
        yield lock
    finally:
        release(lock)


def _stale(payload: dict, stale_after: float) -> bool:
    """Блокировка протухла: возраст, чужой хост или мёртвый PID."""
    try:
        at_ts = float(payload.get("at_ts") or 0)
    except (TypeError, ValueError):
        return True
    if at_ts <= 0 or time.time() - at_ts > stale_after:
        return True
    if str(payload.get("host") or "") != socket.gethostname():
        return True
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return True
    return not _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # процесс есть, но не наш
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows: `os.kill(pid, 0)` живого от мёртвого не отличает (замер S3).

    Работает `OpenProcess` + `GetExitCodeProcess`: 259 (STILL_ACTIVE) — жив.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
