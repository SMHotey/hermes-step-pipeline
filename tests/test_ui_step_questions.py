# -*- coding: utf-8 -*-
"""Вопросы шага и исполненные планы на странице панели (0.33).

Живой случай, из которого выросли эти проверки (17.11.2026): вопрос шага жил только в
окне, которое открывалось один раз; на вкладке «Шаг» его не было вовсе; ответ из окна
не срабатывал (кнопки не гасились, второй клик приходил на уже закрытый вопрос), а экран
шага после ответа продолжал показывать «ждёт ответа», потому что перечитывался только
при смене пары «план + шаг».

Движка JS в проекте нет и в этом шаге не появляется: сторож читает текст страницы, как
соседние тесты вкладки «Планы».
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402,F401  (загружает плагин и алиасы модулей)

PAGE = PLUGIN / "step_pipeline" / "ui" / "index.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def block(text: str, start: str, end: str) -> str:
    """Кусок текста от `start` до ближайшего `end` после него."""
    begin = text.index(start)
    return text[begin:text.index(end, begin + len(start))]


def test_step_tab_carries_its_own_questions_block():
    """Вопросы шага — на вкладке «Шаг», а не только в всплывающем окне."""
    html = page()
    start = html.index('id="tab-step"')
    end = html.index('id="step-contract"', start)
    step = html[start:end]
    for needle in ('id="step-questions-block"', 'id="step-questions"',
                   'id="step-questions-count"', 'id="step-questions-note"'):
        assert needle in step, needle
    # Блок вопросов стоит до контракта: его читают первым, он же держит шаг.
    assert start < html.index('id="step-questions"', start) < end


def test_step_questions_render_from_the_snapshot_not_from_a_guess():
    """Список вопросов приходит снимком; страница делит его на «этот шаг» и «другие»."""
    html = page()
    body = block(html, "function renderStepQuestions(", "function showQuestionDialog(")
    for needle in ("q.step", "String(step)", "questionCard(q, {where: false})",
                   "bindQuestionAnswers(box)", "по другим шагам плана"):
        assert needle in body, needle
    # Снимок кладёт вопросы в окно страницы, а render() раскладывает их по экрану.
    assert "window.__questions = data.questions || [];" in html
    render = block(html, "function render(data) {", "// ── уведомления")
    for needle in ("renderStepQuestions(window.__questions, currentStep)",
                   "showQuestionDialog(window.__questions)"):
        assert needle in render, needle


def test_question_card_puts_the_recommendation_first_and_shows_the_price():
    """Рекомендация — первой и помеченной; у каждого варианта видно, что будет и чем платим."""
    html = page()
    card = block(html, "function questionCard(", "function bindQuestionAnswers(")
    # Сортировка: рекомендованный вариант поднимается наверх, буква при нём остаётся своя.
    assert "sort(" in card and "рекомендую" in card
    assert "data-value=\"${esc(opt.letter || opt.text)}\"" in card
    # Дедлайн назван человеческим языком и только когда автовыбор вообще будет.
    assert "questionDeadlineText(q, rec)" in card
    # Свой ответ — не вместо вариантов, а рядом с ними.
    assert 'data-answer-send=' in card and 'data-answer-input=' in card


def test_answer_disables_buttons_and_reports_refusal_in_engine_words():
    """Ответ: кнопки гаснут на время запроса, отказ движка — его словами, а не «готово»."""
    html = page()
    body = block(html, "function answerQuestion(", "function renderQuestions(")
    assert 'el.disabled = true' in body
    assert 'el.disabled = false' in body                  # возврат после отказа
    assert "ответ не принят" in body
    assert "data.error || data.stderr" in body            # причина — из ответа движка
    assert "stepPairLoaded = \"\"" in body                 # экран шага перечитывается
    assert "loadStep()" in body
    # Ответ уходит тем же маршрутом, что у окна и у бота: одно действие, один путь.
    assert 'action: "answer"' in body and "/api/action" in body


def test_step_screen_reloads_on_its_own_frame():
    """Экран шага обновляется кадром снимка: раньше он ждал смены пары «план + шаг»."""
    html = page()
    assert "const STEP_RELOAD_MS = 4000;" in html
    body = block(html, "function refreshStepScreen(", "function clipNote(")
    for needle in ('classList.contains("active")', "stepPairPending", "Date.now() - stepReloadAt",
                   "loadStep()"):
        assert needle in body, needle
    render = block(html, "function render(data) {", "// ── уведомления")
    assert "refreshStepScreen();" in render


def test_drafts_live_on_the_plans_tab_and_closed_plans_on_overview():
    """Черновики — на вкладке «Планы»; исполненные планы — свёрнутым блоком на «Обзоре»."""
    html = page()
    overview = block(html, 'id="tab-overview"', 'id="tab-plans"')
    plans = block(html, 'id="tab-plans"', 'id="tab-step"')
    assert 'id="drafts"' not in overview
    assert 'id="drafts"' in plans and 'id="drafts-count"' in plans
    # Исполненные планы: свёрнутый блок с выбором проекта, а не общая свалка.
    assert '<details class="block" id="ov-closed">' in overview
    assert 'id="ov-closed-project"' in overview and 'id="ov-closed-rows"' in overview
    body = block(html, "function renderClosedPlans(", "// ── вопросы шага")
    assert 'plan.state === "closed"' in body and "planCard(" in body
    assert 'picker.value' in body


def test_settings_carry_the_wait_for_an_answer():
    """Ожидание ответа на вопрос шага — в основных настройках, в минутах."""
    html = page()
    form = block(html, 'id="form-config"', "</form>")
    assert 'id="answer-timeout" name="answer_timeout"' in form
    assert "Ждать ответа на вопрос шага, мин" in form
    patch = block(html, "function configPatch() {", "function configAction(")
    assert "notify: {answer_timeout:" in patch and "* 60" in patch
    # Секунды из снимка в минуты формы: одна правда о единицах — у движка.
    assert 'Math.round((Number(data.answer_timeout) || 0) / 60)' in html


def test_question_deadline_names_three_different_facts():
    """Пустой дедлайн не читается как «срок вышел» (0.33).

    Карточка обещала автовыбор, которого не будет, когда вопрос взят из файла без времени
    в состоянии плана: владелец ждёт автовыбора и ждёт напрасно.
    """
    src = page()
    body = block(src, "function questionDeadlineText(", "// Карточка вопроса")
    assert "Рекомендации нет" in body          # рекомендация не названа — автовыбора нет
    assert "Срок автовыбора истёк" in body     # время было и прошло
    assert "Дедлайн не назначен" in body       # времени нет вовсе — и это не «прошло»
    card = block(src, "function questionCard(", "function renderQuestions(")
    assert "questionDeadlineText(q, rec)" in card
