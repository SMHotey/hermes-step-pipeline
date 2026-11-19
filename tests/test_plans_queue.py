# -*- coding: utf-8 -*-
"""Очередь планов на вкладке «Планы» (план 15, шаг S4).

Порядок, пометка «следующий» и счётчик ведомых считаются в `/api/projects`: страница
ничего не сортирует и не досчитывает. Сторож держит оба конца — ключ `plan_queue` в
живой строке реестра и текст страницы, который этот ключ печатает. Движка JS в проекте
нет и в этом шаге не появляется: ни node, ни jsdom, ни playwright.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402,F401  (загружает плагин и алиасы модулей)
from helpers import install_project  # noqa: E402
import panel_pause  # noqa: E402
import snapshot  # noqa: E402
import ui_server  # noqa: E402

PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"

OPEN_PLAN_TEMPLATE = """# {number}. Открытый план {number}

Цель: план с открытым шагом для очереди панели.

## Шаги

### S1. Единственный шаг (1 дн)

- Файл: `app/mod{number}.py`
- Готово = шаг на месте

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

CLOSED_PLAN_TEMPLATE = """# {number}. Закрытый план

Цель: план, у которого все шаги закрыты.

## Шаги

### S1. Единственный шаг (1 дн)

- Файл: `app/mod{number}.py`
- Готово = шаг на месте

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| 12.11.2026 | S1 | abc1234 | 4 passed | ЗАКРЫТ |
"""


@pytest.fixture(autouse=True)
def clean_cache():
    """Кэши снимка и планов — состояние процесса: между тестами их обнуляем."""
    snapshot.clear_cache()
    panel_pause.clear_cache()
    yield
    snapshot.clear_cache()
    panel_pause.clear_cache()


def page() -> str:
    """Текст страницы панели: единственный источник разметки блока."""
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    """Кусок текста от `start` до ближайшего `end` после него."""
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def running(root: Path, **kwargs):
    """Сервер в потоке: очередь проверяется на своём реестре и своём конфиге."""
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


def write_plan(root: Path, number: int, closed: bool = False) -> Path:
    """Файл плана с номером в имени и заголовке: журнал открыт или закрыт."""
    template = CLOSED_PLAN_TEMPLATE if closed else OPEN_PLAN_TEMPLATE
    path = root / "docs" / "plans" / f"{number}-extra.md"
    path.write_text(template.format(number=number), encoding="utf-8")
    return path


def driven(root: Path, ids: list[str]) -> None:
    """`driven_plans` проекта: здесь свой конфиг в tmp_path, конфиг владельца не трогается."""
    path = root / "pipeline.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["driven_plans"] = ids
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def write_registry(home: Path, entries: list[dict]) -> Path:
    """Реестр в home профиля: панель читает его, но никогда не пишет (D4)."""
    path = home / "projects.json"
    path.write_text(json.dumps({"projects": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def registry(root: Path, project: str = "alpha") -> list[dict]:
    return [{"id": project, "name": "Альфа", "root": str(root)}]


def only_row(data: dict, project: str) -> dict:
    return {row["id"]: row for row in data["projects"]}[project]


def test_queue_is_numeric_ascending_not_config_order(tmp_path: Path, hermes_stub: Path,
                                                    isolated_hermes_home: Path):
    """Очередь идёт по номеру, а не по порядку `driven_plans`; вне ведения — не попадает."""
    alpha = install_project(tmp_path / "alpha", board="queueboard")
    write_plan(alpha, 4)                    # план вне ведения
    write_plan(alpha, 6, closed=True)       # закрытый, но всё ещё ведомый
    write_plan(alpha, 8)
    write_plan(alpha, 13)
    driven(alpha, ["13", "6", "8"])         # вразнобой — так пишет владелец
    write_registry(isolated_hermes_home, registry(alpha))
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    row = only_row(data, "alpha")
    assert row["plan_queue"] == {"ids": ["6", "8", "13"], "next_id": "8", "count": 3}
    # Все пять планов проекта в ответе есть: очередь сузилась составом ведения, а не разбором.
    assert {plan["id"] for plan in row["plans"]} == {"1", "4", "6", "8", "13"}


def test_star_drives_every_plan_in_number_order(tmp_path: Path, hermes_stub: Path,
                                                isolated_hermes_home: Path):
    """`driven_plans: ["*"]` — ведомы все распознанные планы; у строки плана нет лишних полей."""
    alpha = install_project(tmp_path / "alpha", board="queueboard")
    write_plan(alpha, 2)
    write_plan(alpha, 10)                   # строка «10» стоит после «2», а не перед «1»
    write_registry(isolated_hermes_home, registry(alpha))
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    row = only_row(data, "alpha")
    assert row["plan_queue"] == {"ids": ["1", "2", "10"], "next_id": "1", "count": 3}
    # Очередь — отдельный ключ сводки: строка плана осталась тринадцатипольной (Ф5).
    assert set(row["plans"][0]) == set(ui_server.PLAN_ROW_FIELDS)


def test_all_closed_queue_has_no_next(tmp_path: Path, hermes_stub: Path,
                                      isolated_hermes_home: Path):
    """Единственный ведомый план закрыт: список есть, «следующего» нет."""
    alpha = install_project(tmp_path / "alpha", board="queueboard")
    write_plan(alpha, 6, closed=True)
    driven(alpha, ["6"])
    write_registry(isolated_hermes_home, registry(alpha))
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    row = only_row(data, "alpha")
    assert row["plan_queue"] == {"ids": ["6"], "next_id": "", "count": 1}
    # Литерал проверяется только в теле функции: на экране «Шаг» он уже есть.
    queue = block(page(), "function renderPlansQueue", "function renderPlansTab")
    assert "все закрыты" in queue


def test_project_without_engine_has_empty_queue(tmp_path: Path, hermes_stub: Path,
                                                isolated_hermes_home: Path):
    """Проект без `.pipeline/` отдаёт пустую очередь, и ключ есть у каждой строки реестра."""
    alpha = install_project(tmp_path / "alpha", board="queueboard")
    bare = tmp_path / "bare"
    bare.mkdir()
    write_registry(isolated_hermes_home,
                   [{"id": "bare", "name": "Голый", "root": str(bare)},
                    {"id": "alpha", "name": "Альфа", "root": str(alpha)}])
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    rows = {row["id"]: row for row in data["projects"]}
    assert rows["bare"]["installed"] is False
    assert rows["bare"]["plan_queue"] == {"ids": [], "next_id": "", "count": 0}
    for row in data["projects"]:
        assert set(row["plan_queue"]) == {"ids", "next_id", "count"}
    assert rows["alpha"]["plan_queue"]["ids"] == ["1"]


def test_page_draws_queue_from_answer_without_recomputing():
    """Страница печатает готовые `ids`/`next_id`/`count`; сортировки и счёта в ней нет."""
    html = page()
    assert html.count('id="plans-queue"') == 1
    queue = block(html, "function renderPlansQueue", "function renderPlansTab")
    for needle in ("plan_queue.ids", "plan_queue.next_id", "plan_queue.count", " → ",
                   "esc(", "следующий", "ведомых планов нет"):
        assert needle in queue, needle
    assert queue.count("ведётся") == 1
    for forbidden in (".sort(", ".length", "parseInt", "driven", '"closed"'):
        assert forbidden not in queue, forbidden
    tab = block(html, "function renderPlansTab", "function planCard")
    assert "renderPlansQueue(" in tab
