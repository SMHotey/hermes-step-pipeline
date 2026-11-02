# step_pipeline — конвейер шагов как плагин Hermes

Плагин превращает работу «по плану» в непрерывный конвейер: план → постановка
промптером → исполнение агентом → независимое ревью → журнал → следующий шаг.
Ставится одной командой, спрашивает токен Telegram-бота, поднимает локальный UI
с визуализацией и заводит cron, который двигает конвейер без участия человека.

## Установка (2 минуты)

```bash
# 1. скопировать плагин и включить
./install.sh              # Linux/macOS
install.ps1               # Windows (PowerShell)

# 2. поставить конвейер в проект — мастер спросит борду и токен бота
cd C:/work/my-project
hermes pipeline setup
```

`install.sh` / `install.ps1` копируют пакет в `<hermes_home>/plugins/step_pipeline`
и выполняют `hermes plugins enable step_pipeline`.

Альтернатива — из git (основной способ, начиная с 0.1.0):

```bash
hermes plugins install SMHotey/hermes-step-pipeline
hermes plugins update step_pipeline           # обновление
```

Имя плагина — `step_pipeline` (из `plugin.yaml`): каталог установки и все подкоманды
`hermes plugins` используют одно и то же имя.

Версия плагина — semver в `plugin.yaml` и теги `vX.Y.Z`; что менялось — `CHANGELOG.md`.
Движок в проектах помечен `.pipeline/VERSION`: `hermes pipeline setup --upgrade`
обновляет его, не трогая `pipeline.json`, роли и cron. `hermes pipeline doctor`
ругается, если движок в проекте отстал от плагина.

## Что спросит мастер `hermes pipeline setup`

1. **Борда канбана** — где живут карточки ролей (по умолчанию — имя каталога).
2. **Профиль исполнителя** — тот, что правит код (по умолчанию `default`).
3. **Токен бота от @BotFather** (ввод скрытый, Enter — пропустить).
4. **Chat id** — можно ввести руками, можно «напиши боту /start» и мастер поймает
   chat id сам; токен и чат проверяются живыми запросами к Bot API и тестовым
   сообщением.

Дальше без вопросов: копирует движок в `.pipeline/`, роли в `.hermes/skills/`,
пишет `pipeline.json`, создаёт профили `prompter`/`reviewer` (clone + `reasoning_effort
max`), ставит лимиты канбана, заводит cron `*/10 * * * *` (no-agent), добавляет
проект в доверенные.

Неинтерактивный вариант (CI, скрипт):

```bash
hermes pipeline setup --board myproj --driver myproj \
  --telegram-token 123456:ABC... --telegram-chat 42 --yes
```

## UI

```bash
hermes pipeline ui --detach        # поднять в фоне и выйти, живёт после закрытия терминала
hermes pipeline ui                 # в текущем терминале (Ctrl+C — остановить)
hermes pipeline ui --stop          # погасить фоновый UI
hermes pipeline ui --port 8899 --no-open
```

`--detach` пишет PID в `.pipeline/ui.json` и лог в `.pipeline/ui.log`; повторный запуск
на том же порту просто вернёт адрес.

Порт по умолчанию — 8770. Он переопределяется ключом `ui_port` в `pipeline.json`
проекта (он есть в шаблоне, который пишет мастер установки) или флагом `--port`;
флаг сильнее конфига. Порт живёт в конфиге проекта, а не в конфиге плагина: каждый
проект может поднять свой UI рядом.

```bash
hermes pipeline config --set ui_port 8899   # записать порт в pipeline.json проекта
hermes pipeline config --get ui_port        # проверить
```

### Предпросмотр без установки

```bash
hermes pipeline status --project C:/work/proj --preview --board <борда>
hermes pipeline ui --project C:/work/proj --preview --board <борда> --detach
```

Читает настоящие планы и карточки проекта движком из плагина: в проект не пишется
ничего, действия (тик/стоп) выключены. Полезно, чтобы увидеть состояние до установки.
Без `--preview` тот же проект без `.pipeline/` даёт ошибку «не установлено», а не
выдуманные данные.

Что видно:

- поток текущего шага (Постановка → Исполнение → Ревью → Журнал) с цветом статуса;
- все планы с числом закрытых шагов, следующим шагом и стоп-флагом;
- таблица шагов: дни, исполнитель, «сейчас» / «закрыт»;
- карточки ролей из борды (статус, исполнитель, ключ идемпотентности);
- хвост журнала плана и консоль действий;
- настройки: борда, ведомые планы, таймауты, «шаг без постановки не начинается»,
  Telegram (подключить / тест);
- кнопки: **Тик** (один прогон стража), **Стоп**, **Продолжить**.

UI слушает только localhost и не имеет авторизации — это операторский пульт
владельца, а не сетевой сервис.

