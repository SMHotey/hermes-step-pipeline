# -*- coding: utf-8 -*-
"""Кнопки действий в строке плана и форма «Добавить план» (план 6 S5A).

Пять тестов фиксируют контракт шага: таблица «вид движка → вид панели» знает все восемь
видов строки и три жеста владельца, кнопок в предпросмотре нет, «Добавить план» зовёт
`/api/plan/add`, каждая кнопка ячейки закрыта своим тегом.

Текст страницы читается потому, что движка JS в проекте нет и в этом шаге не появляется
(ни node, ни jsdom, ни playwright) — тот же приём, что у S5. Живая половина (второй и
третий тест) идёт по проводу: настоящий `ThreadingHTTPServer` на 127.0.0.1 и HTTP через
`urllib`, чтобы адреса маршрутов и тела запросов проверялись, а не описывались.
"""
from __future__ import annotations

import json
import re
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
from step_pipeline import plans_add  # noqa: E402

PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"
PANEL = {"X-Pipeline-Panel": "1"}

PLAN2_TEMPLATE = """# 2. {title}

Цель: второй план проекта для кнопок вкладки «Планы».

## Шаги

### S1. Первый шаг (1 дн)

- Файл: `app/a.py`
- Готово = файл `app/a.py` есть

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
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
    """Текст страницы панели: единственный источник разметки вкладки."""
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    """Кусок текста от `start` до ближайшего `end` после него."""
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def write_registry(home: Path, entries: list[dict]) -> Path:
    """Реестр в home профиля: панель читает его, но никогда не пишет (D4)."""
    path = home / "projects.json"
    path.write_text(json.dumps({"projects": entries}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def add_plan(root: Path, title: str) -> Path:
    """Второй файл плана: номер в имени, заголовок свой."""
    path = root / "docs" / "plans" / "2-extra.md"
    path.write_text(PLAN2_TEMPLATE.format(title=title), encoding="utf-8")
    return path


def set_driven(root: Path, plans: list[str]) -> None:
    """Ведомые планы проекта: ключ владельца, шаг его только читает."""
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["driven_plans"] = plans
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def stop_file(root: Path, plan: str) -> Path:
    return root / ".pipeline" / "state" / f"plan_{plan}_stop.json"


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


def post(url: str, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    """POST с JSON-телом; ошибки HTTP тоже разбираются, а не бросаются."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{path}", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def test_eight_engine_kinds_and_three_panel_kinds_are_in_the_tables():
    """Восемь видов движка и три вида панели — в двух таблицах, без команд в тексте страницы."""
    html = page()
    engine = block(html, "const ENGINE_ACTIONS = {", "\n};")
    for kind in ("resume", "approve", "drive", "unblock", "unhold", "tick", "cron", "none"):
        assert f"  {kind}:" in engine, kind
    assert "без действия" in engine
    # Виды, у которых кнопки нет, тоже перечислены: «без действия» — вид, а не молчание.
    assert "resume:  {label: \"Продолжить\"" in engine
    assert "unblock: {label: \"Разблокировать карточку\"" in engine
    assert "cron:    {label: \"Включить джобу стража\"" in engine
    assert "tick:    {label: \"Тик\"" in engine

    panel = block(html, "const PANEL_ACTIONS = {", "\n};")
    for kind in ("start", "stop", "delete"):
        assert f"  {kind}:" in panel, kind
    assert "endpoint" in panel and "when:" in panel

    # Команду страница не собирает: своего `hermes kanban`, `gate.py` и `python .pipeline` нет.
    assert html.count("hermes kanban") == 0
    assert html.count("gate.py") == 0
    assert html.count("python .pipeline") == 0
    assert "plan.action.command" in html


def test_panel_kinds_post_the_row_address_to_their_route(
        tmp_path: Path, monkeypatch, hermes_stub: Path, isolated_hermes_home: Path):
    """Три жеста панели бьют по адресу строки: start, stop и delete в названный проект."""
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    monkeypatch.setenv("PIPELINE_LOCK_WAIT", "0")
    html = page()
    body = block(html, "function bindPlansActions(", "// Подключение проекта из панели")
    assert 'closest("[data-plan-act], [data-plan-panel]")' in body
    assert "dataset.planId" in body
    # Удаление — единственный жест с подтверждением: оно необратимо.
    assert "confirm(" in body
    assert html.count("/api/plan/start") == 1
    assert html.count("/api/plan/stop") == 1
    assert html.count("/api/plan/delete") == 1

    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    set_driven(beta, ["1"])
    plan2 = add_plan(beta, "Второй план беты")
    write_registry(isolated_hermes_home,
                   [{"id": "alpha", "name": "Альфа", "root": str(alpha)},
                    {"id": "beta", "name": "Бета", "root": str(beta)}])
    with running(alpha) as (url, _httpd):
        status, started = post(url, "/api/plan/start",
                               {"project": "beta", "plan": "2"}, PANEL)
        assert status == 200 and started["ok"] is True, started
        assert started["project"]["id"] == "beta"
        driven = json.loads((beta / "pipeline.json").read_text(encoding="utf-8"))["driven_plans"]
        assert "2" in [str(item) for item in driven]

        status, stopped = post(url, "/api/plan/stop",
                               {"project": "alpha", "plan": "1", "reason": "тест"}, PANEL)
        assert status == 200 and stopped["ok"] is True, stopped
        assert stop_file(alpha, "1").is_file()
        # Адресность: стоп альфы не поставил флаг бете.
        assert not stop_file(beta, "1").exists()

        status, deleted = post(url, "/api/plan/delete",
                               {"project": "beta", "plan": "2"}, PANEL)
        assert status == 200 and deleted["ok"] is True, deleted
        assert not plan2.exists()


