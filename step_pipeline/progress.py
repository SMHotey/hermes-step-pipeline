# -*- coding: utf-8 -*-
"""Прогресс шага и плана для панели: проценты этапов, находки ревью, остаток времени.

Модуль только считает по уже прочитанным данным (карточки борды, попытки, отчёт ревью):
ничего не пишет, движок не запускает и не импортирует — инвариант `docs/CONTEXT.md`.

Правила:

P1. Процент шага считается по «каноническому пути»: постановка → исполнение → ревью →
    журнал. Вес этапа — его доля в длительности шага, а не число карточек.
P2. Веса учатся в процессе: когда у этапа набралось `min_samples` замеров (закрытых
    карточек плана), веса пересчитываются из медиан этих замеров. У журнала карточки нет,
    поэтому его вес остаётся из конфигурации, а остальные делят остаток. Замеров мало —
    умолчание (`DEFAULT_WEIGHTS`). Процент при этом ничем не отличается по смыслу: это
    доля пройденного пути, а веса лишь говорят, какой этап типично длиннее.
P3. Круг правок полосу не откатывает: канонический путь считается один раз, а каждый
    круг идёт отдельным отрезком со своим процентом (правки → повторное ревью).
P4. Шаг закрыт журналом — 100% независимо от карточек; открытый шаг 100% не показывает
    (потолок `OPEN_PERCENT`), иначе «100%» и «шаг не закрыт» противоречили бы друг другу.
P5. Идущий этап даёт часть веса: доля потраченного времени от медианы, но не больше
    `LIVE_MAX`; медианы нет — `LIVE_SHARE`.
P6. Находки ревью считаются по отчёту шага; разбор не удался — числа нет (`total=None`),
    а не ноль: ноль читался бы как «чисто».
P7. Остаток времени — вилка по медианам: сумма медиан оставшихся этапов (у идущего —
    остаток от медианы) плюс открытые круги правок. Замеров меньше `min_samples` —
    оценка есть, вилки нет и стоит пометка «грубо». Замеров нет вообще — оценки нет.
"""
from __future__ import annotations

import time

#: Минимум замеров на этап, чтобы веса считались выученными, а вилка — по квартилям.
MIN_SAMPLES = 3
#: Веса этапов канонического пути, процентов (сумма — 100): пока замеров мало.
DEFAULT_WEIGHTS = {"prompt": 10.0, "step": 55.0, "review": 25.0, "journal": 10.0}
#: Вес правок в круге, пока замеров правок мало.
DEFAULT_FIX = 15.0
#: Доля веса идущего этапа без медианы и потолок доли по медиане.
LIVE_SHARE = 0.5
LIVE_MAX = 0.9
#: Потолок процента открытого шага.
OPEN_PERCENT = 99
#: Канонический путь шага: журнал закрывает шаг, карточки у него нет.
CANON = ("prompt", "step", "review", "journal")
TITLES = {"prompt": "Постановка", "step": "Исполнение", "review": "Ревью",
          "journal": "Журнал", "fix": "Правки", "round": "Круг правок"}
STATE_LABELS = {"done": "готово", "run": "идёт", "wait": "ждёт", "bad": "сорвалось"}
#: Суффикс ключа карточки → базовый этап (`planloop:<план>:<шаг>:<этап>[:<номер>]`).
ALIASES = {"prompt": "prompt", "promptreview": "prompt", "promptfix": "prompt",
           "step": "step", "review": "review", "rereview": "review", "fix": "fix"}
#: Статусы незавершённой карточки, которые означают сбой, а не работу.
BAD_STATUSES = {"crashed", "timed_out", "gave_up", "spawn_failed", "blocked", "rate_limited"}
#: Статусы карточки в очереди: работа ещё не начиналась, доля этапа не растёт.
QUEUED_STATUSES = {"", "ready", "todo", "queued", "pending", "scheduled"}
#: Замер дольше недели — не длительность этапа, а простой борды: в медианы не идёт.
MAX_SAMPLE_SECONDS = 7 * 86400


# ────────────────────────────── вспомогательное ──────────────────────────────

def _ts(value) -> float | None:
    """Числовая метка времени или None: `bool`, строки и мусор меткой не считаются."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _duration(seconds) -> str:
    """Длительность по-русски: «4 ч 10 мин», «45 мин», «меньше минуты».

    Формат повторяет `snapshot._duration`: импортировать его отсюда нельзя —
    `snapshot` сам импортирует этот модуль.
    """
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "меньше минуты"
    hours, minutes = total // 3600, total % 3600 // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _noun(count: int, one: str, few: str, many: str) -> str:
    """Форма слова по числу: 1 находка, 2 находки, 5 находок."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def stage_of(key) -> str:
    """Базовый этап карточки по ключу идемпотентности; пусто — ключ не этапа шага."""
    parts = str(key or "").split(":")
    if len(parts) < 4 or parts[0] != "planloop":
        return ""
    return ALIASES.get(parts[3], "")


