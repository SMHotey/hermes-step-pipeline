# -*- coding: utf-8 -*-
"""Мастер установки: копирование движка, конфиг, идемпотентность, Telegram-шаг."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402  (загружает плагин как пакет и регистрирует алиасы)
import cli                                      # noqa: E402
import project as proj                          # noqa: E402
import setup_flow                               # noqa: E402


def args(**kw) -> argparse.Namespace:
    base = dict(project="", board="", driver="", prompter="", reviewer="",
                telegram_token="", telegram_chat="", no_telegram=True, no_test=True,
                no_profiles=True, no_cron=True, no_trust=True, yes=True, force=False,
                no_smoke=True, smoke_timeout=0)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture()
def fake_hermes(monkeypatch):
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


def test_setup_copies_engine_roles_and_config(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    (target / "pipeline.json").unlink(missing_ok=True)
    code = setup_flow.run_setup(args(project=str(target), board="shopboard", driver="shop"))
    assert code == 0
    assert (target / ".pipeline" / "gate.py").is_file()
    assert (target / ".pipeline" / "next_step.py").is_file()
    assert (target / ".hermes" / "skills" / "software-development" /
            "pipeline-reviewer" / "SKILL.md").is_file()
    assert (target / ".hermes" / "skills" / "autonomous-ai-agents" /
            "agent-prompt-authoring" / "SKILL.md").is_file()
    assert (target / "docs" / "plans").is_dir()
    assert (target / "docs" / "templates" / "plan-template.md").is_file()

    cfg = json.loads((target / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["board"] == "shopboard"
    assert cfg["profiles"] == {"driver": "shop", "prompter": "prompter", "reviewer": "reviewer"}
    assert cfg["notify"]["enabled"] is False
    assert cfg["notify"]["token_env"] == setup_flow.TOKEN_ENV
    assert cfg["runtimes"]["step"] == "3h"

    ignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".pipeline/state/" in ignore and ".pipeline/.env" in ignore


def test_setup_is_idempotent_and_keeps_user_config(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shopboard", driver="shop"))
    path = target / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["driven_plans"] = ["2"]
    cfg["project"] = "Склад"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    assert setup_flow.run_setup(args(project=str(target), yes=True)) == 0
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["driven_plans"] == ["2"]      # правка пользователя не потерялась
    assert again["project"] == "Склад"
    assert again["board"] == "shopboard"       # борда из существующего конфига

    ignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ignore.count(".pipeline/state/") == 1


def test_setup_telegram_writes_env_and_enables_notify(tmp_path: Path, monkeypatch, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    home = tmp_path / "hermes_home"
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda profile: home / "profiles" / profile)

    sent: list[tuple] = []
    monkeypatch.setattr(setup_flow.telegram, "get_me", lambda token: {"username": "demo_bot"})
    monkeypatch.setattr(setup_flow.telegram, "validate_chat", lambda token, chat: {"id": chat})
    monkeypatch.setattr(setup_flow.telegram, "send_message",
                        lambda token, chat, text: sent.append((chat, text)) or True)

    code = setup_flow.run_setup(args(project=str(target), board="shop", driver="shop",
                                     no_telegram=False, no_test=False,
                                     telegram_token="123:ABC", telegram_chat="42"))
    assert code == 0
    env = (home / "profiles" / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.TOKEN_ENV}=123:ABC" in env
    assert f"{setup_flow.CHAT_ENV}=42" in env
    cfg = json.loads((target / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is True
    assert sent and "подключены" in sent[0][1]


def test_bad_token_leaves_notifications_off(tmp_path: Path, monkeypatch, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda p: tmp_path / "h" / p)

    def boom(token):
        raise setup_flow.telegram.TelegramError("Unauthorized")

    monkeypatch.setattr(setup_flow.telegram, "get_me", boom)
    setup_flow.run_setup(args(project=str(target), no_telegram=False,
                              telegram_token="bad", telegram_chat="1"))
    cfg = json.loads((target / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is False


def test_setup_writes_engine_version(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shop"))
    version_file = target / ".pipeline" / "VERSION"
    assert version_file.is_file()
    assert version_file.read_text(encoding="utf-8").strip() == setup_flow.plugin_version()


def test_upgrade_restores_engine_and_bumps_version(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shop"))
    gate = target / ".pipeline" / "gate.py"
    gate.write_text("# сломанная копия\n", encoding="utf-8")
    (target / ".pipeline" / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    # конфиг с пользовательской правкой — upgrade не должен его тронуть
    cfg_path = target / "pipeline.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["board"] = "мой-борд"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    assert setup_flow.run_setup(args(project=str(target), upgrade=True)) == 0
    assert "Страж шага" in gate.read_text(encoding="utf-8")
    assert (target / ".pipeline" / "VERSION").read_text(
        encoding="utf-8").strip() == setup_flow.plugin_version()
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["board"] == "мой-борд"


def test_upgrade_without_install_is_error(tmp_path: Path, fake_hermes):
    target = tmp_path / "empty"
    target.mkdir()
    assert setup_flow.run_setup(args(project=str(target), upgrade=True)) == 2


def test_engine_copy_updates_changed_file(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shop"))
    gate = target / ".pipeline" / "gate.py"
    gate.write_text("# устаревшая копия\n", encoding="utf-8")
    setup_flow.run_setup(args(project=str(target), yes=True))
    assert "Страж шага" in gate.read_text(encoding="utf-8")


def test_upgrade_refreshes_changed_role_and_keeps_backup(tmp_path: Path, fake_hermes,
                                                         monkeypatch):
    """Правка `step_pipeline/roles/` доезжает до поставленного проекта, старая не пропадает."""
    monkeypatch.setattr(setup_flow, "ensure_plugin_enabled", lambda profile, log: "пропущено")
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shop"))
    role_dir = target / ".hermes" / "skills" / "software-development" / "pipeline-reviewer"
    role = role_dir / "SKILL.md"
    role.write_text("старая версия роли\n", encoding="utf-8")
    (role_dir / "лишний.md").write_text("файл, которого нет в плагине\n", encoding="utf-8")

    assert setup_flow.run_setup(args(project=str(target), upgrade=True)) == 0

    plugin_role = proj.ROLES_SRC / "pipeline-reviewer" / "SKILL.md"
    assert role.read_text(encoding="utf-8") == plugin_role.read_text(encoding="utf-8")
    assert not (role_dir / "лишний.md").exists(), "роль — вендоренная копия: лишнее уходит"
    backups = list((target / ".pipeline" / "roles-backup").glob("pipeline-reviewer-*"))
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text(encoding="utf-8") == "старая версия роли\n"


def test_upgrade_leaves_fresh_roles_alone(tmp_path: Path, fake_hermes, monkeypatch, capsys):
    """Второй upgrade ничего не переписывает и не плодит бэкапы."""
    monkeypatch.setattr(setup_flow, "ensure_plugin_enabled", lambda profile, log: "пропущено")
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shop"))
    capsys.readouterr()
    assert setup_flow.run_setup(args(project=str(target), upgrade=True)) == 0
    out = capsys.readouterr().out
    assert "обновлена роль" not in out
    assert "движок и роли уже актуальны" in out
    assert not (target / ".pipeline" / "roles-backup").exists()


def test_setup_refreshes_roles_only_with_force(tmp_path: Path, fake_hermes):
    """Повторный `setup` без `--force` оставляет правку проекта в роли."""
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shop"))
    role = (target / ".hermes" / "skills" / "software-development" /
            "pipeline-reviewer" / "SKILL.md")
    role.write_text("проектная правка\n", encoding="utf-8")
    setup_flow.run_setup(args(project=str(target), board="shop"))
    assert role.read_text(encoding="utf-8") == "проектная правка\n"
    setup_flow.run_setup(args(project=str(target), board="shop", force=True))
    assert role.read_text(encoding="utf-8") != "проектная правка\n"


def test_cron_script_written(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda p: home / "profiles" / p)
    calls: list[tuple] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(*argv, timeout=180):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(proj, "run_hermes", run)
    log = setup_flow.Log()
    project = tmp_path / "shop"
    project.mkdir()
    setup_flow.ensure_cron("shop", project, log, skip=False)
    script = home / "profiles" / "shop" / "scripts" / "pipeline_tick.py"
    assert script.is_file()
    assert "gate.py" in script.read_text(encoding="utf-8")
    assert any("cron" in " ".join(c) for c in calls)


def _trust_calls(calls: list[tuple]) -> list[tuple]:
    return [call for call in calls if "trust" in call]


def test_setup_trusts_all_three_profiles(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    code = setup_flow.run_setup(args(project=str(target), board="shop", driver="shop",
                                     no_trust=False))
    assert code == 0
    calls = _trust_calls(fake_hermes)
    assert [call[1] for call in calls] == ["shop", "prompter", "reviewer"]
    for call in calls:
        assert call == ("-p", call[1], "skills", "trust", str(target.resolve()))


def test_upgrade_trusts_roles_from_config(tmp_path: Path, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    assert setup_flow.run_setup(args(project=str(target), board="shop", driver="shop")) == 0
    cfg_path = target / "pipeline.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["profiles"] = {"driver": "shop", "prompter": "pp", "reviewer": "rr"}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    fake_hermes.clear()

    assert setup_flow.run_setup(args(project=str(target), upgrade=True,
                                     no_trust=False)) == 0
    calls = _trust_calls(fake_hermes)
    assert [call[1] for call in calls] == ["shop", "pp", "rr"]
    for call in calls:
        assert call == ("-p", call[1], "skills", "trust", str(target.resolve()))
    again = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert again["profiles"] == {"driver": "shop", "prompter": "pp", "reviewer": "rr"}


SAVED_CREDENTIAL = {"source": "manual", "access_token": "сохранённый-секрет"}
ENV_CREDENTIAL = {"source": "env:OPENCODE_GO_API_KEY", "secret_fingerprint": "abc"}


def _doctor_env(monkeypatch, project: Path, tmp_path: Path,
                trust: dict[str, str],
                entries: dict[str, list[dict]] | None = None) -> None:
    """Окружение `doctor` по образцу tests/test_plan_lint.py: без настоящего hermes.

    `entries` — записи `credential_pool` по роли; по умолчанию у всех трёх ролей ключ
    сохранён в профиле (`manual`), как на живой машине 03.11.2026.
    """
    monkeypatch.setattr(cli.proj, "run_hermes", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="План: страж шага (конвейер)", stderr=""))
    monkeypatch.setattr(cli.proj, "profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(cli.snap, "snapshot", lambda *a, **k: {})
    db = tmp_path / "kanban.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    (project / ".pipeline" / "cron.json").write_text(
        json.dumps({"driver": "demo", "name": "План: страж шага (конвейер)"}),
        encoding="utf-8")
    for name, text in trust.items():
        path = tmp_path / "profiles" / name / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name in ("demo", "prompter", "reviewer"):
        path = tmp_path / "profiles" / name / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "credential_pool": {"opencode-go": (entries or {}).get(name, [SAVED_CREDENTIAL])},
        }), encoding="utf-8")


def test_doctor_reports_untrusted_roles_with_ready_command(project: Path, monkeypatch,
                                                          capsys, tmp_path: Path):
    _doctor_env(monkeypatch, project, tmp_path, {
        "demo": "skills:\n  trusted_project_dirs:\n"
                f"    - {project.resolve()}\n",
    })

    code = cli.cmd_doctor(argparse.Namespace(project=str(project), plan=""))
    out = capsys.readouterr().out
    assert code == 1
    untrusted = [line for line in out.splitlines() if "не доверен этому профилю" in line]
    assert len(untrusted) == 2
    for role in ("prompter", "reviewer"):
        command = f'hermes -p {role} skills trust "{project.resolve()}"'
        assert any(command in line for line in untrusted)
    assert not any("✗ план" in line for line in out.splitlines())


def test_doctor_ok_when_all_roles_trust_project(project: Path, monkeypatch,
                                                capsys, tmp_path: Path):
    path = project.resolve()
    _doctor_env(monkeypatch, project, tmp_path, {
        # драйвер — блок-список, промптер — строчный список, ревьюер — чужой путь + наш
        "demo": f"skills:\n  trusted_project_dirs:\n    - {path}\n",
        "prompter": f'skills:\n  trusted_project_dirs: ["{path}"]\n',
        "reviewer": "skills:\n  trusted_project_dirs:\n    - C:\\Other\\project\n"
                    f"    - {path}\n",
    })
    (project / ".pipeline" / "VERSION").write_text(
        setup_flow.plugin_version() + "\n", encoding="utf-8")

    code = cli.cmd_doctor(argparse.Namespace(project=str(project), plan=""))
    out = capsys.readouterr().out
    assert code == 0
    assert "ИТОГ: OK" in out
    assert "доверен всем ролям" in out
    assert "не доверен этому профилю" not in out


# ───────────────────── смоук установки и проверка сохранённого ключа (S16) ─────────────────────


def _role_config(project: Path, provider: str = "opencode-go") -> str:
    return (f"model:\n  provider: {provider}\n"
            f"skills:\n  trusted_project_dirs:\n    - {project.resolve()}\n")


def test_doctor_reports_missing_saved_credential(project: Path, monkeypatch,
                                                 capsys, tmp_path: Path):
    """Ключ только из окружения (`env:`) — проблема с готовой командой починки."""
    _doctor_env(monkeypatch, project, tmp_path,
                {role: _role_config(project) for role in ("demo", "prompter", "reviewer")},
                entries={role: [ENV_CREDENTIAL] for role in ("demo", "prompter", "reviewer")})

    code = cli.cmd_doctor(argparse.Namespace(project=str(project), plan=""))
    out = capsys.readouterr().out
    assert code == 1
    for role in ("demo", "prompter", "reviewer"):
        command = f"hermes -p {role} auth add opencode-go --type api-key"
        assert any("нет сохранённого ключа" in line and command in line
                   for line in out.splitlines())
    assert not any("✗ план" in line for line in out.splitlines())
    assert "сохранённый-секрет" not in out


def test_doctor_ok_when_roles_have_saved_credentials(project: Path, monkeypatch,
                                                     capsys, tmp_path: Path):
    """Запись `manual` с непустым секретом у всех трёх ролей — зелёный итог."""
    _doctor_env(monkeypatch, project, tmp_path,
                {role: _role_config(project) for role in ("demo", "prompter", "reviewer")})
    (project / ".pipeline" / "VERSION").write_text(
        setup_flow.plugin_version() + "\n", encoding="utf-8")

    code = cli.cmd_doctor(argparse.Namespace(project=str(project), plan=""))
    out = capsys.readouterr().out
    assert code == 0
    assert "ИТОГ: OK" in out
    assert "ключи: сохранённый ключ провайдера есть у всех ролей" in out
    assert "сохранённый-секрет" not in out


def _smoke_env(monkeypatch, tmp_path: Path, statuses: dict[str, str],
               errors: dict[str, str] | None = None) -> tuple[list[tuple], Path]:
    """Поддельный `hermes`: смоук-карточка сразу ложится в sqlite с нужным статусом."""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_flow.proj, "profile_dir",
                        lambda name: home / "profiles" / name)
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    calls: list[tuple] = []
    counter = {"n": 0}

    class Result:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def run(*argv, timeout=180):
        calls.append(argv)
        if "--idempotency-key" not in argv:
            return Result()
        counter["n"] += 1
        card_id = f"t_{counter['n']:08d}"
        role = argv[argv.index("--assignee") + 1]
        status = statuses.get(role, "done")
        con = sqlite3.connect(db)
        try:
            con.executescript(helpers.KANBAN_SCHEMA)
            con.execute(
                "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
                " completed_at, result, last_failure_error, created_at, tenant, workspace,"
                " max_runtime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, argv[4], status, role, argv[argv.index("--idempotency-key") + 1],
                 None, "смоук OK" if status == "done" else "",
                 (errors or {}).get(role, ""), int(time.time()), "plan",
                 argv[argv.index("--workspace") + 1], argv[argv.index("--max-runtime") + 1]))
            con.commit()
        finally:
            con.close()
        return Result(stdout=card_id)

    monkeypatch.setattr(proj, "run_hermes", run)
    return calls, db


def test_setup_smoke_creates_card_for_each_role(tmp_path: Path, monkeypatch, capsys):
    target = tmp_path / "shop"
    target.mkdir()
    calls, _db = _smoke_env(monkeypatch, tmp_path, {})
    code = setup_flow.run_setup(args(project=str(target), board="shopboard", driver="shop",
                                     no_profiles=False, no_cron=False, no_smoke=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "роли стартуют (3 из 3)" in out

    creates = [call for call in calls if "--idempotency-key" in call]
    assert len(creates) == 3
    cfg = json.loads((target / "pipeline.json").read_text(encoding="utf-8"))
    expected = {"shop": cfg["skills"]["step"], "prompter": cfg["skills"]["prompt"],
                "reviewer": cfg["skills"]["review"]}
    roles = [call[call.index("--assignee") + 1] for call in creates]
    assert roles == ["shop", "prompter", "reviewer"]
    for call in creates:
        role = call[call.index("--assignee") + 1]
        assert call[call.index("--workspace") + 1] == f"dir:{Path(target).as_posix()}"
        assert call[call.index("--idempotency-key") + 1].startswith(f"smoke:{role}:")
        assert call[call.index("--max-runtime") + 1] == setup_flow.SMOKE_RUNTIME
        skills = [call[index + 1] for index, token in enumerate(call) if token == "--skill"]
        assert skills == expected[role]
        assert call[call.index("--board") + 1] == "shopboard"


def test_setup_smoke_failure_prints_cause_and_repair(tmp_path: Path, monkeypatch, capsys):
    target = tmp_path / "shop"
    target.mkdir()
    _smoke_env(monkeypatch, tmp_path,
               {"shop": "done", "prompter": "blocked", "reviewer": "done"},
               {"prompter": "Unknown skill(s): pipeline-prompter"})
    code = setup_flow.run_setup(args(project=str(target), board="shopboard", driver="shop",
                                     no_profiles=False, no_cron=False, no_smoke=False))
    out = capsys.readouterr().out
    assert code == 3
    assert "смоук: 2 из 3" in out
    assert "Unknown skill(s): pipeline-prompter" in out
    assert f'починка: hermes -p prompter skills trust "{target.resolve()}"' in out


def test_setup_smoke_skipped_by_flag_or_without_profiles(tmp_path: Path,
                                                         monkeypatch, capsys):
    for name, extra, reason in (
        ("no-smoke", {"no_smoke": True, "no_profiles": False, "no_cron": False},
         "смоук ролей пропущен (--no-smoke)"),
        ("no-profiles", {"no_smoke": False, "no_profiles": True, "no_cron": False},
         "профили и cron в этом запуске не настраивались"),
    ):
        base = tmp_path / name
        base.mkdir()
        target = base / "shop"
        target.mkdir()
        calls, _db = _smoke_env(monkeypatch, base, {})
        code = setup_flow.run_setup(args(project=str(target), board="shopboard",
                                         driver="shop", **extra))
        out = capsys.readouterr().out
        assert code == 0
        assert reason in out
        assert not [call for call in calls if "--idempotency-key" in call]


def test_wait_smoke_card_times_out(tmp_path: Path, monkeypatch):
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    con = sqlite3.connect(db)
    try:
        con.executescript(helpers.KANBAN_SCHEMA)
        con.execute("INSERT INTO tasks (id, status, idempotency_key)"
                    " VALUES ('t_00000009', 'running', 'smoke:prompter:1')")
        con.commit()
    finally:
        con.close()

    ticks = {"now": 0.0}

    def clock() -> float:
        return ticks["now"]

    def sleep(seconds: float) -> None:
        ticks["now"] += seconds

    state = setup_flow.wait_smoke_card(tmp_path, "smoke:prompter:1", 12, 5,
                                       sleep=sleep, clock=clock)
    assert state["timed_out"] is True
    assert state["status"] == "running"
    assert ticks["now"] == 12
