# -*- coding: utf-8 -*-
"""Автозагрузка панели: файл записи на linux/darwin и задача планировщика на Windows.

Настоящая система тестами не трогается: `schtasks` подменяется фейковым runner'ом, а
пути автозапуска уводятся в tmp. Проверяется то, что важно владельцу: включение
создаёт носитель автозапуска с верным портом и проектом, выключение его убирает,
уже существующая автозагрузка не перезаписывается.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autostart  # noqa: E402


class FakeRunner:
    """Подмена `schtasks`: помнит вызовы и «наличие задачи», как настоящий планировщик.

    Задача появляется только успешным `/Create` и исчезает успешным `/Delete`: иначе
    фейк отвечал бы «задача есть» там, где её никто не создавал, и тесты проверяли бы
    не то поведение.
    """

    def __init__(self, codes: dict[str, int] | None = None, existing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.codes = codes or {}
        self.exists = existing

    def __call__(self, cmd: list[str]):
        self.calls.append(list(cmd))
        code = 0
        for key, value in self.codes.items():
            if key in cmd:
                code = value
                break
        if "/Query" in cmd:
            return SimpleNamespace(returncode=0 if self.exists else 1, stdout="", stderr="")
        if "/Create" in cmd and code == 0:
            self.exists = True
        if "/Delete" in cmd and code == 0:
            self.exists = False
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


@pytest.fixture()
def panel_home(tmp_path: Path, isolated_hermes_home: Path) -> Path:
    """Home профиля: там лежат сценарии автозагрузки Windows."""
    return isolated_hermes_home


def test_status_linux_reads_autostart_file(tmp_path: Path, monkeypatch):
    """Linux: «включено» — это файл в автозапуске сеанса, а не запись в настройках."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "proj"
    project.mkdir()

    empty = autostart.status(project, kind="linux")
    assert empty["enabled"] is False and empty["supported"] is True

    result = autostart.enable(project, 8770, kind="linux")
    path = tmp_path / "config" / "autostart" / autostart.DESKTOP_NAME
    assert result["ok"] is True and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "pipeline ui" in text and "--port 8770" in text
    assert str(project) in text
    assert autostart.status(project, kind="linux")["enabled"] is True

    off = autostart.disable(kind="linux")
    assert off["ok"] is True and not path.exists()


def test_enable_is_idempotent_and_keeps_existing(tmp_path: Path, monkeypatch):
    """Уже включённую автозагрузку не переписываем: «включено» — это ответ."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "proj"
    project.mkdir()
    path = tmp_path / "config" / "autostart" / autostart.DESKTOP_NAME
    path.parent.mkdir(parents=True)
    path.write_text("свой сценарий владельца\n", encoding="utf-8")

    result = autostart.enable(project, 8770, kind="linux")

    assert result["ok"] is True and result["already"] is True
    assert path.read_text(encoding="utf-8") == "свой сценарий владельца\n"


def test_enable_refuses_random_port(tmp_path: Path):
    """Порт 0 записать нельзя: в автозагрузке он означал бы «каждый раз новый адрес»."""
    result = autostart.enable(tmp_path, 0, kind="linux")
    assert result["ok"] is False
    assert "--port" in result["error"]


def test_status_darwin_uses_launch_agent(tmp_path: Path):
    """macOS: LaunchAgent в `~/Library/LaunchAgents` с RunAtLoad."""
    project = tmp_path / "proj"
    project.mkdir()
    result = autostart.enable(project, 8771, kind="darwin", home=tmp_path / "home")
    path = tmp_path / "home" / "Library" / "LaunchAgents" / autostart.PLIST_NAME
    assert result["ok"] is True and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key>" in text and "<string>8771</string>" in text
    assert autostart.status(kind="darwin", home=tmp_path / "home")["enabled"] is True


def test_windows_enable_creates_task_and_hidden_scripts(tmp_path: Path, panel_home: Path):
    """Windows: задача ONLOGON, скрытый запуск через wscript и цикл перезапуска."""
    project = tmp_path / "proj"
    project.mkdir()
    runner = FakeRunner()

    result = autostart.enable(project, 8770, kind="windows", runner=runner, home=panel_home)

    assert result["ok"] is True
    folder = panel_home / "panel"
    assert (folder / autostart.CMD_NAME).is_file()
    vbs = (folder / autostart.VBS_NAME).read_text(encoding="utf-8")
    assert autostart.CMD_NAME in vbs and "WScript.Shell" in vbs
    cmd = (folder / autostart.CMD_NAME).read_text(encoding="utf-8")
    assert '"--port" "8770"' in cmd and str(project) in cmd
    assert "goto loop" in cmd
    # Живая панель узнаётся запросом, а не повторным запуском: иначе цикл молотит
    # «порт занят» и владелец видит ERR_CONNECTION_REFUSED вместо пульта.
    assert 'curl.exe -s -o nul --max-time 2 "http://127.0.0.1:8770/api/health"' in cmd
    assert "if not errorlevel 1" in cmd
    # cmd.exe разбирает .cmd построчно и на одиночных \n срывается: «'g' is not recognized»,
    # «'10' is not recognized», цикл уходит в холостой ход и панель не поднимается вовсе.
    raw = (folder / autostart.CMD_NAME).read_bytes()
    assert raw.count(b"\r\n") > 5 and raw.count(b"\n") == raw.count(b"\r\n"), "нужны CRLF"
    assert "\n\n" not in cmd, "перевод строки не должен удваиваться"
    create = [command for command in runner.commands() if "/Create" in command]
    assert create and f"/TN {autostart.TASK_NAME}" in create[0]
    assert "/SC ONLOGON" in create[0]
    assert "wscript.exe" in create[0]


def test_windows_status_queries_task(tmp_path: Path, monkeypatch):
    """Windows: статус спрашивает саму задачу, а не файл настроек панели."""
    project = tmp_path / "proj"
    project.mkdir()
    absent = FakeRunner(existing=False)
    assert autostart.status(project, kind="windows", runner=absent)["enabled"] is False

    present = FakeRunner(existing=True)
    status = autostart.status(project, kind="windows", runner=present)
    assert status["enabled"] is True
    assert "Pipeline_Panel" in status["mechanism"]
    assert present.commands() == [f"schtasks /Query /TN {autostart.TASK_NAME} /FO LIST"]


def test_windows_disable_stops_and_deletes_task(tmp_path: Path):
    """Windows: выключение гасит цикл и удаляет задачу — статус это подтверждает."""
    project = tmp_path / "proj"
    project.mkdir()
    runner = FakeRunner(existing=True)
    assert autostart.status(project, kind="windows", runner=runner)["enabled"] is True

    result = autostart.disable(kind="windows", runner=runner)

    calls = runner.commands()
    assert any("/End" in command for command in calls)
    assert any("/Delete" in command for command in calls)
    assert result["ok"] is True
    assert result["status"]["enabled"] is False


def test_windows_enable_reports_schtasks_failure(tmp_path: Path, panel_home: Path):
    """Отказ `schtasks` не выдаётся за успех: текст утилиты уходит владельцу."""
    project = tmp_path / "proj"
    project.mkdir()
    runner = FakeRunner({"/Create": 1})

    result = autostart.enable(project, 8770, kind="windows", runner=runner, home=panel_home)

    assert result["ok"] is False
    assert result["error"]


def test_platform_kind_matches_os():
    """Платформа определяется по системе: чекбокс не должен врать о механизме."""
    kind = autostart.platform_kind()
    assert kind in ("windows", "darwin", "linux")
    assert (kind == "windows") == (sys.platform.startswith("win"))
