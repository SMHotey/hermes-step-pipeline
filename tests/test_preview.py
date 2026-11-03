# -*- coding: utf-8 -*-
"""Режим предпросмотра: читаем проект без установки конвейера, действия запрещены."""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import PLAN, install_project  # noqa: E402
import cli  # noqa: E402
import snapshot  # noqa: E402
import ui_server  # noqa: E402


PLAN_TEXT = """# 7. Настоящий план проекта

## Шаги

### S1. Первый шаг (1 дн)

- Файл: `app/a.py`
- Тесты/гейты: `pytest tests/test_a.py -q` → 2 passed
- Готово = файл `app/a.py` есть

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| 01.11.2026 | S1 (Первый шаг) | abc1234 | ревью APPROVE | ЗАКРЫТ |
"""


@pytest.fixture()
def bare_project(tmp_path: Path) -> Path:
    """Проект без pipeline.json и без .pipeline: только документы планов."""
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "7-real.md").write_text(PLAN_TEXT, encoding="utf-8")
    return tmp_path


def test_preview_reads_plans_without_creating_files(bare_project: Path):
    data = snapshot.snapshot(bare_project, preview=True, board="some-board")
    assert data["preview"] is True
    assert [p["id"] for p in data["plans"]] == ["7"]
    assert data["plans"][0]["next"] == "—" or data["plans"][0]["next"] == "None"
    assert not (bare_project / ".pipeline").exists(), "предпросмотр не должен создавать файлы"
    assert not (bare_project / "pipeline.json").exists()


def test_preview_shows_steps_and_journal(bare_project: Path):
    data = snapshot.snapshot(bare_project, preview=True)
    assert data["plan"]["id"] == "7"
    assert [s["id"] for s in data["plan"]["steps"]] == ["S1"]
    assert data["plan"]["closed"] == ["S1"]
    assert data["journal"] and data["journal"][-1][-1] == "ЗАКРЫТ"


def test_preview_does_not_hide_missing_config(bare_project: Path):
    """Без preview тот же проект — ошибка «не установлено», а не выдуманные данные."""
    data = snapshot.snapshot(bare_project)
    assert data["errors"], "обычный режим обязан ругаться на отсутствие конвейера"


def test_status_preview_mentions_read_only(bare_project: Path, capsys):
    args = Namespace(project=str(bare_project), plan="", preview=True,
                     board="some-board", json=False)
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "ПРЕДПРОСМОТР" in out
    assert "план 7" in out.lower() or "План 7" in out


def test_preview_ui_blocks_actions(tmp_path: Path):
    server = ui_server.make_server(tmp_path, port=0, preview=True)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = Request(f"{url}/api/action", data=json.dumps({"action": "tick"}).encode(),
                          headers={"Content-Type": "application/json"})
        with pytest.raises(Exception) as excinfo:
            urlopen(request, timeout=10)
        body = getattr(excinfo.value, "read", lambda: b"{}")()
        payload = json.loads(body.decode("utf-8"))
        assert payload["ok"] is False
        assert "предпросмотр" in payload["error"]
        with urlopen(f"{url}/api/snapshot", timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        assert data["preview"] is True
    finally:
        server.shutdown()
        server.server_close()


SECOND_PLAN = """# 2. Второй план

## Шаги

### S1. Первый шаг (1 дн)

- Файл: `app/a.py`
- Тесты/гейты: `pytest tests/test_a.py -q` → 2 passed
- Готово = файл `app/a.py` есть

### S2. Второй шаг (1 дн)

- Файл: `app/b.py`
- Тесты/гейты: `pytest tests/test_b.py -q` → 1 passed
- Готово = файл `app/b.py` есть

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| 01.11.2026 | S1 (Первый шаг) | abc1234 | ревью APPROVE | ЗАКРЫТ |
"""


def test_overview_announces_tick_and_blocks_it_on_stop(tmp_path: Path):
    """Обзор называет действие Тика с причиной и гасит кнопку при стоп-флаге (шаг 3-S2)."""
    root = install_project(tmp_path / "overview", board="overview-board")
    (root / "docs" / "plans" / "2-second.md").write_text(SECOND_PLAN, encoding="utf-8")
    snapshot.clear_cache()

    view = snapshot.snapshot(root, plan="1")["overview"]
    action = view["next_action"]
    assert action["label"] == "Тик: заказ постановки S1"
    assert "нет постановки шага S1" in action["reason"]
    assert action["disabled"] is False and action["why"] == ""
    assert view["state"]["prompt"] == {"exists": False, "file": ""}
    assert view["state"]["fix_rounds"] == 0
    assert view["state"]["risk"] == "normal"
    # Прогресс: у открытого плана число шагов из пакета, у соседнего — из запуска движка
    # по изменению файла плана (в `gate.py --plans` этого числа нет, а свой парсер плана
    # в плагине не заводится).
    by_id = {row["id"]: row for row in view["progress"]}
    assert (by_id["1"]["closed"], by_id["1"]["total"]) == (0, 2)
    assert (by_id["2"]["closed"], by_id["2"]["total"]) == (1, 2)

    stop = root / ".pipeline" / "state" / "plan_1_stop.json"
    stop.write_text(json.dumps({"kind": "owner", "reason": "проверка владельца",
                                "at": "03.11.2026 10:00"}, ensure_ascii=False), encoding="utf-8")
    snapshot.clear_cache()

    blocked = snapshot.snapshot(root, plan="1")["overview"]["next_action"]
    assert blocked["disabled"] is True
    assert "стоп-флаг владельца" in blocked["why"]
    assert "проверка владельца" in blocked["why"]

    # Предпросмотр гасит кнопку тем же полем снимка, а не догадкой страницы.
    bare = tmp_path / "bare-overview"
    (bare / "docs" / "plans").mkdir(parents=True)
    (bare / "docs" / "plans" / "7-real.md").write_text(PLAN_TEXT, encoding="utf-8")
    snapshot.clear_cache()
    preview_action = snapshot.snapshot(bare, preview=True)["overview"]["next_action"]
    assert preview_action["disabled"] is True
    assert "предпросмотр" in preview_action["why"]

    html = (Path(__file__).resolve().parents[1] / "step_pipeline" / "ui" / "index.html").read_text(
        encoding="utf-8")
    for anchor in ('id="next-action"', 'id="next-label"', 'id="next-why"',
                   'id="progress"', 'id="state-rows"', "renderOverview(data)"):
        assert anchor in html
