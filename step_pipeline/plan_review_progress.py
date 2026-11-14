# -*- coding: utf-8 -*-
"""Форма блока «Ревью плана» для панели: этап, круги, время и находки.

Модуль только считает по уже прочитанным данным — карточкам борды и отчёту ревью плана.
Борду он не открывает, движок не запускает, файлов не читает: всё приходит аргументами
(правило «одно чтение борды на рендер», `docs/plans/6-plany-panel.md:265-266`).

Ключевое правило — счёт тот же, что у движка: бюджет кругов правок считается по карточкам
**правок** текущей эпохи, а не по карточкам ревью. До 0.25.2 движок считал по ревью и
заказывал правку только при пустом списке правок: второй REQUEST_CHANGES давал ещё одно
ревью того же текста. Счёт по ревью показал бы «4 из 4» там, где страж видит «3 из 4», а
после переутверждения — «5 из 4» при свежей эпохе 0.

Эпоха начинается с `review_reset_at` записи утверждения (переутверждение плана — решение
владельца, бюджет открывается заново); границу считает `plan_rounds.reset_at`.
"""
from __future__ import annotations

from . import progress

#: Префикс ключей карточек конвейера: `planloop:<план>:<вид>[:<номер>]`.
KEY_PREFIX = "planloop"
#: Вид карточки — третий сегмент ключа. `review` (четвёртый сегмент у ревью шага) занят,
#: поэтому вид определяется именно третьим: `planloop:6:S7:review:1` — это не ревью плана.
REVIEW_KIND = "planreview"
FIX_KIND = "planfix"
#: Закрытые статусы карточки — те же, что `TERMINAL` у движка: по ним этап «закончился».
CLOSED = frozenset({"done", "blocked", "crashed", "timed_out", "gave_up"})
#: Строки этапов — закрытый словарь: по ним страница рисует подпись, тесты сверяют значения.
STAGES = {
    "none": "ревью плана не начиналось",
    "review": "ревью плана идёт",
    "fix": "правки плана идут",
    "owner": "ждёт решения владельца",
    "done": "ревью плана пройдено",
    "wait": "ждёт следующего круга ревью",
}


def kind_of(card: dict) -> str:
    """Вид карточки по третьему сегменту ключа: `planreview`, `planfix` или пустая строка."""
    parts = str(card.get("idempotency_key") or "").split(":")
    if len(parts) >= 3 and parts[0] == KEY_PREFIX and parts[2] in (REVIEW_KIND, FIX_KIND):
        return parts[2]
    return ""


def _order(card: dict) -> tuple:
    """Порядок карточек: по `created_at`, при равенстве — по `id` (форма детерминирована)."""
    return (int(card.get("created_at") or 0), str(card.get("id") or ""))


def _side(card: dict | None, runs: dict) -> dict | None:
    """Сторона круга: карточка, её статус и время; у открытой карточки времени нет.

    Время закрытой карточки считает `progress._card_seconds` (закрытые по `completed_at`,
    открытые — `None`). Нули не подставляются: открытая сторона показывает «идёт», иначе
    подпись была бы «меньше минуты» у карточки, которая ещё работает.
    """
    if card is None:
        return None
    seconds = progress._card_seconds(card, runs.get(str(card.get("id") or "")) or [])
    return {
        "id": str(card.get("id") or ""),
        "status": str(card.get("status") or ""),
        "seconds": seconds,
        "duration": progress._duration(seconds) if seconds is not None else "идёт",
    }


def _pair_state(review: dict | None, fix: dict | None, verdict: str) -> str:
    """Состояние круга: правки ещё нет — `wait`, идут правки — `fix`, идёт ревью — `review`.

    Пары берут подмножество словаря этапов (`done`, `review`, `fix`, `wait`): закрытый круг
    с APPROVE — `done`, закрытый с другим вердиктом — `wait` (ждёт следующего круга).
    """
    if fix is None:
        return "wait"
    if fix["status"] not in CLOSED:
        return "fix"
    if review is not None and review["status"] not in CLOSED:
        return "review"
    return "done" if verdict == "APPROVE" else "wait"


