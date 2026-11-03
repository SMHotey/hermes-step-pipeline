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

from helpers import PLAN, PLUGIN, install_project  # noqa: E402
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


def test_overview_tick_consequence_and_stop(tmp_path: Path, monkeypatch, bare_project: Path):
    """Обзор называет следствие Тика с причиной и гасит кнопку при стоп-флаге (шаг 3-S2)."""
    # Без этого снимок читает НАСТОЯЩУЮ борду профиля, и чужие карточки меняют обзор (D11).
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(tmp_path / "kanban-empty.db"))
    root = install_project(tmp_path / "overview", board="overview-board")
    (root / "docs" / "plans" / "2-second.md").write_text(SECOND_PLAN, encoding="utf-8")
    snapshot.clear_cache()

    data = snapshot.snapshot(root, plan="1")
    ov = data["overview"]
    assert ov["tick_label"] == "Тик: заказ постановки S1"
    assert ov["tick_available"] is True and ov["tick_disabled_reason"] == ""
    assert ov["reason"] and "нет постановки шага S1" in ov["reason"]
    assert ov["prompt"] == {"exists": False, "path": ""}
    assert ov["stop"] is None
    assert ov["risk"] == "normal"
    assert ov["fix_rounds"] == 0
    assert ov["waiting"] is None
    assert ov["checks"] is None
    # Прогресс: по строке на план, число шагов — по файлу плана (движок не запускается).
    by_id = {row["id"]: row for row in ov["progress"]}
    assert {"1", "2"} <= set(by_id)
    assert by_id["1"]["total"] == len(data["plan"]["steps"]) == 2
    assert by_id["1"]["closed"] == 0
    assert (by_id["2"]["closed"], by_id["2"]["total"]) == (1, 2)

    stop = root / ".pipeline" / "state" / "plan_1_stop.json"
    stop.write_text(json.dumps({"kind": "owner", "source": "owner",
                                "reason": "решение владельца", "at": "03.11.2026 10:00:00"},
                               ensure_ascii=False), encoding="utf-8")
    snapshot.clear_cache()

    stopped = snapshot.snapshot(root, plan="1")["overview"]
    assert stopped["tick_available"] is False
    assert "стоп" in stopped["tick_disabled_reason"]
    assert "Продолжить" in stopped["tick_disabled_reason"]
    assert "решение владельца" in stopped["tick_disabled_reason"]
    assert stopped["stop"]["kind"] == "owner"

    # Предпросмотр гасит кнопку тем же полем снимка, а не догадкой страницы.
    snapshot.clear_cache()
    preview = snapshot.snapshot(bare_project, preview=True)["overview"]
    assert preview["tick_available"] is False
    assert "предпросмотр" in preview["tick_disabled_reason"]

    html = (PLUGIN / "ui" / "index.html").read_text(encoding="utf-8")
    for anchor in ('id="overview"', 'id="ov-tick"', 'id="ov-reason"', 'id="ov-bars"',
                   'id="ov-badge"', 'id="ov-waiting"', 'id="ov-rounds"', 'id="ov-prompt"',
                   'id="ov-risk"', "tick_label"):
        assert anchor in html
