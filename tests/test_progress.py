# -*- coding: utf-8 -*-
"""Прогресс шага и плана: проценты этапов, веса, находки ревью и остаток времени.

Числа в проверках — значения, которые тест сам считает от весов и замеров: проверяется
формула шага, а не «ключ присутствует».
"""
from __future__ import annotations

import helpers  # noqa: F401  (загружает плагин и алиасы модулей)
from step_pipeline import progress

NOW = 1_760_000_000.0          # метка «сейчас»: тест не зависит от системных часов
HOUR = 3600


def card(stage: str, step: str = "S1", status: str = "done", *,
         created: float | None = None, started: float | None = None,
         completed: float | None = None, index: int = 0) -> dict:
    """Карточка этапа в том виде, как её отдаёт `snapshot.cards`."""
    return {"id": f"t_{step}_{stage}_{index}", "idempotency_key": f"planloop:1:{step}:{stage}",
            "status": status, "created_at": created if created is not None else NOW - 7200,
            "started_at": started, "completed_at": completed}


def measured(*, prompt: float = 600, step: float = 3600, review: float = 1200,
             count: int = 3) -> dict[str, list[float]]:
    """Замеры этапов: одинаковые значения, чтобы медиана была предсказуемой."""
    return {"prompt": [prompt] * count, "step": [step] * count,
            "review": [review] * count, "fix": []}


def test_empty_step_has_no_percent_and_no_estimate():
    """Карточек нет — 0%, этапы ждут, оценки нет, но причина названа (P7)."""
    row = progress.step([], {}, progress.weights({}), now=NOW)
    assert row["percent"] == 0 and row["closed"] is False
    assert [tile["stage"] for tile in row["tiles"]] == list(progress.CANON)
    assert [tile["state"] for tile in row["tiles"]] == ["wait"] * 4
    assert row["eta"]["seconds"] is None and "замеров нет" in row["eta"]["note"]
    assert row["rounds"] == [] and row["findings"]["total"] is None


def test_canonical_path_without_journal_is_under_hundred():
    """Постановка, исполнение и ревью закрыты — 90% по умолчанию, журнал ещё ждёт (P1, P4)."""
    cards = [card("prompt", completed=NOW - 6000), card("step", completed=NOW - 1800),
             card("review:1", completed=NOW - 600)]
    row = progress.step(cards, {}, progress.weights({}), now=NOW)
    assert row["percent"] == 90
    assert [tile["state"] for tile in row["tiles"]] == ["done", "done", "done", "wait"]
    assert row["tiles"][3]["hint"] == "ждёт закрытия"


def test_queued_card_is_waiting_not_running():
    """Карточка в очереди — «ждёт»: время ожидания слота прогрессом этапа не считается."""
    cards = [card("prompt", completed=NOW - 6000),
             card("step", status="ready", created=NOW - 600)]
    row = progress.step(cards, {}, progress.weights(measured()), now=NOW)
    assert row["tiles"][1]["state"] == "wait" and row["tiles"][1]["percent"] == 0
    # Непройденный этап всё равно входит в остаток: очередь не отменяет работу.
    assert row["eta"]["seconds"] >= 600 + 3600


def test_closed_step_is_hundred_even_without_cards():
    """Закрытый журналом шаг — 100%: строку журнала пишет страж, карточек для неё нет (P4)."""
    row = progress.step([], {}, progress.weights(measured()), closed=True, now=NOW)
    assert row["percent"] == 100 and row["closed"] is True
    assert row["tiles"][-1]["state"] == "done"
    # Закрытому шагу остаток не считается: «осталось» на 100% — шум.
    assert row["eta"]["seconds"] is None and row["eta"]["text"] == ""