def step_of(key) -> str:
    """Номер шага из ключа `planloop:<план>:<шаг>:…` в верхнем регистре."""
    parts = str(key or "").split(":")
    if len(parts) < 3 or parts[0] != "planloop":
        return ""
    return parts[2].strip().upper()


def group_by_step(cards) -> dict[str, list[dict]]:
    """Карточки по шагам плана: ключ — номер шага в верхнем регистре."""
    grouped: dict[str, list[dict]] = {}
    for card in cards or []:
        step_id = step_of(card.get("idempotency_key"))
        if step_id:
            grouped.setdefault(step_id, []).append(card)
    return grouped


def _card_start(card: dict, runs: list[dict]) -> float | None:
    """Начало карточки: `started_at`, иначе первая попытка, иначе `created_at`."""
    started = _ts(card.get("started_at"))
    if started is not None:
        return started
    starts = [_ts(run.get("started_at")) for run in runs or []]
    starts = [value for value in starts if value is not None]
    return min(starts) if starts else _ts(card.get("created_at"))


def _card_seconds(card: dict, runs: list[dict]) -> float | None:
    """Замер длительности закрытой карточки, секунд; открытая — None (замера нет)."""
    end = _ts(card.get("completed_at"))
    if end is None:
        return None
    start = _card_start(card, runs)
    if start is None or end < start:
        return None
    seconds = end - start
    return seconds if seconds <= MAX_SAMPLE_SECONDS else None


