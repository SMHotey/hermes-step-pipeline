# -*- coding: utf-8 -*-
"""«Обзор»: только планы в работе, отдельная строка на план (план 6 S6).

Сторож читает текст страницы и живой ответ `/api/projects`. Движка JS в проекте нет и
в этом шаге не появляется: ни node, ни jsdom, ни playwright — как у S5 и S5A.
"""
from __future__ import annotations

import json
import sys
import threading
import time
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

PLAN2_TEMPLATE = """# 2. {title}

Цель: второй план проекта для блока «Планы в работе».

## Шаги
{steps}
## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

CLOSED_PLAN_TEMPLATE = """# {number}. Закрытый план

Цель: план, у которого все шаги закрыты.

## Шаги

### S1. Единственный шаг (1 дн)

- Файл: `app/mod1.py`
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


def second_plan(title: str, steps: int) -> str:
    """Второй план проекта: свой заголовок и своё число шагов (форма — `helpers.PLAN`)."""
    body = "".join(
        f"\n### S{i}. Шаг {i} плана (1 дн)\n\n"
        f"- Файл: `app/mod{i}.py`\n"
        f"- Готово = строка {i} на месте\n"
        for i in range(1, steps + 1))
    return PLAN2_TEMPLATE.format(title=title, steps=body)


def add_plan(root: Path, title: str, steps: int, number: int = 2) -> Path:
    """Положить в проект второй файл плана: номер в имени, заголовок и число шагов свои."""
    path = root / "docs" / "plans" / f"{number}-extra.md"
    path.write_text(second_plan(title, steps), encoding="utf-8")
    return path


def closed_plan(root: Path, number: int = 3) -> Path:
    """План, у которого единственный шаг закрыт: журнал читает последнюю колонку."""
    path = root / "docs" / "plans" / f"{number}-closed.md"
    path.write_text(CLOSED_PLAN_TEMPLATE.format(number=number), encoding="utf-8")
    return path


