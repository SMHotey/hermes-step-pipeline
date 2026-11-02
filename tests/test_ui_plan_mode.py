# -*- coding: utf-8 -*-
"""Тумблер режима планировщика в панели: снимок, POST, заголовок панели, предпросмотр.

Шесть тестов фиксируют контракт шага S3 плана 4: поле `plan_mode` в снимке состояния,
маршрут `POST /api/plan-mode` с телом `{"on": true|false}` и обязательным заголовком
панели `X-Pipeline-Panel`, отказ в предпросмотре и идемпотентность записи. Тесты идут
по проводу: настоящий `ThreadingHTTPServer` на 127.0.0.1 и HTTP через `urllib`.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from helpers import install_project
import ui_server
from step_pipeline import plan_mode

PANEL = {"X-Pipeline-Panel": "1"}


@pytest.fixture()
def server(tmp_path: Path, hermes_stub: Path):
    """Панель над настоящим проектом: без поддельного `hermes` снимок читал бы борду владельца."""
    root = install_project(tmp_path / "ui-plan-mode", board="uiboard")
    httpd = ui_server.make_server(root, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "root": root, "httpd": httpd}
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def preview_server(tmp_path: Path):
    """Предпросмотр над каталогом без установленного конвейера — только чтение."""
    root = tmp_path / "bare"
    root.mkdir()
    httpd = ui_server.make_server(root, port=0, preview=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "root": root, "httpd": httpd}
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url: str, path: str = "/api/snapshot") -> tuple[int, dict]:
    """GET и разбор JSON; ошибки HTTP тоже разбираются, а не бросаются."""
    try:
        with urllib.request.urlopen(url + path, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def post(url: str, payload: dict, headers: dict | None = None,
         path: str = "/api/plan-mode") -> tuple[int, dict]:
    """POST с JSON-телом; переданные заголовки перекрывают базовые."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{path}", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_snapshot_reports_plan_mode_on_by_default(server):
    """Нет файла состояния — режим включён, и чтение снимка файла не создаёт."""
    status, data = get(server["url"])
    assert status == 200
    assert data["ok"] is True
    assert data["plan_mode"] is True
    assert plan_mode.state_file(server["root"]).exists() is False


def test_snapshot_reflects_off_from_state_file(server):
    """Снимок читает выключенный режим из файла состояния проекта."""
    plan_mode.write_mode(server["root"], False)
    status, data = get(server["url"])
    assert status == 200
    assert data["plan_mode"] is False


def test_post_without_panel_header_is_rejected(server):
    """POST без заголовка панели — 403, состояние не меняется и файл не появляется."""
    status, data = post(server["url"], {"on": False})
    assert status == 403
    assert data["ok"] is False
    assert "X-Pipeline-Panel" in data["error"]
    assert plan_mode.state_file(server["root"]).exists() is False
    status, snapshot = get(server["url"])
    assert status == 200
    assert snapshot["plan_mode"] is True


def test_post_in_preview_is_read_only(preview_server):
    """В предпросмотре переключение отклоняется с 403 и каталог состояния не появляется."""
    status, data = post(preview_server["url"], {"on": False}, headers=PANEL)
    assert status == 403
    assert data["ok"] is False
    assert "предпросмотр только читает" in data["error"]
    assert not (preview_server["root"] / ".pipeline").exists()


def test_post_changes_state_and_is_idempotent(server):
    """POST меняет файл состояния; повтор не перезаписывает changed_at; не-булево — 400."""
    status, data = post(server["url"], {"on": False}, headers=PANEL)
    assert status == 200
    assert data["ok"] is True
    assert data["mode"] == "off"
    assert data["changed"] is True
    path = plan_mode.state_file(server["root"])
    assert data["file"] == str(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_mode"] is False
    datetime.fromisoformat(payload["changed_at"])          # дата разбирается
    stamp = payload["changed_at"]
    assert plan_mode.read_mode(server["root"]) is False

    again_status, again = post(server["url"], {"on": False}, headers=PANEL)
    assert again_status == 200
    assert again["changed"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["changed_at"] == stamp

    bad_status, bad = post(server["url"], {"on": "yes"}, headers=PANEL)
    assert bad_status == 400
    assert bad["ok"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["plan_mode"] is False


def test_page_has_plan_mode_toggle(server):
    """Отдаваемая страница содержит тумблер, подсказку, маршрут и чтение поля снимка."""
    with urllib.request.urlopen(server["url"] + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
        assert response.status == 200
    for needle in ("Режим планировщика", "со следующей сессии", 'id="plan-mode"',
                   "/api/plan-mode", "X-Pipeline-Panel", "data.plan_mode"):
        assert needle in html
