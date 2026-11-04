# -*- coding: utf-8 -*-
"""UI-сервер: снимок, действия, настройки. Без модели и без настоящей борды."""
from __future__ import annotations

import json
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


def test_checks_start_in_not_checked_state(server):
    """«Не проверяли» — отдельное состояние, а не пустой список находок."""
    status, data = get(server["url"] + "/api/checks")
    assert status == 200 and data["ok"] is True
    assert data["checked_at"] == "" and data["critical"] is None
    assert data["busy"] is False
    assert data["notes"] == [] and data["problems"] == []
    assert data["lint"] == {"files": 0, "problems": []}


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
    # Критичные = проблемы доктора (здесь их заведомо больше нуля) + находки линтера.
    assert data["critical"] == len(data["problems"]) + len(findings) > 0
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
    """Бейдж в шапке берёт короткую форму из снимка: до проверки — «не проверяли»."""
    _, before = get(server["url"] + "/api/snapshot")
    assert before["overview"]["checks"] == {"critical": None, "found": False,
                                            "checked_at": "", "busy": False}
    get(server["url"] + "/api/checks?refresh=1")
    _, after = get(server["url"] + "/api/snapshot")
    summary = after["overview"]["checks"]
    assert summary["found"] is True and summary["critical"] > 0
    assert set(summary) == {"critical", "found", "checked_at", "busy"}


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
    """Вкладка «Проверки»: кнопка, пустой вид, находки со ссылкой на шаг."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('data-tab="checks"', 'id="tab-checks"', "/api/checks", "refresh=1",
                   'id="btn-checks"', 'id="checks-clean"', 'id="checks-lint"',
                   'id="checks-lint-note"', 'id="checks-notes"', "renderChecks(data)",
                   "gotoSighting", "ov.checks", "не проверяли"):
        assert anchor in html
