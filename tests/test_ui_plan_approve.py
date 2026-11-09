# -*- coding: utf-8 -*-
"""Утверждение плана из панели: снимок черновиков, POST, предпросмотр, линтер.

Контракт: черновики видны в снимке (`drafts`), утверждение — отдельный маршрут
`POST /api/plan/accept` с заголовком панели `X-Pipeline-Panel`; в предпросмотре он
отклоняется, а черновик с ошибками не переносится и запись утверждения не появляется.
Тесты идут по проводу: настоящий `ThreadingHTTPServer` на 127.0.0.1 и HTTP через `urllib`.
"""
from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from helpers import install_project
import ui_server
from step_pipeline import plan_accept

PANEL = {"X-Pipeline-Panel": "1"}

# Тот же план, что в фикстуре, но с номером 5: черновик утверждается целиком.
DRAFT = helpers.PLAN.replace("# 1. Демо-план конвейера", "# 5. Новый план конвейера")
# Ни журнала, ни контракта шага: линтер обязан отказать.
BROKEN = """# 5. Совсем без журнала

## Шаги

### S1. Шаг (1 дн)

- Файл: `app/x.py`
"""


def _draft(root: Path, text: str = DRAFT, name: str = "5-demo.md") -> Path:
    """Положить черновик в `docs/plans/drafts` проекта."""
    folder = plan_accept.drafts_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def server(tmp_path: Path, hermes_stub: Path):
    """Панель над установленным проектом: снимок читает борду, но не борду владельца."""
    root = install_project(tmp_path / "ui-plan-accept", board="uiboard")
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
    (root / "docs" / "plans" / "drafts").mkdir(parents=True)
    (root / "docs" / "plans" / "drafts" / "5-demo.md").write_text(DRAFT, encoding="utf-8")
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
         path: str = "/api/plan/accept") -> tuple[int, dict]:
    """POST с JSON-телом; переданные заголовки перекрывают базовые."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{path}", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_snapshot_lists_drafts_with_title_and_steps(server):
    """Черновик виден в снимке: номер, путь, заголовок и число шагов."""
    _draft(server["root"])

    status, data = get(server["url"])
    assert status == 200
    rows = data["drafts"]
    assert len(rows) == 1
    assert rows[0]["id"] == "5"
    assert rows[0]["file"] == "docs/plans/drafts/5-demo.md"
    assert "Новый план конвейера" in rows[0]["title"]
    assert rows[0]["steps_total"] == 2


def test_new_draft_appears_after_first_snapshot(server):
    """Отпечаток снимка включает черновики: свежий файл виден без ожидания кэша."""
    status, data = get(server["url"])
    assert status == 200
    assert data["drafts"] == []

    _draft(server["root"])
    status, data = get(server["url"])
    assert status == 200
    assert [row["id"] for row in data["drafts"]] == ["5"]


def test_post_without_panel_header_is_rejected(server):
    """POST без заголовка панели — 403, черновик на месте, записи нет."""
    draft = _draft(server["root"])

    status, data = post(server["url"], {"plan": "5"})
    assert status == 403
    assert data["ok"] is False
    assert "X-Pipeline-Panel" in data["error"]
    assert draft.is_file()
    assert plan_accept.approval_file(server["root"], "5").exists() is False


def test_post_in_preview_is_read_only(preview_server):
    """В предпросмотре утверждение отклоняется, каталог состояния не появляется."""
    status, data = post(preview_server["url"], {"plan": "5"}, headers=PANEL)
    assert status == 403
    assert data["ok"] is False
    assert "предпросмотр только читает" in data["error"]
    assert not (preview_server["root"] / ".pipeline").exists()
    assert (preview_server["root"] / "docs" / "plans" / "drafts" / "5-demo.md").is_file()


def test_post_accepts_draft_moves_file_and_writes_sha(server):
    """Успешное утверждение: файл в каталоге планов, запись с sha256, черновика нет."""
    draft = _draft(server["root"])
    before = draft.read_bytes()

    status, data = post(server["url"], {"plan": "5"}, headers=PANEL)
    assert status == 200
    assert data["ok"] is True
    assert data["status"] == "approved"
    assert data["action"] == "plan_accept"
    assert data["plan"] == "5"
    assert "утверждён" in data["message"]
    assert draft.exists() is False

    final = server["root"] / "docs" / "plans" / "5-demo.md"
    assert final.is_file()
    assert final.read_bytes() == before
    record = json.loads(plan_accept.approval_file(server["root"], "5").read_text(encoding="utf-8"))
    assert record["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert record["file"] == "docs/plans/5-demo.md"

    # Снимок после утверждения: черновик ушёл, план появился, снимок не отдаёт кэш «до».
    status, snapshot = get(server["url"])
    assert status == 200
    assert snapshot["drafts"] == []
    assert any(str(row["id"]) == "5" for row in snapshot["plans"])


def test_post_reports_lint_errors_and_keeps_draft(server):
    """Черновик с ошибками: 400, список находок, файл и запись не тронуты."""
    draft = _draft(server["root"], BROKEN)

    status, data = post(server["url"], {"plan": "5"}, headers=PANEL)
    assert status == 400
    assert data["ok"] is False
    assert data["status"] == "errors"
    assert data["errors"]
    assert "ИТОГ: ошибок" in data["message"]
    assert draft.is_file()
    assert plan_accept.approval_file(server["root"], "5").exists() is False


def test_post_requires_numeric_plan(server):
    """Поле plan обязано быть номером: иначе 400 и никаких записей."""
    status, data = post(server["url"], {"plan": "пять"}, headers=PANEL)
    assert status == 400
    assert data["ok"] is False
    assert "plan" in data["error"]


def test_page_has_plan_approval_hooks(server):
    """Отдаваемая страница содержит блок черновиков, маршрут и кнопку утверждения."""
    with urllib.request.urlopen(server["url"] + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
        assert response.status == 200
    for needle in ('id="drafts"', "/api/plan/accept", "data-approve", "Утвердить",
                   "data.drafts", "renderDrafts", "approvePlan"):
        assert needle in html


def test_approve_core_matches_cli_outcome(server):
    """`plan_accept.approve` — тот же путь, что CLI: исход и сообщение без печати."""
    _draft(server["root"])
    result = plan_accept.approve(server["root"], "5")
    assert result["ok"] is True
    assert result["code"] == 0
    assert result["status"] == "approved"
    assert result["message"].startswith("План 5 утверждён:")
    assert result["sha256"]

    again = plan_accept.approve(server["root"], "5")
    assert again["ok"] is True
    assert again["status"] == "unchanged"
    assert "уже утверждён" in again["message"]
