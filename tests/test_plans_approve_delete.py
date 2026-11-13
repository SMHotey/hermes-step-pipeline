# -*- coding: utf-8 -*-
"""Утверждение и удаление плана из панели: шесть следов, охрана и предпросмотр.

Контракт: `POST /api/plan/accept` остаётся точкой вызова `plan_accept.approve` и пишет
запись утверждения с `version_sha256`; `POST /api/plan/delete` убирает файл плана, запись
утверждения, стоп-флаг, `plan_<N>_state.json`, `plan_<N>_rounds.json` и номер из
`driven_plans`, отказывает `409`, пока у плана есть карточка в не-терминальном статусе, и
отвечает `403` в предпросмотре. Тесты идут по проводу: настоящий `ThreadingHTTPServer` на
127.0.0.1 и HTTP через `urllib`, поэтому проверяются коды и тело ответа, а не возврат
функции.

Переходы наружу подменяются по одному и по существу: утверждение идёт настоящим
`plan_accept.approve` (иначе тест доказывал бы заглушку), борда — карточкой в поддельной
базе (`hermes_stub`), а движок — файлами, которые он пишет в каталог состояния до вызова
(`plan_N_stop.json`, `plan_N_state.json`, `plan_N_rounds.json`). Хуков стража
(`avnew_gate_decision`) здесь нет намеренно: ни один из четырёх тестов не запускает тик, и
подменять его значило бы проверять то, чего маршрут не делает.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from helpers import install_project
import ui_server
from step_pipeline import plan_accept

PANEL = {"X-Pipeline-Panel": "1"}
STATE = Path(".pipeline") / "state"
SECOND = helpers.PLAN.replace("# 1. Демо-план конвейера", "# 2. Второй план")
# Литеральные пути следов, а не помощники: иначе тест проверяет помощник его же руками
# и не поймает неверный каталог состояния (то же правило, что у стоп-флага S3).
TRACES = (Path("docs") / "plans" / "2-second.md",
          STATE / "plan_2_approval.json", STATE / "plan_2_stop.json",
          STATE / "plan_2_state.json", STATE / "plan_2_rounds.json")


# ─────────────────────────────────── стенд ───────────────────────────────────

def write_trace(root: Path, name: str, payload: dict) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def card(db: Path, key: str = "planloop:2:S1:step", card_id: str = "t_ab1234",
         status: str = "running") -> None:
    """Карточка в поддельной борде — тем же ключом, каким её пишет страж."""
    con = sqlite3.connect(db)
    try:
        con.executescript(helpers.KANBAN_SCHEMA)
        con.execute("INSERT INTO tasks (id, title, status, assignee, idempotency_key,"
                    " created_at) VALUES (?, ?, ?, 'driver', ?, ?)",
                    (card_id, "шаг 2-S1", status, key, int(time.time())))
        con.commit()
    finally:
        con.close()


def read_cfg(root: Path) -> dict:
    return json.loads((root / "pipeline.json").read_text(encoding="utf-8"))


def start_server(root: Path, preview: bool = False) -> dict:
    httpd = ui_server.make_server(root, port=0, preview=preview)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "root": root, "httpd": httpd}


def stop_server(server: dict) -> None:
    server["httpd"].shutdown()
    server["httpd"].server_close()


def post(url: str, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    """POST с JSON-телом; ошибки HTTP тоже разбираются, а не бросаются."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{path}", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


@pytest.fixture()
def server(tmp_path: Path, monkeypatch, hermes_stub: Path):
    """Панель над проектом с двумя планами; у обоих — все следы, кроме файла у плана 1.

    Окружение изолируется первым делом (причина та же, что в S3): `PIPELINE_STATE_DIR`
    сдвинул бы каталог состояния, а литеральные пути следов разошлись бы с тем, куда
    пишут помощники. `PIPELINE_BOARD` снимается отдельно: явная борда перекрыла бы
    `PIPELINE_KANBAN_DB` поддельной базы (`proj.board_db` при заданной борде env не
    читает), и охрана смотрела бы в настоящую борду владельца.
    """
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    monkeypatch.delenv("PIPELINE_BOARD", raising=False)
    root = install_project(tmp_path / "plans-delete", board="uiboard")
    (root / "docs" / "plans" / "2-second.md").write_text(SECOND, encoding="utf-8")
    assert plan_accept.approve(root, "2")["ok"] is True
    cfg = read_cfg(root)
    cfg["driven_plans"] = ["1", "2"]
    (root / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    for plan in ("1", "2"):
        write_trace(root, f".pipeline/state/plan_{plan}_stop.json",
                    {"kind": "owner", "source": "owner", "reason": f"стоп плана {plan}",
                     "at": "13.11.2026 10:00:00"})
        write_trace(root, f".pipeline/state/plan_{plan}_state.json", {"hold_steps": []})
    write_trace(root, ".pipeline/state/plan_2_rounds.json",
                {"extra_fix_rounds": 1, "at": "13.11.2026 10:05:00", "reason": "ещё круг"})
    server = start_server(root)
    server["db"] = hermes_stub
    try:
        yield server
    finally:
        stop_server(server)


@pytest.fixture()
def preview_server(tmp_path: Path):
    """Предпросмотр над каталогом без установленного конвейера — только чтение."""
    root = tmp_path / "bare"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "1-demo.md").write_text(helpers.PLAN, encoding="utf-8")
    server = start_server(root, preview=True)
    try:
        yield server
    finally:
        stop_server(server)


