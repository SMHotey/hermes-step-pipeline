recon_commit: 6cdd4079a9069594cad1ed020984f741d79d99b7

# Ревью постановки шага S2 плана 5 — роль `scout`: разведка готовых решений (круг 4)

## ВЕРДИКТ: APPROVE

Объект ревью: коммит `59e44dad02e22423948130def3dbf5f5f56addb3` — постановка
`docs/prompts/2026-11-06_5-S2_prompt.md` (828 строк, 81 318 байт); файл совпадает с HEAD
(`git diff HEAD -- docs/prompts/2026-11-06_5-S2_prompt.md` пуст), этим коммитом изменён
ровно один файл (+84/−38). Шаг: план 5, §S2 (`docs/plans/5-platforma.md:49-60`), строка
журнала — `:189` (`| — | S2 | — | — | открыт |`).

Дата: 2026-11-06. Роль `reviewer` (`pipeline-reviewer`). Источники — ровно три: постановка,
§S2 плана, код и тесты на HEAD. Отчёта исполнителя нет и быть не может: шаг не исполнялся —
каталога `step_pipeline/roles/pipeline-scout/` нет, `tests/engine/test_scout.py` нет,
`grep -rn "scout" step_pipeline tests pipeline.json docs/PLAN-FORMAT.md` пуст (EXIT 1).

## Рекомендации владельцу

1. До старта шага завести `profiles.scout` в `pipeline.json` отдельным коммитом: одна строка
   `"scout": "<существующий профиль>"` после `reviewer` (`pipeline.json:6-10`). Значение —
   профиль Hermes, который будет исполнять разведку: `pipeline` или `prompter` (профиля
   `scout` в Hermes нет — `hermes profile list | grep -ci scout` → `0`). Проверка после
   старта: `grep -n '"scout"' pipeline.json` → три строки (`skills`, `runtimes`, `profiles`).
   Без этой строки исполнитель обязан встать на стоп-сигнале 5
   (`docs/prompts/2026-11-06_5-S2_prompt.md:733-736`), карточка шага уйдёт в `blocked`
   (`kind=needs_input`) — это ожидаемая развилка, а не дефект постановки.
2. Постановку принимать как есть: ни §S2 плана, ни постановку переписывать не нужно, шаг
   можно запускать сразу после п.1. Отдельную задачу на профиль `scout` заводить не
   обязательно, если в `profiles.scout` указан существующий профиль — шаг профилей Hermes
   не создаёт (D7; ни S11, ни S2 этого не делают).

## Что закрыто с прошлого круга

| Находка круга 3 | Факт на HEAD | Итог |
|---|---|---|
| Блокирующая: тесты 2 и 5 без подготовки плана | §4.5 тест 2 (571-572) и тест 5 (584) начинаются с `write_plan(root, constitution=True)`; перечень помощника (555-560) называет тесты 1-6, тест 7 — `constitution=False`, тест 8 — план в своём теле; `PLAN` (`tests/engine/engine_kit.py:29-54`) строки `Конституция:` не содержит — проверено (0 совпадений) | закрыто |
| Значимая: `build_config` на `:1466` приписан `upgrade` | §0 (98-107), D9 (312-320) и запись `CHANGELOG.md` (653-654) называют повторный `setup` и шаг мастера «Проект» (`_perform_step`, `setup_flow.py:1428`, вызов `:1466`); `upgrade` (`:398-436`) конфиг не трогает — `grep -n "build_config(" setup_flow.py` → 520 (def), 1172, 1466, других вызовов нет | закрыто |
| Значимая: «создание профиля `scout` — S11» | §0 (121-126), D7 (300-303), §3 (356-357), §4.3.4 (449-450) и §7 (761-762): профиль заводит владелец, ни этот шаг, ни S11 профилей не создают; §S11 плана (`:157-176`) — мастер с ботом, `ensure_profiles` (`setup_flow.py:539-567`) заводит три роли | закрыто |
| Значимая: стоп-сигнал 4 про `--collect-only` после правок | `:731` — «`pytest --collect-only` на исходном дереве (до первой правки) даёт не `464`»; ложного стопа на позднем прогоне больше нет | закрыто |
| Стилистическая: ключ записан литералом | §4.4.8 (511-513) — `f"planloop:{plan_id}:scout"` с явной оговоркой, что у temp-проекта тестов это `planloop:1:scout`, у плана 5 — `planloop:5:scout` | закрыто |
| Стилистическая: пустые ключи `ctx` без имён | §4.4.5 (491-493) — `ctx.update(scout_needed=False, scout_cards=[], scout_profile="", scout_note="")` | закрыто |

Новых расхождений правка круга 4 не принесла: изменён только файл постановки, все шесть
находок круга 3 закрыты по существу, а не переформулировкой.

