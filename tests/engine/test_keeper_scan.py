# -*- coding: utf-8 -*-
"""Тесты сканера симптомов конвейера (план 10, S2).

Каждый вид симптома доказывается парой «вход → исход», а не присутствием в словаре:
на засорённом проекте находится ровно свой симптом, на чистом — ни одного. Возраст
считается от фиксированного `NOW`, а не от текущих часов: иначе тест флакует на
границе суток. Внешние команды (`git`, `hermes`) и живость панели подменяются.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine_kit import ENGINE  # noqa: F401 - фикстура `project` приходит из conftest

sys.path.insert(0, str(ENGINE))

import config as config_mod  # noqa: E402 - движок на sys.path
import gate  # noqa: E402 - источник паритета engine_digest
import keeper_scan  # noqa: E402
import state_io  # noqa: E402

#: Фиксированный момент замера: 20.11.2026, 12:00 MSK.
NOW = 1795164000.0

#: Схема «живой» борды: в поддельной борде тестов нет `last_heartbeat_at`,
#: `task_events` и `task_runs`, а источнику нужны именно они.
BOARD_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT,
                    created_at INTEGER, started_at INTEGER, completed_at INTEGER,
                    last_heartbeat_at INTEGER, idempotency_key TEXT, tenant TEXT);
CREATE TABLE task_events (task_id TEXT, run_id TEXT, kind TEXT, payload TEXT,
                          created_at INTEGER);
CREATE TABLE task_runs (task_id TEXT, status TEXT, last_heartbeat_at INTEGER,
                        started_at INTEGER, ended_at INTEGER);
"""


# ───────────────────────────── помощники ─────────────────────────────

@pytest.fixture(autouse=True)
def isolated_board(tmp_path, monkeypatch):
    """Борда и состояние — только свои: настоящая база тестам не нужна."""
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(tmp_path / "no-board.db"))
    for name in ("PIPELINE_REPO", "PIPELINE_STATE_DIR", "PIPELINE_CONFIG", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "no-board.db"


class FakeRun:
    """Подмена `run`: вывод cron и git задаётся тестом, команды записываются."""

    def __init__(self, cron_text: str = "", git_out: str = "", cron_rc: int = 0,
                 git_rc: int = 0) -> None:
        self.cron_text = cron_text
        self.git_out = git_out
        self.cron_rc = cron_rc
        self.git_rc = git_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float):
        self.calls.append(list(cmd))
        if "cron" in cmd:
            return SimpleNamespace(returncode=self.cron_rc, stdout=self.cron_text, stderr="")
        if "git" in cmd:
            return SimpleNamespace(returncode=self.git_rc, stdout=self.git_out, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeProbe:
    """Подмена проверки живости панели: ответ задаётся тестом."""

    def __init__(self, alive: bool) -> None:
        self.alive = alive
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.calls.append((host, port))
        return self.alive


def scan_project(project: Path, *, plugin_dir=None, run=None, probe=None,
                 now: float = NOW) -> dict:
    """Просканировать проект через настоящий `Config`, подменив внешние зависимости."""
    cfg = config_mod.load(project)
    return keeper_scan.scan(cfg, plugin_dir=plugin_dir, now=now,
                            run=run if run is not None else FakeRun(),
                            probe=probe if probe is not None else FakeProbe(False),
                            hermes="hermes")


def kinds(result: dict) -> list[str]:
    return [item["kind"] for item in result["symptoms"]]


def source_line(result: dict, name: str) -> dict:
    return next(item for item in result["sources"] if item["name"] == name)


def make_board(db: Path, cards: list[dict], events: list[dict] | None = None) -> Path:
    """Записать борду «как живую»: схема задач, событий и карточки."""
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(BOARD_SCHEMA)
        for card in cards:
            row = {"id": card["id"], "title": card.get("title", "карточка"),
                   "status": card.get("status", "todo"), "assignee": "demo",
                   "created_at": card.get("created_at", int(NOW)),
                   "started_at": card.get("started_at", 0),
                   "completed_at": 0,
                   "last_heartbeat_at": card.get("last_heartbeat_at", 0),
                   "idempotency_key": card.get("key", ""), "tenant": "plan"}
            con.execute("INSERT INTO tasks (" + ", ".join(row) + ") VALUES ("
                        + ", ".join("?" * len(row)) + ")", tuple(row.values()))
        for event in events or []:
            con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (event["task_id"], "r1", "heartbeat", "", event["created_at"]))
        con.commit()
    finally:
        con.close()
    return db