# ─────────────────────────────────── тесты ───────────────────────────────────

def test_delete_removes_all_six_traces_of_named_plan(server):
    """`delete` плана 2: пять файлов и номер в `driven_plans`; план 1 не тронут ни байтом."""
    root = server["root"]
    kept = {name: (root / name).read_bytes() for name in
            ("docs/plans/1-demo.md", ".pipeline/state/plan_1_approval.json",
             ".pipeline/state/plan_1_stop.json", ".pipeline/state/plan_1_state.json")}

    status, data = post(server["url"], "/api/plan/delete", {"plan": "2"}, PANEL)
    assert status == 200, data
    assert data["ok"] is True and data["action"] == "plan_delete"
    assert data["status"] == "deleted" and data["plan"] == "2"
    assert data["changed"] is True and data["driven"] == ["1"]
    assert data["project"]["id"]
    assert set(data["removed"]) == {Path(name).as_posix() for name in TRACES}
    for name in TRACES:
        assert (root / name).exists() is False, name
    for name, before in kept.items():
        assert (root / name).read_bytes() == before, name
    cfg = read_cfg(root)
    assert cfg["driven_plans"] == ["1"]
    # Правка списка не переписала конфиг: остальные ключи на месте.
    assert cfg["project"] == "demo"
    for key in ("profiles", "runtimes", "paths"):
        assert key in cfg


def test_delete_refused_while_card_in_progress(server):
    """Карточка в работе: 409, имя карточки в причине, ни один файл не тронут.

    После перевода карточки в терминальный статус тот же жест проходит: охрана смотрит
    статус, а не факт карточки. Третий жест по уже удалённому плану — 400 `missing`:
    отсутствующий след не ошибка, а `None` из `find_plan` не должен давать 500.
    """
    root = server["root"]
    card(server["db"])
    before = {name: (root / name).read_bytes() for name in TRACES}
    cfg_before = (root / "pipeline.json").read_bytes()

    status, data = post(server["url"], "/api/plan/delete", {"plan": "2"}, PANEL)
    assert status == 409, data
    assert data["ok"] is False and data["status"] == "busy" and data["busy"] is True
    assert "t_ab1234" in data["error"]
    assert data["cards"][0] == {"id": "t_ab1234", "status": "running",
                                "key": "planloop:2:S1:step"}
    for name, payload in before.items():
        assert (root / name).read_bytes() == payload, name
    assert (root / "pipeline.json").read_bytes() == cfg_before

    helpers.complete_card(server["db"], ":S1:step")
    status, data = post(server["url"], "/api/plan/delete", {"plan": "2"}, PANEL)
    assert status == 200, data
    assert data["status"] == "deleted"
    assert (root / TRACES[0]).exists() is False

    # Повторный жест: следов уже нет, файла плана тоже (`find_plan` → `None`) — 400, не 500.
    status, data = post(server["url"], "/api/plan/delete", {"plan": "2"}, PANEL)
    assert status == 400, data
    assert data["ok"] is False and data["status"] == "missing" and data["plan"] == "2"
    assert "removed" not in data and "busy" not in data


def test_accept_route_answers_200_and_writes_version_sha(server):
    """Утверждение из панели: 200 и запись с `version_sha256` (ключ контракта S7)."""
    root = server["root"]
    folder = plan_accept.drafts_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "5-demo.md").write_text(
        helpers.PLAN.replace("# 1. Демо-план конвейера", "# 5. Новый план конвейера"),
        encoding="utf-8")

    status, data = post(server["url"], "/api/plan/accept", {"plan": "5"}, PANEL)
    assert status == 200, data
    assert data["ok"] is True and data["status"] == "approved"
    assert data["action"] == "plan_accept" and data["plan"] == "5"
    final = root / "docs" / "plans" / "5-demo.md"
    record = json.loads(plan_accept.approval_file(root, "5").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{64}", str(record["version_sha256"] or ""))
    assert record["version_sha256"] == plan_accept._version_sha256(root, final)
    assert record["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()


def test_delete_in_preview_is_read_only(preview_server):
    """Предпросмотр: 403 на удаление, файл плана на месте, каталог конвейера не создан."""
    root = preview_server["root"]
    status, data = post(preview_server["url"], "/api/plan/delete", {"plan": "1"}, PANEL)
    assert status == 403, data
    assert data["ok"] is False
    assert "предпросмотр только читает" in data["error"]
    assert (root / "docs" / "plans" / "1-demo.md").is_file()
    assert (root / ".pipeline").exists() is False
    assert (root / "pipeline.json").exists() is False
