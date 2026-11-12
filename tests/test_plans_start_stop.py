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
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from helpers import install_project
import ui_server
from step_pipeline import plans_api

PANEL = {"X-Pipeline-Panel": "1"}
STOP = Path(".pipeline") / "state" / "plan_{plan}_stop.json"

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
    """`stop` на плане 2: owner-стоп с причиной, `driven_plans` не тронут."""
    root = server["root"]
    flag_1 = stop_path(root, "1")
    before_1 = flag_1.read_bytes()
    reason = "ждём решения владельца по разведке"
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


def test_repeated_start_does_not_duplicate_number(server):
    """Второй `start` не дублирует номер и не переписывает файл."""
    root = server["root"]
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
