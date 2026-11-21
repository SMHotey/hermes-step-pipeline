# -*- coding: utf-8 -*-
"""Доказательство починок смотрителя и отказов (план 10, S3).

Каждый пункт «Готово =» плана закрыт прогоном: белый список (пять видов, четыре
команды) чинит, чёрный отклоняется, предохранители (`off`, живой тик, кулдаун,
лимит, `no_hermes`) стоят до действия, dry-run не оставляет ни строки журнала и ни
одного изменённого файла, а починка пишет только каталог состояния.

Помощники заведены здесь свои: модули тестов движка друг друга не импортируют
(`grep -rn '^from test_\\|^import test_' tests/engine/*.py` → пусто), а общие лежат в
`tests/engine/engine_kit.py`, где `write_lock`/`write_ui` нет.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine_kit import ENGINE  # noqa: F401 - фикстура `project` приходит из conftest

sys.path.insert(0, str(ENGINE))

import config as config_mod  # noqa: E402 - движок на sys.path
import gate  # noqa: E402 - формат реестра проверяется читателем движка (Ф7)
import keeper_heal  # noqa: E402
import keeper_scan  # noqa: E402
import state_io  # noqa: E402

NOW = 1_800_000_000.0


# ───────────────────────────── помощники файла ─────────────────────────────

class FakeRun:
    """Подмена `run`: команды и таймауты запоминаются, ответ — по подстроке команды."""

    def __init__(self, sha: str = "", codes: dict | None = None,
                 out: str = "", err: str = "") -> None:
        self.sha = sha
        self.codes = dict(codes or {})
        self.out = out
        self.err = err
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, cmd: list[str], timeout: float):
        self.calls.append(list(cmd))
        self.timeouts.append(timeout)
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0 if self.sha else 1,
                                   stdout=(self.sha + "\n") if self.sha else "",
                                   stderr="" if self.sha else "not a git repository")
        code = 0
        for marker, value in self.codes.items():
            if marker in cmd:
                code = value
        return SimpleNamespace(returncode=code, stdout=self.out, stderr=self.err)

    def commands(self, marker: str) -> list[list[str]]:
        """Запущенные команды, в которых встречается подстрока (git — не в счёт)."""
        return [cmd for cmd in self.calls if marker in cmd]


class FakeProbe:
    """Подмена проверки живости панели: очередь ответов и запись вызовов."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers) or [False]
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.calls.append((host, port))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def write_lock(project: Path, pid: int) -> Path:
    """Замок тика с заданным pid (форма из `tests/engine/test_keeper_scan.py`)."""
    path = Path(project) / ".pipeline" / "state" / state_io.LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "host": "host", "at": "21.11.2026 11:33:00",
                                "what": "tick"}), encoding="utf-8")
    return path


def write_ui(project: Path, pid: int, port: int | None = 8770) -> Path:
    """Метка запуска панели; `port=None` — метка без порта (случай сканера)."""
    data: dict = {"pid": pid, "host": "127.0.0.1"}
    if port is not None:
        data["port"] = port
        data["url"] = f"http://127.0.0.1:{port}"
    path = Path(project) / ".pipeline" / "ui.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_package(root: Path, project: Path, version: str = "0.38.0",
                 roles: tuple[str, ...] = ()) -> Path:
    """Поддельный пакет плагина: движок, `plugin.yaml`, каталоги ролей."""
    package = Path(root) / "step_pipeline"
    engine_dir = package / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    for name in config_mod.ENGINE_FILES:
        (engine_dir / name).write_bytes((Path(project) / ".pipeline" / name).read_bytes())
    (package / "plugin.yaml").write_text(f"name: step_pipeline\nversion: {version}\n",
                                         encoding="utf-8")
    for role in roles:
        role_dir = package / "roles" / role
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "SKILL.md").write_text(f"# роль {role}\nверсия пакета\n", encoding="utf-8")
    return package


def write_version(project: Path, version: str) -> Path:
    path = Path(project) / ".pipeline" / "VERSION"
    path.write_text(version + "\n", encoding="utf-8")
    return path


def set_config(project: Path, **keys) -> Path:
    """Правка `pipeline.json` проекта в тесте (порт панели и т.п.)."""
    path = Path(project) / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(keys)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def state_dir(project: Path) -> Path:
    return config_mod.load(project).state_dir


