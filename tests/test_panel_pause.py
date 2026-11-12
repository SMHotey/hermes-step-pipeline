# -*- coding: utf-8 -*-
"""Глобальная пауза панели: один жест на реестр, а не на проект.

Проверяется то, что видит владелец: стоп-флаги появляются у всех открытых планов,
кнопка становится «Запустить всё» (и это состояние считается из реестра заново —
значит переживает перезагрузку страницы), а «Запустить всё» снимает свои флаги и
не трогает инцидентные.
"""
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

from helpers import KANBAN_SCHEMA, install_project  # noqa: E402
import panel_pause  # noqa: E402
import project as proj  # noqa: E402
import snapshot  # noqa: E402
import ui_server  # noqa: E402


@pytest.fixture(autouse=True)
def clean_caches():
    """Кэши планов и снимка — состояние процесса: между тестами их обнуляем."""
    panel_pause.clear_cache()
    snapshot.clear_cache()
    yield
    panel_pause.clear_cache()
    snapshot.clear_cache()


def rows(root: Path, project_id: str = "demo") -> list[dict]:
    """Строка реестра в той форме, в какой её видит панель."""
    return [{"id": project_id, "name": project_id, "root": str(root)}]


def stop_file(root: Path, plan_id: str = "1") -> Path:
    return root / ".pipeline" / "state" / f"plan_{plan_id}_stop.json"


