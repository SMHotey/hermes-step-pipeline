# -*- coding: utf-8 -*-
"""UI-сервер: снимок, действия, настройки. Без модели и без настоящей борды."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import KANBAN_SCHEMA, install_project, run_gate, stub_cards, complete_card  # noqa: E402
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


# ── экран «Шаг»: контракт, постановка, вердикт, коммит (план 3 S3) ──

REVIEW_APPROVE = """# Ревью: план 1, шаг S1

## ВЕРДИКТ: APPROVE

## Объект ревью
Объект ревью: коммит `{sha}` (HEAD, ветка main).

## Находки

### Блокирующие
— нет

### Значимые
- Обрезка длинного текста проверена, пометка есть
- Коммит шага совпадает с HEAD
"""

# Отчёт в форматах, которыми реально пишут ревью: строка роли `[класс] file:line — …`,
# нумерованные пункты и жирный пункт с классом в скобках (находка Б1 ревью 3-S3).
REVIEW_FORMATS = """# Ревью: план 1, шаг S1

## ВЕРДИКТ: REQUEST_CHANGES

Объект ревью: коммит `abc1234`

## Находки

### Блокирующие

[блокирующая] `app/a.py:12` — выход не проверяется — ожидается проверка

### Значимые

1. `app/b.py:3` — тихая обрезка
   продолжение пункта
2. **[значимая]** `app/c.py:7` — нет пометки обрезки

### Стилистические

**С1 (стилистическая, расширение объёма).** `app/d.py:9` — лишний помощник
"""


def write_prompt(server, text: str = "## Постановка шага S1\n\n- границы: только app/models\n") -> Path:
    path = server["root"] / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_review(server, sha: str = "abc1234") -> Path:
    path = server["root"] / "docs" / "reviews" / "1-S1.md"
    path.write_text(REVIEW_APPROVE.format(sha=sha), encoding="utf-8")
    return path


def add_comment(db: Path, task_id: str, body: str, author: str = "reviewer") -> None:
    """Комментарий карточки в поддельной борде: своей схемой, как у настоящей."""
    con = sqlite3.connect(db)
    try:
        con.executescript("CREATE TABLE IF NOT EXISTS task_comments ("
                          "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
                          " author TEXT, body TEXT, created_at INTEGER);")
        con.execute("INSERT INTO task_comments (task_id, author, body, created_at)"
                    " VALUES (?, ?, ?, ?)", (task_id, author, body, 1))
        con.commit()
    finally:
        con.close()


def add_stub_cards(db: Path, key_prefix: str, count: int, created_at: int) -> None:
    """Карточки чужого шага в поддельной борде: они новее карточки проверяемого шага."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        con.executemany(
            "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
            " created_at, result, tenant) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(f"t_fill{index:03d}", "чужая карточка", "done", "pipeline",
              f"{key_prefix}{index}", created_at + index + 1, "ok", "plan")
             for index in range(count)])
        con.commit()
    finally:
        con.close()