def confirm(project: Path) -> Path:
    """Подтверждение владельца: `state/keeper.on` (D5)."""
    path = keeper_heal.marker_path(state_dir(project), keeper_heal.ON_MARKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("подтверждено владельцем\n", encoding="utf-8")
    return path


def switch_off(project: Path) -> Path:
    """Kill-switch: `state/keeper.off` (D5)."""
    path = keeper_heal.marker_path(state_dir(project), keeper_heal.OFF_MARKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("смотритель остановлен\n", encoding="utf-8")
    return path


def journal_lines(project: Path) -> list[dict]:
    return keeper_heal.read_journal(state_dir(project))


def seed_journal(project: Path, lines: list) -> Path:
    """Положить строки в журнал напрямую: словари или сырой текст (мусор)."""
    path = keeper_heal.journal_path(state_dir(project))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False))
                   + "\n" for line in lines)
    path.write_text(text, encoding="utf-8")
    return path


def journal_line(symptom_id: str, *, age_minutes: float = 30, ran: bool = True,
                 result: str = "ok") -> dict:
    """Строка журнала в форме D7, «возрастом» от `NOW`."""
    return {"ts": int(NOW - age_minutes * 60), "at": "21.11.2026 10:00:00",
            "symptom_id": symptom_id, "kind": "panel_down", "action": "panel_restart",
            "ran": ran, "result": result, "detail": "", "commit": ""}


def symptom(cfg, kind: str, address: str, evidence: str = "признак") -> dict:
    return keeper_scan.make_symptom(cfg, kind, address, evidence)


def run_heal(project: Path, symptoms=None, *, dry_run=None, run=None, probe=None,
             hermes: str | None = "hermes", plugin_dir=None, now: float = NOW) -> dict:
    """`config_mod.load` плюс `keeper_heal.heal` — чтобы кейс занимал 3–6 строк."""
    cfg = config_mod.load(project)
    return keeper_heal.heal(cfg, symptoms, plugin_dir=plugin_dir, now=now,
                            run=run if run is not None else FakeRun(),
                            probe=probe if probe is not None else FakeProbe(False),
                            hermes=hermes, dry_run=dry_run)


def scan_project(project: Path, *, plugin_dir=None, run=None, probe=None,
                 now: float = NOW) -> dict:
    """Настоящий `scan` с подменёнными внешними зависимостями (цепочка «скан → починка»)."""
    cfg = config_mod.load(project)
    return keeper_scan.scan(cfg, plugin_dir=plugin_dir, now=now,
                            run=run if run is not None else FakeRun(),
                            probe=probe if probe is not None else FakeProbe(False),
                            hermes="hermes")


def item(result: dict, kind: str) -> dict:
    return next(entry for entry in result["results"] if entry["kind"] == kind)


def kinds(result: dict) -> list[str]:
    return [entry["kind"] for entry in result["results"]]


def snapshot(project: Path, package: Path | None = None) -> dict[str, str]:
    """Хеши файлов проекта вне `state/` (и пакета): что починка не имеет права менять."""
    out: dict[str, str] = {}
    for path in sorted(Path(project).rglob("*")):
        if not path.is_file() or "state" in path.relative_to(project).parts:
            continue
        out[path.relative_to(project).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if package is not None:
        for path in sorted(Path(package).rglob("*")):
            if path.is_file():
                out["pkg/" + path.relative_to(package).as_posix()] = \
                    hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture(autouse=True)
def restore_env():
    """`main()` пишет `PIPELINE_REPO` в окружение процесса — после теста вернуть как было.

    Иначе переменная утекает в следующие файлы набора: они поднимают настоящие
    подпроцессы движка, а те читают проект из окружения — прогон падает в чужом тесте.
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture(autouse=True)
def dead_pid(monkeypatch):
    """Живость pid подменена: иначе фикстура зависит от того, занят ли номер на машине."""
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: False)


@pytest.fixture(autouse=True)
def isolated_board(monkeypatch, tmp_path: Path) -> None:
    """Ни один тест не читает настоящую борду и не наследует чужое окружение."""
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "keeper-test")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for name in ("PIPELINE_KANBAN_DB", "PIPELINE_REPO", "PIPELINE_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)


# ───────────────────────────── белый список ─────────────────────────────

def test_actions_cover_every_healable_kind() -> None:
    """Таблица `ACTIONS` — ровно те виды, что сканер пометил `healable=True` (1.3)."""
    healable = {kind for kind, (_severity, flag) in keeper_scan.KIND_RULES.items() if flag}
    assert set(keeper_heal.ACTIONS) == healable
    assert set(keeper_heal.ACTIONS.values()) == {"lock_clear", "panel_restart",
                                                 "engine_upgrade"}


def test_panel_down_restarts_panel(project: Path) -> None:
    """`panel_down` в apply: одна команда `pipeline ui --detach` и строка журнала."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "panel_down", "ui", "панель не отвечает")],
                      run=run, probe=FakeProbe(True))
    entry = item(result, "panel_down")
    assert entry["status"] == "ok" and entry["action"] == "panel_restart"
    assert run.commands("ui") == [["hermes", "pipeline", "ui", "--project", str(project),
                                   "--detach", "--port", "8799", "--host", "127.0.0.1"]]
    lines = journal_lines(project)
    assert len(lines) == 1
    assert lines[0]["action"] == "panel_restart" and lines[0]["ran"] is True
    assert lines[0]["result"] == "ok"


