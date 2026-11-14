# -*- coding: utf-8 -*-
"""Блок «Ревью плана» на вкладке «Шаг»: снимок, страница и жест «Ещё круг правок».

Уровень — снимок и живой HTTP: числа берутся из тех же карточек борды, что читает страж,
поэтому тест сверяет не «ключ есть», а значения (`круг 4 из 4`, `круг 2 из 2`, `круг 4 из 5`,
`круг 0 из 4`). Граница эпохи — запись утверждения, бюджет — `max_fix_rounds` проекта
плюс ручные круги маркера.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helpers import KANBAN_SCHEMA
from step_pipeline import plan_rounds, project as proj, snapshot, ui_server

PAGE = Path(ui_server.__file__).resolve().parent / "ui" / "index.html"
PANEL = {"X-Pipeline-Panel": "1"}


@pytest.fixture(autouse=True)
def clean_cache():
    """Кэш снимка — состояние процесса: сбрасываем до и после теста."""
    snapshot.clear_cache()
    yield
    snapshot.clear_cache()


def cfg_max(root: Path, limit: int) -> None:
    """`max_fix_rounds` проекта: панель лимит читает, поэтому тест его и задаёт."""
    path = root / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["max_fix_rounds"] = limit
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def approval(root: Path, plan_id: str = "1", **extra) -> Path:
    """Запись утверждения: `review_reset_at` — начало эпохи, без него граница по `approved_at`."""
    path = root / ".pipeline" / "state" / f"plan_{plan_id}_approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": plan_id, "file": f"docs/plans/{plan_id}-demo.md", "sha256": "x",
               "approved_at": "2026-01-01T00:00:00", "profile": "test"}
    payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def board(db: Path, rows: list[dict]) -> None:
    """Карточки плана в тестовой борде: те же колонки, что у боевой `tasks`."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        for name in ("started_at", "completed_at"):
            if name not in present:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {name} INTEGER")
        for row in rows:
            con.execute("INSERT INTO tasks (id, title, status, assignee, idempotency_key,"
                        " created_at, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (row["id"], row.get("title") or row["id"], row.get("status") or "done",
                         "driver", row["key"], row["created"], row.get("started"),
                         row.get("completed")))
        con.commit()
    finally:
        con.close()


def epoch(rounds: int, base: int | None = None) -> list[dict]:
    """`rounds` кругов ревью плана: столько же карточек ревью и правок, все закрытые."""
    start = base if base is not None else int(time.time()) - 3600
    rows: list[dict] = []
    for number in range(1, rounds + 1):
        created = start + number * 10
        rows.append({"id": f"t_r{number}", "created": created, "started": created,
                     "completed": created + 600,
                     "key": "planloop:1:planreview" if number == 1
                            else f"planloop:1:planreview:{number}"})
        rows.append({"id": f"t_f{number}", "created": created + 5, "started": created + 5,
                     "completed": created + 305, "key": f"planloop:1:planfix:{number}"})
    return rows


def block(root: Path) -> dict:
    """Блок «Ревью плана» из снимка проекта (план 1 — единственный в фикстуре)."""
    data = snapshot.snapshot(root)
    assert not data["errors"], data["errors"]
    return data["progress"]["plan_review"]


def page() -> str:
    """Текст страницы панели — единственный источник разметки блока."""
    return PAGE.read_text(encoding="utf-8")


def part(text: str, start: str, end: str) -> str:
    """Кусок текста страницы от `start` до ближайшего `end` после него."""
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def test_block_counts_engine_budget(project: Path, hermes_stub: Path):
    """Исчерпанный бюджет виден снимком как «круг 4 из 4»; разметка блока есть на странице."""
    cfg_max(project, 4)
    board(hermes_stub, epoch(4))

    data = snapshot.snapshot(project)
    assert not data["errors"], data["errors"]
    blk = data["progress"]["plan_review"]

    assert blk["label"] == "круг 4 из 4"
    assert blk["round"] == 4
    assert blk["budget"] == 4
    assert blk["exhausted"] is True
    assert blk["can_fix"] is True
    assert blk["note"] == ""
    # Время закрытых карточек: 4 ревью по 10 минут и 4 правки по 5 минут.
    assert blk["review_duration"] == "40 мин"
    assert blk["fix_duration"] == "20 мин"

    text = page()
    assert 'id="step-plan-review"' in text
    assert 'id="plan-review-round"' in text
    assert 'id="btn-plan-fix"' in text
    assert "Ещё круг правок" in text
    assert '"plan_fix"' in text

    # План без распознанных шагов блока не получает: страж его не ведёт, ревью плана нет.
    assert snapshot._plan_progress(project, proj.read_config(project), {"id": "9"}, None) == {}