def _quantile(values: list[float], share: float) -> float:
    """Квантиль по ближайшему рангу: для трёх замеров даёт сам замер, а не среднее."""
    ordered = sorted(values)
    index = int(round(share * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


def stats_of(values) -> dict:
    """Медиана, квартили и число замеров; пусто — все None и `n=0`."""
    numbers = [float(value) for value in values or [] if value is not None]
    if not numbers:
        return {"n": 0, "median": None, "p25": None, "p75": None}
    return {"n": len(numbers), "median": _quantile(numbers, 0.5),
            "p25": _quantile(numbers, 0.25), "p75": _quantile(numbers, 0.75)}


def samples(cards, runs) -> dict[str, list[float]]:
    """Замеры по этапам: длительности закрытых карточек плана."""
    measured: dict[str, list[float]] = {"prompt": [], "step": [], "review": [], "fix": []}
    for card in cards or []:
        stage = stage_of(card.get("idempotency_key"))
        if stage not in measured:
            continue
        seconds = _card_seconds(card, runs.get(str(card.get("id") or "")) or [])
        if seconds is not None:
            measured[stage].append(seconds)
    return measured


# ────────────────────────────────── веса ──────────────────────────────────

def _ui_settings(ui: dict | None) -> tuple[dict, int, bool]:
    """Настройки прогресса из `pipeline.json → ui.progress`: веса, порог, обучение."""
    raw = ui if isinstance(ui, dict) else {}
    base = dict(DEFAULT_WEIGHTS)
    for stage, value in (raw.get("weights") or {}).items():
        if stage in base:
            try:
                base[stage] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    if sum(base.values()) <= 0:
        base = dict(DEFAULT_WEIGHTS)
    try:
        min_samples = max(1, int(raw.get("min_samples") or MIN_SAMPLES))
    except (TypeError, ValueError):
        min_samples = MIN_SAMPLES
    return base, min_samples, raw.get("learn") is not False


def weights(measured: dict | None = None, ui: dict | None = None) -> dict:
    """Веса этапов и основа для них: выученные по медианам или умолчание (P2).

    Журнал делит шкалу с остальными, но своего замера не имеет: его вес берётся из
    конфигурации (по умолчанию 10%), а оставшиеся проценты делят постановка, исполнение
    и ревью — пропорционально медианам их длительностей.
    """
    base, min_samples, learn = _ui_settings(ui)
    measured = measured or {}
    stats = {stage: stats_of(measured.get(stage) or []) for stage in ("prompt", "step", "review")}
    fix_stats = stats_of(measured.get("fix") or [])
    journal = base["journal"]
    rest = max(0.0, 100.0 - journal)
    learned = (learn and all(stats[stage]["n"] >= min_samples and stats[stage]["median"]
                             for stage in stats))
    if learned:
        total = sum(stats[stage]["median"] for stage in stats) or 1.0
        canonical = {stage: rest * stats[stage]["median"] / total for stage in stats}
        fix = (rest * fix_stats["median"] / total
               if fix_stats["n"] >= min_samples and fix_stats["median"] else DEFAULT_FIX)
        basis = (f"веса по замерам ({sum(row['n'] for row in stats.values())} карточек): "
                 f"постановка {canonical['prompt']:.0f}%, исполнение {canonical['step']:.0f}%, "
                 f"ревью {canonical['review']:.0f}%")
    else:
        canonical = {stage: base[stage] for stage in ("prompt", "step", "review")}
        fix = base.get("fix", DEFAULT_FIX)
        need = ", ".join(f"{TITLES[stage].lower()} {stats[stage]['n']}/{min_samples}"
                         for stage in stats)
        basis = f"веса по умолчанию: замеров мало ({need})"
    result = {stage: float(canonical.get(stage, base[stage])) for stage in ("prompt", "step", "review")}
    result["journal"] = float(journal)
    result["fix"] = float(fix)
    result["round"] = float(result["review"])       # повторное ревью весит как первое
    result["basis"] = basis
    result["learned"] = bool(learned)
    result["min_samples"] = min_samples
    result["samples"] = {stage: row["n"] for stage, row in stats.items()}
    result["median"] = {stage: row["median"] for stage, row in stats.items()}
    result["median"]["fix"] = fix_stats["median"]
    result["p25"] = {stage: row["p25"] for stage, row in stats.items()}
    result["p75"] = {stage: row["p75"] for stage, row in stats.items()}
    return result


# ───────────────────────────────── находки ─────────────────────────────────

def findings(review: dict | None) -> dict:
    """Счётчик находок отчёта ревью: вердикт, всего, блокирующих, разбор удался ли (P6)."""
    review = review or {}
    rows = review.get("findings") or []
    verdict = str(review.get("verdict") or "NONE").upper()
    exists = bool(review.get("exists"))
    unparsed = bool(exists and not rows and review.get("findings_note"))
    blocking = sum(1 for row in rows
                   if "блокир" in f"{row.get('class') or ''} {row.get('level') or ''}".lower())
    row = {"exists": exists, "verdict": verdict, "total": None,
           "blocking": None, "unparsed": unparsed, "label": "", "note": ""}
    if not exists:
        row["note"] = str(review.get("note") or "отчёта ревью нет")
        return row
    if unparsed:
        row["label"] = "находки не разобраны"
        row["note"] = str(review.get("findings_note") or "")
        return row
    row["total"] = len(rows)
    row["blocking"] = blocking
    parts = [verdict if verdict in ("APPROVE", "REQUEST_CHANGES") else "вердикт не распознан"]
    if row["total"]:
        parts.append(f"{row['total']} {_noun(row['total'], 'находка', 'находки', 'находок')}")
    if blocking:
        parts.append(f"{blocking} блокир.")
    row["label"] = " · ".join(parts)
    return row


# ─────────────────────────────── прогресс шага ───────────────────────────────

def _stage_state(cards: list[dict], runs: dict, median, live_share: float,
                 now: float) -> tuple[str, float]:
    """(состояние, доля выполнения) для набора карточек одного этапа (P5).

    Карточка закрыта — этап выполнен. Открытая и живая — «идёт»: доля по потраченному
    времени от медианы. Открытая со сбоем — «сорвалось»: доля не растёт.
    """
    closed = [card for card in cards or [] if _ts(card.get("completed_at")) is not None]
    if closed:
        return "done", 1.0
    live = [card for card in cards or [] if _ts(card.get("completed_at")) is None]
    if not live:
        return "wait", 0.0
    current = live[-1]
    status = str(current.get("status") or "").strip().lower()
    if status in BAD_STATUSES:
        return "bad", 0.0
    # Карточка в очереди работой не считается: время ожидания слота — не прогресс этапа.
    if status in QUEUED_STATUSES and _ts(current.get("started_at")) is None:
        return "wait", 0.0
    start = _card_start(current, runs.get(str(current.get("id") or "")) or [])
    if start is None:
        return "run", live_share
    elapsed = max(0.0, now - start)
    share = min(LIVE_MAX, elapsed / median) if median else live_share
    return "run", max(0.0, share)


def _tile(stage: str, state: str, share: float, hint: str) -> dict:
    return {"stage": stage, "title": TITLES[stage], "state": state,
            "share": round(float(share), 3), "percent": int(round(100 * share)),
            "label": STATE_LABELS.get(state, state), "hint": hint}


def _eta(weights: dict, shares: dict, live_elapsed: dict, open_rounds: int) -> dict:
    """Остаток времени по медианам этапов (P7): секунды, вилка и основа расчёта.

    Считаются только непройденные этапы канонического пути (журнал работы не требует) и
    открытые круги правок: правки плюс повторное ревью. Вилка — по квартилям, но не уже
    самой оценки: сумма квартилей разных этапов может оказаться меньше медианной.
    """
    medians = weights.get("median") or {}
    p25, p75 = weights.get("p25") or {}, weights.get("p75") or {}
    known = [medians.get(stage) for stage in ("prompt", "step", "review", "fix")
             if medians.get(stage)]
    if not known:
        return {"seconds": None, "text": "", "low_text": "", "high_text": "",
                "basis": "", "note": "замеров нет — оценка появится после первых "
                                     "закрытых карточек"}
    fallback = sum(known) / len(known)
    need = int(weights.get("min_samples") or MIN_SAMPLES)
    counts = weights.get("samples") or {}
    exact = all(medians.get(stage) and int(counts.get(stage) or 0) >= need
                for stage in ("prompt", "step", "review"))
    total = low = high = 0.0
    for stage in CANON:
        if stage == "journal" or shares.get(stage, 0.0) >= 1.0:
            continue
        median = medians.get(stage) or fallback
        left = median
        if stage in live_elapsed and shares.get(stage, 0.0) > 0.0:
            left = max(0.0, median - float(live_elapsed[stage]))
        total += left
        low += p25.get(stage) if p25.get(stage) is not None else left
        high += p75.get(stage) if p75.get(stage) is not None else left
    for _ in range(max(0, int(open_rounds))):
        fix = medians.get("fix") or weights.get("fix") or DEFAULT_FIX * 60
        review = medians.get("review") or fallback
        total += fix + review
        low += (p25.get("fix") if p25.get("fix") is not None else fix * 0.5) + \
               (p25.get("review") if p25.get("review") is not None else review * 0.5)
        high += (p75.get("fix") if p75.get("fix") is not None else fix * 1.5) + \
                (p75.get("review") if p75.get("review") is not None else review * 1.5)
    note = "" if exact else "часть этапов без замеров — вилка грубая"
    if not exact:
        low, high = total * 0.5, total * 1.5
    else:
        low, high = min(low, total), max(high, total)
    return {"seconds": int(round(total)), "text": "~" + _duration(total),
            "low_text": _duration(low), "high_text": _duration(high),
            "basis": str(weights.get("basis") or ""), "note": note}


def step(cards, runs, weights: dict, closed: bool = False, review: dict | None = None,
         now: float | None = None) -> dict:
    """Прогресс одного шага: процент, плитки этапов, круги правок, находки и остаток.

    *runs* — попытки карточек (`{id: [...]}`), нужны для начала карточки и живого этапа.
    *closed* — шаг закрыт строкой журнала (P4). *review* — разобранный отчёт шага.
    """
    stamp = float(now) if now is not None else time.time()
    by_stage: dict[str, list[dict]] = {}
    for card in cards or []:
        stage = stage_of(card.get("idempotency_key"))
        if stage in ("prompt", "step", "review", "fix"):
            by_stage.setdefault(stage, []).append(card)
    for rows in by_stage.values():
        rows.sort(key=lambda card: _ts(card.get("created_at")) or 0.0)
    medians = weights.get("median") or {}
    shares: dict[str, float] = {}
    states: dict[str, str] = {}
    live_elapsed: dict[str, float] = {}
    tiles: list[dict] = []

    review_rows = by_stage.get("review") or []
    fix_rows = by_stage.get("fix") or []
    counts = findings(review)

    for stage in ("prompt", "step", "review"):
        state, share = _stage_state(by_stage.get(stage) or [], runs or {}, medians.get(stage),
                                    LIVE_SHARE, stamp)
        states[stage], shares[stage] = state, share
        if state == "run":
            current = [card for card in by_stage.get(stage) or []
                       if _ts(card.get("completed_at")) is None][-1]
            start = _card_start(current, (runs or {}).get(str(current.get("id") or "")) or [])
            live_elapsed[stage] = max(0.0, stamp - start) if start is not None else 0.0
    # Постановка выполнена, если началось исполнение: карточку шага без неё не создают.
    if states["prompt"] == "wait" and states["step"] != "wait":
        states["prompt"], shares["prompt"] = "done", 1.0
    states["journal"], shares["journal"] = ("done", 1.0) if closed else ("wait", 0.0)


    if closed:
        percent = 100
    else:
        total = sum(float(weights.get(stage) or 0.0) for stage in CANON) or 1.0
        got = sum(float(weights.get(stage) or 0.0) * shares.get(stage, 0.0) for stage in CANON)
        percent = min(OPEN_PERCENT, int(round(100 * got / total)))

    extra_reviews = review_rows[1:]
    rounds: list[dict] = []
    for index in range(max(len(fix_rows), len(extra_reviews))):
        fix_card = fix_rows[index] if index < len(fix_rows) else None
        review_card = extra_reviews[index] if index < len(extra_reviews) else None
        fix_state, fix_share = _stage_state([fix_card] if fix_card else [], runs or {},
                                            medians.get("fix"), LIVE_SHARE, stamp)
        rev_state, rev_share = _stage_state([review_card] if review_card else [], runs or {},
                                            medians.get("review"), LIVE_SHARE, stamp)
        parts = [fix_share] + ([rev_share] if review_card else [])
        rounds.append({"round": index + 1, "fix": fix_state, "review": rev_state,
                       "percent": int(round(100 * sum(parts) / len(parts))),
                       "label": f"круг правок {index + 1}: правки "
                                f"{STATE_LABELS.get(fix_state, fix_state)}"
                                + (f", ревью {STATE_LABELS.get(rev_state, rev_state)}"
                                   if review_card else "")})
    # Плитки — после кругов правок: отчёт прошлого круга законно висит на этапе «Ревью»,
    # который сейчас ждёт, и вердикт в подсказке должен читаться вместе с кругом (P1).
    for stage in CANON:
        hint = STATE_LABELS.get(states[stage], states[stage])
        if stage == "review":
            # В плитке — короткий вердикт, счётчик находок рисуется отдельным бейджем:
            # полная подпись в подсказке бейджа, иначе плитка читается как абзац.
            if counts["unparsed"]:
                hint = "находки не разобраны"
            elif (counts["verdict"] in ("APPROVE", "REQUEST_CHANGES")
                  and (states["review"] != "wait" or rounds)):
                hint = str(counts["verdict"])
        if stage == "journal" and not closed:
            hint = "ждёт закрытия"
        tiles.append(_tile(stage, states[stage], shares[stage], hint))
        if stage == "review":
            tiles[-1]["findings"] = counts
    open_rounds = sum(1 for row in rounds if row["percent"] < 100)
    eta = _eta(weights, shares, live_elapsed, open_rounds)
    if closed:
        # Закрытому шагу остаток не нужен: «осталось меньше минуты» на 100% — шум.
        eta = {"seconds": None, "text": "", "low_text": "", "high_text": "",
               "basis": "", "note": ""}

    return {"percent": percent, "closed": bool(closed), "tiles": tiles,
            "states": states, "shares": shares, "rounds": rounds,
            "rounds_label": (f"кругов правок: {len(rounds)}" if rounds else ""),
            "findings": counts, "eta": eta,
            "weights_basis": str(weights.get("basis") or "")}


def plan(steps, cards, runs, closed, now: float | None = None,
         ui: dict | None = None, reviews: dict | None = None) -> dict:
    """Прогресс плана: строка на шаг, средний процент, число закрытых и остаток.

    *steps* — шаги из `next_step.py`, *closed* — номера закрытых шагов, *reviews* —
    разобранные отчёты `{шаг: отчёт}` (нужны для счётчика находок).
    """
    stamp = float(now) if now is not None else time.time()
    measured = samples(cards, runs)
    weight_rows = weights(measured, ui)
    closed_ids = {str(item).strip().upper() for item in closed or []}
    grouped = group_by_step(cards)
    reviews = reviews or {}
    per_step: dict[str, dict] = {}
    for row in steps or []:
        step_id = str(row.get("id") or "").strip().upper()
        if not step_id:
            continue
        per_step[step_id] = step(grouped.get(step_id) or [], runs or {}, weight_rows,
                                 closed=step_id in closed_ids,
                                 review=reviews.get(step_id), now=stamp)
    total = len(per_step)
    percents = [row["percent"] for row in per_step.values()]
    eta_seconds = 0
    eta_note = ""
    for row in per_step.values():
        if row["closed"]:
            continue
        eta = row["eta"]
        eta_note = eta_note or dict(row["eta"]).get("note") or ""
        eta_seconds += int(eta["seconds"] or 0)
    return {"steps": per_step, "weights": weight_rows,
            "percent": int(round(sum(percents) / total)) if total else 0,
            "closed": len(closed_ids & set(per_step)), "total": total,
            "eta": {"seconds": eta_seconds or None,
                    "text": ("~" + _duration(eta_seconds)) if eta_seconds else "",
                    "basis": str(weight_rows.get("basis") or ""), "note": eta_note}}
