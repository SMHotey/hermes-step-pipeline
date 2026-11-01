---
name: pipeline-orchestrator
description: "Use when setting up or running the step pipeline across roles: реестр ролей, настройки конвейера, карточки, cron, стоп-фразы."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [pipeline, roles, kanban, cron, orchestration]
    category: software-development
    related_skills: [pipeline-prompter, pipeline-reviewer, pipeline-next-step, pipeline-plan-authoring]
---

# Конвейер шагов: роли, настройки, работа без остановок

Пайплайн = план + четыре роли + детерминированный страж. Роль = профиль Hermes
(воркер канбана всегда `hermes -p <assignee>`), специализация воркера задаётся
карточкой: `--skill` + тело карточки + `--workspace` + `--max-runtime`.

| Роль | Кто это | Что делает | Артефакт |
|---|---|---|---|
| `driver` (исполнитель) | профиль проекта | правит код по постановке, гоняет гейты, коммитит файлы шага | код + коммит |
| `prompter` | отдельный профиль, `reasoning_effort: max` | изучает контекст шага → пишет постановку | `<prompts_dir>/<дата>_<план>-<шаг>_prompt.md` |
| `reviewer` | отдельный профиль, `reasoning_effort: max` | независимое ревью по плану и коду | `<reviews_dir>/<план>-<шаг>.md` с вердиктом |
| страж | скрипт `gate.py`, без модели | ставит карточки, пишет журнал, коммитит | файл плана + отчёт ревью |

Почему страж — скрипт: «всё хорошо» — это машиночитаемый вердикт плюс проверки.
Агент на этом месте стоил бы прогона модели и мог бы согласиться с собственной прозой.
Ревьюер выносит суждение, скрипт его исполняет — ровно это разделение и есть смысл роли.

## Обязательные настройки профилей

Профиль-исполнитель (`driver`):

```
agent.max_turns: 300
kanban.max_in_progress: 1                 # на Windows MemTotal не читается -> unset = без лимита
kanban.max_in_progress_per_profile: 1     # один воркер за раз: одна БД, один тяжёлый прогон
kanban.failure_limit: 2                   # auto-block после 2 неудач
kanban.default_assignee: <driver>
skills.trusted_project_dirs: [<корень проекта>]   # иначе --skill не прелоадится
```

Профили `prompter` и `reviewer` создаются клоном с `agent.reasoning_effort: max`
(или `high`, если модель дорогая) и своими скиллами:

```
hermes profile create prompter --clone-from <driver> --description "Постановщик: контекст шага → промт"
hermes profile create reviewer --clone-from <driver> --description "Ревьюер: независимая проверка шага"
hermes -p prompter config set agent.reasoning_effort max
hermes -p reviewer config set agent.reasoning_effort max
```

Уровень рассуждений не переключают на ходу: он живёт в конфиге одноролевого профиля,
а не в переменной сессии. Возврат к `high` «после роли» запрещён — упавший воркер
оставит `max` навсегда либо ревью пойдёт на `high` незаметно.

## Конфигурация конвейера: `pipeline.json`

| Ключ | Смысл |
|---|---|
| `board` | борда канбана; все карточки создаются на ней (`--board`) |
| `profiles.driver/prompter/reviewer` | профили трёх ролей |
| `skills.step/review/prompt` | скиллы, форсируемые в карточки |
| `paths.plans/reviews/prompts/state` | каталоги планов, отчётов, постановок, состояния |
| `markers.journal/steps` | слова-маркеры разделов плана («журнал», «шаг») |
| `step_id_regex` | выражение идентификатора шага (`S3a`, `T10`, `S25-8`) |
| `driven_plans` | какие планы ведёт `--run` без `--plan`; `["*"]` — все |
| `require_prompt` | шаг без постановки не начинается (по умолчанию `true`) |
| `runtimes.step/review/prompt` | `--max-runtime` карточек |
| `git.commit/push` | коммитит ли страж журнал и отчёт ревью |
| `notify.enabled/token_env/chat_env` | уведомления владельцу (Telegram Bot API) |

## Карточки (копировать целиком)

Постановка (ручной заказ, если нужно поторопить промптера):

```
hermes kanban --board <борда> create "Промт: <план> <шаг>" --assignee prompter \
  --tenant plan --workspace "dir:<корень>" \
  --skill pipeline-prompter --skill agent-prompt-authoring --max-runtime 45m
```