def write_marker(project: Path) -> Path:
    """Маркер целостности копии движка — так, как его пишет мастер установки."""
    path = Path(project) / ".pipeline" / "ENGINE.sha256"
    digest = keeper_scan.engine_digest(Path(project) / ".pipeline")
    path.write_text(f"sha256:{digest}\n", encoding="utf-8")
    return path


def make_package(root: Path, project: Path, version: str = "0.37.0",
                 roles: tuple[str, ...] = ()) -> Path:
    """Собрать поддельный пакет плагина: движок, `plugin.yaml`, каталоги ролей."""
    package = Path(root) / "step_pipeline"
    engine = package / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    for name in config_mod.ENGINE_FILES:
        shutil.copy2(Path(project) / ".pipeline" / name, engine / name)
    (package / "plugin.yaml").write_text(
        f"name: step_pipeline\nversion: {version}\n", encoding="utf-8")
    for role in roles:
        role_dir = package / "roles" / role
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "SKILL.md").write_text(f"# роль {role}\nверсия пакета\n", encoding="utf-8")
    return package


def write_version(project: Path, version: str) -> Path:
    path = Path(project) / ".pipeline" / "VERSION"
    path.write_text(version + "\n", encoding="utf-8")
    return path


def write_cron_marker(project: Path, **payload) -> Path:
    path = Path(project) / ".pipeline" / "cron.json"
    data = {"driver": "demo", "name": "План: страж шага", "workdir": str(project),
            "skipped": False}
    data.update(payload)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def write_stop_flag(project: Path, plan: str, age_minutes: float,
                    reason: str = "остановка владельцем") -> Path:
    path = Path(project) / ".pipeline" / "state" / f"plan_{plan}_stop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"reason": reason, "source": "owner", "kind": "owner",
                                "at": "19.11.2026 18:50:53"}, ensure_ascii=False),
                    encoding="utf-8")
    stamp = NOW - age_minutes * 60
    os.utime(path, (stamp, stamp))
    return path


def write_lock(project: Path, pid: int) -> Path:
    path = Path(project) / ".pipeline" / "state" / state_io.LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "host": "host", "at": "19.11.2026 18:50:53",
                                "what": "tick"}), encoding="utf-8")
    return path


def write_ui(project: Path, pid: int, port: int = 8770) -> Path:
    path = Path(project) / ".pipeline" / "ui.json"
    path.write_text(json.dumps({"pid": pid, "port": port, "host": "127.0.0.1",
                                "url": f"http://127.0.0.1:{port}"}), encoding="utf-8")
    return path


def set_watchdog(project: Path, **keys) -> Path:
    path = Path(project) / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["watchdog"] = keys
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cron_output(blocks: list[dict]) -> str:
    """Вывод `hermes cron incidents list` в том виде, в каком его печатает ядро."""
    lines = ["Cron failure incidents:", ""]
    for block in blocks:
        lines.append(f"  {block['id']}  {block.get('state', 'detected')}")
        lines.append(f"    Job: {block.get('job', 'План: страж шага')}")
        lines.append("    Type: agent")
        lines.append(f"    First seen: {block.get('first_seen', block['last_seen'])}")
        lines.append(f"    Last seen: {block['last_seen']}")
        lines.append(f"    Error: {block.get('error', 'agent exited 1')}")
        lines.append("")
    lines.append(f"  {len(blocks)} incident(s)  |  ack one with: hermes cron incidents ack <id>")
    return "\n".join(lines) + "\n"


def iso(hours_ago: float) -> str:
    """Время ISO столько-то часов назад от `NOW` — как `datetime.isoformat()`."""
    return datetime.fromtimestamp(NOW - hours_ago * 3600).isoformat()


# ───────────────────────────── чистый проект ─────────────────────────────

def test_clean_project_has_no_symptoms(project: Path) -> None:
    result = scan_project(project)
    assert result["symptoms"] == []
    statuses = {item["name"]: item["status"] for item in result["sources"]}
    assert set(statuses.values()) <= {"ok", "skipped"}
    assert statuses["cron"] == "skipped"      # метки cron нет
    assert statuses["board"] == "skipped"     # базы нет


# ───────────────────────────── карточки ─────────────────────────────

def test_stuck_card_is_found(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_stuck1", "status": "todo",
                                 "created_at": int(NOW - 31 * 3600)}])
    result = scan_project(project)
    assert kinds(result) == ["card_stuck"]
    symptom = result["symptoms"][0]
    assert symptom["severity"] == "high"
    assert symptom["address"] == "t_stuck1"
    assert symptom["healable"] is False