def test_copy_with_limit_two(project: Path, hermes_stub: Path):
    """Копия проекта с `max_fix_rounds: 2` показывает «круг 2 из 2»: бюджет — из конфига."""
    cfg_max(project, 2)
    board(hermes_stub, epoch(2))

    blk = block(project)

    assert blk["label"] == "круг 2 из 2"
    assert blk["round"] == 2
    assert blk["budget"] == 2
    assert blk["exhausted"] is True
    assert blk["can_fix"] is True


def test_marker_grant_hides_button(project: Path, hermes_stub: Path):
    """Ручной круг из маркера поднимает бюджет до 5 и гасит кнопку: жест уже израсходован."""
    cfg_max(project, 4)
    board(hermes_stub, epoch(4))
    path = plan_rounds.marker_path(project, "1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"extra_fix_rounds": 1, "at": "01.01.2026 00:00:00",
                                "reason": "решение владельца"}, ensure_ascii=False),
                    encoding="utf-8")

    blk = block(project)

    assert blk["label"] == "круг 4 из 5"
    assert blk["extra_fix_rounds"] == 1
    assert blk["exhausted"] is False
    assert blk["can_fix"] is False
    assert blk["note"] == "ручной круг уже израсходован"
    # Кнопка гасится по `can_fix`, а не по факту наличия блока: иначе она была бы живой
    # на исчерпанном бюджете и мёртвой при пустой эпохе одновременно.
    render = part(page(), "function renderPlanReview(data) {", "function renderStep(data)")
    assert "can_fix" in render and "disabled" in render
    assert 'btn.disabled = !canFix;' in render


def test_reapproval_resets_epoch(project: Path, hermes_stub: Path):
    """Переутверждение плана начинает счёт кругов заново: «круг 0 из 4», а не «4 из 4»."""
    cfg_max(project, 4)
    board(hermes_stub, epoch(4))
    assert block(project)["label"] == "круг 4 из 4"

    approval(project, review_reset_at=int(time.time()))
    snapshot.clear_cache()
    fresh = block(project)

    assert fresh["label"] == "круг 0 из 4"
    assert fresh["round"] == 0
    assert fresh["exists"] is False
    assert fresh["can_fix"] is False

    # Запись без `review_reset_at` (старая версия плагина): граница по `approved_at`
    # 2026-01-01, карточки эпохи моложе — снова «4 из 4».
    approval(project)
    snapshot.clear_cache()
    assert block(project)["label"] == "круг 4 из 4"


def test_step_detail_and_gesture(project: Path, hermes_stub: Path, monkeypatch):
    """`step_detail` отдаёт тот же блок, а жест `plan_fix` пишет маркер и зовёт движок адресно."""
    cfg_max(project, 4)
    board(hermes_stub, epoch(4))
    blk = block(project)

    detail = snapshot.step_detail(project, "1", "S1")
    assert detail["plan_review"]["label"] == blk["label"]
    assert detail["plan_review"]["round"] == blk["round"]
    assert detail["plan_review"]["budget"] == blk["budget"]
    assert detail["plan_review"]["exhausted"] == blk["exhausted"]

    calls: list[tuple] = []

    def fake_engine(state, *args, **kwargs):
        calls.append(args)
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(ui_server, "_run_engine", fake_engine)
    httpd = ui_server.make_server(project, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        first = post(url, {"action": "plan_fix", "plan": "1"})
        marker = project / ".pipeline" / "state" / "plan_1_rounds.json"
        assert first[0] == 200 and first[1]["ok"] is True, first[1]
        assert first[1]["extra_fix_rounds"] == 1
        assert calls[0] == ("--plan", "1", "--resume")
        assert marker.is_file()
        assert json.loads(marker.read_text(encoding="utf-8"))["extra_fix_rounds"] == 1
        second = post(url, {"action": "plan_fix", "plan": "1"})
        bad = post(url, {"action": "plan_fix", "plan": "неделя"})
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert second[0] == 200 and second[1]["extra_fix_rounds"] == 2
    assert calls[1] == ("--plan", "1", "--resume")
    assert json.loads(marker.read_text(encoding="utf-8"))["extra_fix_rounds"] == 2
    assert bad[1]["ok"] is False
    assert bad[1]["error"] == "нужно поле plan: номер плана"


def post(url: str, payload: dict) -> tuple[int, dict]:
    """POST действия панели с JSON-телом и меткой панели."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{url}/api/action", data=data,
                                     headers={"Content-Type": "application/json", **PANEL})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