def test_add_plan_form_calls_plan_add_and_keeps_the_server_error(
        tmp_path: Path, monkeypatch, hermes_stub: Path, isolated_hermes_home: Path):
    """«Добавить план» зовёт `plan_add`; отмена и ручной путь — текст, а не гадание."""
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.setenv("PIPELINE_LOCK_WAIT", "0")
    html = page()
    panel = block(html, 'id="tab-plans"', "</table>")
    assert 'select id="plans-add-project"' in panel
    assert 'input type="text" id="plans-add-path"' in panel
    assert 'autocomplete="off"' in panel
    assert 'placeholder="пусто — откроется окно выбора файла"' in panel
    assert 'id="plans-add-btn"' in panel
    # Подпись у кнопки — неразрывный пробел: `label` в строке формы, а не украшение.
    assert "<label>&nbsp;</label>" in panel
    assert html.count("Добавить план") == 1

    body = block(html, "function planAdd(", "function bindPlansActions(")
    for needle in ('/api/plan/add', "PANEL_HEADERS",
                   'JSON.stringify({project: project || "", path: path || ""})',
                   '"cancelled"', '"manual"', "data.errors",
                   '$("plans-add-path").value = ""', "refresh()"):
        assert needle in body, needle

    # Форма живёт в блоке вкладки: её поля пересобирает рендер, а не вторая сборка.
    render = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    assert "plans-add-project" in render
    assert "addPicker.dataset.ids" in render
    # В предпросмотре формы нет: у `.row` свой `display: flex`, и атрибут `hidden` не работает.
    assert '$("plans-add").style.display = data.preview ? "none" : "";' in render
    assert '$("plans-add").hidden' not in html

    bindings = block(html, '$("plans-add-btn").onclick', "bindPlansActions();")
    assert "plans-add-path" in bindings
    assert 'event.key === "Enter"' in bindings

    demo = install_project(tmp_path / "demo", board="uiboard")
    source = tmp_path / "чужой-план.md"
    source.write_text(helpers.PLAN.replace("# 1. Демо-план конвейера", "# 5. Готовый план"),
                      encoding="utf-8")
    with running(demo) as (url, _httpd):
        status, added = post(url, "/api/plan/add",
                             {"project": "demo", "path": str(source)}, PANEL)
        assert status == 200 and added["ok"] is True, added
        assert added["status"] == "added"
        assert (demo / "docs" / "plans" / f"{added['plan']}-чужой-план.md").is_file()

        monkeypatch.setattr(plans_add, "_tk_pick", lambda title="": "")
        status, cancelled = post(url, "/api/plan/add",
                                 {"project": "demo", "path": ""}, PANEL)
        assert status == 400 and cancelled["ok"] is False
        assert cancelled["status"] == "cancelled"
        assert "отменён" in cancelled["error"]


def test_actions_cell_balances_tags_and_takes_preview_first():
    """Ячейка кнопок: каждый открытый тег закрыт, приоритет у предпросмотра, восемь видов."""
    html = page()
    body = block(html, "function planActionsCell(", "function planAdd(")
    for pair in (("<button", "</button>"), ("<span", "</span>"), ("<td", "</td>")):
        assert body.count(pair[0]) == body.count(pair[1]), pair
    # Предпросмотр решается до сборки кнопок: иначе `data-plan-act` успел бы появиться.
    assert body.index("if (preview)") < body.index("data-plan-act")
    assert 'data-plan-panel' in body
    assert "ENGINE_ACTIONS[action.kind]" in body
    # Виды панели рисуются поверх строки, кроме `start` — он уже покрыт видами движка.
    assert 'if (kind === "start") return;' in body
    assert 'item.when === "running" && plan.state !== "running"' in body

    # Девятая ячейка подключена рендером: без вызова разметка колонки пустая.
    render = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    assert "data-plan-id" in render
    assert "${planActionsCell(project, plan, Boolean(data.preview))}" in render
    assert 'colspan="9"' in render
    assert "plansRows = rows;" in render


def test_preview_draws_rows_without_any_button(tmp_path: Path):
    """Предпросмотр рисует строки без кнопок: ячейка — прочерк, форма скрыта."""
    html = page()
    cell = block(html, "function planActionsCell(", "function planAdd(")
    # Ветка предпросмотра — прочерк в девятой ячейке, до всякой сборки кнопок.
    assert "if (preview) return '<td class=\"mono\">—</td>';" in cell

    bare = tmp_path / "bare"
    (bare / "docs" / "plans").mkdir(parents=True)
    (bare / "docs" / "plans" / "1-demo.md").write_text(helpers.PLAN, encoding="utf-8")
    with running(bare, preview=True) as (url, _httpd):
        status, data = post(url, "/api/plan/add", {"path": ""}, PANEL)
    # Второй рубеж: даже если кнопка появится, сервер откажет — предпросмотр только читает.
    assert status == 403 and data["ok"] is False
    assert "предпросмотр только читает" in data["error"]
    assert re.search(r"data\.preview", html)