def test_running_card_with_fresh_heartbeat_is_not_stuck(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_live", "status": "running",
                                 "created_at": int(NOW - 40 * 3600),
                                 "started_at": int(NOW - 40 * 3600),
                                 "last_heartbeat_at": int(NOW - 5 * 60)}])
    result = scan_project(project)
    assert result["symptoms"] == []
    assert source_line(result, "board")["status"] == "ok"


def test_running_card_with_dead_heartbeat_is_stuck(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_dead", "status": "running",
                                 "created_at": int(NOW - 20 * 3600),
                                 "started_at": int(NOW - 20 * 3600),
                                 "last_heartbeat_at": int(NOW - 13 * 3600)}])
    result = scan_project(project)
    assert kinds(result) == ["card_stuck"]
    assert "13.0 ч" in result["symptoms"][0]["evidence"]


def test_terminal_card_is_ignored(project: Path, isolated_board) -> None:
    make_board(isolated_board, [
        {"id": "t_done", "status": "done", "created_at": int(NOW - 100 * 3600)},
        {"id": "t_arch", "status": "archived", "created_at": int(NOW - 100 * 3600)},
    ])
    assert scan_project(project)["symptoms"] == []


def test_card_age_uses_task_events(project: Path, isolated_board) -> None:
    """Возраст — от последнего события борды, а не от создания карточки."""
    make_board(isolated_board,
               [{"id": "t_events", "status": "running", "created_at": int(NOW - 30 * 3600)}],
               events=[{"task_id": "t_events", "created_at": int(NOW - 10 * 60)}])
    assert scan_project(project)["symptoms"] == []


def test_deadline_zero_disables_card_check(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_old", "status": "todo",
                                 "created_at": int(NOW - 100 * 3600)}])
    set_watchdog(project, card_deadline_hours=0)
    result = scan_project(project)
    assert result["symptoms"] == []
    assert source_line(result, "board")["status"] == "skipped"


def test_broken_board_is_source_error_not_symptom(project: Path, isolated_board) -> None:
    """База есть, а схемы борды в ней нет: ошибка источника, а не выдуманная находка."""
    isolated_board.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(isolated_board).close()      # пустая база без таблицы tasks
    result = scan_project(project)
    assert result["symptoms"] == []
    assert source_line(result, "board")["status"] == "error"


# ───────────────────────────── стоп-флаги ─────────────────────────────

def test_old_stop_flag_is_found(project: Path) -> None:
    write_stop_flag(project, "7", age_minutes=90)
    result = scan_project(project)
    assert kinds(result) == ["stop_stale"]
    assert result["symptoms"][0]["address"] == "7"


def test_fresh_stop_flag_is_silent(project: Path) -> None:
    write_stop_flag(project, "7", age_minutes=5)
    assert scan_project(project)["symptoms"] == []


def test_missing_state_dir_is_skipped(project: Path) -> None:
    """Каталога состояния нет — источник помечается, а не падает и не шумит."""
    shutil.rmtree(Path(project) / ".pipeline" / "state")
    result = scan_project(project)
    assert result["symptoms"] == []
    assert source_line(result, "stop_flags")["status"] == "skipped"


# ───────────────────────────── замок тика ─────────────────────────────

def test_dead_tick_lock_is_found(project: Path, monkeypatch) -> None:
    write_lock(project, 999999)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: False)
    result = scan_project(project)
    assert kinds(result) == ["tick_lock_dead"]
    assert "999999" in result["symptoms"][0]["evidence"]
    assert result["symptoms"][0]["healable"] is True


def test_live_tick_lock_is_silent(project: Path, monkeypatch) -> None:
    write_lock(project, 4242)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: True)
    result = scan_project(project)
    assert result["symptoms"] == []
    assert source_line(result, "tick_lock")["status"] == "ok"


# ───────────────────────────── панель ─────────────────────────────

def test_panel_with_dead_pid_is_found(project: Path, monkeypatch) -> None:
    write_ui(project, 31337)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: False)
    result = scan_project(project)
    assert kinds(result) == ["panel_down"]
    assert result["symptoms"][0]["severity"] == "low"


