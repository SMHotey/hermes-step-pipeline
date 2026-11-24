# -*- coding: utf-8 -*-
"""Снимок убитой по лимиту попытки (план 17, S3).

Зачем файл: 20.11.2026 шаг 2-S6 убит на 91-й минуте при лимите 60 минут, а в карточке
осталась одна строка `elapsed 5491s > limit 3600s`. Изменённый файл на диске был, но
следующая попытка об этом не знала и начинала с нуля. Тесты держат четыре вещи, на
которых снимок ломается молча: файлы берутся из рабочего дерева (а не из памяти),
комментарий уходит один раз на попытку, тело следующей карточки несёт блок «Продолжай с
этого», а большие и чужие деревья (git не ответил, дерево чистое) не роняют тик.

Числа ожиданий посчитаны по логу фикстуры (`LOG` ниже) вручную: один вызов модели
(`model_ms` = 10000), инструменты `read_file` 100 мс, `patch` 3500 мс, `terminal` 1000 мс
(`tools_total_ms` = 9600), таймаутов 0, стена 30000 мс — последние три инструмента и есть
хвост снимка.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

import config as config_mod
import gate

#: Шаг демо-плана в том виде, в каком его видит движок запасного пути (Д5).
STEP = {"id": "S1", "title": "Таблица счетов", "files": ["app/models/invoice.py"],
        "goal": "g", "gates": [], "dod": "d", "read": [], "notes": ""}
#: Адрес попытки: карточка шага в статусе «убита по лимиту».
CARD = {"id": "t_step01", "assignee": "demo", "status": "timed_out"}
#: Строка метрики попытки — те же ключи, что пишет `step_metrics.attempt_row`.
ROW = {"ts": 0.0, "at": "20.11.2026 10:31:20", "plan": "1", "step": "S1",
       "card": "t_step01", "card_kind": "step", "attempt": 1, "run_id": "77",
       "role": "demo", "model": "deepseek-v4.1-flash", "calls": 1, "in": 3000,
       "out": 300, "model_ms": 10000, "tools_ms": {"read_file": 100, "patch": 3500,
                                                   "terminal": 1000},
       "tools_total_ms": 9600, "tool_timeouts": 0, "stall": False, "wall_ms": 30000,
       "killed_by": "runtime", "limit_s": 4080, "source": "agent.log", "log": ""}

#: Лог попытки: один вызов модели и четыре инструмента в окне 10:00:00–10:00:30.
LOG = """\
2026-11-20 10:00:00,500 INFO API call #1: model=deepseek-v4.1-flash provider=opencode-go\
 in=3000 out=300 total=3300 latency=10.0s session=s1
2026-11-20 10:00:05,000 INFO tool terminal completed (5.00s, 120 chars)
2026-11-20 10:00:10,000 INFO tool read_file completed (0.10s, 900 chars)
2026-11-20 10:00:15,000 INFO Tool patch returned error (3.50s): {"success": false}
2026-11-20 10:00:20,000 INFO tool terminal completed (1.00s, 40 chars)
"""

TASKS = ("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
         " status TEXT, assignee TEXT, idempotency_key TEXT, completed_at INTEGER,"
         " result TEXT, last_failure_error TEXT, created_at INTEGER)")
TASK_RUNS = ("CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " task_id TEXT NOT NULL, profile TEXT, status TEXT, outcome TEXT,"
             " summary TEXT, error TEXT, started_at INTEGER, ended_at INTEGER,"
             " max_runtime_seconds INTEGER)")


def at(stamp: str) -> int:
    """Момент из лога (`10:00:20`) в epoch — та же локальная зона, что у борды."""
    return int(time.mktime(time.strptime("2026-11-20 " + stamp, "%Y-%m-%d %H:%M:%S")))


@pytest.fixture()
def no_logs(tmp_path: Path, monkeypatch) -> Path:
    """Пустой каталог Hermes: логи профилей хоста в снимок не попадают."""
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return home


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch, no_logs: Path) -> Path:
    """Стенд тика: проект с движком, поддельная борда и пустые логи профилей."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    raw["notify"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                           encoding="utf-8")
    return project


