# -*- coding: utf-8 -*-
"""Раскатка движка по реестру с ожиданием окна: `POST /api/rollout/engine` (S8A).

Проверяется то, чего нет у `setup_flow.upgrade`: окно между шагами (живая блокировка тика
и карточка в работе), пометка `skipped` для проекта без конвейера, сходимость маркера
после настоящего обновления и три отказа маршрута (предпросмотр, второй жест, отсутствие
метки панели) без следов в реестре.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from helpers import KANBAN_SCHEMA
from step_pipeline import project as proj
from step_pipeline import rollout, setup_flow, ui_server

ROUTE = "/api/rollout/engine"
PANEL = {"X-Pipeline-Panel": "1"}
VERSION = ".pipeline/VERSION"


# ───────────────────────────── помощники ─────────────────────────────

def start_server(root: Path, preview: bool = False) -> dict:
    httpd = ui_server.make_server(root, port=0, preview=preview)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "httpd": httpd,
            "state": getattr(httpd, "state")}


def stop_server(server: dict) -> None:
    server["httpd"].shutdown()
    server["httpd"].server_close()


def post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    """POST с JSON-телом; ошибки HTTP разбираются, а не бросаются."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{ROUTE}", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(PANEL if headers is None else headers)})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def write_registry(roots: list[Path]) -> None:
    """Реестр профиля в форме панели: `id` и `name` из имени каталога."""
    entries = [{"id": root.name, "name": root.name, "root": str(root)} for root in roots]
    path = proj.projects_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def registry() -> dict:
    """Реестр профиля как он лежит на диске (пустой словарь, если файла нет)."""
    path = proj.projects_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def engine_entry() -> dict:
    return registry()["rollout"]["engine"]


def engine_row(root: Path) -> dict:
    """Строка проекта в записи раскатки: по корню, а не по порядку."""
    for row in engine_entry()["projects"]:
        if Path(row["root"]).name == root.name:
            return row
    raise AssertionError(f"нет строки проекта {root} в записи rollout.engine")


def hold_tick(root: Path) -> Path:
    """Занять тик так, как его держит `state_io.acquire`: живой pid, наш хост, свежее время.

    Протухшую блокировку движок снимает сам (`state_io._stale`), поэтому pid обязан быть
    живым: иначе окно считалось бы свободным и окно-ожидание не проверить.
    """
    path = root / ".pipeline" / "state" / "tick.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                "at": "14.11.2026 12:00:00", "at_ts": time.time(),
                                "what": "tick"}, ensure_ascii=False), encoding="utf-8")
    return path


