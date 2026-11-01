# -*- coding: utf-8 -*-
"""Атомарная запись состояния и блокировка тика: два тика не портят журнал.

Проверяется наблюдаемое следствие на настоящих подпроцессах: обрыв записи посреди
`gate.save_state` оставляет старый файл состояния целым; живая чужая блокировка
заставляет второй тик и команды владельца выйти с кодом 0 и сообщением «тик уже
идёт», не сделав ни одного действия; протухшая блокировка снимается по возрасту и
по мёртвому PID; `setup --upgrade` привозит новый модуль движка.

Настоящую гонку двух одновременных `--run` детерминированно не воспроизвести,
поэтому проверяется следствие: без блокировки действие случилось бы, с блокировкой —
нет, а после снятия блокировки ровно один тик делает ровно одно действие.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from engine_kit import ENGINE, ENGINE_FILES, make_project

KIT = Path(__file__).resolve().parents[2]
STATE = Path(".pipeline") / "state"
LOCK_REL = STATE / "tick.lock"

HOLD_SCRIPT = '''# -*- coding: utf-8 -*-
"""Держатель блокировки: берёт state_io.acquire и спит, пока его не убьют."""
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / ".pipeline"))
import state_io  # noqa: E402 - модуль проекта-приёмника

lock = state_io.acquire(root / ".pipeline" / "state", what="test")
print("LOCKED" if lock is not None else "BUSY", flush=True)
time.sleep(60)
'''

DIE_SCRIPT = '''# -*- coding: utf-8 -*-
"""Обрыв записи: половина байтов во временный файл состояния, затем os._exit(9).

Подмена io.open перехватывает и `Path.write_text`, и атомарную запись движка:
временный файл атомарной записи называется `plan_1_state.json.tmp-<PID>` и тоже
попадает под условие — то есть kill приходит ровно посреди записи состояния.
"""
import io
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / ".pipeline"))
import config as config_mod  # noqa: E402 - модуль проекта-приёмника
import gate  # noqa: E402

real_open = io.open


class Half:
    """Обёртка файла: пишет половину данных, сбрасывает и убивает процесс."""

    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def write(self, data):
        self._fh.write(data[: max(1, len(data) // 2)])
        self._fh.flush()
        os._exit(9)


def patched(file, mode="r", *args, **kwargs):
    fh = real_open(file, mode, *args, **kwargs)
    if "w" in mode and "plan_1_state.json" in str(file):
        return Half(fh)
    return fh


io.open = patched
p = gate.resolve_paths(config_mod.load(str(root)), "1")
gate.save_state(p, {"new": "полное новое состояние", "хвост": "x" * 2000})
print("запись дошла до конца — тест не имеет смысла", flush=True)
'''


def run_gate(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Запустить `gate.py` проекта-приёмника как отдельный процесс."""
    return subprocess.run([sys.executable, str(root / ".pipeline" / "gate.py"), *args],
                          cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})}, timeout=120)