def paths(root: Path) -> gate.Paths:
    return gate.resolve_paths(config_mod.load(root), "1")


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def make_repo(root: Path) -> Path:
    """Стенд как git-репозиторий: состояние конвейера уже в `.gitignore`."""
    (root / ".gitignore").write_text(".pipeline/state/\n", encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def write_log(home: Path) -> Path:
    """Лог профиля-исполнителя там, где его ищет `step_metrics.agent_log`."""
    folder = home / "profiles" / "demo" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "agent.log"
    path.write_text(LOG, encoding="utf-8")
    return path


def seed_killed(db: Path, card_id: str = "t_step01", status: str = "timed_out",
                key: str = "planloop:1:S1:step", error: str = "elapsed 4264s > limit 4080s",
                runs: bool = True) -> int:
    """Карточка шага, убитая по лимиту, плюс запись прогона; вернуть id прогона."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASKS)
        if runs:
            con.executescript(TASK_RUNS)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, completed_at, result, last_failure_error,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (card_id, "шаг", status, "demo", key, None, "", error, at("09:00:00")))
        run_id = 0
        if runs:
            cur = con.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, error,"
                " started_at, ended_at, max_runtime_seconds) VALUES (?,?,?,?,?,?,?,?)",
                (card_id, "demo", "done", status, error,
                 at("10:00:00"), at("10:00:30"), 4080))
            run_id = int(cur.lastrowid or 0)
        con.commit()
    finally:
        con.close()
    return run_id


def comments(db: Path) -> list[dict]:
    """Комментарии поддельной борды в порядке записи."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        if not con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                           " AND name = 'comments'").fetchall():
            return []
        rows = con.execute("SELECT task_id, body, author FROM comments"
                           " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"task_id": row[0], "body": row[1], "author": row[2]} for row in rows]


def metrics_file(root: Path) -> Path:
    return root / ".pipeline" / "state" / "metrics.jsonl"


def snapshot_file(root: Path) -> Path:
    return root / ".pipeline" / "state" / "kill_snapshot.json"


# ───────────────── П1: файлы снимка — из рабочего дерева ─────────────────

def test_collect_snapshot_lists_changed_and_created(stand: Path, hermes_stub: Path) -> None:
    """Изменённый файл в дереве даёт список путей, diff-строку и источник `git`."""
    tracked = stand / "app" / "models" / "invoice.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("amount = 0\n", encoding="utf-8")
    make_repo(stand)
    tracked.write_text("amount = 1\n", encoding="utf-8")
    (stand / "notes.md").write_text("черновик\n", encoding="utf-8")

    snapshot = gate.collect_kill_snapshot(paths(stand), CARD, dict(ROW, ts=at("10:31:20")))

    assert snapshot["source"] == "git"
    assert "app/models/invoice.py" in snapshot["changed"]
    assert "notes.md" in snapshot["created"]
    assert "app/models/invoice.py" in snapshot["stat"]
    assert snapshot["step"] == "S1" and snapshot["card"] == "t_step01"
    assert snapshot["metrics"]["killed_by"] == "runtime"


def test_snapshot_takes_last_three_tools_of_attempt(stand: Path, hermes_stub: Path,
                                                    no_logs: Path) -> None:
    """Инструменты — хвост окна попытки: последние три из лога, а не все четыре."""
    make_repo(stand)
    write_log(no_logs)

    snapshot = gate.collect_kill_snapshot(paths(stand), CARD, dict(ROW, ts=at("10:31:20")))

    assert [item["name"] for item in snapshot["tools"]] == ["read_file", "patch", "terminal"]
    assert [item["ms"] for item in snapshot["tools"]] == [100, 3500, 1000]
    assert [item["timeout"] for item in snapshot["tools"]] == [False, False, False]


# ───────────────── П2: комментарий в карточку, один на попытку ─────────────────

def test_tick_comments_snapshot_into_card(stand: Path, hermes_stub: Path,
                                          no_logs: Path) -> None:
    """Тик кладёт снимок комментарием: адрес попытки, числа метрики, хвост инструментов."""
    make_repo(stand)
    write_log(no_logs)
    run_id = seed_killed(hermes_stub)

    gate.run_gate(paths(stand), notify_fn=lambda text: None)

    rows = comments(hermes_stub)
    assert len(rows) == 1, rows
    assert rows[0]["task_id"] == "t_step01"
    body = rows[0]["body"]
    assert "СНИМОК УБИТОЙ ПОПЫТКИ" in body
    assert f"прогон {run_id}" in body
    assert "вызовов модели 1" in body
    assert "read_file 0,1 с" in body
    assert "снимок не заменяет коммит" in body


def test_second_tick_keeps_one_comment(stand: Path, hermes_stub: Path,
                                       no_logs: Path) -> None:
    """Повторный тик по той же попытке второй комментарий не пишет (Д4)."""
    make_repo(stand)
    write_log(no_logs)
    seed_killed(hermes_stub)
    p = paths(stand)

    gate.run_gate(p, notify_fn=lambda text: None)
    first = snapshot_file(stand).read_text(encoding="utf-8")
    gate.run_gate(p, notify_fn=lambda text: None)

    assert len(comments(hermes_stub)) == 1
    entry = json.loads(first)["1:S1"]
    assert entry["commented"] is True and entry["comment_at"]


def test_failed_comment_is_retried_next_tick(stand: Path, hermes_stub: Path,
                                             no_logs: Path, monkeypatch) -> None:
    """Отказ `kanban comment` отметку не ставит: следующий тик дошлёт (Д4)."""
    make_repo(stand)
    write_log(no_logs)
    seed_killed(hermes_stub)
    p = paths(stand)
    monkeypatch.setenv("PIPELINE_STUB_COMMENT_RC", "3")

    gate.run_gate(p, notify_fn=lambda text: None)
    assert comments(hermes_stub) == []
    entry = json.loads(snapshot_file(stand).read_text(encoding="utf-8"))["1:S1"]
    assert entry["commented"] is False, "отказ комментария не должен ставить отметку"

    monkeypatch.delenv("PIPELINE_STUB_COMMENT_RC")
    gate.run_gate(p, notify_fn=lambda text: None)
    assert len(comments(hermes_stub)) == 1


# ───────────────── П3: пустое дерево ─────────────────

def test_clean_tree_snapshot_says_no_files(stand: Path, hermes_stub: Path,
                                           no_logs: Path) -> None:
    """Чистое дерево — снимок без списка файлов и с явной строкой «файлов нет»."""
    make_repo(stand)
    write_log(no_logs)
    seed_killed(hermes_stub)

    gate.run_gate(paths(stand), notify_fn=lambda text: None)

    body = comments(hermes_stub)[0]["body"]
    assert "файлов нет: git не показал ни изменённых, ни новых файлов" in body
    assert "- изменено:" not in body and "- создано" not in body


# ───────────────── П4: шаг не закрыт — блок в теле следующей карточки ─────────────────

def test_next_attempt_body_has_continuation_block(stand: Path, hermes_stub: Path,
                                                  no_logs: Path) -> None:
    """Тело следующей попытки несёт блок «Продолжай с этого» с теми же путями."""
    make_repo(stand)
    write_log(no_logs)
    seed_killed(hermes_stub)
    p = paths(stand)
    gate.run_gate(p, notify_fn=lambda text: None)

    body = gate.step_card_body(p, STEP)

    assert "Продолжай с этого" in body
    assert "app/models/invoice.py" in body
    assert "СНИМОК УБИТОЙ ПОПЫТКИ" not in body
    # Блок стоит до «Цель:»: следующая попытка читает его раньше объёма работ.
    assert body.index("Продолжай с этого") < body.index("Цель:")


def test_closed_step_has_no_continuation_block(stand: Path, hermes_stub: Path,
                                               no_logs: Path) -> None:
    """Шаг закрыт в журнале — снимок история, а не инструкция."""
    make_repo(stand)
    write_log(no_logs)
    seed_killed(hermes_stub)
    p = paths(stand)
    gate.run_gate(p, notify_fn=lambda text: None)

    plan = Path(p.plan_file)
    plan.write_text(plan.read_text(encoding="utf-8")
                    .replace("| — | S1 | — | — | открыт |", "| 20.11 | S1 | abc | тесты | ЗАКРЫТ |"),
                    encoding="utf-8")

    assert "Продолжай с этого" not in gate.step_card_body(p, STEP)


def test_body_without_snapshot_is_unchanged(stand: Path, hermes_stub: Path) -> None:
    """Снимка нет — блока нет, тело карточки прежнее."""
    body = gate.step_card_body(paths(stand), STEP)

    assert "Продолжай с этого" not in body
    assert "Цель: g" in body


# ───────────────── П5: текст блока и границы ─────────────────

def test_snapshot_text_matches_numbers(stand: Path, hermes_stub: Path) -> None:
    """Текст блока: лимит, пути, diff, числа метрики и хвост инструментов."""
    p = paths(stand)
    entry = dict(ROW, step="S1", card="t_step01", run_id="77", plan="1",
                 attempt=1, at="24.11.2026 10:31:20", source="git",
                 changed=["app/models/invoice.py"], created=["notes.md"],
                 stat="app/models/invoice.py | 3 +++\n 1 file changed, 3 insertions(+)",
                 tools=[{"name": "read_file", "ms": 100, "timeout": False},
                        {"name": "patch", "ms": 3500, "timeout": False},
                        {"name": "terminal", "ms": 1000, "timeout": False}],
                 metrics=dict(ROW), commented=False, comment_at="")
    gate.write_kill_snapshot(p, entry)

    block = gate.kill_snapshot_text(gate.kill_snapshot_entry(p, "S1") or {})

    assert block.startswith("Продолжай с этого (попытка убита по лимиту 4080 с,")
    assert "снимок 24.11.2026 10:31:20" in block
    assert "изменено: app/models/invoice.py" in block
    assert "создано (git не отслеживает): notes.md" in block
    assert "diff: app/models/invoice.py | 3 +++" in block
    assert ("метрика попытки: вызовов модели 1, модель 10,0 с, инструменты 9,6 с,"
            " таймаутов 0, стена 30,0 с") in block
    assert "последние инструменты: read_file 0,1 с, patch 3,5 с, terminal 1,0 с" in block
    assert "снимок не заменяет коммит" in block


def test_snapshot_text_survives_missing_metrics(stand: Path, hermes_stub: Path) -> None:
    """Строки метрики и событий нет — строки-заглушки, а не пустота и не исключение."""
    p = paths(stand)
    gate.write_kill_snapshot(p, {"plan": "1", "step": "S1", "card": "t_step01",
                                 "run_id": "", "source": "git", "at": "",
                                 "changed": [], "created": [], "stat": "",
                                 "tools": [], "metrics": {}})

    block = gate.kill_snapshot_text(gate.kill_snapshot_entry(p, "S1") or {})

    assert "файлов нет:" in block
    assert "метрика попытки: строки метрики нет" in block
    assert "последние инструменты: событий инструментов в логе нет" in block


def test_broken_snapshot_gives_no_block(stand: Path, hermes_stub: Path) -> None:
    """Битая запись — пустой блок: половина снимка хуже отсутствия снимка (Д6)."""
    assert gate.kill_snapshot_text({}) == ""
    assert gate.kill_snapshot_text({"step": "S1", "source": "память",
                                    "changed": [], "created": []}) == ""
    assert gate.kill_snapshot_text({"step": "", "source": "git",
                                    "changed": [], "created": []}) == ""


def test_git_silence_falls_back_to_step_files(stand: Path, hermes_stub: Path,
                                              monkeypatch) -> None:
    """`git status` дольше лимита — снимок по списку «Файл:» шага (стоп-сигнал шага)."""
    p = paths(stand)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git status", timeout=10)

    monkeypatch.setattr(gate, "_run", boom)

    snapshot = gate.collect_kill_snapshot(p, CARD, dict(ROW, ts=at("10:31:20")), step=STEP)
    block = gate.kill_snapshot_text(snapshot)

    assert snapshot["source"] == "step_files"
    assert snapshot["changed"] == ["app/models/invoice.py"]
    assert "git не ответил за 10 с" in block
    assert "app/models/invoice.py" in block


def test_long_file_list_is_truncated(stand: Path, hermes_stub: Path) -> None:
    """Путей больше двадцати — печатаются первые двадцать и счётчик остальных."""
    p = paths(stand)
    paths_list = [f"app/file{index:02d}.py" for index in range(25)]
    gate.write_kill_snapshot(p, {"plan": "1", "step": "S1", "card": "t_step01",
                                 "source": "git", "at": "24.11.2026 10:31:20",
                                 "changed": paths_list, "created": [], "stat": "",
                                 "tools": [], "metrics": dict(ROW)})

    block = gate.kill_snapshot_text(gate.kill_snapshot_entry(p, "S1") or {})

    assert "app/file19.py" in block and "app/file20.py" not in block
    assert "ещё 5 файлов не показаны" in block


# ───────────────── П6: запасной путь без прогонов и проба состояния ─────────────────

def test_card_error_alone_starts_the_snapshot(stand: Path, hermes_stub: Path,
                                              no_logs: Path) -> None:
    """Прогонов в борде нет — лимит берётся из `last_failure_error` карточки (Д2)."""
    make_repo(stand)
    seed_killed(hermes_stub, runs=False)

    gate.run_gate(paths(stand), notify_fn=lambda text: None)

    rows = comments(hermes_stub)
    assert len(rows) == 1, rows
    assert "СНИМОК УБИТОЙ ПОПЫТКИ" in rows[0]["body"]
    entry = json.loads(snapshot_file(stand).read_text(encoding="utf-8"))["1:S1"]
    assert entry["card"] == "t_step01" and entry["source"] == "git"


def test_status_probe_changes_nothing(stand: Path, hermes_stub: Path,
                                      no_logs: Path) -> None:
    """`execute=False` не пишет ни снимка, ни строки метрики, ни комментария (Д7)."""
    make_repo(stand)
    write_log(no_logs)
    seed_killed(hermes_stub)
    metrics_file(stand).parent.mkdir(parents=True, exist_ok=True)
    metrics_file(stand).write_text(json.dumps(dict(ROW, killed_by="runtime"),
                                              ensure_ascii=False) + "\n", encoding="utf-8")
    before = metrics_file(stand).read_bytes()

    gate.run_gate(paths(stand), execute=False, notify_fn=lambda text: None)

    assert not snapshot_file(stand).is_file()
    assert comments(hermes_stub) == []
    assert metrics_file(stand).read_bytes() == before