def running_card(db: Path) -> None:
    """Карточка «в работе»: по ней видно, что шаг ещё идёт и окна нет."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        con.execute("INSERT INTO tasks (id, title, status, assignee, idempotency_key,"
                    " created_at) VALUES (?, ?, 'running', 'driver', '1:S1:step', ?)",
                    ("t_1", "шаг 1-S1", int(time.time())))
        con.commit()
    finally:
        con.close()


def finish_card(db: Path) -> None:
    """Шаг закончился: карточка больше не держит окно."""
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET status = 'done'")
        con.commit()
    finally:
        con.close()


def upgrade_spy(monkeypatch) -> list:
    """Подмена `rollout._upgrade`: вызовы в список и `VERSION` 9.9.9 вместо копии движка.

    Настоящее обновление здесь не нужно — проверяется расписание (когда именно окно
    сочли свободным), а не то, что делает `setup_flow.upgrade`.
    """
    calls: list[Path] = []

    def fake_upgrade(project, log):
        calls.append(Path(project))
        (Path(project) / VERSION).write_text("9.9.9\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(rollout, "_upgrade", fake_upgrade)
    return calls


def fake_hermes(monkeypatch) -> list:
    """Поддельный `hermes`: доверие ролям и enable не трогают настоящий профиль.

    Фикстуры в `conftest.py` для этого нет (она живёт в `tests/test_setup_flow.py`),
    поэтому помощник локальный и зовётся из теста явно.
    """
    calls: list[tuple] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(*argv, timeout=180):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(proj, "run_hermes", run)
    return calls


def isolate_profile(monkeypatch, tmp_path: Path) -> Path:
    """Изолировать каталог профиля: cron-скрипты пишутся в tmp, а не в живой Hermes.

    `proj.profile_dir` берёт `LOCALAPPDATA`/`~` и `HERMES_HOME` не читает, поэтому
    autouse-фикстуры `isolated_hermes_home` тут не хватает (образец —
    `tests/test_setup_flow.py:788-798`).
    """
    home = tmp_path / "h"
    monkeypatch.setattr(proj, "profile_dir", lambda profile: home / profile)
    return home


def wait_for_release(timeout: float = 10.0) -> None:
    """Дождаться, пока фоновая раскатка отпустит блокировку."""
    deadline = time.monotonic() + timeout
    while rollout.busy() and time.monotonic() < deadline:
        time.sleep(0.01)


# ───────────────────────────── окно между шагами ─────────────────────────────

def test_live_tick_lock_defers_then_applies_engine_upgrade(project: Path, hermes_stub: Path,
                                                           monkeypatch):
    """Живая блокировка тика откладывает обновление, снятая — сразу пускает (D2).

    Сон подменён функцией, которая снимает блокировку: она же фиксирует, что `upgrade`
    до этого момента не звали, — иначе тест доказывал бы только итог.
    """
    calls = upgrade_spy(monkeypatch)
    lock = hold_tick(project)
    seen: list[bool] = []

    def fake_sleep(seconds: float) -> None:
        seen.append(bool(calls))
        lock.unlink()

    write_registry([project])
    result = rollout.run_engine(proj.read_projects(), interval=0.01, sleep=fake_sleep)

    assert seen == [False]                  # под блокировкой движок не подменяли
    assert [path.name for path in calls] == [project.name]
    row = engine_row(project)
    assert row["status"] == "updated"
    assert row["from"] == "" and row["to"] == "9.9.9"
    assert row["at"] and row["error"] == ""
    entry = engine_entry()
    assert entry["running"] is False and entry["ok"] is True
    assert entry["at"] and entry["finished"] and entry["error"] == ""
    assert result["ok"] is True and result["running"] is False


def test_running_card_defers_until_card_is_done(project: Path, hermes_stub: Path,
                                                monkeypatch):
    """Карточка в работе держит окно: сначала предел ожидания, потом `done` (D4).

    Предел ожидания проверяется числом, а не текстом: за 0.2 с окно не открылось, проект
    помечен `failed` с причиной, и в остальные — раскатка идёт дальше.
    """
    calls = upgrade_spy(monkeypatch)
    running_card(hermes_stub)
    write_registry([project])
    rows = proj.read_projects()

    rollout.run_engine(rows, interval=0.01, limit=0.2)

    row = engine_row(project)
    assert row["status"] == "failed"
    assert "окно не найдено" in row["reason"]
    assert calls == []
    assert engine_entry()["ok"] is False

    finish_card(hermes_stub)
    rollout.run_engine(rows, interval=0.01, limit=5.0)

    row = engine_row(project)
    assert row["status"] == "updated" and row["to"] == "9.9.9"
    assert [path.name for path in calls] == [project.name]
    assert engine_entry()["ok"] is True


def test_project_without_pipeline_is_skipped(tmp_path: Path, project: Path,
                                             hermes_stub: Path, monkeypatch):
    """Проект без конвейера — `skipped`, движок в нём не запускается и не `failed` (D7)."""
    calls = upgrade_spy(monkeypatch)
    bare = tmp_path / "bare"
    bare.mkdir()
    write_registry([project, bare])

    result = rollout.run_engine(proj.read_projects(), interval=0.01, limit=1.0)

    row = engine_row(bare)
    assert row["status"] == "skipped"
    assert "не установлен" in row["reason"]
    assert not (bare / ".pipeline").exists()
    assert [path.name for path in calls] == [project.name]
    assert result["ok"] is True             # `skipped` — не отказ раскатки


# ───────────────────────────── настоящее обновление ─────────────────────────────

def test_engine_marker_matches_plugin_after_upgrade(project: Path, hermes_stub: Path,
                                                    tmp_path: Path, monkeypatch):
    """Настоящий `upgrade`: копия движка и маркер сходятся с плагином (D6, D9).

    Перед проходом `.pipeline/gate.py` испорчен: иначе копия уже совпадала бы с плагином
    и проверка «маркер сходится» ничего не доказывала. Заодно проверяется, что конфиг
    проекта не тронут и cron-скрипты легли под подмену профиля (D10).
    """
    fake_hermes(monkeypatch)
    home = isolate_profile(monkeypatch, tmp_path)
    gate = project / ".pipeline" / "gate.py"
    gate.write_text(gate.read_text(encoding="utf-8") + "\n# порча\n", encoding="utf-8")
    config = (project / "pipeline.json").read_bytes()
    write_registry([project])

    result = rollout.run_engine(proj.read_projects(), interval=0.01, limit=1.0)

    assert engine_row(project)["status"] == "updated"
    assert result["ok"] is True
    source = (proj.ENGINE_SRC / "gate.py").read_bytes()
    assert gate.read_bytes() == source
    marker = (project / ".pipeline" / "ENGINE.sha256").read_text(encoding="utf-8")
    digest = setup_flow.engine_digest(project / ".pipeline")
    assert marker.strip() == f"sha256:{digest}"
    assert digest == setup_flow.engine_digest(proj.ENGINE_SRC)
    assert setup_flow.engine_marker_problem(project) == ""
    assert (project / VERSION).read_text(encoding="utf-8").strip() == setup_flow.plugin_version()
    assert (project / "pipeline.json").read_bytes() == config
    scripts = home / "demo" / "scripts"
    assert (scripts / "pipeline_tick.py").is_file()
    assert (scripts / "pipeline_update.py").is_file()


# ───────────────────────────── отказы маршрута ─────────────────────────────

def test_rollout_route_guards_refuse_without_side_effects(project: Path, hermes_stub: Path,
                                                          monkeypatch):
    """Предпросмотр и второй жест: 403 и 409, в реестре ни строки (D11, D5).

    Успешный жест пошёл (200), но `_upgrade` держится на событии: пока он не отпущен,
    второй POST обязан получить `busy`. Затем та же страница в предпросмотре — 403 и
    байты реестра те же, что до него.
    """
    release = threading.Event()
    entered = threading.Event()
    calls: list[Path] = []

    def blocking_upgrade(project_path, log):
        calls.append(Path(project_path))
        entered.set()
        assert release.wait(10), "раскатка не дождалась разрешения теста"
        (Path(project_path) / VERSION).write_text("9.9.9\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(rollout, "_upgrade", blocking_upgrade)
    write_registry([project])
    server = start_server(project)
    try:
        status, data = post(server["url"], {})
        assert status == 200
        assert data["step"] == "engine" and data["running"] is True
        assert entered.wait(10), "фоновая раскатка не дошла до upgrade"
        busy_status, busy = post(server["url"], {})
        assert busy_status == 409
        assert busy["busy"] is True and busy["ok"] is False
    finally:
        release.set()
        wait_for_release()
        stop_server(server)

    assert rollout.busy() is False
    assert [path.name for path in calls] == [project.name]
    row = engine_row(project)
    assert row["status"] == "updated" and row["to"] == "9.9.9"
    assert engine_entry()["running"] is False and engine_entry()["ok"] is True

    snapshot = proj.projects_file().read_bytes()
    preview = start_server(project, preview=True)
    try:
        code, _data = post(preview["url"], {})
    finally:
        stop_server(preview)
    assert code == 403
    assert proj.projects_file().read_bytes() == snapshot
    assert [path.name for path in calls] == [project.name]
