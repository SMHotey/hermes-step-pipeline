# -*- coding: utf-8 -*-
"""Запуск и остановка плана из панели: адресность, повторный жест, предпросмотр.

Четыре теста фиксируют контракт шага S3 плана 6: `POST /api/plan/start` добавляет номер
в `driven_plans` и снимает стоп-флаг адресованной командой движка (`--plan N --resume`),
`POST /api/plan/stop` ставит owner-стоп плана с причиной (`--plan N --stop "причина"`),
повторный `start` файл не переписывает, а в предпросмотре оба маршрута отвечают 403 и не
создают ни одного файла. Тесты идут по проводу: настоящий `ThreadingHTTPServer` на
127.0.0.1 и HTTP через `urllib`.

Приёмка требует проекта с **двумя** планами: на одном плане неадресованный `--resume`
снял бы стоп именно этого единственного плана, и тест не отличил бы его от `--plan N`.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from helpers import install_project
import ui_server
from step_pipeline import plans_api

PANEL = {"X-Pipeline-Panel": "1"}
STATE = Path(".pipeline") / "state"
STOP = STATE / "plan_{plan}_stop.json"

SECOND_PLAN = """# 2. Второй план

## Шаги

### S1. Первый шаг (1 дн)

- Файл: `app/a.py`
- Готово = файл `app/a.py` есть

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


def put_stop(root: Path, plan_id: str, reason: str) -> Path:
    """Стоп-флаг плана: литеральный путь, а не `plans_api.stop_flag` — иначе тест
    проверяет помощник его же руками и не поймает неверный каталог состояния."""
    path = root / str(STOP).format(plan=plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": "owner", "source": "owner", "reason": reason,
                                "at": "12.11.2026 10:00:00"}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def stop_path(root: Path, plan_id: str) -> Path:
    return root / str(STOP).format(plan=plan_id)


def hold_tick(root: Path) -> Path:
    """Занять тик так, как его держит `state_io.acquire`: живой pid, наш хост, свежее время.

    Протухшую блокировку движок снимает сам (`state_io._stale` снимает её по возрасту,
    чужому хосту или мёртвому pid), поэтому pid обязан быть живым: иначе команда
    применится и ветка отказа не исполнится.
    """
    path = root / STATE / "tick.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                "at": time.strftime("%d.%m.%Y %H:%M:%S"),
                                "at_ts": time.time(), "what": "tick"}, ensure_ascii=False),
                    encoding="utf-8")
    return path


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