def set_card_result(db: Path, task_id: str, result: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET result = ? WHERE id = ?", (result, task_id))
        con.commit()
    finally:
        con.close()


def test_step_screen_shows_contract_and_prompt(server):
    write_prompt(server)
    status, data = get(server["url"] + "/api/step?plan=1&id=s1")
    assert status == 200 and data["ok"]
    assert data["plan"] == "1" and data["step"] == "S1"
    assert data["title"].startswith("Таблица счетов")
    contract = data["contract"]
    assert contract["found"] is True
    assert contract["files"] == ["app/models/invoice.py", "tests/test_invoice.py"]
    assert contract["gates"] == ["python -m pytest tests/test_invoice.py -q"]
    assert contract["done"].startswith("таблица `invoice` создана")
    assert data["prompt"]["exists"] is True
    assert data["prompt"]["path"] == "docs/prompts/2026-11-01_1-S1_prompt.md"
    assert "Постановка шага S1" in data["prompt"]["text"]
    # Отчёта ревью нет — экран это пишет, а не выдумывает вердикт.
    assert data["review"]["exists"] is False and data["review"]["verdict"] == "NONE"
    assert data["commit"]["available"] is False


def test_step_screen_shows_verdict_and_findings(server):
    write_review(server)
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    review = data["review"]
    assert review["exists"] is True and review["path"] == "docs/reviews/1-S1.md"
    assert review["verdict"] == "APPROVE" and review["occurrences"] == 1
    assert review["commit"] == "abc1234"
    assert [item["level"] for item in review["findings"]] == ["Значимые", "Значимые"]
    assert "Обрезка" in review["findings"][0]["text"]


def test_step_screen_parses_role_numbered_and_bold_findings(server):
    """Находки разбираются во всех форматах отчётов: роль, нумерация, жирный пункт (Б1)."""
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(REVIEW_FORMATS,
                                                                 encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    review = data["review"]
    assert [item["level"] for item in review["findings"]] == [
        "блокирующая", "Значимые", "значимая", "стилистическая"]
    assert "app/a.py:12" in review["findings"][0]["text"]
    assert "продолжение пункта" in review["findings"][1]["text"]
    assert "app/c.py:7" in review["findings"][2]["text"]
    assert "app/d.py:9" in review["findings"][3]["text"]
    assert review["findings_note"] == ""


def test_step_screen_marks_unparsed_findings_section(server):
    """Непустой раздел без разобранных пунктов помечается, а не выглядит пустым."""
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(
        "# Ревью\n\n## ВЕРДИКТ: APPROVE\n\n## Находки\n\n"
        "Сплошной текст без пунктов и меток класса.\n", encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    assert data["review"]["findings"] == []
    assert "разобрать" in data["review"]["findings_note"]


# Итоги прошлого круга перед разделом шага: таблица «Закрытие находок» (1-S2) и пункты
# «Находки круга 1 — закрыты» (1-S4) — разделом находок шага не считаются (Б1 круга 2).
REVIEW_CLOSED_TABLE = """# Ревью: план 1, шаг S1

## ВЕРДИКТ: REQUEST_CHANGES

## 5. Закрытие находок круга 1

| находка | итог |
|---|---|
| закрытая первая | закрыта |
| закрытая вторая | закрыта |

## 7. Находки

- `app/a.py:12` — текущая находка первая
- `app/b.py:13` — текущая находка вторая
"""

REVIEW_CLOSED_HEADING = """# Ревью: план 1, шаг S1

## ВЕРДИКТ: REQUEST_CHANGES

## 5. Находки круга 1 — закрыты

- `app/old.py:1` — закрытая находка первая
- `app/old.py:2` — закрытая находка вторая

## 6. Находки этого круга

- `app/c.py:7` — текущая находка третья
- `app/d.py:9` — текущая находка четвёртая
"""

# Точный «Находки» идёт позже разделов-кандидатов: он и должен победить.
REVIEW_EXACT_LATER = """# Ревью: план 1, шаг S1

## ВЕРДИКТ: REQUEST_CHANGES

## 5. Находки этого круга

- `app/e.py:5` — не тот раздел

## 7. Находки

- `app/f.py:6` — точный раздел
"""


@pytest.mark.parametrize("report,expected,marker", [(REVIEW_CLOSED_TABLE, 2, "текущая"),
                                                   (REVIEW_CLOSED_HEADING, 2, "текущая"),
                                                   (REVIEW_EXACT_LATER, 1, "точный")])
def test_step_findings_skip_closed_rounds(server, report, expected, marker):
    """Раздел находок — «Находки» шага, а не итоги круга 1 (Б1 ревью 3-S3)."""
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(report, encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    review = data["review"]
    assert len(review["findings"]) == expected
    assert all(marker in item["text"] for item in review["findings"])
    assert review["findings_note"] == ""


def test_step_finding_text_clips_with_note(server):
    """Длинный текст находки обрезается с пометкой, а не молча (Б2 ревью 3-S3)."""
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(
        "# Ревью\n\n## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n- начало находки "
        + "я" * 1500 + "\n", encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    item = data["review"]["findings"][0]
    assert len(item["text"]) == 1200 and item["truncated"] is True
    assert item["total"] > 1200
    assert item["note"] == f"показаны первые 1200 символов, всего {item['total']}"
    # Страница обязана донести пометку обрезки до блока находок.
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "item.note" in html


@pytest.mark.parametrize("line", ["", "Находок нет.", "Блокирующих и значимых находок нет.",
                                  "Замечаний нет."])
def test_step_findings_absence_forms_are_not_an_error(server, line):
    """«Находок нет.» — не ошибка разбора, пометки быть не должно (значимая 1 ревью 3-S3)."""
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(
        "# Ревью\n\n## ВЕРДИКТ: APPROVE\n\n## Находки\n\n" + (line + "\n" if line else ""),
        encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    assert data["review"]["findings"] == []
    assert data["review"]["findings_note"] == ""


def test_step_finding_continuation_starting_with_net_is_kept(server):
    """Строка «Нет …» продолжает жирный пункт, а не отбрасывается (значимая 2 ревью 3-S3)."""
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(
        "# Ревью\n\n## ВЕРДИКТ: REQUEST_CHANGES\n\n## Находки\n\n"
        "**[стилистическая] формат отчёта — неполон.**\n"
        "Нет пофайловых адресов `path:line` и нет остатка владельцу.\n", encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    findings = data["review"]["findings"]
    assert len(findings) == 1
    assert "Нет пофайловых адресов" in findings[0]["text"]
    assert findings[0]["level"] == "стилистическая"


def test_step_cards_found_beyond_last_200_board_cards(server):
    """Карточки шага ищутся по префиксу ключа, а не окном последних задач борды (З1)."""
    run_gate(server["root"], "--run")            # карточка постановки
    (server["root"] / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# p\n", encoding="utf-8")
    run_gate(server["root"], "--run")            # карточка шага
    mine = {str(card["key"]) for card in stub_cards(server["db"])
            if str(card["key"]).startswith("planloop:1:S1:")}
    assert mine, "фикстура не создала карточек шага"
    add_stub_cards(server["db"], "planloop:1:S9:step:", 250, int(time.time()))
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    assert {card["key"] for card in data["cards"]} == mine


def test_step_hold_available_under_owner_stop_flag(server):
    """Стоп-флаг не гасит допуск: правило живёт в снимке (`hold.allowed`), а не в разметке."""
    stop = server["root"] / ".pipeline" / "state" / "plan_1_stop.json"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(json.dumps({"reason": "остановка владельца", "source": "owner",
                                "kind": "owner"}, ensure_ascii=False), encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    assert data["hold"]["held"] is False and data["hold"]["allowed"] is True
    assert data["hold"]["stop"] == "owner"
    assert data["hold"]["stop_reason"] == "остановка владельца"


def test_step_card_result_and_comments_clip_with_note(server):
    """Результат и комментарии карточки обрезаются с пометкой, а не тихо (З3)."""
    run_gate(server["root"], "--run")            # карточка постановки
    (server["root"] / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# p\n", encoding="utf-8")
    run_gate(server["root"], "--run")            # карточка шага
    step_card = next(card for card in stub_cards(server["db"])
                     if str(card["key"]).endswith(":step"))
    set_card_result(server["db"], step_card["id"], "я" * 2500)
    for number in range(25):
        add_comment(server["db"], step_card["id"], f"замечание {number}")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    shown = next(card for card in data["cards"] if card["id"] == step_card["id"])
    assert len(shown["result"]) == 2000 and shown["result_truncated"] is True
    assert shown["result_note"] == "показаны первые 2000 символов, всего 2500"
    assert len(shown["comments"]) == 20
    assert shown["comments_note"] == "показаны первые 20 комментариев, всего 25"


def test_file_route_rejects_scheme_and_names_directory(server):
    """Схема — не путь проекта, а каталог получает своё сообщение (стилистическая С2)."""
    status, data = get(server["url"] + "/api/file?path=" +
                       urllib.parse.quote("file:///C:/Windows/win.ini"))
    assert status == 400 and "вне корня" in data["error"]
    status, data = get(server["url"] + "/api/file?path=docs/plans")
    assert status == 400 and "каталог" in data["error"]


def test_step_screen_marks_ambiguous_verdict(server):
    (server["root"] / "docs" / "reviews" / "1-S1.md").write_text(
        "## ВЕРДИКТ: APPROVE\n\n## ВЕРДИКТ: REQUEST_CHANGES\n", encoding="utf-8")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    assert data["review"]["verdict"] == "AMBIGUOUS" and data["review"]["occurrences"] == 2


def test_step_screen_clips_long_text_with_note(server):
    write_prompt(server, "\n".join(f"строка {number}" for number in range(1, 501)))
    status, data = get(server["url"] + "/api/step?plan=1&id=S1&lines=5")
    assert status == 200
    prompt = data["prompt"]
    assert prompt["truncated"] is True
    assert prompt["shown"] == 5 and prompt["total"] == 500
    assert prompt["note"] == "показаны первые 5 строк, всего 500"
    assert prompt["text"].splitlines()[-1] == "строка 5"


def test_step_screen_unknown_plan_and_step(server):
    status, data = get(server["url"] + "/api/step?plan=1&id=S99")
    assert status == 404 and data["ok"] is False and "S99" in data["error"]
    status, data = get(server["url"] + "/api/step?plan=1")
    assert status == 400 and "plan" in data["error"]
    status, data = get(server["url"] + "/api/step?plan=9&id=S1")
    assert status == 404 and "9" in data["error"] and "не найден" in data["error"]


def test_step_screen_shows_git_show_of_step_commit(server):
    if not shutil.which("git"):
        pytest.skip("git недоступен")

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(server["root"]), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    assert git("init").returncode == 0
    assert git("add", "docs/plans/1-demo.md", "pipeline.json").returncode == 0
    commit = git("-c", "user.name=Тест", "-c", "user.email=test@example.com",
                 "commit", "-m", "feat: шаг 1-S1 (тест)")
    assert commit.returncode == 0, commit.stderr
    sha = git("rev-parse", "HEAD").stdout.strip()
    write_review(server, sha)
    status, data = get(server["url"] + "/api/step?plan=1&id=S1&lines=10")
    assert status == 200
    assert data["commit"]["available"] is True, data["commit"]["error"]
    assert data["commit"]["sha"] == sha
    assert "1-demo.md" in data["commit"]["stat"]
    assert data["commit"]["diff"]["text"], "дифф коммита пуст"


def test_step_screen_cards_results_and_hold(server):
    run_gate(server["root"], "--run")            # карточка постановки
    (server["root"] / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# p\n", encoding="utf-8")
    run_gate(server["root"], "--run")            # карточка шага
    complete_card(server["db"], ":step")
    step_card = next(card for card in stub_cards(server["db"])
                     if str(card["key"]).endswith(":step"))
    add_comment(server["db"], step_card["id"], "замечание ревьюера")
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    shown = {card["key"]: card for card in data["cards"]}
    assert shown[step_card["key"]]["status"] == "done"
    assert shown[step_card["key"]]["result"] == "ok"
    assert shown[step_card["key"]]["comments"][0]["body"] == "замечание ревьюера"
    assert shown["planloop:1:S1:prompt"]["result"] == ""
    assert data["hold"] == {"steps": [], "held": False, "allowed": True, "stop": "",
                            "stop_reason": ""}

    # Допуск идёт с номером плана: иначе записался бы плану по умолчанию.
    status, payload = post(server["url"], {"action": "hold", "step": "S1", "plan": "1"})
    assert status == 200 and payload["ok"]
    assert "--plan 1" in server["state"].tail()[-1]
    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert data["hold"] == {"steps": ["S1"], "held": True, "allowed": False, "stop": "",
                            "stop_reason": ""}


def test_file_route_reads_inside_root_with_clip(server):
    status, data = get(server["url"] + "/api/file?path=docs/plans/1-demo.md&lines=3")
    assert status == 200 and data["ok"] and data["exists"] is True
    assert data["path"] == "docs/plans/1-demo.md"
    assert data["shown"] == 3 and data["truncated"] is True
    assert data["note"] == f"показаны первые 3 строки, всего {data['total']}"
    assert data["text"].splitlines()[0].startswith("# 1.")


def test_file_route_rejects_paths_outside_root(server, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("чужое", encoding="utf-8")
    for path in ("../secret.txt", "docs/../../secret.txt", str(secret), "/etc/passwd",
                 "C:/Windows/win.ini", ".env", ".git/config"):
        status, data = get(server["url"] + "/api/file?path=" + urllib.parse.quote(path))
        assert status == 400, path
        assert data["ok"] is False
        assert "вне корня" in data["error"] or "служебный" in data["error"], data["error"]


def test_file_route_rejects_symlink_outside_root(server, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("чужое", encoding="utf-8")
    link = server["root"] / "docs" / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("симлинки на этой машине недоступны")
    status, data = get(server["url"] + "/api/file?path=docs/link.md")
    assert status == 400 and "вне корня" in data["error"]


def test_index_has_step_screen_markup():
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('id="tab-step"', 'data-tab="step"', "/api/step", 'id="step-contract"',
                   'id="step-prompt"', 'id="step-review"', 'id="step-commit"',
                   'id="step-verdict"', 'id="step-findings"', 'id="step-findings-note"',
                   'id="btn-hold"', 'id="btn-unhold"', "data-comments", "renderStep(data)",
                   # Экран шага перезагружается по паре «план + шаг», а доступность допуска
                   # приходит из снимка (находки Б2 и З4 ревью 3-S3).
                   "stepPairLoaded", "hold.allowed !== false"):
        assert anchor in html


def test_health_reports_version_and_ui_sha(server):
    """Панель должна знать, какую версию и какую страницу ей отдал сервер."""
    base = server["url"]
    with urllib.request.urlopen(base + "/api/health", timeout=5) as res:
        data = json.loads(res.read().decode("utf-8"))
    assert data["ok"] is True
    assert data["version"] and data["version"] != "0"
    assert data["ui_sha"] == ui_server.ui_sha()
    assert len(data["ui_sha"]) == 12


def test_busy_port_is_reported_not_silently_reused(tmp_path, hermes_stub):
    """Двойная привязка на Windows: старый UI отвечает, новый молчит — это ловим."""
    root = install_project(tmp_path / "busy-project", board="busyboard")
    httpd = ui_server.make_server(root, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        assert ui_server.busy_version(host, port) == ui_server.plugin_version()
        with pytest.raises(RuntimeError) as err:
            ui_server.serve(root, host=host, port=port, open_browser=False)
        assert "занят" in str(err.value)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_busy_version_none_on_free_port():
    """Свободный порт — не ошибка: проверка не должна мешать запуску."""
    assert ui_server.busy_version("127.0.0.1", 9) is None


def test_snapshot_carries_ui_version(server):
    """Версия и отпечаток страницы — в самом снимке: панель читает их из любого кадра."""
    code, data = get(server["url"] + "/api/snapshot")
    assert code == 200
    assert data["version"] == ui_server.plugin_version()
    assert data["ui_sha"] == ui_server.ui_sha()


def test_events_frame_carries_ui_version(server):
    """SSE-кадр собирается тем же путём: иначе версия «?» на живой панели."""
    url = server["url"] + "/api/events"
    with urllib.request.urlopen(url, timeout=10) as res:
        buf: list[str] = []
        while len(buf) < 200:
            line = res.readline().decode("utf-8", "replace")
            if not line:
                break
            buf.append(line)
            if line.strip() == "":
                break
    chunk = "".join(buf)
    frame = None
    for ln in chunk.splitlines():
        if ln.startswith("data: "):
            frame = json.loads(ln[6:])
            break
    assert frame is not None, chunk[:300]
    assert frame["version"] == ui_server.plugin_version()


# ───────────────────────── проверки в панели (план 3 S5) ─────────────────────────

BROKEN_PLAN = """# 3. Кривой план

## Шаги

### S1. Шаг без критерия готовности (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py -q` → 1 passed

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    raise AssertionError(f"в тексте нет строки с {needle!r}")


def test_checks_count_only_active_plan_findings(server, monkeypatch):
    """Находки закрытых планов уходят в архив: бейдж считает только текущий план."""
    from step_pipeline import ui_server as ui
    # Активный план задаём явно: в тестовом проекте номер выводится из снимка,
    # а проверяем мы именно разбивку, а не выбор плана.
    monkeypatch.setattr(ui, "active_plan_id", lambda *args, **kwargs: "3")
    root = server["root"]
    (root / "docs" / "plans" / "3-broken.md").write_text(BROKEN_PLAN, encoding="utf-8")
    (root / "docs" / "plans" / "5-old.md").write_text(BROKEN_PLAN, encoding="utf-8")
    status, data = get(server["url"] + "/api/checks?refresh=1")
    assert status == 200 and data["ok"] is True
    lint = data["lint"]
    assert lint["active_plan"] == "3"
    assert len(lint["problems"]) == 2
    assert lint["active"] == 1 and lint["archive"] == 1
    assert data["critical"] == len(data["problems"]) + 1


def test_checks_start_in_not_checked_state(server):
    """«Не проверяли» — отдельное состояние, а не пустой список находок."""
    status, data = get(server["url"] + "/api/checks")
    assert status == 200 and data["ok"] is True
    assert data["checked_at"] == "" and data["critical"] is None
    assert data["busy"] is False
    assert data["notes"] == [] and data["problems"] == []
    assert data["lint"] == {"files": 0, "problems": [], "active_plan": "", "active": 0,
                            "archive": 0}


def test_checks_refresh_reports_line_plan_and_step(server):
    """Находка линтера приходит с номером строки, планом и шагом — для перехода."""
    root = server["root"]
    (root / "docs" / "plans" / "3-broken.md").write_text(BROKEN_PLAN, encoding="utf-8")
    status, data = get(server["url"] + "/api/checks?refresh=1")
    assert status == 200 and data["ok"] is True
    assert data["checked_at"] and data["busy"] is False
    findings = data["lint"]["problems"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["file"] == "docs/plans/3-broken.md"
    assert finding["plan"] == "3" and finding["step"] == "S1"
    assert finding["line"] == _line_of(BROKEN_PLAN, "### S1. Шаг без критерия")
    assert "нет критерия «Готово =»" in finding["text"]
    # Критичные = проблемы доктора плюс находки только активного плана (архив не в счёт).
    assert data["critical"] == len(data["problems"]) + data["lint"]["active"] > 0
    # Доктор сюда свой линтер не подмешивает: находки живут в одном месте.
    assert not any("Готово" in problem for problem in data["problems"])

    # Ограничение одним планом: у чистого плана 1 находок нет.
    scoped = get(server["url"] + "/api/checks?refresh=1&plan=1")[1]
    assert scoped["lint"]["files"] == 1 and scoped["lint"]["problems"] == []


def test_checks_never_expose_bot_token(server, monkeypatch, tmp_path):
    """Отказ Telegram идёт в панель как текст, но токен в ответ не попадает."""
    token = "123456:AAzz-secret-token"
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(project, "profile_dir", lambda name: profiles / name)
    env = profiles / "demo" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("STEP_PIPELINE_TELEGRAM_BOT_TOKEN=" + token + "\n"
                   "STEP_PIPELINE_TELEGRAM_CHAT_ID=42\n", encoding="utf-8")

    def boom(_token):
        raise ui_server.telegram.TelegramError("сеть недоступна: таймаут")

    monkeypatch.setattr(ui_server.telegram, "get_me", boom)
    status, _headers, body = get_raw(server["url"] + "/api/checks?refresh=1")
    assert status == 200
    assert token.encode("utf-8") not in body
    data = json.loads(body.decode("utf-8"))
    assert any(problem.startswith("Telegram:") for problem in data["problems"])


def test_checks_perform_no_actions_and_write_no_files(server, monkeypatch):
    """Проверки — только чтение: ни одного действия и ни одного нового файла.

    Читающие запуски движка (снимок для строки «решение стража») допустимы — это
    `gate.py --plans/--status/next_step.py --json`. Запрещены действия.
    """
    root = server["root"]

    def tree() -> list[str]:
        # `__pycache__` — след самого python у движка, а не запись проекта.
        return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                      if "__pycache__" not in path.parts and path.suffix != ".pyc")

    calls = engine_counter(monkeypatch)
    before = tree()
    status, data = get(server["url"] + "/api/checks?refresh=1")
    after = tree()
    assert status == 200 and data["ok"] is True
    mutating = ["--run", "--stop", "--resume", "--hold", "--unhold", "--approve",
                "--resume"]
    assert not [call for call in calls
                if any(str(arg) in mutating for arg in call[2:])]
    assert before == after


def test_checks_refresh_does_not_start_second_run(server, monkeypatch):
    """Пока проверка идёт, повторный запрос возвращает busy и прежние данные."""
    started: list[str] = []

    def slow(state, plan=""):
        started.append(plan)
        payload = ui_server._blank_checks()
        payload["checked_at"] = "2026-11-04T10:00:00"
        return payload

    monkeypatch.setattr(ui_server, "collect_checks", slow)
    state = server["state"]
    assert state.checks_lock.acquire(blocking=False)
    try:
        status, data = get(server["url"] + "/api/checks?refresh=1")
    finally:
        state.checks_lock.release()
    assert status == 200 and data["busy"] is True
    assert started == [], "второй прогон поверх идущего не запускается"


def test_checks_failure_is_visible_instead_of_empty(server, monkeypatch):
    """Отказ проверки виден как проблема, а не как «проверено, чисто»."""
    def boom(state, plan=""):
        raise RuntimeError("диск отвалился")

    monkeypatch.setattr(ui_server, "collect_checks", boom)
    status, data = get(server["url"] + "/api/checks?refresh=1")
    assert status == 200 and data["ok"] is True
    assert data["problems"] == ["проверка не удалась: RuntimeError: диск отвалился"]
    assert data["critical"] is None and data["busy"] is False
    assert any("проверка не удалась" in line for line in server["state"].tail())


def test_snapshot_carries_checks_summary_for_badge(server):
    """Снимок несёт короткую форму §3.6: до проверки — None, после — четыре поля."""
    _, before = get(server["url"] + "/api/snapshot")
    assert before["overview"]["checks"] is None

    get(server["url"] + "/api/checks?refresh=1")
    _, after = get(server["url"] + "/api/snapshot")
    summary = after["overview"]["checks"]
    assert set(summary) == {"checked_at", "critical", "problems", "notes"}
    assert summary["checked_at"] and summary["critical"] > 0
    assert isinstance(summary["problems"], list) and summary["notes"]


def test_checks_work_in_preview_and_create_nothing(tmp_path: Path, hermes_stub: Path):
    """Предпросмотр: вкладка проверок работает и ничего не пишет в проект."""
    root = tmp_path / "bare"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "7-real.md").write_text(BROKEN_PLAN.replace("# 3.", "# 7."),
                                                       encoding="utf-8")
    httpd = ui_server.make_server(root, port=0, preview=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, data = get(url + "/api/checks?refresh=1")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200 and data["ok"] is True
    assert data["notes"], "доктор обязан отчитаться даже без установленного конвейера"
    assert data["critical"] and data["critical"] > 0
    assert not (root / ".pipeline").exists()
    assert not (root / "pipeline.json").exists()


def test_index_has_checks_tab_markup():
    """Вкладка «Проверки»: опоры §4.1/§4.2, бейдж в шапке, переход к шагу (§5.1)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('data-tab="checks"', 'id="tab-checks"', "/api/checks", "refresh=1",
                   'id="btn-checks-refresh"', 'id="checks-badge"', 'id="checks-empty"',
                   'id="checks-clean"', 'id="checks-when"', 'id="checks-doctor"',
                   'id="checks-lint"', 'id="checks-lint-note"', "Диагностика (doctor)",
                   "renderChecks(data)", "gotoSighting", "ov.checks", "не проверяли",
                   # автозапуск один раз на загрузку (§4.6) и оба поля контекста (§4.4)
                   "checksStarted", "loadChecks(true)", "window.__planId = String(plan)",
                   "currentStep = step"):
        assert anchor in html, anchor
    # §4.1: вкладки в порядке «Обзор, Шаг, Проверки, Установка».
    tabs = [html.index(f'data-tab="{name}"') for name in ("overview", "step", "checks",
                                                          "setup")]
    assert tabs == sorted(tabs), tabs
    # §4.1: панель «Проверок» — сразу после панели «Шаг», до «Установки».
    panels = [html.index(f'id="tab-{name}"') for name in ("step", "checks", "setup")]
    assert panels == sorted(panels), panels


def test_index_makes_only_step_findings_clickable():
    """§4.2: находка без шага — обычный текст; с шагом — title и переход (ревью 3-S5)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    start = html.index('$("checks-lint").innerHTML = findings.map')
    end = html.index("// Идущая чужая проверка", start)
    block = html[start:end]
    # Обработчик вешается только на строки с шагом: у находки без step переходить некуда.
    assert 'querySelectorAll("#checks-lint .finding.goto")' in block
    assert 'querySelectorAll("#checks-lint .finding")' not in block
    # Класс кликабельности и title — условно по непустому step.
    assert 'step ? "bad goto" : "wait"' in block
    assert 'title="открыть шаг ${esc(step)} в плане ${esc(plan || "?")}"' in block
    # Строка без шага не притворяется переходом: ни стрелки, ни подсказки.
    assert 'step ? "→ " + step : (plan ? "план " + plan : "")' in block
    assert "function gotoSighting(plan, step)" in html
    assert "if (!step)" in html
    # CSS: курсор и подсветка — только у кликабельных строк.
    assert ".checks-lint .finding.goto { cursor: pointer; }" in html
    assert ".checks-lint .finding { cursor: pointer; }" not in html

# ───────────────────────── реестр проектов (план 3 S6) ─────────────────────────

def write_registry(home: Path, entries: list[dict]) -> Path:
    """Реестр в home профиля: панель читает его, но никогда не пишет (D4)."""
    path = home / "projects.json"
    path.write_text(json.dumps({"projects": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def running(root: Path, **kwargs):
    """Сервер в потоке: нужен там, где фикстура `server` не подходит (свой реестр)."""
    import contextlib

    @contextlib.contextmanager
    def manager():
        httpd = ui_server.make_server(root, port=0, **kwargs)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}", httpd
        finally:
            httpd.shutdown()
            httpd.server_close()

    return manager()


def test_projects_route_lists_two_projects(tmp_path: Path, hermes_stub: Path,
                                           isolated_hermes_home: Path):
    """Два репозитория видны на одном экране: строка несёт все поля сводки (§4.2)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    second = install_project(tmp_path / "beta", board="uiboard")
    write_registry(isolated_hermes_home,
                   [{"id": "beta", "name": "Бета", "root": str(second)}])
    with running(first) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    rows = {row["id"]: row for row in data["projects"]}
    assert sorted(rows) == ["alpha", "beta"]
    assert data["default"] == "alpha"
    assert rows["alpha"]["source"] == "default"
    assert rows["beta"]["source"] == "registry"
    assert rows["beta"]["name"] == "Бета" and rows["beta"]["installed"] is True
    for row in rows.values():
        assert set(row) >= {"id", "name", "root", "installed", "plan", "next", "closed",
                            "total", "stop", "idle", "source"}
    assert rows["alpha"]["next"] == "S1" and rows["alpha"]["total"] == 2
    assert rows["beta"]["plan"] == "1" and rows["beta"]["stop"] is None


def test_registry_entry_without_pipeline_is_installed_false(tmp_path: Path, hermes_stub: Path,
                                                           isolated_hermes_home: Path):
    """Каталог без `.pipeline` — «не установлен», а снимок по нему всё равно 200 (D9)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    bare = tmp_path / "bare"
    bare.mkdir()
    write_registry(isolated_hermes_home,
                   [{"id": "bare", "name": "Голый", "root": str(bare)}])
    with running(first) as (url, _httpd):
        status, data = get(url + "/api/projects")
        snap_status, snapshot = get(url + "/api/snapshot?project=bare")
        action_status, action = post(url, {"action": "tick", "project": "bare"})
    rows = {row["id"]: row for row in data["projects"]}
    assert status == 200 and rows["bare"]["installed"] is False
    assert rows["bare"]["next"] == "—" and rows["bare"]["total"] == 0
    assert snap_status == 200 and snapshot["ok"] is True
    assert snapshot["installed"] is False and snapshot["plans"] == []
    assert snapshot["errors"], "неустановленный проект обязан объяснить, чего не хватает"
    assert action_status == 400 and "не установлен" in action["error"]
    assert action["project"] == {"id": "bare", "name": "Голый"}
    assert not (bare / ".pipeline").exists()


def test_registry_skips_duplicate_and_missing_roots(tmp_path: Path, hermes_stub: Path,
                                                    isolated_hermes_home: Path):
    """Дубли по каталогу, запись без root и несуществующий каталог в реестр не попадают."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    second = install_project(tmp_path / "beta", board="uiboard")
    write_registry(isolated_hermes_home, [
        {"id": "first-again", "root": str(first)},
        {"id": "no-root"},
        {"id": "gone", "root": str(tmp_path / "нет-такого-каталога")},
        {"id": "beta", "name": "Бета", "root": str(second)},
    ])
    with running(first) as (url, _httpd):
        _, data = get(url + "/api/projects")
    assert [row["id"] for row in data["projects"]] == ["alpha", "beta"]


def test_extra_project_joins_registry_from_command_line(tmp_path: Path, hermes_stub: Path):
    """Разовый путь из `--extra-project` — такая же строка реестра (D3)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    extra = install_project(tmp_path / "gamma", board="uiboard")
    with running(first, extra_projects=[str(extra)]) as (url, _httpd):
        _, data = get(url + "/api/projects")
    rows = {row["id"]: row for row in data["projects"]}
    assert sorted(rows) == ["alpha", "gamma"]
    assert rows["gamma"]["source"] == "command line"


def test_action_by_one_project_does_not_touch_another(tmp_path: Path, hermes_stub: Path,
                                                      isolated_hermes_home: Path):
    """Действие адресуется выбранному проекту и подписано его именем (§4.3)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    second = install_project(tmp_path / "beta", board="uiboard")
    write_registry(isolated_hermes_home,
                   [{"id": "beta", "name": "Бета", "root": str(second)}])
    with running(first) as (url, httpd):
        status, action = post(url, {"action": "stop", "reason": "стоп по альфе",
                                    "project": "alpha"})
        _, alpha_snapshot = get(url + "/api/snapshot")
        _, beta_snapshot = get(url + "/api/snapshot?project=beta")
        _, rows = get(url + "/api/projects")
    assert status == 200 and action["ok"] is True, action
    # Подпись ответа — имя проекта, к которому отнеслись: у A оно своё, у B — «Бета».
    assert action["project"] == {"id": "alpha", "name": "demo"}
    assert httpd.RequestHandlerClass.registry[0]["state"] is httpd.RequestHandlerClass.state
    assert (first / ".pipeline" / "state" / "plan_1_stop.json").is_file()
    # Сосед не тронут: ни стоп-флага, ни изменений в снимке B.
    assert not (second / ".pipeline" / "state" / "plan_1_stop.json").exists()
    assert alpha_snapshot["root"] == str(first.resolve())
    assert beta_snapshot["root"] == str(second.resolve())
    assert beta_snapshot["overview"]["step"] == "S1"
    assert beta_snapshot["overview"]["stop"] is None
    beta_row = {row["id"]: row for row in rows["projects"]}["beta"]
    assert beta_row["stop"] is None


def test_unknown_project_is_400_and_missing_project_is_default(tmp_path: Path, hermes_stub: Path,
                                                               isolated_hermes_home: Path):
    """Неизвестный id — 400 с перечнем; без параметра — проект сервера (D7)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    ghost = "чужой"
    with running(first) as (url, httpd):
        status, data = get(url + "/api/snapshot?project=" + urllib.parse.quote(ghost))
        action_status, action = post(url, {"action": "stop", "project": ghost})
        default_status, default = get(url + "/api/snapshot")
    assert status == 400 and "неизвестный проект" in data["error"]
    assert "alpha" in data["error"] and data["projects"] == ["alpha"]
    assert action_status == 400 and "неизвестный проект" in action["error"]
    assert default_status == 200
    assert default["root"] == str(httpd.RequestHandlerClass.state.project.resolve())


def test_preview_refuses_actions_for_every_project(tmp_path: Path, hermes_stub: Path,
                                                   isolated_hermes_home: Path):
    """Предпросмотр гасит действия всем проектам, а сводку отдаёт (D10)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    second = install_project(tmp_path / "beta", board="uiboard")
    write_registry(isolated_hermes_home,
                   [{"id": "beta", "name": "Бета", "root": str(second)}])
    with running(first, preview=True) as (url, _httpd):
        status, data = get(url + "/api/projects")
        own_status, own = post(url, {"action": "tick"})
        other_status, other = post(url, {"action": "tick", "project": "beta"})
    assert status == 200 and len(data["projects"]) == 2 and data["preview"] is True
    for code, body in ((own_status, own), (other_status, other)):
        assert code == 400 and "предпросмотр" in body["error"]
    assert not (second / ".pipeline" / "state" / "plan_1_stop.json").exists()


def test_helpers_read_registry_and_ignore_broken_file(tmp_path: Path, isolated_hermes_home: Path,
                                                      monkeypatch):
    """`read_projects`: битый файл — пусто, дубли id разводятся, root остаётся Path."""
    monkeypatch.delenv("PIPELINE_PROJECTS_FILE", raising=False)
    (isolated_hermes_home / "projects.json").write_text("{не json", encoding="utf-8")
    assert project.read_projects() == []
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    write_registry(isolated_hermes_home, [{"id": "same", "root": str(one)},
                                          {"id": "same", "root": str(two)}])
    rows = project.read_projects()
    assert [row["id"] for row in rows] == ["same", "same-2"]
    assert all(isinstance(row["root"], Path) for row in rows)
    assert rows[0]["name"] == "one", "без имени берётся имя каталога"


def test_configured_project_root_reads_manifest_key(tmp_path: Path, isolated_hermes_home: Path,
                                                    monkeypatch):
    """Ключ `project_root` из `config.yaml` home профиля читается без PyYAML."""
    monkeypatch.delenv("PIPELINE_PROJECT_ROOT", raising=False)
    root = tmp_path / "configured"
    root.mkdir()
    (isolated_hermes_home / "config.yaml").write_text(
        "agent:\n  max_turns: 10\nplugins:\n  entries:\n    step_pipeline:\n      settings:\n"
        f"        project_root: {root}\n        ui_theme: dark\n", encoding="utf-8")
    assert project.configured_project_root() == root.resolve()

    (isolated_hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - step_pipeline\n", encoding="utf-8")
    assert project.configured_project_root() is None


def test_configured_project_root_env_wins(tmp_path: Path, isolated_hermes_home: Path,
                                          monkeypatch):
    """`PIPELINE_PROJECT_ROOT` важнее файла; несуществующий каталог — `None`."""
    from_yaml = tmp_path / "from-yaml"
    from_env = tmp_path / "from-env"
    from_yaml.mkdir()
    from_env.mkdir()
    (isolated_hermes_home / "config.yaml").write_text(
        "plugins:\n  entries:\n    step_pipeline:\n      settings:\n"
        f"        project_root: {from_yaml}\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_PROJECT_ROOT", str(from_env))
    assert project.configured_project_root() == from_env.resolve()
    monkeypatch.setenv("PIPELINE_PROJECT_ROOT", str(tmp_path / "нет-такого"))
    assert project.configured_project_root() is None


def test_index_has_project_registry_markup():
    """Шапка, сводка и состояние страницы: опоры задачи 7 (§4.2)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('id="project-switch"', 'id="projects-summary"', 'id="projects-table"',
                   "/api/projects", "function withProject", "function switchProject",
                   "currentProject", "renderProjects", "loadProjects()",
                   "data.installed === false", "не установлен"):
        assert anchor in html, anchor
    # Переключение сбрасывает контекст прежнего проекта целиком (§4.4).
    start = html.index("function switchProject(id)")
    end = html.index("function renderProjects", start)
    block = html[start:end]
    for anchor in ("etag = null", "currentPlan = null", "currentStep = null",
                   'stepPairLoaded = ""', "checksStarted = false",
                   "refresh();", "startEvents();"):
        assert anchor in block, anchor
    # GET-адреса идут через withProject: без него запрос ушёл бы чужому проекту.
    for call in ('withProject("/api/snapshot"', 'withProject("/api/events"',
                 'withProject("/api/step?', 'withProject("/api/checks"',
                 'withProject("/api/setup"'):
        assert call in html, call


# ───────────────────── правка конфигурации (план 3 S7) ─────────────────────

def config_file(server) -> Path:
    """Конфигурация tmp-проекта: тот самый файл, что читает и пишет панель."""
    return server["root"] / "pipeline.json"


def test_config_preview_returns_diff_and_writes_nothing(server):
    """Предпросмотр отдаёт дифф со старым и новым значением и файл не трогает (D4)."""
    path = config_file(server)
    before = path.read_bytes()
    status, data = post(server["url"], {"action": "preview_config",
                                       "config": {"runtimes": {"step": "45m"}}})
    assert status == 200 and data["ok"] and data["changed"] == ["runtimes"]
    assert '"step": "3h"' in data["diff"], data["diff"]
    assert '"step": "45m"' in data["diff"], data["diff"]
    assert data["content"].endswith("\n") and data["notes"] == []
    assert path.read_bytes() == before, "предпросмотр файл не пишет"


def test_config_preview_matches_saved_bytes(server):
    """Дифф не врёт: `content` предпросмотра равен записанному тексту (D4)."""
    patch = {"board": "newboard", "runtimes": {"step": "45m"}}
    preview_status, preview = post(server["url"], {"action": "preview_config", "config": patch})
    save_status, saved = post(server["url"], {"action": "save_config", "config": patch})
    assert preview_status == 200 and save_status == 200
    assert preview["ok"] and saved["ok"]
    assert preview["content"] == config_file(server).read_text(encoding="utf-8")
    assert json.loads(preview["content"])["board"] == "newboard"


def test_save_config_rejects_bad_runtime(server):
    """Некорректный таймаут отклоняется с объяснением по ключу, файл не меняется (D2)."""
    path = config_file(server)
    before = path.read_bytes()
    for bad in ("5 часов", "45", "3ч", "0m", "-5m", ""):
        status, data = post(server["url"], {"action": "save_config",
                                            "config": {"runtimes": {"step": bad}}})
        assert status == 400 and not data["ok"], bad
        assert data["error"].startswith("runtimes.step"), (bad, data["error"])
    assert path.read_bytes() == before
    # Годное значение той же формы проходит.
    ok, saved = post(server["url"], {"action": "save_config",
                                     "config": {"runtimes": {"step": "45m"}}})
    assert ok == 200 and saved["ok"] and saved["changed"] == ["runtimes"]


def test_save_config_rejects_path_outside_project(server):
    """Некорректный путь отклоняется с объяснением по ключу, корректный проходит (D3)."""
    path = config_file(server)
    before = path.read_bytes()
    for bad in ("../outside", "/etc", "C:/Windows", "docs/../../.."):
        status, data = post(server["url"], {"action": "save_config",
                                            "config": {"paths": {"plans": bad}}})
        assert status == 400 and not data["ok"], bad
        assert data["error"].startswith("paths.plans"), (bad, data["error"])
        assert "корня" in data["error"] or ".." in bad, (bad, data["error"])
    assert path.read_bytes() == before
    ok, saved = post(server["url"], {"action": "save_config",
                                     "config": {"paths": {"plans": "docs/plans"}}})
    assert ok == 200 and saved["ok"] and saved["changed"] == ["paths"]
    assert json.loads(path.read_text(encoding="utf-8"))["paths"]["plans"] == "docs/plans"


def test_save_config_accepts_hidden_state_path(server):
    """`.pipeline/state` — законное умолчание движка, а не выход за корень (D3, факт 12)."""
    status, data = post(server["url"], {"action": "save_config",
                                        "config": {"paths": {"state": ".pipeline/state"}}})
    assert status == 200 and data["ok"]
    cfg = json.loads(config_file(server).read_text(encoding="utf-8"))
    assert cfg["paths"]["state"] == ".pipeline/state"
    assert cfg["paths"]["plans"] == "docs/plans", "соседние пути не тронуты"


def test_save_config_keeps_unknown_keys(server):
    """Неизвестные панели ключи файла и лишние подключи не теряются (C6, D1)."""
    path = config_file(server)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["custom_x"] = {"a": 1}
    cfg["profiles"]["driver"] = "так-так"
    cfg["runtimes"]["extra"] = "9m"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    status, data = post(server["url"], {"action": "save_config",
                                        "config": {"board": "newboard",
                                                   "runtimes": {"step": "45m"}}})
    assert status == 200 and data["ok"]
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["custom_x"] == {"a": 1}
    assert after["profiles"]["driver"] == "так-так"
    assert after["runtimes"]["extra"] == "9m"
    assert after["runtimes"]["step"] == "45m" and after["board"] == "newboard"


def test_save_config_refuses_broken_json(server):
    """Битый `pipeline.json` — отказ с объяснением, файл байт в байт не тронут (D7)."""
    path = config_file(server)
    path.write_text("{битый", encoding="utf-8")
    before = path.read_bytes()
    status, data = post(server["url"], {"action": "save_config",
                                        "config": {"board": "newboard"}})
    assert status == 400 and not data["ok"]
    assert "не разбирается" in data["error"], data["error"]
    assert path.read_bytes() == before


def test_revert_config_restores_bytes(server):
    """Откат возвращает прежний файл побайтово: в исходнике нет перевода строки (D5)."""
    path = config_file(server)
    original = path.read_bytes()
    save_status, saved = post(server["url"], {"action": "save_config",
                                              "config": {"board": "newboard"}})
    assert save_status == 200 and saved["ok"] and path.read_bytes() != original
    status, data = post(server["url"], {"action": "revert_config"})
    assert status == 200 and data["ok"] and data["bytes"] == len(original)
    assert data["sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original, "откат побайтовый"


def test_revert_config_without_backup(server):
    """Без сохранений копии нет: отказ понятный, а не 500 (§6.4)."""
    status, data = post(server["url"], {"action": "revert_config"})
    assert status == 400 and not data["ok"]
    assert "копии нет" in data["error"], data["error"]


def test_save_config_noop_keeps_file(server):
    """Повтор с теми же значениями: файл и копию не трогаем (D6)."""
    path = config_file(server)
    # Файл приводим к канону сериализации: иначе правкой стал бы один перевод строки.
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")),
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    patch = {"board": "uiboard", "runtimes": {"step": "3h"}}
    preview_status, preview = post(server["url"], {"action": "preview_config", "config": patch})
    status, data = post(server["url"], {"action": "save_config", "config": patch})
    assert preview_status == 200 and preview["diff"] == "", preview["diff"]
    assert status == 200 and data["ok"] and data["noop"] is True
    assert data["changed"] == [] and data["note"] == "изменений нет"
    assert data["backup"]["exists"] is False, "копия при пустой правке не создаётся"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp


def test_config_endpoint_serves_config_and_backup(server):
    """`GET /api/config`: конфигурация формы и состояние резервной копии (§6.5)."""
    path = config_file(server)
    size_before = path.stat().st_size
    status, data = get(server["url"] + "/api/config")
    assert status == 200 and data["ok"]
    assert data["config"]["board"] == "uiboard" and data["errors"] == []
    assert data["backup"] == {"exists": False, "path": ".pipeline/state/pipeline.json.bak",
                              "bytes": 0, "saved_at": ""}
    save_status, saved = post(server["url"], {"action": "save_config",
                                              "config": {"board": "newboard"}})
    assert save_status == 200 and saved["ok"]
    status, after = get(server["url"] + "/api/config")
    assert after["backup"]["exists"] is True and after["backup"]["bytes"] == size_before
    assert after["backup"]["path"] == ".pipeline/state/pipeline.json.bak"
    assert after["config"]["board"] == "newboard"
    assert after["project"]["name"], "ответ подписан проектом"


def test_config_endpoint_addresses_project(tmp_path: Path, hermes_stub: Path,
                                           isolated_hermes_home: Path):
    """Конфигурация адресуется проектом: свой файл, и 400 на неизвестный id (C8, D9)."""
    first = install_project(tmp_path / "alpha", board="alpha-board")
    second = install_project(tmp_path / "beta", board="beta-board")
    write_registry(isolated_hermes_home,
                   [{"id": "beta", "name": "Бета", "root": str(second)}])
    with running(first) as (url, _httpd):
        own_status, own = get(url + "/api/config")
        other_status, other = get(url + "/api/config?project=beta")
        bad_status, bad = get(url + "/api/config?project=nope")
        act_status, body = post(url, {"action": "save_config", "project": "nope",
                                      "config": {"board": "новая"}})
    assert own_status == 200 and own["config"]["board"] == "alpha-board"
    assert own["project"]["id"] == "alpha"
    assert other_status == 200 and other["config"]["board"] == "beta-board"
    assert other["project"]["id"] == "beta"
    assert bad_status == 400 and "неизвестный проект" in bad["error"]
    assert bad["projects"] == ["alpha", "beta"]
    # Действие по неизвестному id отказывает, а не уходит в файл серверного проекта.
    assert act_status == 400 and "неизвестный проект" in body["error"]
    assert json.loads((first / "pipeline.json").read_text(encoding="utf-8"))["board"] == \
        "alpha-board"


def test_index_has_config_diff_and_rollback_markup():
    """Опоры задачи в диалоге: дифф, проверка, откат, каталоги и проект (§6.6)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('id="config-diff"', 'id="config-check"', 'id="config-revert"',
                   'id="config-error"', 'id="config-hint"', 'id="form-config"',
                   'name="path_plans"', 'name="path_reviews"', 'name="path_prompts"',
                   'name="path_state"', 'id="save-config" disabled'):
        assert anchor in html, anchor
    for call in ('/api/config', 'withProject("/api/config")', "preview_config",
                 "revert_config", 'addEventListener("input"', "clearConfigDiff()"):
        assert call in html, call


# ─────────────── подключение проектов (план 3 S8) ───────────────

def post_to(url: str, route: str, payload: dict | None = None,
            headers: dict | None = None) -> tuple[int, dict]:
    """POST произвольного маршрута панели: JSON-тело и метка панели (D2/D3)."""
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{url}{route}", data=data,
        headers={"Content-Type": "application/json", "X-Pipeline-Panel": "1",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def delete(url: str, route: str, headers: dict | None = None) -> tuple[int, dict]:
    """DELETE маршрута панели: тела нет, метка панели есть (D3)."""
    request = urllib.request.Request(
        f"{url}{route}", method="DELETE",
        headers={"Content-Type": "application/json", "X-Pipeline-Panel": "1",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


CAPACITY_COLUMNS = (("worker_pid", "INTEGER"), ("last_heartbeat_at", "INTEGER"),
                    ("max_runtime_seconds", "INTEGER"), ("started_at", "INTEGER"))


def seed_tasks(db: Path, rows: list[dict]) -> None:
    """Карточки в борду теста: недостающие колонки capacity досыпаются (D10)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        for name, kind in CAPACITY_COLUMNS:
            if name not in present:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        for item in rows:
            fields = dict(item)
            fields.setdefault("title", fields["id"])
            fields.setdefault("status", "running")
            fields.setdefault("assignee", "driver")
            fields.setdefault("created_at", int(time.time()))
            columns = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            con.execute(f"INSERT INTO tasks ({columns}) VALUES ({marks})",
                        tuple(fields.values()))
        con.commit()
    finally:
        con.close()


def default_row(data: dict) -> dict:
    """Строка сводки проекта сервера (`default`) — по ней проверяется capacity."""
    return next(row for row in data["projects"] if row["id"] == data["default"])


def test_projects_add_and_persist(tmp_path: Path, hermes_stub: Path,
                                  isolated_hermes_home: Path):
    """Проект из панели виден сразу и остаётся после перезапуска сервера (C1)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    gamma = install_project(tmp_path / "gamma", board="uiboard")
    with running(first) as (url, _httpd):
        status, data = post_to(url, "/api/projects", {"path": str(gamma)})
        assert status == 200 and data["ok"] is True, data
        assert Path(data["project"]["root"]) == gamma.resolve()
        # Без паузы: кэш сводки обязан сброситься самим действием (D4).
        _, listed = get(url + "/api/projects")
        roots = [Path(row["root"]) for row in listed["projects"]]
        assert gamma.resolve() in roots
    # Второй сервер читает уже файл реестра, а не память первого.
    with running(first) as (url, _httpd):
        _, again = get(url + "/api/projects")
    assert gamma.resolve() in [Path(row["root"]) for row in again["projects"]]
    saved = json.loads((isolated_hermes_home / "projects.json").read_text(encoding="utf-8"))
    entry = next(item for item in saved["projects"] if Path(item["root"]) == gamma.resolve())
    assert set(entry) == {"id", "name", "root"}
    assert entry["id"] and entry["name"]


def test_projects_add_rejects_missing_pipeline(tmp_path: Path, hermes_stub: Path,
                                               isolated_hermes_home: Path):
    """Каталог без конвейера и проект без pipeline.json — 400, файл реестра не меняется (C3)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    registry = write_registry(isolated_hermes_home,
                              [{"id": "beta", "name": "Бета", "root": str(beta)}])
    before = registry.read_bytes()
    bare = tmp_path / "bare"
    bare.mkdir()
    half = tmp_path / "half"
    (half / ".pipeline").mkdir(parents=True)
    (half / ".pipeline" / "gate.py").write_text("# заглушка\n", encoding="utf-8")
    with running(first) as (url, _httpd):
        bare_status, bare_body = post_to(url, "/api/projects", {"path": str(bare)})
        half_status, half_body = post_to(url, "/api/projects", {"path": str(half)})
    assert bare_status == 400 and "конвейер" in bare_body["error"]
    assert half_status == 400 and "pipeline.json" in half_body["error"]
    assert registry.read_bytes() == before


def test_projects_add_duplicate_path(tmp_path: Path, hermes_stub: Path,
                                     isolated_hermes_home: Path):
    """Повторный путь — 409, и в файле по-прежнему одна запись (C3)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    gamma = install_project(tmp_path / "gamma", board="uiboard")
    with running(first) as (url, _httpd):
        first_status, _ = post_to(url, "/api/projects", {"path": str(gamma)})
        status, data = post_to(url, "/api/projects", {"path": str(gamma)})
    assert first_status == 200
    assert status == 409 and data["ok"] is False and "уже в реестре" in data["error"]
    saved = json.loads((isolated_hermes_home / "projects.json").read_text(encoding="utf-8"))
    assert len(saved["projects"]) == 1
    # Путь проекта по умолчанию тоже занят, хотя в файле его нет.
    with running(first) as (url, _httpd):
        own_status, own = post_to(url, "/api/projects", {"path": str(first)})
    assert own_status == 409 and "уже в реестре" in own["error"]


def test_projects_add_refused_in_preview(tmp_path: Path, hermes_stub: Path,
                                         isolated_hermes_home: Path):
    """Предпросмотр только читает: POST проектов — 403, файл реестра не создаётся (C3)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    gamma = install_project(tmp_path / "gamma", board="uiboard")
    registry = isolated_hermes_home / "projects.json"
    with running(first, preview=True) as (url, _httpd):
        status, data = post_to(url, "/api/projects", {"path": str(gamma)})
        del_status, del_data = delete(url, "/api/projects?id=gamma")
    assert status == 403 and "предпросмотр" in data["error"]
    assert del_status == 403 and "предпросмотр" in del_data["error"]
    assert not registry.exists()


def test_projects_delete_keeps_directory(tmp_path: Path, hermes_stub: Path,
                                         isolated_hermes_home: Path):
    """Отключение убирает строку реестра, каталог и его файлы остаются целы (C2)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    gamma = install_project(tmp_path / "gamma", board="uiboard")
    config = gamma / "pipeline.json"
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    with running(first) as (url, _httpd):
        added_status, added = post_to(url, "/api/projects",
                                      {"path": str(gamma), "name": "gamma"})
        assert added_status == 200, added
        assert added["project"]["id"] == "gamma"
        status, data = delete(url, "/api/projects?id=gamma")
        _, listed = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True and data["removed"] == "gamma"
    assert gamma.resolve() not in [Path(row["root"]) for row in listed["projects"]]
    assert gamma.is_dir()
    assert hashlib.sha256(config.read_bytes()).hexdigest() == before
    saved = json.loads((isolated_hermes_home / "projects.json").read_text(encoding="utf-8"))
    assert saved["projects"] == []


def test_projects_delete_unknown_id_is_error(tmp_path: Path, hermes_stub: Path,
                                             isolated_hermes_home: Path):
    """Неизвестный id, проект сервера и пустой параметр — 400 с объяснением (C3)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    with running(first) as (url, httpd):
        unknown_status, unknown = delete(url, "/api/projects?id=nope")
        own_status, own = delete(url, "/api/projects?id=alpha")
        empty_status, empty = delete(url, "/api/projects")
        default_id = httpd.RequestHandlerClass.state.id
    assert unknown_status == 400 and "нет записи" in unknown["error"]
    assert own_status == 400 and "нет записи" in own["error"]
    assert empty_status == 400 and "id" in empty["error"]
    assert default_id == "alpha"


def test_capacity_counts_running_and_ready(tmp_path: Path, hermes_stub: Path,
                                           isolated_hermes_home: Path):
    """4 из 6, очередь с причиной, pid и «прошло/лимит» от попытки, а не от создания (C4-C5)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    (isolated_hermes_home / "config.yaml").write_text(
        "kanban:\n  max_in_progress: 6\n  max_in_progress_per_profile: 1\n"
        "  stale_after: 10m\n", encoding="utf-8")
    now = int(time.time())
    staff = ("driver", "prompter", "reviewer", "maintainer")
    seed_tasks(hermes_stub, [
        {"id": f"run{index}", "assignee": who, "worker_pid": 1244 + index,
         "last_heartbeat_at": now - 120, "started_at": now - 600,
         "max_runtime_seconds": 4080, "created_at": now - 999999}
        for index, who in enumerate(staff)
    ] + [
        {"id": "wait-driver", "status": "ready", "assignee": "driver"},
        {"id": "wait-prompter", "status": "ready", "assignee": "prompter"},
    ])
    with running(first) as (url, _httpd):
        status, data = get(url + "/api/projects")
        assert status == 200
        row = default_row(data)
        capacity = row["capacity"]
        assert capacity["limit"] == 6 and capacity["limit_default"] is False
        assert capacity["per_profile"] == 1 and capacity["per_profile_default"] is False
        assert capacity["occupied"] == 4
        assert f"{capacity['occupied']}/{capacity['limit']}" == "4/6"
        assert len(capacity["running"]) == 4
        for index, item in enumerate(capacity["running"]):
            assert item["pid"] == 1244 + index
            assert item["assignee"] == staff[index]
            assert 115 <= item["heartbeat_age"] <= 125, item
            # created_at отстоит на 999999 с: попав в расчёт, он сломал бы границу.
            assert 600 <= item["elapsed"] <= 100000, item
            assert item["limit"] == 4080
            assert item["heartbeat"] and item["elapsed_text"]
        assert len(capacity["queue"]) == 2
        assert {item["reason"] for item in capacity["queue"]} == {"профиль занят"}
        # Глобально 4 из 6 — свободно: причина считается от самих чисел.
        html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
        assert "${p.capacity.occupied}/${p.capacity.limit}" in html
        # Досеиваем два чужих профиля: занято 6 из 6 — причина меняется на общий лимит.
        time.sleep(snapshot.CACHE_TTL + 0.3)     # сводка кэшируется на TTL
        seed_tasks(hermes_stub, [
            {"id": "run-extra-1", "assignee": "writer", "worker_pid": 1300,
             "last_heartbeat_at": now - 60, "started_at": now - 60,
             "max_runtime_seconds": 4080},
            {"id": "run-extra-2", "assignee": "qa", "worker_pid": 1301,
             "last_heartbeat_at": now - 60, "started_at": now - 60,
             "max_runtime_seconds": 4080},
        ])
        _, crowded = get(url + "/api/projects")
    full = default_row(crowded)["capacity"]
    assert full["occupied"] == 6
    assert len(full["queue"]) == 2
    assert {item["reason"] for item in full["queue"]} == {"общий лимит"}


def test_capacity_falls_back_to_defaults(tmp_path: Path, hermes_stub: Path,
                                         isolated_hermes_home: Path):
    """Ключей конфигурации нет — 3 и 3 с пометкой «по умолчанию» (C4)."""
    first = install_project(tmp_path / "alpha", board="uiboard")
    assert not (isolated_hermes_home / "config.yaml").exists()
    now = int(time.time())
    seed_tasks(hermes_stub, [{"id": "run1", "assignee": "driver", "worker_pid": 2001,
                              "last_heartbeat_at": now - 30, "started_at": now - 300,
                              "max_runtime_seconds": 1800}])
    with running(first) as (url, _httpd):
        _, data = get(url + "/api/projects")
    capacity = default_row(data)["capacity"]
    assert capacity["limit"] == 3 and capacity["limit_default"] is True
    assert capacity["per_profile"] == 3 and capacity["per_profile_default"] is True
    assert capacity["occupied"] == 1
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "по умолчанию" in html


def test_index_hotkeys_markup():
    """Клавиши страницы: один обработчик, поиск, подсказка, Esc и «/» (C6)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('document.addEventListener("keydown"', 'id="hotkeys-help"',
                   'id="steps-filter"', '"Escape"', '"/"', '$("btn-tick").click()',
                   '$("btn-stop").click()', '$("btn-resume").click()'):
        assert anchor in html, anchor


def test_index_theme_and_density_markup():
    """Тема и плотность: переменные светлой палитры, dataset и localStorage (C6)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('dataset.theme', '[data-theme="light"]',
                   'localStorage.getItem("pipeline.theme")',
                   'localStorage.getItem("pipeline.density")',
                   'id="btn-theme"', 'id="btn-density"', "compact"):
        assert anchor in html, anchor


def test_index_focus_and_aria_markup():
    """Видимый фокус и aria-метки на кнопках и вкладках (C6)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in (":focus-visible", 'role="tab"', "aria-selected"):
        assert anchor in html, anchor
    assert html.count("aria-label=") >= 5


def test_index_responsive_380_markup():
    """На 380 px: одна колонка у сеток и свои скролл-контейнеры у таблиц (C6)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "@media (max-width: 380px)" in html
    block = html[html.index("@media (max-width: 380px)"):]
    assert "grid-template-columns: 1fr" in block
    assert "overflow-x" in block


def _luminance(color: str) -> float:
    """Относительная яркость WCAG для `#rrggbb`."""
    def channel(value: int) -> float:
        part = value / 255
        return part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast(first: str, second: str) -> float:
    """Отношение контраста двух цветов по формуле WCAG."""
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _palette(html: str, selector: str) -> dict[str, str]:
    """Переменные палитры из блока селектора: `--имя: #hex`."""
    start = html.index(selector + " {")
    end = html.index("}", start)
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", html[start:end]))


def test_index_contrast_from_styles():
    """Тексты обеих тем проходят контраст 4.5:1 по вычисленным цветам стилей (C7)."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    pairs = [("text", "bg"), ("text", "panel"), ("text", "panel-2"),
             ("muted", "bg"), ("muted", "panel"), ("muted", "panel-2"),
             ("accent", "panel-2")]
    for selector in (":root", '[data-theme="light"]'):
        palette = _palette(html, selector)
        for foreground, background in pairs:
            front, back = f"--{foreground}", f"--{background}"
            assert front in palette and back in palette, (selector, front, back)
            ratio = _contrast(palette[front], palette[back])
            assert ratio >= 4.5, f"{selector} {front}/{back}: {ratio:.2f}"


# ─────────────────── лента действий, время шагов и этапы ролей (план 3 S9) ───────────────────
# Всё сидируется в tmp-проекте и tmp-борде фикстур: настоящая борда, профили и сеть не нужны.
# Карточки шагов вставляются напрямую: страж ведёт только следующий шаг плана.

RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, started_at INTEGER,
    ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT,
    last_heartbeat_at INTEGER, max_runtime_seconds INTEGER
);
"""


def write_history(server, rows) -> Path:
    """История тиков в tmp-проекте: словари пишутся JSONL, строки — как есть (битые)."""
    path = server["root"] / ".pipeline" / "state" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    snapshot.clear_cache()      # снимок кэшируется на 2.5 с: иначе тест увидит прежний
    return path


def ensure_card_columns(db: Path) -> None:
    """Колонки времени в тестовой борде: снимок читает их по факту наличия (D6)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        for name, kind in (("started_at", "INTEGER"), ("last_heartbeat_at", "INTEGER"),
                           ("current_run_id", "TEXT"), ("max_runtime_seconds", "INTEGER")):
            if name not in present:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        con.commit()
    finally:
        con.close()


def add_step_card(db: Path, key: str, assignee: str, created: int, status: str = "ready",
                  card_id: str | None = None) -> str:
    """Карточка шага в тестовой борде. Заголовок намеренно общий: этап берётся из ключа."""
    ensure_card_columns(db)
    card_id = card_id or "t_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    con = sqlite3.connect(db)
    try:
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
                    " created_at, result, tenant) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (card_id, "карточка шага", status, assignee, key, int(created), "", "plan"))
        con.commit()
    finally:
        con.close()
    return card_id


def set_card_times(db: Path, suffix: str, created: int | None = None,
                   started: int | None = None, completed: int | None = None) -> None:
    """Метки времени карточки по хвосту ключа идемпотентности."""
    sets, args = [], []
    for column, value in (("created_at", created), ("started_at", started),
                          ("completed_at", completed)):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(int(value))
    if not sets:
        return
    con = sqlite3.connect(db)
    try:
        con.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE idempotency_key LIKE ?",
                    (*args, f"%{suffix}"))
        con.commit()
    finally:
        con.close()


def set_heartbeat(db: Path, suffix: str, ts: int) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE idempotency_key LIKE ?",
                    (int(ts), f"%{suffix}"))
        con.commit()
    finally:
        con.close()


def add_runs(db: Path, card_id: str, runs: list[dict]) -> None:
    """Попытки карточки в `task_runs`: в тестовой схеме этой таблицы нет."""
    con = sqlite3.connect(db)
    try:
        con.executescript(RUNS_SCHEMA)
        for index, run in enumerate(runs, start=1):
            con.execute("INSERT INTO task_runs (id, task_id, profile, status, started_at,"
                        " ended_at, outcome, summary, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run.get("id") or f"r_{card_id}_{index}", card_id,
                         run.get("profile") or "demo", run.get("status") or "done",
                         int(run["started_at"]), run.get("ended_at"), run.get("outcome"),
                         run.get("summary") or "", run.get("error") or ""))
        con.commit()
    finally:
        con.close()


def index_html() -> str:
    return (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")


def test_events_filter_by_plan_step_and_kind(server):
    """Лента: окно строк, счётчики, поля для фильтров и разметка фильтров (C1)."""
    now = int(time.time())
    write_history(server, [
        {"ts": now - 60, "at": "05.11.2026 12:47:00", "plan": "1", "step": "S1",
         "action": "create_step", "duration_s": 1.2, "detail": "карточка шага: t_40bbe197",
         "commit": ""},
        {"ts": now - 50, "plan": "1", "step": "S2", "action": "stop",
         "detail": "план 1: инцидент", "commit": ""},
        {"ts": now - 40, "plan": "2", "step": "S1", "action": "create_prompt",
         "detail": "постановка S1 заказана", "commit": ""},
        {"ts": now - 30, "plan": "2", "step": "S2", "action": "create_step",
         "detail": "шаг 2-S2", "commit": "abc1234"},
        {"ts": now - 20, "plan": "1", "step": "S3", "action": "create_step",
         "detail": "шаг 1-S3", "commit": ""},
        {"ts": now - 10, "plan": "2", "step": "S3", "action": "create_step",
         "detail": "шаг 2-S3", "commit": ""},
        "{битая строка",
    ])
    status, data = get(server["url"] + "/api/snapshot")
    assert status == 200
    events = data["events"]
    assert events["total"] == 6 and events["bad"] == 1
    assert events["limit"] == 200
    assert len(events["rows"]) == 6
    # Свежие сверху; поля, по которым фильтрует страница, заполнены у каждой строки.
    assert events["rows"][0]["ts"] == now - 10
    assert {row["plan"] for row in events["rows"]} == {"1", "2"}
    assert "stop" in {row["kind"] for row in events["rows"]}
    for row in events["rows"]:
        assert row["plan"] and row["step"] and row["kind"] and row["label"] and row["at"]
    assert events["rows"][0]["commit"] == ""

    html = index_html()
    for anchor in ("function filterEvents", 'id="events-plan"', 'id="events-step"',
                   'id="events-kind"', 'id="events-search"', 'id="events-count"',
                   'id="events-empty"', "показано", "renderEvents(data)"):
        assert anchor in html, anchor


def test_events_search_matches_reason(server):
    """Причина стопа ищется по `detail`, а строка подсвечена флагом `bad` (C2)."""
    now = int(time.time())
    write_history(server, [{"ts": now, "plan": "1", "step": "S1", "action": "stop",
                            "detail": "план 1: контракт шага не найден — инцидент",
                            "commit": ""}])
    status, data = get(server["url"] + "/api/snapshot")
    assert status == 200
    row = data["events"]["rows"][0]
    assert row["flag"] == "bad"
    assert "контракт шага не найден" in row["detail"]
    html = index_html()
    assert 'id="events-search"' in html
    # Поиск идёт по тексту действия, а не по заголовку карточки.
    assert "[row.detail, row.label, row.step]" in html


def test_events_empty_state_text(server):
    """Пустая история объясняется текстом, а не пустым экраном (C1)."""
    status, data = get(server["url"] + "/api/snapshot")
    assert status == 200
    events = data["events"]
    assert events["rows"] == [] and events["total"] == 0 and events["bad"] == 0
    assert events["note"] and "истории" in events["note"]
    html = index_html()
    assert 'id="events-empty"' in html
    assert "истории" in html


def test_step_times_closed_and_open(server):
    """Закрытый шаг: начало, окончание и длительность; открытый — от первой карточки (C3)."""
    now = int(time.time())
    t0 = now - 7200
    t1 = t0 + 3600
    add_step_card(server["db"], "planloop:1:S1:prompt", "prompter", t0, status="done")
    add_step_card(server["db"], "planloop:1:S1:step", "demo", t0 + 60, status="done")
    set_card_times(server["db"], "planloop:1:S1:step", completed=t1)
    add_step_card(server["db"], "planloop:1:S2:prompt", "prompter", now - 600,
                  status="running")

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    times = data["times"]
    assert times["live"] is False
    assert times["start"] == snapshot._fmt_ts(t0)
    assert times["end"] == snapshot._fmt_ts(t1)
    assert times["seconds"] == 3600
    assert times["duration"] and times["note"] == ""

    status, data = get(server["url"] + "/api/step?plan=1&id=S2")
    assert status == 200
    times = data["times"]
    assert times["live"] is True
    assert times["end"] == ""
    assert times["seconds"] >= 600
    # Попыток нет: «—», а не ноль, и это не «Invalid Date».
    assert times["attempt_seconds"] is None
    assert times["attempt"] == ""


def test_step_stage_breakdown_by_role(server):
    """Этапы по карточкам: роль из `assignee`, этап из ключа, сумма не больше шага (C4)."""
    now = int(time.time())
    base = now - 9000
    plan = [("prompt", "prompter", 0, 600), ("step", "demo", 600, 5400),
            ("review:1", "reviewer", 5400, 6000), ("fix:1", "demo", 6000, 6900)]
    for suffix, assignee, offset, finish in plan:
        key = f"planloop:1:S1:{suffix}"
        add_step_card(server["db"], key, assignee, base + offset, status="done")
        set_card_times(server["db"], key, created=base + offset,
                       started=base + offset + 10, completed=base + finish)
    # Второй шаг плана: в свод по шагу он попасть не должен.
    add_step_card(server["db"], "planloop:1:S2:prompt", "prompter", base + 7200, status="done")
    set_card_times(server["db"], "planloop:1:S2:prompt", created=base + 7200,
                   completed=base + 7300)

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    stages = {stage["title"]: stage for stage in data["stages"]}
    assert stages["Постановка"]["role_label"] == "постановщик"
    assert stages["Исполнение"]["role_label"] == "исполнитель"
    assert stages["Ревью 1"]["role_label"] == "ревьюер"
    assert stages["Правки 1"]["role_label"] == "исполнитель"
    for stage in data["stages"]:
        assert stage["seconds"] >= 0 and stage["start"]
    assert sum(stage["seconds"] for stage in data["stages"]) <= data["times"]["seconds"]

    roles = {row["label"]: row for row in data["step_roles"]}
    assert roles["постановщик"]["stages"] == 1
    assert roles["исполнитель"]["stages"] == 2
    assert roles["ревьюер"]["stages"] == 1
    assert sum(row["seconds"] for row in data["step_roles"]) == sum(
        stage["seconds"] for stage in data["stages"])
    # Ключи не перепутаны: свод по плану шире, чем свод по шагу.
    assert sum(row["stages"] for row in data["roles"]) > sum(
        row["stages"] for row in data["step_roles"])


def test_open_step_stage_live(server):
    """Идущий этап: длительность от начала попытки, heartbeat — признак живости (C4, C6)."""
    now = int(time.time())
    started = now - 900
    add_step_card(server["db"], "planloop:1:S1:step", "demo", started, status="running")
    set_card_times(server["db"], "planloop:1:S1:step", created=started, started=started)
    set_heartbeat(server["db"], "planloop:1:S1:step", now - 60)

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    stage = next(item for item in data["stages"] if item["stage"] == "step")
    assert stage["live"] is True
    assert stage["end_ts"] is None
    assert stage["seconds"] >= 900
    assert stage["start_ts"] == started
    assert stage["heartbeat_ts"] == now - 60
    assert stage["stale"] is False

    html = index_html()
    for anchor in ("tickTimes", "setInterval(tickTimes, 1000)", "data-live", "liveText"):
        assert anchor in html, anchor


def test_plan_role_totals(server):
    """Сумма по ролям за план равна сумме длительностей этапов всех шагов (C5)."""
    base = int(time.time()) - 20000
    plan = [("planloop:1:S1:prompt", "prompter", 0, 300),
            ("planloop:1:S1:step", "demo", 300, 1500),
            ("planloop:1:S1:review:1", "reviewer", 1500, 600),
            ("planloop:1:S2:prompt", "prompter", 2100, 200),
            ("planloop:1:S2:step", "demo", 2300, 1700),
            ("planloop:1:S2:review:1", "reviewer", 4000, 500)]
    for key, assignee, offset, length in plan:
        add_step_card(server["db"], key, assignee, base + offset, status="done")
        set_card_times(server["db"], key, created=base + offset, started=base + offset,
                       completed=base + offset + length)

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    roles = {row["label"]: row for row in data["roles"]}
    assert roles["постановщик"]["stages"] == 2
    assert roles["исполнитель"]["stages"] == 2
    assert roles["ревьюер"]["stages"] == 2
    for row in data["roles"]:
        assert row["seconds"] > 0 and row["duration"]

    total = 0
    for step in ("S1", "S2"):
        status, payload = get(server["url"] + f"/api/step?plan=1&id={step}")
        assert status == 200
        total += sum(stage["seconds"] for stage in payload["stages"])
    assert sum(row["seconds"] for row in data["roles"]) == total


def test_time_source_is_card_not_journal(server):
    """Время берётся из карточек, а не из даты журнала; нет карточек — «—» и пояснение (C3)."""
    plan_path = server["root"] / "docs" / "plans" / "1-demo.md"
    text = plan_path.read_text(encoding="utf-8")
    plan_path.write_text(text.replace("| — | S1 | — | — | открыт |",
                                      "| 01.01.2020 | S1 | 01.01.2020 | — | открыт |"),
                         encoding="utf-8")
    t0 = int(time.time()) - 3600
    add_step_card(server["db"], "planloop:1:S1:step", "demo", t0, status="done")
    set_card_times(server["db"], "planloop:1:S1:step", created=t0, completed=t0 + 1200)

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    times = data["times"]
    assert times["start"] == snapshot._fmt_ts(t0)
    assert times["seconds"] == 1200
    assert "01.01.2020" not in json.dumps(times, ensure_ascii=False)

    status, data = get(server["url"] + "/api/step?plan=1&id=S2")
    assert status == 200
    times = data["times"]
    assert times["start_ts"] is None and times["end_ts"] is None
    assert times["seconds"] is None and times["start"] == ""
    assert times["note"]


def test_runs_history_for_card(server):
    """История исполнения карточки: две попытки, исход, ожидание слота, вывод (C7)."""
    now = int(time.time())
    created = now - 5000
    key = "planloop:1:S1:step"
    card_id = add_step_card(server["db"], key, "demo", created, status="done")
    set_card_times(server["db"], key, created=created, started=now - 4000,
                   completed=now - 1000)
    t2, t3, t4, t5 = now - 4000, now - 3500, now - 3000, now - 2000
    add_runs(server["db"], card_id, [
        {"started_at": t2, "ended_at": t3, "outcome": "completed", "summary": "шаг сдан"},
        {"started_at": t4, "ended_at": t5, "outcome": "crashed", "error": "упал на импорте"},
    ])

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    card = next(item for item in data["cards"] if item["key"] == key)
    attempts = card["attempts"]
    assert [run["outcome"] for run in attempts] == ["completed", "crashed"]
    assert attempts[0]["label"] == "завершена"
    assert attempts[0]["seconds"] == t3 - t2
    assert attempts[0]["waited"] == t2 - created
    assert attempts[0]["bad"] is False and attempts[1]["bad"] is True
    assert "упал на импорте" in attempts[1]["output"]


def test_runs_history_marks_timeout_and_quota(server):
    """Лимит времени и квота провайдера: исход, подпись и вывод попытки (C7)."""
    now = int(time.time())
    created = now - 4000
    key = "planloop:1:S1:step"
    card_id = add_step_card(server["db"], key, "demo", created, status="done")
    set_card_times(server["db"], key, created=created, completed=now - 100)
    add_runs(server["db"], card_id, [
        {"started_at": now - 3000, "ended_at": now - 2000, "outcome": "timed_out",
         "summary": "лимит 2 ч исчерпан"},
        {"started_at": now - 1500, "ended_at": now - 500, "outcome": "rate_limited",
         "error": "429 Too Many Requests"},
    ])

    status, data = get(server["url"] + "/api/step?plan=1&id=S1")
    assert status == 200
    attempts = next(item for item in data["cards"] if item["key"] == key)["attempts"]
    assert attempts[0]["bad"] is True and attempts[0]["label"] == "лимит времени"
    assert attempts[1]["bad"] is True and attempts[1]["rate_limited"] is True
    assert attempts[1]["label"] == "квота провайдера"
    assert "лимит 2 ч" in attempts[0]["output"]
    assert "429" in attempts[1]["output"]


# ───────────────────────── уведомления (план 3 S10) ─────────────────────────
# Всё на tmp-проектах и tmp-реестре фикстур: сеть и Telegram подменяются, всплывающее
# окно проверяется разметкой страницы — настоящий браузер тесту не нужен.

NOTIFY_DEFAULT = ["step_started", "step_closed", "changes_requested", "plan_stopped",
                  "card_crashed", "card_timed_out", "card_rate_limited"]
NOTIFY_ALL = NOTIFY_DEFAULT + ["tick_action", "plan_idle"]


def notify_registry(home: Path, entries: list[dict], global_enabled: bool = True) -> Path:
    """Реестр с глобальным выключателем: контейнер `notify` плюс записи проектов (§4.1)."""
    path = home / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"notify": {"enabled": global_enabled}, "projects": entries},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_notify_off_by_default(tmp_path: Path, hermes_stub: Path,
                               isolated_hermes_home: Path):
    """Свежий реестр: выключено у всех, девять видов, событий нет (D1, D2)."""
    root = install_project(tmp_path / "alpha", board="uiboard")
    with running(root) as (url, _httpd):
        status, data = get(url + "/api/notify")
        assert status == 200 and data["ok"] is True
        # Ключа `notify` нет — это «выключено», а не «включено по умолчанию».
        assert data["global"]["enabled"] is False
        assert [kind["id"] for kind in data["kinds"]] == NOTIFY_ALL
        assert data["projects"] and all(row["enabled"] is False for row in data["projects"])
        for row in data["projects"]:
            assert row["events"] == NOTIFY_DEFAULT
        snap_status, snap_data = get(url + "/api/snapshot?plan=1")
    assert snap_status == 200
    assert snap_data["notify"]["enabled"] is False
    assert snap_data["notify"]["global_enabled"] is False
    assert snap_data["notify"]["events"] == []
    html = index_html()
    for anchor in ('id="tab-btn-notify"', 'data-tab="notify"', 'id="tab-notify"',
                   "Notification.requestPermission", '"granted"',
                   "только когда вкладка"):
        assert anchor in html, anchor


def test_notify_settings_persist(tmp_path: Path, hermes_stub: Path,
                                 isolated_hermes_home: Path):
    """POST по проекту реестра пишется в файл и переживает перезапуск сервера (D1)."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    write_registry(isolated_hermes_home,
                   [{"id": "alpha", "name": "Альфа", "root": str(alpha)},
                    {"id": "beta", "name": "Бета", "root": str(beta)}])
    with running(alpha) as (url, _httpd):
        status, data = post_to(url, "/api/notify",
                               {"project": "beta", "enabled": True,
                                "events": ["step_closed"]})
        # Тело без `project` и без `global` адресата не описывает: 400, а не молчаливая
        # запись проекту по умолчанию (находка ревью 3-S10).
        bad_status, bad = post_to(url, "/api/notify", {"enabled": True})
    assert status == 200 and data["ok"] is True
    assert bad_status == 400 and "project" in bad["error"]
    saved = json.loads((isolated_hermes_home / "projects.json").read_text(encoding="utf-8"))
    entries = {row["id"]: row for row in saved["projects"]}
    assert entries["beta"]["notify"]["enabled"] is True
    assert entries["beta"]["notify"]["events"] == ["step_closed"]
    # Проект по умолчанию правкой чужой записи не задет: ключа `notify` у него нет.
    assert "notify" not in entries["alpha"]
    # Файл читается заново при старте: настройка не живёт в памяти сервера.
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/notify")
    assert status == 200
    rows = {row["id"]: row for row in data["projects"]}
    assert rows["beta"]["enabled"] is True
    assert rows["beta"]["events"] == ["step_closed"]
    assert rows["alpha"]["enabled"] is False


def test_notify_settings_atomic_and_keeps_unknown_fields(tmp_path: Path, hermes_stub: Path,
                                                         isolated_hermes_home: Path):
    """Чужие ключи целы, битый реестр — отказ без записи и объяснение на экране (D2)."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    path = isolated_hermes_home / "projects.json"
    path.write_text(json.dumps({"panel": {"theme": "dark"},
                                "projects": [{"id": "beta", "name": "Бета",
                                              "root": str(beta), "custom": 42}]},
                               ensure_ascii=False), encoding="utf-8")
    with running(alpha) as (url, _httpd):
        status, data = post_to(url, "/api/notify", {"project": "beta", "enabled": True})
    assert status == 200 and data["ok"] is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["panel"] == {"theme": "dark"}
    assert saved["projects"][0]["custom"] == 42
    assert saved["projects"][0]["name"] == "Бета"
    assert saved["projects"][0]["notify"] == {"enabled": True, "events": NOTIFY_DEFAULT}

    # Одна опечатка в файле не должна стоить всех записей реестра.
    path.write_text('{"', encoding="utf-8")
    before = path.read_bytes()
    with running(alpha) as (url, _httpd):
        bad_status, bad = post_to(url, "/api/notify", {"project": "beta", "enabled": False})
        ok_status, listing = get(url + "/api/notify")
    assert bad_status == 400 and "реестр" in bad["error"]
    assert path.read_bytes() == before
    assert ok_status == 200
    assert listing["error"] and "реестр" in listing["error"]
    assert listing["global"]["enabled"] is False


def test_event_kinds_filter_by_project(tmp_path: Path, hermes_stub: Path,
                                       isolated_hermes_home: Path):
    """Галочки одного проекта не протекают в соседний: фильтр по видам (§4.2)."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    notify_registry(isolated_hermes_home, [
        {"id": "alpha", "name": "Альфа", "root": str(alpha),
         "notify": {"enabled": True, "events": ["step_closed"]}},
        {"id": "beta", "name": "Бета", "root": str(beta),
         "notify": {"enabled": True, "events": NOTIFY_ALL}},
    ])
    now = int(time.time())
    for root in (alpha, beta):
        write_history({"root": root}, [
            {"ts": now - 60, "plan": "1", "step": "S1", "action": "close_step",
             "detail": "шаг 1-S1 закрыт", "commit": "abc1234"},
            {"ts": now - 30, "plan": "1", "step": "S2", "action": "create_fix",
             "detail": "правки 1-S2", "commit": ""},
        ])
    with running(alpha) as (url, _httpd):
        a_status, a_data = get(url + "/api/snapshot?plan=1&project=alpha")
        b_status, b_data = get(url + "/api/snapshot?plan=1&project=beta")
    assert a_status == 200 and b_status == 200
    assert a_data["notify"]["enabled"] is True
    assert [event["kind"] for event in a_data["notify"]["events"]] == ["step_closed"]
    assert {event["kind"] for event in b_data["notify"]["events"]} == {
        "step_closed", "changes_requested"}


def test_event_dedup_window(tmp_path: Path, hermes_stub: Path,
                            isolated_hermes_home: Path):
    """Три одинаковые попытки — одно событие: ключ «проект+план+шаг+вид» (D4)."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    notify_registry(isolated_hermes_home, [
        {"id": "alpha", "name": "Альфа", "root": str(alpha),
         "notify": {"enabled": True, "events": NOTIFY_ALL}}])
    now = int(time.time())
    card = add_step_card(hermes_stub, "planloop:1:S1:step", "demo", now - 600)
    add_runs(hermes_stub, card, [
        {"started_at": now - 500, "ended_at": now - 490, "outcome": "rate_limited"},
        {"started_at": now - 400, "ended_at": now - 390, "outcome": "rate_limited"},
        {"started_at": now - 300, "ended_at": now - 290, "outcome": "rate_limited"},
    ])
    snapshot.clear_cache()
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/snapshot?plan=1")
    assert status == 200
    events = [event for event in data["notify"]["events"]
              if event["kind"] == "card_rate_limited"]
    assert len(events) == 1
    assert events[0]["key"].endswith(":card_rate_limited")
    assert events[0]["card"] == card
    assert "квот" in events[0]["text"]


def test_notify_test_endpoint(tmp_path: Path, hermes_stub: Path,
                              isolated_hermes_home: Path):
    """Тестовая кнопка отвечает только браузеру: ни файла истории, ни Telegram (D9)."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    history = alpha / ".pipeline" / "state" / ui_server.DELIVERY_FILE
    with running(alpha) as (url, _httpd):
        status, data = post_to(url, "/api/notify/test", {"project": "alpha"})
        list_status, listing = get(url + "/api/notify")
    assert status == 200 and data["ok"] is True and data["test"] is True
    assert data["notice"]["test"] is True
    assert "тест" in data["notice"]["text"].lower()
    assert data["project"]["id"] == "alpha"
    assert not history.exists()
    assert list_status == 200 and listing["deliveries"] == []


def test_notify_telegram_mirrors_feed(tmp_path: Path, hermes_stub: Path,
                                      isolated_hermes_home: Path, monkeypatch):
    """Одно событие — одно сообщение с префиксом; повтор и выключенный канал молчат (D8)."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    notify_registry(isolated_hermes_home, [
        {"id": "alpha", "name": "Альфа", "root": str(alpha),
         "notify": {"enabled": True, "events": ["step_closed"]}}])
    cfg = json.loads((alpha / "pipeline.json").read_text(encoding="utf-8"))
    cfg["notify"] = {**(cfg.get("notify") or {}), "enabled": True, "prefix": "[3-S1] "}
    (alpha / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    now = int(time.time())
    write_history({"root": alpha}, [
        {"ts": now - 60, "plan": "1", "step": "S1", "action": "close_step",
         "detail": "шаг 1-S1 закрыт", "commit": "abc1234"}])
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(ui_server.telegram, "send_message",
                        lambda token, chat, text: calls.append((token, chat, text)))
    monkeypatch.setenv("STEP_PIPELINE_TELEGRAM_BOT_TOKEN", "token-тест")
    monkeypatch.setenv("STEP_PIPELINE_TELEGRAM_CHAT_ID", "42")

    state = ui_server.State(alpha, project_id="alpha", name="Альфа")
    state.notify_primed = True      # кадр прошёл: доставка не первый (D7)
    feed = ui_server.notify_feed(state, "1")
    assert len(feed["events"]) == 1
    event = feed["events"][0]
    delivered = ui_server.notify_deliver(state, feed)
    assert len(delivered) == 1 and len(calls) == 1
    token, chat, text = calls[0]
    assert (token, chat) == ("token-тест", "42")
    assert text == "[3-S1] " + event["text"]

    # Тот же ключ повторно не отправляется: отметка живёт в состоянии сервера.
    assert ui_server.notify_deliver(state, ui_server.notify_feed(state, "1")) == []
    assert len(calls) == 1
    rows = (alpha / ".pipeline" / "state" / ui_server.DELIVERY_FILE).read_text(
        encoding="utf-8").splitlines()
    assert len(rows) == 1 and "шаг S1 закрыт" in rows[0]

    # Канал без токена: событие есть, вызовов нет.
    monkeypatch.delenv("STEP_PIPELINE_TELEGRAM_BOT_TOKEN")
    state.notified.clear()
    state.notified_project.clear()
    calls.clear()
    assert ui_server.notify_deliver(state, ui_server.notify_feed(state, "1")) == []
    assert calls == []


def test_index_notify_screen_markup():
    """Экран «Уведомления»: выключатель, разрешение браузера, строки, тест и история (§4.4)."""
    html = index_html()
    for anchor in ('id="tab-btn-notify"', 'data-tab="notify"', 'id="tab-notify"',
                   'id="notify-master"', 'id="notify-permission"', 'id="notify-note"',
                   'id="notify-projects"', 'id="notify-test"', 'id="notify-events"',
                   'id="notify-history"', "Notification.requestPermission",
                   "Notification.permission", "data.notify", "/api/notify/test",
                   "X-Pipeline-Panel"):
        assert anchor in html, anchor
    # Виды внимания приходят с сервера: страница не держит свой список галочек.
    assert "kind.label" in html
    assert "/api/notify" in html
    # Всплытие гейтится состоянием сервера, а не чекбоксом notify-master: тот выставляется
    # только при открытии вкладки, и после F5 панель молчала (находка ревью 3-S10).
    assert "notifyGate.global && notifyGate.project" in html
    assert "applyNotifyGate" in html


# ── строки планов в «Обзоре»: статус, причина и действие (план 5 S2A) ──

def test_snapshot_plan_rows_carry_state_and_action(server):
    """Строка плана знает состояние: без метки cron — действие «включить джобу»."""
    status, data = get(server["url"] + "/api/snapshot?plan=1")
    assert status == 200
    row = data["plans"][0]
    assert row["state"] == "running"
    assert row["state_label"] == "идёт"
    assert row["action"]["kind"] == "cron"
    (server["root"] / ".pipeline" / "cron.json").write_text(
        json.dumps({"driver": "demo", "name": "План: страж шага (конвейер)"},
                   ensure_ascii=False), encoding="utf-8")
    snapshot.clear_cache()
    status, data = get(server["url"] + "/api/snapshot?plan=1")
    assert status == 200
    assert data["plans"][0]["action"]["kind"] == "none"


def test_stop_flag_status_comes_from_decision_not_file(server):
    """Стоп владельца — «остановлен»; инцидентный без причины — «идёт» с пометкой."""
    path = stop_file(server)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": "owner", "source": "owner",
                                "reason": "ждём решения владельца"}, ensure_ascii=False),
                    encoding="utf-8")
    snapshot.clear_cache()
    status, data = get(server["url"] + "/api/snapshot?plan=1")
    assert status == 200
    row = data["plans"][0]
    assert row["state"] == "stopped"
    assert row["action"]["kind"] == "resume"
    assert "--plan 1 --resume" in row["action"]["command"]
    assert data["stop"] is True and data["overview"]["stop"] is not None

    # Причина инцидента исчезла (шаг открыт, борда жива): файл есть, статус — «идёт».
    path.write_text(json.dumps({"kind": "incident", "source": "gate",
                                "reason": "нет постановки шага S1"}, ensure_ascii=False),
                    encoding="utf-8")
    snapshot.clear_cache()
    status, data = get(server["url"] + "/api/snapshot?plan=1")
    assert status == 200
    row = data["plans"][0]
    assert row["state"] == "running"
    assert row["note"].startswith("стоп снят (было:")
    assert data["stop"] is False
    assert data["overview"]["stop"] is None


def test_index_plan_rows_markup():
    """Панель рисует строки планов из снимка; чипов и своей подписи статуса нет."""
    html = index_html()
    for anchor in ('class="plan-row', "plan-badge", "plan-reason", "plan-note",
                   "plan-action", "state_label"):
        assert anchor in html, anchor
    assert "plan-chip" not in html
    assert "${esc(p.closed)} закрыто ·" not in html
    # Контейнер планов — внутри секции «Обзор», до «Потока текущего шага».
    overview = html[html.index('id="overview"'):html.index('id="flow"')]
    assert 'id="plans"' in overview