def test_panel_alive_but_port_silent_is_found(project: Path, monkeypatch) -> None:
    write_ui(project, 4242, port=8771)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: True)
    probe = FakeProbe(False)
    result = scan_project(project, probe=probe)
    assert kinds(result) == ["panel_down"]
    assert "8771" in result["symptoms"][0]["evidence"]
    assert probe.calls == [("127.0.0.1", 8771)]


def test_panel_up_is_silent(project: Path, monkeypatch) -> None:
    write_ui(project, 4242)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: True)
    result = scan_project(project, probe=FakeProbe(True))
    assert result["symptoms"] == []
    assert source_line(result, "panel")["status"] == "ok"


# ───────────────────────────── cron ─────────────────────────────

def test_cron_incident_within_window_is_found(project: Path) -> None:
    write_cron_marker(project)
    run = FakeRun(cron_text=cron_output([{"id": "0a1b2c3d", "last_seen": iso(2)}]))
    result = scan_project(project, run=run)
    assert kinds(result) == ["cron_incident"]
    assert result["symptoms"][0]["address"] == "0a1b2c3d"
    assert result["symptoms"][0]["severity"] == "high"
    assert ["hermes", "-p", "demo", "cron", "incidents", "list"] in run.calls


def test_cron_incident_older_than_window_is_silent(project: Path) -> None:
    write_cron_marker(project)
    run = FakeRun(cron_text=cron_output([{"id": "0a1b2c3d", "last_seen": iso(30)}]))
    assert scan_project(project, run=run)["symptoms"] == []


def test_cron_closed_and_resolved_are_silent(project: Path) -> None:
    write_cron_marker(project)
    run = FakeRun(cron_text=cron_output([
        {"id": "0a1b2c3d", "state": "closed", "last_seen": iso(1)},
        {"id": "0a1b2c3e", "state": "resolved", "last_seen": iso(1)},
    ]))
    assert scan_project(project, run=run)["symptoms"] == []


def test_cron_unavailable_is_source_status_not_symptom(project: Path) -> None:
    write_cron_marker(project)
    result = scan_project(project, run=FakeRun(cron_rc=1))
    assert result["symptoms"] == []
    line = source_line(result, "cron")
    assert line["status"] == "error"
    assert "1" in line["detail"]


def test_cron_marker_skipped_is_source_skipped(project: Path) -> None:
    write_cron_marker(project, skipped=True)
    run = FakeRun(cron_text=cron_output([{"id": "0a1b2c3d", "last_seen": iso(1)}]))
    result = scan_project(project, run=run)
    assert result["symptoms"] == []
    assert source_line(result, "cron")["status"] == "skipped"
    assert not [cmd for cmd in run.calls if "cron" in cmd]


def test_cron_unparsed_time_still_reports(project: Path) -> None:
    """Время не разобралось — симптом всё равно есть: молчание хуже лишней строки."""
    write_cron_marker(project)
    run = FakeRun(cron_text=cron_output([{"id": "0a1b2c3d", "last_seen": "позавчера"}]))
    result = scan_project(project, run=run)
    assert kinds(result) == ["cron_incident"]
    assert "время не разобрано" in result["symptoms"][0]["evidence"]


# ───────────────────────────── git ─────────────────────────────

def test_dirty_git_tree_is_found(project: Path) -> None:
    result = scan_project(project, run=FakeRun(git_out=" M README.md\n?? new.py\n"))
    assert kinds(result) == ["git_dirty"]
    evidence = result["symptoms"][0]["evidence"]
    assert "README.md" in evidence and "new.py" in evidence
    assert result["symptoms"][0]["severity"] == "low"


def test_git_unavailable_is_skipped(project: Path) -> None:
    result = scan_project(project, run=FakeRun(git_rc=128))
    assert result["symptoms"] == []
    assert source_line(result, "git")["status"] == "skipped"


# ───────────────────────────── движок ─────────────────────────────

def test_engine_marker_mismatch_is_drift(project: Path) -> None:
    write_marker(project)
    target = Path(project) / ".pipeline" / "keeper_scan.py"
    target.write_bytes(target.read_bytes() + "\n# правка после установки\n".encode("utf-8"))
    result = scan_project(project)
    assert kinds(result) == ["engine_drift"]
    evidence = result["symptoms"][0]["evidence"]
    assert "движок изменён после установки" in evidence
    assert evidence.count("!=") == 1


def test_engine_digest_matches_gate_digest(project: Path) -> None:
    base = Path(project) / ".pipeline"
    assert keeper_scan.engine_digest(base) == gate.engine_digest(base)


