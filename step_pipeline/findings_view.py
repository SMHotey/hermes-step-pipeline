# -*- coding: utf-8 -*-
"""Реестр находок плана для панели: строки таблицы, а не только счётчик.

Зачем модуль. До этой правки вкладка «Шаг» показывала по ревью плана одну подпись
«5 находок» — из раздела «Находки» отчёта. Сами находки живут в реестре
(`docs/reviews/plan-<N>-findings.md`), и по нему же страж считает вердикт: реестр сильнее
отчёта. Счётчик без текста — это ровно тот случай, когда владелец видит «есть замечания» и
не видит, какие; чинить он их не может.

Почему разбор свой, а не `engine.gate.load_findings`. Панель не импортирует движок: тот
тяжёлый и на битом файле падает `GateError` — страница не должна умирать из-за реестра,
который правит человек. Здесь разбор нарочно терпимый: непонятная строка пропускается,
шапка ищется по ключевым словам колонок, длинные тексты режутся с пометкой. Вердикт по
этому разбору не считается — только показ; считает страж, и он строгий.
"""
from __future__ import annotations

import re
from pathlib import Path

#: Колонки таблицы реестра: ключ → слова, по которым узнаётся заголовок. Порядок колонок в
#: файле может быть любым, поэтому индексы берутся из шапки, а не из предположения.
COLUMNS = {
    "id": "id",
    "step": "шаг",
    "cls": "класс",
    "text": "находка",
    "action": "сделать",
    "status": "статус",
}
#: Статус, которым страж держит вердикт; остальные статусы находку не блокируют.
OPEN = "открыто"
#: Пределы текста: находка бывает на 900 символов, а строк в блоке — десяток.
TEXT_CAP = 900
ACTION_CAP = 400
#: Сколько строк показывать, когда реестр разросся: остальное — счётчиком, не обрезкой молча.
ROWS_CAP = 40


def _cells(line: str) -> list[str]:
    """Ячейки строки таблицы: `| a | b |` → `["a", "b"]` (экранированный `\\|` не делит)."""
    body = str(line or "").strip()
    if not body.startswith("|"):
        return []
    body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", body)]


def _columns(header: list[str]) -> dict[str, int]:
    """Индекс колонки по заголовку; `id` узнаётся по точному совпадению, остальные — по слову."""
    index: dict[str, int] = {}
    lowered = [cell.lower() for cell in header]
    for key, word in COLUMNS.items():
        for position, cell in enumerate(lowered):
            if position in index.values():
                continue
            if key == "id":
                if cell in ("id", "ид"):
                    index[key] = position
                    break
            elif word in cell:
                index[key] = position
                break
    return index


def _cut(text: str, cap: int) -> str:
    """Обрезка с многоточием: читатель видит, что текст короче исходного."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def parse(text: str) -> list[dict]:
    """Строки реестра: `id`, `step`, `cls`, `text`, `action`, `status`, `open`.

    Файла нет — `[]`. Шапки нет — `[]` (реестр не наш формат): панель показывает подпись,
    что строки не разобраны, вместо выдуманного «находок нет».
    """
    lines = str(text or "").splitlines()
    index: dict[str, int] | None = None
    found: list[dict] = []
    for line in lines:
        cells = _cells(line)
        if not cells:
            if index is not None:
                break                      # таблица кончилась: пустая строка после строк
            continue
        if index is None:
            candidate = _columns(cells)
            if {"id", "status"} <= set(candidate):
                index = candidate
            continue
        if all(re.fullmatch(r":?-{2,}:?", cell or "-") for cell in cells):
            continue                        # строка-разделитель шапки
        def cell(key: str) -> str:
            position = index.get(key)  # type: ignore[union-attr]
            return cells[position] if position is not None and position < len(cells) else ""
        row_id = cell("id")
        if not row_id:
            continue
        status = cell("status")
        found.append({
            "id": row_id,
            "step": cell("step"),
            "cls": cell("cls"),
            "text": _cut(cell("text"), TEXT_CAP),
            "action": _cut(cell("action"), ACTION_CAP),
            "status": status,
            "open": status.lower().startswith(OPEN),
        })
    return found


def load(path: Path | str, cap: int = ROWS_CAP) -> dict:
    """Готовый блок для панели: путь, строки, счёт открытых и блокирующих.

    `blocking` — ровно то, по чему страж держит вердикт: статус `открыто`. Остальные
    находки показываются подписью «не блокирует», чтобы счётчик и вердикт не спорили.
    """
    path = Path(path)
    rows: dict = {"exists": False, "path": str(path).replace("\\", "/"), "rows": [],
                  "open": 0, "blocking": 0, "closed": 0, "shown": 0, "note": ""}
    if not path.is_file():
        rows["note"] = "реестра находок плана нет"
        return rows
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        rows["note"] = f"реестр не прочитан: {exc}"
        return rows
    parsed = parse(text)
    rows["exists"] = True
    if not parsed:
        rows["note"] = "строки реестра не разобраны"
        return rows
    opened = [row for row in parsed if row["open"]]
    rows["open"] = len(opened)
    rows["blocking"] = len(opened)
    rows["closed"] = len(parsed) - len(opened)
    # Открытые — первыми: по ним и правят план; закрытые остаются для истории.
    ordered = opened + [row for row in parsed if not row["open"]]
    rows["rows"] = ordered[: max(int(cap or 0), 0)]
    rows["shown"] = len(rows["rows"])
    rows["total"] = len(parsed)
    if len(ordered) > rows["shown"]:
        rows["note"] = f"показаны первые {rows['shown']} из {len(ordered)} находок"
    return rows
