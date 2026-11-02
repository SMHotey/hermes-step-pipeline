# -*- coding: utf-8 -*-
"""UI-сервер: снимок, действия, настройки. Без модели и без настоящей борды."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import install_project, run_gate, stub_cards, complete_card  # noqa: E402
import ui_server  # noqa: E402


@pytest.fixture()
def server(tmp_path: Path, hermes_stub: Path):
    root = install_project(tmp_path / "ui-project", board="uiboard")
    httpd = ui_server.make_server(root, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "root": root, "db": hermes_stub, "httpd": httpd,
               "state": httpd.RequestHandlerClass.state}
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    """POST действия панели: по умолчанию JSON, переданные заголовки перекрывают."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}/api/action", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def stop_file(server) -> Path:
    """Файл стоп-флага плана 1: его появление значит, что действие выполнилось."""
    return server["root"] / ".pipeline" / "state" / "plan_1_stop.json"


def test_index_page(server):
    with urllib.request.urlopen(server["url"] + "/", timeout=10) as response:
        html = response.read().decode("utf-8")
    assert response.status == 200
    assert "Конвейер шагов" in html
    assert "/api/snapshot" in html


def test_snapshot_lists_plans_and_steps(server):
    status, data = get(server["url"] + "/api/snapshot")
    assert status == 200 and data["ok"]
    assert data["project"] == "demo"
    assert data["board"] == "uiboard"
    assert [p["id"] for p in data["plans"]] == ["1"]
    assert [s["id"] for s in data["plan"]["steps"]] == ["S1", "S2"]
    assert data["plan"]["next"]["id"] == "S1"
    assert data["decision"]["action"] == "create_step"


def test_tick_creates_prompt_card(server):
    status, data = post(server["url"], {"action": "tick"})
    assert status == 200 and data["ok"]
    cards = stub_cards(server["db"])
    assert any(c["key"] == "planloop:1:S1:prompt" for c in cards)
    assert data["snapshot"]["plan"]["id"] == "1"


def test_stop_and_resume(server):
    status, data = post(server["url"], {"action": "stop", "reason": "переезд"})
    assert status == 200 and data["ok"]
    stop = json.loads((server["root"] / ".pipeline" / "state" /
                       "plan_1_stop.json").read_text(encoding="utf-8"))
    assert stop["kind"] == "owner"
    status, data = post(server["url"], {"action": "resume"})
    assert status == 200 and data["ok"]
    assert not (server["root"] / ".pipeline" / "state" / "plan_1_stop.json").exists()


def test_hold_and_unhold(server):
    assert post(server["url"], {"action": "hold", "step": "S1"})[0] == 200
    state = json.loads((server["root"] / ".pipeline" / "state" /
                        "plan_1_state.json").read_text(encoding="utf-8"))
    assert state["hold_steps"] == ["S1"]
    assert post(server["url"], {"action": "unhold", "step": "S1"})[0] == 200
    state = json.loads((server["root"] / ".pipeline" / "state" /
                        "plan_1_state.json").read_text(encoding="utf-8"))
    assert state["hold_steps"] == []


def test_hold_without_step_is_error(server):
    status, data = post(server["url"], {"action": "hold"})
    assert status == 400 and not data["ok"]


def test_save_config(server):
    status, data = post(server["url"], {"action": "save_config", "config": {
        "board": "newboard", "driven_plans": "1,2", "require_prompt": False,
        "runtimes": {"step": "5h"}}})
    assert status == 200 and data["ok"]
    cfg = json.loads((server["root"] / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["board"] == "newboard"
    assert cfg["driven_plans"] == ["1", "2"]
    assert cfg["require_prompt"] is False
    assert cfg["runtimes"]["step"] == "5h"
    assert cfg["runtimes"]["review"] == "150m"     # остальные значения не потерялись


def test_save_config_rejects_empty_board(server):
    status, data = post(server["url"], {"action": "save_config", "config": {"board": "  "}})
    assert status == 400 and not data["ok"]


def test_unknown_action(server):
    status, data = post(server["url"], {"action": "launch"})
    assert status == 400


def test_cards_and_journal_in_snapshot(server):
    run_gate(server["root"], "--run")            # карточка постановки
    (server["root"] / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text("# p\n", encoding="utf-8")
    run_gate(server["root"], "--run")            # карточка шага
    complete_card(server["db"], ":step")
    run_gate(server["root"], "--run")            # карточка ревью
    status, data = get(server["url"] + "/api/snapshot?plan=1")
    assert status == 200
    keys = [c["idempotency_key"] for c in data["cards"]]
    assert any(k.endswith(":review:1") for k in keys)
    assert data["plan"]["steps"][0]["closed"] is False


def test_post_with_text_plain_is_rejected(server):
    status, data = post(server["url"], {"action": "stop", "reason": "csrf"},
                        headers={"Content-Type": "text/plain"})
    assert status == 415
    assert data["ok"] is False
    assert "application/json" in data["error"]
    assert not stop_file(server).exists()                # действие не выполнилось
    assert server["state"].tail() == []                   # движок не запускался


def test_post_with_foreign_origin_is_rejected(server):
    status, data = post(server["url"], {"action": "stop", "reason": "csrf"},
                        headers={"Origin": "http://evil.example"})
    assert status == 403
    assert data["ok"] is False
    assert not stop_file(server).exists()
    assert server["state"].tail() == []


def test_post_with_cross_site_fetch_site_is_rejected(server):
    status, data = post(server["url"], {"action": "stop", "reason": "csrf"},
                        headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert data["ok"] is False
    assert not stop_file(server).exists()
    assert server["state"].tail() == []


def test_post_without_origin_works_like_curl(server):
    status, data = post(server["url"], {"action": "stop", "reason": "curl"})
    assert status == 200 and data["ok"]
    assert stop_file(server).exists()
    stop = json.loads(stop_file(server).read_text(encoding="utf-8"))
    assert stop["kind"] == "owner"
    assert stop["reason"] == "curl"


def test_post_from_panel_origin_works(server):
    status, data = post(server["url"], {"action": "stop", "reason": "панель"},
                        headers={"Origin": server["url"], "Sec-Fetch-Site": "same-origin"})
    assert status == 200 and data["ok"]
    assert stop_file(server).exists()


def test_security_headers_on_page(server):
    with urllib.request.urlopen(server["url"] + "/", timeout=10) as response:
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    with urllib.request.urlopen(server["url"] + "/api/health", timeout=10) as response:
        assert response.headers["X-Content-Type-Options"] == "nosniff"

