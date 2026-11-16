# -*- coding: utf-8 -*-
"""Риск шага: пометки «Риск»/«Авто», автоматический ручной допуск и его снятие."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
import next_step
from engine_kit import cards, complete, set_risk_gate, write_approval

PLAN_RISK = """# 1. Демо-план конвейера

Цель: проверить, что страж ведёт шаги.

## Шаги

### S1. Опасный шаг (1 дн)

- Файл: `app/danger.py`
- Тесты/гейты: `python -m pytest tests/test_danger.py -q` → 1 passed
- Риск: high
- Готово = опасный шаг сделан

### S2. Безопасный шаг (1 дн)

- Файл: `app/safe.py`
- Тесты/гейты: `python -m pytest tests/test_safe.py -q` → 1 passed
- Риск: low
- Авто: да
- Готово = безопасный шаг сделан

### S3. Обычный шаг (1 дн)

- Файл: `app/plain.py`
- Тесты/гейты: `python -m pytest tests/test_plain.py -q` → 1 passed
- Готово = обычный шаг сделан

### S4. Опечатка в риске (1 дн)

- Файл: `app/typo.py`
- Тесты/гейты: `python -m pytest tests/test_typo.py -q` → 1 passed
- Риск: когда-нибудь
- Готово = шаг с нераспознанной пометкой

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
| — | S3 | — | — | открыт |
| — | S4 | — | — | открыт |
"""


def write_risk_plan(root: Path) -> Path:
    """Переписать демо-план пометками риска и переутвердить его.

    Запись утверждения хранит sha256 файла плана (шаг 4-S7), поэтому после правки
    плана её надо обновить — иначе страж честно откажется вести «изменённый» план.
    """
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_RISK, encoding="utf-8")
    write_approval(root, path)
    return path


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO.

    Режим риск-гейта — умолчание шаблона (`spec`, 8-S2B): высокорисковый шаг идёт обычной
    веткой и требует спеку в постановке. Стенд ручного допуска — фикстура `stand_hold`.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return cfg, gate.resolve_paths(cfg, "1")


@pytest.fixture()
def stand_hold(project: Path, hermes_stub: Path, monkeypatch):
    """Тот же стенд в режиме `risk_gate: hold`: прежнее поведение ручного допуска.

    Стенды держат проект из `install_project`, а не шаблон плагина: умолчание конфига —
    `spec`, поэтому поведение «high-шаг ждёт `--unhold`» надо включить явно.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    set_risk_gate(project, "hold")
    cfg = config_mod.load(project)
    return cfg, gate.resolve_paths(cfg, "1")


def fake_card(created: list[str], bodies: list[str] | None = None):
    """Подпись `_create_card` (gate.py:792-794), которая только запоминает ключи.

    *bodies* — необязательный приёмник тел карточек: проверки формы живут в теле, а не
    в ключе (8-S2B: «СПЕКА НЕ ГОТОВА» с замечаниями C7).
    """

    def create(p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        created.append(key)
        if bodies is not None:
            bodies.append(body)
        return "t_fake0001"

    return create


def test_risk_parsed_into_contract(project: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    got = {s.id: (s.risk, s.auto) for s in next_step.plan_steps(cfg, p.plan_file)}
    assert got["S1"] == ("high", False)
    assert got["S2"] == ("low", True)
    assert got["S3"] == ("normal", False)
    assert got["S4"] == ("high", False)      # нераспознанная пометка — fail-safe
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"]["risk"] == "high"
    dumped = json.dumps(pkg, ensure_ascii=False)
    assert '"risk"' in dumped
    assert '"auto"' in dumped


PLAN_PROSE = """# 1. Демо-план конвейера

Цель: проверить разбор пометок риска: метка, опечатка, описание.

## Шаги

### S1. Доки и конфиг (1 дн)

- Файл: `app/docs.py`
- Тесты/гейты: `python -m pytest tests/test_docs.py -q` → 1 passed
- Риск: доки описывают ключи, которых нет в конфиге — сверка примеров
- Готово = примеры поставки сверены с конфигом

### S2. Опечатка метки (1 дн)

- Файл: `app/typo.py`
- Тесты/гейты: `python -m pytest tests/test_typo.py -q` → 1 passed
- Риск: высокий
- Готово = шаг с опечаткой метки сделан

### S3. Метка с пояснением (1 дн)

- Файл: `app/risky.py`
- Тесты/гейты: `python -m pytest tests/test_risky.py -q` → 1 passed
- Риск: high — данные и деньги
- Готово = опасный шаг сделан

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
| — | S3 | — | — | открыт |
"""


def test_risk_prose_is_not_a_class(project: Path, stand):
    """8-S2B: описание риска прозой — `normal`; опечатка и метка с пояснением — `high`.

    План 12 писал риски прозой («Риск: доки описывают ключи…»), и страж читал это как
    `high` у всех пяти шагов: `high` был описанием, а не решением автора. Теперь проза
    даёт `normal`, одно слово вне словаря остаётся `high` (П25), а слово из словаря
    остаётся собой, сколько бы ни шло за ним текста.
    """
    cfg, p = stand
    path = project / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_PROSE, encoding="utf-8")
    write_approval(project, path)
    got = {s.id: s.risk for s in next_step.plan_steps(cfg, p.plan_file)}
    assert got == {"S1": "normal", "S2": "high", "S3": "high"}


def test_risk_prose_step_does_not_wait_for_owner(project: Path, hermes_stub: Path, stand):
    """Прозаический риск не делает шаг `high`: ни допуска владельца, ни карточки `hold`."""
    cfg, p = stand
    path = project / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_PROSE, encoding="utf-8")
    write_approval(project, path)
    created: list[str] = []
    res = gate.run_gate(p, execute=True, create_card=fake_card(created),
                        notify_fn=lambda text: None)
    assert res["decision"]["action"] != "hold_risk", res["decision"]
    # Допуск не записан: проза риска не делает шаг `high` (до 8-S2B здесь был `hold_risk`).
    assert "hold_steps" not in gate.load_state(p)


