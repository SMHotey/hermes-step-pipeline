# -*- coding: utf-8 -*-
"""Канал уведомлений в `doctor`: три причины вместо одной немой строки (шаг 15-S5).

План 15 требовал от диагностики различить «выключено владельцем», «нет переменной токена» и
«нет переменной чата» и к каждой причине дать команду, которая её лечит. Проверяем функцию
`doctor.notify_rows` напрямую, без снимка, борды и подпроцессов: у неё собственный вход, а
сеть подменена так, что вызов `get_me` роняет тест — при выключенном канале и при недостаче
значений его быть не должно.

Значения в `.env` фиктивные (`123:FAKE`, `42`): секрет в тестах не живёт, а тест «имена, а не
значения» утверждает, что ни одна строка отчёта токена не содержит.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from step_pipeline import doctor, setup_flow
from step_pipeline import project as proj
from step_pipeline import snapshot as snap

TOKEN = "123:FAKE"
CHAT = "42"
TOKEN_ENV = setup_flow.TOKEN_ENV
CHAT_ENV = setup_flow.CHAT_ENV


def notify_cfg(project: Path, **flags) -> dict:
    """Конфиг фикстурного проекта с состоянием канала из теста (остальные ключи — как есть)."""
    cfg = proj.read_config(project)
    notify = dict(cfg.get("notify") or {})
    notify.update(flags)
    cfg["notify"] = notify
    return cfg


def driver_of(cfg: dict) -> str:
    return setup_flow.roles_from_config(cfg)[0]


def write_env(project: Path, driver: str, *, token: bool = False, chat: bool = False) -> None:
    """Положить в `.env` профиля драйвера только запрошенные переменные.

    Путь берётся через `proj.profile_dir`: автоподстановка `tests/conftest.py` подменяет его
    на pytest-tmp, поэтому настоящие профили Hermes тест не трогает.
    """
    lines: list[str] = []
    if token:
        lines.append(f"{TOKEN_ENV}={TOKEN}")
    if chat:
        lines.append(f"{CHAT_ENV}={CHAT}")
    path = proj.profile_dir(driver) / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


@pytest.fixture()
def no_network(monkeypatch):
    """Telegram, который падает при вызове: там, где сети быть не должно, тест это докажет."""
    def boom(*_args, **_kwargs):
        raise AssertionError("сеть вызвана")

    monkeypatch.setattr(doctor.telegram, "get_me", boom)


class TestDisabledChannel:
    def test_owner_disabled_channel_names_owner_and_recipe(self, project: Path, no_network):
        """Выключено владельцем, значений нет: причина названа и рецепт ведёт к мастеру."""
        cfg = notify_cfg(project, enabled=False, skipped=True)
        driver = driver_of(cfg)

        notes, problems = doctor.notify_rows(project, cfg, driver)

        assert problems == []
        assert len(notes) == 1
        line = notes[0]
        assert "выключен владельцем" in line
        assert "notify.skipped=true" in line
        assert "hermes pipeline setup" in line
        assert "--yes" in line and "--no-smoke" in line
        # Порядок ветвей (Р10 постановки): у включённого канала `skipped` не спрашивается.
        cfg["notify"]["enabled"] = True
        forced_notes, forced_problems = doctor.notify_rows(project, cfg, driver)
        assert forced_notes == []
        assert len(forced_problems) == 1

    def test_channel_off_without_skip_is_unfinished_setup(self, project: Path, no_network):
        """`enabled=false` без `skipped` — незакрытый шаг установки, а не выбор владельца."""
        cfg = notify_cfg(project, enabled=False)
        assert "skipped" not in (cfg.get("notify") or {})
        driver = driver_of(cfg)

        notes, problems = doctor.notify_rows(project, cfg, driver)

        assert problems == []
        assert len(notes) == 1
        assert "шаг Telegram в установке не закрыт" in notes[0]
        assert "hermes pipeline setup" in notes[0]

    def test_disabled_channel_with_env_values_shows_enable_command(
            self, project: Path, no_network):
        """Значения уже лежат: хватает включить канал одним ключом конфига."""
        cfg = notify_cfg(project, enabled=False, skipped=True)
        driver = driver_of(cfg)
        write_env(project, driver, token=True, chat=True)

        notes, problems = doctor.notify_rows(project, cfg, driver)

        assert problems == []
        assert len(notes) == 1
        line = notes[0]
        assert "выключен владельцем" in line
        assert "hermes pipeline config --project" in line
        assert "--set notify.enabled true" in line


class TestEnabledChannel:
    def test_enabled_channel_without_token_is_a_problem(self, project: Path, no_network):
        """Включённый канал без токена теряет уведомления молча — это проблема, не заметка."""
        cfg = notify_cfg(project, enabled=True)
        driver = driver_of(cfg)
        write_env(project, driver, chat=True)

        notes, problems = doctor.notify_rows(project, cfg, driver)

        assert notes == []
        assert len(problems) == 1
        assert TOKEN_ENV in problems[0]
        assert "hermes pipeline setup" in problems[0]
        assert "@" not in problems[0]

    def test_enabled_channel_without_chat_is_a_problem(self, project: Path, no_network):
        """Пинг без chat id бессмыслен: отказ обязан быть проблемой и без сети."""
        cfg = notify_cfg(project, enabled=True)
        driver = driver_of(cfg)
        write_env(project, driver, token=True)

        notes, problems = doctor.notify_rows(project, cfg, driver)

        assert notes == []
        assert len(problems) == 1
        assert CHAT_ENV in problems[0]
        assert "hermes pipeline setup" in problems[0]

    def test_enabled_channel_reports_ready_bot_and_chat(self, project: Path, monkeypatch):
        """Настроенный канал печатает «канал настроен» с именем бота и chat id."""
        cfg = notify_cfg(project, enabled=True)
        driver = driver_of(cfg)
        write_env(project, driver, token=True, chat=True)
        monkeypatch.setattr(doctor.telegram, "get_me",
                            lambda _token, **_kwargs: {"username": "pipe_bot"})

        notes, problems = doctor.notify_rows(project, cfg, driver)

        assert problems == []
        assert notes == [f"Telegram: канал настроен — @pipe_bot → {CHAT}"]

    def test_output_names_variables_but_not_values(self, project: Path, monkeypatch):
        """Токен не попадает ни в строки, ни в готовый текст отчёта: только имена."""
        cfg = notify_cfg(project, enabled=True)
        driver = driver_of(cfg)
        write_env(project, driver, token=True, chat=True)
        monkeypatch.setattr(doctor.telegram, "get_me",
                            lambda _token, **_kwargs: {"username": "pipe_bot"})

        notes, problems = doctor.notify_rows(project, cfg, driver)
        report = doctor.Report(notes=list(notes), problems=list(problems))

        assert TOKEN not in doctor.format_report(report)
        for line in notes + problems:
            assert TOKEN not in line


class TestCollectWiring:
    def test_collect_uses_notify_rows(self, project: Path, monkeypatch, tmp_path: Path):
        """`collect` только распаковывает: уровня note у канала фикстуры хватает на итог OK.

        Итог `doctor` в этом тесте не проверяется: у фикстурного проекта свои проблемы
        (доверие, ключи), к каналу они не относятся. Внешние источники погашены так же, как
        в `tests/test_doctor_drift.py`.
        """
        db = tmp_path / "kanban.db"
        db.write_text("", encoding="utf-8")
        monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
        monkeypatch.setattr(snap, "snapshot", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(proj, "run_hermes",
                            lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="",
                                                                      stderr=""))

        report = doctor.collect(project, with_lint=False)
        text = doctor.format_report(report)

        assert any("шаг Telegram в установке не закрыт" in note for note in report.notes)
        assert "Telegram: не настроен" not in text
        assert not any(problem.startswith("Telegram:") for problem in report.problems)
