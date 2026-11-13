# -*- coding: utf-8 -*-
"""Добавление плана файлом из системного диалога: копия, отказы, ручной путь.

Контракт шага S4 плана 6: `POST /api/plan/add` открывает диалог выбора файла (или берёт
готовый `path` из тела), проверяет файл линтером плана и копирует его в каталог планов
проекта под именем `N-слаг.md`. Отказ с причиной — занятый номер (файл или след в
каталоге состояния), чужое имя (не `.md`) и файл не по формату. Копирование **не**
утверждает план: запись утверждения остаётся отдельным действием владельца (S3A).

Окно в тестах не поднимается: диалог подменяется через `plans_add._tk_pick`, маршрут
вызывает его без параметров. Тесты идут по проводу: настоящий `ThreadingHTTPServer`
на 127.0.0.1 и HTTP через `urllib`.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from helpers import install_project
import ui_server
from step_pipeline import plan_accept, plan_lint, plans_add

PANEL = {"X-Pipeline-Panel": "1"}

# Источник лежит ВНЕ проекта: его имя не обязано быть «номер-слаг.md», а скопированное —
# обязано. `FOREIGN` — чистый план по линтеру (legacy-формат фикстуры, без «Конституция:»).
FOREIGN = helpers.PLAN
MY = FOREIGN.replace("# 1. Демо-план конвейера", "# Мой план")
SECOND = FOREIGN.replace("# 1. Демо-план конвейера", "# 2. Второй план конвейера")
# Ни раздела шагов, ни журнала: линтер обязан отказать.
BROKEN = FOREIGN.split("## Шаги")[0]


@pytest.fixture()
def server(tmp_path: Path, monkeypatch):
    """Панель над установленным проектом: один план занят, каталоги состояния на месте.

    Окружение изолируется первым делом: `PIPELINE_STATE_DIR` ставит движок выше
    `paths.state`, и литеральный путь следа плана в тесте разошёлся бы с тем, который
    читает `plans_add`.
    """
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    root = install_project(tmp_path / "plans-add", board="uiboard")
    httpd = ui_server.make_server(root, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield {"url": url, "root": root, "httpd": httpd}
    finally:
        httpd.shutdown()
        httpd.server_close()


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


def plan_names(root: Path) -> list[str]:
    return sorted(path.name for path in (root / "docs" / "plans").glob("*.md"))


def source_file(tmp_path: Path, name: str, text: str) -> Path:
    """Файл плана вне проекта: его выбирает владелец в диалоге."""
    folder = tmp_path / "foreign"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_dialog_copies_plan_into_project(server, tmp_path, monkeypatch):
    """Диалог вернул файл — план скопирован под свободным номером, файл источника цел."""
    root = server["root"]
    source = source_file(tmp_path, "Мой план.md", MY)
    monkeypatch.setattr(plans_add, "_tk_pick", lambda title: str(source))

    code, data = post(server["url"], "/api/plan/add", {}, PANEL)

    assert code == 200, data
    assert data["ok"] is True
    assert data["action"] == "plan_add"
    assert data["status"] == "added"
    assert data["plan"] == "2"
    assert data["name"] == "2-Мой-план.md"
    assert data["file"] == "docs/plans/2-Мой-план.md"
    assert data["source"] == source.as_posix()
    assert data["title"] == "Мой план"
    # `id` проекта по умолчанию — слаг каталога (`proj.slug`), `name` — поле `project`
    # из `pipeline.json`: панель обслуживает именно этот проект, а не реестровый сосед.
    assert data["project"]["id"] == "plans-add"
    assert data["project"]["name"] == "demo"

    target = root / "docs" / "plans" / "2-Мой-план.md"
    assert target.is_file()
    assert target.read_bytes() == source.read_bytes()
    assert plan_accept.plan_number(target.name) == "2"
    assert plan_lint.lint_file(target, root, plan_lint.settings(root)) == []
    # Копирование не утверждает план: запись утверждения — жест владельца (S3A).
    assert not (root / ".pipeline" / "state" / "plan_2_approval.json").exists()
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    assert cfg["driven_plans"] == ["*"]


def test_occupied_number_refused(server, tmp_path):
    """Занятый номер — отказ с причиной; след в каталоге состояния номер тоже занимает."""
    root = server["root"]
    occupied = root / "docs" / "plans" / "2-second.md"
    occupied.write_text(SECOND, encoding="utf-8")
    source = source_file(tmp_path, "2-second.md", SECOND)

    code, data = post(server["url"], "/api/plan/add", {"path": str(source)}, PANEL)

    assert code == 400, data
    assert data["ok"] is False
    assert data["status"] == "occupied"
    assert data["plan"] == "2"
    assert "docs/plans/2-second.md" in data["error"]
    assert plan_names(root) == ["1-demo.md", "2-second.md"]
    assert occupied.read_text(encoding="utf-8") == SECOND

    # Номера 1, 2 заняты файлами, 3 — только стоп-флагом: следующий свободный — 4.
    state = root / ".pipeline" / "state"
    (state / "plan_3_stop.json").write_text("{}", encoding="utf-8")
    fresh = source_file(tmp_path, "новый.md", FOREIGN)

    code, data = post(server["url"], "/api/plan/add", {"path": str(fresh)}, PANEL)

    assert code == 200, data
    assert data["ok"] is True
    assert data["plan"] == "4"
    assert data["name"] == "4-новый.md"
    assert plan_names(root) == ["1-demo.md", "2-second.md", "4-новый.md"]
    assert not (root / "docs" / "plans" / "3-новый.md").exists()


def test_source_not_a_plan_refused(server, tmp_path):
    """Не `.md` и файл без раздела шагов — отказ с причиной, копии нет."""
    root = server["root"]
    plain = source_file(tmp_path, "план.txt", FOREIGN)

    code, data = post(server["url"], "/api/plan/add", {"path": str(plain)}, PANEL)

    assert code == 400, data
    assert data["ok"] is False
    assert data["status"] == "bad-file"
    assert ".md" in data["error"]

    broken = source_file(tmp_path, "без шагов.md", BROKEN)

    code, data = post(server["url"], "/api/plan/add", {"path": str(broken)}, PANEL)

    assert code == 400, data
    assert data["ok"] is False
    assert data["status"] == "format"
    assert len(data["errors"]) >= 1
    assert any("нет раздела со словом-маркером шагов" in line for line in data["errors"])
    assert plan_names(root) == ["1-demo.md"]


def test_no_window_offers_manual_path(server, tmp_path, monkeypatch):
    """Окно не поднялось или висит — ответ про ручной путь в пределах таймаута."""
    root = server["root"]
    calls: list[str] = []

    def slow_dialog(title: str) -> str:
        calls.append(title)
        time.sleep(2)
        return ""

    monkeypatch.setattr(plans_add, "DIALOG_TIMEOUT", 0.2)
    monkeypatch.setattr(plans_add, "_tk_pick", slow_dialog)
    started = time.monotonic()

    code, data = post(server["url"], "/api/plan/add", {}, PANEL)
    elapsed = time.monotonic() - started

    assert code == 400, data
    assert data["ok"] is False
    assert data["status"] == "manual"
    assert "введите путь руками" in data["message"]
    assert elapsed < 1.5, f"ответ ждал диалог {elapsed:.2f} с вместо таймаута 0.2 с"
    assert calls == [plans_add.TITLE]
    assert plan_names(root) == ["1-demo.md"]

    # Ручной путь — поле `path`: окно уже не поднимается, план добавляется.
    def quiet(title: str) -> str:
        calls.append(title)
        return ""

    monkeypatch.setattr(plans_add, "_tk_pick", quiet)
    calls.clear()
    source = source_file(tmp_path, "Готовый план.md", FOREIGN)

    code, data = post(server["url"], "/api/plan/add", {"path": str(source)}, PANEL)

    assert code == 200, data
    assert data["ok"] is True
    assert data["status"] == "added"
    assert data["name"] == "2-Готовый-план.md"
    assert calls == [], "окно не должно подниматься, когда путь пришёл в теле"