## Контракт «Готово =» плана против постановки

| Пункт плана (`5-platforma.md:49-60`) | Как закрыт в постановке | Итог |
|---|---|---|
| Цель: разведка — роль с профилем, карточкой и вердиктом (стр. 51) | §4.1 (363-396): `SKILL.md`, ровно семь разделов, зона/процедура/таймбокс/приёмка, три исхода; §4.1.3 (378-393) — таймбокс 30 минут и «готового нет» как валидный исход | да |
| Ключ `scout` в `profiles`, `scout: [pipeline-scout]` в `skills` (стр. 55) | §4.2 (397-410): `skills.scout` и `runtimes.scout` ставит шаг; `profiles.scout` — владелец (D9, `AGENTS.md:24-26`), шаг проверяет Г8 (690-695) и при отсутствии останавливается (стоп-сигнал 5) | да, при действии владельца по рекомендации 1 |
| Роль установима (стр. 53) | §4.3.1 (414-416): строка `pipeline-scout` в `SKILL_CATEGORIES` между plan-authoring и agent-prompt-authoring; копирование `setup_flow.py:469-478`; Г6 (682-685) | да |
| Действие `create_scout` по образцу `create_prompt` (стр. 55) | §4.4.8 (511-520): ключ, заголовок, тело, профиль, скиллы, лимит, `result.update`, `touch`, `say`, пустой `cid`; сигнатура и флаги сверены с `gate.py:2375-2403` и `_create_card` (1407-1430) | да |
| Идемпотентный ключ `planloop:<план>:scout` (стр. 56) | D4 (276-287), §4.4.8 (511-514); тесты 1, 2, 8 (564-574, 595-619) | да |
| Карточка несёт план и вопрос (стр. 57) | §4.4.4 (472-484): `РАЗВЕДКА ПЛАНА N`, путь к плану, шаги, `ВОПРОС РАЗВЕДКИ`, `ЧТО СДЕЛАТЬ`, `ГРАНИЦЫ`, `ПРИЁМКА КАРТОЧКИ` | да |
| «ждёт разведку», повторный тик без дубля (стр. 58-59) | D7 (295-303), §4.4.6 (494-507), тест 2 (571-574); `watch_idle` (`gate.py:1536`) получает причину | да |
| По закрытии карточки план продолжает (стр. 58) | §4.4.6 (502-503), тест 3 (575-577) | да |
| «готового нет» — валидный исход, не стоп (стр. 57, 59-60) | D7 (297-299), §4.4.6 (502-503), тест 4 (578-583), пояснение в `create_step` (§4.4.9, 521-523) | да |
| Собственный гейт шага — восемь тестов, `8 passed` (стр. 54) | §4.5 (528-620): восемь имён и утверждения; Г1-Г2 (669-672) | да |

Объём расширен пятью файлами сверх списка шага (`tests/test_roles.py`,
`templates/pipeline.json.example`, `tests/test_setup_flow.py`, `plugin.yaml`, `CHANGELOG.md`) —
обосновано D8 (304-311) и правилами `AGENTS.md` («Добавить роль», «Проверки перед коммитом»);
возражений нет. Запреты §3 (322-360) закрывают `config.py`, `next_step.py`, `registry.py`,
`step_card.py`, `check_task_prompt.py`, `doctor.py`, `snapshot.py`, `ui_server.py`, `ui/**`,
`plan_lint.py`, `plan_accept.py`, остальные роли, ожидания чужих тестов, `docs/plans/**`,
`docs/research/**`, `docs/PLAN-FORMAT.md`, `docs/CONSTITUTION.md`, `AGENTS.md`, `README.md`,
`docs/CONTEXT.md`, отчёт ревью постановки, `profiles`/`driven_plans`, `.pipeline/**`,
миграции/сборки/секреты/сеть/Telegram, второй `-q`, коммит и пуш.
Блок §9 (802-828) требует вердикта роли `reviewer` в `docs/reviews/5-S2.md` — самопроверка
исполнителя шаг не закрывает.

## Находки

Блокирующих и значимых нет. Все ссылки `file:line` из §0, §2.1, §4 и §5 указывают на те же
конструкции, что и названы; числа прогонов и размеры файлов сходятся (см. ниже).

### Стилистические (правка не требуется, на приёмку не влияют)

[стилистическая] `docs/prompts/2026-11-06_5-S2_prompt.md:56-57` — «в прозе `scout`
встречается только в `docs/CONTEXT.md:196`, в тексте плана (§S2), в этой постановке и в отчёте
ревью»: кроме перечисленного, `scout` есть в §S3 плана (`docs/plans/5-platforma.md:65-67`) и
в постановке S1 (`docs/prompts/2026-11-06_5-S1_prompt.md:208`). Доказательная часть — рабочий
`grep` вне `docs/` пуст — верна, поэтому путаницы у исполнителя не будет.

