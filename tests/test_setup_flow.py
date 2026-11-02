# -*- coding: utf-8 -*-
"""Мастер установки: копирование движка, конфиг, идемпотентность, Telegram-шаг."""
from __future__ import annotations

import argparse
import json
import os
import sys
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
                no_profiles=True, no_cron=True, no_trust=True, yes=True, force=False)
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


def _doctor_env(monkeypatch, project: Path, tmp_path: Path,
                trust: dict[str, str]) -> None:
    """Окружение `doctor` по образцу tests/test_plan_lint.py: без настоящего hermes."""
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
