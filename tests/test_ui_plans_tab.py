# -*- coding: utf-8 -*-
"""Вкладка «Планы» (план 6 S5): все планы реестра, восемь колонок и три фильтра.

Сторож читает текст страницы и живой ответ `/api/projects`. Движка JS в проекте нет и
в этом шаге не появляется: ни node, ни jsdom, ни playwright.
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

PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"

PLAN2_TEMPLATE = """# 2. {title}

Цель: второй план проекта для вкладки «Планы».

## Шаги
{steps}
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


def test_plans_tab_button_and_panel_are_in_order():
    """Вкладка «Планы» между «Обзором» и «Шагом»; showTab переключает её без новой ветки."""
    html = page()
    for needle in ('data-tab="plans"', 'id="tab-btn-plans"', 'aria-controls="tab-plans"',
                   'id="tab-plans"', 'id="plans-rows"', "renderPlansTab", "filterPlansTab"):
        assert needle in html, needle
    assert ">Планы</button>" in html
    # Порядок кнопок навигации и порядок панелей — один и тот же.
    assert html.index('data-tab="overview"') < html.index('data-tab="plans"') \
        < html.index('data-tab="step"')
    assert html.index('id="tab-overview"') < html.index('id="tab-plans"') \
        < html.index('id="tab-step"')
    # Панель переключается по "tab-" + name: новой ветки в showTab не нужно.
    assert 'panel.id === "tab-" + name' in html


def test_plans_table_has_eight_columns_in_order():
    """Восемь колонок в порядке §4.1 и источники ячеек — поля строки плана, не текст."""
    html = page()
    panel = block(html, 'id="tab-plans"', "</table>")
    headers = re.findall(r'<th scope="col">([^<]+)', panel)
    assert headers == ["Проект", "План", "Название", "Состояние", "Следующий шаг",
                       "Закрыто/всего", "Утверждение", "Стоп"]
    body = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    for needle in ("project.name", "plan.id", "plan.name", "plan.state_label", "plan.next",
                   "plan.closed", "plan.total", "plan.approval", "plan.stop",
                   "data-project", "data-state"):
        assert needle in body, needle
    # «Утверждение» и «стоп» рисуются из полей ответа, а не разбираются из текста.
    for needle in ("label", "kind", "reason"):
        assert needle in body, needle


def test_three_filters_are_bound_and_compare_state_value():
    """Три фильтра с обработчиками; состояние сравнивается значением, а не подписью."""
    html = page()
    assert 'select id="plans-project"' in html
    assert 'select id="plans-state"' in html
    assert 'input type="text" id="plans-text"' in html
    assert '$("plans-project").addEventListener("change", filterPlansTab);' in html
    assert '$("plans-state").addEventListener("change", filterPlansTab);' in html
    assert '$("plans-text").addEventListener("input", filterPlansTab);' in html
    body = block(html, "function filterPlansTab(", "function renderPlansTab(")
    for needle in ("dataset.project", "dataset.state", "textContent.toLowerCase()", "row.hidden"):
        assert needle in body, needle
    assert "state_label" not in body, "фильтр сравнивает значение состояния, а не подпись"


def test_empty_answer_draws_plans_none_and_render_hangs_on_load_projects(
        tmp_path: Path, hermes_stub: Path, isolated_hermes_home: Path):
    """Пустой список рисует «планов нет», а строки вкладки питает реестр, а не снимок."""
    html = page()
    body = block(html, "function renderPlansTab(", "// Подключение проекта из панели")
    assert 'id="plans-empty"' in body
    assert 'colspan="8"' in body
    assert "планов нет" in body
    # Ряд один: в статической разметке панели его нет, иначе вхождений было бы два.
    assert html.count("планов нет") == 1
    load = block(html, "async function loadProjects(", "// ── глобальная пауза панели")
    assert "renderPlansTab(data)" in load
    assert "/api/snapshot" not in load
    # Живая половина: ветка «планов нет» достижима — проект без конвейера отдаёт пустой список.
    alpha = install_project(tmp_path / "alpha", board="uiboard")
    bare = tmp_path / "bare"
    bare.mkdir()
    write_registry(isolated_hermes_home,
                   [{"id": "bare", "name": "Голый", "root": str(bare)}])
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    rows = {row["id"]: row for row in data["projects"]}
    assert rows["alpha"]["installed"] is True and rows["alpha"]["plans"]
    assert rows["bare"]["installed"] is False
    assert rows["bare"]["plans"] == []


def test_state_options_match_engine_labels(tmp_path: Path, hermes_stub: Path,
                                           isolated_hermes_home: Path):
    """Опции фильтра — пары из `STATE_LABELS` движка; живые строки лежат в том же словаре."""
    from step_pipeline.engine import gate as engine_gate

    html = page()
    options = block(html, 'id="plans-state"', "</select>")
    pairs = [(value, label) for value, label
             in re.findall(r'<option value="([^"]*)">([^<]+)', options) if value]
    assert pairs == list(engine_gate.STATE_LABELS.items())
    # «в работе» — не подпись состояния: ассерт по блоку опций, а не по всей странице
    # (подстрока есть в баннере паузы и в комментариях страницы).
    assert "в работе" not in options

    alpha = install_project(tmp_path / "alpha", board="uiboard")
    beta = install_project(tmp_path / "beta", board="uiboard")
    config = json.loads((beta / "pipeline.json").read_text(encoding="utf-8"))
    config["driven_plans"] = ["1"]      # второй план проекта снят с ведения — в tmp_path
    (beta / "pipeline.json").write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    add_plan(beta, "Второй план беты", 2)
    put_stop(beta, "1", "стоп по бете")
    write_registry(isolated_hermes_home,
                   [{"id": "beta", "name": "Бета", "root": str(beta)}])
    with running(alpha) as (url, _httpd):
        status, data = get(url + "/api/projects")
    assert status == 200 and data["ok"] is True
    live = {(plan["state"], plan["state_label"])
            for row in data["projects"] for plan in row["plans"]}
    assert live == {("running", "идёт"), ("stopped", "остановлен"),
                    ("not-driven", "не ведётся")}
    assert live <= set(engine_gate.STATE_LABELS.items())
