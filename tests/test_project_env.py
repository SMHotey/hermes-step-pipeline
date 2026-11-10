"""Окружение движка: `.env` профиля драйвера доезжает до запуска (0.20.1).

Панель одна на все проекты и живёт в процессе, который профиль проекта не загружает, а
движок ищет токен уведомлений в окружении (`notify.token_env`). Без подстановки тик из
панели молчал, а чужой `STEP_PIPELINE_*` из окружения сервера уводил уведомления проекта
чужому боту. Подстановка — в `project.run_engine`, поэтому её видят и панель, и снимок.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import project as project_mod

TOKEN = "STEP_PIPELINE_TELEGRAM_BOT_TOKEN"


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch) -> Path:
    """Проект с движком-пробником и подменённым каталогом профилей Hermes."""
    root = tmp_path / "proj"
    (root / ".pipeline").mkdir(parents=True)
    (root / "pipeline.json").write_text(json.dumps({
        "project": "demo",
        "board": "demo",
        "profiles": {"driver": "demo", "prompter": "prompter", "reviewer": "reviewer"},
    }), encoding="utf-8")
    # Пробник печатает всё, что относится к каналу уведомлений: так видно и подстановку,
    # и то, что чужие переменные до движка не доехали.
    (root / ".pipeline" / "probe.py").write_text(
        "import json, os\n"
        "keys = [k for k in os.environ if 'TELEGRAM' in k or k in "
        "('PIPELINE_REPO', 'FROM_EXTRA', 'PIPELINE_KEEP_ME')]\n"
        "print(json.dumps({k: os.environ[k] for k in sorted(keys)}))\n",
        encoding="utf-8")
    profiles = tmp_path / "profiles"
    (profiles / "demo").mkdir(parents=True)
    monkeypatch.setattr(project_mod, "profile_dir", lambda name: profiles / name)
    return root


def _env_of(root: Path, **kwargs) -> dict:
    res = project_mod.run_engine(root, "probe.py", timeout=120, **kwargs)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_profile_env_wins_over_process_env(sandbox: Path, monkeypatch):
    """Токен профиля проекта сильнее чужого токена из окружения сервера панели."""
    monkeypatch.setenv(TOKEN, "чужой-бот")
    (sandbox.parent / "profiles" / "demo" / ".env").write_text(
        f"# профиль проекта\n{TOKEN}='123:свой'\n", encoding="utf-8")

    env = _env_of(sandbox)

    assert env[TOKEN] == "123:свой"
    assert env["PIPELINE_REPO"] == str(sandbox)


def test_extra_env_wins_over_profile(sandbox: Path):
    """Явный `extra_env` сильнее файла: у вызывающего кода последнее слово."""
    (sandbox.parent / "profiles" / "demo" / ".env").write_text(
        f"{TOKEN}=из-файла\n", encoding="utf-8")

    env = _env_of(sandbox, extra_env={"FROM_EXTRA": "да"})

    assert env["FROM_EXTRA"] == "да"


def test_foreign_telegram_env_is_stripped(sandbox: Path, monkeypatch):
    """Токен и прокси чужого бота из окружения сервера не доезжают до движка проекта."""
    monkeypatch.setenv(TOKEN, "чужой-бот")
    monkeypatch.setenv("STEP_PIPELINE_TELEGRAM_API_BASE", "https://прокси-чужого-бота")
    monkeypatch.setenv("PIPELINE_KEEP_ME", "окружение")
    (sandbox.parent / "profiles" / "demo" / ".env").write_text(
        f"{TOKEN}=123:свой\n", encoding="utf-8")

    env = _env_of(sandbox)

    assert env[TOKEN] == "123:свой"
    # прокси чужого бота не подставляется: у проекта свой канал
    assert "https://прокси-чужого-бота" not in json.dumps(env, ensure_ascii=False)


def test_without_profile_env_notifications_stay_off(sandbox: Path, monkeypatch):
    """Профиль без `.env` — уведомления проекта выключены, а не уходят чужому боту."""
    monkeypatch.setenv(TOKEN, "из-окружения")
    monkeypatch.setenv("PIPELINE_KEEP_ME", "окружение")

    env = _env_of(sandbox)

    assert env.get(TOKEN) is None
    assert env["PIPELINE_KEEP_ME"] == "окружение"


def test_profile_env_without_driver_is_empty(tmp_path: Path):
    """План без `profiles.driver` ничего не подставляет — не читаем чужой профиль."""
    root = tmp_path / "bare"
    root.mkdir()
    (root / "pipeline.json").write_text(json.dumps({"project": "bare"}), encoding="utf-8")

    assert project_mod.profile_env(root) == {}


def test_snapshot_reports_token_from_profile_env(sandbox: Path, monkeypatch):
    """Снимок панели видит токен из `.env` профиля, а не только из своего окружения."""
    import snapshot as snap
    monkeypatch.delenv(TOKEN, raising=False)
    (sandbox.parent / "profiles" / "demo" / ".env").write_text(
        f"{TOKEN}=123:свой\nSTEP_PIPELINE_TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    cfg = json.loads((sandbox / "pipeline.json").read_text(encoding="utf-8"))
    cfg["notify"] = {"enabled": True}
    (sandbox / "pipeline.json").write_text(json.dumps(cfg), encoding="utf-8")

    state = snap.telegram_state(sandbox)

    assert state["token_set"] is True
    assert state["chat"] == "42"


def test_read_env_file_parses_export_quotes_and_skips_empty(tmp_path: Path):
    """Разбор `.env`: `export`, кавычки, комментарии; `КЛЮЧ=` не затирает окружение."""
    path = tmp_path / ".env"
    path.write_text(
        "# комментарий\n"
        "\n"
        "export STEP_PIPELINE_TELEGRAM_BOT_TOKEN=\"123:свой\"\n"
        "STEP_PIPELINE_TELEGRAM_CHAT_ID=42\n"
        "ПУСТОЙ=\n"
        "мусор без равенства\n",
        encoding="utf-8")

    values = project_mod.read_env_file(path)

    assert values[TOKEN] == "123:свой"
    assert values["STEP_PIPELINE_TELEGRAM_CHAT_ID"] == "42"
    assert "ПУСТОЙ" not in values