## Формат плана

План конвейер читает как данные: разделы, заголовки шагов, пункты и таблицу журнала.
Правила формата собраны в одном нормативном документе —
[`docs/PLAN-FORMAT.md`](docs/PLAN-FORMAT.md): там же таблицы соответствия «что
проверяет линтер планов» и «что читает каждый парсер».

Проверить формат до запуска: `hermes pipeline doctor --plan` (все планы проекта) или
`hermes pipeline doctor --plan 1` (один план). Образец для копирования —
`step_pipeline/templates/plan-template.md`.

## CLI

```bash
hermes pipeline setup        # установка в проект (спрашивает токен)
hermes pipeline status       # состояние + решение стража (--json для машин)
hermes pipeline run          # один тик стража
hermes pipeline ui           # локальный UI
hermes pipeline doctor       # диагностика: движок, борда, cron, Telegram, планы
hermes pipeline notify-test  # тестовое сообщение в Telegram
hermes pipeline config --get paths.plans
hermes pipeline config --set runtimes.step 5h
```

Проект ищется вверх от текущего каталога по `pipeline.json`; можно указать
`--project <путь>` или переменную `PIPELINE_REPO`.

## Инструменты агента

| Инструмент | Что делает |
|---|---|
| `pipeline_status` | снимок: планы, шаги, карточки, решение стража, Telegram |
| `pipeline_run` | один тик (заказ постановки / карточка шага / ревью / закрытие) |
| `pipeline_control` | `stop`, `resume`, `hold`, `unhold` |

Так агент в любой сессии («продолжай план») видит, где остановился конвейер, и не
угадывает задачу.

## Роли и настройки

| Роль | Профиль | Настройки |
|---|---|---|
| исполнитель | `profiles.driver` | `max_turns=300`, `kanban.max_in_progress_per_profile=1`, `failure_limit=2` |
| постановщик | `prompter` | `reasoning_effort=max`, скиллы `pipeline-prompter`, `agent-prompt-authoring` |
| ревьюер | `reviewer` | `reasoning_effort=max`, скилл `pipeline-reviewer`, кап `150m` |
| страж | скрипт без модели | cron `*/10 * * * *`, `--no-agent`, `--deliver local` |

Почему страж — скрипт: «всё хорошо» = вердикт + проверки (наличие отчёта, форма
вердикта, существование коммита). Агент здесь стоил бы прогона модели и мог бы
согласиться с собственной прозой.

## Безопасность и секреты

- Токен бота хранится в `.env` профиля (`STEP_PIPELINE_TELEGRAM_BOT_TOKEN`, права 0600)
  и больше нигде; в UI показывается только признак «настроено».
- `.pipeline/state/` и `.pipeline/.env` добавляются в `.gitignore` проекта.
- Исходящие запросы — только `api.telegram.org`; HTTP-клиенты `urllib`, внешних
  зависимостей нет (Python stdlib + Hermes).

## Структура

```
step_pipeline/
  plugin.yaml        манифест: tools, config_schema, secrets
  __init__.py        register(): CLI `pipeline` + 3 инструмента
  cli.py             setup/status/run/ui/doctor/notify-test/config
  setup_flow.py      мастер установки, профили, cron, Telegram
  telegram.py        Bot API: getMe, getUpdates, getChat, sendMessage
  snapshot.py        снимок состояния (планы, шаги, карточки, журнал)
  ui_server.py       stdlib HTTP-сервер UI + API действий
  ui/index.html      одностраничный интерфейс
  engine/            движок, который копируется в проект (.pipeline/)
  roles/             скиллы ролей (копируются в .hermes/skills/)
  templates/         план, промт, pipeline.json
tests/
  conftest.py        фикстуры плагина (пакет, поддельный hermes, борда)
  test_*.py          плагин: мастер, UI, Telegram, инструменты, предпросмотр
  engine/            тесты движка как отдельного слоя (реестр, разбор плана, страж)
.github/workflows/   CI: pytest на ubuntu+windows, py3.11/3.13, валидация манифеста
CHANGELOG.md         что менялось и почему (semver)
LICENSE              текст лицензии MIT
```

Движок правится ТОЛЬКО здесь, в `step_pipeline/engine/`; в проектах-потребителях
`.pipeline/` — вендоренная копия, её обновляет `setup --upgrade`.

## Удаление

```bash
hermes plugins disable step_pipeline
hermes plugins remove step_pipeline
```

Движок в проекте остаётся рабочим сам по себе (`python .pipeline/gate.py --run`),
роли — обычные скиллы проекта.

## Лицензия

MIT — текст целиком в файле [`LICENSE`](LICENSE). Плагин можно использовать, менять
и распространять свободно, сохраняя строку копирайта; гарантий автор не даёт.