def test_panel_restart_reports_command_failure(project: Path) -> None:
    """Код возврата не 0 — `failed`, а `detail` называет код."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")],
                      run=FakeRun(codes={"ui": 1}, err="cannot start"), probe=FakeProbe(True))
    entry = item(result, "panel_down")
    assert entry["status"] == "failed"
    assert "1" in entry["detail"] and "cannot start" in entry["detail"]
    assert journal_lines(project)[0]["result"] == "failed"


def test_panel_restart_reports_silent_panel(project: Path) -> None:
    """Код 0, а проба молчит — `failed`: журнал не имеет права соврать (риск 5)."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], probe=FakeProbe(False))
    entry = item(result, "panel_down")
    assert entry["status"] == "failed"
    assert "не отвечает" in entry["detail"]


def test_panel_without_port_uses_config_port(project: Path) -> None:
    """Порт без `ui.json`-порта: `ui_port` конфига, затем умолчание 8770 (1.4)."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=None)
    confirm(project)
    probe = FakeProbe(True)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], run=run, probe=probe)
    assert item(result, "panel_down")["status"] == "ok"
    assert "--port" in run.commands("ui")[0]
    assert run.commands("ui")[0][run.commands("ui")[0].index("--port") + 1] == "8770"
    assert probe.calls == [("127.0.0.1", 8770)]

    set_config(project, ui_port=8791)
    write_ui(project, 31337, port=None)
    probe = FakeProbe(True)
    run = FakeRun()
    result = run_heal(project, [symptom(config_mod.load(project), "panel_down", "ui")],
                      run=run, probe=probe, now=NOW + 8000)
    assert item(result, "panel_down")["status"] == "ok"
    command = run.commands("ui")[0]
    assert command[command.index("--port") + 1] == "8791"
    assert probe.calls == [("127.0.0.1", 8791)]


def test_dead_lock_is_removed(project: Path) -> None:
    """`tick_lock_dead` в apply: замок снят, `holder` пуст, в журнале `lock_clear`."""
    cfg = config_mod.load(project)
    path = write_lock(project, 999999)
    confirm(project)
    result = run_heal(project, [symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME)])
    entry = item(result, "tick_lock_dead")
    assert entry["status"] == "ok" and entry["command"] == ""
    assert not path.exists()
    assert state_io.holder(state_dir(project)) is None
    lines = journal_lines(project)
    assert [line["action"] for line in lines] == ["lock_clear"]
    assert lines[0]["ran"] is True


def test_live_lock_is_not_removed(project: Path, monkeypatch) -> None:
    """Живой pid — `refused`/`live_tick`, файл на месте, журнала нет (риск 1)."""
    cfg = config_mod.load(project)
    path = write_lock(project, 999999)
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: True)
    confirm(project)
    result = run_heal(project, [symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME)])
    entry = item(result, "tick_lock_dead")
    assert entry["status"] == "refused" and entry["reason"] == keeper_heal.REASON_LIVE_TICK
    assert path.exists()
    assert journal_lines(project) == []


def test_lock_retaken_between_scan_and_heal_is_kept(project: Path, monkeypatch) -> None:
    """Замок взят между сканом и починкой — снимается не он (риск 4)."""
    write_lock(project, 999999)
    scanned = scan_project(project)
    assert [entry["kind"] for entry in scanned["symptoms"]] == ["tick_lock_dead"]
    path = Path(project) / ".pipeline" / "state" / state_io.LOCK_NAME
    monkeypatch.setattr(state_io, "_pid_alive", lambda pid: True)   # тик уже взял замок
    confirm(project)
    result = run_heal(project, scanned["symptoms"])
    entry = item(result, "tick_lock_dead")
    assert entry["status"] == "refused" and entry["reason"] == keeper_heal.REASON_LIVE_TICK
    assert path.exists()


def test_scan_panel_down_then_heal_restarts_panel(project: Path) -> None:
    """Цепочка «скан панели → починка» без ручной сборки симптома."""
    write_ui(project, 31337, port=8799)
    scanned = scan_project(project)
    assert [entry["kind"] for entry in scanned["symptoms"]] == ["panel_down"]
    confirm(project)
    probe = FakeProbe(True)
    result = run_heal(project, scanned["symptoms"], probe=probe)
    entry = item(result, "panel_down")
    assert entry["status"] == "ok" and entry["action"] == "panel_restart"
    assert probe.calls == [("127.0.0.1", 8799)]


def test_scan_version_skew_then_heal_upgrades(project: Path) -> None:
    """Цепочка «скан версии → починка»: симптом даёт скан, команду — движок."""
    package = make_package(Path(project).parent / "pkg", project, version="0.39.0")
    write_version(project, "0.37.4")
    scanned = scan_project(project, plugin_dir=package)
    assert [entry["kind"] for entry in scanned["symptoms"]] == ["version_skew"]
    confirm(project)
    run = FakeRun()
    result = run_heal(project, scanned["symptoms"], run=run, plugin_dir=package)
    assert item(result, "version_skew")["status"] == "ok"
    assert len(run.commands("--upgrade")) == 1


def test_blacklisted_kinds_are_refused(project: Path) -> None:
    """Виды вне `ACTIONS` — `refused`/`not_in_whitelist`, ни команд, ни журнала."""
    cfg = config_mod.load(project)
    confirm(project)
    symptoms = [symptom(cfg, "git_dirty", "repo", "в дереве правки"),
                symptom(cfg, "stop_stale", "plan_1", "стоп висит"),
                symptom(cfg, "cron_incident", "job", "cron падает")]
    run = FakeRun()
    result = run_heal(project, symptoms, run=run)
    assert {entry["status"] for entry in result["results"]} == {"refused"}
    assert {entry["reason"] for entry in result["results"]} == {keeper_heal.REASON_BLACKLIST}
    assert {entry["action"] for entry in result["results"]} == {""}
    assert {entry["command"] for entry in result["results"]} == {""}
    assert run.calls == []
    assert journal_lines(project) == []


def test_engine_drift_and_version_skew_share_one_upgrade(project: Path) -> None:
    """Два вида — одна команда: первая строка журнала `ran: true`, вторая `ran: false` (D7)."""
    cfg = config_mod.load(project)
    confirm(project)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "version_skew", "version", "0.37.4 < 0.38.0"),
                                symptom(cfg, "engine_drift", "engine", "файлы не совпали")],
                      run=run, plugin_dir=make_package(project.parent / "pkg", project))
    assert [entry["status"] for entry in result["results"]] == ["ok", "ok"]
    assert len(run.commands("setup")) == 1
    lines = journal_lines(project)
    assert [line["kind"] for line in lines] == ["engine_drift", "version_skew"]
    assert [line["ran"] for line in lines] == [True, False]
    assert {line["result"] for line in lines} == {"ok"}
    assert "тем же обновлением" in lines[1]["detail"]


def test_skills_drift_for_two_roles_shares_one_upgrade(project: Path) -> None:
    """Две разошедшиеся роли — то же одно обновление, две строки журнала."""
    cfg = config_mod.load(project)
    confirm(project)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "skills_drift", "pipeline-keeper", "скилл старее"),
                                symptom(cfg, "skills_drift", "pipeline-reviewer", "скилл старее")],
                      run=run, plugin_dir=make_package(project.parent / "pkg", project))
    assert [entry["status"] for entry in result["results"]] == ["ok", "ok"]
    assert len(run.commands("setup")) == 1
    lines = journal_lines(project)
    assert [line["ran"] for line in lines] == [True, False]
    assert {line["action"] for line in lines} == {"engine_upgrade"}


# ───────────────────────────── предохранители ─────────────────────────────

def test_kill_switch_refuses_everything(project: Path) -> None:
    """`keeper.off` сильнее `keeper.on`: режим `off`, всё `refused`/`off`, ни команд (D5)."""
    cfg = config_mod.load(project)
    switch_off(project)
    confirm(project)
    path = write_lock(project, 999999)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME),
                                symptom(cfg, "panel_down", "ui")], run=run)
    assert result["mode"] == "off"
    assert {entry["status"] for entry in result["results"]} == {"refused"}
    assert {entry["reason"] for entry in result["results"]} == {keeper_heal.REASON_OFF}
    assert run.calls == []
    assert path.exists()
    assert journal_lines(project) == []


def test_dry_run_by_default_changes_nothing(project: Path) -> None:
    """Без `keeper.on` — `dry-run`, `planned`, замок цел, журнала нет, `pipeline.json` тот же."""
    cfg = config_mod.load(project)
    path = write_lock(project, 999999)
    before = (Path(project) / "pipeline.json").read_bytes()
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME)], run=run)
    assert result["mode"] == "dry-run" and result["dry_run"] is True
    assert [entry["status"] for entry in result["results"]] == ["planned"]
    assert path.exists()
    assert not keeper_heal.journal_path(state_dir(project)).exists()
    assert run.calls == []
    assert (Path(project) / "pipeline.json").read_bytes() == before


def test_owner_confirmation_switches_to_apply(project: Path) -> None:
    """`keeper.on` включает apply: команда запущена, журнал написан."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], run=run,
                      probe=FakeProbe(True))
    assert result["mode"] == "apply" and result["dry_run"] is False
    assert item(result, "panel_down")["status"] == "ok"
    assert len(run.commands("ui")) == 1
    assert len(journal_lines(project)) == 1