[стилистическая] `docs/prompts/2026-11-06_5-S2_prompt.md:124` — диапазон
`setup_flow.py:539-589` шире функции `ensure_profiles` (она 539-567, дальше идут `DEFAULT_ROLES`
и `SMOKE_*`). Якорь `def ensure_profiles` на 539 верен, а §0 (62-63) прямо называет номера
строк ориентиром, поэтому адрес не мешает.

## Гейты и проверки (мои прогоны)

Код шага ещё не написан, поэтому гейты §5 в их итоговом виде непрогоняемы; проверено то, на
чём они стоят, — числа, адреса и механика на HEAD.

- `.venv/Scripts/python.exe -m pytest --collect-only` → `464 tests collected`; та же команда с
  `tests/engine` → `196`. Арифметика §0/§5 сходится: 196+8=204, 464+9=473.
- `.venv/Scripts/python.exe -m pytest tests/test_roles.py` → `7 passed`;
  `grep -c "^def test_" tests/test_plan_lint.py` → `24` — как в §0 (127-132).
- `.venv/Scripts/python.exe -m pytest tests/engine/test_step_card.py` → `4 passed`; с лишним
  `-q` печатаются только точки без строки итога — предупреждение §0 (66-68) верно.
- `PIPELINE_REPO="$(pwd -W)" .venv/Scripts/python.exe step_pipeline/engine/gate.py --plan 5
  --status` → EXIT 0, `Решение: idle — ревью постановки S2 идёт: карточка t_c81cae97 (running)`,
  ни `create_scout`, ни «ждёт разведку» → Г7 достижим, запись утверждения плана 5 лежит.
- Замер §2.1 на temp-проекте (`engine_kit.make_project`, свой прогон `next_step.py`): без
  `PIPELINE_REPO` — rc 2, `ошибка: нет файла конфигурации конвейера`, `next=None`, `steps=0`;
  с `PIPELINE_REPO` — `next=S1`, `steps=2`. Механизм теста 8 и объяснение §2.1 (238-243) верны.
- `hermes profile list | grep -ci scout` → `0`; `pipeline`, `prompter`, `reviewer` в списке
  есть → §0 (117-121) и D7 верны.
- `grep -n "build_config(" step_pipeline/setup_flow.py` → 520 (def), 1172, 1466; `upgrade`
  (398-436) в вызовах не участвует.
- `grep -rn "scout" step_pipeline tests pipeline.json docs/PLAN-FORMAT.md` → пусто (EXIT 1);
  `grep -n '"scout"' pipeline.json` → пусто (EXIT 1);
  `grep -c "pipeline-scout" step_pipeline/templates/pipeline.json.example` → `0`.
- `head -2 step_pipeline/plugin.yaml | tail -1` → `version: 0.10.0`; `grep -c "0.11.0"
  CHANGELOG.md` → `0`, верхняя запись — `## 0.10.0 — 05.11.2026` (строка 7) → новая запись
  встаёт перед ней, после правки `grep -c` даст `1`.
- `grep -n "^Конституция:" docs/plans/*.md` → единственное `docs/plans/5-platforma.md:3`;
  `driven_plans` = 2, 3, 4, 5 (`pipeline.json:35-40`) → дискриминатор D1 осмыслен.
- `git diff --name-only 8af93a1..6cdd407` → только постановка; `git show --stat 59e44da` →
  один файл (+84/−38); `git status --porcelain` → `?? docs/reviews/5-S2-prompt.md`.
- Размеры файлов совпадают с §0 (58-61): `gate.py` 3200, `pipeline.json` 60, шаблон 53,
  `setup_flow.py` 1582, `test_roles.py` 123, `plugin.yaml` 31, `CHANGELOG.md` 384,
  `roles/pipeline-prompter/SKILL.md` 127, `stub_hermes.py` 159.
