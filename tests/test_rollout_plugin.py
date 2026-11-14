# -*- coding: utf-8 -*-
"""Жест «обновить плагин»: `POST /api/rollout/plugin` и запись итога в реестр профиля.

Установка подменена: настоящий GitHub в тестах не трогается. Проверяется то, чего у
`update.install` нет, — ответ панели, запись `rollout.plugin` в `projects.json`, 409 на
второй жест и 403 в предпросмотре.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from step_pipeline import project as proj
from step_pipeline import rollout, ui_server, update

ROUTE = "/api/rollout/plugin"
PANEL = {"X-Pipeline-Panel": "1"}

SUCCESS = {"ok": True, "updated": True, "from": "0.25.10", "to": "0.26.0",
           "latest": "0.26.0", "error": "",
           "message": "плагин обновлён: v0.25.10 → v0.26.0"}


def start_server(root: Path, preview: bool = False) -> dict:
    httpd = ui_server.make_server(root, port=0, preview=preview)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "httpd": httpd,
            "state": getattr(httpd, "state")}


def stop_server(server: dict) -> None:
    server["httpd"].shutdown()
    server["httpd"].server_close()


def post(url: str, payload: dict, path: str = ROUTE) -> tuple[int, dict]:
    """POST с JSON-телом; ошибки HTTP разбираются, а не бросаются."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{path}", data=data,
                                     headers={"Content-Type": "application/json", **PANEL})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def registry() -> dict:
    """Реестр профиля как он лежит на диске (пустой словарь, если файла нет)."""
    path = proj.projects_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Каталог проекта панели: `.pipeline` в нём не нужен — плагин один на профиль."""
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_rollout_plugin_success_writes_registry(project_dir: Path, tmp_path: Path,
                                                monkeypatch):
    """Успех: ответ несёт версию «после», реестр получает `rollout.plugin`, чужие ключи целы.

    Предзаполнение реестра — часть проверки C3: список `projects`, ключ `rollout.engine`
    (его заполнит S8A) и посторонний ключ контейнера обязаны уцелеть.
    """
    entries = [{"id": "p1", "name": "Проект", "root": str(tmp_path)}]
    registry_file = proj.projects_file()
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    registry_file.write_text(json.dumps({"projects": entries,
                                         "rollout": {"engine": {"ok": True}},
                                         "чужой": 1}, ensure_ascii=False),
                             encoding="utf-8")
    calls: list[str] = []

    def fake_install(tag=""):
        calls.append(tag)
        return dict(SUCCESS)

    monkeypatch.setattr(ui_server.update, "install", fake_install)
    server = start_server(project_dir)
    try:
        status, data = post(server["url"], {})
    finally:
        stop_server(server)
    assert status == 200
    assert rollout.STEPS[0] == "plugin"
    assert data["step"] == "plugin"
    assert data["ok"] is True and data["updated"] is True
    assert data["from"] == "0.25.10" and data["to"] == "0.26.0"
    assert data["latest"] == "0.26.0"
    assert data["error"] == "" and data["message"]
    assert data["at"]
    assert calls == [""]
    assert server["state"].update_info["current"] == "0.26.0"

    saved = registry()
    plugin = saved["rollout"]["plugin"]
    assert plugin["step"] == "plugin" and plugin["at"]
    assert plugin["ok"] is True and plugin["updated"] is True
    assert plugin["to"] == "0.26.0" and plugin["from"] == "0.25.10"
    assert saved["projects"] == entries
    assert saved["rollout"]["engine"] == {"ok": True}
    assert saved["чужой"] == 1


def test_rollout_plugin_error_rewrites_success(project_dir: Path, monkeypatch):
    """Отказ поверх успеха: 400 и запись без «обновлено» — прежний итог не остаётся."""
    monkeypatch.setattr(ui_server.update, "install", lambda tag="": dict(SUCCESS))
    server = start_server(project_dir)
    try:
        first_status, _first = post(server["url"], {})
        assert first_status == 200

        def broken_install(tag=""):
            raise update.UpdateError("нет сети")

        monkeypatch.setattr(ui_server.update, "install", broken_install)
        status, data = post(server["url"], {})
    finally:
        stop_server(server)
    assert status == 400
    assert data["ok"] is False
    assert "нет сети" in data["error"]
    plugin = registry()["rollout"]["plugin"]
    assert plugin["ok"] is False and plugin["updated"] is False
    assert plugin["to"] == ""
    assert "нет сети" in plugin["error"]


def test_rollout_plugin_second_gesture_is_busy(project_dir: Path, monkeypatch):
    """Второй жест, пока первый не закончил: 409, установка вызвана ровно один раз."""
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_install(tag=""):
        calls.append(tag)
        started.set()
        assert release.wait(5)
        return dict(SUCCESS)

    monkeypatch.setattr(ui_server.update, "install", slow_install)
    server = start_server(project_dir)
    first: list[tuple[int, dict]] = []
    try:
        worker = threading.Thread(target=lambda: first.append(post(server["url"], {})),
                                  daemon=True)
        worker.start()
        assert started.wait(5)
        status, data = post(server["url"], {})
        release.set()
        worker.join(10)
    finally:
        release.set()
        stop_server(server)
    assert status == 409
    assert data["busy"] is True
    assert first and first[0][0] == 200
    assert len(calls) == 1
    plugin = registry()["rollout"]["plugin"]
    assert plugin["ok"] is True and plugin["to"] == "0.26.0"


def test_rollout_plugin_forbidden_in_preview(tmp_path: Path, monkeypatch):
    """Предпросмотр только читает: 403, установка не зовётся, реестр не меняется."""
    calls: list[str] = []

    def fake_install(tag=""):
        calls.append(tag)
        return dict(SUCCESS)

    monkeypatch.setattr(ui_server.update, "install", fake_install)
    server = start_server(tmp_path, preview=True)
    try:
        status, data = post(server["url"], {})
    finally:
        stop_server(server)
    assert status == 403
    assert "предпросмотр" in data["error"]
    assert calls == []
    assert "rollout" not in registry()