def test_dry_run_flag_wins_over_confirmation(project: Path) -> None:
    """Флаг `dry_run=True` сильнее файла `keeper.on`: `planned`, ничего не запущено."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    run = FakeRun()
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], dry_run=True, run=run,
                      probe=FakeProbe(True))
    assert result["mode"] == "dry-run"
    assert [entry["status"] for entry in result["results"]] == ["planned"]
    assert run.calls == []
    assert journal_lines(project) == []


def test_missing_cli_refuses_only_command_actions(project: Path, monkeypatch,
                                                  tmp_path: Path) -> None:
    """Без CLI: действие с командой — `no_hermes`, снятие замка — `ok`; в dry-run — `planned`."""
    cfg = config_mod.load(project)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    write_ui(project, 31337, port=8799)
    write_lock(project, 999999)
    confirm(project)
    symptoms = [symptom(cfg, "panel_down", "ui"),
                symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME)]
    result = run_heal(project, symptoms, hermes=None)
    assert item(result, "panel_down")["status"] == "refused"
    assert item(result, "panel_down")["reason"] == keeper_heal.REASON_NO_HERMES
    assert item(result, "tick_lock_dead")["status"] == "ok"

    write_lock(project, 999999)
    planned = run_heal(project, symptoms, hermes=None, dry_run=True, now=NOW + 8000)
    assert {entry["status"] for entry in planned["results"]} == {"planned"}


def test_hour_limit_stops_fourth_heal(project: Path) -> None:
    """Три `ran: true`/`ok` за последние минуты — `refused`/`limit`; старше часа — `ok`."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    seed_journal(project, [journal_line("иное-1", age_minutes=10),
                           journal_line("иное-2", age_minutes=20),
                           journal_line("иное-3", age_minutes=30)])
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], probe=FakeProbe(True))
    assert item(result, "panel_down")["status"] == "refused"
    assert item(result, "panel_down")["reason"] == keeper_heal.REASON_LIMIT

    seed_journal(project, [journal_line(f"иное-{index}", age_minutes=70 + index)
                           for index in range(3)])
    write_ui(project, 31337, port=8799)
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], probe=FakeProbe(True))
    assert item(result, "panel_down")["status"] == "ok"