@pytest.fixture()
def server(tmp_path: Path, monkeypatch, hermes_stub: Path):
    """Панель над проектом с двумя планами.

    Окружение изолируется первым делом: движок ставит `PIPELINE_STATE_DIR` выше
    `paths.state`, `PIPELINE_PLANS` перекрывает `driven_plans` целиком, а `PIPELINE_CONFIG`
    подменил бы сам файл конфигурации — при любом из них литеральный путь стоп-флага в
    тесте разошёлся бы с тем, куда пишет `--stop`.
    """
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    # Ожидание занятой блокировки в тестах не нужно: отказ движка одинаков и при нуле,
    # а штатные 5 с на каждый жест с занятым тиком растянули бы прогон вчетверо.
    monkeypatch.setenv("PIPELINE_LOCK_WAIT", "0")
    root = install_project(tmp_path / "plans-start", board="uiboard")
    (root / "docs" / "plans" / "2-second.md").write_text(SECOND_PLAN, encoding="utf-8")
    cfg = read_cfg(root)
    cfg["driven_plans"] = ["1"]
    (root / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    put_stop(root, "1", "стоп плана 1")
    put_stop(root, "2", "стоп плана 2")
    server = start_server(root)
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


def repeat_stop_same_bytes(url: str, reason: str, first: dict,
                           attempts: int = 12) -> tuple[int, dict]:
    """Повторять `stop` тем же телом, пока запись не попадёт в ту же секунду, что прежняя.

    `at` у движка секундный (`engine/gate.py:336`), поэтому две записи в одну секунду
    дают побайтово одинаковый флаг — только на такой паре и проверяется признак «флаг
    записан». На границе секунды payload отличается, и повтор ничего не доказывает,
    поэтому жест повторяется, а попадание подтверждается по `at` ответа.
    """
    previous = first["stop"]
    for _ in range(attempts):
        status, data = post(url, "/api/plan/stop", {"plan": "2", "reason": reason}, PANEL)
        assert status == 200, data           # равенство payload как признак давало здесь 400
        if data["stop"]["at"] == previous["at"]:
            return status, data
        previous = data["stop"]
    pytest.fail(f"повтор жеста не попал в ту же секунду за {attempts} попыток")


def test_start_adds_plan_to_driven_and_clears_its_stop(server):
    """`start` на плане 2: номер в `driven_plans`, снят именно стоп плана 2."""
    root = server["root"]
    status, data = post(server["url"], "/api/plan/start", {"plan": "2"}, PANEL)
    assert status == 200, data
    assert data["ok"] is True
    assert data["action"] == "plan_start"
    assert data["status"] == "started"
    assert data["plan"] == "2"
    assert data["changed"] is True
    assert data["cleared"] is True
    assert data["driven"] == ["1", "2"]
    assert isinstance(data["project"]["id"], str) and data["project"]["id"]

    cfg = read_cfg(root)
    assert cfg["driven_plans"] == ["1", "2"]
    # Правка списка не переписала конфиг: остальные ключи на месте.
    assert cfg["project"] == "demo"
    for key in ("profiles", "runtimes", "paths"):
        assert key in cfg
    # Адресность: снят флаг плана 2, флаг плана 1 остался с прежней причиной.
    assert stop_path(root, "2").exists() is False
    flag = stop_path(root, "1")
    assert flag.exists()
    assert json.loads(flag.read_text(encoding="utf-8"))["reason"] == "стоп плана 1"


def test_stop_writes_owner_flag_for_named_plan(server):
    """`stop` на плане 2: owner-стоп с причиной, `driven_plans` не тронут.

    Здесь же две ветки признака «команда применена»: занятый тик даёт отказ с
    неизменным флагом, а повторный жест тем же телом — `200 stopped`, хотя файл
    переписан теми же байтами (`at` у движка секундный).
    """
    root = server["root"]
    flag_1 = stop_path(root, "1")
    before_1 = flag_1.read_bytes()
    before_2 = stop_path(root, "2").read_bytes()
    reason = "ждём решения владельца по разведке"

    # Занятый тик: движок выходит с кодом 0, но команду не применяет. Жест обязан
    # ответить отказом, а не выдать прежнюю причину из стоящего флага.
    lock = hold_tick(root)
    status, refused = post(server["url"], "/api/plan/stop",
                           {"plan": "2", "reason": reason}, PANEL)
    assert status == 400, refused
    assert refused["ok"] is False
    assert refused["status"] == "engine_error"
    assert "тик уже идёт" in refused["error"]
    assert "не применена" in refused["error"]
    assert stop_path(root, "2").read_bytes() == before_2
    lock.unlink()

    status, data = post(server["url"], "/api/plan/stop", {"plan": "2", "reason": reason}, PANEL)
    assert status == 200, data
    assert data["action"] == "plan_stop"
    assert data["status"] == "stopped"
    assert data["plan"] == "2"
    assert data["stop"]["reason"] == reason
    assert data["stop"]["kind"] == "owner"
    assert data["stop"]["source"] == "owner"
    assert data["stop"]["at"]

    literal = stop_path(root, "2")
    assert json.loads(literal.read_text(encoding="utf-8")) == data["stop"]
    # Каталог состояния читается в порядке движка: помощник обязан указать на тот же файл.
    assert plans_api.stop_flag(root, "2") == literal
    assert flag_1.read_bytes() == before_1
    assert read_cfg(root)["driven_plans"] == ["1"]

    # Двойное нажатие «остановить»: та же причина в ту же секунду переписывает флаг теми
    # же байтами, и жест обязан ответить 200 — признак «payload изменился» давал здесь 400.
    status, again = repeat_stop_same_bytes(server["url"], reason, data)
    assert again["ok"] is True
    assert again["status"] == "stopped"
    assert again["stop"]["reason"] == reason
    assert again["stop"]["kind"] == "owner"
    assert json.loads(literal.read_text(encoding="utf-8")) == again["stop"]
    assert flag_1.read_bytes() == before_1


def test_repeated_start_does_not_duplicate_number(server):
    """Второй `start` не дублирует номер и не переписывает файл.

    Плюс ветка отказа: при занятом тике `--resume` не применяется, и жест не должен
    записывать `driven_plans` поверх неудавшейся команды.
    """
    root = server["root"]
    before_cfg = (root / "pipeline.json").read_bytes()
    flag_2 = stop_path(root, "2")
    before_flag = flag_2.read_bytes()

    # Занятый тик: движок печатает «команда resume не применена» и выходит с кодом 0.
    lock = hold_tick(root)
    status, refused = post(server["url"], "/api/plan/start", {"plan": "2"}, PANEL)
    assert status == 400, refused
    assert refused["ok"] is False
    assert refused["status"] == "engine_error"
    assert "тик уже идёт" in refused["error"]
    assert "не применена" in refused["error"]
    assert (root / "pipeline.json").read_bytes() == before_cfg
    assert read_cfg(root)["driven_plans"] == ["1"]
    assert flag_2.read_bytes() == before_flag
    lock.unlink()

    status, first = post(server["url"], "/api/plan/start", {"plan": "2"}, PANEL)
    assert status == 200, first
    assert first["changed"] is True and first["cleared"] is True
    bytes_after_first = (root / "pipeline.json").read_bytes()

    status, second = post(server["url"], "/api/plan/start", {"plan": "2"}, PANEL)
    assert status == 200, second
    assert second["changed"] is False
    assert second["cleared"] is False
    assert (root / "pipeline.json").read_bytes() == bytes_after_first
    driven = read_cfg(root)["driven_plans"]
    assert driven == ["1", "2"]
    assert driven.count("2") == 1


def test_start_and_stop_in_preview_are_read_only(preview_server):
    """Предпросмотр: 403 на оба маршрута, ни одного созданного файла."""
    root = preview_server["root"]
    for path, payload in (("/api/plan/start", {"plan": "1"}),
                          ("/api/plan/stop", {"plan": "1", "reason": "нет"})):
        status, data = post(preview_server["url"], path, payload, PANEL)
        assert status == 403, (path, data)
        assert data["ok"] is False
        assert "предпросмотр только читает" in data["error"]
    assert (root / ".pipeline").exists() is False
    assert (root / "pipeline.json").exists() is False
    assert (root / "docs" / "plans" / "1-demo.md").is_file()