Шаг (страж делает это сам, ключ идемпотентности обязателен):

```
hermes kanban --board <борда> create "[план 3 S1] <название>" --body-file <файл> \
  --assignee <driver> --tenant plan --workspace "dir:<корень>" \
  --idempotency-key "planloop:3:S1:step" --skill pipeline-next-step --max-runtime 3h
```

Ревью:

```
hermes kanban --board <борда> create "[ревью 3 S1 #1] <название>" --body-file <файл> \
  --assignee reviewer --tenant plan --workspace "dir:<корень>" \
  --idempotency-key "planloop:3:S1:review:1" --skill pipeline-reviewer --max-runtime 150m
```

Исполнитель обязан закрыть карточку через `kanban_complete` / `kanban_block`;
`rc=0` без канбан-вызова = нарушение протокола, карточка вернётся. Долгие операции —
`kanban_heartbeat`, иначе диспетчер убьёт процесс по reclaim.

## Работа без остановок

1. **cron-джоба стража** (no-agent, каждые 10 минут, доставка `local`):

```
hermes -p <driver> cron create "*/10 * * * *" --name "План: страж шага (конвейер)" \
  --no-agent --script pipeline_tick.py --deliver local --workdir <корень>
```

`--script` cron принимает ТОЛЬКО из `profiles/<driver>/scripts/` — установщик копирует
туда `tick.py` (файл сам находит `.pipeline/gate.py` по `PIPELINE_REPO`/`--workdir`).
Пустой stdout = конвейер ждёт; поэтому `--deliver local`, а не мессенджер.

2. **Стоп-фраза владельца**: «остановка после выполнения шага» в любой сессии =
   `python .pipeline/gate.py --stop "<фраза>"`. Стоп не рвёт текущий шаг — доводит до
   ревью и журнала, следующий не начинает. «Продолжай» = `--resume`.
3. **Ручной допуск**: `--hold <ШАГ>` для шагов с миграциями, деплоем, внешними
   сервисами. Страж держит `idle` с причиной «в списке ручного допуска».
4. **Уведомления**: при `notify.enabled=true` страж пишет владельцу на старте шага,
   при APPROVE, при инциденте и при втором REQUEST_CHANGES. Ошибка сети не валит
   конвейер.
5. **Правило одного прогона**: тяжёлые тесты — только по одному за раз, иначе
   «загадочные» падения из общей БД/слота.

## Как добавить роль-специалиста

```
hermes profile create <role> --clone-from <driver> --description "<зона одной фразой>"
hermes -p <role> config set platform_toolsets.cli '["file","web","browser","memory","skills","todo"]'
hermes -p <role> skills trust <корень проекта>
```

Затем скилл роли в репо: `.hermes/skills/<категория>/<role>/SKILL.md`
(`description` ВСЕГДА в кавычках: двоеточие с пробелом ломает YAML и скилл не индексируется).
В плане шаг получает исполнителя строкой: ``### S4. Название (2 дн) `frontend` ``.

## Приёмка работы роли

Воркер закрывает карточку с: путём артефакта, 3–5 строками итога, явным списком
допущений и следующим шагом. Нет артефакта — нет приёмки. Воркер в память не пишет:
знания — в тело карточки и в скиллы.

## Грабли

- **cron резолвит скиллы из профиля, а не из репо.** После правки роли-скилла в репо —
  скопировать в профиль и перезапустить гейтвей, иначе джоба напишет «skill not found».
- **Настройки канбана читаются при старте гейтвея.** Менять `kanban.*` и состав скиллов
  нужно до рестарта.
- **`HERMES_KANBAN_BOARD` в окружении сессии пинит борду**: команды без `--board`
  уходят на `default`. Всегда указывать `--board` явно.
- **`--skill` реально прелоадит скилл только при доверенном корне**
  (`skills.trusted_project_dirs`). Несуществующее имя попадает в `missing` — воркер
  получает предупреждение и работает без скилла.
- **`auto_decompose: true`** разворачивает сырую карточку в `triage` сама (3 карточки за
  тик). Нужен контроль — создавать сразу с телом, без `--triage`.
- **Параллельные шаги.** Страж не проверяет совместимость: перед `--unhold` двух шагов
  убедиться, что файлы не пересекаются и ресурсы (БД, порты, тестовые слоты) свободны.