def test_cooldown_blocks_same_symptom(project: Path) -> None:
    """Строка по тому же `symptom_id` 30 минут назад — `cooldown`; 121 минуту — `ok`."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    same = keeper_scan.symptom_id("demo", "panel_down", "ui")
    seed_journal(project, [journal_line(same, age_minutes=30, ran=False)])
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], probe=FakeProbe(True))
    assert item(result, "panel_down")["status"] == "refused"
    assert item(result, "panel_down")["reason"] == keeper_heal.REASON_COOLDOWN

    seed_journal(project, [journal_line(same, age_minutes=121, ran=False)])
    write_ui(project, 31337, port=8799)
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], probe=FakeProbe(True))
    assert item(result, "panel_down")["status"] == "ok"


def test_broken_journal_line_is_skipped(project: Path) -> None:
    """Мусор в журнале не роняет прогон, остаётся на месте, новая строка дописана (риск 6)."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    path = seed_journal(project, ["это не json вообще",
                                  journal_line("иное", age_minutes=10)])
    result = run_heal(project, [symptom(cfg, "panel_down", "ui")], probe=FakeProbe(True))
    assert item(result, "panel_down")["status"] == "ok"
    text = path.read_text(encoding="utf-8")
    assert "это не json вообще" in text
    assert len([line for line in text.splitlines() if line.strip()]) == 3
    assert len(journal_lines(project)) == 2


