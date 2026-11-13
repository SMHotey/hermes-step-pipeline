# -*- coding: utf-8 -*-
"""Кнопки действий в строке плана и форма «Добавить план» (план 6 S5A).

Сторож читает текст страницы (`step_pipeline/ui/index.html`) и живой ответ настоящего
`ThreadingHTTPServer`. Движка JS в проекте нет и в этом шаге не появляется: ни node,
ни jsdom, ни playwright.

Контракт, который держат тесты: вид действия приходит от движка (`plan.action.kind`),
команда — только из `plan.action.command`, подписи кнопок — из таблицы соответствия.
Страница не собирает команды и не решает, что доступно.

Таблицы `ENGINE_ACTIONS` и `PANEL_ACTIONS` разбираются построчно: формат «одна запись на
строку» — часть контракта, поэтому ассерты сверяют значения (подпись целиком, адрес
целиком, канал), а не факт присутствия ключа.
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

# Восемь видов действия движка: порядок таблицы, подписи и канал панели (§4.2 постановки).
ENGINE_KINDS = ["resume", "approve", "drive", "unblock", "unhold", "tick", "cron", "none"]
ENGINE_LABELS = {
    "resume": "Продолжить",
    "approve": "Утвердить",
    "drive": "Запустить",
    "unblock": "Разблокировать карточку",
    "unhold": "Снять ручной допуск",
    "tick": "Тик",
    "cron": "Включить джобу стража",
    "none": "без действия",
}
# Три жеста владельца поверх тех же полей строки.
PANEL_KINDS = ["start", "stop", "delete"]
PANEL_LABELS = {"start": "Запустить", "stop": "Остановить после шага", "delete": "Удалить"}
PANEL_ENDPOINTS = {"start": "/api/plan/start", "stop": "/api/plan/stop",
                   "delete": "/api/plan/delete"}

PLAN2_TEMPLATE = """# 2. {title}

Цель: второй план проекта для вкладки «Планы».

## Шаги

### S1. Шаг 1 плана (1 дн)

- Файл: `app/mod1.py`
- Готово = строка 1 на месте

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

# Запись таблицы соответствия: ровно одна на строку, хвостовая запятая допустима.
ENTRY_RE = re.compile(r"^  ([a-z]+):\s+\{.*\},?$")


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


def entries(text: str, header: str) -> list[tuple[str, str]]:
    """Записи таблицы соответствия: список пар «вид → строка таблицы», в порядке файла."""
    body = block(text, header, "\n};")
    out: list[tuple[str, str]] = []
    for line in body.splitlines()[1:]:
        match = ENTRY_RE.match(line)
        assert match, f"строка таблицы не разобрана (формат — контракт): {line!r}"
        out.append((match.group(1), line))
    return out


def get(url: str) -> tuple[int, dict]:
    """GET без тела: ошибки HTTP тоже разбираются, а не бросаются."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def post(url: str, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    """POST с JSON-телом по переданному адресу; метка панели — по умолчанию."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}{path}", data=data,
                                     headers={"Content-Type": "application/json",
                                              **(PANEL if headers is None else headers)})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
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


def add_plan(root: Path, title: str, number: int = 2) -> Path:
    """Положить в проект второй файл плана: номер в имени, заголовок свой."""
    path = root / "docs" / "plans" / f"{number}-extra.md"
    path.write_text(PLAN2_TEMPLATE.format(title=title), encoding="utf-8")
    return path