def running_card(db: Path, card_id: str = "t_1") -> None:
    """Карточка «в работе»: по ней видно, что шаг ещё идёт и пауза не наступила."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        con.execute("INSERT INTO tasks (id, title, status, assignee, idempotency_key,"
                    " created_at) VALUES (?, ?, 'running', 'driver', '1:S1:step', ?)",
                    (card_id, "шаг 1-S1", int(time.time())))
        con.commit()
    finally:
        con.close()


def test_pause_all_stops_every_open_plan(project: Path, hermes_stub: Path):
    """Жест ставит owner-стоп-флаг, помнит его в реестре и видит полную паузу."""
    result = panel_pause.pause_all(rows(project))

    assert result["ok"] is True
    assert result["stopped"]["demo"]["plans"] == ["1"]
    payload = json.loads(stop_file(project).read_text(encoding="utf-8"))
    assert payload["kind"] == "owner" and payload["source"] == "owner"
    assert "остановить после шага" in payload["reason"].lower()

    marker = proj.read_pause()
    assert marker["active"] is True
    assert marker["projects"]["demo"]["plans"] == ["1"]
    assert marker["requested_at_text"]

    state = result["state"]
    assert state["all_stopped"] is True and state["all_paused"] is True
    assert state["projects"][0]["stopped_plans"] == ["1"]


def test_state_is_recomputed_from_registry(project: Path):
    """Подпись кнопки после перезагрузки страницы: считается из флагов, а не из памяти."""
    assert panel_pause.state(rows(project))["all_stopped"] is False

    panel_pause.pause_all(rows(project))
    panel_pause.clear_cache()

    fresh = panel_pause.state(rows(project))
    assert fresh["all_stopped"] is True
    assert fresh["requested_at_text"], "время жеста обязано переживать пересчёт"


def test_state_keeps_pause_until_cards_close(project: Path, hermes_stub: Path):
    """Карточка в работе — «остановлено», но не «на паузе»: уведомление ждёт её."""
    panel_pause.pause_all(rows(project))
    running_card(hermes_stub)
    panel_pause.clear_cache()

    state = panel_pause.state(rows(project))
    assert state["all_stopped"] is True
    assert state["all_paused"] is False
    assert state["running"] == ["demo"]
    assert state["projects"][0]["running"] == ["t_1"]


def test_resume_all_clears_gesture_flags(project: Path):
    """«Запустить всё» снимает флаги жеста и забывает маркер."""
    panel_pause.pause_all(rows(project))
    assert stop_file(project).is_file()

    result = panel_pause.resume_all(rows(project))

    assert result["ok"] is True
    assert result["resumed"]["demo"]["plans"] == ["1"]
    assert not stop_file(project).exists()
    assert proj.read_pause()["active"] is False
    assert result["state"]["all_stopped"] is False


def test_resume_all_without_marker_clears_owner_flags(project: Path):
    """Стоп, поставленный по проекту вручную, «Запустить всё» тоже снимает."""
    proj.run_engine(project, "gate.py", "--stop", "ручной стоп", "--plan", "1")
    assert stop_file(project).is_file()

    result = panel_pause.resume_all(rows(project))

    assert result["ok"] is True
    assert not stop_file(project).exists()


def test_resume_all_keeps_incident_flags(project: Path):
    """Флаг инцидента — не дело панели: его снимает страж, а не кнопка владельца."""
    path = stop_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"reason": "две попытки упали", "source": "gate",
                                "kind": "incident", "at": "01.01.2026 00:00:00"},
                               ensure_ascii=False), encoding="utf-8")
    panel_pause.clear_cache()

    result = panel_pause.resume_all(rows(project))

    assert result["ok"] is True
    assert path.is_file(), "инцидентный флаг панель не снимает"
    assert proj.read_pause()["active"] is False


def test_pause_all_reports_nothing_to_stop(tmp_path: Path):
    """Проект без открытых планов: честный отказ, а не «пауза» на пустом месте."""
    root = install_project(tmp_path / "empty")
    (root / "docs" / "plans" / "1-demo.md").unlink()
    panel_pause.clear_cache()

    result = panel_pause.pause_all(rows(root))

    assert result["ok"] is False
    assert "открытых планов нет" in result["error"]
    assert proj.read_pause()["active"] is False


def test_state_skips_project_without_pipeline(tmp_path: Path):
    """Каталог без `.pipeline` в паузе не участвует: его и останавливать нечем."""
    plain = tmp_path / "plain"
    plain.mkdir()

    state = panel_pause.state([{"id": "plain", "name": "plain", "root": str(plain)}])

    assert state["projects"][0]["installed"] is False
    assert state["all_stopped"] is False


def test_write_pause_wants_container(tmp_path: Path, isolated_hermes_home: Path):
    """Реестр-список: маркер некуда писать, и это сказано прямо, а не молча."""
    proj.projects_file().write_text(json.dumps([{"id": "a", "root": str(tmp_path)}]),
                                    encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        proj.write_pause({"requested_at": 1})
    assert "контейнер" in str(exc.value)
    assert proj.read_pause()["active"] is False


# ── панель: действие, снимок и разметка ──

@pytest.fixture()
def server(tmp_path: Path, hermes_stub: Path):
    root = install_project(tmp_path / "ui-pause", board="pauseboard")
    httpd = ui_server.make_server(root, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "root": root, "httpd": httpd}
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}/api/action", data=data,
                                     headers={"Content-Type": "application/json",
                                              "X-Pipeline-Panel": "1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def wait_gesture(server, timeout: float = 120.0) -> dict:
    """Дождаться конца фонового жеста: он намеренно не держит HTTP-ответ."""
    handler = server["httpd"].RequestHandlerClass
    state = handler.state
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with state.pause_lock:
            if not state.pause_pending:
                break
        time.sleep(0.2)
    _, snap = get(server["url"] + "/api/snapshot")
    return snap["pause"]


def test_action_pause_all_and_resume_all(server):
    """Действия панели: кнопка останавливает реестр, вторая — возвращает в работу."""
    status, data = post(server["url"], {"action": "pause_all"})

    assert status == 200 and data["ok"] is True
    assert data["started"] is True

    pause = wait_gesture(server)
    assert stop_file(server["root"]).is_file()
    assert pause["working"] is False
    assert pause["all_stopped"] is True
    assert pause["all_paused"] is True

    status, data = post(server["url"], {"action": "resume_all"})
    assert status == 200 and data["ok"] is True

    pause = wait_gesture(server)
    assert not stop_file(server["root"]).exists()
    assert pause["all_stopped"] is False


def test_second_gesture_is_refused_while_first_runs(server, monkeypatch):
    """Два жеста сразу не запускаются: они дрались бы за блокировку тика движка."""
    import panel_pause as pause_mod

    started = threading.Event()
    release = threading.Event()

    def slow_pause_all(rows, board=None):
        started.set()
        release.wait(30)
        return {"ok": True, "stopped": {}, "failed": [], "text": "медленно", "state": None}

    monkeypatch.setattr(pause_mod, "pause_all", slow_pause_all)
    status, data = post(server["url"], {"action": "pause_all"})
    assert status == 200 and data["ok"] is True
    assert started.is_set()

    status, second = post(server["url"], {"action": "pause_all"})
    assert status == 409 and second["ok"] is False and second["busy"] is True

    release.set()
    wait_gesture(server)


def test_snapshot_carries_autostart_status(server):
    """Снимок отдаёт статус автозагрузки: чекбокс рисуется по системе, а не по памяти."""
    _, snap = get(server["url"] + "/api/snapshot")
    auto = snap["autostart"]
    assert auto["supported"] is True
    assert auto["mechanism"]
    assert isinstance(auto["enabled"], bool)


def test_action_autostart_requires_flag(server):
    """Без поля `on` действие отказано: угадывать «включить» нельзя."""
    status, data = post(server["url"], {"action": "autostart"})
    assert status == 400 and data["ok"] is False
    assert "on" in data["error"]


def test_index_pause_and_autostart_markup():
    """Разметка: кнопка паузы, строка состояния и чекбокс автозагрузки."""
    html = (PLUGIN / "step_pipeline" / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('id="btn-pause-all"', 'id="pause-banner"', 'id="autostart"',
                   'id="autostart-hint"', "Остановить после шага", "Запустить всё",
                   '$("btn-pause-all").onclick = togglePauseAll',
                   '$("autostart").onchange = (event) => toggleAutostart(event.target.checked)',
                   "renderPause(data.pause)", "renderAutostart(data.autostart)"):
        assert anchor in html, anchor