- Сверка адресов: все ссылки `file:line` постановки прогнаны по HEAD — `build_ctx` 832-892
  (`plan_id` 838, режимы 843-848, ошибка 856-864, `cards` 875, `steps` 880, `next_step` 889),
  `decide` 901-925, `_decide_base` 928-1081 (утверждение 941-948, прошлый шаг 949-977, hold
  978-979, риск/карточки 981-1000, ревью 1001-1081), `_prefetch_decision` 1089-1110
  (1100-1101 — карточка шага в работе), `_prompt_review_needed` 1113-1122, `TERMINAL` 130,
  `read_cards` 418-433 и сборка 440-441, `_create_card` 1407-1430, `_step_of` 2644-2651,
  `step_key` 2653-2661, `create_step` 2334-2373 (`prompt_rel` 2336, заказ 2337-2356),
  `create_prompt` 2375-2403 (ключ 2391), `stop` 2554-2566, `run_gate` 2269, `watch_idle` 1536,
  `prompt_fresh` 493-502, `max_actions_of` 1509-1523, `collector` 821-829, `build_config`
  520-536, `SKILL_CATEGORIES` 68-75, копирование 469-478, `upgrade` 398-436,
  `ensure_profiles` 539-567, `engine_kit` 29-54/86-108/111-121/124-133, `stub_hermes` 27-49,
  `conftest` 21-37, `PLAN-FORMAT` 191-229/196/202-204/228-229, `pipeline.json`
  6-10/11-22/41/42-46, шаблон 28, `AGENTS.md` 24-26, §S11 157-176, `CONTEXT.md` 196,
  `plugin.yaml` 2.

## Проверено по коду (без находок)

- `read_cards` (`gate.py:418-441`) отдаёт `id`, `status`, `result`, `error`, `key`, архив
  отфильтрован, порядок `created_at ASC` — `_scout_decision` хватает всего, что нужно D7.
- `_create_card` (`:1407-1430`) и его вызов из `create_prompt` (`:2392-2394`) дают ровно ту
  сигнатуру, которую повторяет тест 8 (`p, title, body, assignee, key, skills, parent=None,
  max_runtime=None`); `run_gate` принимает `create_card` (`:2269`).
- `stub_hermes` (`tests/engine/stub_hermes.py:27-49`): колонки `skills` нет, `status` при
  `create` всегда `ready`, дедупликации по ключу нет — утверждения теста 1 и обоснование
  теста 8 верны.
- `write_approval` (`engine_kit.py:62-83`) пересчитывает sha файла плана, `make_project`
  (`:86-108`) кладёт `PLAN` на `:94` и принимает `profiles`/`skills`/`runtimes` через
  `data.update(overrides)` (`:103`) — `write_plan` обязан утверждать заново, как и написано.
- `run_setup` (`setup_flow.py:1147-1154`) берёт `board`/`driver`/`prompter`/`reviewer` из
  прежнего конфига, `build_config` вызывается с `existing` (`:1172`), и после правки D9
  (`setdefault` по прежним `profiles`) новый тест `test_setup_keeps_owner_profile_keys`
  должен пройти: первый прогон пишет тройку, второй сохраняет `scout` и `driver: shop`.
- `tests/test_setup_flow.py:68` сверяет `profiles` равенством только на свежем проекте
  (`existing` пуст), поэтому правка D9 чужой тест не красит; `-k` из Г6 находит
  `test_setup_copies_engine_roles_and_config` (`:51`) — вместе с новым тестом будет `2 passed`.
- `tests/test_roles.py:53` — единственное место, где считается число ролей
  (`grep -rn "len(roles)" tests/ step_pipeline/` → только оно); остальные тесты файла
  проверяют frontmatter и якоря, правка одной строки достаточна. Frontmatter промптера
  (`roles/pipeline-prompter/SKILL.md:1-13`) и правило `description` (`test_roles.py:74-86`)
  совпадают с §4.1.1.
- `docs/PLAN-FORMAT.md:191-229` (§5a) — заголовок, строка `Конституция:` (196), таблица
  кандидатов (202-204), три исхода и «`kill` — валидный исход» (228-229); реальный артефакт
  `docs/research/5-prior-art.md` — 52 строки, «Итог» (47-52) — «Готового продукта … нет».
- `paths.research` в `DEFAULTS` нет (`config.py:33-97`), `cfg.state` — свойство
  (`config.py:188-193`), `max_actions_of` (`gate.py:1509-1523`) — годный образец чтения
  сырого ключа, поэтому умолчание `docs/research` и чтение `profiles/skills/runtimes.scout`
  из `cfg.state` обоснованы.
- `_scout_decision` вызывается после блока утверждения и до прошлого шага (D6, §4.4.7):
  `prev`/`prev_verdict` ещё не читались, `step` в `ctx` уже есть; поля `step` в решении нет —
  `step_key` (`:2653-2661`) не посчитает его действием шага, тик (`:2840-2930`) при
  `key=None` завершает цикл, как и для `create_prompt`.
- `tests/engine/test_gate_decide.py` собирает `ctx` без ключей разведки (`:14-19`), значит
  `ctx.get("scout_needed")` даёт «не нужно» и старые тесты решений не краснеют (стоп-сигнал 2
  в постановке это и требует).

Итог: контракт шага закрыт полностью, находки прошлых кругов закрыты, новых блокирующих и
значимых расхождений нет — постановку можно принимать и запускать шаг после рекомендации 1.