def test_live_stage_takes_share_of_its_weight():
    """Идущий этап даёт половину веса, когда прошло полмедианы, и не больше 90% (P5)."""
    started = NOW - 1800
    cards = [card("prompt", completed=NOW - 9000),
             card("step", status="running", started=started)]
    weights = progress.weights(measured())
    row = progress.step(cards, {}, weights, now=NOW)
    # 10% постановки целиком + 50% от 60% исполнения = 40% (веса выучены: 10/60/20/10).
    assert row["percent"] == 40
    assert row["states"]["step"] == "run" and 0.49 <= row["shares"]["step"] <= 0.51

    long_run = [card("prompt", completed=NOW - 9000),
                card("step", status="running", started=NOW - 10 * HOUR)]
    capped = progress.step(long_run, {}, weights, now=NOW)
    assert round(capped["shares"]["step"], 2) == progress.LIVE_MAX


def test_fix_round_does_not_roll_back_the_main_bar():
    """Круг правок идёт отдельным отрезком: канонический процент от него не падает (P3)."""
    base = [card("prompt", completed=NOW - 9000), card("step", completed=NOW - 3000),
            card("review:1", completed=NOW - 1200)]
    verdict = {"exists": True, "verdict": "REQUEST_CHANGES", "findings": [], "note": ""}
    before = progress.step(base, {}, progress.weights({}), review=verdict, now=NOW)
    round_cards = base + [card("fix:1", completed=NOW - 600),
                          card("review:2", status="running", started=NOW - 300, index=1)]
    after = progress.step(round_cards, {}, progress.weights({}), review=verdict, now=NOW)
    assert after["percent"] == before["percent"] == 90
    assert len(after["rounds"]) == 1
    assert after["rounds"][0]["fix"] == "done" and after["rounds"][0]["review"] == "run"
    assert after["rounds"][0]["percent"] == 75          # (1.0 + 0.5) / 2
    assert "круг правок 1" in after["rounds"][0]["label"]


def test_verdict_hint_waits_for_its_card():
    """Вердикт в плитке — только когда ревью дошло: отчёт без карточки — след прошлого круга."""
    verdict = {"exists": True, "verdict": "REQUEST_CHANGES", "findings": [], "note": ""}
    cards = [card("prompt", completed=NOW - 9000), card("step", completed=NOW - 3000)]
    row = progress.step(cards, {}, progress.weights({}), review=verdict, now=NOW)
    assert row["tiles"][2]["state"] == "wait" and row["tiles"][2]["hint"] == "ждёт"
    reviewed = cards + [card("review:1", completed=NOW - 600)]
    row = progress.step(reviewed, {}, progress.weights({}), review=verdict, now=NOW)
    assert row["tiles"][2]["hint"] == "REQUEST_CHANGES"


def test_weights_are_learned_from_medians():
    """Три замера на этап — веса считаются по медианам: 10/60/20 и журнал 10 (P2)."""
    weights = progress.weights(measured())
    assert weights["learned"] is True
    assert (round(weights["prompt"]), round(weights["step"]), round(weights["review"])) == (10, 60, 20)
    assert round(weights["journal"]) == 10
    assert "веса по замерам" in weights["basis"]
    assert weights["samples"] == {"prompt": 3, "step": 3, "review": 3}


def test_weights_fall_back_to_defaults_when_samples_are_few():
    """Двух замеров мало — веса умолчания, и основа честно это называет."""
    weights = progress.weights(measured(count=2))
    assert weights["learned"] is False
    assert (weights["prompt"], weights["step"], weights["review"]) == (10.0, 55.0, 25.0)
    assert "по умолчанию" in weights["basis"] and "2/3" in weights["basis"]


def test_weights_can_be_configured_and_disabled():
    """`ui.progress`: свои веса и отключённое обучение работают без правки кода."""
    weights = progress.weights(measured(), {"weights": {"step": 70}, "learn": False})
    assert weights["learned"] is False and weights["step"] == 70.0
    assert weights["prompt"] == 10.0 and weights["journal"] == 10.0


