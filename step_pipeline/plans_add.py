# -*- coding: utf-8 -*-
"""Добавление плана файлом: системный диалог в потоке, линтер, копия (план 6 S4).

Жест `POST /api/plan/add` открывает системный диалог выбора файла (`tkinter.filedialog`)
в **отдельном потоке**: сервер localhost живёт в своём цикле, и окно, поднятое в потоке
обработчика, заблокировало бы панель до выбора файла. Окно принадлежит той же машине,
поэтому диалог — единственный нативный элемент панели.

Импорт `tkinter` ленивый, внутри `_tk_pick`: пакет грузится в CLI, `doctor` и тестами на
машинах без дисплея, и обязательным требованием для всех `tkinter` быть не должен.
Ошибка импорта — обычное исключение потока, то есть статус `unavailable` и «введите путь
руками».

**Копирование плана не утверждает план.** Файл ложится в каталог планов проекта под именем
`N-слаг.md`; запись утверждения (`plan_N_approval.json`) и ключ владельца `driven_plans`
здесь не пишутся вовсе — утверждение остаётся отдельным жестом владельца (шаг S3A), а план
без утверждения штатно показывается как «не ведётся».

Номер плана берётся из имени источника (`7-сводимость.md` ложится седьмым планом), а без
префикса — первый свободный из 1…99. Занятость считается по трём источникам: файлы
каталога планов, файлы `drafts/` и следы `plan_N_*.json` в каталоге состояния — след без
файла тоже занимает номер, иначе новый план унаследовал бы чужой стоп-флаг.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import plan_accept
    from . import plan_lint
    from . import plans_api
    from . import project as proj
except ImportError:  # pragma: no cover
    import plan_accept  # type: ignore[no-redef]
    import plan_lint  # type: ignore[no-redef]
    import plans_api  # type: ignore[no-redef]
    import project as proj  # type: ignore[no-redef]

DIALOG_TIMEOUT = 120.0        # сколько ждём окно, прежде чем предложить ручной путь
TITLE = "Добавить план: выберите файл .md"
MANUAL = "окно выбора файла недоступно — введите путь руками"
TRACE_RE = re.compile(r"^plan_(\d{1,2})_(?:approval|stop|state|rounds)\.json$")

# Символы, запрещённые в имени файла на Windows, и угловые скобки — в слаг они не идут.
_BAD_CHARS_RE = re.compile(r"[:\"/\\|?*<>]")
_SPACE_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"-{2,}")
SLUG_LIMIT = 60


def _tk_pick(title: str = TITLE) -> str:
    """Открыть системный диалог выбора файла; единственное место импорта `tkinter`."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        return filedialog.askopenfilename(
            title=title,
            filetypes=[("Планы", "*.md"), ("Все файлы", "*.*")],
        ) or ""
    finally:
        root.destroy()