def test_engine_missing_file_is_drift(project: Path) -> None:
    """Маркер сходится, а файла движка нет: `engine_digest` такое пропускает."""
    (Path(project) / ".pipeline" / "scout_report.py").unlink()
    write_marker(project)
    result = scan_project(project)
    assert kinds(result) == ["engine_drift"]
    assert "scout_report.py" in result["symptoms"][0]["evidence"]


def test_package_engine_drift_is_found(project: Path, tmp_path: Path) -> None:
    package = make_package(tmp_path / "pkg", project, version="0.37.0")
    target = package / "engine" / "gate.py"
    target.write_bytes(target.read_bytes() + "\n# пакет новее копии\n".encode("utf-8"))
    result = scan_project(project, plugin_dir=package)
    assert kinds(result) == ["engine_drift"]
    assert "gate.py" in result["symptoms"][0]["evidence"]


def test_matching_package_is_silent(project: Path, tmp_path: Path) -> None:
    package = make_package(tmp_path / "pkg", project, version="0.37.0",
                           roles=("pipeline-keeper",))
    write_version(project, "0.37.0")
    role_dir = Path(project) / ".hermes" / "skills" / "software-development" / "pipeline-keeper"
    role_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package / "roles" / "pipeline-keeper" / "SKILL.md", role_dir / "SKILL.md")
    result = scan_project(project, plugin_dir=package)
    assert result["symptoms"] == []
    assert {item["name"]: item["status"] for item in result["sources"]}["skills"] == "ok"


# ───────────────────────────── версия и роли ─────────────────────────────

def test_package_version_skew_is_found(project: Path, tmp_path: Path) -> None:
    package = make_package(tmp_path / "pkg", project, version="0.37.0")
    write_version(project, "0.36.0")
    result = scan_project(project, plugin_dir=package)
    assert kinds(result) == ["version_skew"]
    evidence = result["symptoms"][0]["evidence"]
    assert "0.36.0" in evidence and "0.37.0" in evidence


def test_skills_drift_is_found(project: Path, tmp_path: Path) -> None:
    package = make_package(tmp_path / "pkg", project,
                           roles=("pipeline-keeper", "pipeline-scout"))
    write_version(project, "0.37.0")
    installed = Path(project) / ".hermes" / "skills" / "software-development" / "pipeline-keeper"
    installed.mkdir(parents=True, exist_ok=True)
    (installed / "SKILL.md").write_text("# старая копия\n", encoding="utf-8")
    result = scan_project(project, plugin_dir=package)
    assert kinds(result) == ["skills_drift", "skills_drift"]
    assert sorted(item["address"] for item in result["symptoms"]) == [
        "pipeline-keeper", "pipeline-scout"]


def test_no_plugin_dir_skips_package_sources(project: Path) -> None:
    result = scan_project(project, plugin_dir=None)
    assert result["symptoms"] == []
    for name in ("version", "skills"):
        line = source_line(result, name)
        assert line["status"] == "skipped"
        assert "--plugin-dir" in line["detail"]


# ───────────────────────────── контракт вывода ─────────────────────────────

def test_symptom_id_is_stable_and_address_sensitive(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_one", "status": "todo",
                                 "created_at": int(NOW - 40 * 3600)},
                                {"id": "t_two", "status": "todo",
                                 "created_at": int(NOW - 50 * 3600)}])
    first = scan_project(project)
    second = scan_project(project, now=NOW + 3600)
    assert [item["symptom_id"] for item in first["symptoms"]] == \
           [item["symptom_id"] for item in second["symptoms"]]
    assert len({item["symptom_id"] for item in first["symptoms"]}) == 2
    assert keeper_scan.symptom_id("demo", "card_stuck", "t_one") == \
        first["symptoms"][0]["symptom_id"]
    assert keeper_scan.symptom_id("demo", "card_stuck", "t_two") != \
        keeper_scan.symptom_id("demo", "card_stuck", "t_one")


def test_scan_does_not_write_state(project: Path, isolated_board, monkeypatch) -> None:
    make_board(isolated_board, [{"id": "t_one", "status": "todo",
                                 "created_at": int(NOW - 40 * 3600)}])
    write_stop_flag(project, "3", age_minutes=120)
    write_lock(project, 777)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: False)

    def snapshot() -> dict:
        root = Path(project)
        return {path.relative_to(root).as_posix(): (path.stat().st_size,
                                                   int(path.stat().st_mtime))
                for path in sorted(root.rglob("*")) if path.is_file()}

    before = snapshot()
    assert scan_project(project)["symptoms"]
    assert snapshot() == before