def write_registry(home: Path, entries: list[dict]) -> Path:
    """Реестр в home профиля: панель читает его, но никогда не пишет (D4)."""
    path = home / "projects.json"
    path.write_text(json.dumps({"projects": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def put_stop(root: Path, plan_id: str, reason: str) -> Path:
    """Стоп-флаг плана: литеральный путь и литеральное тело, а не помощник API."""
    path = root / ".pipeline" / "state" / f"plan_{plan_id}_stop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": "owner", "source": "owner", "reason": reason,
                                "at": "12.11.2026 10:00:00"}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def write_idle(root: Path, plan_id: str, seconds: int = 3900) -> Path:
    """Простой плана: `waiting_since` пишет сторож, панель только читает (правило одно)."""
    path = root / ".pipeline" / "state" / f"plan_{plan_id}_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"waiting_since": time.time() - seconds}), encoding="utf-8")
    return path


def test_three_running_rows_on_two_projects(tmp_path: Path, hermes_stub: Path,
                                            isolated_hermes_home: Path):
    """Два плана одного проекта и один соседнего — три строки `running` с простоем."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    add_plan(alpha, "Второй план в работе", 3)
    write_idle(alpha, "1")
    write_idle(alpha, "2")
    beta = install_project(tmp_path / "beta", board="uiboard")
    add_plan(beta, "План под стопом", 2)
    put_stop(beta, "2", "стоп по бете")
    closed_plan(beta, 3)
    write_idle(beta, "1")
    write_registry(isolated_hermes_home,
                   [{"id": "beta", "name": "Бета", "root": str(beta)}])
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    rows = {row["id"]: row for row in data["projects"]}
    pairs = [(row["id"], plan["id"]) for row in data["projects"] for plan in row["running"]]
    assert pairs == [("alpha", "1"), ("alpha", "2"), ("beta", "1")]
    # Один источник строк: `running` — фильтр по `plans`, а не второй сборщик.
    for row in data["projects"]:
        assert row["running"] == [plan for plan in row["plans"] if plan["state"] == "running"]
    expected_fields = tuple(sorted(ui_server.PLAN_ROW_FIELDS))
    for row in data["projects"]:
        for plan in row["running"]:
            assert tuple(sorted(plan)) == expected_fields
            assert plan["state"] == "running"
            assert plan["driven"] is True
            assert isinstance(plan["idle"], dict) and plan["idle"]["text"]
            assert plan["next"] == "S1"
    beta_states = {plan["id"]: plan["state"] for plan in rows["beta"]["plans"]}
    assert beta_states == {"1": "running", "2": "stopped", "3": "closed"}


def test_not_driven_plan_is_neither_running_nor_bars(tmp_path: Path, hermes_stub: Path,
                                                     isolated_hermes_home: Path):
    """План без номера в `driven_plans`: строка «не ведётся», в `running` и полосах его нет."""
    beta = install_project(tmp_path / "beta", board="uiboard")
    cfg = json.loads((beta / "pipeline.json").read_text(encoding="utf-8"))
    cfg["driven_plans"] = ["1"]
    (beta / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    add_plan(beta, "Снятый с ведения", 2)
    write_idle(beta, "1")
    with running(beta) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    row = data["projects"][0]
    plans = {plan["id"]: plan for plan in row["plans"]}
    assert plans["2"]["state"] == "not-driven"
    assert plans["2"]["state_label"] == "не ведётся"
    assert [plan["id"] for plan in row["running"]] == ["1"]

    snapshot.clear_cache()
    progress = snapshot.snapshot(beta, plan="1")["overview"]["progress"]
    assert [item["id"] for item in progress] == ["1"]

    bars = block(page(), "const rows = ov.progress", "планов с журналом")
    assert "driven" not in bars, "фильтр по ведению живёт в снимке, а не на странице"
    assert ".filter(" not in bars


def test_empty_registry_says_no_plans_in_work(tmp_path: Path, hermes_stub: Path,
                                             isolated_hermes_home: Path):
    """Каталог без конвейера: ключ `running` пуст, страница говорит «планов в работе нет»."""
    bare = tmp_path / "bare"
    bare.mkdir()
    with running(bare) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    row = data["projects"][0]
    assert row["installed"] is False
    assert row["plans"] == []
    assert row["running"] == []

    html = page()
    assert html.count("планов в работе нет") == 1
    body = block(html, "function renderRunning", "function renderOverview")
    assert "планов в работе нет" in body
    assert "esc(" in body, "значения строк проходят через esc()"


def test_running_key_exists_on_every_project_row_and_page_calls_render(
        tmp_path: Path, hermes_stub: Path, isolated_hermes_home: Path):
    """Ранние выходы отдают `running: []`, а рендер висит на том же кадре, без второго запроса."""
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    add_plan(alpha, "Второй план", 2)
    put_stop(alpha, "2", "стоп по альфе")
    bare = tmp_path / "bare"
    bare.mkdir()
    empty = install_project(tmp_path / "empty", board="uiboard")
    (empty / "docs" / "plans" / "1-demo.md").unlink()
    broken = install_project(tmp_path / "broken", board="uiboard")
    (broken / ".pipeline" / "gate.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    write_registry(isolated_hermes_home,
                   [{"id": "bare", "name": "Голый", "root": str(bare)},
                    {"id": "empty", "name": "Пустой", "root": str(empty)},
                    {"id": "broken", "name": "Сломанный", "root": str(broken)}])
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    rows = {row["id"]: row for row in data["projects"]}
    for row in data["projects"]:
        assert "running" in row and isinstance(row["running"], list)
    for name in ("bare", "empty", "broken"):
        assert rows[name]["running"] == []
    assert "gate.py вышел с 3" in rows["broken"]["error"]
    assert [plan["id"] for plan in rows["alpha"]["running"]] == ["1"]

    html = page()
    load = block(html, "async function loadProjects", "\n}\n")
    assert "renderRunning(data)" in load
    body = block(html, "function renderRunning", "function renderOverview")
    assert "running" in body
    assert "api(" not in body and "fetch(" not in body, "второго запроса рендер не делает"