def pick(timeout: float | None = None, dialog=None) -> dict:
    """Спросить путь у владельца, не блокируя сервер дольше `timeout` секунд.

    Диалог инжектируется параметром (`dialog`), а константа таймаута читается в момент
    вызова: тест подменяет атрибут модуля, и в рабочем коде не появляется ветки «если
    тест». Исключение окна глушится здесь, в потоке: необработанное исключение потока
    pytest превращает в ошибку чужого теста.

    Второй `join` после таймаута не делается, окно принудительно не закрывается: `Tk` в
    чужом потоке на части платформ даёт `TclError`, а жест уже ответил «введите путь
    руками» — файл после таймаута не копируется.
    """
    limit = DIALOG_TIMEOUT if timeout is None else float(timeout)
    chooser = dialog or _tk_pick
    box: dict[str, Any] = {"status": "timeout", "path": "", "error": ""}

    def run() -> None:
        try:
            answer = chooser(TITLE)
        except Exception as exc:                            # noqa: BLE001
            box["status"] = "unavailable"
            box["error"] = f"{type(exc).__name__}: {exc}"
            return
        text = str(answer or "").strip()
        box["path"] = text
        box["status"] = "picked" if text else "cancelled"

    thread = threading.Thread(target=run, name="plans-add-dialog", daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        return {"status": "timeout", "path": "", "error": ""}
    return dict(box)


def _taken_numbers(project: Path) -> set[str]:
    """Занятые номера планов: каталог планов, черновики и следы в каталоге состояния.

    Каталог может отсутствовать — это «следов нет», а не ошибка.
    """
    taken: set[str] = set()
    for folder in (plan_accept.plans_dir(project), plan_accept.drafts_dir(project)):
        try:
            entries = list(Path(folder).glob("*.md"))
        except OSError:
            entries = []
        for path in entries:
            number = plan_accept.plan_number(path.name)
            if number:
                taken.add(number)
    try:
        traces = list(plans_api._state_dir(project).iterdir())
    except OSError:
        traces = []
    for path in traces:
        match = TRACE_RE.match(path.name)
        if match:
            taken.add(str(int(match.group(1))))
    return taken


def _slug(name: str) -> str:
    """Слаг имени копии: стем без номера, без запрещённых символов, до 60 символов.

    Кириллица сохраняется как есть: таблицы транслитерации в плагине нет, и второй
    источник имён рядом с `plan_accept._title` шаг не заводит.
    """
    stem = Path(str(name)).stem
    match = plan_accept._PLAN_NUM_RE.match(stem)
    if match:
        stem = stem[match.end():]
    text = _BAD_CHARS_RE.sub("-", stem)
    text = _SPACE_RE.sub("-", text)
    text = _DASH_RE.sub("-", text).strip("-.")
    text = text[:SLUG_LIMIT].strip("-.")
    return text or "plan"


def _holder(project: Path, number: str) -> str:
    """Чем занят номер: путь файла плана, имя черновика, следы или пустая строка."""
    found = plan_accept.find_plan(project, number)
    if found is not None:
        return plan_accept._shown(found, project)
    try:
        drafts = sorted(plan_accept.drafts_dir(project).glob("*.md"))
    except OSError:
        drafts = []
    for path in drafts:
        if plan_accept.plan_number(path.name) == number:
            return plan_accept._shown(path, project)
    if number in _taken_numbers(project):
        return f"следы плана {number} в каталоге состояния"
    return ""


def _number(project: Path, source: Path) -> tuple[str, str]:
    """(номер, чем занят). Номер источника главнее: `7-сводимость.md` ложится седьмым.

    Автонумерация по построению свободна, поэтому без номера источника отказ `occupied`
    не наступил бы никогда, а обещание плана «занятый номер — отказ с причиной» стало бы
    невыполнимым. Возврат «чем занят» — пустая строка значит «номер свободен».
    """
    wanted = plan_accept.plan_number(Path(source).name)
    if wanted:
        return wanted, _holder(project, wanted)
    taken = _taken_numbers(project)
    for value in range(1, 100):
        number = str(value)
        if number not in taken:
            return number, ""
    return "", "свободных номеров 1…99 нет"


def add(project: Path, source: Any = "", *, dialog=None, timeout=None) -> dict:
    """Добавить план файлом: диалог → проверка формата → копия `N-слаг.md`.

    Порядок обязателен, и до последнего пункта не пишется ни одного файла: отказ по
    формату, занятому номеру или источнику не оставляет в проекте мусора. Копия идёт
    через `open(target, "xb")` — занятое имя отказ, а не тихая замена чужого плана;
    частично записанный файл удаляется, исключение уходит наверх (маршрут ответит 500).
    """
    root = Path(project)
    if not proj.read_config(root):
        return {"ok": False, "status": "config",
                "error": f"pipeline.json не читается: {root / proj.MARKER}"}

    raw = str(source or "").strip()
    if not raw:
        outcome = pick(timeout=timeout, dialog=dialog)
        status = outcome.get("status")
        if status == "cancelled":
            return {"ok": False, "status": "cancelled", "error": "выбор файла отменён"}
        if status != "picked":
            answer: dict[str, Any] = {"ok": False, "status": "manual",
                                     "message": MANUAL, "error": MANUAL}
            if outcome.get("error"):
                answer["detail"] = outcome["error"]
            return answer
        raw = str(outcome.get("path") or "").strip()

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return {"ok": False, "status": "missing", "error": f"файл не найден: {raw}"}
    if path.suffix.lower() != ".md":
        return {"ok": False, "status": "bad-file",
                "error": "нужен файл плана с расширением .md: " + path.name}

    problems = plan_lint.lint_project(root, str(path)).problems
    if problems:
        return {"ok": False, "status": "format", "errors": [str(one) for one in problems],
                "error": f"файл не по формату плана: находок {len(problems)} — {problems[0]}"}

    number, holder = _number(root, path)
    if not number:
        return {"ok": False, "status": "no-number", "error": holder}
    if holder:
        return {"ok": False, "status": "occupied", "plan": number,
                "error": f"номер {number} занят: {holder}"}

    folder = plan_accept.plans_dir(root)
    target = folder / f"{number}-{_slug(path.name)}.md"
    folder.mkdir(parents=True, exist_ok=True)
    data = path.read_bytes()
    try:
        handle = open(target, "xb")
    except FileExistsError:            # гонка за номер: имя заняли между проверкой и записью
        return {"ok": False, "status": "occupied", "plan": number,
                "error": f"номер {number} занят: {plan_accept._shown(target, root)}"}
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
    except OSError:
        handle.close()
        target.unlink(missing_ok=True)
        raise

    shown = plan_accept._shown(target, root)
    return {"ok": True, "status": "added", "plan": number, "file": shown,
            "name": target.name, "source": plan_accept._shown(path, root),
            "bytes": len(data), "title": plan_accept._title(target),
            "message": f"план {number} добавлен: {shown}"}