def test_high_risk_step_gets_hold_not_card(project: Path, hermes_stub: Path, stand_hold):
    cfg, p = stand_hold
    write_risk_plan(project)
    created: list[str] = []
    messages: list[str] = []
    res = gate.run_gate(p, execute=True, create_card=fake_card(created),
                        notify_fn=messages.append)
    assert res["decision"]["action"] == "hold_risk", res["decision"]
    assert created == []                     # ни карточки шага, ни заказа постановки
    assert gate.load_state(p)["hold_steps"] == ["S1"]
    assert len(messages) == 1, messages
    assert "Риск: high" in messages[0]
    assert "§S1" in messages[0]
    assert "--unhold S1" in messages[0]


def test_unhold_releases_high_risk_step(project: Path, hermes_stub: Path, stand_hold):
    cfg, p = stand_hold
    write_risk_plan(project)
    created: list[str] = []
    first = gate.run_gate(p, execute=True, create_card=fake_card(created),
                          notify_fn=lambda text: None)
    assert first["decision"]["action"] == "hold_risk"
    assert gate.main(["--unhold", "S1"]) == 0
    state = gate.load_state(p)
    assert state["hold_steps"] == []
    assert state["hold_released"] == ["S1"]
    (project / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")
    res = gate.run_gate(p, execute=True, create_card=fake_card(created),
                        notify_fn=lambda text: None)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:step" in created
    assert gate.load_state(p)["hold_steps"] == []      # hold не вернулся


def decision_ctx(risk: str, auto: bool, risk_gate: str = "spec") -> dict:
    step = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
            "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": "",
            "risk": risk, "auto": auto}
    return {"next": step, "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "released": [], "verdict": None, "journal_closed": False, "plan_id": "1",
            "risk_gate": risk_gate}


def test_auto_does_not_bypass_high_and_normal_starts():
    assert gate.decide(decision_ctx("normal", False))["action"] == "create_step"
    assert gate.decide(decision_ctx("low", True))["action"] == "create_step"
    # 8-S2B: в режиме `spec` (умолчание) high-шаг идёт обычной веткой, ручной допуск
    # остался выключателем `risk_gate: hold` и включается только им.
    assert gate.decide(decision_ctx("high", True))["action"] == "create_step"
    assert gate.decide(decision_ctx("high", True, "spec"))["action"] == "create_step"
    assert gate.decide(decision_ctx("high", True, "hold"))["action"] == "hold_risk"
    assert gate.decide(decision_ctx("normal", False, "hold"))["action"] == "create_step"