def _stage(cards: list[dict], reviews: list[dict], fixes: list[dict], exhausted: bool,
           verdict: str) -> str:
    """Этап плана по закрытому словарю `STAGES`; порядок проверок — из решения D5.

    `none` — в эпохе нет карточек; `review`/`fix` — последняя карточка открыта и её вид;
    `owner` — бюджет кругов исчерпан, а на последний отказ плана правки не было (это
    состояние стопа движка, `gate.py:2637-2645`); `done` — всё закрыто с APPROVE;
    `wait` — всё закрыто, вердикт не APPROVE. Открытая карточка, которая не последняя
    (возможный «хвост» после сбоя), даёт этап по своему виду, а не выдуманный статус.
    """
    if not cards:
        return "none"
    last = max(cards, key=_order)
    opened = [card for card in cards if str(card.get("status") or "") not in CLOSED]
    last_kind = kind_of(last)
    if last_kind == REVIEW_KIND and str(last.get("status") or "") not in CLOSED:
        return "review"
    if last_kind == FIX_KIND and str(last.get("status") or "") not in CLOSED:
        return "fix"
    if exhausted and len(reviews) > len(fixes):
        return "owner"
    if not opened:
        return "done" if verdict == "APPROVE" else "wait"
    newest = max(opened, key=_order)
    return "review" if kind_of(newest) == REVIEW_KIND else "fix"


def _seconds(cards: list[dict], runs: dict) -> float:
    """Сумма времени закрытых карточек набора; открытые в сумму не входят."""
    total = 0.0
    for card in cards:
        seconds = progress._card_seconds(card, runs.get(str(card.get("id") or "")) or [])
        if seconds is not None:
            total += seconds
    return total


def build(plan_id, cards, runs, budget, extra, reset_at, review, now) -> dict:
    """Собрать блок «Ревью плана» из уже прочитанных карточек и отчёта ревью плана.

    `plan_id` — номер плана (для чтения блока человеком; форма счёта от него не зависит),
    `cards` — карточки плана одним запросом по префиксу ключа, `runs` — их прогоны,
    `budget` — бюджет кругов правок (`plan_rounds.budget`), `extra` — ручные круги маркера,
    `reset_at` — граница эпохи (`plan_rounds.reset_at`), `review` — разбор отчёта ревью
    плана (`snapshot._review`), `now` — метка кадра. `now` в счёте не участвует: замеры
    берутся у закрытых карточек, у открытой подпись «идёт», а не «сколько уже идёт».
    """
    del plan_id, now                      # форма блока от них не зависит (см. докстринг)
    budget = max(int(budget or 0), 0)
    extra = max(int(extra or 0), 0)
    rows = [card for card in cards or [] if isinstance(card, dict)]
    rows = [card for card in rows
            if not reset_at or int(card.get("created_at") or 0) >= int(reset_at)]
    reviews = sorted((card for card in rows if kind_of(card) == REVIEW_KIND), key=_order)
    fixes = sorted((card for card in rows if kind_of(card) == FIX_KIND), key=_order)
    review = review or {}
    verdict = str(review.get("verdict") or "NONE")
    round_no = len(fixes)
    exhausted = round_no >= budget
    can_fix = exhausted and extra == 0
    note = "" if can_fix else ("ручной круг уже израсходован" if extra
                               else "бюджет не исчерпан")
    pairs: list[dict] = []
    for index in range(max(len(reviews), len(fixes))):
        review_side = _side(reviews[index] if index < len(reviews) else None, runs)
        fix_side = _side(fixes[index] if index < len(fixes) else None, runs)
        state_key = _pair_state(review_side, fix_side, verdict)
        pairs.append({"round": index + 1, "state_key": state_key,
                      "state": STAGES[state_key], "review": review_side, "fix": fix_side})
    stage_key = _stage(reviews + fixes, reviews, fixes, exhausted, verdict)
    review_seconds = _seconds(reviews, runs)
    fix_seconds = _seconds(fixes, runs)
    return {
        "exists": bool(reviews),
        "verdict": verdict,
        "round": round_no,
        "budget": budget,
        "label": f"круг {round_no} из {budget}",
        "exhausted": exhausted,
        "extra_fix_rounds": extra,
        "can_fix": can_fix,
        "note": note,
        "stage": STAGES[stage_key],
        "stage_key": stage_key,
        "rounds": pairs,
        "review_seconds": review_seconds,
        "fix_seconds": fix_seconds,
        "review_duration": progress._duration(review_seconds),
        "fix_duration": progress._duration(fix_seconds),
        "findings": progress.findings(review),
    }