def test_json_shape_and_order_are_stable(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_one", "status": "todo",
                                 "created_at": int(NOW - 40 * 3600)}])
    write_stop_flag(project, "3", age_minutes=120)
    result = scan_project(project)
    assert set(result) == {"project", "repo", "scanned_at", "scanned_ts", "symptoms", "sources"}
    assert set(result["symptoms"][0]) == {"project", "symptom_id", "kind", "severity",
                                          "address", "evidence", "healable"}
    assert [item["kind"] for item in result["symptoms"]] == ["card_stuck", "stop_stale"]
    assert [item["name"] for item in result["sources"]] == list(keeper_scan.SOURCE_NAMES)
    assert result["scanned_ts"] == int(NOW)


def test_main_prints_json_and_returns_zero(project: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    code = keeper_scan.main(["--scan", "--json", "--project", str(project)])
    out = capsys.readouterr().out
    assert code == 0
    result = json.loads(out)
    assert result["symptoms"] == []
    assert [item["name"] for item in result["sources"]] == list(keeper_scan.SOURCE_NAMES)


def test_main_without_config_returns_two(tmp_path: Path, monkeypatch, capsys) -> None:
    empty = tmp_path / "empty-project"
    empty.mkdir()
    monkeypatch.setenv("PIPELINE_REPO", str(empty))
    code = keeper_scan.main(["--scan", "--json", "--project", str(empty)])
    assert code == 2
    assert "конфигурация не читается" in capsys.readouterr().err


def test_format_human_lists_symptoms_and_sources(project: Path, isolated_board) -> None:
    make_board(isolated_board, [{"id": "t_one", "status": "todo",
                                 "created_at": int(NOW - 40 * 3600)}])
    text = keeper_scan.format_human(scan_project(project))
    assert "Проект demo: симптомов 1" in text
    assert "high  card_stuck  t_one" in text
    assert "источники: cron=" in text


# ───────────────────────────── настройка порогов ─────────────────────────────

def test_watchdog_defaults(project: Path) -> None:
    assert config_mod.load(project).watchdog == {"heartbeat_fresh_minutes": 30,
                                                "card_deadline_hours": 12}


def test_watchdog_garbage_falls_back_with_warning(project: Path, capsys) -> None:
    set_watchdog(project, heartbeat_fresh_minutes="abc", card_deadline_hours=-1)
    cfg = config_mod.load(project)
    assert cfg.watchdog == {"heartbeat_fresh_minutes": 30, "card_deadline_hours": 12}
    err = capsys.readouterr().err
    assert "WARNING: watchdog.heartbeat_fresh_minutes" in err
    assert "WARNING: watchdog.card_deadline_hours=-1 — беру 12" in err


def test_watchdog_zero_and_numeric_string_are_legal(project: Path) -> None:
    set_watchdog(project, heartbeat_fresh_minutes="15", card_deadline_hours=0)
    cfg = config_mod.load(project)
    assert cfg.watchdog == {"heartbeat_fresh_minutes": 15, "card_deadline_hours": 0}


# ───────────────────────────── отчёт источников ─────────────────────────────

def test_dirty_project_reports_every_source(project: Path, isolated_board, tmp_path: Path,
                                            monkeypatch) -> None:
    """Находка не отменяет строку источника: молчащий источник — дефект сканера.

    Регрессия: пока источник добавлял симптом и не писал свою строку, скан на живом
    проекте отдавал `engine: error «источник не отчитался»` вместо `ok`.
    """
    make_board(isolated_board, [{"id": "t_old", "title": "старая", "status": "todo",
                                 "created_at": NOW - 31 * 3600}])
    write_stop_flag(project, "7", age_minutes=90)
    write_lock(project, 999999)
    write_ui(project, 4242)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: False)
    package = make_package(tmp_path / "pkg", project, version="9.9.9")
    write_cron_marker(project)
    run = FakeRun(cron_text=cron_output([{"id": "0a1b2c3d", "last_seen": iso(2)}]),
                  git_out="M README.md")
    result = scan_project(project, plugin_dir=package, run=run)
    assert kinds(result), "засорённый проект обязан дать находки"
    assert [item["name"] for item in result["sources"]] == list(keeper_scan.SOURCE_NAMES)
    assert not [item for item in result["sources"] if "не отчитался" in item["detail"]]
    assert all(item["status"] in {"ok", "skipped", "error"} for item in result["sources"])