def test_hold_visible_in_status_and_snapshot_source(project: Path, hermes_stub: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    assert gate.main(["--hold", "S1"]) == 0
    ctx = gate.build_ctx(p)
    d = gate.decide(ctx)
    assert d["action"] == "idle"
    text = gate.status_text(ctx, d)
    assert "Ручной допуск (hold): S1" in text
    assert "Риск шага: high" in text
    row = gate.plans_rows(cfg)[0]
    assert row["hold"] == ["S1"]
    assert row["risk"] == "high"
    assert "hold: S1" in gate.plans_table(cfg)


def test_hold_refuses_step_absent_from_plan(project: Path, stand):
    """Опечатка в номере шага не оседает в состоянии плана.

    `--hold` шлёт и панель (номер из выбранной вкладки), и бот: устаревшая страница
    присылает шаг, которого в плане уже нет. До 0.25.9 такой номер записывался в
    `hold_released` навсегда — снять его командой было нечем, а страж держал шаг.
    """
    cfg, p = stand
    assert "S1" in gate.declared_steps(cfg, p), "фикстура должна разбирать шаги плана"
    answer = gate.handle_command(cfg, "/hold NOPE-99")
    assert "нет шага NOPE-99" in answer
    assert "S1" in answer                       # отказ называет допустимые шаги
    assert not Path(p.state_file).exists(), "отказ не должен создавать состояние"


def test_cli_hold_refuses_unknown_step(project: Path, stand, capsys):
    """Тот же отказ в CLI: код 2 и причина в stderr — её показывает панель."""
    for flag in ("--hold", "--unhold"):
        assert gate.main(["--plan", "1", flag, "NOPE-99"]) == 2
        assert "нет шага NOPE-99" in capsys.readouterr().err


def test_unhold_of_released_step_stays_allowed(project: Path, stand):
    """Снятие допуска идемпотентно: повторный `--unhold` не отказ.

    Шаг после снятия живёт в `hold_released`, а не в шагах плана — проверка номера
    обязана это учитывать, иначе владелец получал бы отказ на собственном повторе.
    """
    cfg, p = stand
    assert "S1" in gate.handle_command(cfg, "/hold S1")
    assert "—" in gate.handle_command(cfg, "/unhold S1")
    assert "—" in gate.handle_command(cfg, "/unhold S1")
    assert gate.load_state(p)["hold_released"] == ["S1"]


def test_risk_hold_is_idempotent(project: Path, hermes_stub: Path, stand_hold):
    cfg, p = stand_hold
    write_risk_plan(project)
    created: list[str] = []
    messages: list[str] = []
    first = gate.run_gate(p, execute=True, create_card=fake_card(created),
                          notify_fn=messages.append)
    second = gate.run_gate(p, execute=True, create_card=fake_card(created),
                           notify_fn=messages.append)
    assert first["decision"]["action"] == "hold_risk"
    assert first["applied"] is True
    assert second["decision"]["action"] == "idle"
    assert second["applied"] is False
    assert created == []
    assert gate.load_state(p)["hold_steps"] == ["S1"]     # одна запись, без дублей
    assert len(messages) == 1


# ───────────────── спека вместо допуска: risk_gate (8-S2B) ─────────────────

PLAN_ONE_HIGH = """# 1. Демо-план конвейера

Цель: проверить риск-гейт `spec`: спецификация вместо ручного допуска.

## Шаги

### S1. Опасный шаг (1 дн)

- Файл: `app/danger.py`
- Тесты/гейты: `python -m pytest tests/test_danger.py -q` → 1 passed
- Риск: high
- Готово = опасный шаг сделан

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

PROMPT_PLAIN = """# Промт 1 / S1 — опасный шаг

- Постановка: шаг S1 — таблица `danger`.
- Файл: `app/danger.py`
- Гейты: `python -m pytest tests/test_danger.py -q` → 1 passed
"""

PROMPT_SPEC = """# Промт 1 / S1 — опасный шаг

- Постановка: шаг S1 — таблица `danger`.

## Спека (у шага с «Риск: high»)

### Замысел

Шаг вводит таблицу `danger` и разбор её полей: сегодня потребитель узнаёт о расхождении
схемы на запуске, а нужно, чтобы расхождение находил гейт до деплоя и называл поле.
Результат в репозитории — модель, миграция и тест, который падает на старом коде.

### Границы

Входит: модель, миграция, тест на расхождение и гейт конфигурации. Не входит: правка
разбора под новые ключи, чистка исторических данных и любые изменения в панели.

### Критерии готовности

`python -m pytest tests/test_danger.py -q` → 3 passed; в тесте есть случай «поле есть в
схеме, нет в разборе» и обратный; `git diff --stat` не трогает `gate.py` и `snapshot.py`.

### Риски

Миграция может затребовать NOT NULL на существующей таблице — тогда шаг останавливается
и спрашивает владельца, а не придумывает значение по умолчанию.
"""

PROMPT_NAME = "2026-11-01_1-S1_prompt.md"


def write_high_stand(project: Path, prompt: str = PROMPT_PLAIN) -> None:
    """Проект с одним high-шагом и постановкой; план переутверждён после записи."""
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(PLAN_ONE_HIGH, encoding="utf-8")
    write_approval(project, plan)
    (project / "docs" / "prompts" / PROMPT_NAME).write_text(prompt, encoding="utf-8")


def patch_config(project: Path, **keys) -> None:
    """Правка `pipeline.json` стенда: лимиты и режимы приходят из конфигурации."""
    path = project / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(keys)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_high_risk_prompt_without_spec_gets_prompt_fix(project: Path, hermes_stub: Path,
                                                       monkeypatch):
    """High-шаг без спеки: круг правок постановки вместо допуска и вместо карточки шага."""
    write_high_stand(project)
    spec_env(monkeypatch, project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")

    first = gate.run_gate(p, execute=True, notify_fn=lambda text: None)
    decision = first["decision"]
    assert decision["action"] == "create_prompt_fix", decision
    assert decision.get("spec"), decision
    rows = cards(hermes_stub)
    assert [c["key"] for c in rows] == ["planloop:1:S1:promptfix:1"], rows
    assert rows[0]["assignee"] == "prompter"
    assert "дописать спеку" in rows[0]["title"], rows[0]
    assert "hold_steps" not in gate.load_state(p)

    # Второй тик не плодит вторую карточку: правка постановки ещё идёт.
    second = gate.run_gate(p, execute=True, notify_fn=lambda text: None)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert len(cards(hermes_stub)) == 1


def test_repo_plan_with_prose_risks_reads_normal():
    """План 12 репозитория читается пятью `normal` без правки его текста (доказательство).

    Риски плана 12 написаны описанием («Риск: доки описывают ключи…»): до 8-S2B страж
    видел в этом `high` и держал все пять шагов ручным допуском. Теперь это `normal`,
    и текст плана не менялся — разбор стал читать пометку так, как её писали.
    """
    repo = Path(__file__).resolve().parents[2]
    cfg = config_mod.load(repo)
    plans = sorted((repo / "docs" / "plans").glob("12-*.md"))
    assert plans, "план 12 должен лежать в docs/plans"
    steps = next_step.plan_steps(cfg, plans[0])
    assert len(steps) == 5, [step.id for step in steps]
    assert {step.risk for step in steps} == {"normal"}, [(s.id, s.risk) for s in steps]


def test_spec_card_body_lists_c7_findings(project: Path):
    """Тело карточки правки называет замечания C7 и что дописать в спеку."""
    write_high_stand(project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    step = next_step.build_package(cfg, "1")["next"]
    body = gate.prompt_fix_card_body(
        p, step, 1, "docs/reviews/1-S1-prompt.md",
        spec=["нет блока «Спека»: у шага с «Риск: high» спецификация обязательна"],
        max_rounds=6)
    assert "СПЕКА НЕ ГОТОВА (круг 1 из 6)" in body
    assert "нет блока «Спека»" in body
    assert "«Критерии готовности»" in body          # что именно дописать
    # Гейт круга спеки спрашивает спеку, и команда в карточке ровно одна: вторая, без
    # `--risk high`, давала «PASS 8/8» постановке без блока «Спека» (правки по ревью 8-S2B).
    assert body.count("check_task_prompt.py") == 1, body
    gate_lines = [line for line in body.splitlines() if "check_task_prompt.py" in line]
    assert " --risk high` → PASS 8/8" in gate_lines[0], gate_lines
    # Раздел находок ревью не печатается: круг открыт формой постановки, а не отчётом.
    assert "БЛОКИРУЮЩИЕ НАХОДКИ" not in body
    assert "ОТКРЫТЫЕ НАХОДКИ РЕЕСТРА" not in body
    assert "ЗАВЕРШЕНИЕ" in body


def test_full_spec_passes_without_hold(project: Path, hermes_stub: Path, monkeypatch):
    """Полная спека пускает шаг обычной веткой: карточка шага, допуск владельца не нужен."""
    write_high_stand(project, PROMPT_SPEC)
    spec_env(monkeypatch, project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    created: list[str] = []
    res = gate.run_gate(p, execute=True, create_card=fake_card(created),
                        notify_fn=lambda text: None)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert created == ["planloop:1:S1:step"], created
    assert "hold_steps" not in gate.load_state(p)


def test_risk_gate_config_default_and_validation(project: Path):
    """Ключ `risk_gate`: умолчание `spec`, мусор — `ConfigError`, оба режима читаются."""
    assert config_mod.DEFAULTS["risk_gate"] == "spec"
    assert config_mod.load(project).risk_gate == "spec"      # ключа в конфиге нет
    set_risk_gate(project, "hold")
    assert config_mod.load(project).risk_gate == "hold"
    set_risk_gate(project, " SPEC ")
    assert config_mod.load(project).risk_gate == "spec"
    set_risk_gate(project, "держать")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(project)
    assert "risk_gate" in str(exc.value)


def test_spec_fix_cannot_be_switched_off_by_prompt_review_never():
    """`prompt_review: never` не отменяет риск-гейт: спека — форма постановки, не ревью."""
    step = {"id": "S1", "risk": "high"}
    ctx = {"next": step, "cards": [], "prompt": "docs/prompts/2026-11-01_1-S1_prompt.md",
           "prompt_spec": ["нет блока «Спека»: у шага с «Риск: high» спецификация обязательна"],
           "prompt_review": "never", "max_fix_rounds": 2, "max_fix_rounds_high": 6}
    decision = gate._prompt_review_decision(ctx, step)
    assert decision["action"] == "create_prompt_fix", decision
    assert decision["spec"]


def test_spec_branch_wins_over_request_changes():
    """Спека проверяется до разбора вердикта: REQUEST_CHANGES не открывает круг правок."""
    step = {"id": "S1", "risk": "high"}
    ctx = {"next": step, "prompt": "docs/prompts/2026-11-01_1-S1_prompt.md",
           "prompt_spec": ["тело спеки 120 символов из 600"],
           "cards": [{"id": "t_old0001", "key": "planloop:1:S1:promptreview",
                      "status": "done", "created_at": 1}],
           "prompt_verdict": {"verdict": "REQUEST_CHANGES", "path": "docs/reviews/1-S1-prompt.md"},
           "prompt_review": "auto", "max_fix_rounds": 2, "max_fix_rounds_high": 6}
    decision = gate._prompt_review_decision(ctx, step)
    assert decision["action"] == "create_prompt_fix", decision
    assert "C7" in decision["reason"], decision


def test_stale_rebuilds_do_not_eat_spec_budget():
    """Пересборки по свежести в бюджет спеки не входят: счёт идёт по `:promptfix:`."""
    step = {"id": "S1", "risk": "high"}
    ctx = {"next": step, "prompt": "docs/prompts/2026-11-01_1-S1_prompt.md",
           "prompt_spec": ["нет блока «Спека»: у шага с «Риск: high» спецификация обязательна"],
           "cards": [{"id": "t_old0001", "key": "planloop:1:S1:promptfix-stale:1",
                      "status": "done", "created_at": 1}],
           "prompt_review": "auto", "max_fix_rounds": 1, "max_fix_rounds_high": 1}
    decision = gate._prompt_review_decision(ctx, step)
    assert decision["action"] == "create_prompt_fix", decision
    assert decision["round"] == 2        # номер круга учитывает пересборку...


def test_spec_stops_owner_after_limit(project: Path, hermes_stub: Path, monkeypatch):
    """Исчерпание кругов правок постановки — owner-стоп с числом, а не бесконечные круги."""
    write_high_stand(project)
    patch_config(project, max_fix_rounds=1, max_fix_rounds_high=1)
    spec_env(monkeypatch, project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    first = gate.run_gate(p, execute=True, notify_fn=lambda text: None)
    assert first["decision"]["action"] == "create_prompt_fix"
    assert [c["key"] for c in cards(hermes_stub)] == ["planloop:1:S1:promptfix:1"]
    complete(hermes_stub, ":promptfix:1")
    second = gate.run_gate(p, execute=True, notify_fn=lambda text: None)
    decision = second["decision"]
    assert decision["action"] == "stop", decision
    assert decision["kind"] == "owner", decision
    assert "исчерпаны (1 из 1)" in decision["reason"], decision
    assert len(cards(hermes_stub)) == 1     # второй карточки нет


def spec_env(monkeypatch, project: Path) -> None:
    """Стенд: сборщик шагов смотрит в проект, env-подмена конфигурации снята."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)


STALE_DECISION = {"stale": True, "stale_reason": "stale-input", "rebuild": 1, "limit": 2,
                  "fp": "ab" * 32, "prev_recon": None, "review_round": 2, "changed": {},
                  "reason": "постановка S1 протухла: вход изменился после ревью"}


def prompt_gate_line(body: str) -> str:
    """Единственная строка гейта формы постановки в теле карточки.

    Команд должно быть ровно одна: вторая, без флага, давала «PASS 8/8» постановке без
    блока «Спека» (правки по ревью 8-S2B, круг 1).
    """
    lines = [line for line in body.splitlines() if "check_task_prompt.py" in line]
    assert len(lines) == 1, lines
    return lines[0]


def test_gate_line_carries_risk_flag_by_step_risk(project: Path, monkeypatch):
    """Флаг `--risk high` — по пометке шага и режиму, а не по непустому `spec` (F6 8-S2B).

    Обычный круг правок постановки (замечания C7 в карточке не собраны) и карточка
    пересборки протухшей постановки переписывают постановку с нуля так же, как круг
    спеки: команда без флага пропускала постановку без блока «Спека», и страж тут же
    заказывал новый круг — при пересборке ещё и сжигая `max_stale_rebuilds`.
    """
    write_high_stand(project)
    spec_env(monkeypatch, project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    step = next_step.build_package(cfg, "1")["next"]
    assert step["risk"] == "high", step
    report = "docs/reviews/1-S1-prompt.md"

    ordinary = gate.prompt_fix_card_body(p, step, 3, report)
    assert "СПЕКА НЕ ГОТОВА" not in ordinary          # круг открыт не спекой
    assert prompt_gate_line(ordinary).endswith("--risk high` → PASS 8/8."), ordinary

    stale = gate.prompt_stale_card_body(p, step, STALE_DECISION, report)
    assert prompt_gate_line(stale).endswith("--risk high` → `ИТОГ: PASS`.")
    assert "«Спека»" in stale, "пересборка молчит о блоке, который обязан написать"


def test_spec_card_puts_spec_section_before_forbids(project: Path):
    """Стилистическая находка круга 2: «СПЕКА НЕ ГОТОВА» — выше «ЗАПРЕТОВ».

    Круг открыт формой постановки, а не отчётом: первым действием карточка велит дописать
    спеку, а не «прочитать отчёт и закрыть каждую блокирующую находку» — отчёта может не
    быть вовсе, и исполнитель шёл в пустой документ.
    """
    write_high_stand(project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    step = next_step.build_package(cfg, "1")["next"]
    body = gate.prompt_fix_card_body(p, step, 1, "docs/reviews/1-S1-prompt.md",
                                     spec=["нет блока «Спека»"], max_rounds=6)
    assert body.index("СПЕКА НЕ ГОТОВА") < body.index("ЗАПРЕТЫ"), body
    first_action = body.split("ЧТО ДЕЛАТЬ", 1)[1].splitlines()[1]
    assert "«Спека»" in first_action, first_action
    assert "закрыть КАЖДУЮ блокирующую" not in body


def test_gate_line_has_no_risk_flag_where_spec_is_not_required(project: Path, monkeypatch):
    """`normal`-шаг и режим `hold`: флага нет — иначе C7 дал бы ложный FAIL спеки."""
    write_high_stand(project)
    spec_env(monkeypatch, project)
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    high = next_step.build_package(cfg, "1")["next"]
    report = "docs/reviews/1-S1-prompt.md"

    normal = dict(high, risk="normal")
    for body in (gate.prompt_fix_card_body(p, normal, 2, report),
                 gate.prompt_stale_card_body(p, normal, STALE_DECISION, report)):
        assert "--risk high" not in prompt_gate_line(body), body

    set_risk_gate(project, "hold")
    p_hold = gate.resolve_paths(config_mod.load(project), "1")
    for body in (gate.prompt_fix_card_body(p_hold, high, 2, report),
                 gate.prompt_stale_card_body(p_hold, high, STALE_DECISION, report)):
        assert "--risk high" not in prompt_gate_line(body), body
    assert "«Спека»" not in gate.prompt_stale_card_body(p_hold, high, STALE_DECISION, report)