def journal(root: Path) -> str:
    return (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")


def cards(db: Path) -> list[dict]:
    """Карточки борды-заглушки; базы ещё нет — карточек нет."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, title, status, assignee, idempotency_key, max_runtime FROM tasks"
            " ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:      # файл есть, таблицы ещё нет
        return []
    finally:
        con.close()
    return [{"id": r[0], "title": r[1], "status": r[2], "assignee": r[3], "key": r[4],
             "max_runtime": r[5]} for r in rows]


def hold_lock(root: Path, tmp_path: Path) -> subprocess.Popen:
    """Дочерний процесс, который держит настоящую блокировку каталога состояния."""
    script = tmp_path / "hold_lock.py"
    script.write_text(HOLD_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), str(root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            env=dict(os.environ))
    assert proc.stdout is not None
    line = (proc.stdout.readline() or "").strip()
    # Без этой проверки тест завис бы вместо падения.
    assert line == "LOCKED", f"дочерний процесс не взял блокировку: {line!r}"
    return proc


def stop_proc(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(timeout=30)
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def write_lock(root: Path, pid: int, at_ts: float) -> Path:
    """Положить файл блокировки вручную — по схеме полезной нагрузки движка."""
    path = root / LOCK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": int(pid), "host": socket.gethostname(),
                                "at": time.strftime("%d.%m.%Y %H:%M:%S",
                                                    time.localtime(at_ts)),
                                "at_ts": float(at_ts), "what": "test"},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_state_file_is_not_truncated_when_writer_dies_mid_write(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    state_file = root / STATE / "plan_1_state.json"
    old = {"seed": "старое",
           "pending_commit": {"files": ["docs/plans/1-demo.md"], "step": "S1"},
           "hold_steps": ["S9"]}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")

    script = tmp_path / "die_mid_write.py"
    script.write_text(DIE_SCRIPT, encoding="utf-8")
    res = subprocess.run([sys.executable, str(script), str(root)], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)

    assert res.returncode == 9, res.stderr
    assert json.loads(state_file.read_text(encoding="utf-8")) == old


def test_second_tick_reports_lock_and_does_not_act(tmp_path: Path, hermes_stub: Path) -> None:
    root = make_project(tmp_path / "proj")
    # Постановка на месте: тик сразу создаёт карточку шага (а не заказ промптеру),
    # поэтому повторный тик обязан ждать: шаг уже в работе.
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")
    plan_before = journal(root)
    proc = hold_lock(root, tmp_path)
    try:
        res = run_gate(root, "--run", env={"PIPELINE_LOCK_WAIT": "0"})
        assert res.returncode == 0, res.stderr
        assert "тик уже идёт" in res.stdout
        assert cards(hermes_stub) == []
        assert journal(root) == plan_before
    finally:
        stop_proc(proc)

    res = run_gate(root, "--run")
    assert res.returncode == 0, res.stderr
    made = cards(hermes_stub)
    assert [c["key"] for c in made] == ["planloop:1:S1:step"]

    res = run_gate(root, "--run")
    assert res.returncode == 0, res.stderr
    assert cards(hermes_stub) == made


def test_owner_commands_wait_for_lock_and_do_not_act(tmp_path: Path) -> None:
    root = make_project(tmp_path / "proj")
    proc = hold_lock(root, tmp_path)
    try:
        for args in (("--stop", "ручная остановка"), ("--resume",),
                     ("--hold", "S2"), ("--unhold", "S2")):
            res = run_gate(root, *args, env={"PIPELINE_LOCK_WAIT": "0"})
            assert res.returncode == 0, res.stderr
            assert "тик уже идёт" in res.stdout, res.stdout
    finally:
        stop_proc(proc)

    # Команды не применились: стоп-флага нет, состояния (правки hold_steps) тоже.
    assert not (root / STATE / "plan_1_stop.json").exists()
    assert not (root / STATE / "plan_1_state.json").exists()


def test_stale_lock_by_age_is_taken_over(tmp_path: Path, hermes_stub: Path) -> None:
    root = make_project(tmp_path / "proj")
    proc = hold_lock(root, tmp_path)
    try:
        # PID живой, но блокировке час: возраст важнее живости (D4).
        write_lock(root, pid=proc.pid, at_ts=time.time() - 3600)
        res = run_gate(root, "--run")
    finally:
        stop_proc(proc)

    assert res.returncode == 0, res.stderr
    assert "planloop:1:S1:prompt" in [c["key"] for c in cards(hermes_stub)]
    assert not (root / LOCK_REL).exists()


def test_stale_lock_by_dead_pid_is_taken_over(tmp_path: Path, hermes_stub: Path) -> None:
    root = make_project(tmp_path / "proj")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=60)
    write_lock(root, pid=dead.pid, at_ts=time.time())

    res = run_gate(root, "--run")

    assert res.returncode == 0, res.stderr
    assert "planloop:1:S1:prompt" in [c["key"] for c in cards(hermes_stub)]
    assert not (root / LOCK_REL).exists()


def test_upgrade_brings_state_io(tmp_path: Path) -> None:
    sys.path.insert(0, str(KIT))
    from step_pipeline import setup_flow

    assert "state_io.py" in setup_flow.ENGINE_FILES
    assert sorted(setup_flow.ENGINE_FILES) == sorted(ENGINE_FILES)

    root = make_project(tmp_path / "proj")
    # Проект, поставленный до S3: модуля в его копии движка нет.
    (root / ".pipeline" / "state_io.py").unlink()
    copied = setup_flow.copy_engine(root, force=False)

    assert ".pipeline/state_io.py" in copied
    assert (root / ".pipeline" / "state_io.py").read_bytes() == (ENGINE / "state_io.py").read_bytes()