def test_review_findings_count_blocking_separately():
    """Находки: всего, блокирующих, и подпись для плитки (P6)."""
    review = {"exists": True, "verdict": "REQUEST_CHANGES", "findings_note": "", "findings": [
        {"level": "блокирующая", "class": "блокирующая", "text": "app/a.py:1 — нет проверки"},
        {"level": "Значимые", "class": "", "text": "app/b.py:2 — нет теста"},
        {"level": "стилистическая", "class": "стилистическая", "text": "app/c.py:3 — имя"},
    ]}
    row = progress.findings(review)
    assert (row["total"], row["blocking"], row["unparsed"]) == (3, 1, False)
    assert row["label"] == "REQUEST_CHANGES · 3 находки · 1 блокир."
    assert progress.findings({"exists": True, "verdict": "APPROVE",
                              "findings": []})["label"] == "APPROVE"


def test_unparsed_findings_show_question_not_zero():
    """Раздел находок не разобран — числа нет: ноль читался бы как «чисто»."""
    row = progress.findings({"exists": True, "verdict": "APPROVE", "findings": [],
                             "findings_note": "раздел есть, но разобрать не удалось"})
    assert row["total"] is None and row["unparsed"] is True
    assert row["label"] == "находки не разобраны"

    missing = progress.findings({"exists": False, "verdict": "NONE", "findings": [],
                                 "note": "отчёта ревью нет"})
    assert missing["total"] is None and missing["label"] == ""
    assert missing["note"] == "отчёта ревью нет"


def test_eta_sums_medians_of_stages_ahead():
    """Остаток — сумма медиан оставшихся этапов: 10 + 60 + 20 минут (P7)."""
    row = progress.step([], {}, progress.weights(measured()), now=NOW)
    assert row["eta"]["seconds"] == 5400 and row["eta"]["text"] == "~1 ч 30 мин"
    assert row["eta"]["note"] == ""


def test_eta_subtracts_elapsed_of_live_stage():
    """У идущего этапа из медианы вычитается уже потраченное: 1800 из 3600."""
    cards = [card("prompt", completed=NOW - 9000),
             card("step", status="running", started=NOW - 1800)]
    row = progress.step(cards, {}, progress.weights(measured()), now=NOW)
    # Постановка закрыта, у исполнения осталось 1800 из 3600, ревью ещё не начиналось.
    assert row["eta"]["seconds"] == 1800 + 1200


def test_eta_is_marked_rough_without_quartiles():
    """Замеров меньше порога — оценка есть, вилки нет и стоит пометка «грубо»."""
    row = progress.step([card("prompt", completed=NOW - 6000)], {},
                        progress.weights(measured(count=1)), now=NOW)
    assert row["eta"]["seconds"] == 3600 + 1200
    assert "грубая" in row["eta"]["note"]


def test_plan_counts_closed_steps_and_sums_eta():
    """План: средний процент по шагам, закрытые, остаток по открытым."""
    steps = [{"id": "S1"}, {"id": "S2"}]
    row = progress.plan(steps, [], {}, ["S1"], now=NOW, ui=None)
    assert row["closed"] == 1 and row["total"] == 2
    assert row["percent"] == 50                     # (100 + 0) / 2
    assert row["steps"]["S1"]["percent"] == 100 and row["steps"]["S2"]["percent"] == 0
    assert row["eta"]["seconds"] is None            # замеров нет — оценки нет
    assert "замеров нет" in row["eta"]["note"]


def test_plan_eta_and_findings_come_into_step_rows():
    """Отчёт ревью шага виден в его строке: счётчик находок и остаток по замерам."""
    steps = [{"id": "S1"}]
    cards = [card("prompt", "S1", completed=NOW - 9000),
             card("step", "S1", completed=NOW - 3000),
             card("review:1", "S1", completed=NOW - 1200)]
    runs_rows = {card_row["id"]: [] for card_row in cards}
    review = {"exists": True, "verdict": "REQUEST_CHANGES", "findings_note": "",
              "findings": [{"level": "блокирующая", "class": "блокирующая", "text": "x"},
                           {"level": "Значимые", "class": "", "text": "y"}]}
    row = progress.plan(steps, cards, runs_rows, [], now=NOW,
                        reviews={"S1": review})
    assert row["steps"]["S1"]["findings"]["total"] == 2
    assert row["steps"]["S1"]["findings"]["blocking"] == 1
    assert row["steps"]["S1"]["findings"]["label"].startswith("REQUEST_CHANGES")
