# -*- coding: utf-8 -*-
"""Обзор планов в панели: статус, причина и «что сделать» (план 5, шаг S2A).

Проверяется то, что читает панель: `gate.plans_rows` — состояние строки считается
решением стража (`build_ctx` + `decide`), а остаточный стоп-файл статус не подменяет.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import PLAN, cards, complete, make_project, write_approval

LONG_REASON = ("остановка владельцем: ждём решения по разведке готовых решений, потому "
               "что план опирается на найденный код, а без ответа владельца дальше идти "
               "нельзя — причина длиннее ста двадцати символов, чтобы проверить перенос")


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте (шаблон — tests/engine/test_risk.py)."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, config_mod.load(project)


def rows(root: Path) -> list[dict]:
    return gate.plans_rows(config_mod.load(root))


def plan_row(root: Path, plan_id: str = "1") -> dict:
    return next(row for row in rows(root) if str(row["id"]) == plan_id)


def write_stop(root: Path, plan_id: str = "1", **payload) -> Path:
    path = Path(root) / ".pipeline" / "state" / f"plan_{plan_id}_stop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def mark_cron(root: Path) -> Path:
    """Метка заведённой cron-джобы: её пишет мастер установки (`setup_flow`)."""
    path = Path(root) / ".pipeline" / "cron.json"
    path.write_text(json.dumps({"driver": "demo", "name": gate.CRON_JOB_NAME,
                                "workdir": str(root)}, ensure_ascii=False), encoding="utf-8")
    return path


def test_running_plan_with_cron_marker_has_no_action(stand):
    root, _cfg = stand
    mark_cron(root)
    row = plan_row(root)
    assert row["state"] == "running"
    assert row["state_label"] == "идёт"
    assert row["note"] == ""
    assert row["action"] == {"kind": "none", "text": "", "command": ""}


def test_missing_cron_marker_gives_cron_action(stand):
    root, _cfg = stand
    row = plan_row(root)
    assert row["state"] == "running"
    assert row["action"]["kind"] == "cron"
    assert "cron create" in row["action"]["command"]
    assert f'--name "{gate.CRON_JOB_NAME}"' in row["action"]["command"]
    assert f'--workdir "{root}"' in row["action"]["command"]


def test_owner_stop_is_stopped_and_keeps_reason_whole(stand):
    root, _cfg = stand
    write_stop(root, kind="owner", source="owner", reason=LONG_REASON,
               at="06.11.2026 22:00:00")
    row = plan_row(root)
    assert row["state"] == "stopped"
    assert row["state_label"] == "остановлен"
    assert row["reason"] == LONG_REASON              # посимвольно, без обрезки
    assert row["action"]["kind"] == "resume"
    assert row["action"]["command"].endswith("--plan 1 --resume")


def test_blocked_step_card_gives_unblock_with_command(tmp_path: Path, hermes_stub: Path,
                                                     monkeypatch):
    # require_prompt=False — карточка шага создаётся сразу, без заказа постановки.
    root = make_project(tmp_path / "blocked", require_prompt=False)
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    assert gate.main(["--plan", "1", "--run"]) == 0
    created = cards(hermes_stub)
    assert [card["key"] for card in created] == ["planloop:1:S1:step"]
    card_id = created[0]["id"]
    complete(hermes_stub, "S1:step", status="blocked")
    row = plan_row(root)
    assert row["state"] == "stopped"
    assert row["action"]["kind"] == "unblock"
    assert card_id in row["action"]["text"]
    assert row["action"]["command"] == f"hermes kanban --board testboard unblock {card_id}"


def test_incident_without_blocked_card_gives_tick(tmp_path: Path, hermes_stub: Path,
                                                  monkeypatch):
    root = make_project(tmp_path / "incident", require_prompt=False)
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    assert gate.main(["--plan", "1", "--run"]) == 0
    card_id = cards(hermes_stub)[0]["id"]
    complete(hermes_stub, "S1:step", status="crashed")
    # Инцидентный стоп-флаг ставит сам страж; причина та же, что в решении.
    write_stop(root, kind="incident", source="gate",
               reason=f"карточка шага {card_id} в статусе crashed: прогон упал")
    row = plan_row(root)
    assert row["state"] == "stopped"
    assert row["action"]["kind"] == "tick"
    assert row["action"]["command"].endswith("--plan 1 --run")


def test_cleared_incident_stop_is_running_with_note(stand):
    root, _cfg = stand
    write_stop(root, kind="incident", source="gate",
               reason="нет постановки шага S1: заказана роли prompter")
    row = plan_row(root)
    assert row["state"] == "running"
    assert row["note"] == ("стоп снят (было: нет постановки шага S1: "
                           "заказана роли prompter)")
    assert row["stop"] is not None                   # файл-то есть
    assert row["action"]["kind"] == "none"


def test_hold_gives_unhold_action(stand):
    root, _cfg = stand
    assert gate.main(["--plan", "1", "--hold", "S1"]) == 0
    row = plan_row(root)
    assert row["state"] == "running"
    assert row["action"]["kind"] == "unhold"
    assert "S1" in row["action"]["text"]
    assert "--unhold S1" in row["action"]["command"]


def test_plan_outside_driven_list_is_not_driven(tmp_path: Path, hermes_stub: Path,
                                                monkeypatch):
    root = make_project(tmp_path / "not-driven", driven_plans=["2"])
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    row = plan_row(root)
    assert row["state"] == "not-driven"
    assert row["state_label"] == "не ведётся"
    assert row["reason"] == gate.NOT_DRIVEN_REASON
    assert row["action"]["kind"] == "drive"
    assert row["action"]["command"] == ""


def test_closed_plan_ignores_stop_file(stand):
    root, _cfg = stand
    path = root / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN.replace("| открыт |", "| закрыт |"), encoding="utf-8")
    write_approval(root, path)
    # Стоп-файл поставлен заранее: закрытый план обязан остаться закрытым.
    write_stop(root, kind="owner", source="owner", reason="остановка владельцем")
    row = plan_row(root)
    assert row["state"] == "closed"
    assert row["state_label"] == "закрыт"
    assert row["action"] == {"kind": "none", "text": "закрыт, действий не требуется",
                             "command": ""}


def test_plan_row_carries_approval_label(stand):
    """Утверждение видно отдельным бейджем: состояния конвейера про него не говорят (0.22.0)."""
    root, _cfg = stand
    mark_cron(root)
    row = plan_row(root)
    assert row["approval"]["state"] == "approved", row["approval"]
    assert row["approval"]["label"] == "утверждён"
    assert row["approval"]["approved_at"]
    assert row["state_label"] in ("идёт", "остановлен", "закрыт", "не ведётся")


def test_plan_row_marks_changed_plan(stand):
    """План правился после утверждения: бейдж «изменён после утверждения», а не «утверждён»."""
    root, _cfg = stand
    mark_cron(root)
    plan = Path(root) / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\n- правка после утверждения\n",
                    encoding="utf-8")
    row = plan_row(root)
    assert row["approval"]["state"] == "changed", row["approval"]
    assert row["approval"]["label"] == "изменён после утверждения"