def test_journal_commit_is_project_head(project: Path) -> None:
    """`commit` — HEAD проекта из подменяемого `run`; git упал — пустая строка."""
    cfg = config_mod.load(project)
    write_ui(project, 31337, port=8799)
    confirm(project)
    run_heal(project, [symptom(cfg, "panel_down", "ui")], run=FakeRun(sha="abc1234"),
             probe=FakeProbe(True))
    assert journal_lines(project)[0]["commit"] == "abc1234"

    write_ui(project, 31337, port=8799)
    seed_journal(project, [])
    run_heal(project, [symptom(cfg, "panel_down", "ui")], run=FakeRun(sha=""),
             probe=FakeProbe(True), now=NOW + 8000)
    assert journal_lines(project)[0]["commit"] == ""


def test_heal_never_writes_outside_state(project: Path) -> None:
    """Apply по всем пяти видам: изменился только `state/keeper.jsonl` (риск 2)."""
    cfg = config_mod.load(project)
    package = make_package(project.parent / "pkg", project, roles=("pipeline-keeper",))
    write_lock(project, 999999)
    write_ui(project, 31337, port=8799)
    write_version(project, "0.37.4")
    confirm(project)
    before = snapshot(project, package)
    symptoms = [symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME),
                symptom(cfg, "panel_down", "ui"),
                symptom(cfg, "engine_drift", "engine"),
                symptom(cfg, "version_skew", "version"),
                symptom(cfg, "skills_drift", "pipeline-keeper")]
    result = run_heal(project, symptoms, plugin_dir=package, probe=FakeProbe(True))
    assert [entry["status"] for entry in result["results"]] == ["ok"] * 5
    after = snapshot(project, package)
    assert after == before
    journal = keeper_heal.journal_path(state_dir(project))
    assert journal.exists() and len(journal_lines(project)) == 5


# ───────────────────────────── отчёт и точка входа ─────────────────────────────

def test_results_are_ordered_and_counted(project: Path) -> None:
    """Порядок `high`→`normal`→`low` (внутри — вид, адрес), `counts` сходится (1.7)."""
    cfg = config_mod.load(project)
    symptoms = [symptom(cfg, "git_dirty", "repo"),
                symptom(cfg, "panel_down", "ui"),
                symptom(cfg, "version_skew", "version"),
                symptom(cfg, "tick_lock_dead", state_io.LOCK_NAME),
                symptom(cfg, "cron_incident", "job")]
    result = run_heal(project, symptoms)
    assert [entry["severity"] for entry in result["results"]] == \
        ["high", "high", "normal", "low", "low"]
    assert kinds(result) == ["cron_incident", "tick_lock_dead", "version_skew",
                             "git_dirty", "panel_down"]
    counts = result["counts"]
    assert set(counts) == set(keeper_heal.STATUSES)
    assert sum(counts.values()) == len(result["results"])
    assert counts["refused"] == 2 and counts["planned"] == 3
    assert result["journal"].endswith(".pipeline/state/keeper.jsonl")