def set_driven(root: Path, plans: list[str]) -> None:
    """Ведомые планы проекта: ключ владельца, тест его только читает."""
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["driven_plans"] = plans
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def write_registry(home: Path, entries_: list[dict]) -> Path:
    """Реестр в home профиля: панель читает его, но никогда не пишет (D4)."""
    path = home / "projects.json"
    path.write_text(json.dumps({"projects": entries_}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def stop_file(root: Path, plan: str) -> Path:
    return root / ".pipeline" / "state" / f"plan_{plan}_stop.json"


def test_engine_kinds_map_to_labels_and_channels():
    """Восемь видов движка: подписи дословно, у каждого свой канал, команд в тексте нет."""
    html = page()
    table = entries(html, "const ENGINE_ACTIONS = {")
    # Восемь записей в порядке приоритета движка — девятый вид не рисуется молча.
    assert [kind for kind, _ in table] == ENGINE_KINDS
    assert len(table) == 8
    lines = dict(table)
    for kind in ENGINE_KINDS:
        assert f'label: "{ENGINE_LABELS[kind]}"' in lines[kind], kind

    # Каналы: start (per-plan resume) у resume и drive, черновик у approve,
    # текст команды у unblock и cron, поля строки — у unhold и tick.
    assert "planRowCommand(\"start\", project, plan)" in lines["resume"]
    assert "planRowApprove(project, plan)" in lines["approve"]
    assert "planRowCommand(\"start\", project, plan)" in lines["drive"]
    assert "planRowCommandText(plan)" in lines["unblock"]
    assert 'act("unhold"' in lines["unhold"]
    assert "step: plan.next" in lines["unhold"]
    assert 'act("tick"' in lines["tick"]
    assert "planRowCommandText(plan)" in lines["cron"]
    # `none` — вид без кнопки: канала нет вовсе, а не пустая функция.
    assert "run:" not in lines["none"]
    # Подписи у resume и drive разные, хотя адрес один: «Продолжить» ≠ «Запустить».
    assert ENGINE_LABELS["resume"] != ENGINE_LABELS["drive"]

    # Источник вида — поле движка в ячейке и в обработчике, а не текст страницы.
    cell = block(html, "function planActionsCell(", "function planAdd(")
    for needle in ("ENGINE_ACTIONS[action.kind]", "plan.action || {}", "action.command",
                   "esc(action.kind", "clip bad"):
        assert needle in cell, needle
    bind = block(html, "function bindPlansActions(", "// Подключение проекта из панели")
    assert "ENGINE_ACTIONS[button.dataset.planAct]" in bind

    # Команду страница не собирает: своих `hermes kanban`, `gate.py`, `python .pipeline` нет.
    assert html.count("hermes kanban") == 0
    assert html.count("gate.py") == 0
    assert html.count("python .pipeline") == 0


def test_panel_kinds_hit_their_routes_and_rules(
        tmp_path: Path, monkeypatch, hermes_stub: Path, isolated_hermes_home: Path):
    """Три жеста панели: свой адрес, своё правило «когда рисовать», тело из строки."""
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    monkeypatch.setenv("PIPELINE_LOCK_WAIT", "0")
    html = page()
    table = entries(html, "const PANEL_ACTIONS = {")
    assert [kind for kind, _ in table] == PANEL_KINDS
    assert len(table) == 3
    lines = dict(table)
    for kind in PANEL_KINDS:
        assert f'label: "{PANEL_LABELS[kind]}"' in lines[kind], kind
        assert f'endpoint: "{PANEL_ENDPOINTS[kind]}"' in lines[kind], kind
        # Один адрес — одно место в странице: копия рядом означала бы второй источник.
        assert html.count(PANEL_ENDPOINTS[kind]) == 1, kind
    assert 'when: "running"' in lines["stop"]
    assert 'when: "any"' in lines["delete"]
    assert "when:" not in lines["start"]

    command = block(html, "function planRowCommand(", "function planRowApprove(")
    for needle in ("item.endpoint", "PANEL_HEADERS",
                   "JSON.stringify({project: project.id, plan: plan.id})"):
        assert needle in command, needle
    bind = block(html, "function bindPlansActions(", "// Подключение проекта из панели")
    for needle in ('closest("[data-plan-act], [data-plan-panel]")', "dataset.planId", "confirm("):
        assert needle in bind, needle

    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    set_driven(beta, ["1"])
    plan2 = add_plan(beta, "Второй план беты")
    write_registry(isolated_hermes_home,
                   [{"id": "alpha", "name": "Альфа", "root": str(alpha)},
                    {"id": "beta", "name": "Бета", "root": str(beta)}])
    with running(alpha) as (url, _httpd):
        status, started = post(url, "/api/plan/start", {"project": "beta", "plan": "2"})
        assert status == 200 and started["ok"] is True, started
        assert started["project"]["id"] == "beta"
        driven = json.loads((beta / "pipeline.json").read_text(encoding="utf-8"))["driven_plans"]
        assert [str(item) for item in driven] == ["1", "2"]

        status, stopped = post(url, "/api/plan/stop", {"project": "alpha", "plan": "1"})
        assert status == 200 and stopped["ok"] is True, stopped
        assert stop_file(alpha, "1").is_file()
        # Адресность: стоп альфы не поставил флаг бете.
        assert not stop_file(beta, "1").exists()

        status, deleted = post(url, "/api/plan/delete", {"project": "beta", "plan": "2"})
        assert status == 200 and deleted["ok"] is True, deleted
        assert not plan2.exists()


def test_add_plan_form_posts_plan_add_and_reports_statuses(
        tmp_path: Path, monkeypatch, hermes_stub: Path, isolated_hermes_home: Path):
    """Форма «Добавить план»: четыре исхода одним отчётом, путь и проект — из полей."""
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    monkeypatch.setenv("PIPELINE_LOCK_WAIT", "0")
    html = page()
    panel = block(html, 'id="tab-plans"', "</table>")
    assert '<div class="row" id="plans-add">' in panel
    assert '<label for="plans-add-project">Проект</label>' in panel
    assert '<select id="plans-add-project">' in panel
    assert '<option value="">проект панели</option>' in panel
    assert '<label for="plans-add-path">Файл плана</label>' in panel
    assert '<input type="text" id="plans-add-path" autocomplete="off"' in panel
    assert 'placeholder="пусто — откроется окно выбора файла"' in panel
    # Кнопка не отправляет форму: перезагрузки страницы нет.
    assert '<button type="button" id="plans-add-btn" class="primary">Добавить план</button>' in panel
    assert '<div class="clip" id="plans-status"></div>' in panel
    assert html.count("Добавить план") == 1

    body = block(html, "function planAdd(", "function bindPlansActions(")
    for needle in ("/api/plan/add", "PANEL_HEADERS",
                   'JSON.stringify({project: project || "", path: path || ""})',
                   '"cancelled"', '"manual"', "data.errors",
                   '$("plans-add-path").value = ""', "refresh()"):
        assert needle in body, needle

    render = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    assert "plans-add-project" in render
    assert "addPicker.dataset.ids" in render
    # В предпросмотре формы нет: у `.row` свой `display: flex`, атрибут `hidden` не прячет.
    assert '$("plans-add").style.display = data.preview ? "none" : "";' in render
    assert '$("plans-add").hidden' not in html

    bindings = block(html, '$("plans-add-btn").onclick', "bindPlansActions();")
    assert '$("plans-add-project").value' in bindings
    assert '$("plans-add-path").value' in bindings
    assert 'event.key === "Enter"' in bindings

    demo = install_project(tmp_path / "demo", board="uiboard")
    write_registry(isolated_hermes_home,
                   [{"id": "demo", "name": "Демо", "root": str(demo)}])
    source = tmp_path / "src" / "5-Готовый план.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(helpers.PLAN.replace("# 1. Демо-план конвейера", "# 5. Готовый план"),
                      encoding="utf-8")
    with running(demo) as (url, _httpd):
        status, added = post(url, "/api/plan/add", {"project": "demo", "path": str(source)})
        assert status == 200 and added["ok"] is True, added
        assert added["status"] == "added"
        assert [p.name for p in sorted((demo / "docs" / "plans").glob("5-*.md"))] \
            == ["5-Готовый-план.md"]

        # Окно недоступно/отменено: путь пуст, сервер отвечает `cancelled`, а не молчит.
        monkeypatch.setattr(plans_add, "_tk_pick", lambda title="": "")
        status, cancelled = post(url, "/api/plan/add", {"project": "demo", "path": ""})
        assert status == 400 and cancelled["ok"] is False
        assert cancelled["status"] == "cancelled"
        assert "отменён" in cancelled["error"]


def test_preview_draws_rows_without_buttons(tmp_path: Path):
    """Предпросмотр: прочерк вместо кнопок, форма скрыта стилем, сервер отказывает."""
    html = page()
    body = block(html, "function planActionsCell(", "function planAdd(")
    # Прочерк — до сборки кнопок: иначе `data-plan-act` успел бы появиться.
    assert 'if (preview) return \'<td class="mono">—' in body
    assert body.index("if (preview)") < body.index("data-plan-act")
    # Баланс тегов: каждая кнопка, подпись и ячейка закрыты.
    for opened, closed in (("<button", "</button>"), ("<span", "</span>"), ("<td", "</td>")):
        assert body.count(opened) == body.count(closed), (opened, closed)

    render = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    assert "Boolean(data.preview)" in render
    assert '$("plans-add").style.display = data.preview ? "none" : "";' in render
    assert '$("plans-add").hidden' not in html

    bare = tmp_path / "bare"
    (bare / "docs" / "plans").mkdir(parents=True)
    (bare / "docs" / "plans" / "1-demo.md").write_text(helpers.PLAN, encoding="utf-8")
    with running(bare, preview=True) as (url, _httpd):
        status, data = get(f"{url}/api/projects")
        assert status == 200 and data["ok"] is True, data
        assert data["preview"] is True
        status, refused = post(url, "/api/plan/add", {"project": "", "path": ""})
    # Второй рубеж: даже если кнопка появится, сервер откажет — предпросмотр только читает.
    assert status == 403 and refused["ok"] is False
    assert "предпросмотр только читает" in refused["error"]


def test_action_cell_is_ninth_column_and_kinds_come_from_engine(
        tmp_path: Path, monkeypatch, hermes_stub: Path, isolated_hermes_home: Path):
    """«Действие» — девятая колонка, а вид действия берётся из ответа движка."""
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_PLANS", raising=False)
    monkeypatch.delenv("PIPELINE_CONFIG", raising=False)
    monkeypatch.setenv("PIPELINE_LOCK_WAIT", "0")
    html = page()
    table = block(html, 'id="tab-plans"', "</table>")
    headers = re.findall(r'<th scope="col">([^<]+)</th>', table)
    assert headers == ["Проект", "План", "Название", "Состояние", "Следующий шаг",
                       "Закрыто/всего", "Утверждение", "Стоп", "Действие"]

    render = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    assert 'data-plan-id="${esc(plan.id)}"' in render
    assert "${planActionsCell(project, plan, Boolean(data.preview))}" in render
    assert 'colspan="9"' in render
    assert "plansRows = rows;" in render

    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    set_driven(beta, ["1"])
    add_plan(beta, "Второй план беты")
    write_registry(isolated_hermes_home,
                   [{"id": "alpha", "name": "Альфа", "root": str(alpha)},
                    {"id": "beta", "name": "Бета", "root": str(beta)}])
    with running(alpha) as (url, _httpd):
        status, data = get(f"{url}/api/projects")
    assert status == 200 and data["ok"] is True, data
    rows = [plan for project in data["projects"] for plan in project.get("plans", [])]
    assert len(rows) == 3
    for plan in rows:
        action = plan["action"]
        # Вид приходит от движка и входит в таблицу страницы: иначе кнопка — догадка.
        assert action["kind"] in ENGINE_KINDS, plan
        assert isinstance(action["text"], str), plan
        assert isinstance(action["command"], str), plan
