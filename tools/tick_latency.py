# -*- coding: utf-8 -*-
"""Замер простоев конвейера: сколько времени уходит на ожидание стража, а не на работу.

Зачем. Тик стража — опрос борды, а не работа: модель работает в карточках. Значит цена
интервала тика — это простой между «карточка закрыта» и «страж заметил». Замер 17.11.2026
на проекте конвейера: медиана простоя 4.3 мин при медиане исполнения карточки 17.4 мин — то
есть на 268 переходах набежало около 19 часов ожидания. Числа, а не впечатления: инструмент
печатает их и пишет отчёт.

Источники: `.pipeline/state/history.jsonl` проекта (действия стража с временем и
длительностью) и `kanban.db` борды (создание, взятие в работу, закрытие карточек).

Запуск:

    python tools/tick_latency.py --project C:/Users/malik/PycharmProjects/self-analyzer
    python tools/tick_latency.py --project <путь> --since "2026-11-17T23:54" --write

`--since/--until` — границы окна (местное время, минута точности): так сравниваются «до» и
«после» правки расписания. `--write` кладёт markdown в `docs/reports/` проекта.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

#: Ключ карточек конвейера в борде: `planloop:<план>:<вид>[:<номер>]`. Им отсеиваются чужие
#: карточки доски (уборка, ручные заказы, служебные прогоны).
KEY_PREFIX = "planloop"


def _epoch(value) -> float | None:
    """Время в секундах: в базе оно встречается и в секундах, и в миллисекундах."""
    if value in (None, ""):
        return None
    number = float(value)
    return number / 1000 if number > 10 ** 11 else number


def _stamp(text: str | None) -> float | None:
    """Граница окна из аргумента: `2026-11-17T23:54` (местное время) → секунды."""
    if not text:
        return None
    return datetime.fromisoformat(text).timestamp()


def _project(project: Path) -> tuple[Path, str]:
    """Корень проекта и имя борды: борда берётся из `pipeline.json`, а не из имени каталога."""
    cfg = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    board = str(cfg.get("board") or project.name)
    return project, board


def _board_db(project: Path, board: str) -> Path:
    """База борды в домашнем каталоге Hermes (профиль берётся из общего реестра)."""
    home = Path.home() / "AppData" / "Local" / "hermes"
    path = home / "kanban" / "boards" / board / "kanban.db"
    if not path.is_file():
        raise SystemExit(f"нет базы борды: {path}")
    return path


def _history(project: Path) -> list[dict]:
    """Действия стража, по возрастанию времени; строки без времени отбрасываются."""
    path = project / ".pipeline" / "state" / "history.jsonl"
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _epoch(row.get("ts")):
            rows.append(row)
    return sorted(rows, key=lambda row: _epoch(row["ts"]) or 0)


def _cards(db: Path) -> list[dict]:
    """Карточки конвейера: время создания, взятия и закрытия."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select id, idempotency_key, created_at, started_at, completed_at, status"
            " from tasks where idempotency_key like ?", (f"{KEY_PREFIX}%",)).fetchall()
    finally:
        conn.close()
    return [{"id": row[0], "key": row[1], "created": _epoch(row[2]),
             "started": _epoch(row[3]), "completed": _epoch(row[4]), "status": row[5]}
            for row in rows]


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _p90(values: list[float]) -> float:
    """Девяностый процентиль: у коротких выборок — максимум, а не падение на индексе."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) < 10:
        return ordered[-1]
    return statistics.quantiles(ordered, n=10)[8]


def measure(project: Path, since: float | None = None,
            until: float | None = None) -> dict:
    """Собрать метрики окна: простой стража, диспетчер, исполнение карточек."""
    project, board = _project(project)
    history = _history(project)
    cards = _cards(_board_db(project, board))

    def inside(moment: float | None) -> bool:
        if moment is None:
            return False
        return ((since is None or moment >= since) and (until is None or moment <= until))

    # 1. Простой: карточка закрыта → следующее действие стража. Меряется по парам «действие
    #    с упоминанием карточки» и «следующее действие»: другого следа связки у нас нет.
    import re
    pattern = re.compile(r"t_[0-9a-f]{8}")
    by_id = {card["id"]: card for card in cards}
    idle: list[float] = []
    for position, row in enumerate(history[:-1]):
        match = pattern.search(str(row.get("detail") or ""))
        if not match:
            continue
        card = by_id.get(match.group(0))
        follow = _epoch(history[position + 1].get("ts"))
        if not card or not card["completed"] or follow is None:
            continue
        if not inside(follow):
            continue
        gap = follow - card["completed"]
        if -60 <= gap <= 7200:
            idle.append(gap)

    # 2. Тик стража: длительность самого действия из истории.
    ticks = [float(row["duration_s"]) for row in history
             if row.get("duration_s") and inside(_epoch(row.get("ts")))]

    # 3. Диспетчер и исполнение: вся карточка — от заказа до закрытия.
    dispatch = [card["started"] - card["created"] for card in cards
                if card["created"] and card["started"] and card["started"] >= card["created"]
                and inside(card["created"])]
    run = [card["completed"] - card["started"] for card in cards
           if card["started"] and card["completed"] and card["completed"] >= card["started"]
           and inside(card["started"])]

    # 4. Ритм стража: зазоры между его действиями (по ним видно, насколько тик частый).
    stamps = [_epoch(row.get("ts")) for row in history]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])
            if a and b and 0 < b - a < 7200 and inside(b)]

    fast = sum(1 for gap in idle if gap <= 60)
    return {
        "project": str(project), "board": board,
        "since": since, "until": until,
        "transitions": len(idle),
        "idle_median": _median(idle), "idle_mean": (statistics.mean(idle) if idle else 0.0),
        "idle_p90": _p90(idle),
        "idle_fast_share": (100 * fast / len(idle)) if idle else 0.0,
        "tick_median": _median(ticks), "ticks": len(ticks),
        "dispatch_median": _median(dispatch), "dispatch_p90": _p90(dispatch),
        "run_median": _median(run), "run_p90": _p90(run),
        "gap_median": _median(gaps), "gap_p90": _p90(gaps),
        "cards": len(cards), "idle_total_h": sum(idle) / 3600,
    }


def _minutes(seconds: float) -> float:
    return seconds / 60


def report(rows: dict) -> str:
    """Текст отчёта: числа в минутах, доли в процентах — без оценок и прилагательных."""
    window = ""
    if rows["since"] or rows["until"]:
        start = (datetime.fromtimestamp(rows["since"]).strftime("%d.%m.%Y %H:%M")
                 if rows["since"] else "начало")
        end = (datetime.fromtimestamp(rows["until"]).strftime("%d.%m.%Y %H:%M")
               if rows["until"] else "сейчас")
        window = f" окно: {start} — {end}"
    lines = [
        f"# Простои конвейера: {rows['project']} (борда {rows['board']}){window}",
        "",
        f"- переходов «карточка закрыта → действие стража»: {rows['transitions']}",
        f"- простой после закрытия карточки: медиана {_minutes(rows['idle_median']):.1f} мин,"
        f" среднее {_minutes(rows['idle_mean']):.1f} мин,"
        f" p90 {_minutes(rows['idle_p90']):.1f} мин",
        f"- уложилось в минуту: {rows['idle_fast_share']:.0f}%",
        f"- суммарный простой: {rows['idle_total_h']:.1f} ч",
        f"- длительность тика: медиана {rows['tick_median']:.2f} с ({rows['ticks']} замеров)",
        f"- диспетчер (создана → взята): медиана {_minutes(rows['dispatch_median']):.2f} мин,"
        f" p90 {_minutes(rows['dispatch_p90']):.1f} мин",
        f"- исполнение карточки: медиана {_minutes(rows['run_median']):.1f} мин,"
        f" p90 {_minutes(rows['run_p90']):.1f} мин",
        f"- зазор между действиями стража: медиана {_minutes(rows['gap_median']):.1f} мин,"
        f" p90 {_minutes(rows['gap_p90']):.1f} мин",
        f"- карточек конвейера в борде: {rows['cards']}",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Простои конвейера: где ждём, а где работаем")
    parser.add_argument("--project", default=".", help="каталог проекта конвейера")
    parser.add_argument("--since", default="", help="начало окна, местное время (2026-11-17T23:54)")
    parser.add_argument("--until", default="", help="конец окна, местное время")
    parser.add_argument("--write", action="store_true", help="положить отчёт в docs/reports")
    args = parser.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    rows = measure(project, _stamp(args.since), _stamp(args.until))
    text = report(rows)
    print(text, end="")
    if args.write:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        target = project / "docs" / "reports" / f"{stamp}_prostoi-konvejera.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"отчёт: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
