# -*- coding: utf-8 -*-
"""Маркер ручных кругов правок плана, бюджет кругов и граница эпохи ревью.

Правило живёт здесь, а не в движке: движок вендорится в проект, и плагин не импортирует
его как модуль (`AGENTS.md`, «Границы»). Поэтому чтение маркера `plan_<N>_rounds.json`
существует двумя копиями — `engine/gate.py:load_extra_fix_rounds` и этой. Копии обязаны
отвечать одинаково: панель пишет файл, а читает его страж на каждом тике, и разное
толкование одного файла означало бы «кнопка нажата, лимит не вырос». Расхождение ловит
тест-сторож `tests/test_plan_review_progress.py` (сверяет числа обеих копий на одном
файле), а не «поправим тест».

Бюджет кругов — `max_fix_rounds` проекта плюс `extra_fix_rounds` маркера: то же сложение,
что у `gate.plan_fix_budget`. Граница эпохи — `review_reset_at` записи утверждения (её
пишет `plan_accept`), иначе `approved_at`: переутверждение плана начинает счёт кругов
заново, и панель обязана видеть ту же эпоху, что движок.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import plan_accept
from . import project as proj

#: Имя маркера — часть контракта с движком (`gate.ROUNDS_FILE`), менять нельзя.
ROUNDS_FILE = "plan_{plan}_rounds.json"
#: Каталог состояния, если в `pipeline.json` ключа `paths.state` нет (умолчание движка).
DEFAULT_STATE_DIR = ".pipeline/state"
#: Умолчание бюджета, если `max_fix_rounds` не задан (`gate.DEFAULT_FIX_ROUNDS`).
DEFAULT_FIX_ROUNDS = 2
#: Формат `at` в маркере: тот же, что у стоп-флага движка (`gate.py:347-351`) — строка
#: местного времени, а не epoch, чтобы два файла состояния читались одинаково.
AT_FORMAT = "%d.%m.%Y %H:%M:%S"


def state_dir(project) -> Path:
    """Каталог состояния проекта в порядке движка: env → `paths.state` → умолчание.

    Копия правила `plans_api._state_dir` сознательная: `plans_api` импортирует `snapshot`,
    а `snapshot` импортирует этот модуль — импорт дал бы цикл. Порядок источников обязан
    совпадать с движком (`engine/config.py:381-383`): при заданном `PIPELINE_STATE_DIR`
    страж читает маркер оттуда, и панель, пишущая в `paths.state`, не меняла бы ничего.
    """
    root = Path(project)
    raw = os.environ.get("PIPELINE_STATE_DIR") or (proj.read_config(root).get("paths")
                                                  or {}).get("state")
    path = Path(str(raw or DEFAULT_STATE_DIR))
    return path if path.is_absolute() else root / path


def marker_path(project, plan_id) -> Path:
    """Путь маркера: `plan_<N>_rounds.json` в каталоге состояния, номер канонический.

    Номер нормализует `plan_accept` (тот же `02` → `2` и та же ошибка на нецифровом):
    маркер и запись утверждения обязаны называться одним номером, иначе утверждение плана
    «2» открывает бюджет, а маркер ложится рядом как `plan_02_rounds.json`.
    """
    number = plan_accept._normalize(plan_id)
    return state_dir(project) / ROUNDS_FILE.format(plan=number)


def read_marker(project, plan_id) -> dict:
    """Содержимое маркера; нет файла, битый JSON или не словарь — пустой словарь."""
    path = marker_path(project, plan_id)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value, default: int) -> int:
    """Мягкий `int()`: `bool`, `None` и мусор дают умолчание (копия `gate._as_int`)."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def extra_fix_rounds(project, plan_id) -> int:
    """Сколько ручных кругов правок разрешено маркером — правила `gate.load_extra_fix_rounds`.

    `bool` (`true` — не число кругов, хотя `int(True) == 1`), отрицательное, дробное с
    остатком (`1.5`) и мусор → 0; `1.0` и строка из цифр → целое. Битый маркер — не повод
    для исключения: тик идёт с нулём дополнительных кругов, панель показывает бюджет без них.
    """
    value = read_marker(project, plan_id).get("extra_fix_rounds")
    if isinstance(value, bool):
        return 0
    if isinstance(value, float):           # 1.0 — целое, 1.5 — не число кругов
        value = int(value) if value.is_integer() else 0
    number = _as_int(value, 0)
    return number if number > 0 else 0


def budget(cfg: dict, extra) -> int:
    """Бюджет кругов правок плана: `max_fix_rounds` проекта плюс ручные круги маркера.

    Сложение — как у `gate.plan_fix_budget`: у конфига `max(..., 1)` (иначе `0` или мусор
    превращали бы первый отказ плана в исчерпанный бюджет), у маркера `max(..., 0)`
    (минус кругов не бывает). Расхождение с движком показало бы «4 из 4» там, где страж
    видит «4 из 5», и кнопка «Ещё круг правок» появлялась бы на живом лимите.
    """
    raw = cfg.get("max_fix_rounds") if isinstance(cfg, dict) else None
    automatic = max(_as_int(raw, DEFAULT_FIX_ROUNDS), 1)
    return automatic + max(_as_int(extra, 0), 0)


def reset_at(project, plan_id) -> int:
    """Начало эпохи кругов ревью (epoch секунд; 0 — считать не с чего).

    `review_reset_at` пишет `plan_accept` в момент решения владельца; записи старой версии
    плагина этого поля не несут — для них годится `approved_at` (локальное время, формат
    `%Y-%m-%dT%H:%M:%S`). Правило — копия `gate.approval_reset_at`: панель и страж обязаны
    считать эпоху от одного момента, иначе после переутверждения блок показывал бы старые
    круги.
    """
    record = plan_accept._read_approval(project, plan_id) or {}
    explicit = _as_int(record.get("review_reset_at"), 0)
    if explicit:
        return explicit
    stamp = str(record.get("approved_at") or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(time.mktime(time.strptime(stamp, fmt)))
        except ValueError:
            continue
    return 0


def epoch(cards: list[dict], reset_at_value: int) -> list[dict]:
    """Карточки текущей эпохи: `created_at >= reset_at`; границы нет — все карточки.

    Эпоха — та же, что у движка (`gate._plan_review_decision`): переутверждение плана
    начинает счёт кругов заново, а карточки прошлой версии в бюджет не входят.
    """
    rows = [card for card in cards or [] if isinstance(card, dict)]
    if not reset_at_value:
        return rows
    return [card for card in rows if _as_int(card.get("created_at"), 0) >= reset_at_value]


def grant(project, plan_id, reason: str = "") -> dict:
    """Выдать ещё один ручной круг правок: `extra_fix_rounds + 1` в маркере.

    Жест владельца, поэтому пишется на диск сразу и атомарно (временный файл рядом,
    `os.replace`): следующий тик читает маркер из файла, а не из памяти панели. `reason`
    и `at` движок не читает — они для человека, который откроет маркер руками.
    """
    current = extra_fix_rounds(project, plan_id)
    payload = {
        "extra_fix_rounds": current + 1,
        "at": time.strftime(AT_FORMAT),
        "reason": str(reason or "ручной круг правок из панели"),
    }
    path = marker_path(project, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return payload
