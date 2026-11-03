# -*- coding: utf-8 -*-
"""UI-сервер: снимок, действия, настройки. Без модели и без настоящей борды."""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import install_project, run_gate, stub_cards, complete_card  # noqa: E402
import project  # noqa: E402
import snapshot  # noqa: E402
import ui_server  # noqa: E402


@pytest.fixture(autouse=True)
def clean_cache():
    """Кэш снимка — состояние процесса: между тестами его обнуляем."""
    snapshot.clear_cache()
    yield
    snapshot.clear_cache()


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


def engine_counter(monkeypatch) -> list:
    """Счётчик запусков движка: `project.run_engine` — единственная точка всех запусков."""
    calls: list = []
    original = project.run_engine

    def counting(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(project, "run_engine", counting)
    return calls


def get_raw(url: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """GET с заголовками: 304 приходит HTTPError'ом с пустым телом. Заголовки — в нижнем регистре."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def open_events(url: str, timeout: float = 30.0) -> dict:
    """Открыть поток событий в потоке-демоне и копить разобранные кадры (строки `data:`).

    Тайм-аут сокета длинный: сервер молчит между кадрами (пинг раз в 10 с), а терпение
    теста задаёт `wait_frames`, а не чтение.
    """
    frames: list[dict] = []
    headers: dict = {}
    errors: list[BaseException] = []
    holder: dict = {}
    lock = threading.Lock()
    stop = threading.Event()

    def worker() -> None:
        try:
            response = urllib.request.urlopen(url, timeout=timeout)
            holder["response"] = response
            headers.update({k.lower(): v for k, v in response.headers.items()})
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    with lock:
                        frames.append(json.loads(line[5:].strip()))
                if stop.is_set():
                    break
        except BaseException as exc:            # noqa: BLE001 — поток не должен ронять прогон
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True, name="sse-reader")
    thread.start()
    return {"frames": frames, "headers": headers, "errors": errors, "stop": stop,
            "thread": thread, "lock": lock, "holder": holder}


def wait_frames(event: dict, count: int, limit: float = 5.0) -> list[dict]:
    """Дождаться count кадров: опрос списка с шагом 0.1 с и жёстким пределом."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        with event["lock"]:
            if len(event["frames"]) >= count:
                return list(event["frames"])
        time.sleep(0.1)
    with event["lock"]:
        return list(event["frames"])


def wait_event_frame(event: dict, predicate, limit: float = 15.0):
    """Дождаться кадра (кроме первого), который удовлетворяет предикату. None — не дождались.

    Пересборка снимка — три запуска движка, поэтому между изменением на диске и кадром
    проходит несколько секунд; промежуточные изменения дают лишние кадры, поэтому ждём
    именно нужный, а не «второй по счёту».
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        with event["lock"]:
            frames = list(event["frames"])
        for frame in frames[1:]:
            if predicate(frame):
                return frame
        time.sleep(0.1)
    return None


def close_events(event: dict) -> None:
    """Закрыть читателя и соединение: обработчик сервера получит закрытый сокет и выйдет."""
    event["stop"].set()
    response = event["holder"].get("response")
    if response is not None:
        try:
            response.close()
        except OSError:
            pass


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


# ── кэш снимка, ETag/304 и поток событий (план 3 S1) ──

def test_snapshot_cache_skips_engine_when_nothing_changed(server, monkeypatch):
    calls = engine_counter(monkeypatch)
    url = server["url"] + "/api/snapshot?plan=1"
    status, first = get(url)
    assert status == 200 and first["ok"]
    assert len(calls) == 3                      # планы + решение + шаги
    status, second = get(url)
    assert status == 200 and second["ok"]
    assert len(calls) == 3                      # второй запрос собран из кэша
    assert [p["id"] for p in second["plans"]] == [p["id"] for p in first["plans"]]
    assert second["plan"]["id"] == first["plan"]["id"]
    assert second["decision"] == first["decision"]


def test_snapshot_cache_rebuilds_after_state_change(server, monkeypatch):
    calls = engine_counter(monkeypatch)
    url = server["url"] + "/api/snapshot?plan=1"
    assert get(url)[0] == 200
    assert len(calls) == 3
    stop = server["root"] / ".pipeline" / "state" / "plan_1_stop.json"
    stop.write_text(json.dumps({"kind": "owner", "reason": "тест"}, ensure_ascii=False),
                    encoding="utf-8")
    status, data = get(url)
    assert status == 200
    assert data["stop"] is True
    assert len(calls) == 6                      # инвалидация отпечатком, а не по TTL


def test_snapshot_cache_rebuilds_after_plan_edit(server, monkeypatch):
    calls = engine_counter(monkeypatch)
    url = server["url"] + "/api/snapshot?plan=1"
    assert get(url)[0] == 200
    assert len(calls) == 3
    plan_file = server["root"] / "docs" / "plans" / "1-demo.md"
    text = plan_file.read_text(encoding="utf-8")
    edited = text.replace("| — | S1 | — | — | открыт |",
                          "| 03.11.2026 | S1 (Таблица счетов) | abc1234 | ревью APPROVE | ЗАКРЫТ |")
    assert edited != text
    plan_file.write_text(edited, encoding="utf-8")
    status, data = get(url)
    assert status == 200
    assert data["plan"]["closed"] == ["S1"]
    assert data["plan"]["next"]["id"] == "S2"
    assert len(calls) == 6


def test_snapshot_etag_and_304_do_not_touch_engine(server, monkeypatch):
    calls = engine_counter(monkeypatch)
    url = server["url"] + "/api/snapshot?plan=1"
    status, headers, body = get_raw(url)
    assert status == 200
    tag = headers["etag"]
    assert tag and tag.startswith('"') and tag.endswith('"')
    assert body
    assert headers["cache-control"] == "no-cache"
    assert len(calls) == 3
    status, headers, body = get_raw(url, {"If-None-Match": tag})
    assert status == 304
    assert body == b""
    assert headers["etag"] == tag
    assert len(calls) == 3                      # 304 движок не запускает


def test_events_stream_first_frame_is_snapshot(server):
    event = open_events(server["url"] + "/api/events?plan=1")
    try:
        # Холодный поток — это одна полная сборка снимка (три запуска движка: 5–6 с на этой
        # машине, под нагрузкой прогона дольше), поэтому первый кадр ждём до 20 с. Ждать
        # очередного опроса из страницы при этом не надо — его и нет.
        frames = wait_frames(event, 1, limit=20.0)
        assert frames, f"первый кадр не пришёл: {event['errors']}"
        assert event["headers"]["content-type"].startswith("text/event-stream")
        first = frames[0]
        assert first["ok"] is True
        assert [p["id"] for p in first["plans"]] == ["1"]
        assert first["plan"]["next"]["id"] == "S1"
    finally:
        close_events(event)


def test_events_stream_pushes_on_change(server, monkeypatch):
    calls = engine_counter(monkeypatch)
    event = open_events(server["url"] + "/api/events?plan=1")
    try:
        assert wait_frames(event, 1, limit=20.0), f"первый кадр не пришёл: {event['errors']}"
        (server["root"] / ".pipeline" / "state" / "plan_1_stop.json").write_text(
            json.dumps({"kind": "owner", "reason": "тест"}, ensure_ascii=False),
            encoding="utf-8")
        frames = wait_frames(event, 2, limit=15.0)
        assert len(frames) >= 2, f"кадр на изменение не пришёл: {event['errors']}"
        pushed = wait_event_frame(event, lambda frame: frame["stop"] is True, limit=15.0)
        assert pushed is not None, f"кадр со стоп-флагом не пришёл: {event['errors']}"
        assert len(calls) == 6                  # ровно одна пересборка, без опроса из страницы
    finally:
        close_events(event)


def test_tick_pushes_event_with_new_card(server):
    event = open_events(server["url"] + "/api/events?plan=1")
    try:
        assert wait_frames(event, 1, limit=20.0), f"первый кадр не пришёл: {event['errors']}"
        status, data = post(server["url"], {"action": "tick", "plan": "1"})
        assert status == 200 and data["ok"]
        frames = wait_frames(event, 2, limit=15.0)
        assert len(frames) >= 2, f"кадр после Тика не пришёл: {event['errors']}"
        pushed = wait_event_frame(
            event, lambda frame: "planloop:1:S1:prompt" in
            [c["idempotency_key"] for c in frame["cards"]], limit=20.0)
        assert pushed is not None, f"кадр с новой карточкой не пришёл: {event['errors']}"
    finally:
        close_events(event)


def test_index_subscribes_to_events_and_slow_poll():
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "EventSource" in html
    assert "/api/events" in html
    assert "setInterval(refresh, 30000)" in html
    assert "setInterval(refresh, 5000)" not in html

