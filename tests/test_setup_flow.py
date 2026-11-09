# -*- coding: utf-8 -*-
"""Мастер установки: копирование движка, конфиг, идемпотентность, Telegram-шаг."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
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


def test_setup_keeps_owner_profile_keys(tmp_path: Path, fake_hermes):
    """Ключ владельца в `profiles` переживает повторный setup (D9)."""
    target = tmp_path / "shop"
    target.mkdir()
    setup_flow.run_setup(args(project=str(target), board="shopboard", driver="shop"))
    path = target / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["profiles"]["scout"] = "scout"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    assert setup_flow.run_setup(args(project=str(target), yes=True)) == 0
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["profiles"]["scout"] == "scout"
    assert again["profiles"]["driver"] == "shop"


def test_setup_telegram_writes_env_and_enables_notify(tmp_path: Path, monkeypatch, fake_hermes):
    target = tmp_path / "shop"
    target.mkdir()
    home = tmp_path / "hermes_home"
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda profile: home / "profiles" / profile)

    sent: list[tuple] = []
    monkeypatch.setattr(setup_flow.telegram, "get_me", lambda token: {"username": "demo_bot"})
    monkeypatch.setattr(setup_flow.telegram, "get_webhook_info", lambda token: {"url": ""})
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


def test_setup_warns_about_webhook(tmp_path: Path, monkeypatch, capsys, fake_hermes):
    """Вебхук на боте занимает getUpdates: мастер предупреждает заранее (D8, S4)."""
    target = tmp_path / "shop"
    target.mkdir()
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda p: tmp_path / "h" / p)
    monkeypatch.setattr(setup_flow.telegram, "get_me", lambda token: {"username": "demo_bot"})
    monkeypatch.setattr(setup_flow.telegram, "get_webhook_info",
                        lambda token: {"url": "https://example.test/hook"})
    # Сеть в тестах — только подделки §4: ни getChat, ни sendMessage наружу не идут.
    monkeypatch.setattr(setup_flow.telegram, "validate_chat", lambda token, chat: {"id": chat})
    monkeypatch.setattr(setup_flow.telegram, "send_message", lambda token, chat, text: True)

    code = setup_flow.run_setup(args(project=str(target), board="shop", driver="shop",
                                     no_telegram=False, telegram_token="123:ABC",
                                     telegram_chat="42", no_test=True))
    out = capsys.readouterr().out
    assert "вебхук" in out and "getUpdates → 409" in out
    # Мастер не сломался на предупреждении: код 0 и токен/чат в .env (§5.1 п.14).
    assert code == 0
    env = (tmp_path / "h" / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.TOKEN_ENV}=123:ABC" in env
    assert f"{setup_flow.CHAT_ENV}=42" in env


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


def test_update_cron_writes_script_and_creates_job(monkeypatch, tmp_path: Path):
    """Автообновление — своя cron-джоба со скриптом из профиля, а не «когда открыта панель»."""
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

    monkeypatch.setattr(setup_flow.proj, "run_hermes", run)
    log = setup_flow.Log()
    project = tmp_path / "shop"
    project.mkdir()
    setup_flow.ensure_update_cron("shop", project, log, skip=False)

    script = home / "profiles" / "shop" / "scripts" / "pipeline_update.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "from update import auto" in text and "plugins" in text
    created = [call for call in calls if "create" in call]
    assert created and "pipeline_update.py" in created[0]
    assert setup_flow.UPDATE_CRON in created[0]


def test_update_cron_respects_no_cron(monkeypatch, tmp_path: Path):
    """`--no-cron` — обещание не заводить джобы: автообновление тоже не заводится."""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda p: home / "profiles" / p)
    calls: list[tuple] = []
    monkeypatch.setattr(setup_flow.proj, "run_hermes",
                        lambda *argv, **kw: calls.append(argv))
    log = setup_flow.Log()
    project = tmp_path / "shop"
    project.mkdir()
    setup_flow.ensure_update_cron("shop", project, log, skip=True)
    assert calls == []
    assert not (home / "profiles" / "shop" / "scripts" / "pipeline_update.py").exists()


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


# ───────────────── S11: бот опционален, токен скрыт, доступ только владельцу ─────────────────

TOKEN = "123456:AAH-demo-secret"
OLD_TOKEN = "999000:OLD-demo-token"


def _isolate_profile(monkeypatch, tmp_path: Path) -> Path:
    """Изолировать `.env`: тесты шага не читают и не пишут профиль настоящего Hermes.

    `proj.profile_dir` берёт `LOCALAPPDATA`/`~/.hermes` и `HERMES_HOME` не читает, поэтому
    подмена обязательна везде, где шаг доходит до `write_env`/`env_values`.
    """
    monkeypatch.setattr(proj, "profile_dir", lambda profile: tmp_path / "h" / profile)
    for name in (setup_flow.TOKEN_ENV, setup_flow.CHAT_ENV, setup_flow.OWNER_ENV,
                 "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "h"


def _telegram_stubs(monkeypatch, *, detected=("42", "777"), webhook: str = "") -> None:
    """Telegram без сети: тот же бот, тот же чат, никаких запросов наружу (§3)."""
    monkeypatch.setattr(setup_flow.telegram, "get_me", lambda token: {"username": "demo_bot"})
    monkeypatch.setattr(setup_flow.telegram, "get_webhook_info", lambda token: {"url": webhook})
    monkeypatch.setattr(setup_flow.telegram, "validate_chat", lambda token, chat: {"id": chat})
    monkeypatch.setattr(setup_flow.telegram, "send_message", lambda token, chat, text: True)
    monkeypatch.setattr(setup_flow.telegram, "detect_chat_and_owner",
                        lambda token, wait_seconds=60: detected)


def test_setup_without_bot_is_not_an_error(tmp_path: Path, monkeypatch, capsys):
    """Отказ от бота — допустимый выбор: ни строки `! `, ни слова «ошибка» (D3)."""
    _isolate_profile(monkeypatch, tmp_path)
    log = setup_flow.Log()
    result = setup_flow.setup_telegram(tmp_path / "shop", "shop",
                                       args(no_telegram=True, yes=False), log)
    out = capsys.readouterr().out
    assert result == (None, True)
    assert not [line for line in out.splitlines() if line.startswith("  ! ")]
    assert "ошибка" not in out and "не настроены" not in out


def test_full_setup_without_bot_has_no_warnings(tmp_path: Path, fake_hermes, monkeypatch,
                                                capsys):
    """Полный `run_setup` без бота: код 0, ни одной строки `! `, исход сохранён (D1)."""
    target = tmp_path / "shop"
    target.mkdir()
    _isolate_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_flow, "run_smoke", lambda *a, **k: (3, 3))

    code = setup_flow.run_setup(args(project=str(target), board="shop", driver="shop",
                                     no_telegram=True, no_profiles=False, no_cron=False,
                                     no_trust=False, no_smoke=False))
    out = capsys.readouterr().out
    assert code == 0
    assert not [line for line in out.splitlines() if line.startswith("  ! ")]
    cfg = json.loads((target / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["skipped"] is True
    assert cfg["notify"]["enabled"] is False


def test_panel_cron_step_without_bot_has_no_warnings(tmp_path: Path, fake_hermes, monkeypatch):
    """Шаг `cron` панели после выбора «без бота» тоже молчит предупреждениями (D3)."""
    root = tmp_path / "shop"
    root.mkdir()
    _isolate_profile(monkeypatch, tmp_path)
    assert setup_flow.run_setup_step(root, "project",
                                     {"board": "shop", "driver": "shop"})["ok"] is True
    assert setup_flow.run_setup_step(root, "telegram", {"bot": "0"})["ok"] is True

    cron = setup_flow.run_setup_step(root, "cron")
    assert cron["ok"] is True
    assert not [line for line in cron["log"] if line.startswith("  ! ")]
    assert "выбором владельца" in "\n".join(cron["log"])


def test_bot_question_defaults_to_off(tmp_path: Path, monkeypatch):
    """Умолчание вопроса — «нет»: пустой ответ означает «без бота» (D2)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(setup_flow.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    log = setup_flow.Log()
    result = setup_flow.setup_telegram(tmp_path / "shop", "shop",
                                       args(no_telegram=False, yes=False), log)
    assert result == (None, True)
    assert any("[y/N]" in prompt for prompt in prompts)
    assert not (home / "shop").exists()


def test_repeat_setup_asks_to_replace_token(tmp_path: Path, monkeypatch):
    """Повторный запуск с токеном в окружении предлагает замену и пишет новый (D5)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    monkeypatch.setenv(setup_flow.TOKEN_ENV, OLD_TOKEN)
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return "y" if "Заменить" in prompt else ""

    monkeypatch.setattr(setup_flow.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(setup_flow.getpass, "getpass", lambda prompt="": TOKEN)
    _telegram_stubs(monkeypatch)

    log = setup_flow.Log()
    chat, skipped = setup_flow.setup_telegram(
        tmp_path / "shop", "shop",
        args(no_telegram=False, yes=False, telegram_chat="42"), log)
    env = (home / "shop" / ".env").read_text(encoding="utf-8")
    assert (chat, skipped) == ("42", False)
    assert f"{setup_flow.TOKEN_ENV}={TOKEN}" in env
    assert any("Заменить" in prompt for prompt in prompts)


def test_repeat_setup_keeps_token_when_replace_declined(tmp_path: Path, monkeypatch):
    """Отказ от замены оставляет прежний токен и не ломает повторную установку (D5)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    monkeypatch.setenv(setup_flow.TOKEN_ENV, OLD_TOKEN)
    monkeypatch.setattr(setup_flow.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    _telegram_stubs(monkeypatch)

    log = setup_flow.Log()
    result = setup_flow.setup_telegram(
        tmp_path / "shop", "shop",
        args(no_telegram=False, yes=False, telegram_chat="42"), log)
    env = (home / "shop" / ".env").read_text(encoding="utf-8")
    assert result == ("42", False)
    assert f"{setup_flow.TOKEN_ENV}={OLD_TOKEN}" in env


def test_setup_log_never_shows_token_fragments(tmp_path: Path, monkeypatch, capsys,
                                               fake_hermes):
    """Ни токена, ни его первых 8 и последних 4 символов в stdout; `_mask` удалён (D4)."""
    target = tmp_path / "shop"
    target.mkdir()
    _isolate_profile(monkeypatch, tmp_path)
    _telegram_stubs(monkeypatch)

    code = setup_flow.run_setup(args(project=str(target), board="shop", driver="shop",
                                     no_telegram=False, telegram_token=TOKEN,
                                     telegram_chat="42"))
    out = capsys.readouterr().out
    assert code == 0
    assert TOKEN not in out
    assert TOKEN[:8] not in out
    assert TOKEN[-4:] not in out
    assert not hasattr(setup_flow, "_mask")


def test_master_mentions_botfather_rules(tmp_path: Path, monkeypatch, capsys, fake_hermes):
    """Мастер называет deleteWebhook, privacy mode и allowlist — без «команды» BotFather."""
    target = tmp_path / "shop"
    target.mkdir()
    _isolate_profile(monkeypatch, tmp_path)
    _telegram_stubs(monkeypatch)

    setup_flow.run_setup(args(project=str(target), board="shop", driver="shop",
                              no_telegram=False, telegram_token=TOKEN, telegram_chat="42"))
    out = capsys.readouterr().out
    for word in ("BotFather", "вебхук", "privacy", "owner", "deleteWebhook"):
        assert word in out, word
    assert "/deletewebhook" not in out


@pytest.mark.skipif(os.name == "nt", reason="на Windows chmod почти ничего не значит")
def test_env_file_is_owner_only(tmp_path: Path, monkeypatch):
    """`.env` с токеном доступен только владельцу (0600)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    _telegram_stubs(monkeypatch)
    log = setup_flow.Log()
    setup_flow.setup_telegram(tmp_path / "shop", "shop",
                              args(no_telegram=False, telegram_token=TOKEN,
                                   telegram_chat="42"), log)
    mode = stat.S_IMODE((home / "shop" / ".env").stat().st_mode)
    assert mode == 0o600


def test_detected_owner_id_is_written_not_chat(tmp_path: Path, monkeypatch):
    """Owner id из `/start` — достовернее chat id: в `.env` пишется user id (D6)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_flow.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": "y" if "/start" in prompt else "")
    _telegram_stubs(monkeypatch, detected=("42", "777"))

    log = setup_flow.Log()
    setup_flow.setup_telegram(tmp_path / "shop", "shop",
                              args(no_telegram=False, yes=False, telegram_token=TOKEN), log)
    env = (home / "shop" / ".env").read_text(encoding="utf-8")
    assert f"{setup_flow.CHAT_ENV}=42" in env
    assert f"{setup_flow.OWNER_ENV}=777" in env


def test_build_config_keeps_notify_skipped(tmp_path: Path):
    """`build_config` сохраняет чужой `notify.skipped` и не выдумывает его сам (D1)."""
    project = tmp_path / "shop"
    project.mkdir()
    cfg = setup_flow.build_config(project, "shop", "shop", "prompter", "reviewer",
                                  {"notify": {"skipped": True, "enabled": False,
                                              "prefix": "склад"}})
    assert cfg["notify"]["skipped"] is True
    assert cfg["notify"]["enabled"] is False
    assert cfg["notify"]["prefix"] == "склад"
    fresh = setup_flow.build_config(project, "shop", "shop", "prompter", "reviewer", {})
    assert "skipped" not in (fresh.get("notify") or {})


def test_telegram_step_requires_project_step(tmp_path: Path, monkeypatch):
    """Без `pipeline.json` шаг Telegram красный: драйвер проекта ещё не известен (§4.8 п.4)."""
    _isolate_profile(monkeypatch, tmp_path)
    root = tmp_path / "bare"
    root.mkdir()
    result = setup_flow.run_setup_step(root, "telegram",
                                       {"token": TOKEN, "chat": "42", "bot": "1"})
    assert result["ok"] is False
    assert "сначала шаг Проект" in result["error"]
    assert not (root / "pipeline.json").exists()


def test_telegram_step_with_bot_checked_and_no_token_is_red(tmp_path: Path, monkeypatch):
    """Включённый чекбокс без токена и без `.env` — красный шаг «нужен токен бота» (D8 п.4)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    root = tmp_path / "shop"
    root.mkdir()
    assert setup_flow.run_setup_step(root, "project",
                                     {"board": "shop", "driver": "shop"})["ok"] is True
    before = (root / "pipeline.json").read_bytes()

    result = setup_flow.run_setup_step(root, "telegram", {"bot": "1"})
    assert result["ok"] is False
    assert "нужен токен бота" in result["error"]
    assert not (home / "shop" / ".env").exists()
    assert (root / "pipeline.json").read_bytes() == before


def test_telegram_detect_error_is_reported_without_disk_writes(tmp_path: Path, monkeypatch):
    """detect на ошибке Telegram: `ok=False`, подсказка `/start`, на диск ничего не пишется."""
    home = _isolate_profile(monkeypatch, tmp_path)
    root = tmp_path / "shop"
    root.mkdir()
    assert setup_flow.run_setup_step(root, "project",
                                     {"board": "shop", "driver": "shop"})["ok"] is True

    def boom(token, wait_seconds=15):
        raise setup_flow.telegram.TelegramError("нет сообщений")

    monkeypatch.setattr(setup_flow.telegram, "detect_chat_and_owner", boom)
    before = (root / "pipeline.json").stat().st_mtime_ns

    result = setup_flow.run_setup_step(root, "telegram",
                                       {"token": TOKEN, "detect": "1"})
    assert result["ok"] is False
    assert "нет сообщений" in "\n".join(result["log"])
    assert "/start" in "\n".join(result["log"])
    assert (root / "pipeline.json").stat().st_mtime_ns == before
    assert not (home / "shop" / ".env").exists()


def test_telegram_detect_without_token_is_red(tmp_path: Path, monkeypatch):
    """detect без токена ни в форме, ни в `.env` — красный шаг, сети нет (§4.7 п.1)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    root = tmp_path / "shop"
    root.mkdir()
    assert setup_flow.run_setup_step(root, "project",
                                     {"board": "shop", "driver": "shop"})["ok"] is True
    before = (root / "pipeline.json").read_bytes()

    def forbidden(token, wait_seconds=15):
        raise AssertionError("detect без токена")

    monkeypatch.setattr(setup_flow.telegram, "detect_chat_and_owner", forbidden)
    result = setup_flow.run_setup_step(root, "telegram", {"detect": "1"})
    assert result["ok"] is False
    assert "сначала токен бота" in result["error"]
    assert not (home / "shop" / ".env").exists()
    assert (root / "pipeline.json").read_bytes() == before


def test_telegram_detect_uses_token_from_env(tmp_path: Path, monkeypatch):
    """detect берёт токен из `.env` через `.get` и на диск ничего не пишет (D7)."""
    home = _isolate_profile(monkeypatch, tmp_path)
    root = tmp_path / "shop"
    root.mkdir()
    assert setup_flow.run_setup_step(root, "project",
                                     {"board": "shop", "driver": "shop"})["ok"] is True
    setup_flow.write_env("shop", {setup_flow.TOKEN_ENV: TOKEN})
    env_path = home / "shop" / ".env"
    env_before = env_path.stat().st_mtime_ns
    cfg_before = (root / "pipeline.json").stat().st_mtime_ns
    _telegram_stubs(monkeypatch, detected=("42", "777"))

    result = setup_flow.run_setup_step(root, "telegram", {"detect": "1"})
    assert result["ok"] is True
    assert result["found"] == {"chat": "42", "owner": "777"}
    assert env_path.stat().st_mtime_ns == env_before
    assert (root / "pipeline.json").stat().st_mtime_ns == cfg_before
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["notify"]["enabled"] is False
    assert "skipped" not in cfg["notify"]