def test_main_prints_json_and_returns_zero(project: Path, capsys) -> None:
    """`main --json` печатает схему §1.7 и возвращает 0 (починок может быть ноль)."""
    code = keeper_heal.main(["--heal", "--json", "--project", str(project)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"project", "repo", "healed_at", "healed_ts", "mode", "dry_run",
                            "results", "counts", "journal"}
    assert payload["mode"] == "dry-run"


def test_main_returns_two_without_config(tmp_path: Path, capsys) -> None:
    """Проект без `pipeline.json` — код 2 и текст причины в stderr."""
    empty = tmp_path / "empty"
    empty.mkdir()
    code = keeper_heal.main(["--heal", "--project", str(empty)])
    assert code == 2
    assert "конфигурация" in capsys.readouterr().err


def test_main_dry_run_flag_keeps_planned(project: Path, capsys) -> None:
    """`--dry-run` в CLI — принудительный dry-run даже при `keeper.on`."""
    write_ui(project, 31337, port=8799)
    confirm(project)
    code = keeper_heal.main(["--heal", "--dry-run", "--json", "--project", str(project)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert {entry["status"] for entry in payload["results"]} == {"planned"}


def test_format_human_names_mode_and_counts(project: Path) -> None:
    """Человеческий вывод: шапка с режимом и счётчиками, строка на симптом."""
    cfg = config_mod.load(project)
    result = run_heal(project, [symptom(cfg, "git_dirty", "repo")])
    text = keeper_heal.format_human(result)
    assert "Проект demo: режим dry-run" in text
    assert "refused 1" in text
    assert "git_dirty" in text and "not_in_whitelist" not in text


def test_env_is_clean_after_main(project: Path) -> None:
    """`main --project` пишет `PIPELINE_REPO`, как сканер: путь остаётся в окружении."""
    keeper_heal.main(["--json", "--project", str(project)])
    assert os.environ.get("PIPELINE_REPO") == str(Path(project).resolve())


# ─────────────────── маршрут кодового дефекта: находка вместо правки (S5) ───────────────────

CODE_ADDRESS = "step_pipeline/engine/gate.py:139"


def code_symptom(cfg, address: str = CODE_ADDRESS,
                 evidence: str = "счётчик не сбрасывается между тиками",
                 severity: str = "normal") -> dict:
    """Кодовый дефект: вид `code` сканер не производит, поля — как у симптома (Ф3, Ф7)."""
    return {"project": str(cfg.project),
            "symptom_id": keeper_scan.symptom_id(str(cfg.project), "code", address),
            "kind": "code", "severity": severity, "address": address,
            "evidence": evidence, "healable": False}


def registry_file(project: Path) -> Path:
    return keeper_heal.findings_path(state_dir(project))


def registry_rows_file(project: Path) -> list[dict]:
    """Строки реестра глазами движка: `gate.parse_findings`, без своего разбора таблицы (Ф7)."""
    path = registry_file(project)
    return gate.parse_findings(path.read_text(encoding="utf-8"), path)


def test_registry_header_copies_gate() -> None:
    """Шапка реестра — буквальная копия `gate.FINDINGS_HEADER`: иначе читатель не найдёт таблицу."""
    assert keeper_heal.FINDING_HEADER == gate.FINDINGS_HEADER
    assert keeper_heal.FINDING_CLASS in gate.FINDING_KINDS
    assert keeper_heal.FINDING_STATUS in gate.FINDING_STATUSES


def test_code_symptom_becomes_one_finding(project: Path) -> None:
    """Дефект движка — одна строка реестра формата плана, а не починка (Т1, D2–D4)."""
    cfg = config_mod.load(project)
    confirm(project)
    run = FakeRun()
    result = run_heal(project, [code_symptom(cfg)], run=run)
    entry = item(result, "code")
    assert entry["status"] == "found" and entry["action"] == keeper_heal.FINDING_ACTION
    assert entry["finding"]["id"] == "F1" and entry["finding"]["added"] is True
    assert entry["finding"]["path"].endswith("state/keeper-findings.md")
    assert result["counts"]["found"] == 1
    assert sum(result["counts"].values()) == len(result["results"])
    rows = registry_rows_file(project)
    assert len(rows) == 1
    assert rows[0]["id"] == "F1" and rows[0]["kind"] == keeper_heal.FINDING_CLASS
    assert rows[0]["status"] == keeper_heal.FINDING_STATUS and rows[0]["step"] == "—"
    assert CODE_ADDRESS in rows[0]["text"]
    assert "счётчик не сбрасывается" in rows[0]["text"]
    assert f"(симптом {code_symptom(cfg)['symptom_id']})" in rows[0]["text"]
    assert run.commands("ui") == [] and run.commands("setup") == []
    assert journal_lines(project) == []


def test_second_run_does_not_duplicate_finding(project: Path) -> None:
    """Повтор того же дефекта — прежняя строка: `added false` и тот же ID (Т2, D7)."""
    cfg = config_mod.load(project)
    confirm(project)
    first = run_heal(project, [code_symptom(cfg)])
    second = run_heal(project, [code_symptom(cfg)], now=NOW + 3600)
    entry = item(second, "code")
    assert first["counts"]["found"] == 1 and second["counts"]["found"] == 1
    assert entry["finding"]["id"] == "F1" and entry["finding"]["added"] is False
    assert len(registry_rows_file(project)) == 1


def test_dry_run_plans_code_finding_without_writing(project: Path) -> None:
    """Dry-run: `planned`, «записал бы», файла нет; повтор — «уже в реестре» (Т3, D8)."""
    cfg = config_mod.load(project)
    entry = item(run_heal(project, [code_symptom(cfg)], dry_run=True), "code")
    assert entry["status"] == "planned" and entry["finding"]["id"] == "F1"
    assert "записал бы" in entry["detail"]
    assert entry["detail"].endswith("keeper-findings.md")
    assert not registry_file(project).exists()
    confirm(project)
    run_heal(project, [code_symptom(cfg)])                 # находка уже в реестре
    again = item(run_heal(project, [code_symptom(cfg)], dry_run=True), "code")
    assert again["status"] == "planned"
    assert "уже в реестре" in again["detail"] and "F1" in again["detail"]


def test_kill_switch_refuses_code_finding(project: Path) -> None:
    """Kill-switch абсолютен и для находки: `refused`/`off`, файла нет (Т4, D8)."""
    cfg = config_mod.load(project)
    switch_off(project)
    entry = item(run_heal(project, [code_symptom(cfg)]), "code")
    assert entry["status"] == "refused" and entry["reason"] == keeper_heal.REASON_OFF
    assert not registry_file(project).exists()


@pytest.mark.parametrize("address,evidence,missing", [
    ("step_pipeline/engine/gate.py", "счётчик не сбрасывается", "путь:строка"),
    (CODE_ADDRESS, "   ", "доказательство"),
])
def test_code_finding_requires_address_and_proof(project: Path, address: str,
                                                 evidence: str, missing: str) -> None:
    """Без адреса `путь:строка` или без доказательства находки нет (Т5, D6)."""
    cfg = config_mod.load(project)
    confirm(project)
    entry = item(run_heal(project, [code_symptom(cfg, address, evidence)]), "code")
    assert entry["status"] == "refused" and entry["reason"] == keeper_heal.REASON_NO_PROOF
    assert missing in entry["detail"]
    assert not registry_file(project).exists()


def test_broken_registry_fails_without_rewrite(project: Path) -> None:
    """Битый реестр — `failed` с путём и без записи: чужие данные не перезаписываются (Т6)."""
    cfg = config_mod.load(project)
    confirm(project)
    path = registry_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    junk = "# чужой файл\n\n| F1 | без шапки |\n"
    path.write_text(junk, encoding="utf-8")
    entry = item(run_heal(project, [code_symptom(cfg)]), "code")
    assert entry["status"] == "failed"
    assert "keeper-findings.md" in entry["detail"]
    assert path.read_text(encoding="utf-8") == junk


def test_format_human_names_finding_and_registry(project: Path) -> None:
    """Сводка называет пятый статус, а строка находки — ID и путь реестра (Т7)."""
    cfg = config_mod.load(project)
    confirm(project)
    text = keeper_heal.format_human(run_heal(project, [code_symptom(cfg)]))
    assert "Проект demo: режим apply" in text
    for name in ("ok", "failed", "refused", "planned", "found"):
        assert f"{name} 0" in text or f"{name} 1" in text
    assert "found 1" in text and "строк 1" in text
    assert CODE_ADDRESS in text and "F1" in text and "keeper-findings.md" in text


def test_code_finding_keeps_engine_and_tree_untouched(project: Path) -> None:
    """«Готово =»: `engine/*.py` не меняется — равенство хешей до и после (Т8)."""
    cfg = config_mod.load(project)
    package = make_package(project.parent / "pkg", project, roles=("pipeline-keeper",))
    confirm(project)
    before = snapshot(project, package)
    result = run_heal(project, [code_symptom(cfg)], plugin_dir=package)
    assert [entry["status"] for entry in result["results"]] == ["found"]
    assert snapshot(project, package) == before
    assert registry_file(project).is_file()
    assert state_dir(project).resolve() in registry_file(project).resolve().parents


def test_two_code_symptoms_get_next_numbers_and_survive_pipe(project: Path) -> None:
    """Две находки — `F1` и `F2` по порядку `_order`; `|` и перевод строки не рвут таблицу (Т9)."""
    cfg = config_mod.load(project)
    confirm(project)
    high = code_symptom(cfg, "step_pipeline/engine/tick.py:88",
                        "сбой | в таблице\nи вторая строка", severity="high")
    low = code_symptom(cfg, "step_pipeline/engine/state_io.py:12", "мелочь", severity="low")
    result = run_heal(project, [low, high])
    assert [entry["finding"]["id"] for entry in result["results"]] == ["F1", "F2"]
    assert [entry["address"] for entry in result["results"]] == [high["address"], low["address"]]
    rows = registry_rows_file(project)              # `GateError` здесь и был бы падением
    assert [row["id"] for row in rows] == ["F1", "F2"]
    assert "|" not in rows[0]["text"] and "\n" not in rows[0]["text"]
    assert "вторая строка" in rows[0]["text"]
