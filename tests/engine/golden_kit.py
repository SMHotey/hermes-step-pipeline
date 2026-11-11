# -*- coding: utf-8 -*-
"""Golden-вердикты: фиксированные состояния проекта + ожидаемое решение стража.

Зачем: любая правка движка (перенос кода, новая ветка решения, смена формата журнала)
обязана не менять поведение. Пока это проверяли только утверждения в тестах «по месту»,
регрессия в непокрытой ветке проходила молча. Здесь состояние проекта и решение стража
лежат в репозитории как данные:

    tests/engine/golden/<случай>/project/   — файлы проекта (план, постановки, ревью, состояние)
    tests/engine/golden/<случай>/case.json  — как собрать: карточки на борде, что писать после коммита
    tests/engine/golden/<случай>/expected.json — записанный вердикт (эталон)

Запуск: `pytest tests/engine/test_golden_verdicts.py`. Перезапись эталонов после
осознанного изменения поведения: `GOLDEN_UPDATE=1 pytest tests/engine/test_golden_verdicts.py`.

В эталон попадает только то, что относится к решению: действие стража, созданные
карточки, стоп-флаг, строки журнала плана. Время, pid, абсолютные пути и sha коммитов
нормализуются — иначе эталон «дрожал» бы на каждом прогоне.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from engine_kit import ENGINE, ENGINE_FILES, KIT

GOLDEN = Path(__file__).resolve().parent / "golden"
UPDATE_ENV = "GOLDEN_UPDATE"
HEAD_PLACEHOLDER = "{{HEAD}}"
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
DATE_RE = re.compile(r"\b20\d\d-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?\b")
RU_DATE_RE = re.compile(r"\b\d\d\.\d\d\.20\d\d\b")
CARD_ID_RE = re.compile(r"\bt_[0-9a-f]{6,}\b")


def load_case(case_dir: Path) -> dict:
    """`case.json` случая: как собрать проект и сколько тиков прогнать."""
    path = case_dir / "case.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def case_dirs() -> list[Path]:
    """Все случаи корпуса (каталоги с `case.json`), порядок — по имени."""
    if not GOLDEN.is_dir():
        return []
    return sorted(path for path in GOLDEN.iterdir() if (path / "case.json").is_file())


def _write_config(root: Path, case: dict) -> None:
    """`pipeline.json` случая: шаблон плагина + минимум, нужный стражу."""
    data = json.loads((KIT / "step_pipeline" / "templates" / "pipeline.json.example")
                      .read_text(encoding="utf-8"))
    data.update({"project": "golden", "board": "testboard", "max_actions": 1})
    data["profiles"] = {"driver": "demo", "prompter": "prompter", "reviewer": "reviewer"}
    data["routing"] = {area: "demo" for area in
                       ("frontend", "backend", "devops", "designer", "security")}
    data["git"] = {"commit": True, "push": False}
    data["notify"] = {"enabled": False}
    data.update(case.get("config") or {})
    (root / "pipeline.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")


def _copy_overlay(case_dir: Path, root: Path, *, skip: tuple[str, ...] = ()) -> None:
    """Скопировать `project/` случая поверх проекта.

    Файлы с `{{HEAD}}` (отчёт ревью ссылается на коммит) в проект не попадают:
    их пишет `build_project` уже после коммита, иначе sha не существует.
    """
    src = case_dir / "project"
    if not src.is_dir():
        return
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if rel in skip:
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def _write_approval(root: Path, plan_file: Path) -> None:
    """Запись утверждения плана — та же, что кладёт владелец (`plan_accept`)."""
    sys.path.insert(0, str(ENGINE))
    import approval  # noqa: PLC0415 - движок на пути только ради этого

    number = plan_file.name.split("-")[0]
    path = root / ".pipeline" / "state" / f"plan_{number}_approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan": number,
               "file": plan_file.relative_to(root).as_posix(),
               "sha256": approval.sha256_file(plan_file),
               "version_sha256": approval.plan_version_sha(plan_file),
               "approved_at": "2026-01-01T00:00:00",
               "profile": "golden"}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"git {args} упал: {res.stderr or res.stdout}")
    return res.stdout.strip()


def _seed_cards(db: Path, cards: list[dict]) -> None:
    """Карточки на борде до тика: «шаг выполнен», «ревью завершено» и т. п."""
    if not cards:
        return
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
                    " status TEXT DEFAULT 'ready', assignee TEXT, idempotency_key TEXT,"
                    " completed_at INTEGER, result TEXT, last_failure_error TEXT,"
                    " created_at INTEGER, tenant TEXT, workspace TEXT, max_runtime TEXT)")
        for index, card in enumerate(cards):
            con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                        " idempotency_key, completed_at, result, created_at, tenant,"
                        " workspace, max_runtime) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (f"card-{index}", card.get("title", ""), card.get("status", "done"),
                         card.get("assignee", "demo"), card["key"],
                         card.get("completed_at", 1), card.get("result", "ok"),
                         index, "plan", "", card.get("max_runtime", "")))
        con.commit()
    finally:
        con.close()


def build_project(case_dir: Path, root: Path, case: dict, db: Path) -> str:
    """Собрать проект случая: движок, конфиг, файлы, коммит, отчёты после коммита.

    Возвращает sha начального коммита: по нему подставляется `{{HEAD}}` в отчётах.
    """
    (root / ".pipeline").mkdir(parents=True, exist_ok=True)
    for name in ENGINE_FILES:
        shutil.copy2(ENGINE / name, root / ".pipeline" / name)
    _write_config(root, case)

    untracked = tuple(case.get("untracked") or ())
    _copy_overlay(case_dir, root, skip=untracked)
    for rel in ("docs/plans", "docs/prompts", "docs/reviews"):
        (root / rel).mkdir(parents=True, exist_ok=True)

    plan_file = root / "docs" / "plans" / str(case.get("plan") or "1-demo.md")
    if case.get("approved", True) and plan_file.is_file():
        _write_approval(root, plan_file)

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "golden@example.com")
    _git(root, "config", "user.name", "Golden")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "golden: исходное состояние")
    head = _git(root, "rev-parse", "HEAD")

    # Отчёты ревью ссылаются на коммит — значит появляются только после него.
    for rel in untracked:
        src = case_dir / "project" / rel
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8").replace(HEAD_PLACEHOLDER, head)
        dest.write_text(text, encoding="utf-8")

    _seed_cards(db, case.get("seed_cards") or [])
    return head


def run_ticks(root: Path, count: int) -> list[dict]:
    """Прогнать `gate.py --run --json` нужное число раз и собрать ответы.

    Перед JSON движок может напечатать служебные строки (`[commit] …`, `[notify] …`),
    поэтому берём последний разбираемый объект из вывода, а не весь stdout.
    """
    decoder = json.JSONDecoder()
    results = []
    for _ in range(max(1, int(count))):
        res = subprocess.run([sys.executable, str(root / ".pipeline" / "gate.py"),
                              "--run", "--json"], cwd=str(root), capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"тик упал ({res.returncode}): {res.stderr or res.stdout}")
        payload = None
        for index, char in enumerate(res.stdout):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(res.stdout[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "decision" in payload:
                break
            payload = None
        if payload is None:
            raise RuntimeError(f"тик не вернул JSON: {res.stdout[:400]!r} {res.stderr[:400]!r}")
        results.append(payload)
    return results


def _normalize(text: object, root: Path) -> str:
    """Убрать из строки всё, что меняется от прогона к прогону.

    Маскируются: корень проекта (временный каталог), sha коммитов, идентификаторы
    карточек борды-заглушки, даты (`11.11.2026` в журнале, ISO-метки в состоянии).
    Всё остальное — текст причины, действие, имена карточек — остаётся как есть:
    именно он и есть контракт стража.
    """
    value = str(text or "")
    for variant in {str(root), root.as_posix(), str(root).replace("\\", "\\\\")}:
        value = value.replace(variant, "<root>")
    value = value.replace("\\", "/")
    value = CARD_ID_RE.sub("<card>", value)
    value = SHA_RE.sub("<sha>", value)
    value = RU_DATE_RE.sub("<date>", value)
    return DATE_RE.sub("<time>", value)


def _journal(plan_file: Path, root: Path) -> list[str]:
    """Строки журнала плана: единственный артефакт, который пишет сам страж."""
    if not plan_file.is_file():
        return []
    rows = []
    for line in plan_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and "---" not in stripped:
            rows.append(_normalize(re.sub(r"\s+", " ", stripped), root))
    return rows


def _cards(db: Path) -> list[dict]:
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT idempotency_key, assignee, status, title, max_runtime"
                           " FROM tasks ORDER BY idempotency_key").fetchall()
    finally:
        con.close()
    return [{"key": key, "assignee": assignee, "status": status, "title": title,
             "max_runtime": runtime}
            for key, assignee, status, title, runtime in rows]


def _stop(root: Path) -> dict | None:
    path = root / ".pipeline" / "state" / "plan_1_stop.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {"kind": payload.get("kind") or "incident",
            "reason": _normalize(payload.get("reason"), root)}


def _tick_record(result: dict, root: Path) -> dict:
    """Сухая выжимка ответа тика: решение, действия, причина."""
    decision = result.get("decision") or {}
    return {
        "applied": bool(result.get("applied")),
        "plan_id": result.get("plan_id"),
        "action": decision.get("action"),
        "decision_reason": _normalize(decision.get("reason"), root),
        "detail": _normalize(result.get("detail"), root),
        "reason": _normalize(result.get("reason"), root),
        "incident_cleared": bool(result.get("incident_cleared")),
        "actions": [{"plan": item.get("plan"), "action": item.get("action"),
                     "step": item.get("step"), "applied": bool(item.get("applied")),
                     "detail": _normalize(item.get("detail"), root)}
                    for item in (result.get("actions") or [])],
    }


def record(case_dir: Path, root: Path, db: Path, ticks: list[dict]) -> dict:
    """Эталон случая: что страж решил и что после этого стало с планом и бордой."""
    plan_file = root / "docs" / "plans" / str(load_case(case_dir).get("plan") or "1-demo.md")
    return {
        "ticks": [_tick_record(result, root) for result in ticks],
        "cards": _cards(db),
        "stop": _stop(root),
        "journal": _journal(plan_file, root),
    }


def expected_path(case_dir: Path) -> Path:
    return case_dir / "expected.json"


def dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def diff_paths(expected, actual, prefix: str = "") -> list[str]:
    """Где именно эталон разошёлся с фактом — по строкам, а не «не равно»."""
    problems: list[str] = []
    if type(expected) is not type(actual):
        return [f"{prefix}: тип {type(expected).__name__} → {type(actual).__name__}"]
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                problems.append(f"{prefix}.{key}: появилось {actual[key]!r}")
            elif key not in actual:
                problems.append(f"{prefix}.{key}: пропало {expected[key]!r}")
            else:
                problems += diff_paths(expected[key], actual[key], f"{prefix}.{key}")
        return problems
    if isinstance(expected, list):
        if len(expected) != len(actual):
            problems.append(f"{prefix}: элементов {len(expected)} → {len(actual)}")
            return problems
        for index, (left, right) in enumerate(zip(expected, actual)):
            problems += diff_paths(left, right, f"{prefix}[{index}]")
        return problems
    if expected != actual:
        problems.append(f"{prefix}: {expected!r} → {actual!r}")
    return problems


def update_mode() -> bool:
    return (os.environ.get(UPDATE_ENV) or "").strip() not in ("", "0")
