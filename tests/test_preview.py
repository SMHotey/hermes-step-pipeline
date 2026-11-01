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

from helpers import PLAN  # noqa: E402
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
