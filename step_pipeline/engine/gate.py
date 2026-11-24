# -*- coding: utf-8 -*-
"""Страж шага: конвейер «шаг → ревью → следующий шаг» без остановок.

Разделение ролей, которое и есть смысл конструкции:
    исполнитель  делает шаг и коммитит его;
    промптер     заранее пишет постановку шага (без неё шаг не начинается);
    ревьюер      выносит машиночитаемый вердикт по плану и коду;
    страж (этот скрипт) исполняет вердикт: ставит карточки, пишет журнал, коммитит.

Почему страж — скрипт, а не «сотрудник-агент»: решение «всё хорошо» — это вердикт
(`<reviews_dir>/<план>-<шаг>.md`) плюс проверки. Агент на этом месте стоил бы
модельного прогона и мог бы согласиться с собственной прозой.

Состояния (до `max_actions` действий за тик, cron каждые 10 минут):

    шага нет                  → карточка шага        (профиль исполнителя)
    нет постановки шага       → карточка промптеру + инцидентный стоп
    шаг done                  → карточка ревью       (профиль ревьюера)
    вердикт APPROVE           → строка в журнал плана + коммит/пуш
    вердикт REQUEST_CHANGES   → есть блокирующая находка: карточка правок (1-й раз),
                                стоп + уведомление (2-й раз);
                                блокирующих нет: шаг закрывается «с замечаниями»,
                                замечания — строками реестра находок плана
    ревью постановки: REQUEST_CHANGES → карточка промптеру «переписать» + повторное ревью,
                                        стоп только по исчерпании `max_fix_rounds`
    шаг упал/ревью упало      → стоп + уведомление
    план не утверждён         → карточки нет: причина в выводе, одно уведомление владельцу
    план изменён после утв.   → то же: sha256 файла не совпал с записью утверждения
    план уже идёт             → унаследованная запись (`inherited`) и работа продолжается

Утверждение плана сверяется перед любой карточкой: запись `plan_<НОМЕР>_approval.json`
в каталоге состояния плюс пересчёт sha256 файла плана. Исключение — план, который уже
идёт (есть файл состояния или закрытые шаги журнала): ему страж пишет унаследованную
запись и ведёт дальше, иначе шаг перекрыл бы дорогу сам себе. Второе исключение —
правка плана карточкой по разведке (0.20.0): после неё запись не наследуется, а простой
несёт готовую команду `hermes pipeline plan --accept N` — утверждает владелец.

Стоп-флаг блокирует ТОЛЬКО начало следующего шага: текущий шаг доводится до ревью и
журнала (это и значит «остановка ПОСЛЕ выполнения шага»).

Сколько действий делает тик, задаёт `max_actions` в `pipeline.json` проекта (умолчание 1 —
прежнее поведение: ровно одно действие за тик), флаг `--max-actions N` перекрывает это
значение на один запуск (cron берёт значение из файла). Состояние перечитывается перед
каждым следующим действием, поэтому цепочка «ревью APPROVE → журнал → следующий шаг →
заказ постановки» проезжается одним тиком. Инвариант предсказуемости: за тик шаг
получает не больше ОДНОГО действия — иначе быстрый воркер успел бы закрыть карточку
между действиями, и тик проскочил бы шаг целиком (исполнение и ревью сразу). Каждое
применённое действие дописывается в состояние плана (`last_actions`, последние 20
записей) и в историю тиков: файл `history.jsonl` в каталоге состояния, по строке JSONL на
действие, с ротацией по числу строк и размеру. Посчитанные по истории метрики отдаёт
`--status --json` (ключ `metrics`), снимок — под тем же ключом: видно, сколько было
действий за неделю, сколько шагов закрыто, сколько ушло на шаг и сколько было кругов
правок и инцидентов.

Простой виден сам: `idle` пишет `waiting_since`/`waiting_reason`, ожидание дольше
`notify.watchdog_hours` даёт одно напоминание на причину не чаще раза в час, а раз в
сутки после `notify.digest_at` уходит сводка (закрыто, ждёт, стоп — по строке на план).

Расписание работы: окна паузы из `quiet_hours` (до двух интервалов системного времени,
окно может идти через полночь) гасят тик целиком — в окне `--run` выходит с кодом 0 и не
трогает ни доску, ни план, ни сводку. `--pause` отвечает, действует ли окно сейчас.
Команды владельца (`--stop`, `--resume`, `--hold`) паузы не слушают.

Использование:
    python .pipeline/gate.py --status            # что видно и что было бы сделано
    python .pipeline/gate.py --plans             # таблица планов: шаг, закрыто, стоп
    python .pipeline/gate.py --plan 3 --status   # состояние конкретного плана
    python .pipeline/gate.py --run               # тик: до max_actions действий (режим cron)
    python .pipeline/gate.py --run --max-actions 3   # предел на один запуск
    python .pipeline/gate.py --run --plan 3      # тик только по одному плану
    python .pipeline/gate.py --pause             # пауза по расписанию: окна, действует ли сейчас
    python .pipeline/gate.py --stop "причина"    # остановиться после текущего шага
    python .pipeline/gate.py --resume            # снять стоп
    python .pipeline/gate.py --hold S3a          # шаг требует ручного допуска владельца
    python .pipeline/gate.py --unhold S3a
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402 - исход идеи, вопросы и ретроспектива (план 5, S10)
import approval  # noqa: E402 - утверждение плана и привязка ревью к версии (S6)
import config as config_mod  # noqa: E402 - модуль рядом
import registry  # noqa: E402 - реестр планов
import plan_cycle  # noqa: E402 - гейт корректировки плана по разведке (план 5, S4)
import next_step  # noqa: E402 - шаги плана: проверка номера в ручном допуске
import check_task_prompt  # noqa: E402 - форма постановки: спека high-шага (8-S2B)
import state_io  # noqa: E402 - атомарная запись состояния и блокировка тика
import step_metrics  # noqa: E402 - метрика попытки шага: время по фазам (план 17, S1)

# ──────────────────── целостность движка ────────────────────
# Проект работает на КОПИИ движка в `.pipeline/`. Если движок правили в плагине,
# а копию не обновили, на одном плане оказываются две разные логики с одинаковым
# номером версии (прецедент 02.11.2026: gate.py 1364 строки в проекте против 1599
# в плагине, оба 0.2.0). Маркер ENGINE.sha256 пишется при установке/обновлении,
# тик сверяет его и отказывается работать при расхождении.
ENGINE_MARKER = "ENGINE.sha256"


def engine_digest(base: Path) -> str:
    """sha256 по содержимому файлов движка из `ENGINE_FILES`: имя + байты, порядок стабилен.

    Хешируются ровно файлы движка, а не все `*.py` каталога: в `.pipeline/` проекта
    законно лежат вспомогательные скрипты (свой `check_*.py`, проверки), и раньше их
    появление меняло хеш — маркер «расходился» без единой правки движка, а удаление
    скрипта роняло тик (раскатка 07.11.2026 в DoorSystem). Пропущенный файл списка
    здесь не ошибка: о нём отдельно говорит `verify_engine_integrity`.
    """
    digest = hashlib.sha256()
    for name in sorted(config_mod.ENGINE_FILES):
        path = Path(base) / name
        if not path.is_file():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_engine_integrity(base: Path) -> tuple[bool, str]:
    """Сверить движок с маркером. Маркера нет — легаси-установка, не мешаем.

    Порядок проверок — от дешёвой к дорогой: пустой маркер, пропавший файл движка,
    затем хеш. Пропавший файл называется по имени: «движок изменён» на вопрос «почему
    тик не идёт» не отвечает.
    """
    marker = Path(base) / ENGINE_MARKER
    if not marker.is_file():
        return True, "маркер целостности отсутствует (легаси-установка)"
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return False, f"маркер не читается: {exc}"
    expected = text.split()[0].removeprefix("sha256:") if text else ""
    if not expected:
        return False, "маркер пуст"
    missing = [name for name in sorted(config_mod.ENGINE_FILES)
               if not (Path(base) / name).is_file()]
    if missing:
        return False, f"нет файла движка: {missing[0]}"
    actual = engine_digest(base)
    if actual != expected:
        return False, (f"движок изменён после установки: {actual[:12]} != {expected[:12]}")
    return True, "движок соответствует маркеру"


TERMINAL = {"done", "blocked", "crashed", "timed_out", "gave_up"}

# Архивная карточка — закрытая, а не висящая (находка F1 ревью 10-S2A): `hermes kanban
# archive` ставит `archived` уже закрытой карточке, и собственный читатель карточек стража
# её не видит (`read_cards`, `status <> 'archived'`). В набор `TERMINAL` статус не входит
# намеренно: набор читают `reused_card` и ветки `_decide_base`, где архив — отдельный
# случай, а не терминальность, и смена набора изменила бы их решения.
CARD_HIDDEN_STATUSES = frozenset({"archived"})

# ── Предел ожидания карточки (план 10, S2A) ──────────────────────────────────
# Источники метки «карточка жива»: те же, что у сканера смотрителя (S2), плюс
# комментарии. Свежий комментарий и есть жизнь карточки: ревьюер объяснил, что
# делает, — конвейер останавливать не за что. Таблица или колонка отсутствует —
# источник просто не участвует (схема борды от версии к версии разная).
CARD_EVENT_SOURCES = (("task_events", "created_at"), ("task_runs", "last_heartbeat_at"),
                      ("task_runs", "ended_at"), ("comments", "created_at"))

# Разбор вердикта — один шаблон на весь движок: он живёт в `approval.py`, где его читает
# и привязка ревью плана к версии (S6). Здесь только псевдонимы для кода стража.
VERDICT_RE = approval.VERDICT_RE
COMMIT_RE = approval.COMMIT_RE

# ── Разведка готовых решений (план 5, S2) ────────────────────────────────────
# Роль разведки живёт в конфиге ключом `scout`: `skills.scout` и `runtimes.scout` пишет
# шаг, `profiles.scout` — только владелец (`AGENTS.md`). Поэтому ключи читаются сырым
# словарём конфига с умолчаниями, а не создаются: правка владельца не должна теряться,
# а её отсутствие не должно валить тик.
SCOUT_KEY = "scout"
SCOUT_PROFILE_DEFAULT = "scout"
SCOUT_SKILLS_DEFAULT = ("pipeline-scout",)
SCOUT_RUNTIME_DEFAULT = "30m"
RESEARCH_DIR_DEFAULT = "docs/research"
# Признак валидного исхода «готового нет» (D7): ищется в результате/ошибке карточки без
# учёта регистра — провал без этой фразы останавливает конвейер, с ней — нет.
SCOUT_NO_READY = "готового нет"
# Строка шапки плана действующего формата (`docs/PLAN-FORMAT.md:196`) — та же метка,
# что читает `approval.has_constitution`: один признак формата на движок.
CONSTITUTION_MARK = approval.CONSTITUTION_MARK

# ── Ревью плана как отдельный шаг (план 5, S5) ───────────────────────────────
# Вид карточки в `idempotency_key`: номер круга правок идёт отдельным сегментом ключа
# (`planloop:<план>:planreview:2`), поэтому вид остаётся узнаваемым для `card_of`.
PLAN_REVIEW_KEY = "planreview"
PLAN_FIX_KEY = "planfix"
# Правка плана по разведке (0.20.0): вид карточки в ключе `planloop:<план>:scoutfix:<круг>`.
# Отдельный вид, а не `planfix`: у разведки свой бюджет кругов, и по ключу видно, из-за
# чего план правился — по ревью плана или по разведке.
PLAN_SCOUTFIX_KEY = "scoutfix"
PLAN_REVIEW_SKILLS_DEFAULT = ("pipeline-plan-reviewer",)
PLAN_FIX_SKILLS_DEFAULT = ("pipeline-plan-authoring",)
PLAN_REVIEW_RUNTIME_DEFAULT = "45m"
PLAN_FIX_RUNTIME_DEFAULT = "45m"
# Ключ профиля ревью плана: `profiles.plan_reviewer` — владельческий, как у разведки.
PLAN_REVIEW_PROFILE_KEY = "plan_reviewer"
# Ключ профиля правки плана: `profiles.plan_fixer`, иначе исполнитель плана (D3).
PLAN_FIX_PROFILE_KEY = "plan_fixer"
# Лимит автоматических кругов правок плана: каждый `REQUEST_CHANGES` даёт карточку правок,
# пока не исчерпан `max_fix_rounds` проекта (конституция п.4: одна правка редко закрывает
# все замечания). Это же число — умолчание для копий движка без ключа в конфиге.
PLAN_REVIEW_MAX_ROUNDS = 2
# Допуск расширения плана правкой (план 7, S4): столько процентов версии плана и столько
# символов на каждую закрытую находку правка может добавить. Действует МЕНЬШЕЕ из двух:
# иначе кратная находкам граница перекрывала бы процент при любом объёме плана. Умолчание
# проекта — ключ `max_plan_growth` в `pipeline.json`.
# Числа допуска карточка правок называет из конфигурации проекта (`plan_fix_card_body`).
PLAN_GROWTH_PERCENT = 10
PLAN_GROWTH_CHARS_PER_FINDING = 400
# Требование к карточке правок: место пояснений ревьюеру — отчёт и журнал, не план.
# Числа допуска карточка правок называет из конфигурации проекта (`plan_fix_card_body`).
# Уведомление о зависшем конвейере: единый заголовок, частота и файл отметок.
# Заголовок обязан быть громким и одинаковым: по нему владелец отличает «конвейер
# сломался» от обычных сообщений о ходе плана.
ATTENTION_HEADER = "ВНИМАНИЕ!!! Требуется починить конвейер."
ATTENTION_GAP = 3600              # одна тревога на план — не чаще раза в час
ATTENTION_FILE = "attention.json"
# Сколько одинаковых действий подряд считаем зависанием: тик идёт раз в 10 минут,
# три повтора — это полчаса без единого сдвига. Ключ `notify.stuck_repeats`.
STUCK_REPEATS_DEFAULT = 3
# Состояние вердикта «привязан к текущей версии плана» (S6): само слово живёт в
# `approval.py`, здесь — имя для решений стража.
PLAN_REVIEW_FRESH = approval.REVIEW_FRESH

# Предел действий за тик: ключ `max_actions` в pipeline.json проекта, умолчание 1 (D1).
MAX_ACTIONS_DEFAULT = 1
# Сколько кругов правок по ревью разрешено автоматически, если в pipeline.json не задано
# `max_fix_rounds`. Два круга — правило владельца: одна правка редко закрывает все замечания,
# а второй отказ уже требует решения человека.
DEFAULT_FIX_ROUNDS = 2
# Предел кругов правок для шага с `Риск: high` (план 12, S3): умолчание проекта — ключ
# `max_fix_rounds_high`. Ступеней выше `high` нет — `_risk_of` знает три значения и любую
# нераспознанную пометку сводит к `high`.
DEFAULT_FIX_ROUNDS_HIGH = 6
# ANCHOR:rounds_classification
# Классификация серий карточек кругов: что считает бюджет правок шага, а что нет.
# «Бюджет» — находка ревью по существу, серия `:fix:{N}` (правка кода шага) и
# `:promptfix:{N}` (переписывание постановки шага): обе — круг правок шага, обе берут лимит
# по риску (`step_fix_limit`).
# «Не бюджет»: `:promptfix-stale:{N}` — пересборка протухшей постановки, у неё свой лимит
# `max_stale_rebuilds` (сдвиг HEAD и правка по ревью — разные события); `:rereview:{N}` —
# повтор прогона ревью, а не правка.
# Классы держат тесты `tests/engine/test_fix_rounds_risk.py`: пункт 5 (пересборка не съедает
# бюджет `:fix:`), пункт 6 (`:rereview:` не двигает ни один из двух бюджетов).
# Упавшая карточка круга в классификацию не входит: бюджет считает карточки независимо от
# статуса, а при упавшей карточке тик уходит в стоп — это поведение не меняется.
# Сколько записей о действиях держим в состоянии плана: это краткая выжимка, полная
# история — в `history.jsonl`.
LAST_ACTIONS_KEPT = 20
# История тиков: файл в каталоге состояния, пределы ротации и окно метрик (план 2, S3).
HISTORY_NAME = "history.jsonl"
HISTORY_MAX_LINES = 2000
HISTORY_MAX_BYTES = 512 * 1024
METRICS_DAYS = 7
# Маркер ручных кругов правок плана: файл в каталоге состояния (`paths.state`).
# Имя — часть контракта: тот же файл читает панель (S7), поля — `extra_fix_rounds`,
# `at`, `reason`. Движок читает только число кругов, остальное — для человека.
ROUNDS_FILE = "plan_{plan}_rounds.json"
# Комментарий с находками в карточке правок: автор, предел длины и сколько находок берём.
COMMENT_AUTHOR = "страж"
COMMENT_MAX_LEN = 4000
FINDINGS_LIMIT = 5
# Пределы текста находок в уведомлении о правках плана (0.34.0): находки — в сообщении,
# а не только счётом. Строка режется с многоточием, общий бюджет оставляет место шапке
# сообщения и не упирается в лимит Telegram в 4096 символов.
NOTICE_TEXT_CAP = 260
NOTICE_ACTION_CAP = 140
NOTICE_TOTAL_CAP = 3300
# Реестр находок плана (план 7, S1): закрытые словари классов и статусов, шапка таблицы.
# Класс выбирает маршрут находки (S3), статус решает вердикт (S2): блокирует только `открыто`.
FINDING_KINDS = ("замысел", "двусмысленность", "формат-стража", "гейт-исполнение", "стиль")
FINDING_STATUSES = ("открыто", "закрыто", "не блокирует")
FINDINGS_HEADER = ("ID", "Шаг", "Класс", "Находка", "Что сделать", "Статус")
FINDING_ID_RE = re.compile(r"F(\d+)")
# Реестр находок постановки (план 12, S2): та же таблица без колонки «Шаг» — постановка
# принадлежит одному шагу. Порядок ключей идёт за шапкой: разбор ячеек и запись строки
# живут на одном списке ключей, иначе шапка и данные разошлись бы молча.
FINDINGS_KEYS = ("id", "step", "kind", "text", "action", "status")
PROMPT_FINDINGS_HEADER = ("ID", "Класс", "Находка", "Что сделать", "Статус")
PROMPT_FINDINGS_KEYS = ("id", "kind", "text", "action", "status")
# Блокирующая сила находки постановки (0.37.2) — из класса, а не из подписи ревьюера.
# Симптом, ради которого правило заведено: три круга правок постановки 9-S2 держались
# строками «двусмысленность» (находки настоящие — постановка неверно описывала ветви кода),
# а соседние строки того же веса («стиль») шаг не держали: статус ставился на глаз, и один
# класс то держал шаг, то нет. `закрыто` остаётся за ревьюером: это утверждение о факте
# правки, а не степень важности.
PROMPT_FINDING_STATUS = {"замысел": "открыто", "двусмысленность": "открыто",
                         "гейт-исполнение": "открыто", "формат-стража": "не блокирует",
                         "стиль": "не блокирует"}
# Классы находок в отчёте ревью шага (план 13, S1): метка в квадратных скобках в пункте
# раздела «Находки». Сверка вердикта с классами решает, откроется ли круг правок, поэтому
# разбор один: `report_finding_classes` считает, `_decide_base` решает.
FINDING_CLASS_STEMS = (("блокирующ", "блокирующая"),
                       ("значим", "значимая"),
                       ("стилистическ", "стилистическая"))
FINDING_SECTION_WORD = "наход"
FINDING_CLOSED_WORD = "закрыт"
FINDING_LABEL_RE = re.compile(r"^\[([^\[\]]{1,120})\]")
FINDING_SUBLABEL_SEP = "/"
# Текст строки реестра плана для замечания, принятого при закрытии шага (§4.3): класс
# ревью остаётся в тексте меткой, класс строки — из словаря реестра, статус — не держит.
REMARK_KIND = "стиль"
REMARK_STATUS = "не блокирует"
REMARK_ACTION = "принято при закрытии шага, шаг принят; закрывает ревьюер плана фактом"
# Признак закрытия «с замечаниями» — в тексте `_evidence` (четвёртая колонка журнала).
# Его же читает решение (`journal_closed_with_remarks`): закрытие с замечаниями при
# REQUEST_CHANGES — штатное состояние, а не расхождение.
REMARKS_EVIDENCE = "принято с замечаниями"
# Вопросы шага (план 8, S5): своя таблица того же механизма, что реестр находок, и своей
# шапки. Файл — `docs/reviews/plan-<N>-questions.md`; ID вида `Q-N`, статусы из двух слов.
# Вопрос закрывает владелец (ответ, `/answer`, окно панели или автовыбор рекомендации),
# поэтому реестр находок и файл вопросов не смешиваются: вердикт ревью плана считается по
# находкам, а вопрос вердикт не двигает.
QUESTIONS_HEADER = ("ID", "Шаг", "Вопрос", "Варианты", "Рекомендация", "Статус", "Ответ")
QUESTIONS_KEYS = ("id", "step", "text", "options", "recommend", "status", "answer")
QUESTION_STATUSES = ("открыт", "закрыт")
QUESTION_OPEN, QUESTION_CLOSED = "открыт", "закрыт"
QUESTION_ID_RE = re.compile(r"Q-(\d+)")
# Строка вопроса в отчёте карточки (постановка, ревью, исполнение):
#   `Q1: <вопрос> — A) <вариант> (цена) / B) <вариант> (цена) — рекомендация A`
# Маркер списка перед `Q` допускается: отчёт — markdown, автор пишет пунктом.
QUESTION_LINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?Q-?\s*(\d+)\s*:\s*(?P<body>\S.*)$")
#: Начало блока вопроса: того же вида, но без тела — по нему режется абзац автора.
QUESTION_LINE_START_RE = re.compile(r"^\s*(?:[-*+]\s*)?Q-?\s*\d+\s*:")
# Разделитель «вопрос — варианты»: длинное тире, короткое и дефис. Автор пишет по-разному,
# а строка вопроса — не место для спора об оформлении.
QUESTION_DASH_RE = re.compile(r"\s+[—–-]{1,2}\s+")
# ── Замер вопроса вместо вопроса (S2B плана 10) ───────────────────────────────────────────
# Вопрос, ответ на который даёт команда, не должен ждать человека: план 15 стоял на Q-1
# «копия не равна выпуску» 1 ч 30 мин, а ответ — `sha256sum` плюс `grep -c`, полминуты.
# Страж исполняет такую команду сам, пишет строку «замерил: …» в историю тиков и при
# `questions.enforce: true` (умолчание) вовсе не заводит строку вопроса.
#: Команда замера — первый фрагмент в обратных кавычках абзаца вопроса: разобранный текст
#: кавычки уже снял (`parse_question_line`), поэтому адрес — сырой блок `question_blocks`.
QUESTION_COMMAND_RE = re.compile(r"`([^`\n]{1,200})`")
#: Белый список первого токена: только чтение. `sha256sum`/`shasum` — хеш файла, `grep` —
#: подсчёт и поиск, `git` — одна подкоманда ниже. Пайп, редирект и `sh -c` не исполняются:
#: такой вопрос не измерим и заводится как обычный (стоп-сигнал постановки 10-S2B).
QUESTION_MEASURE_PROGRAMS = ("sha256sum", "shasum", "grep", "git")
#: У `git` разрешена одна подкоманда: остальные читают историю и состояние, но ответа на
#: вопрос «равна ли копия выпуску» не дают, а разрешать весь `git` — это уже shell.
QUESTION_MEASURE_GIT_SUBCOMMAND = "rev-parse"
#: Символы, которых в команде быть не может: оболочка по ним разбирает строку, а движок
#: исполняет argv без shell. Замер с ними — не замер.
QUESTION_SHELL_CHARS = ("|", "&", ";", "<", ">", "$", "(", ")", "{", "}", "\n")
#: Предел одного замера. Дольше — замер не удался, вопрос остаётся владельцу.
QUESTION_MEASURE_TIMEOUT = 60
#: Предел строки «замерил: …» в истории: та же длина, что у `detail` в `remember_action`.
QUESTION_MEASURE_DETAIL = 200
#: Ключ состояния плана с отметками исполненных замеров: `(шаг, текст вопроса)` → запись.
#: Отметка нужна, чтобы повторный тик не исполнял команду снова и не писал вторую строку
#: (`questions.enforce: true` не заводит строку вопроса — файла, по которому дедуплицировать,
#: там нет).
QUESTION_MEASURE_STATE_KEY = "question_measures"
# Вариант с ценой: буква, скобка, текст. Буквы латинские и кириллические (`А)`, `Б)`),
# но только буквы вариантов: иначе «и.т.д.» в тексте варианта стало бы вариантом.
QUESTION_OPTION_RE = re.compile(r"(?<![А-Яа-яA-Za-z])([ABCDАБВГ])\s*[)\.]\s*")
# Рекомендация — буква варианта; «рекомендации нет» — явный отказ от автовыбора.
# Регистр не важен, как у соседнего правила: маркер `рекомендаци\w*` в `parse_question_line`
# ищется с `re.I`, автор пишет «Рекомендация» с большой буквы. Без флага маркер находился,
# хвост отрезался, а буква не читалась — рекомендация молча терялась, автовыбор по таймауту
# не срабатывал, вопрос стоял до ручного ответа (живой Q-1, 21.11.2026).
QUESTION_RECOMMEND_RE = re.compile(r"рекомендац\w*\s*:?\s*([А-Яа-яA-Za-z])(?![А-Яа-яA-Za-z])", re.I)
QUESTION_NO_RECOMMEND_RE = re.compile(r"рекомендац\w*\s*(?:нет|отсутствует|—|-|:?\s*$)", re.I)
# Автовыбор рекомендации по таймауту (план 8, S5 п.5): секунды, 0 — выключить. Время вопроса
# живёт в состоянии плана (`questions: {"Q-1": {"asked_at", "notified_at"}}`), а не в
# таблице: шапка файла остаётся нормативной.
# Час: вопрос — решение владельца, а не дежурная мелочь; пяти минут не хватало (17.11.2026).
ANSWER_TIMEOUT_DEFAULT = 3600
AUTO_ANSWER_NOTE = "авто: рекомендация по таймауту"
# Тревога «вопрос без рекомендации ждёт владельца»: не чаще раза в 6 ч на вопрос. Автовыбор
# по таймауту такой вопрос не закрывает (нет буквы), поэтому единственный признак простоя —
# тревога; без неё вопрос молчит до ответа человека (21.11.2026, план 34: 3 ч простоя и вся
# ночь впереди, причины «рекомендации нет» таймера не имели).
QUESTION_ALARM_GAP = 21600
# Напоминание о вопросе — не чаще раза в час: тот же порядок, что у тревог и сторожа.
QUESTION_NOTICE_GAP = 3600
#: Код возврата «команда владельца не применена: идёт тик». Отдельный от 0, потому что код 0
#: читается панелью и `plans_api` как «сделано»: ответ на вопрос, не записанный из-за
#: блокировки, выглядел успехом (случай 17.11.2026). Коды 2 (конфиг и разбор) и 3 (движок
#: не сходится с маркером) заняты, поэтому 4.
EXIT_BUSY = 4

# Постановка следующего шага и ревью постановки (план 2, S6): отметка свежести recon
# и что считать расхождением постановки с кодом.
RECON_RE = re.compile(r"recon_commit\s*[:=]\s*`?([0-9a-f]{7,40})", re.IGNORECASE)
# Правки вне `docs/` — расхождение постановки с кодом (D4): постановка и отчёты ревью
# лежат в `docs/`, коммит самой постановки свежесть не отменяет.
FRESH_DIFF_ARGS = [".", ":(exclude)docs"]


# ───────────────────────────── окружение ─────────────────────────────

@dataclass
class Paths:
    """Все внешние точки, которые трогает страж (в тестах подменяются на временные)."""

    cfg: config_mod.Config
    repo: Path
    plan_file: Path
    review_dir: Path
    kanban_db: Path
    state_file: Path
    stop_file: Path
    plan_id: str
    journal_heading: str
    review_prefix: str
    spec: registry.PlanSpec

    @property
    def step_re(self) -> re.Pattern:
        return self.cfg.step_regex


class GateError(RuntimeError):
    """Понятный сбой движка: текст уходит в стоп-флаг, уведомление и `--json`."""

    def __init__(self, message: str, plan_id: str | None = None):
        super().__init__(message)
        self.plan_id = plan_id


def resolve_paths(cfg: config_mod.Config, plan_id: str | None = None) -> Paths:
    """Пути выбранного плана. Неизвестный номер — честная ошибка реестра."""
    want = registry.normalize(plan_id or registry.default_plan_id(cfg))
    spec = registry.get(cfg, want)
    return Paths(
        cfg=cfg,
        repo=cfg.repo,
        plan_file=Path(spec.file),
        review_dir=cfg.reviews_dir,
        kanban_db=cfg.kanban_db,
        state_file=Path(spec.state_file),
        stop_file=Path(spec.stop_file),
        plan_id=spec.id,
        journal_heading=spec.journal_heading,
        review_prefix=spec.review_prefix,
        spec=spec,
    )


def history_path(cfg: config_mod.Config) -> Path:
    """Файл истории тиков проекта: `<state_dir>/history.jsonl` (каталог может не быть)."""
    return Path(cfg.state_dir) / HISTORY_NAME


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or Path.cwd()), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def _hermes() -> str:
    return shutil.which("hermes") or "hermes"


def _python(p: Paths) -> str:
    for cand in (Path(p.repo) / ".venv" / "Scripts" / "python.exe",
                 Path(p.repo) / ".venv" / "bin" / "python",
                 Path(p.repo) / "venv" / "Scripts" / "python.exe"):
        if cand.is_file():
            return str(cand)
    return sys.executable


# ───────────────────────────── состояние ─────────────────────────────

def load_state(p: Paths) -> dict:
    path = Path(p.state_file)
    if not path.exists():                 # отсутствие файла — норма
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"файл состояния не читается: {path}: {exc}", plan_id=p.plan_id) from exc
    return data if isinstance(data, dict) else {}


def save_state(p: Paths, state: dict) -> None:
    try:
        state_io.save_json(Path(p.state_file), state)
    except (OSError, ValueError, TypeError) as exc:
        raise GateError(f"состояние не записывается: {Path(p.state_file)}: {exc}",
                        plan_id=p.plan_id) from exc


def load_stop(p: Paths) -> dict | None:
    path = Path(p.stop_file)
    if not path.exists():                 # стоп-флага нет — норма
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"файл стоп-флага не читается: {path}: {exc}",
                        plan_id=p.plan_id) from exc
    return data if isinstance(data, dict) else {"reason": str(data)}


def write_stop(p: Paths, reason: str, source: str, kind: str | None = None) -> dict:
    """`kind=owner` — снимает только владелец; `kind=incident` — страж снимает сам."""
    payload = {"reason": reason, "source": source,
               "kind": kind or ("owner" if source == "owner" else "incident"),
               "at": time.strftime("%d.%m.%Y %H:%M:%S")}
    try:
        state_io.save_json(Path(p.stop_file), payload)
    except (OSError, ValueError, TypeError) as exc:
        raise GateError(f"стоп-флаг не записывается: {Path(p.stop_file)}: {exc}",
                        plan_id=p.plan_id) from exc
    return payload


def clear_stop(p: Paths) -> bool:
    path = Path(p.stop_file)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


def rounds_path(p: Paths) -> Path:
    """Маркер ручных кругов правок плана: `plan_<N>_rounds.json` в каталоге состояния."""
    return Path(p.cfg.state_dir) / ROUNDS_FILE.format(plan=p.plan_id)


def load_extra_fix_rounds(p: Paths) -> int:
    """Сколько дополнительных кругов правок плана разрешил владелец маркером.

    Правила чтения: файла нет, он не читается, это не словарь, ключа `extra_fix_rounds`
    нет, значение `bool`, отрицательное или не число → 0. Целое ≥ 0 берётся как есть,
    строка из цифр — тоже (маркер правят руками), дробное без остатка (`1.0`) — как целое:
    так пишут некоторые сериализаторы, и жест владельца иначе теряется молча. Дробное
    с остатком (`1.5`) — «не число кругов», тоже 0.
    Битый маркер — не повод для `GateError` и не повод для стопа: тик идёт дальше с нулём
    дополнительных кругов. Разбор не бросает: `isdigit()` истинно и для «²», которые
    `int()` не принимает, а на цифровой строке длиннее лимита CPython (4300) `int()`
    отказывает — оба случая отдаёт толерантный `_as_int`.
    """
    path = rounds_path(p)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    value = data.get("extra_fix_rounds")
    if isinstance(value, bool):           # `true` — не число кругов, хотя int(True) == 1
        return 0
    if isinstance(value, float):          # 1.0 — целое, 1.5 — не число кругов
        value = int(value) if value.is_integer() else 0
    number = _as_int(value, 0)            # единый толерантный разбор: функция `_as_int` ниже в файле
    return number if number > 0 else 0


# ───────────────────────────── вердикт ─────────────────────────────

def review_path(p: Paths, step_id: str) -> Path:
    return Path(p.review_dir) / f"{p.review_prefix}-{step_id}.md"


def verdict_of(p: Paths, step_id: str) -> dict | None:
    """Разобрать отчёт ревьюера. None — отчёта нет; verdict=NONE/AMBIGUOUS — отчёт негоден."""
    return verdict_at(review_path(p, step_id))


def verdict_at(path: Path) -> dict | None:
    """Разбор файла вердикта по пути — тело переехало в `approval.verdict_at` (S6).

    Один шаблон на весь движок: ревью шага, ревью постановки и ревью плана обязаны
    читать строку вердикта одинаково. Обёртка остаётся ради вызывающего кода стража.
    """
    return approval.verdict_at(path)


# ───────────────────────────── журнал плана ─────────────────────────────

def _journal_rows(p: Paths) -> list[int]:
    """Индексы строк таблицы журнала: раздел ищет реестр планов, не страж."""
    lines = Path(p.plan_file).read_text(encoding="utf-8", errors="replace").splitlines()
    found = registry.journal_section(lines, p.cfg.journal_marker, p.step_re)
    return list(found[1]) if found else []


def journal_closed(p: Paths, step_id: str) -> bool:
    """Закрыт ли шаг в журнале своего плана (идентификатор — по выражению плана)."""
    path = Path(p.plan_file)
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i in _journal_rows(p):
        if registry.step_of_row(lines[i], p.step_re) != step_id.upper():
            continue
        if registry.status_of_row(lines[i]).startswith("ЗАКРЫТ"):
            return True
    return False


def journal_closed_with_remarks(p: Paths, step_id: str) -> bool:
    """Шаг закрыт строкой «принято с замечаниями» (13-S1).

    Признак берётся из журнала — того же текста `_evidence`, который видит владелец:
    с 13-S1 «журнал закрыт + отчёт REQUEST_CHANGES» стало штатным состоянием (шаг принят
    с замечаниями), и ветка повторного ревью не имеет права его задевать. Расхождением
    остаётся только закрытие НЕ по замечаниям — например, ручное «закрыт» при отчёте
    «нужны правки».

    Шага в журнале нет, строка не закрыта, колонок меньше четырёх или план не читается —
    `False`: ветка повторного ревью ведёт себя как прежде.
    """
    path = Path(p.plan_file)
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    found = registry.journal_section(lines, p.cfg.journal_marker, p.step_re)
    if not found:
        return False
    for i in found[1]:
        if registry.step_of_row(lines[i], p.step_re) != step_id.upper():
            continue
        if not registry.status_of_row(lines[i]).startswith("ЗАКРЫТ"):
            continue
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        return len(cells) >= 4 and REMARKS_EVIDENCE in cells[3]
    return False


def append_journal_row(p: Paths, step_id: str, title: str, commit: str, evidence: str) -> bool:
    """Записать строку журнала. False — шаг уже закрыт (идемпотентно).

    Если строка шага уже есть (шаблон плана заводит их со статусом «открыт»), она
    ПЕРЕЗАПИСЫВАЕТСЯ, а не дублируется: два ряда на один шаг ломают чтение журнала.
    """
    path = Path(p.plan_file)
    if not path.is_file():
        raise SystemExit(f"нет файла плана: {path}")
    if journal_closed(p, step_id):
        return False
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rows = _journal_rows(p)
    if not rows:
        raise SystemExit("в плане нет таблицы журнала — строка не добавлена")
    date = time.strftime("%d.%m.%Y")
    cells = [date, f"{step_id} ({title})", commit or "—", evidence or "ревью APPROVE", "ЗАКРЫТ"]
    row = "| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |"
    target = None
    for i in rows:
        if registry.step_of_row(lines[i], p.step_re) == step_id.upper():
            target = i
            break
    if target is not None:
        lines[target] = row
    else:
        lines.insert(rows[-1] + 1, row)
    state_io.atomic_write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    return True


def _git_ignored(p: Paths, rel: str) -> bool:
    """Путь не отслеживается git (`.gitignore`): `git check-ignore -q` вернул 0.

    Нужен коммиту шага: отчёт ревью лежит в `docs/reviews/`, а этот каталог проект держит
    вне репозитория. Раньше `git add` на такой путь возвращал 1, и коммит шага навсегда
    оставался в `pending_commit` — журнальная строка не попадала в git никогда.
    """
    result = _run(["git", "check-ignore", "-q", "--", rel], p.repo)
    return result.returncode == 0


def commit_and_push(p: Paths, files: list[str], message: str) -> tuple[bool, str]:
    """Закоммитить ТОЛЬКО перечисленные файлы; чужие правки в индексе остаются в индексе."""
    if not p.cfg.git_commit:
        return True, "(коммит выключен в pipeline.json)"
    status = _run(["git", "-c", "core.quotepath=off", "status", "--porcelain", "-z"], p.repo)
    entries = [e for e in status.stdout.split("\0") if e.strip()]
    dirty = [e[3:].strip() for e in entries]
    allowed = {f.replace("\\", "/") for f in files}
    strangers = [d for d in dirty if d.replace("\\", "/") not in allowed]
    if strangers:
        print("[commit] в дереве чужие правки — не трогаю: " + ", ".join(strangers[:5]))
    # Игнорируемые файлы (например `docs/reviews/` в `.gitignore`) не ошибка: `git add` на
    # такой путь возвращает 1, и раньше это навсегда вешало коммит шага в `pending_commit`.
    # Отчёт ревью живёт вне репозитория по решению проекта — коммитим только то, что git
    # действительно отслеживает.
    tracked = [f for f in files if not _git_ignored(p, f)]
    if not tracked:
        sha = _run(["git", "rev-parse", "--short", "HEAD"], p.repo).stdout.strip()
        return True, f"нечего коммитить: своих отслеживаемых изменений нет (HEAD {sha})"
    files = tracked
    add = _run(["git", "add", "--", *files], p.repo)
    if add.returncode != 0:
        return False, f"git add: {add.stderr.strip()[:200]}"
    diff = _run(["git", "diff", "--cached", "--name-only", "--", *files], p.repo)
    if diff.returncode != 0:
        return False, f"git diff: {diff.stderr.strip()[:200]}"
    changed = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
    if changed:
        commit = _run(["git", "commit", "-m", message, "--", *files], p.repo)
        if commit.returncode != 0:
            return False, f"git commit: {(commit.stderr or commit.stdout).strip()[:200]}"
    # sha читается один раз и уже ПОСЛЕ коммита: в ветке «нечего коммитить» HEAD не меняется,
    # поэтому одно чтение покрывает обе ветки, а detail всегда указывает на коммит шага.
    sha = _run(["git", "rev-parse", "--short", "HEAD"], p.repo).stdout.strip()
    detail = sha if changed else f"нечего коммитить: своих изменений нет (HEAD {sha})"
    if p.cfg.git_push:
        upstream = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                         "@{upstream}"], p.repo)
        if upstream.returncode != 0:
            detail += " · пуш пропущен: у ветки нет upstream"
        else:
            push = _run(["git", "push"], p.repo, timeout=180)
            if push.returncode != 0:
                return False, f"git push: {(push.stderr or push.stdout).strip()[:200]}"
            detail += " · пуш выполнен"
    return True, detail


# ───────────────────────────── доска ─────────────────────────────

def _run_summaries(con: sqlite3.Connection) -> dict[str, str]:
    """Сводка последнего прогона каждой карточки: `task_runs.summary` (13-S4, §4.3).

    `kanban_complete(summary=…)` не заполняет `tasks.result`, а без сводки ответ
    исполнителя по форме не нашёлся бы и карточка возвращалась бы зря. Таблицы нет
    (поддельная борда старых тестов, старая борда) или колонок мало — пустой словарь:
    чтение борды не имеет права валить тик.
    """
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "task_runs" not in tables:
            return {}
        columns = {row[1] for row in con.execute("PRAGMA table_info(task_runs)")}
        if not {"id", "task_id", "summary"} <= columns:
            return {}
        rows = con.execute(
            "SELECT task_id, COALESCE(summary, '') FROM task_runs"
            " WHERE id IN (SELECT MAX(id) FROM task_runs GROUP BY task_id)"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(task_id): str(summary or "") for task_id, summary in rows}


def read_cards(p: Paths, key_prefix: str) -> list[dict]:
    db = Path(p.kanban_db)
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            # `created_at`/`started_at` нужны проверке свежести отчёта
            # (`_report_written_after`), но набор колонок борды от версии к версии разный:
            # берём их такой записью, какая есть, вместо падения на «no such column».
            have = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            extra = "".join(
                f", COALESCE({name}, 0) AS {name}"
                for name in ("created_at", "started_at") if name in have)
            cur = con.execute(
                f"""SELECT id, title, status, assignee, idempotency_key, completed_at,
                          COALESCE(result,'') AS result,
                          COALESCE(last_failure_error,'') AS last_failure_error{extra}
                   FROM tasks
                   WHERE idempotency_key LIKE ? AND status <> 'archived'
                   ORDER BY created_at ASC""",
                (key_prefix + "%",),
            )
            columns = [item[0] for item in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            summaries = _run_summaries(con)
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        # Существующая база есть, а прочитать нельзя — это сбой, а не «карточек нет»:
        # иначе тик молча теряет ход конвейера.
        raise GateError(f"борда недоступна: {db}: {exc}", plan_id=p.plan_id) from exc
    return [{"id": r["id"], "title": r["title"], "status": r["status"],
             "assignee": r["assignee"], "key": r["idempotency_key"],
             "completed_at": int(r["completed_at"] or 0),
             "result": r["result"], "summary": summaries.get(str(r["id"]), ""),
             "error": r["last_failure_error"],
             "created_at": int(r.get("created_at") or 0),
             "started_at": int(r.get("started_at") or 0)} for r in rows]


def card_status(p: Paths, card_id: str, key: str) -> str:
    """Статус карточки по её ключу: `hermes kanban create` идемпотентен и на повторный
    ключ возвращает прежнюю карточку ЛЮБОГО статуса, включая закрытую. Пустая строка —
    карточку не видно (новую создавали не мы или база недоступна)."""
    try:
        cards = read_cards(p, key)
    except GateError:
        return ""
    for card in cards:
        if str(card.get("id") or "") == str(card_id):
            return str(card.get("status") or "")
    return ""


def watchdog_limits(raw: object) -> dict:
    """Пороги смотрителя и стража из сырого `pipeline.json` (план 10, S2A).

    Тонкая обёртка над разбором шага S2 (`config._watchdog`): у диагностики нет
    загруженного `Config` — `doctor` держит сырой словарь `proj.read_config`, а
    `config.load` в нём падает на любом другом сломанном ключе ровно там, где
    диагностика нужнее всего. Своего разбора здесь нет: второй разбор — второй
    ответ на один вопрос. Умолчания (30/12) живут в `config.DEFAULTS`, не тут.
    """
    section = raw.get("watchdog") if isinstance(raw, dict) else None
    return dict(config_mod._watchdog(section))


def _stamp(value: object) -> int:
    """Метка борды в epoch; не число, `null` и отрицательное — 0 («метки нет»)."""
    if isinstance(value, bool):
        return 0
    try:
        stamp = int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return stamp if stamp > 0 else 0


def _board_cards(con: sqlite3.Connection, key_prefix: str = "") -> list[dict]:
    """Карточки борды с меткой последнего события (план 10, S2A; только чтение).

    Схема борды от версии к версии разная: колонки берутся по фактическому
    `PRAGMA table_info`, отсутствующая таблица или колонка — не ошибка, а
    «метки нет» (образец — `_run_summaries`). Нет `id`/`status` — таблица не
    борда: `sqlite3.Error` наружу, вызывающий отвечает пустым результатом.

    Цена на живой борде (находка F4 ревью 10-S2A): 99.7 мс на проход при 667 карточках
    и 21 190 событиях — четыре `GROUP BY task_id` читают события всей борды, а ключ
    фильтрует только `tasks`. Проход идёт на каждый тик каждого ведомого плана и на
    каждый `--status` панели: для тика раз в 10 минут терпимо, для панели заметно.
    Сужение событий до отобранных карточек (`WHERE task_id IN (…)`) — возможное
    ускорение, но оно меняет чтение при деградации схемы, поэтому цена записана,
    а не спрятана.
    """
    have = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
    if not {"id", "status"} <= have:
        raise sqlite3.Error("в таблице tasks нет колонок id/status")
    columns = [name for name in ("id", "title", "status", "idempotency_key",
                                 "created_at", "started_at", "last_heartbeat_at")
               if name in have]
    query = "SELECT " + ", ".join(columns) + " FROM tasks"
    params: tuple = ()
    if key_prefix:
        if "idempotency_key" not in have:      # фильтровать нечем — чужих не берём
            return []
        query += " WHERE idempotency_key LIKE ?"
        params = (key_prefix + "%",)
    cards = [dict(zip(columns, row)) for row in con.execute(query, params)]
    events: dict[str, int] = {}
    for table, column in CARD_EVENT_SOURCES:
        try:
            rows = con.execute(f"SELECT task_id, MAX({column}) FROM {table}"
                               " GROUP BY task_id").fetchall()
        except sqlite3.Error:
            continue                           # таблицы или колонки нет — что есть, то и берём
        for card_id, stamp in rows:
            if stamp is None:
                continue
            events[str(card_id)] = max(events.get(str(card_id), 0), _stamp(stamp))
    for card in cards:
        card["last_event"] = max(_stamp(card.get("created_at")),
                                 _stamp(card.get("started_at")),
                                 _stamp(card.get("last_heartbeat_at")),
                                 events.get(str(card.get("id")), 0))
    return cards


def stuck_cards(db, *, deadline_hours: int, fresh_minutes: int, now: float,
                key_prefix: str = "") -> dict:
    """Висящие карточки борды: предел ожидания по последнему событию (план 10, S2A).

    Одно исполнение правила «карточка висит» на стража и на диагностику (`build_ctx`
    для своего плана, `doctor.collect` для всей борды): второй разбор в другом месте
    дал бы владельцу два ответа на один вопрос.

    Возврат: `{"stuck": [...], "nearest": ...}`.

    - `stuck` — только просроченные (`age > deadline_hours * 3600`), по убыванию `age`:
      причина стопа называет первую запись, то есть самую старую карточку;
    - `nearest` — непросроченная карточка с наименьшим остатком
      `remaining = deadline_hours * 3600 - age` (`0` — ровно на пределе, ещё не
      висящая) либо `None`;
    - `age`/`remaining` — целые секунды, карточка — `{id, status, title}`.

    Возраст — от максимума меток: `tasks.created_at`, `tasks.started_at`,
    `tasks.last_heartbeat_at`, `MAX(task_events.created_at)`,
    `MAX(task_runs.last_heartbeat_at)`, `MAX(task_runs.ended_at)`,
    `MAX(comments.created_at)`. `running` со свежим heartbeat (`fresh_minutes`) —
    живой шаг, а не висящий: он не попадает ни в `stuck`, ни в `nearest`.
    Нетерминальность — по `TERMINAL` стража, а не по набору сканера: `blocked` страж
    считает терминальным и останавливаться из-за него не имеет права. Архив
    (`CARD_HIDDEN_STATUSES`) — тоже не висящий: это закрытая карточка, которую страж
    не видит и своим читателем (`read_cards`).

    Деградация молчаливая (`_run_summaries`, `_board_cards`): нет базы, таблицы,
    колонки, у карточки ни одной метки времени — карточка никуда не попадает. Ложный
    стоп на самодельной борде хуже пропущенного. `deadline_hours <= 0` — проверка
    выключена (пустые ключи). Часы приходят параметром `now`: `_now()` остаётся
    единственной точкой подмены времени.
    """
    empty: dict = {"stuck": [], "nearest": None}
    try:
        limit = float(deadline_hours or 0)
    except (TypeError, ValueError):
        return empty
    if limit <= 0:
        return empty
    try:
        path = Path(db) if db else None
    except TypeError:
        return empty
    if path is None or not path.is_file():
        return empty
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cards = _board_cards(con, key_prefix)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return empty
    deadline = int(limit * 3600)
    fresh = max(int(fresh_minutes or 0), 0) * 60
    for card in cards:
        status = str(card.get("status") or "")
        if status in TERMINAL or status in CARD_HIDDEN_STATUSES:
            continue                           # закрытая, архивная — не висящая (F1)
        heartbeat = _stamp(card.get("last_heartbeat_at"))
        if status == "running" and heartbeat and now - heartbeat <= fresh:
            continue                           # живой шаг: ни висящий, ни «ближайший»
        last = int(card.get("last_event") or 0)
        if last <= 0:
            continue                           # времени нет — возраст не вычисляем
        age = int(max(now - last, 0))
        entry = {"id": str(card.get("id") or ""), "status": status,
                 "title": str(card.get("title") or ""), "age": age}
        if age > deadline:
            empty["stuck"].append(entry)
            continue
        remaining = deadline - age
        nearest = empty["nearest"]
        if nearest is None or remaining < nearest["remaining"]:
            empty["nearest"] = dict(entry, remaining=remaining)
    empty["stuck"].sort(key=lambda item: item["age"], reverse=True)
    return empty


def next_fix_round(cards: list[dict], fallback: int = 1) -> int:
    """Следующий свободный номер круга правок плана по ключам уже созданных карточек.

    Номер в ключе обязан быть уникальным на всю историю плана: ядро карточек дедуплицирует
    по `idempotency_key` и на занятый ключ возвращает прежнюю карточку любого статуса.
    """
    rounds: list[int] = []
    for card in cards:
        match = re.search(rf":{PLAN_FIX_KEY}:(\d+)$", str(card.get("key") or ""))
        if match:
            rounds.append(int(match.group(1)))
    if rounds:
        return max(rounds) + 1
    try:
        return max(int(fallback or 1), 1)
    except (TypeError, ValueError):
        return 1


def card_of(cards: list[dict], kind: str) -> list[dict]:
    """Карточки одного вида конвейера: `:<kind>` или `:<kind>:<номер>` в ключе."""
    pattern = re.compile(rf":{kind}(:\d+)?$")
    return [c for c in cards if pattern.search(c.get("key") or "")]


# ───────────────────────────── тексты карточек ─────────────────────────────

def _report_rel(p: Paths, step_id: str) -> str:
    return p.cfg.rel(Path(p.review_dir) / f"{p.review_prefix}-{step_id}.md")


def prompt_rel(p: Paths, step_id: str) -> str | None:
    """Постановка шага: `<prompts_dir>/<дата>_<план>-<шаг>_prompt.md` (дата любая)."""
    folder = Path(p.cfg.prompts_dir)
    if not folder.is_dir():
        return None
    suffix = f"_{p.plan_id}-{step_id}_prompt.md"
    matches = sorted(folder.glob(f"*{suffix}"))
    if not matches:
        return None
    return p.cfg.rel(matches[-1])


# ───────────── отпечаток входа постановки: второй признак устаревания (план 12, S1) ─────────────

#: Литерал вместо `sha256` для входа, которого нет на диске (или это не файл).
INPUT_ABSENT = "absent"


def prompt_input_parts(p: Paths, step: dict) -> dict[str, str]:
    """`{путь: sha256}` по входам постановки: файлы из «Файл:» шага плюс сама постановка.

    Вход — то, с чего снята постановка. Отпечаток считается по **рабочему дереву**, а не по
    git: несохранённая в коммит правка входа — тоже изменение (решение D3 шага). Нет файла
    постановки — пустой словарь: признак молчит, решение принимается по `recon_commit`.

    Стоимость — только чтение файлов, ни одного `subprocess`: тик по-прежнему делает один
    `git diff` (`build_ctx`).
    """
    sid = str((step or {}).get("id") or "")
    prompt = prompt_rel(p, sid) if sid else None
    if not prompt:
        return {}
    rels = {str(item).strip() for item in ((step or {}).get("files") or []) if str(item).strip()}
    rels.add(prompt)
    parts: dict[str, str] = {}
    for rel in sorted(rels):
        try:
            parts[rel] = hashlib.sha256((Path(p.repo) / rel).read_bytes()).hexdigest()
        except (OSError, ValueError):
            # Нет файла, это каталог, путь битый — литерал `absent`, а не исключение наружу.
            parts[rel] = INPUT_ABSENT
    return parts


def prompt_input_fp(parts: dict[str, str]) -> str:
    """Агрегат отпечатка для показа в карточке: `sha256` от склейки `путь\\0hash\\n`.

    Решение принимается по пофайловому `parts`, а не по агрегату: «другой отпечаток»
    одинаково звучит и при подмене одного файла другим, а карточка обязана назвать, что
    именно изменилось.
    """
    if not parts:
        return ""
    joined = "".join(f"{rel}\0{parts[rel]}\n" for rel in sorted(parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def prompt_input_changed(ctx: dict) -> dict[str, tuple[str, str]]:
    """`{путь: (было, стало)}` по разошедшимся входам; пусто — изменения нет.

    Опора (`state["prompt_input"][шаг]`) пишется в момент заказа ревью постановки: «эта
    редакция уходит ревьюеру». Опора есть и что-то разошлось — признак сработал; нет опоры,
    нет в ней `parts` или текущие `parts` пусты — признак молчит (иначе первый же тик после
    правки состояния пересобирал бы постановку без причины, решение D10).
    """
    baseline = (ctx.get("prompt_input_baseline") or {}).get("parts") or {}
    current = ctx.get("prompt_input_parts") or {}
    if not baseline or not current:
        return {}
    changed: dict[str, tuple[str, str]] = {}
    for rel in sorted(set(baseline) | set(current)):
        was = str(baseline.get(rel) or INPUT_ABSENT)
        now = str(current.get(rel) or INPUT_ABSENT)
        if was != now:
            changed[rel] = (was, now)
    return changed


def prompt_recon(path: Path | str | None) -> str | None:
    """`recon_commit` из шапки постановки: короткий или полный sha; нет отметки — None.

    Отметку пишет промптер при снятии recon. Нет её — свежесть не подтвердить, поэтому
    в режиме `auto` такая постановка ревью не требует (D5): иначе после шага встал бы
    каждый проект со старыми постановками.
    """
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = RECON_RE.search(text)
    return found.group(1).lower() if found else None


def scout_research_dir(cfg: config_mod.Config) -> Path:
    """Каталог артефактов разведки: `paths.research`, умолчание `docs/research` (D2).

    Относительный путь разрешается от корня проекта; конфиг читается сырым словарём,
    `config.py` не правится.
    """
    raw = _config_section(cfg, "paths").get("research")
    value = raw.strip() if isinstance(raw, str) else ""
    path = Path(value or RESEARCH_DIR_DEFAULT)
    return path if path.is_absolute() else Path(cfg.repo) / path


def scout_artifact(p: Paths) -> Path:
    """Артефакт разведки плана: `<research>/<номер>-prior-art.md` (§5a, D2).

    Для плана 5 это `docs/research/5-prior-art.md`; существование файла и означает
    «разведка уже есть».
    """
    return scout_research_dir(p.cfg) / f"{p.plan_id}-prior-art.md"


def plan_has_constitution(p: Paths) -> bool:
    """План действующего формата: строка `Конституция:` в шапке до первого `## ` (D1).

    Дискриминатор, а не украшение: планы 2-4 и фикстуры тестов строки не имеют и карточку
    разведки получать не должны. Тело проверки живёт в `approval.has_constitution` (S6):
    ревью плана и утверждение обязаны видеть формат плана одинаково.
    """
    return approval.has_constitution(p.plan_file)


def scout_needed(p: Paths) -> bool:
    """Разведка нужна: план действующего формата и артефакта разведки ещё нет (D1+D2)."""
    return plan_has_constitution(p) and not scout_artifact(p).is_file()


def head_commit(p: Paths) -> str:
    """sha текущего HEAD; git недоступен или не репозиторий — пустая строка."""
    try:
        res = _run(["git", "rev-parse", "HEAD"], Path(p.repo), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip().lower() if res.returncode == 0 else ""


# ───────────────── прогоны шага: исполнение стражем (план 9, S2) ─────────────────
# Прогон — пункт «Прогон» шага, разобранный `next_step` в `StepRun`. Страж исполняет
# прогоны ПЕРЕД ревью: красный прогон отсекает шаг карточкой правок с находкой F03 и не
# тратит круг ревью, зелёный — заказывает ревью, а его строка идёт в отчёт ревьюера,
# в уведомление и в строку журнала. Исполнение не держится на плагине: движок копируется
# в проект (`ENGINE_FILES`), поэтому здесь только stdlib и subprocess.

#: Каталог доказательств прогона: один файл на план-шаг-sha (`state/runs/`).
RUNS_DIR_NAME = "runs"
#: Хвост вывода прогона в доказательстве и в карточке правок (2000 символов, S2).
RUN_TAIL_CHARS = 2000


def runs_dir(p: Paths) -> Path:
    """Каталог доказательств прогона: `<state_dir>/runs/` (создаётся при записи)."""
    return Path(p.cfg.state_dir) / RUNS_DIR_NAME


def runs_proof_path(p: Paths, step_id: str, sha: str) -> Path:
    """Доказательство прогонов шага: `plan-{план}-{шаг}-{sha}.json`, полный sha.

    Файл на план-шаг-sha, а не на паре «sha и команда» (D9): команда живёт внутри
    файла, и счётчик N/M доказательства считается по шагу целиком.
    """
    return runs_dir(p) / f"plan-{p.plan_id}-{str(step_id).upper()}-{sha}.json"


def load_runs_proof(p: Paths, step_id: str, sha: str) -> dict | None:
    """Доказательство прогона текущего sha: нет файла, битый файл или не объект — `None`.

    «Доказательства нет» — не сбой, а триггер исполнения: именно этот `None` читает
    `runs_gate_decision`, а `run_gate` по нему запускает прогоны (§4 п.5-6). Падение на
    битом файле оставило бы шаг навсегда — файл пишет прошлая версия движка.
    """
    if not sha:
        return None
    try:
        data = json.loads(runs_proof_path(p, step_id, sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_runs_proof(p: Paths, proof: dict) -> None:
    """Записать доказательство прогона атомарной заменой (`state_io.save_json`).

    Путь собирается из самого доказательства (`plan`, `step`, `sha`), поэтому запись из
    `run_step_runs` и запись `fix_card` из ветки `create_fix` попадают в один файл.
    `OSError` не глотается: без записи ветвь «fix_card заполнен» недостижима, и следующий
    тик заказал бы второй круг на том же sha (D6).
    """
    runs_dir(p).mkdir(parents=True, exist_ok=True)
    path = runs_proof_path(p, str(proof.get("step") or ""), str(proof.get("sha") or ""))
    try:
        state_io.save_json(path, proof)
    except OSError as exc:
        raise GateError(f"доказательство прогона не записано: {path}: {exc}",
                        plan_id=p.plan_id) from exc


def runs_proof_for(p: Paths, step_id: str, sha: str) -> dict | None:
    """Доказательство прогона к коммиту шага (§4 п.8).

    Точно по коммиту из отчёта ревью, а если для него файла нет — по текущему HEAD:
    ревью обычно смотрит последний коммит шага, и расхождение сокращённого sha в отчёте
    с полным именем файла не должно терять строку журнала. Нет ни того, ни другого —
    `None`: строка прогона в журнал и уведомление не добавляется.
    """
    proof = load_runs_proof(p, step_id, sha) if sha else None
    return proof if proof is not None else load_runs_proof(p, step_id, head_commit(p))


def runs_argv(p: Paths, command: str) -> list[str]:
    """`argv` прогона: строка плана режется `shlex.split` в posix-режиме, `shell` не
    участвует (D1): поведение `cmd.exe` и `sh` на одной строке различается, а план
    обязан давать один и тот же результат на любой машине.

    Первый токен `python`/`python3` заменяется на интерпретатор движка (D2): голый
    `python` в PATH — чужой venv, а строки «- Прогон» почти всегда начинаются с него.
    Другие имена не подставляются: `ruff` в PATH нет, и это строка плана (D8).
    """
    argv = shlex.split(str(command or ""), posix=True)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = _python(p)
    return argv


def _seconds_text(seconds: float) -> str:
    """41 с / 1 мин 20 с — длительность прогона для журнала и панели."""
    total = int(round(float(seconds or 0)))
    if total >= 60:
        return f"{total // 60} мин {total % 60} с"
    return f"{total} с"


def runs_counter(proof: dict | None) -> str:
    """Счётчик прогона для журнала, уведомления и карточки: `Прогоны: 3/3, 41 с`.

    N — зелёные, M — всего, S — секунды: числа берутся из доказательства, а не
    пересчитываются по файлам, и строка одинакова во всех трёх местах, где её видит
    владелец (журнал, уведомление о закрытии, карточка ревью). Доказательства нет или
    у шага нет пунктов «Прогон» — пустая строка, тогда дословно прежняя четвёртая
    колонка журнала (golden `04-review-approve` это держит).
    """
    if not proof or not int(proof.get("total") or 0):
        return ""
    return (f"Прогоны: {int(proof.get('green') or 0)}/{int(proof['total'])}, "
            f"{_seconds_text(proof.get('seconds') or 0)}")


def _text_of(value) -> str:
    """Вывод субпроцесса строкой: `subprocess` отдаёт байты даже при `text=True` (timeout)."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _run_tail(text: str) -> str:
    """Хвост вывода прогона: 2000 символов конца, замер повреждённых байтов не падает."""
    text = text or ""
    return text[-RUN_TAIL_CHARS:]


def runs_outcome(run: dict, code: int | None, output: str,
                 timed_out: bool = False) -> tuple[str, str]:
    """Исход прогона и короткая причина: `green` | `red` | `timeout`.

    Ровно три вида ожидания из §5 формата плана: `exit`, `содержит`, `regex`. Неизвестный
    вид — не совпало с пояснением: ошибка в плане не должна останавливать шаг как успех.
    Таймаут — исход `timeout` отдельно от `red`: владелец читает, что прогон УБИТ, а не
    «не завершился». Сбой запуска (`code is None`) — красный: исходы `green`/`red`/`timeout`
    исчерпывают форму, `skipped` ставит только бюджет.
    """
    if timed_out:
        return "timeout", "прогон убит по таймауту, результата нет"
    if code is None:
        return "red", "прогон не запустился"
    expect = str(run.get("expect") or "exit")
    value = str(run.get("value") or "")
    if expect == "exit":
        try:
            want = int(value)
        except ValueError:
            return "red", f"ожидание `exit` не число: {value!r}"
        return ("green" if code == want else "red"), f"код {code}"
    if expect == "contains":
        found = value in output
        return (("green" if found else "red"),
                f"подстрока {value!r} найдена" if found
                else f"подстроки {value!r} в выводе нет")
    if expect == "regex":
        try:
            found = re.search(value, output) is not None
        except re.error as exc:
            return "red", f"выражение `{value}` не разбирается: {exc}"
        return (("green" if found else "red"),
                f"выражение `{value}` совпало" if found
                else f"выражение `{value}` не совпало")
    return "red", f"неизвестное ожидание `{expect}`"


# ANCHOR:прогоны_исполнение — стабильная ссылка для спеки и ревью
def run_step_runs(p: Paths, step: dict) -> dict | None:
    """Исполнить пункты «Прогон» шага и записать доказательство текущего sha (S2).

    Первым читается доказательство текущего sha: файл есть — возвращается он как есть и
    НЕ запускается ни одной команды (F03, тест 7). Иначе прогоны идут по порядку списка
    `step["runs"]` в корне проекта: `argv` собирает `runs_argv` (без `shell`, D1), вывод
    читается в UTF-8 с `errors="replace"`, stdout и stderr склеиваются, хвост — 2000
    символов. Исход каждого прогона — строка доказательства: `green`, `red`, `timeout`
    либо `skipped` (бюджет, D5), плюс код возврата, секунды и хвост.

    Бюджет шага (`step_runs.budget`, 600 с по умолчанию) считается по времени,
    потраченному В ЭТОМ вызове: кэш бюджет не ест. Признак `budget.exhausted` — факт
    пропуска прогона (`skipped`), а не «потрачено ≥ лимита»: два зелёных прогона, чья
    сумма перевалила за лимит, ничего не пропустили и обязаны вести в ревью
    (находка 9-S2 F1). Исчерпанный бюджет молча не поднимается — `budget.exhausted`
    в файле читает решение и превращает тик в инцидентный стоп (D5).

    У шага без пунктов «Прогон» — `None`: решению нечего читать, файл не создаётся.
    Команда, которая не запустилась, — красный прогон с `code: null`, а не исключение:
    падение тика оставило бы шаг без доказательства навсегда. Сбой чтения git или
    записи файла — `GateError`: это сбой стенда, и без доказательства шаг не двигается.
    """
    declared = [dict(run) for run in (step or {}).get("runs") or []]
    if not declared:
        return None
    sha = head_commit(p)
    proof = load_runs_proof(p, str(step.get("id") or ""), sha)
    if proof is not None:
        return proof                      # кэш: файл есть — ни одной команды (F03)
    if not sha:
        raise GateError(f"прогоны шага {step.get('id')}: коммита нет — `git rev-parse "
                        f"HEAD` не отвечает, доказательство не записано",
                        plan_id=p.plan_id)
    settings = p.cfg.step_runs
    limit = int(settings["budget"])
    rows: list[dict] = []
    spent = 0.0
    for run in declared:
        command = str(run.get("command") or "")
        if not command:
            continue
        timeout = int(run.get("timeout") or int(settings["timeout"]))
        if spent >= limit:
            # Бюджет исчерпан ДО запуска: исход `skipped`, а не `red` (D5) — хвост не
            # помечен красным, а остаток догоняет следующий тик на том же sha.
            rows.append({"command": command, "expect": str(run.get("expect") or ""),
                         "value": str(run.get("value") or ""), "outcome": "skipped",
                         "code": None, "seconds": 0.0, "tail": ""})
            continue
        argv = runs_argv(p, command)
        started = time.monotonic()
        code: int | None = None
        output = ""
        timed_out = False
        try:
            res = subprocess.run(argv, cwd=str(Path(p.repo)), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout)
            code = int(res.returncode)
            output = (res.stdout or "") + (res.stderr or "")
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output = f"таймаут {timeout} с\n{_run_tail(_text_of(exc.stdout))}" \
                     f"{_text_of(exc.stderr)}"
        except OSError as exc:
            # Команды нет или интерпретатор не запустился: красный прогон с `code: null`
            # и текстом ошибки в хвосте, а не исключение — тик обязан дойти до решения.
            output = f"{type(exc).__name__}: {exc}"
        seconds = round(time.monotonic() - started, 3)
        spent += seconds
        outcome, reason = runs_outcome(run, code, output, timed_out)
        rows.append({"command": command, "expect": str(run.get("expect") or ""),
                     "value": str(run.get("value") or ""), "outcome": outcome,
                     "code": code, "seconds": seconds, "tail": _run_tail(output),
                     # Причина — поле сверх формы §4 п.3: карточка правок обязана назвать
                     # ПОЧЕМУ прогон красный («подстроки нет», «код 1»), а не только код.
                     "reason": reason})
    proof = {"plan": p.plan_id, "step": str(step.get("id") or ""), "sha": sha,
             "total": len(rows),
             "green": sum(1 for row in rows if row.get("outcome") == "green"),
             "seconds": round(sum(float(row.get("seconds") or 0) for row in rows), 3),
             "budget": {"limit": limit, "spent": round(spent, 3),
                        "exhausted": any(row.get("outcome") == "skipped" for row in rows)},
             "runs": rows,
             # id карточки правок, заказанной по этому доказательству (D7): пустая строка —
             # не заказывалась. Без неё ветвь «fix_card заполнен» недостижима, и тик
             # заказывал бы новый круг на том же sha вопреки D6.
             "fix_card": ""}
    save_runs_proof(p, proof)
    return proof



def prompt_fresh(p: Paths, recon: str | None) -> bool:
    """Постановка снята с текущего кода: с `recon` до HEAD нет правок вне `docs/` (D4)."""
    if not recon:
        return False
    try:
        res = _run(["git", "diff", "--quiet", f"{recon}..HEAD", "--", *FRESH_DIFF_ARGS],
                   Path(p.repo), timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _risk_spec_required(p: Paths, step: dict) -> bool:
    """Спека обязательна: у шага с «Риск: high» и режим `risk_gate: spec` (8-S2B).

    Одно условие на все места. Им решает проверка формы постановки
    (`prompt_spec_problems`), и им же помечается строка гейта в карточках переписывания
    постановки: флаг `--risk high` привязан к пометке шага и режиму, а не к тому, что
    замечания C7 уже собраны. Иначе обычный круг правок и карточка пересборки печатали
    команду, чей «PASS 8/8» спеку не спрашивает, — постановщик отдавал постановку без
    блока «Спека», и страж тут же заказывал круг спеки.
    """
    if str((step or {}).get("risk") or "normal").strip().lower() != "high":
        return False
    return str(getattr(p.cfg, "risk_gate", "spec") or "spec").strip().lower() == "spec"


def _prompt_gate_cmd(p: Paths, step: dict, prompt_path: str) -> str:
    """Команда гейта формы постановки для карточки (8-S2B).

    У шага с «Риск: high» и режимом `risk_gate: spec` — с флагом `--risk high`: без него
    C7 смотрит только общую форму, и «PASS 8/8» достижим постановкой без блока «Спека».
    У `normal`-шага и в режиме `hold` флага нет: там он дал бы ложный FAIL C7.
    """
    cmd = f"python .pipeline/check_task_prompt.py {prompt_path}"
    return f"{cmd} --risk high" if _risk_spec_required(p, step) else cmd


def prompt_spec_problems(p: Paths, step: dict) -> list[str]:
    """Пункты спеки постановки high-шага (C7): пусто — спека не требуется или полная.

    Требование предъявляет режим: `risk_gate: hold` спеки не ждёт (шаг держит ручной
    допуск), `spec` — ждёт у каждого шага с «Риск: high». Нет постановки — пусто: её
    ещё никто не писал, и отказ «нет спеки» говорил бы о файле, которого нет; заказ
    постановки делает обычная ветка. Чтение файла — здесь, потому что `decide` получает
    только `ctx` (D4) и файлов не читает.
    """
    if not _risk_spec_required(p, step):
        return []
    rel = prompt_rel(p, step["id"])
    if not rel:
        return []
    try:
        text = (Path(p.repo) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return check_task_prompt.spec_problems(text)


# ───────────────── D6: сверка версии ревью постановки по тексту ─────────────────

def _newlines_unified(text: str) -> str:
    """Переводы строк к LF перед сравнением текстов.

    Файл в рабочем дереве бывает с CRLF (`core.autocrlf`), а blob в git — с LF: это одна
    редакция, и расхождение переводов строк не должно выглядеть правкой постановки.
    """
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")

def _resolve_commit(base: Path | str | None, sha: str | None) -> str:
    """Полный sha коммита по короткой или полной записи; пусто/неизвестно — пустая строка.

    Отметку версии в отчёт ревьюер пишет руками, поэтому она бывает короткой (`d7fa024e`)
    и может быть вовсе не коммитом этого репозитория. Сравнивать записи как строки нельзя —
    принимаем только то, что git подтвердил.
    """
    value = (sha or "").strip().lower()
    if not value or base is None:
        return ""
    try:
        res = _run(["git", "rev-parse", "--verify", "--quiet", f"{value}^{{commit}}"],
                   Path(base), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    full = res.stdout.strip().lower() if res.returncode == 0 else ""
    return full if re.fullmatch(r"[0-9a-f]{40}", full) else ""


def prompt_text_same(p: Paths, prompt: str | None, sha: str | None) -> bool:
    """Текст постановки на коммите `sha` совпадает с её текущим файлом (D6).

    Единственное, что разрешает принять вердикт при расхождении отметок: ревьюер помечает
    отчёт sha HEAD вместо sha постановки (инцидент 09.11.2026, план 29 R1-C — 8.5 часа
    простоя на формальном расхождении). Подмены вердикта здесь нет: переписанная
    постановка даёт на своём коммите ДРУГОЙ текст и по-прежнему не принимается — страж
    заказывает пересборку постановки (`:promptfix-stale:`). Нет файла,
    нет коммита или sha вне репозитория — подтвердить версию нечем, ответ `False`.
    """
    if not prompt:
        return False
    full = _resolve_commit(Path(p.repo), sha)
    if not full:
        return False
    rel = str(prompt)
    try:
        res = _run(["git", "show", f"{full}:{rel}"], Path(p.repo), timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode != 0:
        return False
    try:
        current = (Path(p.repo) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _newlines_unified(res.stdout) == _newlines_unified(current)


def prompt_marker_same_commit(p: Paths, prompt_recon: str | None,
                              report_marker: str | None) -> bool:
    """Обе отметки — один коммит (D6): короткая запись равна полной.

    Ревьюер пишет короткий sha, промптер — полный; это тот же коммит, а не расхождение.
    Пустая или неизвестная запись коммитом не считается: подтверждать нечего.
    """
    base = Path(p.repo)
    left = _resolve_commit(base, prompt_recon)
    right = _resolve_commit(base, report_marker)
    return bool(left) and left == right


def prompt_review_path(p: Paths, step_id: str) -> Path:
    """Отчёт ревью постановки: `docs/reviews/<префикс>-<шаг>-prompt.md`."""
    return Path(p.review_dir) / f"{p.review_prefix}-{step_id}-prompt.md"


def prompt_review_rel(p: Paths, step_id: str) -> str:
    return p.cfg.rel(prompt_review_path(p, step_id))


def prompt_findings_path(p: Paths, step_id: str) -> Path:
    """Реестр находок постановки: `prompt-<номер плана>-<шаг>-findings.md` (план 12, S2).

    Имя берётся из плана буквально (§S2, строка 114) и строится от `review_dir` и
    `review_prefix`, а не через `with_name(stem + "-findings.md")`: тот дал бы
    `12-S2-prompt-findings.md` и разошёлся бы с форматом плана.
    """
    return Path(p.review_dir) / f"prompt-{p.review_prefix}-{step_id}-findings.md"


def prompt_findings_rel(p: Paths, step_id: str) -> str:
    """Путь реестра постановки для текстов карточек: относительный, не абсолютный."""
    return p.cfg.rel(prompt_findings_path(p, step_id))


# ───────────────────── реестр находок шага (план 13, S2) ─────────────────────
# Реестр шага — память круга правок: одна строка на находку ревью КОДА, живущая от первого
# `REQUEST_CHANGES` до закрытия шага. Шапка и ключи — псевдонимы реестра постановки (план
# требует «того же формата»), а словарь классов свой: у реестров плана и постановки `kind`
# — класс-маршрут (`замысел`, `стиль`), у реестра шага — класс находки. Общий словарь
# (`FINDING_KINDS`) не расширяется: иначе реестр плана начал бы принимать классы находок.
STEP_FINDINGS_HEADER = PROMPT_FINDINGS_HEADER
STEP_FINDINGS_KEYS = PROMPT_FINDINGS_KEYS
STEP_FINDING_KINDS = ("блокирующая", "значимая", "стилистическая")
# Причина круга правок (13-S5, §4.2): старший класс находки, по которой круг открыт.
# Порядок `STEP_FINDING_KINDS` и есть старшинство — блокирующая сильнее значимой.
# Нет ни одного класса — пустая строка; метрика считает такой круг «неизвестно»
# (`stats.ROUND_CAUSES`), а не догадывается о нём.
FIX_CAUSE_ORDER = STEP_FINDING_KINDS
#: Класс находки → ключ метрики (`stats.ROUND_CAUSES`): имя базы не зависит от языка отчёта.
ROUND_CAUSE_KEYS = {"блокирующая": "blocking", "значимая": "significant",
                    "стилистическая": "style"}
# Статус строки — из класса находки: держит шаг только блокирующая.
STEP_FINDING_STATUS = {"блокирующая": "открыто", "значимая": "не блокирует",
                       "стилистическая": "не блокирует"}
STEP_FINDING_ACTION = ("Закрыть находку правкой и ответить фактом "
                       "(адрес-символ или sha коммита)")


def step_findings_path(p: Paths, step_id: str) -> Path:
    """Реестр находок шага: `step-<номер плана>-<шаг>-findings.md` (план 13, S2).

    Собирается от `review_dir` и `review_prefix`, как реестр постановки: `with_name` от
    отчёта ревью дал бы `13-S2-findings.md` и разошёлся бы с форматом плана.
    """
    return Path(p.review_dir) / f"step-{p.review_prefix}-{step_id}-findings.md"


def step_findings_rel(p: Paths, step_id: str) -> str:
    """Путь реестра шага для текстов карточек: относительный, не абсолютный."""
    return p.cfg.rel(step_findings_path(p, step_id))


def plan_review_path(p: Paths) -> Path:
    """Отчёт ревью плана: `docs/reviews/plan-<N>.md` (D4) — один файл на план.

    Путь собирает `approval.review_path`: тот же файл читает привязка вердикта к версии
    плана (S6), и второй сборщик пути разошёлся бы с ним молча.
    """
    return approval.review_path(p.cfg, p.plan_id)


def plan_review_rel(p: Paths) -> str:
    return p.cfg.rel(plan_review_path(p))


def plan_findings_path(p: Paths) -> Path:
    """Реестр находок плана: рядом с отчётом ревью, `plan-N-findings.md`.

    Путь выводится из `plan_review_path`, а не собирается заново: тот привязан к
    `approval.review_path` и `cfg.reviews_dir`, и второй сборщик молча разошёлся бы с ним.
    """
    report = plan_review_path(p)
    return report.with_name(f"{report.stem}-findings.md")


def plan_findings_rel(p: Paths) -> str:
    """Путь реестра для текстов: карточки и отчёты печатают его, а не абсолютный."""
    return p.cfg.rel(plan_findings_path(p))


_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


def _finding_cells(line: str) -> list[str]:
    """Ячейки строки таблицы: внешние `|` ячейками не считаются, `\\|` — литерал.

    Ревьюер пишет реестр руками и экранирует `|` внутри команды (`ls docs/reports \\| grep`),
    как учит markdown. Пока разбор резал строку по любому `|`, такой реестр считался битым
    («ячеек 6, нужно 5») и шаг не закрывался по неизвестному реестру — случай 19.11.2026,
    9-S1: APPROVE есть, коммит есть, закрытие стоит на разборе таблицы.
    """
    body = str(line).strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.replace("\\|", "|").strip() for cell in _CELL_SPLIT_RE.split(body)]


def _findings_title(path: Path, kind: str = "плана", prefix: str = "plan-") -> str:
    """Первая строка реестра: номер из имени `<prefix><номер>-findings.md`, иначе имя файла.

    Обобщено на реестр постановки (план 12, S2): у него префикс `prompt-`, а номер в имени
    — «12-S2». Второй сборщик заголовка разошёлся бы с первым так же, как разошёлся бы
    второй сборщик шапки таблицы.
    """
    stem = Path(path).stem
    suffix = "-findings"
    if stem.startswith(prefix) and stem.endswith(suffix):
        return f"# Реестр находок {kind} {stem[len(prefix):-len(suffix)]}"
    return f"# Реестр находок {kind} {stem}"


def findings_table(findings: list[dict], header: tuple = FINDINGS_HEADER,
                   keys: tuple = FINDINGS_KEYS) -> list[str]:
    """Строки markdown-таблицы реестра: шапка, разделитель, находки в переданном порядке.

    Один построитель для записи файла и для тела карточки: второй шаблон шапки разошёлся бы
    с первым, и `load_findings` перестал бы находить таблицу. Шапка и ключи — параметры
    (план 12, S2): у реестра постановки нет колонки «Шаг», и копия цикла разъехалась бы
    с этим построителем.
    """
    rows = ["| " + " | ".join(header) + " |",
            "|" + "|".join("---" for _ in header) + "|"]
    for item in findings:
        cells = [item.get(key) or "" for key in keys]
        rows.append("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ").strip()
                                      for c in cells) + " |")
    return rows


def read_findings_text(path) -> str:
    """Прочитать текст реестра терпимо. Файла нет — `""`; ошибка чтения — `GateError`.

    Чтение отделено от разбора (план 12, S2, ревью): тик берёт текст один раз и считает по
    нему и `exists`, и строки. Два чтения одного файла давали окно, в котором реестр,
    переписанный ревьюером между ними, выглядел как «файл есть, открытых нет» — то есть
    APPROVE по неизвестному реестру.
    """
    path = Path(path)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        # ValueError — это UnicodeDecodeError: реестр, записанный не в UTF-8 (cp1251 — штатная
        # кодировка консоли на Windows), обязан дать GateError с путём, а не сырое исключение.
        raise GateError(f"реестр находок не читается: {path}: {exc}") from exc


def load_findings(path, header: tuple = FINDINGS_HEADER,
                  keys: tuple = FINDINGS_KEYS, **rules) -> list[dict]:
    """Прочитать реестр находок. Файла нет — `[]` (норма, как у `load_state`).

    Битый файл — `GateError`, а не пустой список: молча вернуть `[]` значило бы «открытых
    находок нет» и открыть дорогу APPROVE по неизвестному реестру.

    Единственное правило блокирования, общее для S2 и S3: блокирует только статус — находка
    со статусом `открыто` блокирует вердикт, `закрыто` и `не блокирует` — нет. Класс ничего
    не блокирует: он выбирает маршрут находки (S3).

    `**rules` уходит в `parse_findings`: файл вопросов (план 8, S5) разбирается этим же
    разбором с другой шапкой, другими словарями и своим видом идентификатора.
    """
    path = Path(path)
    return parse_findings(read_findings_text(path), path, header, keys, **rules)


def _id_hint(id_prefix: str, id_sep: str) -> str:
    """Подсказка вида идентификатора для сообщений об ошибке (план 8, S5, §4.2).

    Собирается из тех же `id_prefix`/`id_sep`, которыми функция присваивает ID: у вопросов
    («Q» + «-») это `Q-N`, у находок («F» + «») — `F<число>`: писать `FN` было бы неправдой,
    настоящие ID находок — `F1`, `F2`.
    """
    return f"{id_prefix}{id_sep}N" if id_sep else f"{id_prefix}<число>"


def _status_of(raw, statuses: tuple) -> str | None:
    """Статус строки реестра: слово словаря, даже если автор дописал пояснение.

    Гейт решает по слову, а не по форме записи: `закрыто (круг 1: …)`, `Закрыто: сделано`,
    `не блокирует — принято` читаются как `закрыто`/`не блокирует` (случай 18.11.2026:
    реестр постановки S3 остановил план инцидентом из-за пояснения в ячейке статуса).
    Слово берётся только целым — после него обязана идти граница: пробел, скобка,
    двоеточие, тире, знак препинания или конец строки; `закрытое` статусом не становится,
    иначе опечатка в слове открыла бы шаг. Регистр не важен: реестр шага пишет `Закрыто:`.
    """
    text = " ".join(str(raw or "").split())
    folded = text.casefold()
    for status in sorted(statuses, key=len, reverse=True):
        word = str(status).casefold()
        if not folded.startswith(word):
            continue
        rest = folded[len(word):]
        if not rest or not (rest[0].isalnum() or rest[0] == "_"):
            return str(status)
    return None


def parse_findings(text: str, path, header: tuple = FINDINGS_HEADER,
                   keys: tuple = FINDINGS_KEYS, *,
                   id_re: re.Pattern = FINDING_ID_RE, id_prefix: str = "F", id_sep: str = "",
                   statuses: tuple = FINDING_STATUSES, kinds: tuple | None = FINDING_KINDS,
                   what: str = "реестр находок",
                   what_at: str = "реестре находок") -> list[dict]:
    """Разобрать текст реестра; `path` — только для сообщений об ошибке.

    Правила те же, что были у чтения: пустой текст — `[]`, нет шапки — `GateError`.

    Шапка, ключи, вид идентификатора и словари — параметры (план 8, S5): файл вопросов
    разбирается ЭТИМ разбором, а не вторым, иначе два разбора одной таблицы разошлись бы
    молча. `kinds=None` — таблица без классов (у вопросов класса нет). Проверка идёт только
    по тем колонкам, которые есть в `keys`: у таблицы без колонки «Статус» проверять нечего.

    Идентификатор собирается из `id_prefix` и `id_sep` («F» + «» и «Q» + «-»), подсказка
    в ошибке — из них же: `Q-N` у вопросов, `F<число>` у находок.
    """
    path = Path(path)
    id_hint = _id_hint(id_prefix, id_sep)
    lines = str(text).splitlines()
    start = None
    for index, line in enumerate(lines):
        if tuple(_finding_cells(line)) == tuple(header):
            start = index + 1
            break
    if start is None:
        if not text.strip():
            return []
        raise GateError(f"в {what_at} нет шапки таблицы "
                        f"`| {' | '.join(header)} |`: {path}")
    found: list[dict] = []
    seen: set[str] = set()
    for number, line in enumerate(lines[start:], start=start + 1):
        if not line.strip().startswith("|"):
            continue
        cells = _finding_cells(line)
        if all(cell and set(cell) <= set("-: ") for cell in cells):
            continue                                   # строка-разделитель
        if len(cells) != len(header):
            raise GateError(f"{what}, строка {number}: ячеек {len(cells)}, "
                            f"нужно {len(header)}: {path}")
        item = dict(zip(keys, cells))
        if "id" in keys:
            if not id_re.fullmatch(item["id"]):
                raise GateError(f"{what}, строка {number}: идентификатор не вида {id_hint}: "
                                f"{item['id']!r}: {path}")
            if item["id"] in seen:
                raise GateError(f"{what}: идентификатор {item['id']} повторяется: {path}")
            seen.add(item["id"])
        if kinds and "kind" in item and item["kind"] not in kinds:
            raise GateError(f"{what}: класс {item['kind']!r} вне словаря "
                            f"({' | '.join(kinds)}): {path}")
        if "status" in keys:
            status = _status_of(item["status"], statuses)
            if status is None:
                raise GateError(f"{what}: статус {item['status']!r} вне словаря "
                                f"({' | '.join(statuses)}): {path}")
            item["status"] = status
        found.append(item)
    return found


def _findings_preamble(path: Path, header: tuple) -> list[str]:
    """Строки файла до шапки таблицы — то, что запись обязана сохранить (13-S1, §4.3).

    Реестр плана несёт привязку к версии плана (`plan_sha256:` первой строкой). Пока
    `write_findings` писала «заголовок + таблица» с нуля, первая же строка, дописанная
    стражем при закрытии шага, стирала эту привязку, и реестр молча переставал
    соответствовать плану. Файла нет или шапки в нём нет — преамбулы нет.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if tuple(_finding_cells(line)) == tuple(header):
            preamble = lines[:index]
            if preamble and preamble[-1].strip():
                preamble = preamble + [""]
            return preamble
    return []


def write_findings(path, findings: list[dict], header: tuple = FINDINGS_HEADER,
                   keys: tuple = FINDINGS_KEYS, title_kind: str = "плана",
                   title_prefix: str = "plan-", *, title: str = "",
                   id_re: re.Pattern = FINDING_ID_RE, id_prefix: str = "F", id_sep: str = "",
                   statuses: tuple = FINDING_STATUSES,
                   kinds: tuple | None = FINDING_KINDS,
                   what: str = "реестр находок") -> Path:
    """Записать реестр целиком: порядок находок сохраняется, дубль ID — ошибка.

    Запись атомарная (`state_io.atomic_write_text`): kill посреди записи оставляет прежний
    реестр целым, а не обрезанным на середине таблицы.

    `title` заменяет вычисленный заголовок: у файла вопросов он свой, а сборщик заголовка
    («Реестр находок …») в нём читался бы как второй вид файла.

    Проверка идентификатора идёт только при колонке `id` в `keys`, статус — только при
    колонке `status` (план 8, S5, §4.2): таблица без такой колонки не обязана их иметь.
    """
    path = Path(path)
    id_hint = _id_hint(id_prefix, id_sep)
    rows = [dict(item) for item in (findings or [])]
    if "id" in keys:
        ids = [str(item.get("id") or "") for item in rows]
        repeat = sorted({item for item in ids if ids.count(item) > 1})
        if repeat:
            raise GateError(f"{what}: идентификаторы повторяются: {', '.join(repeat)}: {path}")
        for item_id in ids:
            if not id_re.fullmatch(item_id):
                raise GateError(f"{what}: идентификатор не вида {id_hint}: {item_id!r}: {path}")
    for item in rows:
        if kinds and "kind" in item and item.get("kind") not in kinds:
            raise GateError(f"{what}: класс {item.get('kind')!r} вне словаря "
                            f"({' | '.join(kinds)}): {path}")
        if "status" in keys:
            status = _status_of(item.get("status"), statuses)
            if status is None:
                raise GateError(f"{what}: статус {item.get('status')!r} вне словаря "
                                f"({' | '.join(statuses)}): {path}")
            item["status"] = status
    heading = title or _findings_title(path, title_kind, title_prefix)
    preamble = _findings_preamble(path, header) or [heading, ""]
    state_io.atomic_write_text(path, "\n".join(preamble + findings_table(rows, header, keys))
                               + "\n")
    return path


def add_findings(path, items: list[dict], header: tuple = FINDINGS_HEADER,
                 keys: tuple = FINDINGS_KEYS, title_kind: str = "плана",
                 title_prefix: str = "plan-", *, title: str = "",
                 id_re: re.Pattern = FINDING_ID_RE, id_prefix: str = "F", id_sep: str = "",
                 statuses: tuple = FINDING_STATUSES, kinds: tuple | None = FINDING_KINDS,
                 what: str = "реестр находок", what_at: str = "") -> list[dict]:
    """Добавить строки реестра: ID присваивает функция — следующий свободный номер таблицы.

    У находок это `F<N>`, у вопросов шага (`id_prefix="Q"`, `id_sep="-"`) — `Q-N`; вид ID
    ведёт тот же параметр, что у разбора, а не автор отчёта. Нумерация в отчётах сквозная
    только внутри отчёта: автор каждой постановки пишет `Q1`, поэтому его номер — не ID
    реестра (план 8, S5, ревью шага: вопрос второго шага терялся).

    `status` по умолчанию — первый статус словаря (`открыто` у находок, `открыт` у
    вопросов). Поле `id` во входных словарях игнорируется: иначе после ручной правки файла
    в реестре появятся дубли, а ведущий реестра — не тот, кто считает.
    """
    path = Path(path)
    rules = {"id_re": id_re, "id_prefix": id_prefix, "id_sep": id_sep,
             "statuses": statuses, "kinds": kinds, "what": what}
    if what_at:
        rules["what_at"] = what_at
    current = load_findings(path, header, keys, **rules)
    numbers = [int(id_re.fullmatch(item["id"]).group(1)) for item in current if item.get("id")]
    next_number = max(numbers, default=0) + 1
    default_status = str(statuses[0]) if statuses else "открыто"
    added: list[dict] = []
    for raw in items or []:
        item = {"id": f"{id_prefix}{id_sep}{next_number}"}
        for key in keys:
            if key != "id":
                item[key] = str(raw.get(key) or "").strip()
        item["status"] = str(raw.get("status") or default_status).strip()
        added.append(item)
        next_number += 1
    result = current + added
    write_findings(path, result, header, keys, title_kind, title_prefix, title=title,
                   id_re=id_re, id_prefix=id_prefix, id_sep=id_sep, statuses=statuses,
                   kinds=kinds, what=what)
    return result


def _finding_norm(text, limit: int = 120) -> str:
    """Нормализованный текст находки — ключ сверки строк между кругами (13-S2, §4.4).

    Отчёт круга 2 часто цитирует круг 1 дословно: без сверки по нормализованному тексту
    каждая цитата давала бы вторую строку реестра, и реестр рос бы вдвое за круг.
    """
    raw = str(text or "").replace("`", "").replace("*", "").replace("__", "")
    return " ".join(raw.split()).lower()[:limit]


def step_finding_rows(text) -> list[dict]:
    """Находки отчёта ревью строками реестра шага: только пункты с непустым классом.

    Разбор отчёта — существующий `scan_report_findings` (D2): второй разбор метки класса
    разошёлся бы с первым на живых отчётах. Статус строки — из класса находки, текст —
    поле `body` (без метки), подметка (`[блокирующая/тест-недетерминирован]`) остаётся
    в тексте в скобках: терять её нельзя, она называет вид блокирующей находки.
    """
    _section, items = scan_report_findings(text)
    rows: list[dict] = []
    for item in items:
        kind = str(item.get("class") or "")
        if kind not in STEP_FINDING_KINDS:
            continue                       # пункт без класса реестра не заводит (D3)
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        sublabel = str(item.get("sublabel") or "")
        if sublabel:
            body = f"{body} (подметка: {sublabel})"
        rows.append({"kind": kind, "text": body, "action": STEP_FINDING_ACTION,
                     "status": STEP_FINDING_STATUS[kind]})
    return rows


def ensure_step_findings(p: Paths, step: dict, report_rel: str) -> list[dict]:
    """Завести и пополнить реестр находок шага из отчёта ревью (13-S2, §4.4).

    Возвращает ВСЕ строки реестра после записи: по ним строится сообщение тика. Правила:

    - первым проходом страж заводит файл из отчёта (у ревьюера файла ещё нет), дальше файл
      ведёт ревьюер, а страж только дописывает то, чего в реестре нет;
    - отчёт без единого пункта с классом файла не создаёт: пустой реестр на отчёте без
      раздела «Находки» читался бы как «находок нет» и стирал бы память круга;
    - строка, текст которой уже есть в реестре, не добавляется: цитата прошлого круга —
      не новая находка;
    - статусы существующих строк страж не переписывает: их ставит ревьюер фактом.

    Ошибки чтения и разбора реестра наружу не глотаются (`GateError`, `OSError`,
    `ValueError`): вызывающая ветка ставит по ним инцидентный стоп. Нечитаемый отчёт
    ошибкой не считается — он не даёт новых строк, а решение по реестру от него не зависит.
    """
    path = step_findings_path(p, step["id"])
    current = load_findings(path, STEP_FINDINGS_HEADER, STEP_FINDINGS_KEYS,
                            kinds=STEP_FINDING_KINDS)
    try:
        text = (Path(p.repo) / report_rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return current
    fresh: list[dict] = []
    known = {_finding_norm(item.get("text")) for item in current}
    for row in step_finding_rows(text):
        key = _finding_norm(row["text"])
        if key in known:
            continue
        known.add(key)
        fresh.append(row)
    if not fresh:
        return current
    return add_findings(path, fresh, STEP_FINDINGS_HEADER, STEP_FINDINGS_KEYS, "шага", "step-",
                        kinds=STEP_FINDING_KINDS)


# ───────────────────── вопросы шага (план 8, S5) ─────────────────────
# Вопрос шага — повод ожидания, который читается из файла, а не из `hold_steps` (допущение 6):
# ручной допуск владелец ставит жестом, а вопрос задаёт автор отчёта (постановка, ревью,
# исполнение). Ответ закрывает вопрос, и повод исчезает; `hold_released` не пополняется.

def plan_questions_path(p: Paths) -> Path:
    """Файл вопросов шага: рядом с отчётом ревью плана, `plan-N-questions.md` (допущение 4)."""
    report = plan_review_path(p)
    return report.with_name(f"{report.stem}-questions.md")


def plan_questions_rel(p: Paths) -> str:
    """Путь файла вопросов для текстов карточек и уведомлений: относительный, не полный."""
    return p.cfg.rel(plan_questions_path(p))


def _question_rules() -> dict:
    """Правила разбора файла вопросов: тот же разбор, что у реестра находок (S5, п.1).

    Второго парсера таблицы нет: шапка, словарь статусов и вид идентификатора — параметры
    `parse_findings`/`add_findings`. ID собирается из `id_prefix="Q"` и `id_sep="-"` —
    теми же параметрами, что у находок («F» + «»); класса у вопроса нет, поэтому `kinds=None`.
    """
    return {"id_re": QUESTION_ID_RE, "id_prefix": "Q", "id_sep": "-",
            "statuses": QUESTION_STATUSES,
            "kinds": None, "what": "файл вопросов", "what_at": "файле вопросов"}


def load_questions(path) -> list[dict]:
    """Прочитать файл вопросов. Файла нет — `[]` (норма); битый — `GateError`."""
    return load_findings(path, QUESTIONS_HEADER, QUESTIONS_KEYS, **_question_rules())


def write_questions(path, rows: list[dict], plan_id: str = "") -> Path:
    """Записать файл вопросов целиком: шапка нормативна, время вопроса — в состоянии плана."""
    rules = _question_rules()
    rules.pop("what_at", None)
    number = str(plan_id or "").strip()
    title = f"# Вопросы плана {number}" if number else "# Вопросы шага"
    return write_findings(path, rows, QUESTIONS_HEADER, QUESTIONS_KEYS, title=title, **rules)


def question_id_of(value) -> str:
    """Идентификатор вопроса из записи автора или команды: «Q1», «q-2» → `Q-1`, `Q-2`."""
    text = str(value or "").strip().upper().replace(" ", "")
    match = QUESTION_ID_RE.fullmatch(text) or re.fullmatch(r"Q(\d+)", text)
    return f"Q-{int(match.group(1))}" if match else ""


def question_options(options) -> list[dict]:
    """Варианты вопроса списком: `[{"letter": "A", "text": "…"}]` — для кнопок панели.

    Оформление автора («A) … / B) …») разбирается здесь, а не в разметке: у панели и у
    текста уведомления один источник вариантов. Вариантов может не быть вовсе — тогда и
    кнопок нет, ответ приходит свободным текстом.
    """
    text = str(options or "").strip()
    if not text:
        return []
    marks = list(QUESTION_OPTION_RE.finditer(text))
    if not marks:
        return [{"letter": "", "text": part.strip()}
                for part in re.split(r"\s*/\s*", text) if part.strip()]
    rows: list[dict] = []
    for pos, mark in enumerate(marks):
        end = marks[pos + 1].start() if pos + 1 < len(marks) else len(text)
        chunk = text[mark.end():end].strip(" /;,").strip("—–-").strip()
        rows.append({"letter": mark.group(1).upper(), "text": chunk})
    return rows


def parse_question_line(line: str) -> dict | None:
    """Строка вопроса из отчёта карточки (S5, п.2); `None` — строка вопросом не является.

    Форма: `Q<N>: <вопрос> — A) <вариант> (цена) / B) <вариант> (цена) — рекомендация <буква>`.
    Разбор терпим к автору: вариантов может не быть (вопрос со свободным ответом), а
    «рекомендации нет» даёт пустую рекомендацию — автовыбора по такому вопросу не будет.
    """
    match = QUESTION_LINE_RE.match(str(line or ""))
    if not match:
        return None
    # Переносы склеиваются в один пробел: абзац автора — один вопрос, и его ширина по
    # странице не должна попадать ни в текст, ни в варианты.
    body = " ".join(match.group("body").replace("`", "").split())
    head, recommend = body, ""
    mark = re.search(r"рекомендаци\w*", body, re.IGNORECASE)
    if mark:
        tail = body[mark.start():]
        head = body[:mark.start()].strip().strip("—–-").strip()
        if not QUESTION_NO_RECOMMEND_RE.search(tail):
            letter = QUESTION_RECOMMEND_RE.search(tail)
            recommend = letter.group(1).upper() if letter else ""
    # Варианты начинаются с метки `A)`/`А)` — текст вопроса это всё до неё. Деление по
    # первому тире резало вопрос, внутри которого автор поставил тире («… — кто первый?»),
    # и уносило половину вопроса в колонку вариантов.
    option = QUESTION_OPTION_RE.search(head)
    if option:
        text = head[:option.start()].strip().strip("—–-/:;,.").strip()
        options = head[option.start():].strip()
    else:
        parts = [part.strip() for part in QUESTION_DASH_RE.split(head) if part.strip()]
        text = (parts[0] if parts else head).strip()
        options = " — ".join(parts[1:]) if len(parts) > 1 else ""
    if not text:
        return None
    return {"id": f"Q-{int(match.group(1))}", "text": text, "options": options,
            "recommend": recommend}


def question_blocks(text) -> list[list[str]]:
    """Блоки строк отчёта, каждый начинается строкой `Q<N>:` (S5, п.2).

    Автор переносит абзац по ширине страницы, поэтому вопрос — блок до пустой строки,
    заголовка, строки таблицы или следующего `Q<N>:`. Обрыв на первом переносе терял хвост
    абзаца вместе с вариантами и рекомендацией (живой случай: `13-S2-prompt.md`).
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            current = None                    # пустая строка закрывает абзац
            continue
        if line.startswith("#") or line.startswith("|"):
            current = None                    # заголовок и таблица вопрос не продолжают
            continue
        if QUESTION_LINE_START_RE.match(line):
            current = [line]
            blocks.append(current)
            continue
        if current is not None:
            current.append(line)              # перенос строки внутри абзаца автора
    return blocks


def parse_question_lines(text) -> list[dict]:
    """Все вопросы отчёта: по записи на блок `Q<N>:` (S5, п.2).

    Вопрос — абзац, а не строка. Второго разбора нет: блок склеивается в одну строку и
    уходит в тот же `parse_question_line`.
    """
    found: list[dict] = []
    for block in question_blocks(text):
        parsed = parse_question_line(" ".join(block))
        if parsed:
            found.append(parsed)
    return found


def parse_question_blocks(text) -> list[tuple[list[str], dict]]:
    """Пары «сырой абзац вопроса — разобранная запись» (S2B плана 10).

    Разбор тот же, что у `parse_question_lines` (второго парсера нет), но абзац остаётся
    рядом с записью: команда замера ищется в сыром тексте, а `parse_question_line` кавычки
    снимает — по разобранному тексту замер не найти.
    """
    found: list[tuple[list[str], dict]] = []
    for block in question_blocks(text):
        parsed = parse_question_line(" ".join(block))
        if parsed:
            found.append((block, parsed))
    return found


def question_measure_argv(block) -> list[str] | None:
    """`argv` команды замера из абзаца вопроса; `None` — вопрос не измерим (S2B плана 10).

    Правило постановки: команда — первый фрагмент в обратных кавычках абзаца, первый токен
    которого в белом списке (`sha256sum`, `shasum`, `grep`, `git rev-parse`). Форму выбирает
    белый список, а не текст находки: пайп, редирект, `sh -c` и `python -c` не исполняются —
    такой вопрос заводится как обычный. «Версия из манифеста» отдельного способа не имеет:
    это `grep` по `plugin.yaml`/`version.txt`.
    """
    text = " ".join(block or [])
    for span in QUESTION_COMMAND_RE.findall(text):
        try:
            argv = shlex.split(span, posix=True)
        except ValueError:            # незакрытая кавычка внутри фрагмента
            continue
        if not argv or argv[0] not in QUESTION_MEASURE_PROGRAMS:
            continue
        if any(char in token for token in argv for char in QUESTION_SHELL_CHARS):
            return None                # оболочка не участвует: команда не измерима
        if argv[0] == "git" and (len(argv) < 3
                                or argv[1] != QUESTION_MEASURE_GIT_SUBCOMMAND):
            return None
        return argv
    return None


def measure_question(p: Paths, block) -> dict | None:
    """Замер вопроса: `{"command", "ok", "detail"}`; `None` — команды замера нет (S2B).

    Исполнение — `_run` (argv без shell, `shlex.split`, `cwd` проекта, таймаут 60 с), одна
    точка подмены в тестах. Замер удался, если код возврата 0 и в объединённом выводе есть
    непустой текст: чтение без ответа — не ответ. Строка истории нужна и при неудаче:
    владелец видит, что команду страж пробовал и почему ответа нет.
    """
    argv = question_measure_argv(block)
    if not argv:
        return None
    command = " ".join(argv)
    try:
        done = _run(argv, cwd=Path(p.cfg.repo), timeout=QUESTION_MEASURE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"command": command, "ok": False,
                "detail": f"замер не удался: «{command}» — таймаут {QUESTION_MEASURE_TIMEOUT} с"}
    except OSError:
        return {"command": command, "ok": False,
                "detail": f"замер не удался: «{command}» — не запустилась"}
    output = " ".join(f"{_text_of(done.stdout)} {_text_of(done.stderr)}".split())
    if int(done.returncode or 0) != 0:
        return {"command": command, "ok": False,
                "detail": f"замер не удался: «{command}» — код {done.returncode}"}
    if not output:
        return {"command": command, "ok": False,
                "detail": f"замер не удался: «{command}» — пустой вывод"}
    detail = f"замерил: «{command}» → «{output}»"
    return {"command": command, "ok": True, "detail": detail[:QUESTION_MEASURE_DETAIL]}


def questions_enforce(p: Paths) -> bool:
    """`questions.enforce` из живого конфига проекта: умолчание `true` (S2B плана 10).

    Ключ читается в движке (`p.cfg.state` — сырой конфиг, слитый с умолчаниями), а не в
    `config.py`: `config` — файл закрытого шага S2. Значение не `bool` — умолчание `true`:
    опечатка владельца не должна молча превращать правило в замер (это факт отчёта, а не
    WARNING в stdout конвейера).
    """
    raw = getattr(p.cfg, "state", None) or {}
    section = (raw.get("questions") or {}) if isinstance(raw, dict) else {}
    value = section.get("enforce") if isinstance(section, dict) else None
    return value if isinstance(value, bool) else True


def question_measure_key(step_id: str, text: str) -> str:
    """Ключ отметки замера: шаг и текст вопроса без лишних пробелов."""
    return f"{str(step_id or '').strip().upper()}|{' '.join(str(text or '').split())}"


def question_measure(p: Paths, step_id: str, text: str, block, state: dict) -> dict | None:
    """Замер вопроса с отметкой в состоянии плана: один замер на пару (шаг, текст).

    Отметка живёт в том же словаре `state`, который `register_questions` сохраняет
    `save_state`: второй `load_state`/`save_state` затёр бы снимок тика. На пути
    `questions.enforce: true` строки вопроса не заводится и общего сохранения не происходит,
    поэтому отметку сохраняет сам замер — иначе следующий тик исполнил бы команду снова.
    """
    if not isinstance(state, dict):
        return None
    key = question_measure_key(step_id, text)
    marks = state.get(QUESTION_MEASURE_STATE_KEY)
    marks = dict(marks) if isinstance(marks, dict) else {}
    if key in marks:
        mark = marks[key]
        return {"command": str(mark.get("command") or ""), "ok": bool(mark.get("ok")),
                "detail": str(mark.get("detail") or "")}
    measured = measure_question(p, block)
    if measured is None:
        return None
    marks[key] = dict(measured, at=time.strftime("%d.%m.%Y %H:%M:%S"))
    state[QUESTION_MEASURE_STATE_KEY] = marks
    append_history(p.cfg, {"ts": time.time(), "at": marks[key]["at"], "plan": p.plan_id,
                           "step": step_id, "action": "measure", "duration_s": 0.0,
                           "detail": measured["detail"], "commit": "", "stale": False,
                           "cause": ""})
    save_state(p, state)
    return measured


def step_questions(path, step_id: str = "") -> list[dict]:
    """Открытые вопросы шага (или все открытые, если шаг не задан). Порядок — как в файле."""
    want = str(step_id or "").strip().upper()
    return [item for item in load_questions(path)
            if item.get("status") == QUESTION_OPEN
            and (not want or str(item.get("step") or "").strip().upper() == want)]


def question_view(item: dict, asked_at=None, timeout: float = 0) -> dict:
    """Вопрос для панели: ID, шаг, текст, варианты с ценой, рекомендация, момент и дедлайн.

    Дедлайн — момент автовыбора, поэтому он есть только там, где автовыбор возможен:
    рекомендация названа автором и таймаут не выключен. Иначе `deadline_at` пуст, и панель
    не обещает владельцу «осталось N мин» у вопроса, который будет ждать человека.
    """
    asked = asked_at if isinstance(asked_at, (int, float)) else None
    recommend = str(item.get("recommend") or "").strip()
    deadline = (float(asked) + timeout) if (asked is not None and timeout > 0 and recommend) else None
    return {"id": item.get("id") or "", "step": item.get("step") or "",
            "text": item.get("text") or "",
            "options": question_options(item.get("options")),
            "options_text": item.get("options") or "",
            "recommend": recommend,
            "status": item.get("status") or "",
            "asked_at": _local_stamp(float(asked)) if asked is not None else "",
            # Нет рекомендации или таймаут выключен — ключ `null`, как в §4.8: панель читает
            # его как `row.deadline_at || ""`, и «пусто» должно быть одним значением, а не двумя.
            "deadline_at": _local_stamp(deadline) if deadline else None}


def questions_view(ctx: dict) -> list[dict]:
    """Открытые вопросы плана для панели: все шаги, а не только текущий.

    Вопрос другого шага владельцу нужен там же, где он смотрит шаг (вкладка «Шаг»), поэтому
    отбор по шагу здесь не делается: номер шага — поле строки, фильтрует страница.
    Момент и дедлайн берутся из состояния плана.
    """
    opened = ctx.get("plan_questions")
    if opened is None:
        opened = ctx.get("step_questions") or []
    if not opened:
        return []
    state = ctx.get("state") if isinstance(ctx.get("state"), dict) else {}
    stamps = state.get("questions") if isinstance(state.get("questions"), dict) else {}
    timeout = float(ctx.get("answer_timeout") or 0)
    rows: list[dict] = []
    for item in opened:
        asked = (stamps.get(item["id"]) or {}).get("asked_at")
        rows.append(question_view(item, asked, timeout))
    return rows


def _question_minutes(seconds: float) -> int:
    """Минуты для пометки «авто»: не меньше одной, иначе «0 мин» читалось бы как «сразу»."""
    return max(1, int(round(float(seconds) / 60)))


def question_notice_text(p: Paths, item: dict, timeout: float = 0) -> str:
    """Уведомление о вопросе для человека: текст, варианты с ценой, рекомендация, ответ.

    Однострочное «варианты: A) … / B) …» в телефоне не читается, поэтому варианты идут
    списком, а строка ответа — последней: её и копируют в чат. Дедлайн называется только
    там, где автовыбор возможен (рекомендация есть и таймаут не выключен).
    """
    lines = [f"❓ {item.get('id')} — нужно ваше решение",
             f"план {p.plan_id}, шаг {item.get('step') or '—'}",
             "",
             " ".join(str(item.get("text") or "").split())]
    options = question_options(item.get("options"))
    if options:
        lines.append("")
        lines.extend(f"{opt['letter']}) {opt['text']}" for opt in options)
    recommend = str(item.get("recommend") or "").strip()
    lines.append("")
    if recommend:
        lines.append(f"Рекомендую {recommend}.")
        if timeout and timeout > 0:
            lines.append(f"Если не ответить за {_question_minutes(timeout)} мин, "
                         f"приму {recommend} сам.")
    else:
        lines.append("Рекомендации нет — жду вашего ответа.")
    lines.append(f"Ответить: /answer {item.get('id')} {recommend or '<вариант>'} "
                 f"или кнопкой в панели.")
    return "\n".join(lines)


def question_hold_reason(step_id: str, item: dict, tail: str = "") -> str:
    """Причина ожидания по вопросу: номер, текст, варианты, рекомендация, способ ответить."""
    recommend = str(item.get("recommend") or "").strip()
    parts = [f"шаг {step_id} ждёт ответа на вопрос {item.get('id')}: "
             f"{' '.join(str(item.get('text') or '').split())}"]
    if item.get("options"):
        parts.append(f"варианты: {item['options']}")
    parts.append(f"рекомендация {recommend}" if recommend else "рекомендации нет")
    parts.append(f"ответ: /answer {item.get('id')} {recommend or '<вариант>'} "
                 f"или окно в панели{tail}")
    return "; ".join(parts)


def step_report_texts(p: Paths, step: dict, cards=()) -> list[str]:
    """Тексты отчётов шага, где автор мог выписать вопрос (S5, п.2).

    Источники — постановка, отчёт ревью постановки, отчёт ревью шага и результат каждой
    карточки шага. Чтение терпимое: нет файла или он не читается — вопроса из него просто
    нет, а тик не встаёт из-за отсутствия необязательного файла.

    Карточки фильтруются по шагу: ключ `planloop:<план>:<шаг>:<вид>` (S5, ревью шага).
    Результат карточки чужого шага — чужой отчёт, и вопрос из него завёл бы ложное ожидание
    текущему шагу. Карточка без ключа источником не считается: у неё нет адреса шага.
    """
    texts: list[str] = []
    sid = step.get("id") or ""
    rel = prompt_rel(p, sid)
    paths = [Path(p.repo) / rel] if rel else []
    paths += [prompt_review_path(p, sid), review_path(p, sid)]
    for path in paths:
        try:
            texts.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    prefix = f"planloop:{p.plan_id}:{sid}:"
    for card in cards or ():
        if not str(card.get("key") or "").startswith(prefix):
            continue
        texts.append(f"{card.get('result') or ''}\n{card.get('error') or ''}")
    return texts


def register_questions(p: Paths, ctx: dict, state: dict | None = None) -> list[dict]:
    """Перенести строки вопросов из отчётов шага в файл вопросов (S5, п.2).

    Идемпотентно: страж только заводит недостающие строки со статусом «открыт» и пишет
    время вопроса в состояние плана. Дубликат — ровно пара «шаг + текст»: автор мог
    повторить вопрос в двух отчётах или повторить тик. ID присваивает `add_findings`
    (следующий свободный `Q-N` по файлу): нумерация в отчёте своя у каждого автора — каждая
    постановка пишет `Q1`, — поэтому номер автора ID реестра не задаёт (ревью шага S5:
    вопрос второго и следующих шагов молча терялся). Битый файл вопросов — причина в
    `ctx["step_questions_error"]`, второй файл молча не заводится.

    Уже записанный обрезанный вопрос приводится к полному тексту на месте (см.
    `_question_truncated_prefix`): до правки разбора вопрос обрывался на первом переносе,
    и в файле лежал кусок без вариантов и рекомендации. Второй строки при этом не заводится.

    Вопрос с командой замера (S2B плана 10) страж исполняет сам: `questions.enforce: true`
    (умолчание) — строка вопроса не заводится вовсе, владельцу остаётся строка истории
    «замерил: …»; `false` — правило понижено до замера, вопрос остаётся владельцу. Неудачный
    замер ничего не отменяет: вопрос заводится как обычный, а причина неудачи — в истории.
    """
    step = ctx.get("next")
    if not step or ctx.get("step_questions_error"):
        return []
    sid = step.get("id") or ""
    path = plan_questions_path(p)
    current = load_questions(path)
    pairs = {(_question_pair_key(item)) for item in current}
    fresh: list[dict] = []
    migrated: list[dict] = []
    # Замер пишет отметку в снимок состояния тика: своего `load_state` у него нет.
    enforce = questions_enforce(p)
    if not isinstance(state, dict):
        state = ctx.get("state") if isinstance(ctx.get("state"), dict) else None
    for text in step_report_texts(p, step, ctx.get("cards") or []):
        for block, parsed in parse_question_blocks(text):
            item = dict(parsed, step=sid, status=QUESTION_OPEN, answer="")
            pair = _question_pair_key(item)
            if pair in pairs:
                continue
            target = next((row for row in current if _question_truncated_prefix(row, item)), None)
            if target is not None:
                target["text"] = item["text"]
                target["options"] = item["options"]
                target["recommend"] = item["recommend"]
                pairs.add(_question_pair_key(target))
                migrated.append(target)
                continue
            measured = question_measure(p, sid, item["text"], block, state)
            pairs.add(pair)
            if measured is not None and measured.get("ok") and enforce:
                continue                  # измеримый вопрос не заводится: ответ в истории
            fresh.append(item)
    if migrated:
        write_questions(path, current, p.plan_id)
        ctx["step_questions"] = step_questions(path, sid)
        ctx["plan_questions"] = step_questions(path)
    if not fresh:
        return []
    rules = _question_rules()
    number = str(p.plan_id or "").strip()
    title = f"# Вопросы плана {number}" if number else "# Вопросы шага"
    rows = add_findings(path, fresh, QUESTIONS_HEADER, QUESTIONS_KEYS, title=title, **rules)
    added = rows[len(current):]
    if not isinstance(state, dict):
        state = ctx.get("state") if isinstance(ctx.get("state"), dict) else load_state(p)
    stamps = state.get("questions")
    stamps = dict(stamps) if isinstance(stamps, dict) else {}
    now = _now()
    for item in added:
        stamps[item["id"]] = dict(stamps.get(item["id"]) or {}, asked_at=now)
    state["questions"] = stamps
    ctx["state"] = state
    save_state(p, state)
    # Решение этого же тика обязано видеть новый вопрос: файл перечитан, а не «запомнен».
    ctx["step_questions"] = step_questions(path, sid)
    ctx["plan_questions"] = step_questions(path)
    ctx["step_questions_rel"] = plan_questions_rel(p)
    return added


#: Сколько первых символов обрезанного вопроса должны совпасть, чтобы строка считалась его
#: началом: короче — уже случайное совпадение, а не миграция.
QUESTION_MIGRATION_MIN = 12


def _question_truncated_prefix(old: dict, new: dict) -> bool:
    """`old` — обрезанное начало `new`: тот же шаг, вопрос открыт, текст — префикс нового."""
    if str(old.get("status") or "") != QUESTION_OPEN:
        return False
    if str(old.get("step") or "").strip().upper() != str(new.get("step") or "").strip().upper():
        return False
    before = " ".join(str(old.get("text") or "").split()).lower()
    after = " ".join(str(new.get("text") or "").split()).lower()
    return len(before) >= QUESTION_MIGRATION_MIN and before != after and after.startswith(before)


def _question_pair_key(item: dict) -> tuple[str, str]:
    """Ключ дубликата вопроса: шаг и текст, без регистра и лишних пробелов."""
    return (str(item.get("step") or "").strip().upper(),
            " ".join(str(item.get("text") or "").split()).lower())


def answer_timeout_seconds(cfg: config_mod.Config) -> int:
    """Секунды до автовыбора рекомендации: `notify.answer_timeout`, 0 — выключено (S5, п.5).

    Ноль берётся как есть (`_positive` здесь не годится: она съела бы ноль и вернула
    умолчание), нечисловое значение — умолчание `ANSWER_TIMEOUT_DEFAULT`.
    """
    notify = cfg.notify if isinstance(getattr(cfg, "notify", None), dict) else {}
    raw = notify.get("answer_timeout", ANSWER_TIMEOUT_DEFAULT)
    if isinstance(raw, bool):
        return ANSWER_TIMEOUT_DEFAULT
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return ANSWER_TIMEOUT_DEFAULT
    return value if value > 0 else 0


def _open_questions_text(rows: list[dict]) -> str:
    """Перечень открытых вопросов для отказа: `Q-1 · шаг S5: текст` (S5, п.4)."""
    return "; ".join(f"{item.get('id')} · шаг {item.get('step') or '—'}: {item.get('text') or ''}"
                     for item in rows)


def find_question_plan(cfg: config_mod.Config, qid: str) -> dict:
    """План, которому принадлежит открытый вопрос: ответ из чата адресуется сам (S5, п.4)."""
    found: list = []
    for spec in plans_of(cfg):
        try:
            p = resolve_paths(cfg, spec.id)
            rows = step_questions(plan_questions_path(p))
        except (GateError, OSError, ValueError, config_mod.ConfigError):
            continue
        if any(item["id"] == qid for item in rows):
            found.append(p)
    if len(found) == 1:
        return {"plan": found[0], "plans": found, "error": ""}
    if not found:
        return {"plan": None, "plans": [], "error": f"нет открытого вопроса {qid}"}
    numbers = ", ".join(str(item.plan_id) for item in found)
    return {"plan": None, "plans": found,
            "error": f"вопрос {qid} открыт в нескольких планах ({numbers}): укажи --plan"}


def answer_question(cfg: config_mod.Config, plan_id, question_id, value, *,
                    auto: bool = False) -> dict:
    """Ответить на вопрос шага — единственный путь записи ответа (S5, п.4).

    Панель, `/answer`, CLI и автовыбор по таймауту зовут эту функцию, поэтому файл вопросов
    руками не правится: два пути к одному состоянию разошлись бы. Проверки — номер вопроса,
    шаг вопроса (шага нет в плане — ответ не принимается) и статус «открыт». Отказ не
    исключение: и чат, и панель показывают его владельцу строкой.
    """
    qid = question_id_of(question_id)
    if not qid:
        return {"ok": False, "message": f"номер вопроса не вида Q-N: {question_id!r}"}
    # Ответ пишется дословно (§4.4): нормализация схлопывала бы авторский текст, а ячейку
    # от `|` и перевода строки защищает `findings_table`. Пустой ответ по-прежнему не ответ.
    value = str(value or "")
    if not value.strip():
        return {"ok": False, "message": f"нужен ответ: /answer {qid} <вариант>"}
    try:
        p = resolve_paths(cfg, str(plan_id) if plan_id else None)
    except config_mod.ConfigError as exc:
        return {"ok": False, "message": str(exc)}
    path = plan_questions_path(p)
    try:
        rows = load_questions(path)
    except (GateError, OSError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    opened = _open_questions_text([item for item in rows
                                   if item.get("status") == QUESTION_OPEN])
    target = next((item for item in rows if item["id"] == qid), None)
    if target is None:
        return {"ok": False, "message": f"нет вопроса {qid} в плане {p.plan_id}: "
                                        f"открытые — {opened or '—'}"}
    if target.get("status") != QUESTION_OPEN:
        # Повторное нажатие с тем же ответом — не ошибка: ответ уже записан (двойной клик в
        # панели, повтор из чата). Другой ответ на закрытый вопрос — отказ с названием того,
        # что уже принято.
        if " ".join(value.split()) == " ".join(str(target.get("answer") or "").split()):
            return {"ok": True, "qid": qid,
                    "step": str(target.get("step") or "").strip().upper(),
                    "question": dict(target), "answer": value,
                    "message": f"вопрос {qid} уже закрыт тем же ответом: {value}"}
        return {"ok": False, "message": f"вопрос {qid} уже закрыт: "
                                        f"{target.get('answer') or '—'}; открытые — "
                                        f"{opened or '—'}"}
    step = str(target.get("step") or "").strip().upper()
    declared = declared_steps(cfg, p)
    if declared and step not in declared:
        return {"ok": False,
                "message": (f"вопрос {qid} относится к шагу {step or '—'}, которого нет в "
                            f"плане {p.plan_id}: открытые — {opened or '—'}")}
    answer = value if not auto else (f"{value} — {AUTO_ANSWER_NOTE} "
                                     f"{_question_minutes(answer_timeout_seconds(cfg))} мин")
    target["status"] = QUESTION_CLOSED
    target["answer"] = answer
    write_questions(path, rows, p.plan_id)
    state = load_state(p)
    stamps = state.get("questions") if isinstance(state.get("questions"), dict) else {}
    if stamps.pop(qid, None) is not None:
        # Ожидание снимается вместе с поводом: записи в hold_steps/hold_released нет.
        if stamps:
            state["questions"] = stamps
        else:
            state.pop("questions", None)
        save_state(p, state)
    return {"ok": True, "qid": qid, "step": step, "question": dict(target), "answer": answer,
            "message": f"вопрос {qid} закрыт: {answer}"}


def _question_alarm_due(entry: dict, now: float) -> bool:
    """Пора ли снова тревожить по вопросу без рекомендации: одна тревога в `QUESTION_ALARM_GAP`."""
    last = entry.get("alarm_at")
    return not isinstance(last, (int, float)) or now - float(last) >= QUESTION_ALARM_GAP


def question_idle_text(p: Paths, item: dict, timeout: int) -> str:
    """Текст тревоги: вопрос без рекомендации стоит, автовыбор по таймауту не сработает."""
    return attention_text(
        p.plan_id,
        f"вопрос {item['id']} без рекомендации — конвейер стоит, шаг {item.get('step') or '—'}",
        evidence=[
            f"Вопрос: {item.get('text') or '—'}",
            f"Варианты: {item.get('options') or '—'}",
            f"Рекомендация не названа, поэтому автовыбор по таймауту "
            f"({_question_minutes(timeout)} мин) не срабатывает — вопрос закрывает владелец",
        ],
        todo=[f"ответить на вопрос {item['id']}: вариант или отклонение "
              f"(`python .pipeline/gate.py --plan {p.plan_id} --answer {item['id']} <буква>`)"],
        commands=[f"python .pipeline/gate.py --plan {p.plan_id} --status"],
    )


def sweep_questions(cfg: config_mod.Config, p: Paths, ctx: dict, state: dict | None = None,
                    say=None) -> list[dict]:
    """Принять рекомендацию по таймауту (S5, п.5); запись идёт той же `answer_question`.

    Автовыбор — не молчаливое согласие: он работает только при названной рекомендации,
    пишет пометку «авто» и уведомляет владельца строкой. Вопрос без рекомендации, без
    известного времени вопроса и ключ `answer_timeout: 0` ждут человека — но вопрос без
    рекомендации, у которого истёк срок, не молчит: он поднимает тревогу и пишет строку
    в stderr, чтобы тик перестал быть «silent» (21.11.2026, план 34: вопрос с решением
    «рекомендации нет» ждал ответа 3 ч, таймера у него не было).
    """
    timeout = answer_timeout_seconds(cfg)
    if timeout <= 0:
        return []
    try:
        rows = load_questions(plan_questions_path(p))
    except (GateError, OSError, ValueError):
        return []                      # причина уже в ctx: по ней решение даёт стоп
    if not isinstance(state, dict):
        state = ctx.get("state") if isinstance(ctx.get("state"), dict) else load_state(p)
    stamps = state.get("questions") if isinstance(state.get("questions"), dict) else {}
    stamps = dict(stamps)
    now = _now()
    done: list[dict] = []
    alarmed = False
    for item in rows:
        if item.get("status") != QUESTION_OPEN:
            continue
        recommend = str(item.get("recommend") or "").strip()
        entry = stamps.get(item["id"])
        entry = dict(entry) if isinstance(entry, dict) else {}
        asked = entry.get("asked_at")
        if not recommend or not isinstance(asked, (int, float)):
            # Вопрос без рекомендации автовыбором не закрывается никогда: решение срабатывает
            # только по букве варианта. Раньше такой вопрос просто `continue` — и стоял молча
            # до ответа владельца, а тик выходил «silent». Тревога не решает за человека: она
            # называет вопрос, будит и повторяется не чаще QUESTION_ALARM_GAP.
            if (isinstance(asked, (int, float)) and now - float(asked) >= timeout
                    and _question_alarm_due(entry, now)):
                entry["alarm_at"] = now
                stamps[item["id"]] = entry
                alarmed = True
                sent = attention(cfg, question_idle_text(p, item, timeout),
                                 f"question_idle:{p.plan_id}:{item['id']}", say=say)
                print(f"[attention] план {p.plan_id}, вопрос {item['id']}: рекомендации нет, "
                      f"ждёт владельца {_question_minutes(now - float(asked))} мин — "
                      f"{'тревога отправлена' if sent else 'повтор подавлен'}",
                      file=sys.stderr)
            continue
        if now - float(asked) < timeout:
            continue
        result = answer_question(cfg, p.plan_id, item["id"], recommend, auto=True)
        if not result.get("ok"):
            continue
        stamps.pop(item["id"], None)
        done.append(result)
        (say or (lambda text: notify(cfg, text)))(
            f"⏱ {item['id']} принят автоматически: {recommend} (рекомендация) — "
            f"прошло {_question_minutes(timeout)} мин")
    if done or alarmed:
        closed = {item["qid"] for item in done}
        if stamps:
            state["questions"] = stamps
        else:
            state.pop("questions", None)
        ctx["state"] = state
        save_state(p, state)
        ctx["step_questions"] = [item for item in (ctx.get("step_questions") or [])
                                 if item["id"] not in closed]
        if ctx.get("plan_questions") is not None:
            ctx["plan_questions"] = [item for item in ctx.get("plan_questions") or []
                                     if item["id"] not in closed]
    return done


def announce_questions(cfg: config_mod.Config, p: Paths, state: dict | None = None,
                       say=None) -> list[dict]:
    """Уведомить владельца об открытых вопросах: на вопрос — одно, повтор не чаще часа.

    Открытые вопросы читаются из файла, а не из `ctx`: функция не зависит от того, успел ли
    тик собрать контекст. `notified_at` живёт в состоянии плана, как у тревог.
    """
    opened = step_questions(plan_questions_path(p))
    if not opened:
        return []
    if not isinstance(state, dict):
        state = load_state(p)
    stamps = state.get("questions") if isinstance(state.get("questions"), dict) else {}
    stamps = dict(stamps)
    now = _now()
    sent: list[dict] = []
    for item in opened:
        entry = stamps.get(item["id"])
        entry = dict(entry) if isinstance(entry, dict) else {}
        last = entry.get("notified_at")
        if isinstance(last, (int, float)) and now - float(last) < QUESTION_NOTICE_GAP:
            continue
        if not isinstance(entry.get("asked_at"), (int, float)):
            entry["asked_at"] = now
        entry["notified_at"] = now
        stamps[item["id"]] = entry
        sent.append(item)
        (say or (lambda text: notify(cfg, text)))(
            question_notice_text(p, item, answer_timeout_seconds(cfg)))
    if sent:
        state["questions"] = stamps
        save_state(p, state)
    return sent


def prompt_verdict(p: Paths, step_id: str) -> dict | None:
    """Вердикт ревью постановки + `recon` из отчёта: сверять надо ту же версию (D1)."""
    result = verdict_at(prompt_review_path(p, step_id))
    if result is None:
        return None
    result["recon"] = prompt_recon(result["path"])
    return result


def next_prompt_step(steps: list[dict], step_id: str) -> dict | None:
    """Шаг, чья постановка заказывается заранее: N+1, если он не последний (D2).

    У предпоследнего шага N+1 — последний, и заказ/гейт не работают: иначе конвейер
    встал бы на закрытии последнего шага без постановки, которой уже не будет.
    """
    ids = [str(s.get("id") or "").upper() for s in steps or []]
    want = str(step_id or "").upper()
    if want not in ids:
        return None
    index = ids.index(want)
    if index + 1 >= len(ids) - 1:
        return None
    return steps[index + 1]


def _step_tokens(cell) -> list[str]:
    """Шаги находки как токены: «S1,S3» — два адресата, «S0-S2» — не токен и не адресат."""
    return [part for part in re.split(r"[,;\s]+", str(cell or "").upper()) if part]


def step_execution_findings(findings: list[dict], step_id: str) -> list[dict]:
    """Находки класса `гейт-исполнение` по этому шагу: план они не держат, проверяются в работе.

    Маршрут решает класс, вердикт — статус (правило S1): поэтому фильтр по классу и по «не
    закрыто», а не по «открыто». Закрытая находка уже закрыта правкой плана — проверять при
    исполнении нечего. Открытая остаётся видимой: иначе живая находка молча выпала бы из карточки,
    если ревьюер забыл поставить `не блокирует`.
    """
    want = str(step_id or "").strip().upper()
    if not want:
        return []
    return [item for item in findings
            if item.get("kind") == "гейт-исполнение"
            and item.get("status") in ("открыто", "не блокирует")
            and want in _step_tokens(item.get("step"))]


def execution_check_lines(p: Paths, step: dict) -> list[str]:
    """Блок «проверить при исполнении» для карточки шага; пустой список — блока нет.

    Реестр читает сам построитель тела, а не вызывающая сторона: у карточки шага два входа
    (`run_gate` и ручной `step_card.py`), и второй не должен отставать от первого.
    Битый реестр карточку не отменяет: ревью плана на таком реестре встаёт инцидентом раньше, а
    шаг без карточки встал бы молча и надолго.
    """
    try:
        rows = step_execution_findings(load_findings(plan_findings_path(p)), step.get("id", ""))
    except (GateError, OSError, ValueError) as exc:
        # `exc` от `load_findings` уже несёт путь и причину — второй раз их не дублируем.
        return ["", f"проверить при исполнении: {exc} — находки класса `гейт-исполнение` "
                    "сверь по файлу"]
    if not rows:
        return []
    lines = ["", "проверить при исполнении (находки реестра класса `гейт-исполнение`, "
                 "вердикт плана они не держат):"]
    lines += [f"- {item.get('id')} (шаг {item.get('step')}, {item.get('status')}): "
              f"{item.get('text')} → {item.get('action')}" for item in rows]
    return lines


def stall_line(row: dict) -> str:
    """Строка «инструмент залип» по строке журнала метрики; пусто, если залипания нет.

    Владельцу и исполнителю нужны четыре числа: какой инструмент, сколько вызовов, сколько
    суммарно секунд и сколько из них упали по таймауту (17-S2, Д2). Формат — один, потому
    что его читают двое: комментарий стража (`report_stalls`) и карточка шага.
    """
    if not isinstance(row, dict) or not row.get("stall"):
        return ""
    tool = row.get("stall_tool") or "—"
    return (f"инструмент залип: {tool},"
            f" вызовов {_as_int(row.get('stall_calls'), 0)},"
            f" суммарно {_as_int(row.get('stall_s'), 0)} с,"
            f" таймаутов {_as_int(row.get('stall_timeouts'), 0)}")


#: Сколько отметок `stall_reported` держать в состоянии плана. Помним только хвост: отметка
#: нужна, чтобы не повторять комментарий по тому же прогону (Д7), а не как отчёт.
STALL_KEEP = 20


def method_rule_lines(p: Paths, step: dict) -> list[str]:
    """Блок «ПРАВИЛО МЕТОДА» для тела карточки, если прошлая попытка залипла (Д6, 17-S2).

    Источник — последняя по времени строка метрики этого шага по карточке шага или правок:
    залипание в карточке ревью или разведки ничего не говорит о следующей работе. Нет строк
    или залипания нет — пустой список, тело карточки остаётся прежним дословно.
    """
    try:
        cfg = getattr(p, "cfg", None)
        plan_id, step_id = str(p.plan_id or ""), str(step.get("id") or "")
        if cfg is None or not plan_id or not step_id:
            return []
        rows = [row for row in step_metrics.read_rows(step_metrics.metrics_path(cfg))
                if str(row.get("plan") or "") == plan_id
                and str(row.get("step") or "") == step_id
                and str(row.get("card_kind") or "") in ("step", "fix")]
        if not rows:
            return []
        line = stall_line(max(rows, key=lambda item: _as_float(item.get("ts"), 0.0)))
        if not line:
            return []
        seconds, _repeats = step_metrics.stall_thresholds(cfg)
        return [
            "ПРАВИЛО МЕТОДА",
            f"- прошлая попытка: {line}.",
            "- собирать файл целой записью (`write_file`), а не десятками мелких правок:",
            "  правка в залипшем инструменте стоит секунд, которых в шаге нет.",
            f"- если инструмент снова идёт дольше {seconds} с — менять метод и писать об этом",
            "  в карточке, а не ждать таймаута: ожидание стоит тех же секунд.",
            "",
        ]
    except Exception as exc:  # noqa: BLE001 - тело карточки не имеет права падать
        config_mod.debug_note("method_rule", exc)
        return []


def step_card_body(p: Paths, step: dict, prompt: str | None = None) -> str:
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    lines = [f"ШАГ ПЛАНА {plan_id} {step['id']} — {step['title']}", ""]
    if prompt:
        lines += [f"ПОСТАНОВКА (прочитать ПЕРВОЙ, до кода): `{prompt}`", ""]
    lines += [f"Цель: {step.get('goal', '')}", "", "Файлы (править/создавать):"]
    lines += [f"- {f}" for f in step.get("files") or []]
    lines += ["", "Гейты (запустить и показать фактический результат):"]
    lines += [f"- {g}" for g in step.get("gates") or []]
    lines += execution_check_lines(p, step)          # ← 7-S3: находки реестра к проверке в работе
    lines += ["", f"Готово = {step.get('dod', '')}", "", "Что прочитать перед началом:"]
    if prompt:
        lines += [f"- {prompt} (постановка шага — границы, стоп-сигналы, формат отчёта)"]
    lines += [f"- {r}" for r in step.get("read") or []]
    if step.get("notes"):
        lines += ["", f"Важно: {step['notes']}"]
    # 17-S2: прошлая попытка залипла в инструменте — карточка говорит, чем метод менять.
    lines += [""]
    lines += method_rule_lines(p, step)
    lines += [
        "ЖЁСТКИЕ ПРАВИЛА",
        "- Работать только по этому шагу, не расширять объём и не начинать следующий.",
        "- Существующие тесты не удалять и не ослаблять; новые — happy path + границы + интеграция.",
        "- Вывод команд ≤ 200 строк: `pytest --tb=line`, `git status --porcelain | head -30`,",
        "  большой вывод — в файл и `grep` по нему (объём вывода = токены каждого хода).",
        "- Не перечитывать уже прочитанный файл: адресно `grep`/`read_file offset`.",
        "- Прогоны: сначала точечные (по затронутым файлам), полный прогон — один раз перед",
        "  коммитом. `-q` не дублировать, если он уже в `pytest.ini`: второй скрывает итог.",
        "- Медленные тесты помечены `slow`: точечный прогон — `pytest -m \"not slow\"`",
        "  по затронутым файлам; полный — один раз перед коммитом (`python -m pytest`, без `-m`).",
        "",
        "ЗАВЕРШЕНИЕ (обязательно, иначе конвейер встанет)",
        "1. Гейты зелёные — показать фактические цифры.",
        "2. Коммит и пуш ТОЛЬКО файлов шага: `git add <файлы шага>` (НЕ `git add -A`: в дереве",
        "   могут быть правки параллельной сессии), сообщение на русском, `git push`.",
        "3. Закрыть карточку через kanban_complete: что сделано, прогоны с цифрами, sha коммита.",
        "   Строку в журнал плана НЕ добавлять — её добавит страж после APPROVE ревьюера.",
        "",
        f"Контракт шага: `{plan_rel}` §{step['id']}.",
    ]
    return "\n".join(lines)


class UnknownAssignee(Exception):
    """Токен «Исполнитель:» — ни профиль, ни маршрут: карточку ставить некуда (план 5, S7).

    Исключение, а не тихий переход к драйверу: шаг с опечаткой в профиле обязан встать
    инцидентным стопом с перечнем того, что писать можно.
    """

    def __init__(self, step_id: str, token: str, known: set[str]) -> None:
        self.step_id = str(step_id)
        self.token = str(token)
        self.known = set(known)
        super().__init__(f"шаг {self.step_id}: неизвестный исполнитель {self.token!r}")


def unknown_assignee_reason(p: Paths, step: dict, exc: UnknownAssignee) -> str:
    """Текст стопа «неизвестный исполнитель»: предметки, профили и что с этим делать."""
    subjects = ", ".join(config_mod.SUBJECT_AREAS)
    known = ", ".join(sorted(exc.known)) or "—"
    step_id = step.get("id") or exc.step_id
    return (f"шаг {step_id}: исполнитель `{exc.token}` — не профиль и не предметка маршрута, "
            f"план не начинаю; предметки: {subjects}; профили: {known}; укажи профиль из "
            f"`profiles` или добавь предметку в `routing`")


def _step_assignee(p: Paths, step: dict) -> str:
    """Профиль-исполнитель карточки: явный профиль, предметка маршрута, иначе драйвер.

    Порядок именно такой: имя профиля сильнее предметки, предметка без записи в `routing`
    ведёт к драйверу. Токен, который не профиль и не предметка, — `UnknownAssignee`
    (S7), кроме пути и фразы из кавычек: `pipeline.json` — файл, а не исполнитель.
    """
    owner = str(step.get("owner") or "").strip()
    raw_profiles = (p.cfg._raw or {}).get("profiles")
    profiles = ({str(value).strip() for value in raw_profiles.values()}
                if isinstance(raw_profiles, dict) else set())
    routing = config_mod.routing_map(p.cfg)
    for token in re.split(r"[+,]", owner):
        token = token.strip().strip("`")
        if not token or "." in token or "/" in token or " " in token:
            continue          # пусто, путь или фраза из кавычек — не исполнитель
        if token in profiles:
            return token
        if token in config_mod.SUBJECT_AREAS:
            return routing.get(token, p.cfg.driver)
        raise UnknownAssignee(str(step.get("id") or ""), token,
                              config_mod.known_profiles(p.cfg))
    return p.cfg.driver


def assignee_error(p: Paths, step: dict) -> str:
    """Причина, по которой карточку шага ставить некуда, или "" (S7).

    Нужна и решению (инцидентный стоп вместо повторного снятия), и тексту стопа.
    """
    try:
        _step_assignee(p, step)
    except UnknownAssignee as exc:
        return unknown_assignee_reason(p, step, exc)
    return ""


def scout_card_body(p: Paths, steps: list[dict]) -> str:
    """Тело карточки разведки: план, вопрос, состав шагов, границы и приёмка (C6).

    Шаги приходят из `ctx["steps"]` — сборщик второй раз не вызывается; пустой список
    печатает пустой блок, без исключения.
    """
    plan_rel = p.cfg.rel(Path(p.plan_file))
    artifact = p.cfg.rel(scout_artifact(p))
    lines = [
        f"РАЗВЕДКА ПЛАНА {p.plan_id} — готовые решения и практики",
        "",
        f"План: `{plan_rel}`",
        "",
        "СОСТАВ ШАГОВ",
    ]
    for step in steps or []:
        lines.append(f"- {step.get('id', '')}. {step.get('title', '')}".rstrip(". "))
    lines += [
        "",
        "ВОПРОС РАЗВЕДКИ",
        "Есть ли готовое решение (продукт, библиотека, практика) для целей этого плана?",
        "Что берём, что форкаем, а что делаем своим?",
        "",
        "ЧТО СДЕЛАТЬ",
        f"- Положить артефакт `{artifact}` по §5a формата (`docs/PLAN-FORMAT.md:191-229`).",
        "- Кандидат — строкой таблицы: название, ссылка, звёзды, дата последнего коммита,",
        "  лицензия, что даёт, чего стоит в днях, вердикт «берём / форкаем / своё».",
        "- Исход разведки — `go`, `needs-clarification` или `kill`.",
        "",
        "ГРАНИЦЫ",
        "- Таймбокс 30 минут на карточку: по истечении артефакт сдаётся как есть.",
        "- Код чужих проектов не копируется; лицензия «своя»/NOASSERTION — только идеи.",
        "- План не править (это отдельный шаг), код приложения не писать, ничего не коммитить.",
        "- «Готового нет» — валидный исход, а не провал: артефакт с обоснованием всё равно",
        "  кладётся, а первая строка результата — `Итог: Готового нет — …`.",
        "",
        "ПРИЁМКА КАРТОЧКИ",
        "- Закрыть карточку результатом, первая строка — `Итог: …`",
        "  (например, `Итог: берём X — 2 дня на интеграцию`).",
    ]
    return "\n".join(lines)


def plan_review_freeze_lines(round_no: int) -> list[str]:
    """Блок заморозки для карточки ревью круга ≥ 2 (S2 плана 7); круг 1 — пусто.

    Смысл: после правки площадь проверки не растёт. Ревьюер смотрит изменённый текст и его
    стыки, а не перечитывает план заново — иначе каждый круг рождает находки в неизменённых
    местах, и цикл не сходится.
    """
    if int(round_no or 1) < 2:
        return []
    return [
        f"ЗАМОРОЗКА НАХОДОК (круг {round_no})",
        "- Новые находки — только по строкам, изменённым последней правкой плана, и по их",
        "  стыкам с соседними шагами: находка вне изменённого текста возвращается автору",
        "  без правки плана.",
        "- Незакрытые ID прошлого круга остаются в реестре и закрываются фактом, а не новой",
        "  строкой; закрытые повторно не открываются.",
        "- Правка сломала соседний шаг — это новая находка с новым F-N и ссылкой на прежний ID.",
        "",
    ]


def plan_review_card_body(p: Paths, plan_rel: str, report_rel: str, profile: str,
                          round_no: int = 1) -> str:
    """Тело карточки ревью плана: замысел до кода, чек-лист, формат вердикта (C1, C5).

    Ключевые слова карточки проверяются тестом: «НЕ code review», «связность», «1.5»,
    `plan-1.md`, `APPROVE`, «Конституция». Роль берёт их отсюда, а не из скилла: карточка
    самодостаточна. С шага 5-S9 в карточке есть конституция проекта: вердикт без ссылки
    на её пункт (`approval.CONSTITUTION_REF_RE`) страж считает непригодным (`unreferenced`).
    С шага 7-S1 тело несёт реестр находок плана (`plan_findings_rel`) целиком — путь, правило
    блокирования и таблицу: закрыть все открытые ID можно, только если они перед глазами.
    С круга 2 (S2 плана 7) тело несёт заморозку находок: новые — только по строкам,
    изменённым последней правкой.
    """
    findings_path = plan_findings_path(p)
    if findings_path.is_file():
        # Реестр есть — печатаем его таблицу даже без находок: файл с одной шапкой значит
        # «находок пока нет», а не «реестра ещё нет».
        registry_lines = findings_table(load_findings(findings_path))
    else:
        registry_lines = ["- реестра ещё нет: первая находка создаёт файл с шапкой выше."]
    return "\n".join([
        f"# Ревью плана {p.plan_id}: замысел до кода",
        "",
        "Ты — роль pipeline-plan-reviewer. Это НЕ code review: код шагов ещё не написан.",
        f"План: `{plan_rel}`. Отчёт: `{report_rel}` — ровно одна строка вердикта:",
        "`## ВЕРДИКТ: APPROVE` либо `## ВЕРДИКТ: REQUEST_CHANGES`.",
        "",
        "Первая строка отчёта — метка версии: `plan_sha256:` и 64 hex-символа sha256",
        f"ВЕРСИИ плана: `python .pipeline/gate.py --plan-version-sha {plan_rel}` — журнал",
        "выполнения в версию не входит (строка журнала вердикт не сбрасывает, правка шага",
        "сбрасывает). Без метки вердикт считается",
        "непривязанным к версии плана, и страж закажет повторное ревью.",
        "",
        "Вторая обязательная строка отчёта — ссылка на пункты конституции проекта:",
        "`Конституция: п.N — …` (номера пунктов `docs/CONSTITUTION.md`, которым план",
        "соответствует, или номер нарушенного). Без неё вердикт не принимается: страж",
        "считает отчёт непригодным и закажет повторное ревью.",
        "",
        "ЧЕК-ЛИСТ (замысел, а не текст)",
        "- соответствие конституции: назвать номера пунктов и нарушенный пункт, если он есть;",
        "- связность шагов: каждый опирается на результат предыдущего, шагов-сирот нет;",
        "- размер шага не больше 1.5 дней и один проверяемый результат;",
        "- пересечение файлов между шагами: общий файл требует причины;",
        "- риски названы, а не подразумеваются; у каждого шага есть стоп-сигнал;",
        "- разведка учтена: раздел «## Разведка», вердикты кандидатов, шаг интеграции;",
        "- гейт назван командой; число — ориентир и сверяется при исполнении шага, не здесь.",
        "",
        "НЕ НАХОДКИ (не блокируют и не идут в вердикт)",
        "- номера строк и точность адресов в коде (`файл:строка`): они разъезжаются от любой",
        "  правки движка, а цена такой находки — целый круг правок плана;",
        "- арифметика числа тестов и суммы гейтов по шагам;",
        "- имена внутренних функций и полей, если шаг называет источник данных;",
        "- формулировки, стиль, пунктуация, полнота перечня пунктов.",
        "",
        "НЕ БЛОКИРУЕТ: ИСПОЛНИМОСТЬ ГЕЙТОВ (класс `гейт-исполнение`)",
        "- выполнится ли команда как записана: каталог вывода создан, бинарь есть на PATH, `grep`",
        "  считает то, что названо; ожидаемые числа, пути вывода и способ подсчёта вывода —",
        "  проверяются при исполнении шага, а не кругом правок плана;",
        "- такие находки ведутся классом `гейт-исполнение` и статусом `не блокирует`: вердикт они",
        "  не держат, а уходят в карточку шага блоком «проверить при исполнении»;",
        "- блокирует только отсутствие команды у гейта: у такой находки статус `открыто`, и только",
        "  она держит вердикт плана до правки.",
        "",
        "РЕЕСТР НАХОДОК (обязательно)",
        f"- Файл: `{plan_findings_rel(p)}`; шапка таблицы — `{findings_table([])[0]}`.",
        "- Открытые ID (`открыто`) закрыть фактом или подтвердить, новые находки добавлять",
        "  строками с новым F-N; закрытые повторно не поднимаются. Блокирует вердикт только",
        "  `открыто` вместе с держащим классом: `замысел`, `двусмысленность` и `гейт-исполнение`",
        "  держат; `стиль` и `формат-стража` не держат даже при `открыто` — они остаются строками",
        "  реестра и маршрутом правки не становятся (мелкое — строка, а не круг правок;",
        "  «подробность» — имя, порядок слов — пометка внутри строки `стиль`, а не отдельный класс).",
        "",
        *plan_review_freeze_lines(round_no),
        "РЕЕСТР СЕЙЧАС",
        *registry_lines,
        "",
        "ГРАНИЦЫ",
        "- План не правишь, шаг не закрываешь, код шагов не пишешь.",
        "- Блокирует только то, из-за чего шаг нельзя выполнить или появятся два источника",
        "  правды: шаг без входа, общий файл без причины, нет риска или стопа при внешней",
        "  зависимости, разведка не учтена, два шага противоречат друг другу, нарушен",
        "  пункт конституции.",
        "- Находка — номер шага и путь файла (без номеров строк), что не так и что сделать.",
        "- Находки, закрытые в прошлом круге, повторно не поднимаются: новый круг смотрит",
        "  изменённый текст плана и его стыки.",
        "- Вердикт — только по плану на текущем HEAD.",
        "- `REQUEST_CHANGES` даёт круг правок плана, пока не исчерпан лимит "
        "`max_fix_rounds`; после лимита решение принимает владелец.",
        "",
        f"Профиль карточки — `{profile}`; карточка поставлена стражем конвейера.",
    ])


def plan_fix_notice(plan_id: str, cid: str, profile: str, round_no: int,
                    rows: list[dict] | None) -> str:
    """Сообщение о круге правок плана: прежняя первая строка + открытые находки.

    Зачем. До 0.34.0 владелец получал только «правим план кругом 1, карточка t_…»: он видел
    счёт находок в панели и не видел ни одной. Открытые находки реестра — это ровно то, за что
    держится вердикт, поэтому они идут в сообщение строками: ID, шаг, суть, что сделать.
    Текст режется по-строчно, а не молча: хвост называет число невлезших находок, а полный
    реестр остаётся файлом.
    """
    head = (f"план {plan_id}: ревью вернуло REQUEST_CHANGES — правим план кругом "
            f"{round_no} (карточка {cid}, профиль {profile}); после правки ревью повторится")
    opened = [row for row in (rows or [])
              if str(row.get("status") or "") == "открыто"]
    if not opened:
        return head
    lines = [head, f"открыто находок: {len(opened)} — что править:"]
    budget = NOTICE_TOTAL_CAP
    shown = 0
    for row in opened:
        text = _notice_part(row.get("text"), NOTICE_TEXT_CAP)
        action = _notice_part(row.get("action"), NOTICE_ACTION_CAP)
        line = f"{row.get('id') or '?'} ({row.get('step') or '—'}): {text}"
        if action:
            line += f" → {action}"
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
        shown += 1
    if shown < len(opened):
        lines.append(f"…и ещё {len(opened) - shown}: целиком — в реестре находок плана")
    return "\n".join(lines)


def _notice_part(value, cap: int) -> str:
    """Одна фраза для сообщения: пробелы схлопнуты, длинное обрезано с многоточием."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def plan_fix_card_body(p: Paths, plan_rel: str, report_rel: str,
                       findings: list[str], round_no: int = 1,
                       max_rounds: int = 0) -> str:
    """Тело карточки правки плана: круг правок, только план, ревью повторится (D7).

    *round_no* — номер этого круга правок, *max_rounds* — лимит кругов проекта: по ним
    исполнитель видит, сколько попыток осталось, а не «круг 1» в каждой карточке.
    """
    limit = f" из {max_rounds}" if max_rounds else ""
    # Числа допуска — из конфигурации проекта: карточка не держит вторую копию правила,
    # которая разошлась бы с движком после правки `max_plan_growth`.
    limits = _plan_growth_limits(getattr(p.cfg, "max_plan_growth", None))
    lines = [
        f"# Правки плана {p.plan_id} по ревью (круг {round_no}{limit})",
        "",
        f"Ревью плана вернуло REQUEST_CHANGES. План: `{plan_rel}`. Отчёт: `{report_rel}`.",
        "",
        "НАХОДКИ РЕВЬЮ",
    ]
    picked = list(findings or [])[:FINDINGS_LIMIT]
    lines += [f"- {item}" for item in picked] or ["- Отчёт ревью без пунктов: читать целиком."]
    lines += [
        "",
        "ЧТО СДЕЛАТЬ",
        "- Правится только план: замысел, шаги, файлы, гейты, стоп-сигналы.",
        "- Правится только то, что названо в находках: план не переписывается целиком,",
        "  номера строк в коде и суммы тестов не пересчитываются, если находка не об этом.",
        f"- Находка закрывается минимальной правкой: рост версии плана — не больше "
        f"{limits['percent']}%",
        f"  и не больше {limits['chars_per_finding']} символов на закрытую находку.",
        "- Пояснения для ревьюера в план не писать: их место — отчёт карточки и журнал.",
        "- Рост сверх лимита останавливает конвейер стоп-сигналом `max_plan_growth`:",
        "  решает владелец.",
        "- Разведка остаётся учтённой: раздел «## Разведка» и отметка правок на месте.",
        "- Код шагов в этой карточке не пишется и не правится.",
        "- После правки ревью повторится само; когда круги правок исчерпаны, отказ",
        "  останавливает цикл и уходит владельцу с разбором.",
        "",
        "ЗАКРЫТИЕ",
        "- Результат карточки — первая строка `Итог: …` и перечень правок с адресами.",
    ]
    return "\n".join(lines)


def scout_fix_card_body(p: Paths, ctx: dict, round_no: int) -> str:
    """Тело карточки правки плана по разведке (0.20.0): что именно не так и как чинить.

    Карточка правит только план: разведка проведена, но план её не учёл, поэтому
    `plan_cycle.check_plan` вернул FAIL по P1-P4 и ревью плана не заказывается.
    """
    plan_id = p.plan_id
    plan_rel = ctx.get("plan_rel") or p.cfg.rel(Path(p.plan_file))
    artifact_rel = ctx.get("scout_artifact_rel") or p.cfg.rel(scout_artifact(p))
    fails = [row for row in (ctx.get("plan_checks") or []) if row[0] == "FAIL"]
    takes = ctx.get("plan_take_candidates") or ()
    lines = [
        f"# Правка плана {plan_id} по разведке (круг {round_no})",
        "",
        "Разведка проведена, а план её не учёл: пока `plan_cycle.check_plan` возвращает",
        f"FAIL по P1-P4, ревью плана не заказывается. План: `{plan_rel}`.",
        f"Артефакт разведки: `{artifact_rel}` — читать целиком, это источник правок.",
        "",
        "ПРОВАЛЕННЫЕ ПРОВЕРКИ (P1-P4)",
    ]
    lines += [f"- {code}: {message}" for _level, code, message in fails] or [
        "- проверки P1-P4 находок не дали — причину читать в причине решения стража"]
    lines += [
        "",
        "ЧТО СДЕЛАТЬ",
        "1. Вписать в раздел «## Разведка» строку `Правки по разведке: N`, где N — сколько",
        "   правок внесено по разведке (`0` — правок нет, но разведка учтена): без отметки",
        "   правка не оформлена (проверка P3).",
    ]
    if takes and any(code == "P2" for _level, code, _msg in fails):
        lines += [
            "2. Для каждого кандидата с вердиктом «берём» добавить шаг, в заголовке которого",
            "   есть «Интеграц…»: `### S<n>. Интеграция <кандидат>`. Кандидаты «берём»: "
            + ", ".join(f"`{name}`" for name in takes) + ".",
        ]
    elif takes:
        # P2 проходит: шаг интеграции в плане уже есть. Второй не заказываем — иначе
        # карточка нарушила бы собственные границы («шаги работ не трогаются»).
        lines += [
            "2. Шаг интеграции по кандидатам «берём» уже есть: "
            f"`{ctx.get('plan_integration_step') or 'см. раздел «Шаги»'}` — проверка P2 "
            "проходит, состав и порядок шагов не менять.",
        ]
    else:
        lines += [
            "2. Кандидатов с вердиктом «берём» в разделе нет — шаг интеграции не нужен.",
        ]
    lines += [
        "3. Ссылка раздела «## Разведка» обязана указывать на артефакт ЭТОГО плана:",
        f"   `{artifact_rel}`. Ссылка на артефакт другого плана — находка P4: тогда",
        "   разведка проверялась по чужому файлу.",
        "",
        "ГРАНИЦЫ",
        f"- Правится только файл плана `{plan_rel}`: замысел, шаги, ссылка, отметка.",
        "- Код, шаги работ (их состав и порядок) и утверждение не трогаются.",
        "- Утверждение обновляет владелец: после правки страж попросит",
        f"  `hermes pipeline plan --accept {plan_id}` — сам он его не пишет и не подделывает.",
        "- Коммит не делается: правку плана коммитит владелец или шаг после утверждения.",
        "",
        "ЗАКРЫТИЕ",
        "- Результат карточки — первая строка `Итог: …` и перечень правок с адресами.",
    ]
    return "\n".join(lines)


def prompt_order_body(p: Paths, step: dict) -> str:
    """Тело карточки промптеру: заказ постановки, без которой шаг не начинается."""
    plan_rel = p.cfg.rel(Path(p.plan_file))
    step_id = step["id"]
    files = ", ".join(f"`{f}`" for f in (step.get("files") or [])) or "—"
    gates = "; ".join(f"`{g}`" for g in (step.get("gates") or [])) or "—"
    return "\n".join([
        f"ПОСТАНОВКА ШАГА {step_id} ПЛАНА {p.plan_id} — {step['title']}",
        "",
        f"Ты — роль `{p.cfg.prompter}`. Шаг не начинается, пока нет постановки.",
        "Задача — написать постановку и довести гейт формы до PASS.",
        "",
        "КОНТРАКТ ШАГА (из сборщика шагов, не менять):",
        f"- План и раздел: `{plan_rel}` §{step_id}",
        f"- Цель: {step.get('goal', '')}",
        f"- Файлы: {files}",
        f"- Гейты: {gates}",
        f"- Готово = {step.get('dod', '')}",
        "",
        "ЧТО СДЕЛАТЬ:",
        f"1. Прочитать §{step_id} плана и фактическое состояние кода (файлы выше, `git log -3 -- <файлы>`).",
        "2. Написать постановку по блокам шаблона (роль и цель; контекст с адресами-символами;",
        "   границы; задачи; как проверить; стоп-сигналы; формат отчёта; вопросы; ревью).",
        "3. В шапку постановки добавить строку `recon_commit:` с полным sha HEAD, на котором снят",
        "   recon (рядом с датой, ветвью и HEAD): по ней страж отличает свежую постановку",
        "   от устаревшей и не гоняет лишнее ревью.",
        "4. Раздел «Состояние на момент постановки»: что уже сделано, чего не хватает до «Готово =».",
        "5. Гейт: `python .pipeline/check_task_prompt.py <файл>` → `ИТОГ: PASS`.",
        "6. Коммит и пуш ТОЛЬКО файла постановки, затем kanban_complete: путь, результат гейта, sha.",
        "",
        "ЗАПРЕТЫ: не править код приложения, не запускать тесты и сборки, не менять план,",
        "не коммитить чужие файлы (`git add` перечислением, не `-A`).",
    ])


# Служебная строка «реестр шага не читается»: одна и та же в блоке карточки (13-S2, §4.7)
# и в строках реестра — второй формулировки причины не вводим.
STEP_FINDINGS_UNREADABLE = "- реестр находок шага не читается: "


def step_registry_lines(p: Paths, step: dict, only_open: bool = False) -> list[str]:
    """Строки реестра шага для тела карточки; битый реестр — строкой с причиной (13-S2).

    Читается терпимо, как блок `registry_lines` в `prompt_review_card_body`: сборка тела
    карточки из-за реестра не падает (живой стоп на нём ставит решение). Файла нет — одна
    служебная строка: реестр заводит страж на первом круге правок. `only_open=True` —
    только статус `открыто`: карточке правок нужны ровно те строки, которые она закрывает.
    """
    path = step_findings_path(p, step["id"])
    try:
        rows = load_findings(path, STEP_FINDINGS_HEADER, STEP_FINDINGS_KEYS,
                             kinds=STEP_FINDING_KINDS)
    except (GateError, OSError, ValueError) as exc:
        return [f"{STEP_FINDINGS_UNREADABLE}{exc}"]
    if not path.is_file():
        return ["- реестра ещё нет: первая находка создаёт файл с шапкой выше."]
    if only_open:
        rows = [item for item in rows if str(item.get("status") or "") == "открыто"]
    return [f"- {item.get('id')} ({item.get('kind')}): {item.get('text')} → {item.get('action')}"
            for item in rows]


def open_step_findings(ctx: dict) -> list[dict]:
    """Открытые находки реестра шага: строки со статусом `открыто` в порядке файла.

    Реестра нет — пустой список: решение по несуществующему реестру ничего не держит.
    """
    return [item for item in (ctx.get("step_findings") or [])
            if str(item.get("status") or "") == "открыто"]


def round_cause_of(counts: dict) -> str:
    """Старший присутствующий класс находки — причина открытого круга (13-S5, §4.2).

    `counts` — счёт по классам (`{"блокирующая": 2, "значимая": 0, ...}`); берётся первый
    ненулевой в порядке старшинства `FIX_CAUSE_ORDER`. Нет ни одного — пустая строка: метрика
    пишет такой круг «неизвестно», а не подставляет класс наугад.
    """
    for cause in FIX_CAUSE_ORDER:
        if int(counts.get(cause) or 0) > 0:
            return cause
    return ""


def open_step_findings_lines(p: Paths, step: dict) -> list[str]:
    """Только строки находок реестра шага — без служебных («реестра ещё нет», ошибка)."""
    return [line for line in step_registry_lines(p, step, only_open=True)
            if line.startswith("- F")]


def step_findings_block(p: Paths, step: dict) -> list[str]:
    """Блок открытых находок реестра шага для карточки правок и комментария (13-S2, §4.7).

    Нет реестра или открытых строк нет — пустой список: тело карточки без реестра обязано
    остаться дословно прежним (на этом стоят `test_comments` и `test_prompt_fix_card`).
    Реестр не читается — блоком идёт одна служебная строка с причиной (§4.7): молча
    пропущенная ошибка читалась бы исполнителем как «открытых находок нет».
    """
    lines = step_registry_lines(p, step, only_open=True)
    open_lines = [line for line in lines if line.startswith("- F")]
    if not open_lines:
        unreadable = [line for line in lines if line.startswith(STEP_FINDINGS_UNREADABLE)]
        return [unreadable[0], ""] if unreadable else []
    header = findings_table([], STEP_FINDINGS_HEADER, STEP_FINDINGS_KEYS)[0]
    return [
        "ОТКРЫТЫЕ НАХОДКИ РЕЕСТРА ШАГА (круг закрывает только их)",
        f"- Реестр: `{step_findings_rel(p, step['id'])}`; шапка: `{header}`.",
        *open_lines,
        "",
    ]


def _step_fix_verdict_line(open_lines: list[str] | None, report_verdict: str,
                           report_rel: str) -> str:
    """Первая строка карточки правок шага: почему круг открыт — по реестру, а не по отчёту.

    С шага 13-S2 круг правок открывает и открытая строка реестра — в том числе при
    `APPROVE` в отчёте. Прежняя фраза «Ревьюер вернул REQUEST_CHANGES» в этом случае
    врала исполнителю: он видел в отчёте `APPROVE`, решал, что карточка от прошлого
    круга, и не правил ничего. Открытых строк нет — прежняя фраза дословно.
    """
    if open_lines:
        return (f"Реестр находок шага держит {len(open_lines)} открытых находок "
                f"(отчёт: {report_verdict or '—'}). Отчёт: `{report_rel}`.")
    return f"Ревьюер вернул REQUEST_CHANGES. Отчёт: `{report_rel}`."


def review_card_body(p: Paths, step: dict, attempt: int, report_rel: str,
                     prompt: str | None = None, runs: dict | None = None) -> str:
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    if prompt:
        prompt_block = [
            "СВЕРКА С ПОСТАНОВКОЙ (обязательный раздел отчёта)",
            f"- Постановка шага: `{prompt}` — прочитать её целиком.",
            "- Сверить итог по трём разрезам: «Что сделать» (каждый пункт — выполнен/нет, с file:line),",
            "  «Границы» (запрещённое — проверить, что НЕ сделано), «Формат отчёта».",
            "- Выполненное СВЕРХ постановки — находка (расширение объёма), а не плюс;",
            "  расхождение постановки с планом/кодом — отдельная находка с указанием, что верно.",
        ]
        if str(step.get("risk") or "").lower() == "high":
            prompt_block += [
                "- У шага пометка «Риск: high»: сверить сделанное с блоком «Спека» постановки.",
                "  Расхождение со спекой (сделано не то или не всё) — блокирующая находка;",
                "  стилистические замечания по-прежнему не блокируют.",
            ]
    else:
        prompt_block = [
            "СВЕРКА С ПОСТАНОВКОЙ (обязательный раздел отчёта)",
            "- Постановки шага в каталоге промтов НЕТ — зафиксировать это находкой",
            "  («шаг исполнен без постановки») и ревьюить только по §шага плана.",
        ]
    # Прогоны шага (план 9, S2): строка стража в карточке ревью — то же доказательство,
    # что в журнале и в уведомлении. Ревьюер видит, что прогон уже прогнан, и сверяет СВОИМ
    # прогоном гейта: расхождение двух доказательств — находка, а не повод закрыть шаг.
    runs_block = []
    counter = runs_counter(runs) if runs else ""
    if counter:
        proof_rel = p.cfg.rel(runs_proof_path(p, step["id"],
                                              str(runs.get("sha") or "")))
        runs_block = [
            "",
            "ПРОГОНЫ ШАГА (исполнил страж до ревью)",
            f"- {counter}",
            f"- Доказательство: `{proof_rel}` (sha `{runs.get('sha') or '—'}`)",
            "- Свой прогон гейта обязателен и идёт отдельно: если он красный, а строка стража",
            "  зелёная (или наоборот) — это находка про расхождение доказательств, а не",
            "  повод закрыть шаг по одной из двух строк.",
        ]
    return "\n".join([
        f"РЕВЬЮ ШАГА {step['id']} плана {plan_id} — {step['title']} (круг {attempt})",
        "",
        f"Отчёт писать в: `{report_rel}` (перезаписать, если файл есть).",
        "",
        f"Источники — РОВНО два (роль `{p.cfg.reviewer}`): (1) раздел шага в плане",
        f"`{plan_rel}` §{step['id']}; (2) код — `git show` коммита шага, сами файлы, тесты.",
        "Отчёт исполнителя и его транскрипт — НЕ доказательство.",
        "",
        *prompt_block,
        *runs_block,
        "",
        "РЕЕСТР НАХОДОК ШАГА (обязательно)",
        f"- Файл: `{step_findings_rel(p, step['id'])}`; шапка таблицы — "
        f"`{findings_table([], STEP_FINDINGS_HEADER, STEP_FINDINGS_KEYS)[0]}`.",
        "- Блокирует вердикт только статус `открыто`: `закрыто` и `не блокирует` шаг не держат.",
        "- Открытый ID закрывается фактом в колонке «Что сделать»: "
        "`Закрыто: адрес-символ или sha правки`.",
        "- Закрытая находка повторно не поднимается: круга она не даёт.",
        f"- Новая тема вне контракта шага — строка в реестр плана (`{plan_findings_rel(p)}`)",
        "  со статусом `не блокирует`; круга правок она не открывает.",
        "- Судят открытые строки и дифф правок: открытая строка держит шаг сильнее вердикта",
        "  отчёта, APPROVE с открытой строкой шаг не закроет.",
        "РЕЕСТР СЕЙЧАС",
        *step_registry_lines(p, step),
        "",
        "Гейты шага (прогнать САМОСТОЯТЕЛЬНО, свои цифры):",
        *[f"- {g}" for g in step.get("gates") or []],
        "",
        f"Готово = {step.get('dod', '')}",
        "",
        "ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТЧЁТА",
        f"- Файл `{report_rel}` содержит РОВНО ОДНУ строку вердикта (заголовок допустим):",
        "  `## ВЕРДИКТ: APPROVE` либо `## ВЕРДИКТ: REQUEST_CHANGES`.",
        "  Две строки вердикта или цитата чужого вердикта = страж остановит конвейер (AMBIGUOUS).",
        "- Первая строка блока «Объект ревью» — коммит шага в обратных кавычках:",
        "  Объект ревью: коммит `<sha>`. Без него стражу нечего сверять.",
        "- Разбор каждого пункта «Готово =» с file:line и свои прогоны с цифрами.",
        "(а) Раздел «Находки»: каждый пункт начинается меткой класса в квадратных скобках — [блокирующая], [значимая], [стилистическая]; у блокирующей допустима подметка через дробь ([блокирующая/тест-недетерминирован]).",
        "(б) Блок «Классы находок» со счётом по трём классам: блокирующих N, значимых N, стилистических N — без счёта стражу не с чем сверять вердикт.",
        "(в) Вердикт обязан совпадать с классами: есть блокирующая — REQUEST_CHANGES, нет — APPROVE. REQUEST_CHANGES без блокирующих закрывает шаг с замечаниями (они уходят в реестр находок плана), а отчёт без меток классов — ошибка формата: круг правок не заказывается, шаг остаётся открытым.",
        "",
        "ЗАПРЕТЫ: не править код, не коммитить, не пушить, не запускать полный набор тестов,",
        "не трогать данные приложения. Ревьюер выносит суждение — исправляет исполнитель.",
        "",
        "Закрыть карточку через kanban_complete: вердикт одной строкой + путь отчёта.",
    ])


def prompt_review_card_body(p: Paths, step: dict, report_rel: str,
                            prompt: str | None, round_no: int = 1) -> str:
    """Тело карточки ревью постановки: постановка против текущего HEAD (правило 3).

    С шага 12-S2 тело несёт реестр находок постановки (`prompt_findings_rel`) целиком —
    путь, правило блокирования и таблицу: закрыть все открытые ID можно, только если они
    перед глазами. Файл читается терпимо: битый реестр печатается строкой с причиной, а не
    роняет сборку тела (живой стоп на нём ставит решение).
    """
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    prompt_line = f"`{prompt}`" if prompt else "— (файл постановки не найден)"
    recon = prompt_recon(Path(p.repo) / prompt) if prompt else None
    head = head_commit(p) or "—"
    findings_path = prompt_findings_path(p, step["id"])
    try:
        rows = findings_table(prompt_findings_status(
                                  load_findings(findings_path, PROMPT_FINDINGS_HEADER,
                                                PROMPT_FINDINGS_KEYS)),
                              PROMPT_FINDINGS_HEADER, PROMPT_FINDINGS_KEYS)
        # Файл с одной шапкой значит «находок пока нет», а не «реестра ещё нет».
        registry_lines = rows if findings_path.is_file() else [
            "- реестра ещё нет: первая находка создаёт файл с шапкой выше."]
    except (GateError, OSError, ValueError) as exc:
        registry_lines = [f"- реестр находок постановки не читается: {exc}"]
    return "\n".join([
        f"РЕВЬЮ ПОСТАНОВКИ ШАГА {step['id']} плана {plan_id} — {step['title']}",
        "",
        f"Отчёт писать в: `{report_rel}` (перезаписать, если файл есть).",
        "",
        f"Постановка: {prompt_line}",
        f"Снята на: recon_commit `{recon or '—'}`; текущий HEAD: `{head}`.",
        *([f"Постановка переписана после REQUEST_CHANGES (круг {round_no}) — проверь, что",
           "находки прошлого отчёта закрыты и что правка не принесла новых расхождений."]
          if round_no > 1 else
          ["Постановка старше кода: проверить, не разъехалась ли она с фактом.",
           "Файлы уже существуют, поведение уже сделано, гейты не совпадают с реальными —",
           "это находки, а не повод согласиться «на глаз»."]),
        "",
        "ИСТОЧНИКИ — РОВНО ТРИ: (1) постановка; (2) раздел шага в плане",
        f"`{plan_rel}` §{step['id']}; (3) код — `git show`, файлы и тесты на HEAD.",
        "Отчёт исполнителя и его транскрипт — НЕ доказательство.",
        "",
        "ЧТО ПРОВЕРИТЬ",
        "- Символы и команды из постановки находятся в коде на HEAD. Несуществующий символ —",
        "  блокирующая находка; сдвиг строки находкой не считается.",
        "- Имена символов, ключей конфига и тестов не выдуманы: находятся по команде из",
        "  постановки.",
        "- «Готово =» и гейты постановки не противоречат разделу шага в плане.",
        "- Объём и запреты на месте: постановка не расширяет шаг и не разрешает запрещённое.",
        "- Стоп-сигналы не обходятся формулировками («уточнить», «при необходимости»).",
        "- Блок «Ревью» требует вердикта роли `reviewer`, а не самопроверки исполнителя.",
        "- Расхождение постановки с планом или кодом — находка с указанием, что верно.",
        "",
        "РЕЕСТР НАХОДОК ПОСТАНОВКИ (обязательно)",
        f"- Файл: `{prompt_findings_rel(p, step['id'])}`; шапка таблицы — "
        f"`{findings_table([], PROMPT_FINDINGS_HEADER, PROMPT_FINDINGS_KEYS)[0]}`.",
        "- Статус выводится стражем из класса, а не ставится подписью: `замысел`,",
        "  `двусмысленность`, `гейт-исполнение` держат старт шага; `формат-стража` и `стиль`",
        "  нет. `закрыто` ставится только с фактом правки.",
        "- Находка класса `двусмысленность` обязана назвать ДВА чтения и наблюдаемую разницу:",
        "  что разойдётся в коде или тесте. Разницы нет — это `стиль`, и шаг он не держит:",
        "  рассуждение о формулировке не повод для круга правок постановки.",
        "- Находка «постановка не говорит, в какой ветви вставлять код» — тоже `стиль`:",
        "  постановка задаёт поведение и приёмку, место вставки выбирает исполнитель, если",
        "  гейты зелены. Сверять надо расхождение с планом и кодом, а не наличие инструкции.",
        "- Открытые ID закрыть фактом или подтвердить статус, новые находки добавлять строками",
        "  с новым F-N. Закрытые повторно не поднимаются.",
        "- `закрыто` — только с фактом: адрес-символ (`файл.py::символ`) в действующей",
        "  редакции постановки либо sha коммита круга правок. Запись `закрыто` без факта —",
        "  дефект ревьюера.",
        "- Реестр переживает переписывание постановки: статусы `закрыто`/`не блокирует` остаются,",
        "  новая находка встаёт со следующим ID, нумерация F-N сквозная по шагу.",
        "- Отсутствие в отчёте строки `recon_commit:` — находка `не блокирует` (шаг стартует),",
        "  а не повод остановить конвейер.",
        "",
        "РЕЕСТР СЕЙЧАС",
        *registry_lines,
        "",
        "ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТЧЁТА",
        f"- Файл `{report_rel}` содержит РОВНО ОДНУ строку вердикта (заголовок допустим):",
        "  `## ВЕРДИКТ: APPROVE` либо `## ВЕРДИКТ: REQUEST_CHANGES`.",
        "  Две строки вердикта или цитата чужого вердикта = страж остановит конвейер (AMBIGUOUS).",
        "- В ревью постановки вердикт ставится по реестру находок: отчёт остаётся разбором и",
        "  перечнем находок, а старт шага решает закрытый реестр.",
        f"- Первая строка отчёта — отметка версии: `recon_commit: {recon or '<sha постановки>'}`;",
        "  тот же sha, что в шапке постановки. Другой sha — страж закажет пересборку постановки",
        "  и повторное ревью.",
        "",
        "ЗАПРЕТЫ: не править постановку и код, не коммитить, не пушить, не запускать тесты.",
        f"Постановку после REQUEST_CHANGES переписывает роль `{p.cfg.prompter}`, не ревьюер.",
        "",
        "Закрыть карточку через kanban_complete: вердикт одной строкой + путь отчёта.",
    ])


def _prompt_fix_verdict_line(findings: list[str] | None, report_verdict: str,
                             report_rel: str) -> str:
    """Первая строка карточки правок: почему круг открыт — по действующему вердикту.

    С шага 12-S2 круг правок открывает не только отчёт `REQUEST_CHANGES`: реестр с открытой
    находкой держит постановку и при отчёте `APPROVE`. Текст «ревью вернуло REQUEST_CHANGES»
    в этом случае врал исполнителю — он видел в отчёте `APPROVE`, решал, что карточка от
    прошлого круга, и не правил ничего (правило «0 закрытых» давало owner-стоп). Без
    действующего вердикта (прямой вызов) строка прежняя.
    """
    if report_verdict in ("APPROVE", "REQUEST_CHANGES"):
        return (f"Реестр находок постановки держит {len(findings or [])} открытых находок "
                f"(отчёт: {report_verdict}). Отчёт: `{report_rel}`.")
    return f"Ревью постановки вернуло REQUEST_CHANGES. Отчёт: `{report_rel}`."


def _prompt_spec_section(spec: list[str], limit: int, round_no: int) -> list[str]:
    """Блок «спека не готова» для карточки правок high-шага (8-S2B).

    Спека — часть роли постановщика (D7): здесь названы недостатки формы (C7) и что в них
    дописать, чтобы исполнитель правил постановку, а не выяснял гейт заново.
    """
    round_text = f"круг {round_no} из {limit}" if limit else f"круг {round_no}"
    return [
        "",
        f"СПЕКА НЕ ГОТОВА ({round_text}) — у шага пометка «Риск: high» "
        f"и режим `risk_gate: spec`:",
        *[f"- {item}" for item in spec],
        "Дописать в блок «Спека» (шаблон — `.pipeline/task-prompt.md`):",
        "- «Замысел» — что появляется в репозитории и что мешает это получить сейчас;",
        "- «Границы» — что входит и что не входит в шаг;",
        "- «Критерии готовности» — проверяемые пункты «команда → значение»;",
        "- «Риски» — что может сломаться и на что смотреть в диффе.",
        "- Тело блока — не меньше 600 символов; подразделы ищутся по смыслу названия.",
        "- Без блока «Спека» гейт не пройдёт, а шаг не начнётся: команда гейта — в списке",
        "  «ЧТО ДЕЛАТЬ» выше, она спрашивает спеку (`--risk high`).",
    ]


def prompt_fix_card_body(p: Paths, step: dict, round_no: int, report_rel: str,
                         findings: list[str] | None = None,
                         max_rounds: int = 0, report_verdict: str = "",
                         spec: list[str] | None = None) -> str:
    """Тело карточки «переписать постановку»: закрыть находки ревью, код не трогать.

    *findings* — открытые ID реестра находок постановки (шаг 12-S2): реестр говорит, что
    осталось, отчёт — что не так, поэтому блокирующие пункты отчёта не заменяются, а
    дополняются списком реестра. *max_rounds* — предел кругов правок проекта: по нему
    исполнитель видит, сколько попыток осталось («круг N из M»), а не «круг N» в каждой
    карточке. Умолчание `0` сохраняет прежний вид строки. *report_verdict* — вердикт отчёта
    ревью: по нему первая строка карточки говорит правду и при отчёте `APPROVE`, где
    постановку держит открытая находка реестра. *spec* — пункты C7 «спека не готова»
    (8-S2B): с ними круг открыт риск-гейтом, а не ревью, поэтому вместо находок ревью
    карточка несёт раздел «СПЕКА НЕ ГОТОВА» и что дописать в блок «Спека».
    """
    sid = step["id"]
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    prompt_path = prompt_rel(p, sid) or ""
    head = head_commit(p) or "—"
    recon = prompt_recon(Path(p.repo) / prompt_path) if prompt_path else None
    limit = f" из {max_rounds}" if max_rounds else ""
    spec_items = [str(item) for item in (spec or []) if str(item)]
    # Гейт круга правок у high-шага проверяет и спеку (F6 ревью 8-S2B): флаг привязан к
    # пометке шага и режиму, а не к непустому `spec` — обычный круг правок и пересборка
    # пишут постановку с нуля так же, и «PASS 8/8» без флага спеку не спрашивает.
    # Вторая такая же строка в карточке не печатается: команда одна.
    gate_cmd = _prompt_gate_cmd(p, step, prompt_path)
    head_line = (f"ПОСТАНОВКУ ШАГА {sid} плана {plan_id} ПЕРЕПИСАТЬ — {step['title']} "
                 f"(круг {round_no}{limit})")
    if spec_items:
        head_line += " — спека high-шага"
    lines = [
        head_line,
        "",
        (f"Спека high-шага не готова (замечаний: {len(spec_items)}). "
         f"Отчёт ревью постановки, если он есть: `{report_rel}`."
         if spec_items else _prompt_fix_verdict_line(findings, report_verdict, report_rel)),
        f"Постановка: `{prompt_path}` (recon_commit `{recon or '—'}`, текущий HEAD `{head}`).",
        f"Контракт шага — `{plan_rel}` §{sid}.",
        "",
        "ЧТО ДЕЛАТЬ",
    ]
    if spec_items:
        # Круг открыт формой постановки, а не отчётом ревью (правки по ревью 8-S2B, круг 2):
        # первое действие — недостающий блок «Спека», а отчёт «прочитать целиком» уводил бы
        # исполнителя в отчёт, которого может не быть. Раздел «СПЕКА НЕ ГОТОВА» печатается
        # сразу за списком, до «ЗАПРЕТОВ»: что дописать, читают прежде, чем чего нельзя.
        lines += [
            "- Дописать в постановку блок «Спека» — по разделу «СПЕКА НЕ ГОТОВА» ниже.",
            "- Готовые разделы постановки не переписывать: правка — спека, а не новый документ.",
            "- Сверить факты с текущим HEAD: символы и команды поиска, числа тестов, пути.",
            f"- Обновить в шапке `recon_commit:` на текущий HEAD (`{head}`).",
            f"- Прогнать `{gate_cmd}` → PASS 8/8.",
            "- Коммит и пуш ТОЛЬКО файла постановки, сообщение на русском.",
            *_prompt_spec_section(spec_items, max_rounds or 0, round_no),
        ]
    else:
        lines += [
            "- Прочитать отчёт целиком и закрыть КАЖДУЮ блокирующую находку правкой постановки.",
            "- Значимые находки — по существу, а не косметикой; «уточнить» и «при необходимости» не годятся.",
            "- Сверить факты с текущим HEAD: символы и команды поиска, числа тестов, пути.",
            "- Решения D-блока не пересматривать; если находка требует иного — назвать цену.",
            f"- Обновить в шапке `recon_commit:` на текущий HEAD (`{head}`).",
            f"- Прогнать `{gate_cmd}` → PASS 8/8.",
            "- Коммит и пуш ТОЛЬКО файла постановки, сообщение на русском.",
        ]
    lines += [
        "",
        "СВИП КЛАССА — почему правка прошлого круга рождала находку следующего",
        "- Закрывать находку не в одной строке, а во всём классе: назвать класс дефекта",
        "  (правило, приём теста, изоляция окружения, ожидаемая форма значения) и найти",
        "  `search_files` все места постановки, где он встречается; править одним проходом.",
        "- Вводишь правило в одном тесте — применять его к каждому тесту, где оно нужно,",
        "  и перечислить эти тесты; оставить одно место из списка нельзя.",
        "- После правки пройти документ целиком, а не диффом: числа тестов, адреса-символы,",
        "  пары «вход → исход», ожидаемые типы в соседних разделах не должны разойтись.",
        "- В отчёте карточки: находка → класс → где ещё нашёл → что стало. Место, оставленное",
        "  как есть, объяснить одной фразой — почему правило там не нужно.",
        "",
        "ЗАПРЕТЫ",
        "- Не менять код, тесты, план и журнал; «ЗАКРЫТ» не ставить.",
        "- Не расширять объём шага и не смягчать гейты.",
        "",
    ]
    if spec_items:
        return "\n".join(lines + ["ЗАВЕРШЕНИЕ",
                                  "kanban_complete: что дописано в спеку (по замечаниям), "
                                  "вывод проверки постановки, sha коммита.",
                                  "Следующий круг ревью постановки ставит страж — "
                                  "ревью сам не заказывай."])
    blocking = _blocking_findings(Path(p.repo) / report_rel, FINDINGS_LIMIT)
    if blocking:
        lines.append("БЛОКИРУЮЩИЕ НАХОДКИ — закрыть полностью, ответить на каждую:")
        lines += [f"{num}. {finding}" for num, finding in enumerate(blocking, 1)]
    else:
        others = _review_findings(Path(p.repo) / report_rel, 5)
        lines.append("Блокирующих пометок в отчёте нет — замечания ревьюера:")
        lines += [f"{num}. {finding}" for num, finding in enumerate(others, 1)] or ["- (отчёт пуст)"]
    registry = list(findings or [])
    if registry:
        # Реестр — источник вердикта (12-S2): без его открытых ID правка закроет только то,
        # что назвал отчёт, и реестр останется открытым на новый круг.
        lines += ["", "ОТКРЫТЫЕ НАХОДКИ РЕЕСТРА — закрыть полностью, ответить на каждую:",
                  *[f"- {item}" for item in registry]]
    lines += [
        "",
        "ЗАВЕРШЕНИЕ",
        "kanban_complete: какие находки закрыты (по номерам), вывод проверки постановки, sha коммита.",
        "Следующий круг ревью постановки ставит страж — ревью сам не заказывай.",
    ]
    return "\n".join(lines)


def _short_hash(value: str | None) -> str:
    """Первые 8 знаков sha256 для карточки; литерал `absent` остаётся словом."""
    text = str(value or "")
    if not text or text == INPUT_ABSENT:
        return INPUT_ABSENT
    return text[:8]


def prompt_stale_card_body(p: Paths, step: dict, decision: dict, report_rel: str) -> str:
    """Тело карточки «пересобрать протухшую постановку»: почему протухла и что делать.

    Всё печатаемое берётся из решения и `Paths`: в состояние функция не смотрит, поэтому её
    можно звать в тесте руками. Отпечаток показывается агрегатом (12 знаков) плюс пофайловым
    перечнем: `sha256` целиком человеку и агенту ничего не даёт, а перечень называет, что
    именно изменилось.
    """
    sid = step["id"]
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    prompt_path = prompt_rel(p, sid) or ""
    head = head_commit(p) or "—"
    rebuild = int(decision.get("rebuild") or 1)
    limit = int(decision.get("limit") or 0)
    fp = str(decision.get("fp") or "")
    changed = decision.get("changed") or {}
    lines = [
        f"ПЕРЕСОБРАТЬ ПРОТУХШУЮ ПОСТАНОВКУ ШАГА {sid} плана {plan_id} — "
        f"{step['title']} (пересборка {rebuild} из {limit})",
        "",
        f"Постановка: `{prompt_path}`. Контракт шага — `{plan_rel}` §{sid}.",
        f"Отчёт прошлого ревью: `{report_rel}`.",
        "",
        "ПОЧЕМУ ПРОТУХЛА",
        f"- Прошлый recon_commit: `{decision.get('prev_recon') or '—'}`; текущий HEAD: `{head}`.",
        f"- Причина: {decision.get('reason') or 'расхождение версий'}",
    ]
    if fp:
        lines.append(f"- Отпечаток входа (первые 12 знаков): `{fp[:12]}`")
    if changed:
        lines.append("- Изменившиеся входы:")
        for rel in sorted(changed):
            was, now = changed[rel]
            lines.append(f"  - `{rel}`: было {_short_hash(was)}, стало {_short_hash(now)}")
    elif decision.get("stale_reason") == "stale-recon":
        lines.append("- Вход не менялся: причина — отметка версии.")
    else:
        lines.append("- Опоры нет: признак по отметке.")
    prev = _resolve_commit(Path(p.repo), decision.get("prev_recon"))
    commits: list[str] = []
    if prev:
        try:
            res = _run(["git", "log", "--oneline", f"{prev}..HEAD"], Path(p.repo), timeout=60)
        except (OSError, subprocess.SubprocessError):
            res = None
        if res is not None and res.returncode == 0:
            commits = [line for line in (res.stdout or "").splitlines() if line.strip()]
    if commits:
        lines.append("- Коммиты между версиями (не больше 20):")
        lines += [f"  - {line}" for line in commits[:20]]
        if len(commits) > 20:
            lines.append(f"  - … и ещё {len(commits) - 20}")
    else:
        lines.append("- Отметка не разрешается в коммит: список коммитов не собрать.")
    lines += [
        "",
        "ЧТО ДЕЛАТЬ",
        "- Пересобрать постановку от текущего HEAD: символы и команды поиска, числа тестов и",
        "  гейты должны совпадать с кодом на этом коммите.",
        f"- Обновить в шапке `recon_commit:` на текущий HEAD (`{head}`).",
        "- Принятые находки прошлого ревью НЕ переоткрывать: закрытое остаётся закрытым,",
        "  пересборка возвращает постановку к факту, а не начинает ревью заново.",
        *(["- У шага «Риск: high» и режим `risk_gate: spec`: постановка обязана нести блок",
           "  «Спека» (четыре подраздела, не меньше 600 символов) — пересборка пишет его заново."]
          if _risk_spec_required(p, step) else []),
        f"- Прогнать `{_prompt_gate_cmd(p, step, prompt_path)}` → `ИТОГ: PASS`.",
        "",
        "ЗАПРЕТЫ",
        "- Не менять код, тесты, план и журнал; «ЗАКРЫТ» не ставить и версию не поднимать.",
        "- Не расширять объём шага и не смягчать гейты.",
        "- Ревью постановки заказывает страж: сам его не заказывай.",
        f"- После пересборки ревью пойдёт кругом {int(decision.get('review_round') or 2)}.",
        "",
        "ЗАВЕРШЕНИЕ",
        "kanban_complete: путь постановки, что пересобрано, новый recon_commit, вывод гейта.",
    ]
    return "\n".join(lines)


# ───────────── форма ответа исполнителя по номерам реестра шага (13-S4) ─────────────

#: Три метки ответа — дословно. Второй копии этих строк в коде нет: карточка, роль и
#: проверка берут их отсюда.
FIX_ANSWER_FORMS = ("исправлено", "не находка", "вне объёма шага")

#: Командный токен доказательства: план требует команду и её итог, а не пересказ.
ANSWER_COMMAND_TOKENS = ("pytest", "python", "git", "grep", "ruff", "hermes")

#: Маркер списка в начале строки ответа (`-`, `*`, `+`, `1.`, `1)`).
_ANSWER_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

#: Разделитель частей ответа: длинное тире, короткое тире и дефис с пробелами.
_ANSWER_PART_RE = re.compile(r"\s+[—–-]\s+")

#: Упоминание реестра плана — замена командному токену для формы «вне объёма шага».
_PLAN_REGISTRY_RE = re.compile(r"plan-\d+-findings\.md")


def _answer_line_ok(fid: str, line: str) -> bool:
    """Отвечает ли строка за номер `fid` в одной из трёх форм (13-S4, §4.2 правила 2–5)."""
    match = re.match(rf"^{re.escape(fid)}\s*:\s*(.*)$", line)
    if not match:
        return False
    rest = match.group(1).strip()
    label = next((form for form in FIX_ANSWER_FORMS if rest.startswith(form)), "")
    if not label:
        return False
    tail = rest[len(label):].strip()
    parts = [part.strip() for part in _ANSWER_PART_RE.split(tail) if part.strip()]
    if len(parts) < (1 if label == FIX_ANSWER_FORMS[2] else 2):
        return False
    lowered = rest.lower()
    if any(token in lowered for token in ANSWER_COMMAND_TOKENS):
        return True
    return label == FIX_ANSWER_FORMS[2] and bool(_PLAN_REGISTRY_RE.search(rest)
                                                 or "реестр" in lowered)


def fix_answer_problems(open_ids: list[str], text: str) -> list[str]:
    """ID строк реестра шага, по которым ответа формы нет; порядок — как в `open_ids`.

    Чистая функция, как `step_key`: без путей, состояния и subprocess. Один номер может
    быть отвечен несколькими строками — годится любая, где правила формы выполнены;
    повтор номера не ошибка. Пустой список — ответ по форме есть у каждого номера.
    """
    lines = [_ANSWER_BULLET_RE.sub("", raw) for raw in str(text or "").splitlines()]
    return [fid for fid in open_ids
            if fid and not any(_answer_line_ok(fid, line) for line in lines)]


def fix_answer_text(card: dict) -> str:
    """Текст ответа исполнителя: `result` плюс `summary`, склеенные переводом строки.

    Оба поля обязательны: `kanban_complete(summary=…)` не заполняет `tasks.result`, а
    закрытие через `result=` не заполняет `task_runs.summary` (§4.3). Проверка формы по
    одному полю возвращала бы карточку живому исполнителю за то, что он ответил «не туда».
    """
    return "\n".join(str(card.get(key) or "") for key in ("result", "summary")).strip()


def fix_return_cards(cards: list[dict], round_no: int) -> list[dict]:
    """Карточки возврата правок круга: ключи с суффиксом `:fix:<круг>:return`.

    Чистая функция: в `fixes`/`attempt` такие карточки не попадают (`card_of` берёт только
    `:fix` и `:fix:<N>`), поэтому номера кругов возврат не сдвигает.
    """
    suffix = f":fix:{round_no}:return"
    return [card for card in cards if str(card.get("key") or "").endswith(suffix)]


def fix_answer_block(p: Paths, step: dict, open_lines: list[str] | None = None) -> list[str]:
    """Блок формы ответа для карточки правок (13-S4, §4.1).

    Печатается тем же условием, что блок реестра: открытых строк нет — пустой список и
    тело карточки дословно прежнее. Метки трёх форм берутся из `FIX_ANSWER_FORMS`.
    """
    lines = open_step_findings_lines(p, step) if open_lines is None else open_lines
    if not lines:
        return []
    return [
        "ФОРМА ОТВЕТА ПО КАЖДОМУ НОМЕРУ РЕЕСТРА (обязательно)",
        f"- Ответить на КАЖДЫЙ открытый номер: сейчас их {len(lines)} (`F1…`); одна строка "
        "на номер, ровно одна из трёх форм:",
        f"  `F1: {FIX_ANSWER_FORMS[0]} — <коммит/файл> — <команда и итог>`",
        f"  `F1: {FIX_ANSWER_FORMS[1]} — <почему, с доказательством> — <команда>`",
        f"  `F1: {FIX_ANSWER_FORMS[2]} — в реестр плана <строка>`",
        "- Форма по номеру и есть его строка реестра шага: ID берётся из реестра, а не",
        "  из абзаца отчёта ревьюера.",
        "- Ответ писать в `kanban_complete` (`result` и `summary`), карточку закрывать только",
        "  с ответом: по нему судит страж, иначе ответа не увидит никто.",
        "- «Не находка» — законный ответ, но только с доказательством: команда и её вывод;",
        "  спор без доказательства остаётся находкой.",
        "- Ответ без формы — незакрытый пункт: новый круг не заказывается, карточка",
        "  возвращается тем же кругом один раз.",
        "",
    ]


def fix_return_problem_lines(p: Paths, step: dict, problems: list[str]) -> list[str]:
    """Строки реестра шага только по пунктам без формы — повод возврата (13-S4, §4.5)."""
    wanted = {str(item) for item in problems}
    rows = [line for line in open_step_findings_lines(p, step)
            if line.startswith("- ") and line[2:].split(" ", 1)[0].rstrip(":") in wanted]
    return rows or [f"- {item}: строка реестра шага не найдена — сверься с реестром"
                    for item in problems]


def fix_return_card_body(p: Paths, step: dict, round_no: int, problems: list[str],
                         report_rel: str) -> str:
    """Карточка возврата правок: ответ не по форме, круг тот же (13-S4, §4.5)."""
    plan_rel = p.cfg.rel(Path(p.plan_file))
    ids = ", ".join(problems)
    return "\n".join([
        f"ВОЗВРАТ ПРАВОК шага {step['id']} плана {p.plan_id} — {step['title']} "
        f"(круг {round_no})",
        "",
        f"Ответ карточки правок не по форме: пункт {ids} без формы.",
        f"Реестр находок шага: `{step_findings_rel(p, step['id'])}`; "
        f"отчёт ревью: `{report_rel}`.",
        f"Контракт шага — `{plan_rel}` §{step['id']}, критерий: {step.get('dod', '')}",
        "",
        "ПУНКТЫ БЕЗ ФОРМЫ",
        *fix_return_problem_lines(p, step, problems),
        "",
        "ФОРМА ОТВЕТА (одна строка на номер, метка дословно)",
        f"- `F1: {FIX_ANSWER_FORMS[0]} — <коммит/файл> — <команда и итог>`",
        f"- `F1: {FIX_ANSWER_FORMS[1]} — <почему, с доказательством> — <команда>`",
        f"- `F1: {FIX_ANSWER_FORMS[2]} — в реестр плана <строка>`",
        "- «Не находка» — законный ответ, но только с доказательством: команда и её вывод.",
        "",
        "ЭТО НЕ НОВЫЙ КРУГ",
        f"- Круг {round_no} тот же: правки круга не переделывать, повторный коммит не нужен,",
        "  если он уже был.",
        "- Ответ на каждый пункт — в `kanban_complete` (`result` и `summary`): по этому",
        "  тексту страж проверяет форму.",
        "- Второй неформальный ответ уходит на ревью: возврат на круг один.",
        "",
        "ГЕЙТЫ КРУГА",
        *[f"  - {gate}" for gate in step.get("gates") or []],
    ])


def fix_return_comment_body(p: Paths, step: dict, round_no: int, problems: list[str],
                            fix_card_id: str | None) -> str:
    """Комментарий в карточку правок: какие пункты вернулись без формы (13-S4, §4.5)."""
    return "\n".join([
        f"ВОЗВРАТ ПРАВОК: шаг {step['id']} плана {p.plan_id} — {step['title']} "
        f"(круг {round_no})",
        f"Карточка правок: {fix_card_id or '—'}",
        f"Реестр шага: {step_findings_rel(p, step['id'])}",
        "",
        "Пункты без формы — ответить по номеру одной строкой:",
        *fix_return_problem_lines(p, step, problems),
        "Формы: " + "; ".join(f"`{form}`" for form in FIX_ANSWER_FORMS)
        + "; ответ — в `kanban_complete` (`result` и `summary`).",
    ])[:COMMENT_MAX_LEN]


def fix_card_body(p: Paths, step: dict, round_no: int, report_rel: str,
                  max_rounds: int = 0, report_verdict: str = "") -> str:
    """Карточка правок шага. `max_rounds` — предел кругов по риску шага (`step_fix_limit`).

    Умолчание `0` сохраняет прежний вид карточки: без числа кругов «(круг N)» и прежняя
    строка про последний круг. Когда предел известен, шапка печатает «(круг N из M)», а
    фраза «это последний автоматический круг» остаётся только на последнем круге — на
    `high`-шаге прежний текст врал бы про единственный круг.

    С шага 13-S2 карточка несёт блок открытых находок реестра шага (только их закрывает
    круг) и говорит правду о поводе круга (`_step_fix_verdict_line`): реестр держит шаг и
    при `APPROVE` в отчёте. Нет реестра или открытых строк — тело дословно прежнее.

    С шага 13-S4 к блоку реестра добавлен блок формы ответа (`fix_answer_block`): ответ на
    каждый открытый номер одной из трёх форм, иначе карточка вернётся тем же кругом.
    """
    plan_rel = p.cfg.rel(Path(p.plan_file))
    round_label = f"(круг {round_no} из {max_rounds})" if max_rounds else f"(круг {round_no})"
    if max_rounds and round_no >= max_rounds:
        tail = [
            "ВАЖНО: это последний автоматический круг правок. Следующий REQUEST_CHANGES",
            "останавливает конвейер и уходит владельцу — доведи находки до конца.",
        ]
    elif max_rounds:
        tail = [
            f"Осталось кругов: {max(0, max_rounds - round_no)} из {max_rounds}. "
            "Исчерпание лимита — стоп владельцу:",
            "следующий отказ после последнего круга останавливает конвейер.",
        ]
    else:
        tail = [
            "ВАЖНО: это последний автоматический круг правок. Следующий REQUEST_CHANGES",
            "останавливает конвейер и уходит владельцу — доведи находки до конца.",
        ]
    registry = step_findings_block(p, step)
    answer = fix_answer_block(p, step)
    # Разбор классов отчёта (13-S3): разбор делает S1, второй не пишем — карточка печатает счёт.
    classes = report_finding_classes_at(Path(p.repo) / report_rel)
    classes_line = (f"Классы находок отчёта: блокирующих {classes['blocking']}, "
                    f"значимых {classes['significant']}, стилистических {classes['style']}")
    if classes["sublabels"]:
        classes_line += "; подметки: " + ", ".join(
            f"{name} ×{count}" for name, count in sorted(classes["sublabels"].items()))
    return "\n".join([
        f"ПРАВКИ ПО РЕВЬЮ шага {step['id']} плана {p.plan_id} — {step['title']} {round_label}",
        "",
        _step_fix_verdict_line(open_step_findings_lines(p, step), report_verdict, report_rel),
        f"Контракт шага — `{plan_rel}` §{step['id']}, критерий: {step.get('dod', '')}",
        classes_line,
        "",
        *registry,
        *answer,
        "ЧТО ДЕЛАТЬ",
        "- Исправить БЛОКИРУЮЩИЕ находки полностью; значимые — по существу (не косметикой).",
        "- Находка — это класс: проверить соседние вызовы, ветки и тесты на тот же дефект",
        "  и починить их сразу; в отчёте перечислить места, а не только строку из отчёта.",
        "- Ничего не расширять за пределы шага; тесты не ослаблять и не удалять.",
        "- Гейты шага перегнать и показать фактические цифры:",
        *[f"  - {g}" for g in step.get("gates") or []],
        "- Прогоны: точечно по затронутым тестам, полный прогон — один раз перед коммитом",
        "  (на крупном проекте это десятки минут: круг правок не должен ждать его дважды).",
        "- Точечный прогон — `pytest -m \"not slow\"` по затронутым тестам: медленные тесты",
        "  помечены `slow` и в него не входят; полный (`python -m pytest`) — один раз перед коммитом.",
        "- `-q` не дублировать, если он уже в `pytest.ini` проекта: второй скрывает итог прогона.",
        "ДЕТЕРМИНИРОВАННОСТЬ: мигающий тест — дефект теста, а не машины",
        "- Затронутые тесты прогнать ТРИ раза подряд, три итога — в отчёт.",
        "- Расхождение прогонов — находка, а не повод повторить.",
        "- Повторяемость доказывается фиксацией времени, порядка и окружения (`monkeypatch`, `os.replace`).",
        "- Цикл повторов до успеха запрещён: `pytest.fail` в retry-цикле, `for attempt in range(N)`, `--reruns`.",
        "- Находка «тест недетерминирован» — блокирующая с подметкой `[блокирующая/тест-недетерминирован]`.",
        "- Три прогона — только затронутые тесты; полный набор — один раз перед коммитом.",
        "",
        "ЗАВЕРШЕНИЕ",
        "1. Коммит и пуш ТОЛЬКО файлов правок (`git add <файлы>`, НЕ `-A`), сообщение на русском.",
        "2. kanban_complete: какие находки закрыты (по номерам), прогоны с цифрами, sha коммита.",
        "   Журнал плана не трогать. Следующий круг ревью ставит страж.",
        "",
        *tail,
    ])


def runs_fix_card_body(p: Paths, step: dict, round_no: int, proof: dict,
                       max_rounds: int) -> str:
    """Карточка правок по красному прогону (план 9, S2, §4 п.7).

    Находка F03 идёт текстом до S4 (коды в реестре вводит он): карточка обязана назвать
    команду, ожидание, код возврата, секунды и хвост вывода, иначе исполнителю нечего
    чинить — «прогон красный» без вывода это догадка, а не находка. Отчёта ревью ещё нет,
    поэтому реестр находок шага НЕ заводится (`ensure_step_findings` не зовётся: строка
    без отчёта была бы сиротой) и блок классов в тело не входит.

    `max_rounds` — предел кругов из `step_fix_limit`: тело обязано печатать «Осталось
    кругов: N из M», а в поле решения `create_fix` предела нет. Пятый аргумент —
    осознанное отклонение от сигнатуры §4 п.7, и его причина — ровно эта строка.
    """
    round_label = (f"(круг {round_no} из {max_rounds})" if max_rounds
                   else f"(круг {round_no})")
    sha = str(proof.get("sha") or "")
    lines = [
        f"ПРАВКИ ПО ПРОГОНАМ шага {step['id']} плана {p.plan_id} — {step['title']} "
        f"{round_label}",
        "",
        "F03 Прогон красный: страж исполнил пункты «Прогон» шага ДО ревью, и шаг не",
        "     получил карточки ревью — круг ревью не потрачен.",
        f"Доказательство: `{p.cfg.rel(runs_proof_path(p, step['id'], sha))}` "
        f"(sha `{sha or '—'}`)",
        runs_counter(proof),
        "",
        "НАХОДКИ (F03 — метка обязательна: до S4 находка с кодом — это метка в тексте)",
    ]
    red = [row for row in proof.get("runs") or []
           if isinstance(row, dict) and row.get("outcome") != "green"]
    for row in red:
        code = row.get("code")
        where = f"код {code}" if code is not None else "без кода возврата"
        reason = str(row.get("reason") or row.get("outcome") or "").strip()
        lines.append(f"F03 — прогон `{row.get('command')}` красный ({where}): {reason}")
    lines += ["", "КОМАНДЫ И ИТОГИ"]
    for row in proof.get("runs") or []:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        lines.append(f"- {row.get('command')} → {row.get('outcome')} "
                     f"(ожидание `{row.get('expect')} {row.get('value')}`; код "
                     f"{code if code is not None else '—'}; "
                     f"{_seconds_text(float(row.get('seconds') or 0))})")
        tail = str(row.get("tail") or "").strip()
        if tail and row.get("outcome") != "green":
            lines.append("  хвост вывода (конец):")
            lines.append("  ```")
            lines.extend(f"  {line}" for line in tail.splitlines()[-25:])
            lines.append("  ```")
    lines += [
        "",
        "ЧТО ДЕЛАТЬ",
        "- Починить ПРИЧИНУ, а не прогон: прогон объявлен в плане, шаг без него не закроется.",
        "- Пункт «Прогон» в плане править нельзя (его ведёт только владелец): если команда",
        "  неверна — это вопрос владельцу в отчёте, а не правка плана исполнителем.",
        "- Гейты шага перегнать и показать фактические цифры:",
        *[f"  - {g}" for g in step.get("gates") or []],
        "- Тесты не ослаблять и не удалять; расширение объёма шага — находка.",
        "",
        "ВАЖНО",
        "- До S4 «находка с кодом» — это метка F01–F09 в ТЕКСТЕ находки: блокирует",
        "  наличие метки, а не её проверка. Закрытый словарь кодов и отказ на код вне",
        "  словаря появятся в S4 — сейчас словаря нет и метка ни с чем не сверяется.",
        "- Номера в скобках по всему документу — из реестра находок шага и к метке",
        "  карточки не относятся; реестр шага здесь НЕ заводится (отчёта ревью ещё нет).",
        "",
        "ЗАВЕРШЕНИЕ",
        "1. Коммит и пуш ТОЛЬКО файлов правок (`git add <файлы>`, НЕ `-A`), сообщение на русском.",
        "2. kanban_complete: что исправлено, прогоны с цифрами, sha коммита.",
        "   Журнал плана не трогать. Следующий тик снова исполнит прогоны и при зелёном",
        "   результате сам заказывает ревью.",
        "",
        f"Осталось кругов: {max(0, max_rounds - round_no)} из {max_rounds}. "
        "Исчерпание лимита — стоп владельцу.",
    ]
    return "\n".join(lines)


def fix_comment_body(p: Paths, step: dict, round_no: int, report_rel: str,
                     review_card_id: str | None) -> str:
    """Комментарий к карточке правок: находки ревьюера там, где идёт работа (S2).

    С шага 13-S2 первым блоком идут открытые находки реестра шага: круг правок закрывает
    именно их, а выжимка отчёта — вторична (она может цитировать уже закрытое). Нет
    реестра или открытых строк — тело дословно прежнее.
    """
    report = Path(p.repo) / report_rel
    lines = [
        f"ПРАВКИ ПО РЕВЬЮ: шаг {step['id']} плана {p.plan_id} — {step['title']} "
        f"(круг {round_no})",
        f"Отчёт ревью: {report_rel}",
        f"Карточка ревью: {review_card_id or '—'}",
        "",
    ]
    registry = step_findings_block(p, step)
    if registry:
        lines += registry
    blocking = _blocking_findings(report, FINDINGS_LIMIT)
    if blocking:
        lines.append("Блокирующие находки — закрыть полностью, ответить на каждую:")
        lines += [f"{num}. {finding}" for num, finding in enumerate(blocking, 1)]
    else:
        others = _review_findings(report, 3)
        if others:
            lines.append("Блокирующих находок с пометкой в отчёте нет — замечания из отчёта:")
            lines += [f"{num}. {finding}" for num, finding in enumerate(others, 1)]
        else:
            lines.append("Отчёт без пунктов: разбирать целиком.")
    lines += ["",
              "Ответ на находку — комментарий в этой карточке по её номеру; "
              "отчёт ревью не переписывать."]
    return "\n".join(lines)[:COMMENT_MAX_LEN]


# ───────────────────────────── решения ─────────────────────────────

def collector(p: Paths, step: str | None = None) -> dict:
    cmd = [_python(p), str(Path(__file__).resolve().parent / "next_step.py"),
           "--plan", p.plan_id, "--json"]
    if step:
        cmd += ["--step", step]
    res = _run(cmd, Path(p.repo), timeout=120)
    if res.returncode != 0:
        raise SystemExit(f"next_step.py упал: {(res.stderr or res.stdout)[-400:]}")
    return json.loads(res.stdout)


def build_ctx(p: Paths) -> dict:
    state = load_state(p)
    ctx: dict = {"state": state, "stop": load_stop(p), "hold": list(state.get("hold_steps") or []),
                 # Ручные круги правок плана из маркера владельца (S1 плана 6): считаются
                 # рядом со стоп-флагом, потому что живут в том же каталоге состояния.
                 "extra_fix_rounds": load_extra_fix_rounds(p),
                 # Шаги, допуск по которым владелец уже снимал: автоматический риск-hold
                 # по ним не ставится повторно (D4).
                 "released": list(state.get("hold_released") or [])}
    ctx["plan_id"] = p.plan_id
    ctx["review_prefix"] = p.review_prefix
    # Файл плана: причина hold обязана ссылаться на раздел плана, а у `decide` нет `Paths`.
    ctx["plan_file"] = p.cfg.rel(Path(p.plan_file))
    # Режимы плана 2 (S6) — в ctx, чтобы `decide` оставалась чистой функцией.
    ctx["prefetch"] = bool(p.cfg.prefetch_prompt)
    ctx["prompt_review"] = str(p.cfg.prompt_review)
    # Режим риск-гейта (8-S2B): `hold` держит high-шаг ручным допуском, `spec` требует
    # спеку в постановке. В ctx — потому что `decide` видит только его (D4).
    ctx["risk_gate"] = str(getattr(p.cfg, "risk_gate", "spec") or "spec")
    ctx["require_prompt"] = bool(p.cfg.require_prompt)
    # Лимит автоматических кругов правок по ревью: решение чистым остаётся, но правило
    # живёт в конфигурации проекта, а не в коде.
    ctx["max_fix_rounds"] = int(p.cfg.max_fix_rounds)
    ctx["max_fix_rounds_high"] = int(p.cfg.max_fix_rounds_high)
    # Лимит расширения плана правкой (S4): правило из конфигурации проекта, базовая версия —
    # из состояния (`plan_fix_snapshot` при заказе карточки правок). Объём версии плана
    # считается здесь, а не в `decide`: решение стража остаётся функцией состояния и файлов
    # не читает. `approval` здесь ещё модуль версии плана — ниже это имя переопределяется
    # ответом реестра, поэтому объём считает `plan_version_chars`, а не строки этого места.
    ctx["max_plan_growth"] = dict(getattr(p.cfg, "max_plan_growth", None) or {})
    ctx["plan_fix_baseline"] = state.get("plan_fix_baseline") or {}
    ctx["plan_version_chars"] = plan_version_chars(Path(p.plan_file), p.cfg.journal_marker)
    # Утверждение считаем ДО сборщика: при его сбое решение всё равно должно знать,
    # что план не утверждён.
    approval = registry.approval_status(p.cfg, p.spec)
    approval["started"] = registry.plan_started(p.cfg, p.spec)
    ctx["approval"] = approval
    # Снимок расширения плана (7-S4) уходит решению как есть, в том числе при утверждённой
    # версии: переутверждение (в том числе `plan --accept` выросшей версии) не снимает замер,
    # иначе владелец узнавал бы о перерасходе только при устаревшем утверждении, а стоп
    # роста снимается его `--resume` — как и любой owner-стоп (D5, D7).
    try:
        pkg = collector(p)
    except SystemExit as exc:
        ctx.update(next=None, collector_error=str(exc), cards=[], verdict=None,
                   journal_closed=False, prev_closed=None, prev_verdict=None,
                   # 13-S1: без журнала закрытие «с замечаниями» не видно — False
                   # оставляет ветку повторного ревью прежней.
                   prev_closed_remarks=False,
                   # Без сборщика шагов ни постановки, ни N+1 не видно; «свежей» постановку
                   # не считаем, иначе решение пропустило бы обязательное ревью.
                   steps=[], prompt=None, prompt_recon=None, prompt_fresh=False,
                   prompt_report=None, prompt_verdict=None, next_step=None,
                   # 8-S2B: без шагов спеки не видно — пустой список, а не отсутствие
                   # ключа: решение получает только `ctx` и падать не имеет права.
                   prompt_spec=[],
                   # План 12, S2: без шагов реестр постановки не читается, но ключи обязаны
                   # быть — решение получает только `ctx` и на сбое сборщика падать не имеет
                   # права. База правки тоже пустая: сравнивать не с чем.
                   prompt_findings_rel="", prompt_findings=[], prompt_findings_exists=False,
                   prompt_findings_error="", prompt_fix_baseline={},
                   # 13-S2: реестр находок шага — те же четыре ключа, что в рабочей ветке.
                   # На сбое сборщика шага читать нечего, но решение обязано видеть ключи, а
                   # не падать `KeyError` там, где оно ищет открытые строки.
                   step_findings_rel="", step_findings=[], step_findings_exists=False,
                   step_findings_error="",
                   # 9-S2: прогоны на сбое сборщика неисчислимы — пустой ключ, а не
                   # отсутствие: решение читает только `ctx` и падать не имеет права.
                   step_runs=None,
                   # D6: расхождение отметок проверять не на чем — обе сверки `False`,
                   # и решение по ревью постановки остаётся стопом, а не падением.
                   prompt_marker_text_same=False, prompt_marker_same_commit=False,
                   # План 12, S1: без шагов входов постановки не видно — отпечаток и опора
                   # пусты, лимит пересборок берётся из конфигурации, как в рабочей ветке.
                   prompt_input_parts={}, prompt_input_fp="", prompt_input_baseline={},
                   max_stale_rebuilds=int(getattr(p.cfg, "max_stale_rebuilds", 0) or 0),
                   next_prompt=None, next_cards=[],
                   # Та же форма `ctx`, что и в нормальной ветке (D4): решение разведки
                   # не должно падать `NameError` на сбое сборщика.
                   scout_needed=False, scout_cards=[], scout_profile="", scout_note="",
                   # Ревью плана: на сбое сборщика шагов карточки не заказываем — плана
                   # без шагов всё равно не видно, а решение обязано не падать `NameError`.
                   plan_review_format=False, plan_review_ok=True, plan_review_reason="",
                   plan_review_cards=[], plan_review_verdict=None, plan_review_rel="",
                   plan_review_fresh=False, plan_started_journal=False,
                   plan_review_profile="", plan_fix_cards=[],
                   # S4: те же ключи, что в нормальной ветке, — иначе решение на сбое
                   # сборщика падало бы `NameError` вместо честного «сравнивать нечего».
                   max_plan_growth={}, plan_version_chars=0, plan_fix_baseline={},
                   # Исход идеи, вопросы и ретроспектива (план 5, S10): те же ключи, что в
                   # нормальной ветке, — решение обязано не падать `NameError` на сбое сборщика.
                   plan_decision="", plan_decision_note="", questions_rel="",
                   questions_open=None, step_questions_rel="", step_questions=[],
                   step_questions_error="", answer_timeout=0,
                   # План 10, S2A: на сбое сборщика шагов судить о висящих карточках не
                   # по чему, но ключи обязаны быть — решение читает только `ctx`. Порог
                   # берётся разобранным из конфига: второго разбора в `decide` нет.
                   watchdog=dict(getattr(p.cfg, "watchdog", None) or {}),
                   stuck_cards=[], card_deadline_reported=str(state.get("card_deadline_reported") or ""),
                   retro_rel="", retro_missing=False)
        return ctx
    step = pkg.get("next")
    ctx["next"] = step
    ctx["steps_total"] = int(pkg.get("steps_total") or 0)
    ctx["no_steps"] = bool(pkg.get("steps_total") is not None and ctx["steps_total"] == 0)
    ctx["note"] = pkg.get("note") or ""
    ctx["closed"] = [s.upper() for s in (pkg.get("closed") or [])]
    ctx["prev_closed"] = ctx["closed"][-1] if ctx["closed"] else None
    ctx["prev_verdict"] = verdict_of(p, ctx["prev_closed"]) if ctx["prev_closed"] else None
    ctx["verdict"] = verdict_of(p, step["id"]) if step else None
    ctx["journal_closed"] = bool(step) and journal_closed(p, step["id"])
    ctx["cards"] = read_cards(p, f"planloop:{p.plan_id}:{step['id']}:") if step else []
    ctx["prev_cards"] = (read_cards(p, f"planloop:{p.plan_id}:{ctx['prev_closed']}:")
                         if ctx.get("prev_closed") else [])
    # 13-S1: закрытие «с замечаниями» — штатное состояние при вердикте REQUEST_CHANGES.
    # Решение отличает его от расхождения «журнал закрыл, ревью требует правок» по строке
    # журнала: закрытие читается из четвёртой колонки, а не из вердикта отчёта.
    ctx["prev_closed_remarks"] = (journal_closed_with_remarks(p, ctx["prev_closed"])
                                  if ctx.get("prev_closed") else False)
    # Постановка шага, её свежесть и постановка N+1 (правила 1-3 плана 2 S6). Стоимость:
    # один `git diff` на тик и только когда постановка есть.
    ctx["steps"] = list(pkg.get("steps") or [])
    # S7: карточку некуда ставить, если «Исполнитель:» шага — не профиль и не предметка
    # маршрута. Причина лежит в `ctx`, чтобы решение (`decide`) видело её без `Paths`:
    # иначе тик «снимал» бы инцидент и ставил его заново каждые 10 минут.
    ctx["assignee_error"] = assignee_error(p, step or {})
    prompt = prompt_rel(p, step["id"]) if step else None
    recon = prompt_recon(Path(p.repo) / prompt) if prompt else None
    ctx["prompt"] = prompt
    ctx["prompt_recon"] = recon
    ctx["prompt_fresh"] = prompt_fresh(p, recon) if prompt else False
    # Прогоны шага (план 9, S2): доказательство текущего HEAD. `None` — файла нет, и это
    # триггер исполнения в `run_gate` (§4 п.6); чтение файла и `git rev-parse` живут здесь,
    # `decide` получает только `ctx`. Один вызов `head_commit` на тик.
    ctx["step_runs"] = load_runs_proof(p, step["id"], head_commit(p)) if step else None
    # Спека high-шага (8-S2B): считается до решения о ветке — страж пускает шаг по факту
    # полной спеки, а не по наличию файла постановки.
    ctx["prompt_spec"] = prompt_spec_problems(p, step) if step else []
    ctx["prompt_report"] = prompt_review_rel(p, step["id"]) if step else None
    ctx["prompt_verdict"] = prompt_verdict(p, step["id"]) if step else None
    # Реестр находок постановки (план 12, S2): источник действующего вердикта. Читается
    # терпимо, как реестр плана: битый файл уходит в `ctx` строкой, а стоп по нему ставит
    # решение (`_prompt_findings_stop`) — `decide` получает только `ctx` и файлов не читает.
    # Реестром считается файл с непустым текстом: `load_findings` отдаёт `[]` и для
    # отсутствующего, и для пробельного файла, а вердикт по ним был бы выдуман.
    prompt_findings_file = prompt_findings_path(p, step["id"]) if step else None
    ctx["prompt_findings_rel"] = prompt_findings_rel(p, step["id"]) if step else ""
    ctx["prompt_findings"] = []
    ctx["prompt_findings_exists"] = False
    ctx["prompt_findings_error"] = ""
    if prompt_findings_file is not None:
        try:
            # Одно чтение на тик (12-S2, ревью): текст берётся один раз, и `exists`, и строки
            # считаются по нему. Два чтения давали окно, в котором переписанный между ними
            # реестр выглядел как «файл есть, открытых нет» — то есть APPROVE по отчёту
            # REQUEST_CHANGES и «правка закрыла всё» у правила «0 закрытых».
            findings_text = read_findings_text(prompt_findings_file)
            ctx["prompt_findings"] = prompt_findings_status(
                parse_findings(findings_text, prompt_findings_file,
                               PROMPT_FINDINGS_HEADER, PROMPT_FINDINGS_KEYS))
            ctx["prompt_findings_exists"] = bool(findings_text.strip())
        except (GateError, OSError, ValueError) as exc:
            ctx["prompt_findings"] = []
            ctx["prompt_findings_error"] = str(exc)
    # Реестр находок шага (13-S2): память круга правок. Читается тем же образцом — одно
    # чтение на тик, ошибка уходит строкой в `ctx`, а стоп по ней ставит решение
    # (`_decide_base`): `decide` получает только `ctx` и файлов не читает.
    step_findings_file = step_findings_path(p, step["id"]) if step else None
    ctx["step_findings_rel"] = step_findings_rel(p, step["id"]) if step else ""
    ctx["step_findings"] = []
    ctx["step_findings_exists"] = False
    ctx["step_findings_error"] = ""
    if step_findings_file is not None:
        try:
            step_text = read_findings_text(step_findings_file)
            # Словарь классов — свой и только ключевым именем (13-S2, §4.2): позиционный
            # аргумент у этих функций — `TypeError`, а общий `FINDING_KINDS` реестр шага
            # читать не должен (`замысел` — класс-маршрут, а не класс находки).
            ctx["step_findings"] = parse_findings(step_text, step_findings_file,
                                                  STEP_FINDINGS_HEADER, STEP_FINDINGS_KEYS,
                                                  kinds=STEP_FINDING_KINDS)
            ctx["step_findings_exists"] = bool(step_text.strip())
        except (GateError, OSError, ValueError) as exc:
            ctx["step_findings"] = []
            ctx["step_findings_error"] = str(exc)
    # Второй признак устаревания постановки (план 12, S1): пофайловый отпечаток входов и
    # опора, снятая в момент заказа ревью. Только чтение файлов — тик по-прежнему делает
    # один `git diff`, стоимость не растёт.
    ctx["prompt_input_parts"] = prompt_input_parts(p, step) if step else {}
    ctx["prompt_input_fp"] = prompt_input_fp(ctx["prompt_input_parts"])
    saved_input = (state.get("prompt_input") or {}).get(step["id"]) if step else None
    ctx["prompt_input_baseline"] = dict(saved_input) if isinstance(saved_input, dict) else {}
    # База правки постановки (12-S2): какие ID были открыты в реестре на момент заказа
    # круга правок. По ней видно, закрыла ли правка хоть одну находку (правило «0 закрытых»).
    saved_fix = state.get("prompt_fix_baseline")
    ctx["prompt_fix_baseline"] = dict(saved_fix) if isinstance(saved_fix, dict) else {}
    # `getattr`: старый кэш конфига в тестах и в чужом коде не обязан знать новый ключ.
    ctx["max_stale_rebuilds"] = int(getattr(p.cfg, "max_stale_rebuilds", 0) or 0)
    # D6: отметка версии в отчёте и `recon_commit` постановки сверяются не как строки.
    # Ревьюер пишет sha HEAD (иногда короткий) вместо sha постановки — формальное
    # расхождение стоило 8.5 часа простоя (план 29 R1-C, 09.11.2026). Сверка по тексту
    # постановки и по коммиту считается здесь, потому что `decide` получает только `ctx`;
    # git зовётся лишь при реальном расхождении отметок, а не на каждом тике.
    marker = str((ctx["prompt_verdict"] or {}).get("recon") or "")
    mismatch = bool(marker) and marker != (recon or "")
    ctx["prompt_marker_same_commit"] = (mismatch
                                       and prompt_marker_same_commit(p, recon, marker))
    ctx["prompt_marker_text_same"] = bool(mismatch and not ctx["prompt_marker_same_commit"]
                                         and prompt_text_same(p, ctx["prompt"], marker))
    nxt = next_prompt_step(ctx["steps"], step["id"]) if step else None
    ctx["next_step"] = nxt
    ctx["next_prompt"] = prompt_rel(p, nxt["id"]) if nxt else None
    ctx["next_cards"] = (read_cards(p, f"planloop:{p.plan_id}:{nxt['id']}:") if nxt else [])
    # Разведка готовых решений (план 5, S2): признаки плана, карточки роли, профиль и
    # пояснение «готового нет». Всё, что входит в текст причины решения, обязано лежать
    # в `ctx` — `decide` получает только его (D4). Файл плана читается здесь один раз.
    ctx["scout_cards"] = read_cards(p, f"planloop:{p.plan_id}:{SCOUT_KEY}")
    ctx["scout_needed"] = scout_needed(p)
    ctx["scout_profile"] = scout_profile(p.cfg)
    ctx["scout_note"] = scout_note(ctx["scout_cards"])
    # Ревью плана как отдельный шаг (план 5, S5). Дискриминатор формата и признак «план
    # идёт» считаются здесь: у `decide` нет `Paths`, а решение обязано быть чистым (D4).
    # Текст плана читается один раз; файла нет — `can_review("")` даёт `False`.
    ctx["plan_review_format"] = plan_has_constitution(p)
    try:
        plan_text = Path(p.plan_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        plan_text = ""
    ctx["plan_review_ok"], ctx["plan_review_reason"] = plan_cycle.can_review(
        plan_text, p.plan_id, p.cfg.rel(scout_research_dir(p.cfg)))
    # Находки P1-P4 целиком: из них собирается и причина простоя, и тело карточки правки
    # плана по разведке (0.20.0). Проверки чистые, поэтому считаются здесь один раз.
    ctx["plan_checks"] = plan_checks(p, plan_text)
    ctx["plan_take_candidates"] = plan_cycle.take_candidates(plan_text)
    # Раздел-якорь «Разведка» (P1): без него ветка правки плана по разведке не включается —
    # план старого/иного формата остаётся с прежним поведением (0.20.0).
    ctx["plan_has_research"] = plan_cycle.research_section(plan_text) is not None
    # Шаг интеграции, который видит P2: если он уже есть, карточка не велит добавлять
    # второй — состав и порядок шагов правка по разведке не трогает.
    ctx["plan_integration_step"] = plan_cycle.integration_step(plan_text)
    ctx["plan_rel"] = p.cfg.rel(Path(p.plan_file))
    # Исход идеи и вопросы (план 5, S10): текст плана уже прочитан выше, второй раз файл не
    # читается. `decide` получает только `ctx` (D4), поэтому строка «Решение:» разбирается
    # здесь, а не в решении.
    ctx["plan_decision"], ctx["plan_decision_note"] = analyze.plan_decision(plan_text)
    questions = scout_research_dir(p.cfg) / f"{p.plan_id}-questions.md"
    ctx["questions_rel"] = p.cfg.rel(questions)
    ctx["questions_open"] = analyze.open_questions(questions)
    # Вопросы шага (план 8, S5): повод ожидания, который читается из файла вопросов, а не из
    # `hold_steps`. Читается терпимо, как реестр находок: битый файл уходит строкой в `ctx`,
    # а стоп по нему ставит решение — `decide` получает только `ctx` и файлов не читает.
    ctx["step_questions_rel"] = plan_questions_rel(p)
    ctx["step_questions"] = []
    ctx["plan_questions"] = []
    ctx["step_questions_error"] = ""
    ctx["answer_timeout"] = answer_timeout_seconds(p.cfg)
    try:
        # Шага может не быть вовсе (план закрыт) — тогда открытых вопросов не ищем.
        ctx["step_questions"] = step_questions(plan_questions_path(p),
                                               (step or {}).get("id") or "")
        # Все открытые вопросы плана — для вкладки «Шаг»: вопрос второго шага тоже виден.
        ctx["plan_questions"] = step_questions(plan_questions_path(p))
    except (GateError, OSError, ValueError) as exc:
        ctx["step_questions"] = []
        ctx["plan_questions"] = []
        ctx["step_questions_error"] = str(exc)
    # Ретроспектива после закрытия плана (D8): признак «файла ещё нет» — единственное, что
    # решает, писать ли черновик; идемпотентность держится на существовании файла.
    retro = Path(p.cfg.reviews_dir) / f"retro-{p.plan_id}.md"
    ctx["retro_rel"] = p.cfg.rel(retro)
    ctx["retro_missing"] = not retro.is_file()
    # Свежесть вердикта — привязка к sha плана из утверждения (S6). Имя намеренно новое:
    # `plan_review_ok` — про корректировку по разведке, `plan_review_verdict` — разобранный
    # отчёт, `plan_review_fresh` — признак «вердикт про эту версию плана».
    approval_state = ctx["approval"]
    ctx["plan_review_cards"] = read_cards(p, f"planloop:{p.plan_id}:{PLAN_REVIEW_KEY}")
    ctx["plan_review_verdict"] = verdict_at(plan_review_path(p))
    ctx["plan_review_fresh"] = ((approval_state.get("review") or {}).get("state")
                                == PLAN_REVIEW_FRESH)
    # D2: «план идёт» для гейта ревью — закрытые шаги журнала (`ctx["closed"]`), а не файл
    # состояния: состояние пишет сам тик, и признак «идёт» от него отключал бы гейт
    # на втором тике любого ожидания (находка 1 ревью 5-S5).
    ctx["plan_started_journal"] = bool(ctx.get("closed"))
    ctx["plan_review_rel"] = plan_review_rel(p)
    # Реестр находок плана — источник действующего вердикта (S2 плана 7). Путь берётся у S1
    # (`plan_findings_rel`), второго сборщика нет. Файла нет — пусто; битый файл — `GateError`,
    # и он уходит в ctx строкой: `decide` получает только ctx и обязан остаться чистым.
    # Реестром считается файл с непустым текстом: `load_findings` отдаёт `[]` и для пустого,
    # и для пробельного файла, а вердикт по ним был бы `APPROVE` без единого ревью (Р3).
    findings_file = plan_findings_path(p)
    ctx["plan_findings_rel"] = plan_findings_rel(p)
    ctx["plan_findings"] = []
    ctx["plan_findings_exists"] = False
    try:
        ctx["plan_findings"] = load_findings(findings_file)
        ctx["plan_findings_error"] = ""
        ctx["plan_findings_exists"] = bool(findings_file.is_file()
                                           and findings_file.read_text(encoding="utf-8").strip())
    except (GateError, OSError, ValueError) as exc:
        ctx["plan_findings"] = []
        ctx["plan_findings_error"] = str(exc)
    ctx["plan_review_profile"] = plan_review_profile(p.cfg)
    ctx["plan_fix_cards"] = read_cards(p, f"planloop:{p.plan_id}:{PLAN_FIX_KEY}")
    # Правка плана по разведке (0.20.0): артефакт, круги и профиль карточки. Артефакт —
    # тот же, что ждёт разведка (`scout_artifact`): одна точка правды на имя файла.
    ctx["scout_fix_cards"] = read_cards(p, f"planloop:{p.plan_id}:{PLAN_SCOUTFIX_KEY}")
    ctx["scout_artifact_rel"] = p.cfg.rel(scout_artifact(p))
    ctx["scout_artifact_ready"] = scout_artifact(p).is_file()
    ctx["plan_fix_profile"] = plan_fix_profile(p.cfg)
    # Предел ожидания карточки (план 10, S2A): порог приходит из разобранного конфига
    # (`cfg.watchdog`), висящие карточки своего плана считаются здесь один раз за тик —
    # `decide` получает только `ctx` и борду не читает. Чужие планы на той же борде свой
    # страж не останавливают: их остановит страж их плана, а владелец увидит их в `doctor`.
    ctx["watchdog"] = dict(getattr(p.cfg, "watchdog", None) or {})
    ctx["stuck_cards"] = stuck_cards(
        p.kanban_db,
        deadline_hours=int(ctx["watchdog"].get("card_deadline_hours") or 0),
        fresh_minutes=int(ctx["watchdog"].get("heartbeat_fresh_minutes") or 0),
        now=_now(), key_prefix=f"planloop:{p.plan_id}:")["stuck"]
    # Отметка «по этой карточке уже доложено»: owner-стоп снимает владелец, и без
    # отметки тот же стоп ставился бы после каждого `--resume` и каждого тика.
    ctx["card_deadline_reported"] = str(state.get("card_deadline_reported") or "")
    return ctx


def stop_kind(stop: dict | None) -> str:
    if not stop:
        return ""
    return stop.get("kind") or ("owner" if stop.get("source") == "owner" else "incident")


def _card_deadline_stop(ctx: dict) -> dict | None:
    """Владельческий стоп по висящей карточке (план 10, S2A).

    Заменяет молчаливое ожидание: карточка в нетерминальном статусе, дольше предела
    не менявшая последнего события, — это простой, о котором владелец должен узнать
    с командами, а не «в работе: карточка» каждые десять минут.

    Решение чистое: всё нужное пришло в `ctx["stuck_cards"]` из `build_ctx`. Отметка
    `card_deadline_reported` гасит повторный стоп на ту же карточку — стоп
    владельческий, снимается `--resume`, и без отметки он возвращался бы после каждого
    снятия. Другая карточка после снятия даёт новый стоп: гасится только свой id.
    """
    stuck = [item for item in (ctx.get("stuck_cards") or []) if isinstance(item, dict)]
    if not stuck:
        return None
    oldest = stuck[0]
    card_id = str(oldest.get("id") or "")
    if not card_id or card_id == str(ctx.get("card_deadline_reported") or ""):
        return None
    limits = ctx.get("watchdog")
    limit = int((limits or {}).get("card_deadline_hours") or 0) if isinstance(limits, dict) else 0
    age = int(oldest.get("age") or 0)
    status = str(oldest.get("status") or "")
    title = str(oldest.get("title") or "")[:60]
    tail = f" (предел {limit} ч)" if limit else ""
    reason = (f"карточка {card_id} висит {_ago(age)}{tail}: статус {status} — конвейер "
              f"не двигается, нужно решение владельца")
    details = "\n".join([
        f"- карточка {card_id} «{title}», статус {status}, последнее событие "
        f"{_ago(age)} назад;",
        f"- предел ожидания: `watchdog.card_deadline_hours` = {limit or '—'} ч "
        f"(предел смотрителя, `pipeline.json` проекта);",
        "Что сделать (одно из трёх):",
        f"  1) работа сделана — закрыть карточку: "
        f"`hermes kanban --board <борда> complete {card_id} --result \"…\"`;",
        f"  2) работа не пойдёт — заблокировать: "
        f"`hermes kanban --board <борда> block {card_id} \"причина\"`;",
        f"  3) карточка потеряна — снять прогон и выпустить заново: "
        f"`hermes kanban --board <борда> reclaim {card_id}`, затем тик закажет новую;",
        "Поднять предел: `watchdog.card_deadline_hours` в `pipeline.json` проекта "
        "(0 — проверка выключена).",
        "Стоп владельческий: снимается `python .pipeline/gate.py --resume`.",
    ])
    return {"action": "stop", "kind": "owner", "card_deadline": card_id,
            "reason": reason, "details": details}


# Ожидание по законной причине (находка F2 ревью 10-S2A): `idle` с одним из этих флагов —
# не простой, а ожидание ответа, допуска или закрытие плана, и останавливать там нечего.
# Ограда одна на обе ветки, которые перебивают `idle` (висящая карточка и предзаказ
# постановки): два списка дали бы два ответа на один вопрос и разошлись бы при первой
# правке одного из них. `question` — из той же семьи, поэтому он здесь наравне с
# `plan_clarify`: шаг, ждущий ответа владельца, тоже не «висит».
IDLE_WAIT_FLAGS = ("approval_blocked", "incident", "plan_done", "close_blocked",
                   "plan_clarify", "question")


def _legit_idle_wait(base: dict) -> bool:
    """`idle` ждёт по делу (ответа, допуска, закрытия плана), а не простаивает молча."""
    return any(bool(base.get(flag)) for flag in IDLE_WAIT_FLAGS)


def decide(ctx: dict) -> dict:
    """Чистое решение по состоянию: одно действие (или ожидание). Без побочных эффектов.

    Стоп-флаг владельца блокирует всё. Инцидентный флаг (его ставит сам страж) держит
    конвейер, но снимается автоматически, как только причина исчезла.
    """
    stop = ctx.get("stop")
    if stop and stop_kind(stop) == "owner":
        return {"action": "idle", "reason": f"стоп-флаг владельца: {stop.get('reason', '')}"}
    base = _decide_base(ctx)
    if stop:
        if base["action"] == "stop":
            return {"action": "idle", "incident": True,
                    "reason": f"инцидент ждёт устранения: {base.get('reason', '')}"}
        return dict(base, clear_incident=True,
                    reason=f"стоп-причина снята (было: {stop.get('reason', '')}); "
                           f"дальше — {base['action']}")
    # План 10, S2A: висящая карточка вместо тихого ожидания. Стоит после стоп-флага
    # (владельческий стоп не перебивается) и до предзаказа постановки: ждать карточку,
    # которая висит дольше предела, значит стоять молча. Заменяет только `idle` — и только
    # `idle`, который действительно простой: та же ограда, что у предзаказа ниже
    # (`_legit_idle_wait`), иначе стоп подменял решение закрытого плана (`plan_done`),
    # ожидание утверждения (`approval_blocked`) и ожидание ответа (`question`) — F2.
    deadline = _card_deadline_stop(ctx)
    if deadline and base["action"] == "idle" and not _legit_idle_wait(base):
        return deadline
    # Предзаказ постановки N+1 (правило 2): только поверх ожидания и без его блокировок.
    # `plan_clarify` — тоже блокировка (план 5, S10): у плана, остановленного вопросами
    # посередине, тик иначе заказал бы постановку следующего шага — работу, которую
    # решение только что отложило.
    if base["action"] == "idle" and not _legit_idle_wait(base):
        prefetch = _prefetch_decision(ctx)
        if prefetch:
            return prefetch
    return base


def step_fix_limit(ctx: dict, step: dict | None) -> int:
    """Предел автоматических кругов правок шага: ступень по риску шага (план 12, S3).

    Риск читается так же, как в `hold_risk` — пометка `Риск:` шага, иначе `normal`;
    нераспознанная пометка уже сведена `next_step._risk_of` к `high`. Классов ровно три:
    `low` и `normal` получают базу `max_fix_rounds`, `high` — `max_fix_rounds_high`.
    Ступени выше `high` нет: четвёртое значение читалось бы как `high` и лестницу не меняло.

    Бюджет **плана** (`plan_fix_budget`/`PLAN_REVIEW_MAX_ROUNDS`) в лестнице не участвует:
    у плана класса риска нет. Серии `:fix:{N}` и `:promptfix:{N}` — обе круг правок шага и
    обе берут этот предел; `:promptfix-stale:{N}` и `:rereview:{N}` — нет (ANCHOR
    `rounds_classification`).
    """
    risk = str((step or {}).get("risk") or "normal").lower()
    if risk == "high":
        limit = int(ctx.get("max_fix_rounds_high") or DEFAULT_FIX_ROUNDS_HIGH)
    else:
        limit = int(ctx.get("max_fix_rounds") or DEFAULT_FIX_ROUNDS)
    return max(limit, 1)


def _risk_hold_pending(ctx: dict, step: dict) -> bool:
    """Ждёт ли шаг ещё и ручного допуска: `risk_gate: hold`, риск `high`, допуска не было.

    Условие то же, что у ветки `hold_risk` (`ANCHOR:hold_risk_decision`), только без
    побочного эффекта: причина ожидания по вопросу обязана назвать второй повод (S5, S2B).
    """
    if str(step.get("risk") or "normal").lower() != "high":
        return False
    if str(ctx.get("risk_gate") or "spec").strip().lower() != "hold":
        return False
    sid = str(step.get("id") or "").upper()
    held = {str(item).upper() for item in (ctx.get("hold") or [])}
    released = {str(item).upper() for item in (ctx.get("released") or [])}
    return sid not in held and sid not in released


def _question_hold_decision(ctx: dict, step: dict) -> dict | None:
    """Открытый вопрос шага — повод ожидания, а не стоп и не запись в `hold_steps` (S5, п.3).

    Стоит выше всех карточек: пока вопрос открыт, шаг не получает ни заказа постановки,
    ни карточки шага, ни карточки правок, ни ревью. Причина называет номер вопроса, текст,
    рекомендацию и способ ответить — по ней владелец отвечает из чата или из панели.
    """
    error = str(ctx.get("step_questions_error") or "")
    if error:
        return {"action": "stop", "step": step.get("id"),
                "reason": f"файл вопросов шага не читается: {error}"}
    opened = ctx.get("step_questions") or []
    if not opened:
        return None
    item = opened[0]
    sid = step.get("id")
    tail = ""
    if _risk_hold_pending(ctx, step):
        tail = (f"; второй повод ожидания — ручной допуск (риск `high`): "
                f"`python .pipeline/gate.py --unhold {sid}`")
    return {"action": "idle", "step": sid, "question": item["id"],
            "reason": question_hold_reason(sid, item, tail)}


def _classes_stop(sid: str, reason: str, what: str) -> dict:
    """Инцидентный стоп сверки вердикта с классами: причина и три пункта «что сделать».

    Вид стопа — инцидентный (умолчание `run_gate`): причина живёт в отчёте, и как только
    отчёт поправлен, страж снимает стоп сам. Владелец не должен остаться с ручным стопом
    из-за опечатки в отчёте ревьюера.
    """
    return {"action": "stop", "step": sid, "reason": reason, "details": "\n".join([
        "Вердикт сверяется с классами находок отчёта ревью:",
        f"  - {what};",
        "  - либо переписать вердикт под классы: есть блокирующая — REQUEST_CHANGES, "
        "нет — APPROVE;",
        "  - стоп инцидентный: снимется сам после правки отчёта, вручную — "
        "`python .pipeline/gate.py --resume`.",
    ])}


def _step_findings_error_stop(sid: str, error: str) -> dict:
    """Инцидентный стоп «реестр шага не читается» (13-S2, §4.6 п.2).

    Ключа `kind` нет: стоп снимается сам, как только реестр прочитан (как у
    `_classes_stop`). Шаг остаётся открытым — закрывать его по неизвестному реестру
    значило бы простить находку, которой никто не видел.
    """
    return {"action": "stop", "step": sid,
            "reason": (f"реестр находок шага {sid} не читается: {error} — шаг не закрыть "
                       f"по неизвестному реестру")}


def _step_findings_stop(sid: str, rows: list[dict], verdict: str, limit: int) -> dict:
    """Инцидентный стоп «открытые строки реестра шага, бюджет кругов исчерпан» (D6).

    Свой стоп, а не дописанный блок в прежнем стопе по лимиту: тексты того стопа
    закреплены тестами и golden, и одно состояние не должно описываться двумя решениями.
    """
    ids = ", ".join(str(item.get("id")) for item in rows)
    reason = (f"реестр находок шага {sid} держит открытые находки ({ids}) — "
              f"вердикт {verdict} шаг не закрывает; лимит автоматических кругов правок "
              f"исчерпан ({limit})")
    details = [f"- {item.get('id')} ({item.get('kind')}): {item.get('text')} → "
               f"{item.get('action')}" for item in rows]
    details += [
        "Что делать (выбери одно):",
        "  1) закрыть находки правкой и закрыть их строки фактом, запустить тик — "
        "ревью пойдёт заново;",
        "  2) если правка уже сделана, закрыть строки реестра фактом и запустить тик;",
        "  3) если реестр ведётся неверно — переписать отчёт ревью и строки реестра под "
        "фактическое состояние, запустить тик.",
        "Стоп инцидентный: снимется сам, как только открытых строк не останется.",
    ]
    return {"action": "stop", "step": sid, "reason": reason, "details": "\n".join(details)}


def _step_registry_decision(ctx: dict, sid: str, step: dict, verdict: str,
                            fixes: list, parent: str, blocking: int = 0) -> dict | None:
    """Ветка реестра шага внутри ветвей вердикта (13-S2, §4.6 п.4-5).

    Порядок обязателен: открытая строка реестра сильнее вердикта отчёта, поэтому ветка
    стоит после стопов формата S1 (ошибку формата шаг обязан показать, а не сгладить) и
    перед терминальным действием вердикта. `None` — открытых строк нет, и решение идёт
    прежним порядком без единого изменения.

    `blocking` — блокирующих в отчёте. Когда бюджет кругов исчерпан, а отчёт сам держит шаг
    блокирующей находкой, печатается ПРЕЖНИЙ стоп по лимиту: нового в состоянии нет (ID и
    разбор находки владелец видит в отчёте), а тексты того стопа закреплены тестом
    `test_fix_rounds_risk::test_high_step_stops_at_six_and_names_class`, и править их или
    этот тест постановка шага запрещает. Свой стоп реестра нужен там, где без него шаг
    закрылся бы: отчёт без блокирующих (в том числе `APPROVE`) при открытой строке.
    """
    rows = open_step_findings(ctx)
    if not rows:
        return None
    limit = step_fix_limit(ctx, step)
    if len(fixes) >= limit:
        if blocking:
            return None
        return _step_findings_stop(sid, rows, verdict, limit)
    # Причина круга — старший класс открытых находок (§4.2): по нему метрика считает, из-за
    # чего круги открывались. Счёт берётся из строк реестра, а не из вердикта отчёта.
    kinds: dict[str, int] = {}
    for item in rows:
        kind = str(item.get("kind") or "")
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"action": "create_fix", "step": sid, "data": step, "round": len(fixes) + 1,
            "parent": parent, "registry_open": [str(item.get("id")) for item in rows],
            "cause": round_cause_of(kinds)}


def runs_gate_decision(ctx: dict, step: dict, fixes: list[dict],
                       step_card_id: str) -> dict | None:
    """Ветвь прогонов перед заказом ревью (план 9, S2). `None` — идти в ревью.

    Порядок ветвей строгий (§4 п.5): у шага нет пунктов «Прогон» → доказательства нет
    (это триггер исполнения в `run_gate`, а не решение) → исчерпанный бюджет →
    зелёное доказательство → исчерпанный лимит кругов → карточка правок → на том же sha
    круг уже заказан. Помощник читает только `ctx` и `step`: ни файлов, ни подпроцессов,
    ни `Paths`. Стоп-флаг пишет `run_gate` в общей ветке `action == "stop"` с
    `kind="incident"`: у функции исполнения нет права останавливать конвейер, а
    `--resume` инцидент не снимает (D5).
    """
    if not (step or {}).get("runs"):
        return None
    proof = ctx.get("step_runs")
    if not isinstance(proof, dict):
        return None                      # файла нет — триггер фазы исполнения (п.6)
    sid = str(step.get("id") or "")
    total = int(proof.get("total") or 0)
    green = int(proof.get("green") or 0)
    rows = [row for row in proof.get("runs") or [] if isinstance(row, dict)]
    skipped = sum(1 for row in rows if row.get("outcome") == "skipped")
    executed = max(total - skipped, 0)   # исполнено — строки без `skipped` (F1), не зелёные
    budget = proof.get("budget") if isinstance(proof.get("budget"), dict) else {}
    if budget.get("exhausted"):
        return {"action": "stop", "step": sid, "kind": "incident",
                "reason": (f"прогоны шага {sid}: бюджет прогонов исчерпан "
                           f"({executed} из {total} исполнено за "
                           f"{_seconds_text(budget.get('spent') or 0)} при лимите "
                           f"{_seconds_text(budget.get('limit') or 0)})"),
                "details": ("Как снять: поднять `step_runs.budget` в `pipeline.json` и "
                            "закоммитить (sha меняется — доказательство пересчитается "
                            "заново), либо удалить файл доказательства этого sha из "
                            "`state/runs` решением владельца. `--resume` этот стоп не "
                            "снимает.")}
    if total and green >= total:
        return None                      # всё зелёное — ревью заказывается прежним путём
    fix_card = str(proof.get("fix_card") or "")
    if not fix_card:
        limit = step_fix_limit(ctx, step)
        if len(fixes) >= limit:
            red_rows = [row for row in proof.get("runs") or []
                        if isinstance(row, dict) and row.get("outcome") != "green"]
            last = red_rows[-1] if red_rows else {}
            return {"action": "stop", "step": sid, "kind": "incident",
                    "reason": (f"прогон шага {sid} красный — исчерпан лимит "
                               f"автоматических кругов правок ({limit}, класс риска: "
                               f"{step.get('risk') or 'normal'})"),
                    "details": (
                        f"Круг {len(fixes) + 1} не заказан: израсходовано кругов "
                        f"{len(fixes)} при пределе {limit}. Последний красный прогон: "
                        f"`{last.get('command') or '—'}` (код "
                        f"{last.get('code')}). Варианты владельцу: (1) доправить код и "
                        f"закрыть карточку правок — тик пойдёт дальше; (2) поднять "
                        f"`max_fix_rounds` (или `max_fix_rounds_high`) в `pipeline.json` "
                        f"— цена: больше кругов исполнителя без человека; (3) переписать "
                        f"строку «- Прогон» плана, если прогон требует оболочки или "
                        f"чужого интерпретатора (D1, D2) — цена: это зона владельца (D8). "
                        f"Стоп инцидентный: снимется сам, как только прогон позеленеет "
                        f"или предел поднят.")}
        return {"action": "create_fix", "step": sid, "data": step, "runs": True,
                "round": len(fixes) + 1, "parent": step_card_id,
                "cause": "",
                "reason": (f"прогоны шага {sid} красные: {green} из {total} зелёных — "
                           f"карточка правок вместо ревью (круг {len(fixes) + 1})")}
    return {"action": "stop", "step": sid, "kind": "incident",
            "reason": (f"правки шага {sid} по красному прогону уже заказаны карточкой "
                       f"`{fix_card}`, а прогон на этом sha красный: второго круга на "
                       f"том же sha нет"),
            "details": ("Как снять: новый коммит правки (доказательство пересчитается "
                        "на новом sha) или удаление файла доказательства этого sha из "
                        "`state/runs` решением владельца.")}


def _decide_base(ctx: dict) -> dict:
    """Решение без учёта стоп-флага."""
    step = ctx.get("next")
    if not step:
        if ctx.get("collector_error"):
            return {"action": "stop",
                    "reason": f"сборщик шагов не работает: {ctx['collector_error'][:200]}"}
        if ctx.get("no_steps"):
            return {"action": "idle",
                    "reason": f"у плана {ctx.get('plan_id')} не распознан раздел шагов — "
                              f"конвейер его не ведёт"}
        retro = _retro_decision(ctx)
        if retro:
            return retro
        return {"action": "idle", "reason": "план закрыт — следующих шагов нет", "plan_done": True}
    sid = step["id"]
    # Правка плана расширила план сверх лимита (S4): проверяется ДО утверждения — правленый
    # план иначе показал бы «не утверждён», и владелец не узнал бы про перерасход объёма.
    growth = _plan_growth_decision(ctx)
    if growth:
        return growth
    approval = ctx.get("approval") or {}
    # S6: не хватает именно утверждения владельца. Вердикт ревью плана (`block_kind`
    # "review") — предусловие другого рода, и его ветка своя: она заказывает повторное
    # ревью вместо простоя, поэтому здесь такой случай пропускается вниз. Сравнение
    # `is False`, а не `not`: синтетические `ctx` тестов решений ключа `approval` не имеют
    # вовсе, и `None` должен остаться нейтральным.
    if (approval.get("ok") is False and not approval.get("started")
            and approval.get("block_kind") != "review"):
        # Неутверждённый план не заказывает карточку. Решение именно `idle`, а не
        # отдельное действие: иначе пропуск съедал бы единственное действие тика (D5)
        # и вытеснял остальные планы на каждом тике. Идущий план сюда не попадает: у него
        # запись утверждения наследуется (`ensure_approval`), а правку плана карточкой по
        # разведке стережёт отдельная ветка ревью плана (0.20.0).
        return {"action": "idle", "step": sid,
                "reason": approval.get("reason") or "план не утверждён",
                "approval": approval.get("state"), "approval_blocked": True}
    # Разведка — такое же предусловие плана, как утверждение (D6): проверяется сразу после
    # него и до ветки прошлого шага. При уже идущем шаге карточка разведки существует,
    # и проверка бездействует.
    scout = _scout_decision(ctx)
    if scout:
        return scout
    # Исход — из раздела «Разведка» плана (D5, D7): `plan_decision(ctx)` читает строку
    # `Решение:` из ctx, совпадение с артефактом стережёт A3. Стоит до ревью плана:
    # ревью плана, который решено не делать, — работа впустую.
    plan_decision = _plan_decision(ctx)
    if plan_decision:
        return plan_decision
    # Ревью плана — второе предусловие плана, после разведки и до карточек шагов (C9, C10):
    # пока вердикта APPROVE нет, шаг не начинается (D5).
    plan_review = _plan_review_decision(ctx)
    if plan_review:
        return plan_review
    prev, prev_verdict = ctx.get("prev_closed"), ctx.get("prev_verdict")
    # 13-S1: признак расхождения — не сам вердикт, а закрытие НЕ по замечаниям. Шаг,
    # принятый «с замечаниями» (в журнале стоит тот же текст), остаётся закрытым: его
    # замечания живут строками реестра плана, и повторное ревью здесь было бы вторым
    # кругом ради того же отчёта. Ветка по-прежнему стережёт ручное «закрыт» при отчёте
    # «нужны правки».
    if (prev and prev_verdict and prev_verdict["verdict"] == "REQUEST_CHANGES"
            and not ctx.get("prev_closed_remarks")):
        # Журнал закрыл шаг, а ревью говорит «нужны правки»: вперёд идти нельзя.
        # Даём ОДИН автоматический повторный круг ревью.
        prev_cards = ctx.get("prev_cards") or []
        rereviews = [c for c in prev_cards if (c.get("key") or "").endswith(":rereview:1")]
        prev_fix = card_of(prev_cards, "fix")
        if not rereviews:
            if prev_fix and prev_fix[-1]["status"] not in TERMINAL:
                return {"action": "idle",
                        "reason": f"правки шага {prev} в работе: карточка {prev_fix[-1]['id']} "
                                  f"({prev_fix[-1]['status']})"}
            if prev_fix and prev_fix[-1]["status"] != "done":
                detail = (prev_fix[-1].get("error") or prev_fix[-1].get("result") or "")[:300]
                return {"action": "stop", "step": sid,
                        "reason": f"карточка правок {prev_fix[-1]['id']} в статусе "
                                  f"{prev_fix[-1]['status']}: {detail}"}
            prev_steps = card_of(prev_cards, "step")
            parent = (prev_fix[-1]["id"] if prev_fix
                      else (prev_steps[-1]["id"] if prev_steps else None))
            return {"action": "create_rereview", "step": prev, "verdict": prev_verdict,
                    "parent": parent,
                    "reason": f"шаг {prev} закрыт при REQUEST_CHANGES — ставится повторное ревью"}
        if rereviews[-1]["status"] not in TERMINAL:
            return {"action": "idle",
                    "reason": f"повторное ревью шага {prev} идёт: карточка {rereviews[-1]['id']}"}
        return {"action": "stop", "step": sid,
                "reason": f"повторное ревью шага {prev} снова REQUEST_CHANGES "
                          f"({Path(prev_verdict['path']).name}) — нужно решение владельца"}
    if sid.upper() in {str(h).upper() for h in (ctx.get("hold") or [])}:
        return {"action": "idle", "reason": f"шаг {sid} в списке ручного допуска владельца"}
    # Открытый вопрос шага (8-S5): ожидание, а не стоп, и без записи в `hold_steps` —
    # повод живёт в файле вопросов и исчезает вместе с ответом. Стоит выше всех карточек,
    # включая заказ постановки и риск-допуск: пока владелец не ответил, шаг не начинается.
    question = _question_hold_decision(ctx, step)
    if question:
        return question

    cards = ctx.get("cards") or []
    steps = card_of(cards, "step")
    if not steps:
        if ctx.get("assignee_error"):
            # S7: «Исполнитель:» не профиль и не предметка маршрута — карточки нет, стоп.
            # Решение, а не отказ ветки создания: иначе тик снимал бы инцидент и ставил
            # его заново, уведомляя владельца каждые 10 минут.
            return {"action": "stop", "step": sid, "reason": ctx["assignee_error"]}
        # Рискованный шаг в режиме `hold` не получает НИ ОДНОЙ карточки (включая заказ
        # постановки), пока владелец не допустил: проверка стоит внутри «карточки шага ещё
        # нет», иначе уже начатый шаг застрял бы на hold и не дошёл до ревью (D5).
        # В режиме `spec` (умолчание, 8-S2B) hold не ставится: шаг идёт обычной веткой —
        # заказ постановки → проверка спеки → круг правок постановки или карточка шага.
        # Ручной `--hold` держит шаг в обоих режимах: он проверяется выше и режиму не подчинён.
        risk = str(step.get("risk") or "normal").lower()
        risk_gate = str(ctx.get("risk_gate") or "spec").strip().lower()
        held = {str(h).upper() for h in (ctx.get("hold") or [])}
        released = {str(r).upper() for r in (ctx.get("released") or [])}
        # ANCHOR:hold_risk_decision — стабильная ссылка для спеки и ревью
        if (risk == "high" and risk_gate == "hold" and sid.upper() not in held
                and sid.upper() not in released):
            where = ctx.get("plan_file") or f"план {ctx.get('plan_id') or '?'}"
            return {"action": "hold_risk", "step": sid, "data": step, "risk": risk,
                    "reason": (f"шаг {sid} помечен «Риск: high» — нужен допуск владельца; "
                               f"раздел плана: `{where}` §{sid}; снять: "
                               f"`python .pipeline/gate.py --unhold {sid}`")}
        review = _prompt_review_decision(ctx, step)
        if review:
            return review
        return {"action": "create_step", "step": sid, "data": step}
    last_step = steps[-1]
    if last_step["status"] not in TERMINAL:
        return {"action": "idle", "reason": f"шаг {sid} в работе: карточка {last_step['id']} "
                                           f"({last_step['status']})"}
    if last_step["status"] != "done":
        detail = (last_step.get("error") or last_step.get("result") or "")[:300]
        return {"action": "stop", "step": sid,
                "reason": f"карточка шага {last_step['id']} в статусе {last_step['status']}: {detail}"}

    fixes = card_of(cards, "fix")
    attempt = len(fixes) + 1
    this_fix = [c for c in fixes if (c.get("key") or "").endswith(f":fix:{attempt - 1}")]
    reviews = [c for c in card_of(cards, "review")
               if (c.get("key") or "").endswith(f":review:{attempt}")]
    if not reviews:
        if this_fix and this_fix[-1]["status"] not in TERMINAL:
            return {"action": "idle",
                    "reason": f"правки шага {sid} в работе: карточка {this_fix[-1]['id']} "
                              f"({this_fix[-1]['status']})"}
        if this_fix and this_fix[-1]["status"] != "done":
            detail = (this_fix[-1].get("error") or this_fix[-1].get("result") or "")[:300]
            return {"action": "stop", "step": sid,
                    "reason": f"карточка правок {this_fix[-1]['id']} в статусе "
                              f"{this_fix[-1]['status']}: {detail}"}
        # Прогоны шага (план 9, S2): ветвь стоит в обоих возвратах `create_review` — после
        # проверок карточки правок (пока она в работе, нечего гонять) и возврата по форме.
        # Красный прогон отсекает шаг карточкой правок с находкой F03 и не тратит круг
        # ревью; зелёный — заказывает ревью, чья карточка и уведомление несут строку
        # прогона. Смотрит только `ctx` и `step` — `runs_gate_decision`.
        # Возврат карточки правок (13-S4, §4.4): ответ исполнителя не по форме — карточка
        # возврата того же круга. Стоит ПОСЛЕ проверок карточки правок: пока она не закрыта,
        # судить нечего, и пока карточка возврата не закрыта — шаг ждёт ту же работу того же
        # исполнителя (`idle`, а не стоп). Возврат один на круг: он уже был — идём в ревью.
        returns = fix_return_cards(cards, attempt - 1)
        if returns:
            last_return = returns[-1]
            if last_return["status"] not in TERMINAL:
                return {"action": "idle",
                        "reason": f"возврат карточки правок {sid} в работе: "
                                  f"карточка {last_return['id']} "
                                  f"({last_return['status']})"}
            if last_return["status"] != "done":
                detail = (last_return.get("error") or last_return.get("result") or "")[:300]
                return {"action": "stop", "step": sid,
                        "reason": f"карточка возврата правок {last_return['id']} в статусе "
                                  f"{last_return['status']}: {detail}"}
            return runs_gate_decision(ctx, step, fixes, last_step["id"]) or {
                "action": "create_review", "step": sid, "data": step,
                "attempt": attempt, "parent": last_return["id"]}
        # Номера для проверки формы — открытые строки реестра шага, а не пункты отчёта:
        # `decide` остаётся чистой функцией от `ctx`. Нет открытых строк (или реестра) —
        # проверки нет: шаг без реестра обязан закрываться как раньше (13-S2).
        open_ids = [str(item.get("id") or "") for item in open_step_findings(ctx)]
        problems = (fix_answer_problems(open_ids, fix_answer_text(this_fix[-1]))
                    if this_fix else [])
        if problems:
            return {"action": "create_fix_return", "step": sid, "data": step,
                    "round": attempt - 1, "problems": problems,
                    "parent": this_fix[-1]["id"]}
        return runs_gate_decision(ctx, step, fixes, last_step["id"]) or {
            "action": "create_review", "step": sid, "data": step, "attempt": attempt,
            "parent": this_fix[-1]["id"] if this_fix else last_step["id"]}
    last_review = reviews[-1]
    if last_review["status"] not in TERMINAL:
        return {"action": "idle", "reason": f"ревью {sid} идёт: карточка {last_review['id']}"}
    if last_review["status"] != "done":
        detail = (last_review.get("error") or last_review.get("result") or "")[:300]
        return {"action": "stop", "step": sid,
                "reason": f"ревью {sid} упало ({last_review['status']}): {detail}"}

    verdict = ctx.get("verdict")
    if verdict is None:
        prefix = ctx.get("review_prefix") or "?"
        return {"action": "stop", "step": sid,
                "reason": f"ревью {sid} закрыто, но отчёта {prefix}-{sid}.md нет"}
    if verdict["verdict"] in ("NONE", "AMBIGUOUS"):
        what = "строки вердикта нет" if verdict["verdict"] == "NONE" else "строк вердикта несколько"
        return {"action": "stop", "step": sid,
                "reason": f"вердикт {sid} не распознан ({what}): {Path(verdict['path']).name}"}
    # Битый реестр шага — стоп до ветвей вердикта (13-S2, §4.6 п.2): по неизвестному реестру
    # шаг не закрывается ни при каком вердикте. Стоп инцидентный: снимется сам, когда файл
    # прочитается.
    if ctx.get("step_findings_error"):
        return _step_findings_error_stop(sid, str(ctx["step_findings_error"]))
    if verdict["verdict"] == "APPROVE":
        # Обратное расхождение (13-S1): в отчёте есть блокирующая, а вердикт APPROVE.
        # Это видимая ошибка ревьюера, а не прощение: иначе блокирующая находка закрыла бы
        # шаг молча. Отчёт нечитаем — блокирующих ноль, закрытие идёт как раньше.
        classes = report_finding_classes_at(verdict["path"])
        if classes["blocking"] > 0:
            return _classes_stop(
                sid,
                f"вердикт APPROVE расходится с классами: блокирующих находок "
                f"{classes['blocking']} — вердикт обязан совпадать с классами "
                f"(есть блокирующая — REQUEST_CHANGES)",
                "в отчёте есть блокирующая находка, а вердикт APPROVE")
        # Ветка реестра шага (13-S2, §4.6 п.4-5): открытая строка реестра сильнее вердикта
        # отчёта — APPROVE с открытой находкой шаг не закрывает и заказывает круг.
        registry = _step_registry_decision(ctx, sid, step, verdict["verdict"], fixes,
                                           last_review["id"])
        if registry:
            return registry
        nxt = ctx.get("next_step")
        # Правило 1: шаг не считается выполненным, пока нет постановки N+1. Гейт живёт
        # в решении, а не в отказе `apply` (D10): иначе каждый тик давал бы повторный
        # `applied=False` и съедал единственное действие, ничего не заказывая.
        if (ctx.get("prefetch") and ctx.get("require_prompt") and nxt
                and ctx.get("next_prompt") is None):
            reason = close_block_reason(sid, nxt["id"])
            orders = card_of(ctx.get("next_cards") or [], "prompt")
            if orders:
                return {"action": "idle", "step": sid, "reason": reason, "close_blocked": True,
                        "detail": f"карточка заказа {orders[-1]['id']}"}
            return {"action": "create_prompt", "step": nxt["id"], "data": nxt,
                    "after_close": True, "reason": reason}
        return {"action": "close_step", "step": sid, "data": step, "verdict": verdict,
                "journal_closed": ctx.get("journal_closed")}
    # Сверка вердикта с классами находок (13-S1, §4.2). Порядок обязателен: ошибки формата —
    # стопы, а не круги, и проверяются ДО бюджета кругов; бюджет не меняется.
    classes = report_finding_classes_at(verdict["path"])
    if not classes["read"]:
        return _classes_stop(
            sid, "отчёт ревью не читается: вердикт не сверить с классами",
            "отчёт не прочитан (файл удалён, права или кодировка)")
    if not classes["section"]:
        return _classes_stop(
            sid,
            "ошибка формата отчёта: нет раздела «Находки» — находки идут разделом с метками "
            "класса; вердикт не сверить с классами",
            "в отчёте нет раздела «Находки» с пунктами и метками классов")
    if classes["labeled"] == 0:
        return _classes_stop(
            sid,
            "ошибка формата отчёта: ни одной метки класса (блокирующая, значимая, "
            "стилистическая) — вердикт не сверить с классами; поправь отчёт",
            "ни один пункт раздела не начинается с метки класса в квадратных скобках")
    # Ветка реестра шага (13-S2, §4.6 п.4-5): стоит ПОСЛЕ стопов формата — ошибку формата
    # шаг обязан показать, а не сгладить реестром. Открытые строки есть → круг или стоп по
    # исчерпанному бюджету; открытых нет — прежний порядок ниже без единого изменения.
    registry = _step_registry_decision(ctx, sid, step, verdict["verdict"], fixes,
                                       last_review["id"], int(classes["blocking"] or 0))
    if registry:
        return registry
    if classes["blocking"] == 0:
        # Круга нет: замечания без блокирующего класса шаг не держат. Шаг принимается, а
        # замечания не теряются — `run_gate` дописывает их в реестр находок плана (§4.3).
        return {"action": "close_step", "step": sid, "data": step, "verdict": verdict,
                "remarks": classes["significant"] + classes["style"],
                "unlabeled": classes["unlabeled"],
                "journal_closed": ctx.get("journal_closed")}
    max_fix_rounds = step_fix_limit(ctx, step)
    if len(fixes) < max_fix_rounds:
        # Круги правок не бесконечны, но и не один: после первого REQUEST_CHANGES исполнитель
        # получает ещё круг, и только исчерпание лимита останавливает конвейер (правило 4).
        return {"action": "create_fix", "step": sid, "data": step,
                "round": len(fixes) + 1, "parent": last_review["id"],
                "cause": round_cause_of({"блокирующая": classes["blocking"],
                                         "значимая": classes["significant"],
                                         "стилистическая": classes["style"]})}
    findings = _review_findings(Path(verdict["path"]), 5)
    high = str((step or {}).get("risk") or "normal").lower() == "high"
    tier = " (класс риска: high)" if high else ""
    reason = (f"REQUEST_CHANGES по шагу {sid} — исчерпан лимит автоматических кругов правок "
              f"({max_fix_rounds}){tier}")
    details = [f"Замечания ревью ({Path(verdict['path']).name}):"]
    details += [f"  - {f}" for f in findings] or ["  - пунктов в отчёте не нашёл, смотри отчёт целиком"]
    recs = _review_recommendations(Path(verdict["path"]), 3)
    if recs:
        details += ["Рекомендации ревьюера:"] + [f"  - {r}" for r in recs]
    details += [
        "Что делать (выбери одно):",
        "  1) принять как есть, если замечания непринципиальны: gate.py --resume и закрыть шаг;",
        "  2) переписать постановку шага в docs/prompts/ и вернуть шаг в работу;",
        "  3) доправить вручную по замечаниям и запустить тик: ревью пойдёт заново.",
    ]
    if high:
        details += [
            "Лимит `high` — это предел, а не настройка: повышение не предусмотрено —",
            "нужен разбор плана и решение владельца.",
        ]
    details += ["Стоп инцидентный: снимется сам, как только появится вердикт APPROVE."]
    return {"action": "stop", "step": sid, "reason": reason, "details": "\n".join(details)}


def close_block_reason(step_id: str, next_id: str) -> str:
    """Фраза гейта закрытия (правило 1): одна на решение, отказ `apply` и уведомление."""
    return f"шаг {step_id} принят, но закрыть не могу: нет постановки {next_id}"


def _has_no_ready(card: dict) -> bool:
    """В результате/ошибке карточки есть фраза «готового нет» (регистр не важен, D7)."""
    text = f"{card.get('result') or ''}\n{card.get('error') or ''}".lower()
    return SCOUT_NO_READY in text


def scout_note(cards: list[dict]) -> str:
    """Пояснение к карточке шага после разведки без готового решения; иначе пусто (D7)."""
    if not cards or not _has_no_ready(cards[-1]):
        return ""
    return (f"разведка: готового нет — план идёт своим решением "
            f"(карточка {cards[-1].get('id')})")


def _scout_decision(ctx: dict) -> dict | None:
    """Разведка как предусловие плана: карточка, ожидание, валидный исход или стоп (D6, D7).

    Чистое решение: `None` — разведка не нужна либо уже позади, и план идёт обычным
    порядком. Поля `step` в решении нет намеренно: это решение планового уровня, иначе
    инвариант «одно действие на шаг за тик» счёл бы его действием шага (`step_key`).
    """
    if not ctx.get("scout_needed"):
        return None                    # ключа нет — старые тесты решений целы (D4)
    plan_id = ctx.get("plan_id")
    cards = ctx.get("scout_cards") or []
    if not cards:
        profile = ctx.get("scout_profile") or SCOUT_PROFILE_DEFAULT
        return {"action": "create_scout",
                "reason": (f"разведка плана {plan_id} не проводилась — ставится карточка "
                           f"роли {profile}")}
    card = cards[-1]
    status = str(card.get("status") or "")
    if status not in TERMINAL:
        return {"action": "idle", "scout": True,
                "reason": (f"ждёт разведку плана {plan_id}: карточка {card.get('id')} "
                           f"({status})")}
    if status == "done" or _has_no_ready(card):
        return None                    # разведка состоялась — план идёт дальше (C8, C9)
    detail = " ".join(str(card.get("error") or card.get("result") or "").split())[:300]
    return {"action": "stop",
            "reason": (f"разведка плана {plan_id} упала: карточка {card.get('id')} "
                       f"в статусе {status}: {detail}")}


def _plan_decision(ctx: dict) -> dict | None:
    """Исход разведки как предусловие плана: kill закрывает, needs-clarification ждёт.

    Чистое решение планового уровня: строка «Решение:» разобрана в `build_ctx`, совпадение
    с артефактом стережёт анализатор (A3). Поля `step` нет намеренно — как у решения
    разведки: иначе `step_key` счёл бы его действием шага (D5).
    """
    kind = str(ctx.get("plan_decision") or "")
    plan_id = ctx.get("plan_id")
    if kind == "kill":
        note = str(ctx.get("plan_decision_note") or "").strip() or "причина — в разделе «Разведка»"
        return {"action": "idle", "plan_done": True, "plan_killed": True,
                "reason": f"план {plan_id} закрыт исходом kill (разведка): {note}"}
    if kind == "needs-clarification":
        opened = ctx.get("questions_open")
        if opened is None:
            detail = f"файла вопросов нет: {ctx.get('questions_rel')}"
        elif opened[0]:
            detail = f"открыто {opened[0]} из {opened[1]}: {ctx.get('questions_rel')}"
        else:
            detail = f"все ответы получены ({opened[1]}) — переведи план в go"
        return {"action": "idle", "plan_clarify": True,
                "reason": f"план {plan_id} ждёт ответов на вопросы — {detail}"}
    return None


def _retro_decision(ctx: dict) -> dict | None:
    """Черновик ретроспективы: шаги плана закрыты, файла ещё нет (D8)."""
    if str(ctx.get("plan_decision") or "") == "kill":
        return None
    if not ctx.get("retro_missing") or not ctx.get("steps_total") or not ctx.get("closed"):
        return None
    return {"action": "write_retro",
            "reason": f"план {ctx.get('plan_id')} закрыт — кладём черновик ретроспективы "
                      f"{ctx.get('retro_rel')}"}


def _plan_check_details(ctx: dict) -> str:
    """Находки P1-P4 плана для `details` стопа: что именно не так, адресом раздела."""
    rows = [row for row in (ctx.get("plan_checks") or []) if row[0] == "FAIL"]
    head = (f"Находки корректировки плана {ctx.get('plan_id')} по разведке "
            f"({ctx.get('plan_rel') or 'файл плана'}, артефакт "
            f"{ctx.get('scout_artifact_rel') or '—'}):")
    if not rows:
        return head + f"\n- проверки P1-P4 находок не дали: {ctx.get('plan_review_reason') or '—'}"
    return "\n".join([head] + [f"- {code}: {message}" for _level, code, message in rows])


def _scout_fix_decision(ctx: dict) -> dict | None:
    """Правка плана по разведке: карточка, ожидание, стоп или эскалация владельцу (0.20.0).

    Чистое решение планового уровня: `None` — артефакта разведки нет, править нечего
    (план либо ждёт разведку, либо идёт старым порядком). Поля `step` нет намеренно:
    иначе инвариант «одно действие на шаг за тик» (`step_key`) счёл бы его действием шага.

    Бюджет — `PLAN_REVIEW_MAX_ROUNDS` карточек на план: круг считается по числу уже
    созданных карточек, повторный тик второй карточки того же круга не заводит (ключ
    идемпотентности тот же), а после исчерпания решает владелец. Зацикливания нет ни при
    каком ответе карточки: ожидание, стоп или эскалация.
    """
    if not ctx.get("scout_artifact_ready") or not ctx.get("plan_has_research"):
        # Артефакта нет — править нечего (план ждёт разведку). Раздела «Разведка» нет —
        # это находка P1 и прежнее поведение: такой план карточку правки не получает.
        return None
    plan_id = ctx.get("plan_id")
    cards = ctx.get("scout_fix_cards") or []
    if cards:
        card = cards[-1]
        status = str(card.get("status") or "")
        if status not in TERMINAL:
            return {"action": "idle", "plan_review": True, "scout_fix": True,
                    "reason": (f"ждёт правку плана по разведке: карточка "
                               f"{card.get('id')} ({status})")}
        if status != "done":
            detail = " ".join(str(card.get("error") or card.get("result") or "").split())[:300]
            return {"action": "stop",
                    "reason": (f"правка плана {plan_id} по разведке упала: карточка "
                               f"{card.get('id')} в статусе {status}: {detail}")}
        if len(cards) >= PLAN_REVIEW_MAX_ROUNDS:
            return {"action": "stop", "kind": "owner",
                    "reason": (f"правка плана по разведке не сошлась за {len(cards)} "
                               f"кругов: решает владелец"),
                    "details": _plan_check_details(ctx)}
    round_no = len(cards) + 1
    return {"action": "create_scout_fix", "round": round_no,
            "reason": (f"план {plan_id} не прошёл корректировку по разведке: "
                       f"{ctx.get('plan_review_reason') or 'нет раздела «Разведка»'} — "
                       f"карточка правки плана по разведке (круг {round_no})")}


def approval_reset_at(record: dict) -> int:
    """Момент, с которого считается бюджет кругов ревью (epoch секунд; 0 — считать не с чего).

    `review_reset_at` пишет `plan_accept`; записи, сделанные старой версией плагина или
    давно запущенной панелью, этого поля не несут. Для них годится сам `approved_at`:
    владелец утвердил план после вердикта — значит круги прежней версии считать перестали.
    Без такого запасного пути утверждение «срабатывало», а следующий тик сразу ставил
    owner-stop «второй REQUEST_CHANGES» — со стороны это выглядело как «кнопка не работает».
    """
    explicit = int(record.get("review_reset_at") or 0)
    if explicit:
        return explicit
    stamp = str(record.get("approved_at") or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(time.mktime(time.strptime(stamp, fmt)))
        except ValueError:
            continue
    return 0


def plan_fix_budget(ctx: dict) -> int:
    """Бюджет кругов правок плана: автоматические из конфига плюс ручные из маркера.

    `max_fix_rounds` — ключ проекта (конституция п.4), `extra_fix_rounds` — жест владельца,
    прочитанный из `plan_<N>_rounds.json` в каталоге состояния. `max(..., 1)` у конфига:
    отчёт, написанный руками без карточек, не должен превращать первый отказ во второй.
    У маркера `max(..., 0)`: минус кругов не бывает. Одна точка правила на движок — тем же
    числом живут причины решения и тело карточки правок.
    """
    automatic = max(int(ctx.get("max_fix_rounds") or PLAN_REVIEW_MAX_ROUNDS), 1)
    manual = max(int(ctx.get("extra_fix_rounds") or 0), 0)
    return automatic + manual


def effective_plan_verdict(ctx: dict) -> dict | None:
    """Действующий вердикт ревью плана: реестр находок сильнее отчёта (S2 плана 7).

    Отчёта нет — вердикта нет: реестр его не выдумывает (`None`). Реестр переопределяет
    только распознанный вердикт отчёта (`APPROVE`/`REQUEST_CHANGES`): `NONE`/`AMBIGUOUS`
    остаются как есть, иначе нечитаемый отчёт «лечился» бы чистым реестром.

    Порог подробности (S2B плана 10): вердикт держит строка со статусом `открыто` **и**
    держащим классом — конъюнкция, а не пересчёт статусов. Класс берётся из колонки «Класс»
    и той же таблицы, что у реестра постановки (`PROMPT_FINDING_STATUS`): `стиль` и
    `формат-стража` не держат никогда, `замысел`, `двусмысленность` и `гейт-исполнение`
    держат. Строка `стиль` со `открыто` остаётся в реестре и в карточке правок, но круг
    правок не заказывает и `max_fix_rounds` не тратит (план 9: три круга из трёх — про
    формулировку). Неизвестный класс считается держащим: незнание не прощает находку.

    Ключи: `verdict` — действующий, `report` — из отчёта, `open` — блокирующие ID в порядке
    файла (их и называет причина решения), `total` — строк в реестре, `registry` — файл
    реестра есть, `discrepancy` — действующий вердикт расходится с отчётом, `error` —
    реестр не читается.
    """
    report = str((ctx.get("plan_review_verdict") or {}).get("verdict") or "")
    if not report:
        return None
    rows = ctx.get("plan_findings") or []
    open_ids = [str(item.get("id") or "") for item in rows
                if str(item.get("status") or "") == "открыто"
                and PROMPT_FINDING_STATUS.get(str(item.get("kind") or ""), "открыто")
                == "открыто"]
    registry = bool(ctx.get("plan_findings_exists"))
    known = report in ("APPROVE", "REQUEST_CHANGES")
    verdict = ("REQUEST_CHANGES" if open_ids else "APPROVE") if (registry and known) else report
    return {"verdict": verdict, "report": report, "open": open_ids, "total": len(rows),
            "registry": registry, "discrepancy": bool(registry and known and verdict != report),
            "error": str(ctx.get("plan_findings_error") or "")}


def open_findings_lines(ctx: dict) -> list[str]:
    """Открытые находки реестра строками для карточки: ID, шаг, что не так и что сделать."""
    return [f"{item.get('id')} ({item.get('step')}): {item.get('text')} — {item.get('action')}"
            for item in (ctx.get("plan_findings") or [])
            if str(item.get("status") or "") == "открыто"]


def prompt_findings_status(rows: list[dict]) -> list[dict]:
    """Статус находки постановки — по классу; `закрыто` остаётся за ревьюером.

    Симптом: подпись «Статус» ставилась на глаз, и один и тот же класс то держал шаг, то нет
    (9-S2: «стиль» — `не блокирует`, рядом «двусмысленность» с тем же весом — `открыто`).
    Теперь держит шаг класс, а не оценка: `замысел`, `двусмысленность` и `гейт-исполнение`
    (неисполнимый гейт держит шаг так же, как неверная приёмка) — держат; `формат-стража`
    и `стиль` — нет. `закрыто` не переписывается: это утверждение
    о факте правки, а не степень важности, и закрывать может только ревьюер.

    Возвращается копия: те же строки печатает карточка ревью, и правка на месте дала бы
    расхождение между напечатанным и посчитанным вердиктом.
    """
    out: list[dict] = []
    for item in rows or []:
        row = dict(item)
        kind = str(row.get("kind") or "")
        if str(row.get("status") or "") != "закрыто" and kind in PROMPT_FINDING_STATUS:
            row["status"] = PROMPT_FINDING_STATUS[kind]
        out.append(row)
    return out


def effective_prompt_verdict(ctx: dict) -> dict | None:
    """Действующий вердикт ревью постановки: реестр находок сильнее отчёта (план 12, S2).

    Отчёта нет — вердикта нет: реестр его не выдумывает (`None`). Реестр переопределяет
    только распознанный вердикт (`APPROVE`/`REQUEST_CHANGES`), а файла реестра нет —
    переопределять нечего, и действующий вердикт равен отчётному (у плана это условие
    `registry and known`). `NONE`/`AMBIGUOUS` не лечатся: иначе нечитаемый отчёт
    «вылечивался» бы чистым реестром.

    Ключи: `verdict` — действующий, `report` — из отчёта, `open` — ID со статусом
    `открыто` в порядке файла, `total` — строк в реестре, `registry` — файл реестра есть,
    `discrepancy` — действующий вердикт расходится с отчётом, `error` — реестр не читается.
    """
    report = str((ctx.get("prompt_verdict") or {}).get("verdict") or "")
    if not report:
        return None
    rows = ctx.get("prompt_findings") or []
    open_ids = [str(item.get("id") or "") for item in rows
                if str(item.get("status") or "") == "открыто"]
    registry = bool(ctx.get("prompt_findings_exists"))
    known = report in ("APPROVE", "REQUEST_CHANGES")
    verdict = ("REQUEST_CHANGES" if open_ids else "APPROVE") if (registry and known) else report
    return {"verdict": verdict, "report": report, "open": open_ids, "total": len(rows),
            "registry": registry, "discrepancy": bool(registry and known and verdict != report),
            "error": str(ctx.get("prompt_findings_error") or "")}


def open_prompt_findings_lines(ctx: dict) -> list[str]:
    """Открытые находки реестра постановки строками для карточки правок: ID, что не так.

    Колонки «Шаг» у реестра постановки нет — постановка принадлежит одному шагу, и
    повторять его в каждой строке незачем.
    """
    return [f"{item.get('id')}: {item.get('text')} — {item.get('action')}"
            for item in (ctx.get("prompt_findings") or [])
            if str(item.get("status") or "") == "открыто"]


def verdict_discrepancy_text(ctx: dict) -> str:
    """Строка о расхождении вердикта отчёта с реестром; пустая строка — расхождения нет.

    Уходит комментарием в карточку ревью: это она породила отчёт, вердикт которого
    разошёлся с тем, чем страж руководствуется.
    """
    view = effective_plan_verdict(ctx)
    if not view or not view["discrepancy"]:
        return ""
    rel = ctx.get("plan_review_rel") or f"docs/reviews/plan-{ctx.get('plan_id')}.md"
    if view["verdict"] == "REQUEST_CHANGES":
        return (f"Вердикт по реестру находок: REQUEST_CHANGES (открыты "
                f"{', '.join(view['open'])}) — в отчёте {rel} записано {view['report']}. "
                f"Вердикт считается по реестру (шаг 7-S2): ревьюеру закрыть эти ID фактом "
                f"или подтвердить статус.")
    return (f"Вердикт по реестру находок: APPROVE (открытых находок нет) — в отчёте {rel} "
            f"записано {view['report']}. Вердикт считается по реестру (шаг 7-S2): если "
            f"находка осталась, оставь её в реестре открытой строкой.")


def plan_version_chars(path, journal_marker: str = "журнал") -> int:
    """Объём версии плана в символах — мера расширения плана правкой (план 7, S4).

    Версия плана, а не файл: строки журнала меняются при каждом закрытии шага, и сырой
    объём показывал бы расширение там, где план не правился (`approval.plan_version_text`).
    Файла нет — 0, исключения наружу не идут: правка без плана не расширяет ничего.

    Функция, а не прямой вызов в `build_ctx`: там имя `approval` переопределяется ответом
    реестра ниже по коду, и обращение к модулю стало бы `UnboundLocalError`.
    """
    return len(approval.plan_version_text(path, journal_marker))


def _plan_growth_limits(raw) -> dict:
    """Допуск расширения плана: `percent` и `chars_per_finding` из конфига или умолчание.

    Разбор тот же, что у `config._plan_growth`: число или числовая строка, не bool, не
    отрицательное, процент — только целый. Разные правила на одном ключе означали бы, что
    при живом конфиге и при синтетическом `ctx` действуют разные лимиты. В отличие от
    конфига, здесь мусор не роняет тик: `decide` — функция состояния, и падать ей нечем,
    поэтому негодное значение уступает умолчанию.
    """
    limit = {"percent": PLAN_GROWTH_PERCENT,
             "chars_per_finding": PLAN_GROWTH_CHARS_PER_FINDING,
             "enforce": False}
    raw = raw if isinstance(raw, dict) else {}
    if isinstance(raw.get("enforce"), bool):
        limit["enforce"] = raw["enforce"]
    for key in ("percent", "chars_per_finding"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if number < 0 or (key == "percent" and number != int(number)):
            continue
        limit[key] = int(number)
    return limit


def _percent_text(value) -> str:
    """Процент с одним знаком, без хвостовых нулей: 15.0 → «15», 4.5 → «4.5».

    Округление до целого скрывало бы малый перерасход (900 символов на плане в 20000 — это
    4.5 %, а не 5 %), а хвостовые нули читались бы как точность, которой нет.
    """
    text = f"{float(value or 0):.1f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def plan_growth_report(before: int, after: int, closed: int, percent,
                       chars_per_finding, ordered: int = 0) -> dict:
    """Замер расширения плана: рост, обе границы допуска и превышение (план 7, S4).

    Допуск — МЕНЬШЕЕ из двух границ: процента версии плана и символов на каждую находку,
    ради которой правку заказывали. Большее из них работало бы как «правке всегда выгодная
    граница»: при плане на 10000 символов и десяти находках граница находок (4000)
    перекрывала бы процент (1000), и лимит не ограничивал бы ничего.

    `ordered` — сколько находок карточка правок получила при заказе (снимок версии до
    правки). Без него граница считалась по числу находок, ЗАКРЫТЫХ на том же круге, а на
    первом круге их всегда ноль: допуск выходил нулевой, и любая правка по ревью упиралась
    в стоп — то есть проверка наказывала ровно за то, ради чего ревью существует (решение
    владельца 19.11.2026). Закрытые находки остаются нижней границей смысла: закрыто
    больше, чем заказано, — бюджет растёт вместе с ними.

    `percent` в результате — фактический рост в процентах (`growth * 100 / before`), а не
    потолок из конфига: цифра читается владельцем в стоп-сигнале и в состоянии плана, и
    подмена её лимитом показывала бы «10 %» при росте на 15 %. Сравнивать не с чем
    (`before <= 0`) — превышения нет и допуск нулевой.
    """
    before = max(_as_int(before, 0), 0)
    after = max(_as_int(after, 0), 0)
    closed = max(_as_int(closed, 0), 0)
    ordered = max(_as_int(ordered, 0), 0)
    percent = max(_as_int(percent, PLAN_GROWTH_PERCENT), 0)
    chars_per_finding = max(_as_int(chars_per_finding, PLAN_GROWTH_CHARS_PER_FINDING), 0)
    growth = max(after - before, 0)
    percent_cap = before * percent // 100
    budget = max(closed, ordered)
    per_finding_cap = chars_per_finding * budget
    allowed = min(percent_cap, per_finding_cap)
    if before <= 0:
        # Сравнивать не с чем: не наказываем и не считаем рост долей пустого знаменателя.
        return {"before": before, "after": after, "growth": growth, "percent": 0.0,
                "percent_cap": 0, "per_finding_cap": per_finding_cap, "ordered": ordered,
                "allowed": 0, "closed": closed, "budget": budget, "over": False}
    return {"before": before, "after": after, "growth": growth,
            "percent": growth * 100.0 / before,
            "percent_cap": percent_cap, "per_finding_cap": per_finding_cap,
            "allowed": allowed, "closed": closed, "ordered": ordered, "budget": budget,
            "over": growth > allowed}


def plan_fix_snapshot(p: Paths, round_no: int, ctx: dict, findings: list[str]) -> dict:
    """Версия плана ДО правки: объём, метка и открытые ID реестра находок (S4).

    Пишется при заказе карточки правок: после правки файл перезаписан, и сравнивать
    расширение было бы не с чем. `open_ids` берутся из реестра (`ctx["plan_findings"]`,
    статус `открыто`, порядок реестра), а не из строк карточки: карточка говорит, что
    чинить, реестр — что закрыто, и «сколько закрыто» считается только по реестру.
    """
    path = Path(p.plan_file)
    marker = approval.journal_marker_of(p.cfg)
    rows = [item for item in (ctx.get("plan_findings") or []) if isinstance(item, dict)]
    open_ids = [str(item.get("id") or "").strip() for item in rows
                if str(item.get("status") or "") == "открыто"]
    return {
        "round": int(round_no),
        "version_sha256": approval.plan_version_sha(path, marker),
        "chars": plan_version_chars(path, marker),
        "open_ids": [fid for fid in open_ids if fid][:FINDINGS_LIMIT],
        # Ровно столько находок карточка получила (§4.2): минимум 1 подменял бы «закрывать
        # нечего» допуском в 400 символов, которого контракт не даёт.
        "findings_open": len(findings or []),
        "at": time.strftime("%d.%m.%Y %H:%M:%S"),
    }


def _closed_findings(ctx: dict, baseline: dict) -> tuple[int, list[str], int]:
    """Сколько находок правки закрыто, какие именно и сколько было заказано (S4).

    Мера — реестр находок плана (`docs/reviews/plan-<N>-findings.md`): открытые ID записаны
    в базовую версию при заказе правки, и закрытыми считаются те, что сменили статус на
    `закрыто`. Третье число — бюджет допуска: столько находок правка получила при заказе.
    Считать допуск только по закрытым нельзя: на первом круге статусы ещё `открыто` (их
    меняет сама правка), и допуск выходил нулевым — любая правка по ревью упиралась в стоп
    (находка владельца 19.11.2026, план 2 проекта self-analyzer).

    План без реестра (и база, записанная до появления `open_ids`) считается по-старому:
    числом находок карточки правок — и как бюджет, и как закрытые.
    """
    open_ids = [str(item) for item in (baseline.get("open_ids") or []) if str(item or "")]
    rows = ctx.get("plan_findings") or []
    if open_ids and rows:
        statuses = {str(item.get("id") or ""): str(item.get("status") or "") for item in rows}
        closed = [fid for fid in open_ids if statuses.get(fid) == "закрыто"]
        return len(closed), closed, len(open_ids)
    findings_open = max(_as_int(baseline.get("findings_open"), 0), 0)
    return findings_open, [], findings_open


def _plan_growth_decision(ctx: dict) -> dict | None:
    """Правка расширила план сверх допуска — owner-стоп с замером, план не усекается (S4).

    Срабатывает один раз на версию плана: закрытая карточка правок дала новую версию, её
    объём сравнивается с базой из состояния. Отметка `reported_version_sha256` — признак
    «по этой версии решение уже вынесено», иначе после `--resume` тот же owner-стоп
    повторялся бы каждый тик.

    `None` — либо допуск не превышен, либо сравнивать нечего (нет базовой версии, правка ещё
    не закрыта, план не менялся). Молчаливого усечения плана нет: решение всегда `stop`.
    Поля `step` в решении нет намеренно: стоп планового уровня не действие шага и не должно
    занимать слот «одно действие на шаг за тик» (`step_key`).
    """
    baseline = ctx.get("plan_fix_baseline") or {}
    if not isinstance(baseline, dict) or not baseline:
        return None                       # предыдущей версии нет — сравнивать не с чем
    fixes = card_of(ctx.get("plan_fix_cards") or [], PLAN_FIX_KEY)
    if not fixes or str(fixes[-1].get("status") or "") != "done":
        return None                       # правка ещё не закрыта: меряем после неё
    current_sha = str((ctx.get("approval") or {}).get("version_sha256") or "")
    base_sha = str(baseline.get("version_sha256") or "")
    if not current_sha or not base_sha or current_sha == base_sha:
        return None                       # версия неизвестна либо правка её не меняла
    if str(baseline.get("reported_version_sha256") or "") == current_sha:
        return None                       # по этой версии плана решение уже вынесено
    limits = _plan_growth_limits(ctx.get("max_plan_growth"))
    percent = limits["percent"]
    chars_per_finding = limits["chars_per_finding"]
    closed, closed_ids, ordered = _closed_findings(ctx, baseline)
    report = plan_growth_report(baseline.get("chars"), ctx.get("plan_version_chars"), closed,
                                percent, chars_per_finding, ordered)
    if not report["over"]:
        return None
    if not limits["enforce"]:
        # Владелец понизил правило до замера (умолчание с 19.11.2026): план вырос сверх
        # допуска, но работу не останавливаем — цифры уходят в состояние плана, шаги идут.
        percent_text = _percent_text(report["percent"])
        return {"action": "note_growth", "kind": "owner", "growth": report,
                "reason": (f"правка расширила план на {percent_text}% "
                           f"({report['growth']} символов: было {report['before']}, стало "
                           f"{report['after']}) сверх допуска {report['allowed']} — замер, "
                           f"исполнение продолжается")}
    percent_text = _percent_text(report["percent"])
    card_id = str(fixes[-1].get("id") or "")
    reason = (f"правка плана расширила план на {percent_text}% ({report['growth']} символов: "
              f"было {report['before']}, стало {report['after']}) — сверх лимита "
              f"{report['allowed']} ({percent}% версии = {report['percent_cap']} "
              f"против {chars_per_finding} × {report['closed']} = "
              f"{report['per_finding_cap']}, берётся меньшее); находок закрыто "
              f"{report['closed']}; страж план не усекает — решает владелец")
    details = "\n".join([
        f"- версия плана: было {report['before']} символов, стало {report['after']} "
        f"(+{report['growth']}, {percent_text}%)",
        f"- допустимо {report['allowed']}: меньшее из {percent}% версии "
        f"({report['percent_cap']}) и {chars_per_finding} × {report['closed']} "
        f"закрытых находок ({report['per_finding_cap']})",
        f"- карточка правок: {card_id or '—'}; закрыто находок реестра: "
        f"{', '.join(closed_ids) if closed_ids else (report['closed'] or '—')}",
        "- варианты: вернуть план к объёму правки, поднять `max_plan_growth` в "
        "`pipeline.json` или принять как есть (`--resume`)",
    ])
    comment_text = "\n".join([
        f"СТОП-СИГНАЛ СТРАЖА: правка расширила план на {percent_text}% "
        f"(было {report['before']} символов, стало {report['after']})",
        f"находок закрыто {report['closed']}; допустимый рост {report['allowed']} символов — "
        f"меньшее из {percent}% версии ({report['percent_cap']}) и "
        f"{chars_per_finding} на находку ({report['per_finding_cap']})",
        "страж план не усекает: вернуть план к объёму правки, поднять max_plan_growth "
        "в pipeline.json или принять как есть (python .pipeline/gate.py --resume)",
    ])
    return {"action": "stop", "plan_review": True, "kind": "owner", "growth": report,
            "reason": reason, "details": details,
            "comment": {"card": card_id, "text": comment_text}}


def _record_plan_growth(state: dict, ctx: dict, growth: dict) -> None:
    """Положить замер расширения плана в состояние: цифры, время и метка версии.

    Метка `reported_version_sha256` — «по этой версии плана решение уже вынесено»: без неё
    owner-стоп (или замер) повторялся бы каждый тик и после каждого `--resume`. Отчёт
    кладётся целиком, а не одной дельтой: по состоянию читаются и границы, и число
    закрытых находок — иначе «доложено» не диагностируется по цифрам.
    """
    state["plan_growth_report"] = dict(growth, at=time.strftime("%d.%m.%Y %H:%M:%S"))
    baseline = state.get("plan_fix_baseline")
    if isinstance(baseline, dict):
        baseline["reported_version_sha256"] = str(
            (ctx.get("approval") or {}).get("version_sha256") or "")
        baseline["reported_growth"] = dict(growth)
        baseline["reported_at"] = state["plan_growth_report"]["at"]


def _plan_review_decision(ctx: dict) -> dict | None:
    """Ревью плана: карточка, ожидание, вердикт, круги правок по лимиту, эскалация (C6, D1-D7).

    Бюджет кругов правок — `plan_fix_budget`: `max_fix_rounds` проекта плюс ручные круги
    из маркера владельца `plan_<N>_rounds.json`. Исчерпан расширенный бюджет — owner-стоп.

    Чистое решение планового уровня: `None` — ревью не нужно либо уже позади, и план идёт
    обычным порядком. Поля `step` в решении нет намеренно: иначе инвариант «одно действие
    на шаг за тик» (`step_key`) счёл бы его действием шага — как у решения разведки.
    """
    if not ctx.get("plan_review_format"):
        return None                    # план старого формата ревью плана не получает (D1)
    cards = ctx.get("plan_review_cards") or []
    if ctx.get("plan_started_journal") and not cards:
        # D2: идущий план не останавливаем — ревью ему не заказывается. Признак «идёт» —
        # ЗАКРЫТЫЕ ШАГИ ЖУРНАЛА, т.е. то, что писал не этот тик. Файл состояния для D2 не
        # годится: его пишет сам страж (`touch` после create_scout, `watch_idle` на
        # ожидании), и после первого же ожидания гейт отключил бы сам себя — в путях
        # «разведка → ревью» и «ожидание корректировки по разведке» шаг начинался бы без
        # вердикта (находка 1 ревью 5-S5). Как только карточка ревью заказана, гейт ведёт
        # её до вердикта независимо от журнала.
        return None
    plan_id = ctx.get("plan_id")
    rel = ctx.get("plan_review_rel") or f"docs/reviews/plan-{plan_id}.md"
    verdict = ctx.get("plan_review_verdict")
    # S2 плана 7: действующий вердикт считает реестр находок. Битый реестр — инцидент, а не
    # «открытых нет»: ошибку поймал `build_ctx`, решение её только называет. Инцидентный стоп
    # снимается сам, как только файл починили (`decide` снимает его при смене решения).
    view = effective_plan_verdict(ctx)
    if view and view["error"]:
        return {"action": "stop", "plan_review": True,
                "reason": f"реестр находок плана {plan_id} не читается: {view['error']}"}
    # План правился карточкой правки по разведке (0.20.0): sha256 изменился, и утверждение
    # обновляет владелец. Страж его не наследует и не подделывает, а просит переутвердить
    # готовой командой; до этого ревью не заказывается — оно было бы ревью неутверждённого
    # плана. После `plan --accept N` запись совпадёт, и ветка снова откроется.
    approval = ctx.get("approval") or {}
    if ctx.get("scout_fix_cards") and approval.get("block_kind") == "approval":
        return {"action": "idle", "plan_review": True, "approval_blocked": True,
                "reapprove": True,
                "reason": (f"план {plan_id} изменён после утверждения — переутверди: "
                           f"hermes pipeline plan --accept {plan_id}")}
    # C9: дверь к шагам — сам вердикт, а не карточка. План, прошедший ревью раньше
    # (например, идущий план или ревью задним числом), шаги не ждёт.
    if view is not None and view["verdict"] == "APPROVE":
        # Отклонение от порядка ветвей §4.3 (названо в отчёте шага): свежий вердикт,
        # написанный по текущей версии плана, и есть дверь — иначе фикстура
        # `tests/engine/test_scout.py:167` (план без раздела «Разведка» плюс готовый
        # APPROVE) упиралась бы в предусловие разведки. Свежесть — привязка к sha плана
        # (S6), а не mtime отчёта.
        if ctx.get("plan_review_fresh"):
            return None
        if not ctx.get("plan_review_ok"):
            # Правку плана по разведке заказывает страж, а не владелец руками (0.20.0):
            # карточка правит план, дальше переутверждение и повторное ревью.
            fix = _scout_fix_decision(ctx)
            if fix:
                return fix
            return {"action": "idle", "plan_review": True,
                    "reason": (f"план {plan_id} не прошёл корректировку по разведке: "
                               f"{ctx.get('plan_review_reason') or 'нет раздела «Разведка»'} "
                               f"(вердикт {rel} привязан к другой версии плана)")}
        if cards:
            # Пока идёт/упала карточка прошлого круга, новый круг не заказываем: иначе
            # каждый тик заводил бы новый исполнитель на тот же отчёт (он остаётся
            # устаревшим, пока ревьюер не напишет новый).
            card = cards[-1]
            status = str(card.get("status") or "")
            if status not in TERMINAL:
                return {"action": "idle", "plan_review": True,
                        "reason": (f"ждёт повторного ревью плана {plan_id}: карточка "
                                   f"{card.get('id')} ({status})")}
            if status != "done":
                detail = " ".join(str(card.get("error") or card.get("result")
                                      or "").split())[:300]
                return {"action": "stop",
                        "reason": (f"ревью плана {plan_id} упало: карточка {card.get('id')} "
                                   f"в статусе {status}: {detail}")}
        round_no = len(cards) + 1
        review = (ctx.get("approval") or {}).get("review") or {}
        return {"action": "create_plan_review", "round": round_no,
                "reason": (f"план {plan_id} правился после APPROVE: повторное ревью "
                           f"(круг {round_no}): "
                           f"{review.get('reason') or 'вердикт про прошлую версию'}")}
    if not ctx.get("plan_review_ok"):
        # Разведка — предусловие ЗАКАЗА ревью (D5): план без корректировки по разведке
        # ждёт. Артефакт разведки на месте — правим план карточкой (0.20.0): без неё цикл
        # «план → разведка → правка → ревью» не замыкался и владелец правил руками.
        fix = _scout_fix_decision(ctx)
        if fix:
            return fix
        return {"action": "idle", "plan_review": True,
                "reason": (f"план {plan_id} не прошёл корректировку по разведке: "
                           f"{ctx.get('plan_review_reason') or 'нет раздела «Разведка»'}")}
    if not cards:
        profile = ctx.get("plan_review_profile") or PLAN_REVIEW_PROFILE_KEY
        return {"action": "create_plan_review", "round": 1,
                "reason": f"ревью плана {plan_id} не проводилось — карточка роли {profile}"}
    card = cards[-1]
    status = str(card.get("status") or "")
    if status not in TERMINAL:
        return {"action": "idle", "plan_review": True,
                "reason": (f"ждёт ревью плана {plan_id}: карточка {card.get('id')} "
                           f"({status})")}
    if status != "done":
        detail = " ".join(str(card.get("error") or card.get("result") or "").split())[:300]
        return {"action": "stop",
                "reason": (f"ревью плана {plan_id} упало: карточка {card.get('id')} "
                           f"в статусе {status}: {detail}")}
    if verdict is None:
        return {"action": "stop",
                "reason": f"ревью плана {plan_id} закрыто, но отчёта {rel} нет"}
    if verdict["verdict"] in ("NONE", "AMBIGUOUS"):
        return {"action": "stop",
                "reason": (f"вердикт ревью плана {plan_id} не распознан "
                           f"({verdict['verdict']}): {Path(verdict['path']).name}")}
    if view is not None and view["verdict"] == "APPROVE":
        return None                    # замысел принят — шаг начинается обычным порядком
    # REQUEST_CHANGES: круг правок плана, пока не исчерпан лимит `max_fix_rounds`
    # (конституция п.4); после лимита решает владелец.
    if not _report_written_after(verdict, card):
        return {"action": "stop",
                "reason": (f"отчёт {rel} не обновлён после карточки {card.get('id')} — "
                           f"вердикт может быть от прошлого круга")}
    # Вердикт про ПРЕЖНЮЮ версию плана: владелец правил текст и переутвердил его
    # (`plan --accept N` пишет `review_reset_at`). Правки по такому отчёту заказывать
    # нельзя — они относились к старому тексту, и карточка правила бы уже исправленное.
    # Свежий круг ревью даёт новый вердикт. Признак берётся НЕ из «отчёт не свежий»
    # (так бывает и у отчёта без привязки к sha, и у вердикта прошлого круга — их лечит
    # стоп выше), а из явного решения владельца: переутверждение позже последней карточки.
    reset_at = approval_reset_at((ctx.get("approval") or {}).get("record") or {})
    last_review_at = max((int(c.get("created_at") or 0) for c in cards), default=0)
    if (reset_at and reset_at > last_review_at and not ctx.get("plan_review_fresh")
            and (ctx.get("approval") or {}).get("state") == "approved"):
        return {"action": "create_plan_review", "round": len(cards) + 1,
                "reason": (f"план {plan_id} переутверждён после вердикта {rel} — нужен свежий "
                           f"круг ревью (круг {len(cards) + 1})")}
    # Владелец, переутвердив правленый план (`plan --accept N`), открывает бюджет кругов
    # ревью заново: `review_reset_at` в записи утверждения — момент его явного решения.
    # Считаются только карточки этой версии плана, иначе третья правка упиралась бы в
    # owner-stop на исчерпанных двух кругах и шаги не открылись бы никогда.
    epoch_cards = ([c for c in cards if int(c.get("created_at") or 0) >= reset_at]
                   if reset_at else cards)
    fixes = ctx.get("plan_fix_cards") or []
    epoch_fixes = ([f for f in fixes if int(f.get("created_at") or 0) >= reset_at]
                   if reset_at else fixes)
    # Бюджет кругов правок — `max_fix_rounds` проекта (тот же ключ, что у правок шага,
    # конституция п.4) плюс ручные круги из маркера владельца, и считается по КАРТОЧКАМ
    # ПРАВОК. До 0.25.2 он считался по карточкам ревью, а правка заказывалась только когда
    # карточек правок нет вовсе: второй REQUEST_CHANGES давал не вторую правку, а ещё одно
    # ревью того же текста — 2–4 ревью подряд и owner-stop при живом лимите кругов правок.
    max_rounds = plan_fix_budget(ctx)
    last_fix = epoch_fixes[-1] if epoch_fixes else None
    if last_fix is not None:
        fix_status = str(last_fix.get("status") or "")
        if fix_status not in TERMINAL:
            return {"action": "idle", "plan_review": True,
                    "reason": (f"ждёт правку плана {plan_id}: карточка {last_fix.get('id')} "
                               f"({fix_status})")}
        if fix_status != "done":
            detail = " ".join(str(last_fix.get("error") or last_fix.get("result")
                                  or "").split())[:300]
            return {"action": "stop",
                    "reason": (f"правка плана {plan_id} упала: карточка {last_fix.get('id')} "
                               f"в статусе {fix_status}: {detail}")}
    # Отказ этого круга ещё не получил своей карточки правок. Признак — счётчики эпохи, а
    # не метки времени: карточки живут с точностью до секунды, и правка с ревью легко
    # делят одну секунду (в тестах — всегда). Инвариант простой: сколько карточек ревью,
    # столько и ответных правок; ревью больше — на последний отказ правки ещё не было.
    if len(epoch_cards) > len(epoch_fixes):
        if len(epoch_fixes) >= max_rounds:
            report = Path(str(verdict["path"]))
            findings = (_blocking_findings(report, FINDINGS_LIMIT)
                        or _review_findings(report, FINDINGS_LIMIT))
            return {"action": "stop", "kind": "owner",
                    "reason": (f"круги правок плана {plan_id} исчерпаны "
                               f"({len(epoch_fixes)} из {max_rounds}) после REQUEST_CHANGES — "
                               f"решает владелец: {rel}"),
                    "details": ("\n".join(f"- {item}" for item in findings)
                                or f"Разбор — в отчёте {rel}.")}
        # Источник действующего вердикта называем в причине: «ревью» и «реестр» — разные поводы
        # править план, и владелец должен видеть, какой сработал. Подстроки «кругом N из M» и
        # путь отчёта остаются: по ним читают журнал тиков и тесты прошлых шагов.
        verdict_source = (f"реестр находок плана {plan_id} держит REQUEST_CHANGES "
                          f"(открыты {', '.join(view['open'])})"
                          if view and view["registry"] and view["open"]
                          else f"ревью плана {plan_id} вернуло REQUEST_CHANGES")
        return {"action": "create_plan_fix", "round": len(epoch_fixes) + 1,
                # Маркер круга ревью живёт в ключе карточки правки (T3e): по нему бюджет
                # отличается «первый отказ после круга, заказанного по устаревшему
                # APPROVE» от настоящего второго отказа. `max(..., 1)` — отчёт, написанный
                # руками без карточек, не должен превращать первый отказ во второй.
                "review_round": max(len(epoch_cards), 1),
                "reason": (f"{verdict_source} — правим план кругом "
                           f"{len(epoch_fixes) + 1} из {max_rounds}: {rel}")}
    return {"action": "create_plan_review", "round": len(cards) + 1,
            "reason": (f"план {plan_id} поправлен по ревью — повторное ревью "
                       f"(круг {len(cards) + 1})")}


def _prefetch_decision(ctx: dict) -> dict | None:
    """Заказ постановки N+1, пока шаг N в работе (правило 2), либо None.

    Ожидание кодируется решением `idle`, а не повторным `applied=False`: иначе каждый
    тик тратил бы единственное действие на одно и то же (D10).
    """
    if not ctx.get("prefetch"):
        return None
    step, nxt = ctx.get("next"), ctx.get("next_step")
    if not step or not nxt:
        return None                        # следующий шаг последний или его нет (D2)
    if not card_of(ctx.get("cards") or [], "step"):
        return None                        # шаг ещё не взят в работу
    if ctx.get("next_prompt") is not None:
        return None                        # постановка N+1 уже есть
    orders = card_of(ctx.get("next_cards") or [], "prompt")
    if orders:
        return {"action": "idle", "step": step["id"],
                "reason": (f"постановка {nxt['id']} заказана заранее: карточка "
                           f"{orders[-1]['id']} ({orders[-1]['status']})")}
    return {"action": "create_prompt", "step": nxt["id"], "data": nxt,
            "reason": f"постановка {nxt['id']} заказана заранее, пока шаг {step['id']} в работе"}


def _prompt_review_needed(ctx: dict) -> bool:
    """Режим `prompt_review`: `never` — нет, `always` — да, `auto` — только устаревшая."""
    mode = str(ctx.get("prompt_review") or "auto").strip().lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    if not ctx.get("prompt_recon"):
        return False                       # без отметки свежесть не проверить — не гоним (D5)
    return not bool(ctx.get("prompt_fresh"))


def _prompt_review_cards(ctx: dict, sid: str) -> list[dict]:
    """Карточки ревью постановки шага: `:promptreview` и повторные `:promptreview:{N}`."""
    pat = f":{sid}:promptreview"
    return [c for c in (ctx.get("cards") or []) if pat in (c.get("key") or "")]


def _round_of_key(card: dict) -> int:
    """Номер круга из ключа карточки `...:<N>`; не число — 0."""
    try:
        return int((card.get("key") or "").rsplit(":", 1)[-1])
    except ValueError:
        return 0


def _prompt_fix_cards(ctx: dict, sid: str) -> list[dict]:
    """Круги автопереписывания постановки `:promptfix:{N}` по возрастанию номера."""
    pat = f":{sid}:promptfix:"
    cards = [c for c in (ctx.get("cards") or []) if pat in (c.get("key") or "")]
    return sorted(cards, key=_round_of_key)


def _prompt_stale_cards(ctx: dict, sid: str) -> list[dict]:
    """Пересборки протухшей постановки `:promptfix-stale:{N}` по возрастанию номера.

    Отдельная серия от `:promptfix:`: шаблоны не пересекаются, поэтому счётчики не сливаются
    и пересбор по свежести не съедает бюджет правок (`max_fix_rounds`).
    """
    pat = f":{sid}:promptfix-stale:"
    cards = [c for c in (ctx.get("cards") or []) if pat in (c.get("key") or "")]
    return sorted(cards, key=_round_of_key)


def _prompt_findings_stop(ctx: dict, sid: str) -> dict | None:
    """Битый реестр постановки — стоп-инцидент; `None` — реестр читается или его нет.

    `kind` не задаётся: инцидент страж снимает сам, как только файл починили. Вызывается
    ровно там, где реестр читается: перед заказом карточки (её тело печатает таблицу) и
    перед действующим вердиктом. Пока ревью идёт, реестр никто не читает, и стоп по нему
    был бы шумом.
    """
    error = str(ctx.get("prompt_findings_error") or "")
    if not error:
        return None
    return {"action": "stop", "step": sid,
            "reason": f"реестр находок постановки {sid} не читается: {error}"}


def _prompt_zero_fix(ctx: dict, sid: str, fixes: list[dict]) -> dict | None:
    """Правило «0 закрытых»: правка, не закрывшая ни одной находки, новый круг не открывает.

    Проверяется в ветке бюджета, когда ревью новой редакции уже закрыто отчётом: только там
    видно, закрыла ли правка хоть что-нибудь. База (`prompt_fix_baseline`) пишется при
    заказе круга `:promptfix:N`; сравнивается номер последней карточки правок, а не
    `len(fixes)`: номер круга считает и пересборки по свежести.

    Стоп owner и один раз (`reported_round`): иначе он повторялся бы каждый тик и после
    `--resume`. Пропавший реестр — инцидент, а не «0 закрытых»: `load_findings` на
    отсутствующем файле даёт `[]`, и владелец искал бы правку, которой не было.
    """
    baseline = ctx.get("prompt_fix_baseline") or {}
    if not isinstance(baseline, dict) or not baseline or not fixes:
        return None
    if str(baseline.get("step") or "") != sid:
        return None
    round_no = _round_of_key(fixes[-1])
    if int(baseline.get("round") or 0) != round_no:
        return None
    if int(baseline.get("reported_round") or 0) == round_no:
        return None
    open_ids = [str(item) for item in (baseline.get("open_ids") or []) if str(item)]
    if not open_ids:
        return None                        # правка и не должна была ничего закрывать
    if not ctx.get("prompt_findings_exists"):
        return {"action": "stop", "step": sid,
                "reason": (f"реестр находок постановки {sid} исчез: база круга {round_no} "
                           f"не с чем сверить")}
    status = {str(item.get("id") or ""): str(item.get("status") or "")
              for item in (ctx.get("prompt_findings") or [])}
    if any(status.get(item) in ("закрыто", "не блокирует") for item in open_ids):
        return None
    return {"action": "stop", "step": sid, "kind": "owner", "zero_fix": round_no,
            "reason": (f"правка постановки не закрыла ни одной находки "
                       f"(круг {round_no}: {', '.join(open_ids)} — открыты)")}


def _prompt_latest_card(fixes: list[dict], stales: list[dict]) -> tuple[dict, str, str] | None:
    """Самая свежая карточка двух серий, её глагол и существительное для причины.

    Работают по одной постановке, поэтому «идёт / упала» смотрит на объединённую серию:
    `cards` приходят в порядке `created_at`, серии сортируются по номеру круга; при равном
    `created_at` (тик поставил обе в одну секунду) решает номер круга.
    """
    candidates = ([(c, "переписывается", "переписывание") for c in fixes]
                  + [(c, "пересобирается", "пересборка") for c in stales])
    if not candidates:
        return None
    return max(candidates, key=lambda item: (int(item[0].get("created_at") or 0),
                                             _round_of_key(item[0])))


def _prompt_review_of_round(ctx: dict, sid: str, round_no: int) -> dict | None:
    """Карточка ревью именно этого круга: первый круг — ключ без номера (так жили до правки)."""
    suffix = f":{sid}:promptreview" + ("" if round_no <= 1 else f":{round_no}")
    for card in _prompt_review_cards(ctx, sid):
        if (card.get("key") or "").endswith(suffix):
            return card
    return None


def _report_written_after(verdict: dict | None, card: dict) -> bool:
    """Отчёт переписан в этом круге: иначе прочитан вердикт прошлого круга."""
    if not verdict:
        return False
    try:
        path = Path(str(verdict["path"]))
        started = float(card.get("started_at") or card.get("created_at") or 0)
    except (KeyError, TypeError, ValueError):
        return True
    if started <= 0 or not path.is_file():
        return True
    return path.stat().st_mtime >= started - 1.0


def _prompt_review_decision(ctx: dict, step: dict) -> dict | None:
    """Ревью постановки до старта шага (правило 3): заказ, пересборка, стоп или None.

    REQUEST_CHANGES не останавливает конвейер: постановку переписывает роль prompter
    (`:promptfix:{N}`), после каждого круга ревью повторяется. Протухшая постановка (отчёт
    снят по другой версии или вход изменился после ревью) конвейер тоже не останавливает:
    страж заказывает пересборку (`:promptfix-stale:{N}`, свой лимит `max_stale_rebuilds`,
    не входящий в `max_fix_rounds`) и повторное ревью. С шага 12-S2 вердикт ставится по
    реестру находок постановки, и порядок ветвей жёсткий: свежесть → реестр → бюджет, иначе
    пересборка съедала бы круг правок, а закрытый реестр не смог бы открыть шаг. Стоп —
    когда исчерпаны соответствующие круги, упала карточка, вердикт не распознан, реестр
    находок постановки не читается или исчез, либо круг правок не закрыл ни одной находки.

    Ветка спеки high-шага (8-S2B) стоит отдельно и выше остальных решений: неполный блок
    «Спека» в постановке даёт `create_prompt_fix` с номером круга и пределом `step_fix_limit`,
    а исчерпание предела — owner-стоп «решает владелец». Она стоит выше стража
    `prompt_review: never` (выключатель ревью постановок не выключает риск-гейт) и выше
    разбора вердикта (нет спеки — отказ постановки, а не карточка шага), но ниже блока
    «постановка переписывается / упала»: иначе второй тик заказал бы вторую карточку правок.
    """
    sid = step["id"]
    if not ctx.get("prompt"):
        return None                        # постановки нет: решает обычная ветка create_step
    report_rel = ctx.get("prompt_report") or prompt_review_rel_of(ctx, sid)
    fixes = _prompt_fix_cards(ctx, sid)
    stales = _prompt_stale_cards(ctx, sid)
    mode = str(ctx.get("prompt_review") or "auto").strip().lower()
    # Номер круга ревью: и круг правок, и пересборка сдвигают его на единицу. Без пересборок
    # в счёте повторное ревью взяло бы закрытую карточку круга 1 и прочитало вердикт прошлой
    # редакции — пересборка пошла бы по кругу до лимита. Считается до проверок режима: от
    # `fixes`/`stales` зависит, а от режима нет — и нужен ветке спеки, которая стоит выше
    # стража `never` (8-S2B, D5).
    round_no = len(fixes) + len(stales) + 1
    # «Идёт / упала» смотрит на самую свежую карточку объединённой серии: и правка, и
    # пересборка работают по той же постановке, вторая карточка поверх первой не нужна.
    latest = _prompt_latest_card(fixes, stales)
    if latest is not None:
        last_card, verb, noun = latest
        if last_card["status"] not in TERMINAL:
            return {"action": "idle", "step": sid,
                    "reason": (f"постановка {sid} {verb}: карточка "
                               f"{last_card['id']} ({last_card['status']})")}
        if last_card["status"] != "done":
            detail = (last_card.get("error") or last_card.get("result") or "")[:300]
            return {"action": "stop", "step": sid,
                    "reason": f"{noun} постановки {sid} упало ({last_card['status']}): {detail}"}

    # Спека высокорискового шага (8-S2B): выше стража `never` и выше разбора вердикта (D5).
    # Выключатель ревью постановок не выключает риск-гейт, а `APPROVE` без спеки шаг не
    # пускает: спеки нет — отказ постановки, а не ручной допуск и не карточка шага. Стоит
    # ниже блока «идёт / упала», иначе второй тик заказал бы вторую карточку правок.
    spec = [str(item) for item in (ctx.get("prompt_spec") or []) if str(item)]
    if spec:
        limit = step_fix_limit(ctx, step)
        if len(fixes) >= limit:
            return {"action": "stop", "step": sid, "kind": "owner",
                    "reason": (f"спека шага {sid} не готова, круги правок постановки "
                               f"исчерпаны ({len(fixes)} из {limit}) — решает владелец: "
                               + "; ".join(spec))}
        return {"action": "create_prompt_fix", "step": sid, "data": step,
                "round": round_no, "spec": spec,
                "reason": (f"спека шага {sid} не готова (C7): постановку переписываю "
                           f"автоматически (круг {round_no}): " + "; ".join(spec))}
    if mode == "never" and not fixes and not stales:
        return None                        # владелец выключил ревью постановок

    def _stale(reason_code: str, changed: dict | None = None) -> dict:
        """Оба признака устаревания: пересборка, а после лимита — стоп владельцу."""
        limit = int(ctx.get("max_stale_rebuilds") or 0)
        if len(stales) >= limit:
            # `kind: owner`, а не инцидент: инцидентный стоп страж снимает сам, как только
            # причина ушла, — после исчерпания бюджета любая правка отметки вернула бы
            # конвейер в работу без решения владельца.
            return {"action": "stop", "step": sid, "kind": "owner",
                    "reason": (f"постановка {sid} протухла, пересборок исчерпано "
                               f"({len(stales)} из {limit}, ключ max_stale_rebuilds) — "
                               f"решает владелец")}
        prev_recon = (str(((ctx.get("prompt_input_baseline") or {}).get("recon") or ""))
                      or str(ctx.get("prompt_recon") or ""))
        report_recon = str((ctx.get("prompt_verdict") or {}).get("recon") or "")
        detail = (f"в отчёте recon_commit {report_recon}, "
                  f"в постановке {ctx.get('prompt_recon') or '—'}"
                  if reason_code == "stale-recon" else "вход изменился после ревью")
        return {"action": "create_prompt_fix", "step": sid, "data": step,
                "stale": True, "stale_reason": reason_code,
                "rebuild": len(stales) + 1, "limit": limit, "review_round": round_no,
                "fp": ctx.get("prompt_input_fp") or "",
                "changed": changed or {}, "prev_recon": prev_recon,
                "reason": (f"постановка {sid} протухла: {detail} — пересобираю "
                           f"({len(stales) + 1} из {limit})")}

    need = _prompt_review_needed(ctx) or round_no > 1
    card = _prompt_review_of_round(ctx, sid, round_no)
    if card is None:
        if not need:
            return None
        # Битый реестр останавливает до заказа: тело карточки печатает таблицу, и
        # `GateError` из `run_gate` вышел бы сырым исключением вместо решения `stop`.
        # Правила «0 закрытых» здесь нет: статусы реестра в этот момент менять некому.
        stop = _prompt_findings_stop(ctx, sid)
        if stop is not None:
            return stop
        return {"action": "create_prompt_review", "step": sid, "data": step, "round": round_no,
                "reason": (f"постановка {sid} требует ревью (круг {round_no}, "
                           f"recon_commit: {ctx.get('prompt_recon') or '—'}), отчёт {report_rel}")}
    if card["status"] not in TERMINAL:
        return {"action": "idle", "step": sid,
                "reason": f"ревью постановки {sid} идёт: карточка {card['id']} ({card['status']})"}
    if card["status"] != "done":
        detail = (card.get("error") or card.get("result") or "")[:300]
        return {"action": "stop", "step": sid,
                "reason": f"ревью постановки {sid} упало ({card['status']}): {detail}"}
    verdict = ctx.get("prompt_verdict")
    if verdict is None:
        return {"action": "stop", "step": sid,
                "reason": f"ревью постановки {sid} закрыто, но отчёта {report_rel} нет"}
    if verdict["verdict"] in ("NONE", "AMBIGUOUS"):
        what = "строки вердикта нет" if verdict["verdict"] == "NONE" else "строк вердикта несколько"
        return {"action": "stop", "step": sid,
                "reason": f"вердикт ревью постановки {sid} не распознан ({what}): "
                          f"{Path(verdict['path']).name}"}
    if not _report_written_after(verdict, card):
        return {"action": "stop", "step": sid,
                "reason": (f"ревью постановки {sid} закрыто, но отчёт {report_rel} не обновлён "
                           f"после карточки {card['id']} — вердикт может быть от прошлого круга")}
    # Свежесть (план 12, S1) — выше реестра и бюджета: протухшая постановка делала вердикт
    # о другом тексте, и её пересбор не должен тратить круг правок (6-S8A, допущение 6).
    # Сравнение только при непустой отметке: пустая строка сравнилась бы с sha постановки
    # и дала бы ложную пересборку на каждом тике.
    if verdict.get("recon") and verdict["recon"] != (ctx.get("prompt_recon") or ""):
        # Формальная путаница отметок конвейер не роняет (D6, инцидент 09.11.2026): если
        # текст постановки на sha из отчёта тот же, что в файле сейчас, или обе отметки —
        # один коммит (короткий sha против полного), вердикт относится к этой редакции.
        # Дальше — к реестру, а не на старт: иначе открытая находка прошла бы мимо вердикта.
        if not (ctx.get("prompt_marker_text_same") or ctx.get("prompt_marker_same_commit")):
            # Расхождение отметок — не стоп, а протухшая постановка: пересобираем её от
            # текущего HEAD и ревьюим заново. Стоп остаётся только на исчерпанном лимите
            # пересборок внутри `_stale`.
            return _stale("stale-recon")
    # Отметка совпала (или её нет) — второй признак устаревания: вход постановки (файлы шага
    # и её файл) изменился после того, как эта редакция ушла ревьюеру. Правка мимо git не
    # двигает `recon_commit`, поэтому без отпечатка она прошла бы незамеченной.
    changed = prompt_input_changed(ctx)
    if changed:
        return _stale("stale-input", changed)
    # Реестр находок постановки (план 12, S2): действующий вердикт считается по нему, а не по
    # строке отчёта. Третьей проверки здесь нет: расхождение реестра с отчётом («закрыто» без
    # правки в коммите круга) разбирает ревьюер, а не страж (D11).
    stop = _prompt_findings_stop(ctx, sid)
    if stop is not None:
        return stop
    active_view = effective_prompt_verdict(ctx) or {}
    active = str(active_view.get("verdict") or "")
    open_count = len(active_view.get("open") or [])
    report_verdict = str(active_view.get("report") or "")
    if active != "APPROVE":
        # Бюджет считает действующий вердикт: отчёт APPROVE при открытой находке круги
        # расходует так же, как REQUEST_CHANGES. Текст и число стопа прежние — они
        # закреплены тестом `test_prompt_fix_rounds_exhausted_stops`.
        max_rounds = step_fix_limit(ctx, step)
        if len(fixes) >= max_rounds:
            high = str((step or {}).get("risk") or "").lower() == "high"
            tier = ", класс риска: high" if high else ""
            return {"action": "stop", "step": sid,
                    "reason": (f"ревью постановки {sid} вернуло REQUEST_CHANGES, круги "
                               f"автопереписывания исчерпаны ({len(fixes)}{tier}) — решает владелец: "
                               f"{Path(verdict['path']).name}")}
        # Правило приёмки — только когда следующий круг ещё можно открыть: круг правок,
        # закрывший 0 находок, нового круга не открывает, а уходит владельцу.
        zero = _prompt_zero_fix(ctx, sid, fixes)
        if zero is not None:
            return zero
        return {"action": "create_prompt_fix", "step": sid, "data": step, "round": round_no,
                "open_count": open_count, "report": report_verdict,
                "reason": (f"реестр находок постановки {sid} держит {open_count} открытых "
                           f"находок (отчёт: {report_verdict or '—'}) — постановку "
                           f"переписываю автоматически (круг {round_no}): "
                           f"{Path(verdict['path']).name}")}
    return None                            # действующий APPROVE — шаг стартует


def prompt_review_rel_of(ctx: dict, step_id: str) -> str:
    """Путь отчёта ревью постановки для синтетического ctx (без `Paths`)."""
    prefix = ctx.get("review_prefix") or "?"
    return f"docs/reviews/{prefix}-{step_id}-prompt.md"


def _report_items(text: str) -> list[tuple[str, bool]]:
    """Пункты списков отчёта: нормализованный текст и «идёт под разделом про блокирующие»."""
    items: list[tuple[str, bool]] = []
    under_blocking = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("|"):
            continue
        if line.startswith("#"):
            # Заголовок переключает флаг: подраздел «Блокирующие» — да, любой другой — нет.
            under_blocking = "блокирующ" in line.lower()
            continue
        if line[:2] in ("- ", "* ") or (line[:1].isdigit() and line[1:3] in (". ", ") ")):
            item = line[2:].strip() if line[:2] in ("- ", "* ") else line[3:].strip()
            item = (item.replace("`", "").replace("**", "").replace("__", "")
                    .strip(" .;"))
            if len(item) >= 12:
                items.append((item, under_blocking))
    return items


def _finding_class(word: str) -> str:
    """Класс находки по основе слова: «блокирующ» → «блокирующая»; иначе пусто.

    Сравнение по основе, а не по целому слову: в отчётах пишут «блокирующая», «блокирующих»
    и «Блокирующие» (заголовок подраздела) — падеж и число класс не меняют.
    """
    stem = str(word or "").strip().lower()
    for prefix, name in FINDING_CLASS_STEMS:
        if stem.startswith(prefix):
            return name
    return ""


def _finding_label(label: str) -> tuple[str, str]:
    """Класс и подметка метки: `[блокирующая/тест-недетерминирован]` → (блокирующая, тест-…).

    Класс — первое слово до запятой или дроби: `[значимая, вне объёма шага]` — значимая.
    Метка вне словаря (`[закрыта]` — это статус, `[вне объёма]` — не класс) даёт пустой
    класс, и пункт считается неразмеченным: выдумывать класс из незнакомой метки нельзя.
    Незнакомая подметка после дроби ошибкой не является — её читает тот, кому она нужна.
    """
    head, _, tail = str(label).partition(FINDING_SUBLABEL_SEP)
    return _finding_class(head.split(",")[0].strip()), tail.strip()


def _finding_item_text(line: str) -> str:
    """Текст пункта списка без маркера и разметки; пусто — строка пунктом не является.

    Правила те же, что у `_report_items` (`,` `* `, `1. `, `1) `, не короче 12 символов после
    снятия `**`, `__` и бэктиков): пункт отчёта один, и второй шаблон пункта разошёлся бы
    с первым на живых отчётах.
    """
    stripped = str(line).strip()
    if stripped[:2] in ("- ", "* "):
        text = stripped[2:]
    elif stripped[:1].isdigit() and stripped[1:3] in (". ", ") "):
        text = stripped[3:]
    else:
        return ""
    text = text.replace("`", "").replace("**", "").replace("__", "").strip(" .;")
    return text if len(text) >= 12 else ""


def scan_report_findings(text) -> tuple[bool, list[dict]]:
    """Раздел находок отчёта: найден ли он и что в нём за пункты (план 13, S1, §4.1).

    Правила:

    - область — раздел, в заголовке которого есть «наход» и нет «закрыт» (регистр не важен);
      заголовок-класс («Блокирующие», «Значимые», «Стилистические») внутри области задаёт
      класс пунктам без своей метки;
    - отчёт в стиле подразделов-классов БЕЗ родительского заголовка «Находки» тоже считается
      областью: это живая форма (её держит фикстура `tests/engine/test_comments.py`, отбор
      блокирующих из которой закреплён контрактом комментария карточки правок). Иначе
      правило останавливало бы отчёты, уже размеченные по классам, и решение расходилось бы
      с отбором блокирующих;
    - пункты — правила `_report_items`, но только внутри области;
    - метка — фрагмент в квадратных скобках в начале пункта; класс — до запятой или дроби по
      основе слова, подметка после дроби — отдельным полем.

    Возврат `(найден, пункты)`, пункт — `{"text", "class", "sublabel", "label"}`: значение, а
    не «есть строка». Второй шаблон пункта рядом с `_report_items` намеренный — та функция
    держит отбор блокирующих для комментария карточки правок (S2 плана 2), и переписывать её
    под счёт классов значило бы менять поведение комментария.
    """
    items: list[dict] = []
    section = False
    inside = False
    level = 0
    group = ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            depth = len(line) - len(line.lstrip("#"))
            low = line.lstrip("#").strip().lower()
            if FINDING_SECTION_WORD in low and FINDING_CLOSED_WORD not in low:
                section = inside = True
                level, group = depth, ""
                continue
            cls = _finding_class(low)
            if cls:
                # Подразделы-классы и есть область, даже если родительского заголовка нет.
                if not inside:
                    section = inside = True
                    level = depth - 1
                group = cls
                continue
            if inside and depth <= level:
                inside, group = False, ""
            continue
        if not inside:
            continue
        item_text = _finding_item_text(line)
        if not item_text:
            continue
        match = FINDING_LABEL_RE.match(item_text)
        cls, sublabel = _finding_label(match.group(1)) if match else ("", "")
        body = item_text[match.end():].strip(" —-;:") if match else item_text
        items.append({"text": item_text, "body": body, "class": cls or group,
                      "sublabel": sublabel, "label": match.group(1) if match else ""})
    return section, items


def report_finding_classes(text) -> dict:
    """Счёт находок отчёта по классам — единственный источник сверки вердикта (13-S1).

    Ключи: `blocking`, `significant`, `style` — счёт по трём классам; `labeled` — их сумма;
    `unlabeled` — пункты без метки словаря (в том числе с меткой статуса `[закрыта]`);
    `sublabels` — счёт подметок после дроби; `section` — найден ли раздел находок; `read` —
    удалось ли прочитать отчёт.

    `text=None` — отчёт не прочитан: счёт пуст, `read` ложно. Функция не бросает и не читает
    путей: чтение — на вызывающей стороне (`report_finding_classes_at`), как у реестра.
    """
    empty = {"blocking": 0, "significant": 0, "style": 0, "labeled": 0, "unlabeled": 0,
             "sublabels": {}, "section": False, "read": False}
    if text is None:
        return empty
    section, items = scan_report_findings(text)
    counts = {name: 0 for _stem, name in FINDING_CLASS_STEMS}
    unlabeled = 0
    sublabels: dict[str, int] = {}
    for item in items:
        cls = item["class"]
        if cls not in counts:
            unlabeled += 1
            continue
        counts[cls] += 1
        sublabel = item["sublabel"]
        if sublabel:
            sublabels[sublabel] = sublabels.get(sublabel, 0) + 1
    return {"blocking": counts["блокирующая"], "significant": counts["значимая"],
            "style": counts["стилистическая"], "labeled": sum(counts.values()),
            "unlabeled": unlabeled, "sublabels": sublabels,
            "section": section, "read": True}


def report_finding_classes_at(path) -> dict:
    """Счёт классов по файлу отчёта: нечитаемый файл — `read` ложно, без исключения."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        text = None
    return report_finding_classes(text)


def report_remarks(text) -> list[dict]:
    """Замечания отчёта вне блокирующих — строки реестра плана при закрытии шага (§4.3).

    Замечание — пункт класса «значимая» или «стилистическая»: блокирующая держит шаг и
    в реестр закрытия не попадает, неразмеченный пункт класса не имеет (его считает
    `unlabeled`, и в реестр он идёт только если у него есть класс).
    """
    if text is None:
        return []
    _section, items = scan_report_findings(text)
    return [item for item in items if item["class"] in ("значимая", "стилистическая")]


def _review_findings(path: Path, limit: int = 5) -> list[str]:
    """Выжимка замечаний из отчёта ревью: пункты списков без воды."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: list[str] = []
    for item, _ in _report_items(text):
        short = item[:180]
        if short not in items:
            items.append(short)
        if len(items) >= limit:
            break
    return items


def _blocking_findings(path: Path, limit: int = 5) -> list[str]:
    """Блокирующие находки отчёта: метка в самом пункте ИЛИ подраздел «Блокирующие»."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: list[str] = []
    for item, under_blocking in _report_items(text):
        if not under_blocking and "блокирующ" not in item.lower():
            continue
        short = item[:180]
        if short not in items:
            items.append(short)
        if len(items) >= limit:
            break
    return items


def _review_recommendations(path: Path, limit: int = 3) -> list[str]:
    """Раздел «Рекомендации» из отчёта ревью: что владельцу делать, а не что не так."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    picked: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            inside = "рекомендац" in line.lower()
            continue
        if not inside or not line:
            continue
        if line[:2] in ("- ", "* ") or (line[:1].isdigit() and line[1:3] in (". ", ") ")):
            item = (line[2:] if line[:2] in ("- ", "* ") else line[3:]).strip()
            item = item.replace("`", "").strip(" .;")
            if len(item) >= 8 and item not in picked:
                picked.append(item[:200])
        if len(picked) >= limit:
            break
    return picked


def _step_summary(step: dict, limit: int = 260) -> str:
    """Суть шага одной фразой: цель, а если её нет — критерий готовности."""
    def norm(text: str) -> str:
        return " ".join(str(text or "").replace("`", "").split())

    for source in ("goal", "dod"):
        text = norm(step.get(source))
        if not text:
            continue
        cut = text.find(". ")
        if 0 < cut < limit:
            return text[:cut + 1]
        return text[:limit].rstrip(" ,;") + ("…" if len(text) > limit else "")
    return norm(step.get("title"))[:limit]

def status_text(ctx: dict, decision: dict) -> str:
    step = ctx.get("next") or {}
    plan_id = ctx.get("plan_id") or "?"
    lines = [
        f"План {plan_id}: следующий открытый шаг — {step.get('id') or 'нет'} {step.get('title') or ''}",
        f"Закрыто в журнале: {', '.join(ctx.get('closed') or []) or '—'}",
        f"Карточек конвейера: {len(ctx.get('cards') or [])}",
    ]
    for c in ctx.get("cards") or []:
        lines.append(f"  - {c['id']} {c['status']:<10} {c['key']}")
    # Риск и ручной допуск видны всегда: по ним владелец решает, снимать ли hold.
    hold = [str(h).upper() for h in (ctx.get("hold") or [])]
    lines.append(f"Ручной допуск (hold): {', '.join(hold) or '—'}")
    # Шага нет — и риска нет: «normal» у пустой строки читается как «риск обычный».
    lines.append(f"Риск шага: {((step.get('risk') or 'normal') if step else '—')}")
    # Прогоны шага (план 9, S2): строка видна всегда, даже в простое: по ней владелец
    # решает, гоняются ли прогоны и чем они заканчиваются. Формат — та же строка-счётчик,
    # что в журнале, уведомлении и карточке (`runs_counter`), источник — доказательство.
    counter = runs_counter(ctx.get("step_runs"))
    if counter:
        lines.append(f"Прогоны шага: {counter}")
    # Пересборки постановки по свежести видны всегда (план 12, S1): по цифре владелец решает,
    # поднимать ли лимит. При нуле строка тоже есть — иначе счётчик невидим.
    if step:
        rebuilds = _prompt_stale_cards(ctx, str(step.get("id") or ""))
        lines.append("Пересборок постановки по свежести: "
                     f"{len(rebuilds)} из {int(ctx.get('max_stale_rebuilds') or 0)}")
    if ctx.get("verdict"):
        v = ctx["verdict"]
        lines.append(f"Вердикт: {v['verdict']} (строк: {v['occurrences']}, "
                     f"коммит: {v['commit'] or '—'})")
    else:
        lines.append("Вердикт: отчёта нет")
    if ctx.get("prev_verdict"):
        lines.append(f"Прошлый закрытый шаг {ctx.get('prev_closed')}: "
                     f"вердикт {ctx['prev_verdict']['verdict']}")
    if ctx.get("stop"):
        lines.append(f"СТОП-ФЛАГ: {ctx['stop'].get('reason')} (с {ctx['stop'].get('at')})")
    # Исход идеи виден владельцу всегда (план 5, S10): по нему решается, идёт план или нет.
    kind = str(ctx.get("plan_decision") or "")
    lines.append(f"Исход разведки: {kind} — {ctx.get('plan_decision_note') or 'без пояснения'}"
                 if kind else "Исход разведки: не назван")
    lines.append(f"Решение: {decision['action']} — {decision.get('reason', '')}")
    return "\n".join(lines)


# ───────────────────────────── побочные действия ─────────────────────────────


def report_stalls(p: Paths, ctx: dict) -> int:
    """Сказать в карточки шага о залипших инструментах по журналу метрики; вернуть число.

    Тик не останавливается (§4 п.2): залипание — довод сменить метод, а не повод встать.
    Комментарий уходит один раз на прогон: отметка `run_id` живёт в состоянии плана
    (`stall_reported`), поэтому повторный тик по той же строке молчит (Д7). Шага нет или
    состояния нет — молчим: адресовать находку некуда.
    """
    step = ctx.get("step") or ctx.get("next") or {}
    step_id = str(step.get("id") or "")
    state = ctx.get("state")
    if not step_id or not isinstance(state, dict):
        return 0
    reported = [str(item) for item in (state.get("stall_reported") or [])]
    sent = 0
    for row in step_metrics.read_rows(step_metrics.metrics_path(p.cfg)):
        if not isinstance(row, dict) or not row.get("stall"):
            continue
        if (str(row.get("plan") or "") != str(p.plan_id)
                or str(row.get("step") or "") != step_id):
            continue
        run_id = str(row.get("run_id") or "")
        card_id = str(row.get("card") or "")
        if not run_id or not card_id or run_id in reported:
            continue
        # Отметка ставится и при отказе CLI: иначе недоступная борда превратила бы
        # залипание в поток попыток на каждом тике, а причина уже в stderr (`[comment]`).
        _comment(p, card_id, stall_line(row))
        reported.append(run_id)
        sent += 1
    if sent:
        state["stall_reported"] = reported[-STALL_KEEP:]
        save_state(p, state)
    return sent


def _comment(p: Paths, card_id: str, text: str) -> bool:
    """Комментарий в карточку борды. Отказ — предупреждение `[comment]`, не исключение (S2)."""
    # Тело уходит одной строкой: обёртка CLI `.cmd` на Windows режет командную строку по
    # переводу строки и молча теряет хвост, а тела из файла у `comment` нет. Пункты
    # разделяются «; » — содержание и адреса находок сохраняются полностью.
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "; ".join(line for line in lines if line)
    if not card_id or not text:
        return False
    cmd = [_hermes(), "kanban", "--board", p.cfg.board, "comment", card_id, text,
           "--author", COMMENT_AUTHOR, "--max-len", str(COMMENT_MAX_LEN)]
    try:
        res = _run(cmd, Path(p.repo), timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        # Нет `hermes` в PATH или команда не ответила: карточка уже создана, тик живёт.
        print(f"[comment] не отправлен в {card_id}: {str(exc)[:300]}", file=sys.stderr)
        return False
    if res.returncode != 0:
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        print(f"[comment] не отправлен в {card_id}: {out[:300]}", file=sys.stderr)
        return False
    return True


CARD_VISIBLE_TRIES = 6
CARD_VISIBLE_PAUSE = 0.25


def _card_visible(p: Paths, card_id: str, key: str) -> bool:
    """Карточка видна на борде по своему ключу — с короткими повторами.

    `hermes kanban create` печатает id после записи, но строка может стать видимой
    чуть позже (чужой процесс, отдельная база, кэш). Ожидание ограничено шестью
    попытками по 0.25 с: страж остаётся детерминированным, а не «подожди и посмотрим».
    """
    for attempt in range(CARD_VISIBLE_TRIES):
        if card_status(p, card_id, key):
            return True
        if attempt + 1 < CARD_VISIBLE_TRIES:
            time.sleep(CARD_VISIBLE_PAUSE)
    return False


def _create_card(p: Paths, title: str, body: str, assignee: str, key: str,
                 skills: tuple[str, ...], parent: str | None = None,
                 max_runtime: str | None = None) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        body_path = fh.name
    cmd = [_hermes(), "kanban", "--board", p.cfg.board,
           "create", title, "--body-file", body_path,
           "--assignee", assignee, "--tenant", p.cfg.tenant,
           "--workspace", f"dir:{Path(p.repo).as_posix()}",
           "--idempotency-key", key,
           "--max-runtime", max_runtime or p.cfg.step_runtime]
    for skill in skills:
        cmd += ["--skill", skill]
    if parent:
        cmd += ["--parent", parent]
    res = _run(cmd, Path(p.repo), timeout=180)
    out = (res.stdout or "") + (res.stderr or "")
    m = re.search(r"t_[0-9a-f]{6,}", out)
    if res.returncode != 0 and not m:
        print(f"[card] не создано: {out.strip()[:300]}", file=sys.stderr)
        return None
    if not m:
        return None
    cid = m.group(0)
    # Заказ без карточки — тихая потеря хода конвейера: на борде карточки нет, страж
    # видит пустоту, план стоит, а в журнале тиков «заказано». Раньше id из вывода CLI
    # считался доказательством; при сбое записи (гонка за базу, чужая борда) это давало
    # зависание на часы и мигающий тест кругов постановки. Теперь это сбой с ключом.
    if not _card_visible(p, cid, key):
        raise GateError(
            f"карточка {cid} заказана, но борды её не знает (ключ {key}, борда "
            f"{p.cfg.board}): строки нет в {p.kanban_db} — план не двигаю, пока карточки "
            f"нет; вывод CLI: {out.strip()[:200]}",
            plan_id=p.plan_id)
    return cid


TG_USER_AGENT = "step-pipeline (+https://github.com/SMHotey/hermes-step-pipeline)"


def tg_api_base() -> str:
    """Хост Bot API: прокси из окружения, иначе прямой Telegram.

    Свой ключ конвейера сильнее общего ключа Hermes (`TELEGRAM_API_BASE_URL`): там прокси
    уже настроен для шлюза, и уведомления обязаны ходить тем же путём. Прокси-воркер
    (Cloudflare) отдаёт те же пути `/bot<токен>/<метод>` — подменяется только хост.
    База, оканчивающаяся на `/bot` (так её пишет шлюз Hermes), префикс не дублирует:
    вызывающий код дописывает `/bot<токен>/<метод>` сам.
    """
    base = (os.environ.get("STEP_PIPELINE_TELEGRAM_API_BASE")
            or os.environ.get("TELEGRAM_API_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return "https://api.telegram.org"
    return base[: -len("/bot")] if base.endswith("/bot") else base


# Python без явного набора корней доверяет хранилищу ОС, а в нём бывают просроченные
# сертификаты (в Windows — DST Root CA X3 и другие старые корни): OpenSSL строит через
# них просроченный путь для Let's Encrypt, и уведомление падает с
# `CERTIFICATE_VERIFY_FAILED: certificate has expired`. Корни берём из явного набора.
CA_ENV_VARS = ("STEP_PIPELINE_CA_BUNDLE", "HERMES_CA_BUNDLE", "SSL_CERT_FILE",
               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def ca_bundle() -> str:
    """Путь к набору корневых сертификатов; пусто — доверять хранилищу ОС."""
    for name in CA_ENV_VARS:
        path = (os.environ.get(name) or "").strip()
        if path and os.path.isfile(path):
            return path
    try:
        import certifi
    except ImportError:
        return ""
    path = certifi.where()
    return path if os.path.isfile(path) else ""


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Контекст TLS с найденным набором корней (строится один раз на процесс)."""
    bundle = ca_bundle()
    return ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()


def notify(cfg: config_mod.Config, text: str) -> bool:
    """Уведомление владельцу. Выключено — печатаем (cron --deliver покажет в выводе)."""
    prefix = str(cfg.notify.get("prefix") or "")
    msg = f"{prefix}{text}" if prefix else text
    if not cfg.notify.get("enabled"):
        # stderr, не stdout: `--json` должен остаться чистым JSON для UI и инструментов
        print(f"[notify] {msg}", file=sys.stderr)
        return True
    token = os.environ.get(str(cfg.notify.get("token_env") or "PIPELINE_TG_TOKEN"))
    chat = os.environ.get(str(cfg.notify.get("chat_env") or "PIPELINE_TG_CHAT"))
    if not token or not chat:
        print(f"[notify] нет токена/чата, сообщение не отправлено: {msg}", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg,
                                   "disable_web_page_preview": "true"}).encode()
    url = f"{tg_api_base()}/bot{token}/sendMessage"
    request = urllib.request.Request(url, data=data, headers={"User-Agent": TG_USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20, context=ssl_context()) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError) as exc:
            if attempt == 2:
                print(f"[notify] не доставлено: {exc}", file=sys.stderr)
        time.sleep(2)
    return False


# ───────────────── тревога о зависшем конвейере ─────────────────

def _attention_path(cfg: config_mod.Config) -> Path:
    """Файл отметок тревог: `<state_dir>/attention.json` (общий на все планы)."""
    return Path(cfg.state_dir) / ATTENTION_FILE


def attention_text(plan_id: str, what: str, *, evidence=(), todo=(), commands=()) -> str:
    """Текст тревоги: громкий заголовок, что случилось, что делать, где смотреть.

    Одна форма на все случаи зависания: владелец узнаёт аварию по первой строке, а не
    по вчитанному тексту. Подробности — списком, команды — отдельным блоком.
    """
    lines = [ATTENTION_HEADER, ""]
    lines.append(f"План {plan_id}: {what}" if plan_id else what)
    if evidence:
        lines += ["", "Что случилось:"]
        lines += [f"- {item}" for item in evidence]
    if todo:
        lines += ["", "Что нужно сделать:"]
        lines += [f"{number}. {item}" for number, item in enumerate(todo, 1)]
    if commands:
        lines += ["", "Команды:"]
        lines += [f"  {cmd}" for cmd in commands]
    lines += ["", f"Время: {_local_stamp(_now())}"]
    return "\n".join(lines)


def _attention_seen(cfg: config_mod.Config) -> dict:
    """Прочитанные отметки тревог; битый или чужой файл — пустая карта, не сбой тика."""
    path = _attention_path(cfg)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def attention_ready(cfg: config_mod.Config, reason: str,
                    gap: float = ATTENTION_GAP) -> bool:
    """Пора ли тревожить владельца по этой причине: одна тревога на причину в `gap`.

    Отметка пишется сразу: повторный тик с той же причиной молчит, даже если Телеграм
    не ответил — иначе cron засыпал бы чат одним и тем же сообщением.
    """
    if not cfg.notify.get("enabled"):
        return True                    # выключенные уведомления печатаются в stderr
    now = _now()
    data = {key: value for key, value in _attention_seen(cfg).items()
            if isinstance(value, (int, float)) and now - float(value) < 86400}
    last = data.get(reason)
    fresh = not isinstance(last, (int, float)) or now - float(last) >= gap
    if fresh:
        data[reason] = now
        try:
            state_io.save_json(_attention_path(cfg), data)
        except (OSError, ValueError, TypeError) as exc:
            print(f"[attention] отметка не записана: {exc}", file=sys.stderr)
    return fresh


def attention(cfg: config_mod.Config, text: str, reason: str, say=None,
              gap: float = ATTENTION_GAP) -> bool:
    """Отправить тревогу владельцу (с оглядкой на частоту). True — сообщение ушло."""
    if not attention_ready(cfg, reason, gap=gap):
        return False
    (say or (lambda value: notify(cfg, value)))(text)
    return True


def stuck_repeats_of(cfg: config_mod.Config) -> int:
    """Сколько одинаковых действий подряд считать зависанием (`notify.stuck_repeats`)."""
    raw = cfg.notify.get("stuck_repeats")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return STUCK_REPEATS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return STUCK_REPEATS_DEFAULT
    return value if value >= 2 else STUCK_REPEATS_DEFAULT


# ───────────────── сторож простоя и дневная сводка ─────────────────

WATCHDOG_DEFAULT_HOURS = 3          # умолчание §S9, ключ notify.watchdog_hours
WATCHDOG_NOTICE_GAP = 3600          # одно уведомление на причину — не чаще раза в час
DIGEST_DEFAULT = "09:00"            # умолчание §S9, ключ notify.digest_at
DIGEST_FILE = "digest.json"         # отметка «сводка за сутки отправлена» (как tg_offset.json)


def _now() -> float:
    """Текущее время (epoch). Единственное чтение часов в новом коде.

    Отдельная функция, а не time.time() на месте: тесты подменяют gate._now, и без
    единой точки подмены «сутки простоя» иначе не проверить.
    """
    return time.time()


def _local_date(ts: float) -> str:
    """Дата от переданного момента: формат журнала, 02.11.2026."""
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def _local_stamp(ts: float) -> str:
    """Момент для сообщения владельцу: 02.11.2026 03:00."""
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


def _ago(seconds: float) -> str:
    """Длительность по-русски: «4 ч 10 мин», «4 ч», «45 мин», «меньше минуты»."""
    total = max(0, int(seconds))
    hours, minutes = total // 3600, total % 3600 // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _positive(raw, default: float) -> float:
    """Число из pipeline.json; битое или неположительное значение — умолчание (D4)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_actions_of(cfg: config_mod.Config) -> int:
    """Предел действий за тик из pipeline.json; нет ключа или значение битое — 1 (D1).

    Ключ читается сырым словарём конфига (`cfg.state`, D3): типизированного поля в
    `Config` нет, и шаг не выходит за свои файлы. Исключений не бросает — битый конфиг
    не должен валить тик (сравни `_positive`).
    """
    raw = (cfg.state or {}).get("max_actions")
    if raw is None or isinstance(raw, bool):
        return MAX_ACTIONS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_ACTIONS_DEFAULT
    return value if value >= 1 else MAX_ACTIONS_DEFAULT


def _config_section(cfg: config_mod.Config, name: str) -> dict:
    """Раздел сырого `pipeline.json` словарём; нет раздела или он не словарь — пусто (D3)."""
    value = (cfg.state or {}).get(name)
    return value if isinstance(value, dict) else {}


def _config_text(cfg: config_mod.Config, section: str, key: str = SCOUT_KEY) -> str:
    """Непустая строка из раздела конфига; битое значение — пустая строка (D3).

    `key` — параметр, а не константа: у ролей плана ключ свой (D8 — разведка зовёт
    функцию без третьего аргумента и её поведение не меняется).
    """
    raw = _config_section(cfg, section).get(key)
    return raw.strip() if isinstance(raw, str) else ""


def scout_profile(cfg: config_mod.Config) -> str:
    """Профиль исполнителя разведки: `profiles.scout`, умолчание `scout` (D3).

    Ключ принадлежит владельцу (`AGENTS.md`) — шаг его только читает. Пустое или
    нестроковое значение даёт умолчание, без исключений (сравни `max_actions_of`).
    """
    return _config_text(cfg, "profiles") or SCOUT_PROFILE_DEFAULT


def scout_skills(cfg: config_mod.Config) -> tuple[str, ...]:
    """Скиллы карточки разведки: `skills.scout`; не список, пустые элементы — умолчание (D3)."""
    raw = _config_section(cfg, "skills").get(SCOUT_KEY)
    if not isinstance(raw, (list, tuple)):
        return SCOUT_SKILLS_DEFAULT
    items = tuple(str(item).strip() for item in raw
                  if isinstance(item, str) and str(item).strip())
    return items or SCOUT_SKILLS_DEFAULT


def scout_runtime(cfg: config_mod.Config) -> str:
    """Лимит времени карточки разведки: `runtimes.scout`, умолчание `30m` (D3)."""
    return _config_text(cfg, "runtimes") or SCOUT_RUNTIME_DEFAULT


def _config_list(cfg: config_mod.Config, section: str, key: str,
                 default: tuple[str, ...]) -> tuple[str, ...]:
    """Список скиллов из раздела конфига; не список или пустые элементы — умолчание (D3).

    Общий читатель для ролей ревью и правки плана: у разведки остался свой (S2),
    чтобы шаг S5 не менял поведение закрытого шага.
    """
    raw = _config_section(cfg, section).get(key)
    if not isinstance(raw, (list, tuple)):
        return default
    items = tuple(str(item).strip() for item in raw
                  if isinstance(item, str) and str(item).strip())
    return items or default


def plan_review_profile(cfg: config_mod.Config) -> str:
    """Профиль ревью плана: `profiles.plan_reviewer`, иначе `cfg.reviewer` (D3).

    Отдельного профиля в проекте может не быть, а карточка с неизвестным assignee
    молча висит: поэтому умолчание — штатный ревьюер, а вид ревью различает скилл.
    """
    return _config_text(cfg, "profiles", PLAN_REVIEW_PROFILE_KEY) or cfg.reviewer


def plan_review_skills(cfg: config_mod.Config) -> tuple[str, ...]:
    """Скиллы карточки ревью плана: `skills.plan_review`, умолчание — роль ревью плана."""
    return _config_list(cfg, "skills", "plan_review", PLAN_REVIEW_SKILLS_DEFAULT)


def plan_review_runtime(cfg: config_mod.Config) -> str:
    """Лимит времени карточки ревью плана: `runtimes.plan_review`, умолчание `45m`."""
    return _config_text(cfg, "runtimes", "plan_review") or PLAN_REVIEW_RUNTIME_DEFAULT


def plan_fix_profile(cfg: config_mod.Config) -> str:
    """Профиль правки плана: `profiles.plan_fixer`, иначе исполнитель плана (D3).

    План правит тот, кто его написал: отдельного профиля в шаблоне нет, ключ владельческий.
    """
    return _config_text(cfg, "profiles", PLAN_FIX_PROFILE_KEY) or cfg.driver


def plan_fix_skills(cfg: config_mod.Config) -> tuple[str, ...]:
    """Скиллы карточки правки плана: `skills.plan_fix`, умолчание — роль автора плана."""
    return _config_list(cfg, "skills", "plan_fix", PLAN_FIX_SKILLS_DEFAULT)


def plan_fix_runtime(cfg: config_mod.Config) -> str:
    """Лимит времени карточки правки плана: `runtimes.plan_fix`, умолчание `45m`."""
    return _config_text(cfg, "runtimes", "plan_fix") or PLAN_FIX_RUNTIME_DEFAULT


def plan_checks(p: Paths, plan_text: str) -> list[tuple[str, str, str]]:
    """Проверки корректировки плана по разведке P1-P4 для этого проекта (0.20.0).

    Номер плана и каталог разведки передаются явно: P4 сверяет ссылку раздела с
    артефактом ЭТОГО плана, а `paths.research` в проекте может быть своим.
    """
    research = p.cfg.rel(scout_research_dir(p.cfg))
    return plan_cycle.check_plan(plan_text, p.plan_id, research)


def _digest_at(cfg: config_mod.Config) -> tuple[int, int]:
    """Час и минута сводки из notify.digest_at; битое значение — умолчание (D4)."""
    raw = str(cfg.notify.get("digest_at") or DIGEST_DEFAULT).strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not match:
        match = re.match(r"^(\d{1,2}):(\d{2})$", DIGEST_DEFAULT)
    hour, minute = int(match.group(1)), int(match.group(2))
    return (hour, minute) if hour < 24 and minute < 60 else (9, 0)


def watch_idle(cfg: config_mod.Config, p: Paths, ctx: dict, decision: dict,
               say=None) -> dict:
    """Простой: запомнить ожидание и разбудить владельца, если ждём дольше watchdog_hours.

    waiting_since ставится один раз за полосу простоя и снимается первым же действием
    (см. touch). Уведомление — одно на причину, не чаще раза в час (D6). Закрытый план
    простоем не считается: там работы нет по определению (D10).
    """
    if decision.get("plan_done"):
        return {"notice": False, "waiting_since": None}
    state = ctx.get("state") or {}
    now = _now()
    reason = str(decision.get("reason") or "нет работы")
    started = not isinstance(state.get("waiting_since"), (int, float))
    since = float(now if started else state["waiting_since"])
    changed = state.get("waiting_reason") != reason
    state["waiting_since"] = since
    state["waiting_reason"] = reason
    hours = _positive(cfg.notify.get("watchdog_hours"), WATCHDOG_DEFAULT_HOURS)
    notice_at = state.get("watchdog_notice_at")
    gap_passed = (not isinstance(notice_at, (int, float))
                  or now - float(notice_at) >= WATCHDOG_NOTICE_GAP)
    notice = now - since >= hours * 3600 and (changed or gap_passed)
    if notice:
        text = (f"⏳ простой {_ago(now - since)}: {reason} "
                f"(план {p.plan_id}, ждём с {_local_stamp(since)})")
        (say or (lambda value: notify(cfg, value)))(text)
        state["watchdog_notice_at"] = now
        state["watchdog_notice_reason"] = reason
    if started or changed or notice:
        save_state(p, state)
    return {"notice": notice, "waiting_since": since}


APPROVAL_NOTICE_KEY = registry.APPROVAL_NOTICE_KEY


def inherit_approval(p: Paths, decision: dict) -> dict:
    """Записать унаследованную запись утверждения для уже идущего плана.

    Ключи — те же, что у `plan_accept._write_approval`, плюс `inherited`: два формата
    одной записи недопустимы. Прежние поля сохраняются: `review_reset_at` — момент
    явного решения владельца, и без него бюджет кругов ревью снова считался бы за всю
    жизнь плана (правка плана обнуляла метку и упирала цикл в owner-stop до повторного
    ревью).

    Параметр называется `decision`, а не `approval`: модуль `approval` нужен здесь же
    (`plan_version_sha`), и одноимённая переменная его затеняет.
    """
    path = registry.approval_file_for(p.cfg, p.plan_id)
    previous: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            previous = loaded
    except (OSError, ValueError):
        previous = {}
    if not previous:
        known = (decision.get("record") or {})
        previous = dict(known) if isinstance(known, dict) else {}
    record = {
        **previous,
        "plan": str(p.plan_id),
        "file": p.cfg.rel(Path(p.plan_file)),
        "sha256": decision.get("sha256") or registry.sha256_file(p.plan_file),
        # Версия плана без журнала (0.22.1): по ней сверяется утверждение, иначе закрытие
        # шага («открыт» → «ЗАКРЫТ») выглядело правкой плана и требовало переутверждения.
        "version_sha256": (decision.get("version_sha256")
                           or approval.plan_version_sha(p.plan_file,
                                                        approval.journal_marker_of(p.cfg))),
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "profile": os.environ.get("HERMES_PROFILE") or "default",
        "inherited": True,
    }
    try:
        state_io.save_json(path, record)
    except OSError as exc:
        raise GateError(f"запись утверждения не пишется: {path}: {exc}", p.plan_id) from exc
    return record


def ensure_approval(p: Paths, ctx: dict) -> dict | None:
    """Дочитать утверждение для идущего плана: одна запись и одна строка в stdout.

    Только исполняющий проход (`--run`): `--status` состояние не меняет. Повторный
    вызов после записи ничего не пишет — в ctx уже `approved`.

    Наследуется запись идущего плана, чтобы шаг не перекрывал дорогу сам себе. Исключение
    одно: план правился карточкой правки по разведке (0.20.0) — там утверждение обновляет
    владелец (`hermes pipeline plan --accept N`), и наследование подделало бы его молча.
    """
    approval = ctx.get("approval") or {}
    if approval.get("state") == "approved" or not approval.get("started"):
        return None
    if ctx.get("scout_fix_cards"):
        return None
    record = inherit_approval(p, approval)
    approval.update(state="approved", inherited=True, record=record, reason="")
    ctx["approval"] = approval
    print(f"план {p.plan_id}: запись утверждения унаследована "
          f"(inherited, sha256 {str(record.get('sha256') or '')[:12]})")
    return record


def approval_notice(p: Paths, ctx: dict, decision: dict, say=None) -> bool:
    """Одно уведомление владельцу на план о пропуске по утверждению.

    Отметка в состоянии плана (`approval_notice`) не зависит от `notify.enabled` —
    так же работают сторож простоя и сводка. `touch()` этот ключ не снимает, поэтому
    «одно на план» держится и после действий по другим шагам.
    """
    if not decision.get("approval_blocked"):
        return False
    state = ctx.get("state") or {}
    if state.get(APPROVAL_NOTICE_KEY):
        return False
    reason = str(decision.get("reason") or "план не утверждён")
    (say or (lambda text: notify(p.cfg, text)))(
        f"⛔ план {p.plan_id} не ведётся: {reason}")
    state[APPROVAL_NOTICE_KEY] = {
        "at": time.strftime("%d.%m.%Y %H:%M:%S"),
        "state": decision.get("approval") or "missing",
        "reason": reason[:300],
    }
    ctx["state"] = state
    save_state(p, state)
    return True


def digest_rows(cfg: config_mod.Config) -> list[dict]:
    """Строка сводки по каждому ведомому плану: закрыто за сутки, ожидание, стоп.

    «Закрыто за сутки» — карточки шагов со completed_at в последних 24 часах (D8):
    дата журнала хранится без времени и для суточного окна не годится.
    """
    now = _now()
    rows: list[dict] = []
    for spec in registry.driven_specs(cfg):
        p = resolve_paths(cfg, spec.id)
        state = load_state(p)
        closed = 0
        for card in read_cards(p, f"planloop:{spec.id}:"):
            done_at = float(card.get("completed_at") or 0)
            if (str(card.get("key") or "").endswith(":step")
                    and card.get("status") == "done" and 0 <= now - done_at <= 86400):
                closed += 1
        rows.append({"id": spec.id, "closed": closed,
                     "waiting_since": state.get("waiting_since"),
                     "waiting_reason": state.get("waiting_reason"),
                     "stop": load_stop(p)})
    return rows


def digest_text(cfg: config_mod.Config, rows: list[dict]) -> str:
    """Текст суточной сводки: шапка, сколько закрыто, строка на план."""
    now = _now()
    lines = [f"📊 сводка за сутки, {_local_date(now)}",
             f"закрыто за сутки: {sum(row['closed'] for row in rows)}"]
    for row in rows:
        waiting = "ничего"
        if isinstance(row.get("waiting_since"), (int, float)):
            waiting = (f"{row.get('waiting_reason') or 'ждём'} "
                       f"({_ago(now - float(row['waiting_since']))})")
        stop = "нет"
        if row.get("stop"):
            stop = f"{stop_kind(row['stop'])}: {row['stop'].get('reason', '')}"
        lines.append(f"план {row['id']}: закрыто {row['closed']}, "
                     f"ждёт — {waiting}, стоп — {stop}")
    return "\n".join(lines)


def send_digest(cfg: config_mod.Config, say=None) -> bool:
    """Суточная сводка: одна на проект, после notify.digest_at, отметка в state_dir (D2, D7)."""
    marker = Path(cfg.state_dir) / DIGEST_FILE
    try:
        sent = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    now = _now()
    today = _local_date(now)
    if isinstance(sent, dict) and sent.get("date") == today:
        return False
    local = time.localtime(now)
    if (local.tm_hour, local.tm_min) < _digest_at(cfg):
        return False
    rows = digest_rows(cfg)
    if not rows:                       # ведомых планов нет — сообщать нечего
        return False
    (say or (lambda value: notify(cfg, value)))(digest_text(cfg, rows))
    state_io.save_json(marker, {"date": today, "at": _local_stamp(now)})
    return True


# ───────────────────── команды владельцу (Telegram, вход) ─────────────────────

TG_TEXT_LIMIT = 3900
# action `typing` в Telegram живёт около 5 секунд — обновляем чаще.
TYPING_REFRESH = 4.0

TG_HELP = (
    "Команды конвейера:\n"
    "/status — состояние: решение стража, стоп-флаг и таблица планов\n"
    "/plans — только таблица планов\n"
    "/stop [причина] — остановиться после текущего шага\n"
    "/resume — снять стоп-флаг\n"
    "/hold S3 — шаг требует ручного допуска\n"
    "/unhold S3 — снять ручной допуск\n"
    "/answer Q-1 A — ответ на вопрос шага (без ответа рекомендация берётся по таймауту)\n"
    "/reset — начать диалог с агентом заново\n"
    "Свободный текст без «/» уходит агенту: он ответит в этот же чат.\n"
    "Слишком частые сообщения не обрабатываются (защита от флуда).\n"
    "Работают только из чата, указанного в настройках."
)


def _tg_call(token: str, method: str, http_timeout: int = 20, **params) -> dict:
    """Один вызов Bot API. Любой сбой — {"ok": False, "description": ...}, без исключений."""
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    url = f"{tg_api_base()}/bot{token}/{method}"
    request = urllib.request.Request(url, data=body, headers={"User-Agent": TG_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=http_timeout,
                                    context=ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return data if isinstance(data, dict) else {"ok": False, "description": "пустой ответ"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"ok": False, "error_code": exc.code, "description": detail}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "description": str(exc)[:200]}


def tg_target(cfg: config_mod.Config) -> tuple[str, str] | None:
    """Токен и чат из окружения (имена переменных — из pipeline.json)."""
    token = os.environ.get(str(cfg.notify.get("token_env") or "PIPELINE_TG_TOKEN"))
    chat = os.environ.get(str(cfg.notify.get("chat_env") or "PIPELINE_TG_CHAT"))
    if not token or not chat:
        return None
    return str(token), str(chat)


def commands_enabled(cfg: config_mod.Config) -> bool:
    """Приём команд: по умолчанию включён там, где включены уведомления."""
    commands = cfg.notify.get("commands")
    if isinstance(commands, dict):
        return bool(commands.get("enabled", True))
    return bool(cfg.notify.get("enabled"))


def _offset_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / "tg_offset.json"


def _read_offset(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("offset") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def _write_offset(path: Path, offset: int) -> None:
    try:
        state_io.save_json(path, {"offset": int(offset)})
    except OSError as exc:
        print(f"[commands] offset не сохранён: {exc}", file=sys.stderr)


# ───────────────────────── диалог с агентом (S4) ─────────────────────────

DIALOG_STATE_NAME = "tg_dialog.json"
COMMANDS_OFF_NAME = "tg_commands_off.json"
RATE_STATE_NAME = "tg_rate.json"
RATE_DEFAULTS = {"max": 5, "window": 60}

# Умолчания берутся из `notify.dialog` (config.py), чтобы числа шага жили в одном месте:
# сюда попадает только запасной вариант для конфига, собранного до S4 (ключа `dialog` там
# ещё нет), и `DEFAULTS` остаётся единственным источником значений.
DIALOG_DEFAULTS = {
    "enabled": True,
    "max_turns": 8,
    "run_budget": 200,
    "timeout": 240,
    "session_max_messages": 40,
    "session_max_tokens": 120000,
    "tokens_per_char": 0.34,
    "toolsets": "web,file,skills,todo,memory,session_search",
}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dialog_settings(cfg: config_mod.Config) -> dict:
    """Числа диалога: умолчания D3 поверх `notify.dialog`; мусор в конфиге не ломает."""
    raw = cfg.notify.get("dialog") if isinstance(cfg.notify, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    defaults = DIALOG_DEFAULTS
    return {
        "enabled": raw.get("enabled", defaults["enabled"]) is not False,
        "max_turns": _as_int(raw.get("max_turns"), defaults["max_turns"]),
        "run_budget": _as_int(raw.get("run_budget"), defaults["run_budget"]),
        "timeout": _as_int(raw.get("timeout"), defaults["timeout"]),
        "session_max_messages": _as_int(raw.get("session_max_messages"),
                                        defaults["session_max_messages"]),
        "session_max_tokens": _as_int(raw.get("session_max_tokens"),
                                      defaults["session_max_tokens"]),
        "tokens_per_char": _as_float(raw.get("tokens_per_char"), defaults["tokens_per_char"]),
        "toolsets": str(raw.get("toolsets") or defaults["toolsets"]),
    }


def _dialog_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / DIALOG_STATE_NAME


def _read_dialogs(cfg: config_mod.Config) -> dict:
    try:
        data = json.loads(_dialog_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_dialogs(cfg: config_mod.Config, data: dict) -> None:
    try:
        state_io.save_json(_dialog_path(cfg), data)
    except OSError as exc:
        print(f"[commands] состояние диалога не сохранено: {exc}", file=sys.stderr)


def rate_settings(cfg: config_mod.Config) -> dict:
    """Лимит частоты свободного текста: умолчания D4 поверх `notify.commands.rate_limit`.

    Ключ необязательный: его нет в `config.py` и `pipeline.json`, поэтому мусор в конфиге
    не ломает приём, а даёт умолчания. `max <= 0` — лимита нет вовсе.
    """
    commands = cfg.notify.get("commands") if isinstance(cfg.notify, dict) else None
    raw = commands.get("rate_limit") if isinstance(commands, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    max_msgs = _as_int(raw.get("max"), RATE_DEFAULTS["max"])
    window = _as_int(raw.get("window"), RATE_DEFAULTS["window"])
    if window <= 0:
        window = RATE_DEFAULTS["window"]
    if max_msgs < 0:
        max_msgs = 0
    return {"max": max_msgs, "window": window}


def _rate_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / RATE_STATE_NAME


def _read_rate(cfg: config_mod.Config) -> dict:
    """Счётчики частоты; битый JSON и отсутствие файла — пустое состояние."""
    try:
        data = json.loads(_rate_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def rate_allow(cfg: config_mod.Config, chat: str) -> dict:
    """Пропустить сообщение владельца или отсеять по частоте (D4).

    Квота живёт в `tg_rate.json`: cron запускает новый процесс каждый тик, поэтому счётчик
    в памяти процесса ничего не ограничивает. Одно предупреждение на окно (`warn`),
    дальше тишина. Сбой записи — fail-open: потерять сообщение владельца хуже, чем
    пропустить лишнее.
    """
    settings = rate_settings(cfg)
    if settings["max"] <= 0:
        return {"allowed": True, "warn": False}
    state = _read_rate(cfg)
    key = str(chat)
    item = state.get(key)
    item = dict(item) if isinstance(item, dict) else {}
    now = _now()
    window_start = _as_float(item.get("window_start"), 0.0)
    count = _as_int(item.get("count"), 0)
    warned = item.get("warned") is True
    if not item or now - window_start >= settings["window"]:
        count, warned, window_start = 0, False, now
    warn = False
    if count >= settings["max"]:
        warn = not warned
        warned = True
        allowed = False
    else:
        count += 1
        allowed = True
    state[key] = {"count": count, "window_start": window_start, "warned": warned}
    try:
        state_io.save_json(_rate_path(cfg), state)
    except OSError as exc:
        print(f"[commands] квота не сохранена: {exc}", file=sys.stderr)
        return {"allowed": True, "warn": False}
    return {"allowed": allowed, "warn": warn}


def _estimate_tokens(text: str, per_char: float) -> int:
    """Оценка токенов по символам (D6): ceil(len * tokens_per_char)."""
    return int(math.ceil(max(0, len(text or "")) * max(0.0, per_char)))


def _dialog_item(dialogs: dict, key: str) -> dict:
    """Запись диалога как словарь: испорченное значение не ломает разбор."""
    item = dialogs.get(key) if isinstance(dialogs, dict) else None
    return dict(item) if isinstance(item, dict) else {}


def dialog_session(cfg: config_mod.Config, chat: str, text: str = "") -> dict:
    """Сессия диалога чата; при исчерпании даты/сообщений/токенов — следующая (D6).

    Имя сессии `tg-<chat>-<дата>-<N>`: `--continue` получает именно его, поэтому после
    ротации агент начинает чистый диалог. Токены проецируются вместе с текущим вопросом —
    иначе вопрос-простыня уходил бы в сессию, которая по счёту уже переполнена.
    """
    settings = dialog_settings(cfg)
    today = time.strftime("%Y%m%d")
    dialogs = _read_dialogs(cfg)
    key = str(chat)
    item = _dialog_item(dialogs, key)
    serial = _as_int(item.get("serial"), 0)
    messages = _as_int(item.get("messages"), 0)
    tokens = _as_int(item.get("tokens"), 0)
    date = str(item.get("date") or "")
    fresh = serial < 1
    rotated = False
    if fresh:
        serial, messages, tokens, date = 1, 0, 0, today
    else:
        projected = tokens + _estimate_tokens(text, settings["tokens_per_char"])
        rotated = (date != today
                   or messages + 2 > settings["session_max_messages"]
                   or projected >= settings["session_max_tokens"])
        if rotated:
            serial, messages, tokens, date = serial + 1, 0, 0, today
    session = {"chat": key, "date": date, "serial": serial, "messages": messages,
               "tokens": tokens, "name": f"tg-{key}-{date}-{serial}",
               "started": str(item.get("started") or "")}
    if fresh or rotated or session["started"] != item.get("started"):
        session["started"] = time.strftime("%d.%m.%Y %H:%M:%S")
        dialogs[key] = dict(session)
        _save_dialogs(cfg, dialogs)
    return session


def save_dialog_session(cfg: config_mod.Config, session: dict) -> None:
    """Сохранить счётчики сессии (сообщения и токены) после ответа агента."""
    dialogs = _read_dialogs(cfg)
    dialogs[str(session.get("chat") or "")] = dict(session)
    _save_dialogs(cfg, dialogs)


def reset_dialog(cfg: config_mod.Config, chat: str) -> str:
    """`/reset`: следующая реплика начнёт новую сессию (serial + 1)."""
    today = time.strftime("%Y%m%d")
    dialogs = _read_dialogs(cfg)
    key = str(chat)
    serial = _as_int(_dialog_item(dialogs, key).get("serial"), 0) + 1
    dialogs[key] = {"chat": key, "date": today, "serial": serial, "messages": 0,
                    "tokens": 0, "name": f"tg-{key}-{today}-{serial}",
                    "started": time.strftime("%d.%m.%Y %H:%M:%S")}
    _save_dialogs(cfg, dialogs)
    return "диалог сброшен: следующий вопрос начнёт новую сессию"


def _only_dialog_chat(cfg: config_mod.Config) -> str:
    """Единственный чат в состоянии диалога — для вызова `/reset` без chat."""
    dialogs = _read_dialogs(cfg)
    return str(next(iter(dialogs))) if len(dialogs) == 1 else ""


# ─────────────── запрет приёма команд после 409 (вебхук/второй поллер) ───────────────

def _commands_off_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / COMMANDS_OFF_NAME


def commands_off(cfg: config_mod.Config) -> dict:
    """Причина, по которой приём команд выключен (пусто — приём включён)."""
    try:
        data = json.loads(_commands_off_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_commands_off(cfg: config_mod.Config) -> bool:
    """Снять маркер «приём команд выключен» (`--commands-reset`). True — снят сейчас."""
    path = _commands_off_path(cfg)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"[commands] маркер не снят: {exc}", file=sys.stderr)
        return False


def _log_chat(cfg: config_mod.Config, action: str, detail: str,
              started: float | None = None) -> None:
    """Действие из чата — строкой в историю тиков (S3). Сбой записи ответ не ломает."""
    try:
        plan_id = _plan_paths(cfg).plan_id
    except Exception:  # noqa: BLE001 - истории нужен план, но ответ владельцу важнее
        plan_id = ""
    duration = 0.0 if started is None else max(0.0, time.monotonic() - started)
    try:
        append_history(cfg, {"ts": time.time(), "at": time.strftime("%d.%m.%Y %H:%M:%S"),
                             "plan": plan_id, "step": "", "action": action,
                             "duration_s": round(duration, 3),
                             "detail": str(detail or "")[:200], "commit": ""})
    except OSError as exc:
        print(f"[commands] история не записана: {exc}", file=sys.stderr)


def plans_of(cfg: config_mod.Config) -> list:
    """Все ведомые планы; ошибки разбора не мешают ответить на команду."""
    try:
        return list(registry.driven_specs(cfg))
    except Exception as exc:  # noqa: BLE001 - статус обязан отвечать
        print(f"[commands] планы не прочитаны: {exc}", file=sys.stderr)
        return []


def tg_status(cfg: config_mod.Config) -> str:
    """Короткая сводка для чата: стоп-флаг и таблица планов."""
    lines = [f"Конвейер: {cfg.project} (борда {cfg.board})"]
    stops = []
    for spec in plans_of(cfg):
        try:
            p = resolve_paths(cfg, spec.id)
        except Exception as exc:  # noqa: BLE001 - сводка обязана ответить целиком
            config_mod.debug_note("commands", exc)
            continue
        stop = load_stop(p)
        if stop:
            kind = "владелец" if stop_kind(stop) == "owner" else "инцидент"
            stops.append(f"план {spec.id} — стоп ({kind}, {stop.get('at', '')}): "
                         f"{str(stop.get('reason', ''))[:200]}")
    lines.append("\n".join(stops) if stops else "стоп-флагов нет")
    lines.append("")
    try:
        lines.append(plans_table(cfg))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(таблица планов недоступна: {exc})")
    # Решение стража — то, что конвейер сделает дальше; `execute=False` на диск не пишет.
    try:
        p = _plan_paths(cfg)
        dec = (run_gate(p, execute=False) or {}).get("decision") or {}
        lines.append("")
        lines.append(f"Решение стража (план {p.plan_id}): {dec.get('action') or '—'} — "
                     f"{str(dec.get('reason') or '')[:200]}")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - статус обязан отвечать
        lines.append(f"решение стража недоступно: {exc}")
    return "\n".join(lines)


def _plan_paths(cfg: config_mod.Config, plan: str | None = None) -> Paths:
    """Пути плана; без --plan берётся план по умолчанию, иначе первый ведомый."""
    target = plan or cfg.default_plan
    if target is None:
        specs = plans_of(cfg)
        target = specs[0].id if specs else None
    if target is None:
        raise registry.PlanNotFound("нет ни одного плана с журналом")
    return resolve_paths(cfg, target)


def declared_steps(cfg: config_mod.Config, p: Paths) -> set[str]:
    """Идентификаторы шагов из файла плана; пусто — план не разобран.

    Ошибку разбора не превращаем в отказ команды: у legacy-плана без раздела шагов
    проверять нечего, и `--hold` там работает как раньше. Отказ на таком плане был бы
    новым способом остановить владельца, а не защитой от опечатки.
    """
    try:
        return {step.id.upper() for step in next_step.plan_steps(cfg, Path(p.plan_file))}
    except Exception:                                           # noqa: BLE001
        return set()


def hold_state_change(cfg: config_mod.Config, p: Paths, step: str, add: bool) -> dict:
    """Ручной допуск шага: единственный путь и для CLI, и для команд из чата.

    Возвращает `{ok, message}` либо `{ok: False, error}`. Номер шага сверяется с планом:
    раньше `--hold NOPE-99` отвечал «шаги с ручным допуском: NOPE-99» и навсегда
    дописывал мусор в `hold_released`, откуда его уже не убрать командой (панель шлёт
    в `--hold` то, что выбрано на вкладке шага, поэтому опечатка доходит и оттуда).
    """
    key = str(step or "").strip().upper()
    if not key:
        return {"ok": False, "error": "укажи шаг: /hold S3"}
    state = load_state(p)
    hold = {str(item).upper() for item in (state.get("hold_steps") or [])}
    released = {str(item).upper() for item in (state.get("hold_released") or [])}
    declared = declared_steps(cfg, p)
    # Уже упомянутый шаг остаётся допустимым: без этого повторный `--unhold` того же
    # шага отвечал бы отказом, а снятие допуска обязано быть идемпотентным.
    if declared and key not in (declared | hold | released):
        return {"ok": False,
                "error": f"нет шага {key} в плане {p.plan_id}: шаги плана — "
                         + (", ".join(sorted(declared)) or "—")}
    if add:
        hold.add(key)
    else:
        hold.discard(key)
        # Отметка владельца: автоматический риск-hold по этому шагу больше не ставится,
        # иначе снять допуск было бы невозможно (D4).
        released.add(key)
    state["hold_steps"] = sorted(hold)
    state["hold_released"] = sorted(released)
    save_state(p, state)
    return {"ok": True,
            "message": "шаги с ручным допуском: " + (", ".join(sorted(hold)) or "—"),
            "hold": sorted(hold)}


def _hold_change(cfg: config_mod.Config, step: str, add: bool) -> str:
    p = _plan_paths(cfg)
    result = hold_state_change(cfg, p, step, add)
    return result["message"] if result["ok"] else result["error"]


def _answer_change(cfg: config_mod.Config, arg: str) -> str:
    """Ответ из чата: `/answer Q-1 A` — тот же путь записи, что у панели (S5, п.4б).

    План ищется по вопросу: номер `Q-N` уникален в момент ответа, поэтому `--plan` из чата
    не нужен. Вопрос открыт в двух планах — отказ с их номерами и подсказкой `--plan`.
    """
    parts = (arg or "").split(maxsplit=1)
    if len(parts) < 2:
        return "форма: /answer Q-1 A — номер вопроса и вариант"
    qid = question_id_of(parts[0])
    if not qid:
        return f"номер вопроса не вида Q-N: {parts[0]!r}"
    found = find_question_plan(cfg, qid)
    if found["plan"] is None:
        return found["error"]
    result = answer_question(cfg, found["plan"].plan_id, qid, parts[1])
    return result["message"]


def handle_command(cfg: config_mod.Config, text: str, chat: str | None = None) -> str:
    """Разобрать одну команду владельца и вернуть текст ответа.

    `chat` нужен только `/reset` (диалог живёт на чат); вызов без него работает на
    единственном чате из состояния, а если его нет — новая сессия начнётся и так.
    """
    head, _, rest = (text or "").strip().partition(" ")
    cmd = head.split("@", 1)[0].lower()
    arg = rest.strip()
    if cmd in ("/start", "/help"):
        return TG_HELP
    if cmd == "/status":
        return tg_status(cfg)
    if cmd == "/plans":
        return plans_table(cfg)
    if cmd == "/stop":
        p = _plan_paths(cfg)
        payload = write_stop(p, arg or "остановка владельцем из чата", "owner")
        return f"⏸ стоп поставлен: {payload['reason']}"
    if cmd == "/resume":
        p = _plan_paths(cfg)
        return "▶️ стоп снят, конвейер продолжит" if clear_stop(p) else "стоп-флага не было"
    if cmd in ("/hold", "/unhold"):
        return _hold_change(cfg, arg, add=cmd == "/hold")
    if cmd == "/answer":
        return _answer_change(cfg, arg)
    if cmd == "/reset":
        target = str(chat or _only_dialog_chat(cfg))
        if not target:
            return "диалог сброшен: следующий вопрос начнёт новую сессию"
        return reset_dialog(cfg, target)
    return "не знаю такую команду. /help — список."


def owner_ids(cfg: config_mod.Config) -> set[str]:
    """Кто имеет право управлять ботом: только владелец (или несколько, через запятую)."""
    env_name = str(cfg.notify.get("owner_env") or "STEP_PIPELINE_TELEGRAM_OWNER_ID")
    raw = (os.environ.get(env_name) or "").replace(";", ",")
    ids = {part.strip() for part in raw.split(",") if part.strip()}
    if not ids:
        chat = os.environ.get(str(cfg.notify.get("chat_env") or "PIPELINE_TG_CHAT"))
        if chat:
            ids = {str(chat)}
    return ids


ANSI_RE = re.compile("\x1b" + r"\[[0-9;?]*[A-Za-z]")

# Служебные строки одноразового ответа (`--cli --oneshot`): шапка профиля, строки сессии,
# отчёты о вызовах инструментов. D4: такой строки в чате быть не должно.
SERVICE_PREFIXES = (
    "session_id:", "Session", "Hermes Agent", "Provider:", "Model:", "Mode:", "Toolsets:",
    "Workspace:", "Resumed session", "↻", "⟳", "⚙", "🔧", "🛠", "📄", "🧠", "⎿", "↳",
    "┌", "┐", "└", "┘", "│", "├", "┤", "─",
)


def clean_agent_output(text: str) -> str:
    """Ответ агента без служебных строк, CR и ANSI-цветов — чтобы чат читался."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    clean = ANSI_RE.sub("", raw)
    lines = [line for line in clean.splitlines()
             if not line.strip().startswith(SERVICE_PREFIXES)]
    return "\n".join(lines).strip()




def split_message(text: str, limit: int = TG_TEXT_LIMIT) -> list[str]:
    """Режет длинный ответ по строкам под лимит Telegram (без разрыва слов, если можно).

    Порог — `TG_TEXT_LIMIT` (3900): лимит Telegram 4096, запас остаётся на служебные
    символы Bot API.
    """
    chunks: list[str] = []
    current = ""
    for line in (text or "").splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def send_answer(token: str, chat: str, text: str) -> None:
    """Отправить ответ целиком: длинный текст уходит несколькими сообщениями."""
    for chunk in split_message(text):
        sent = _tg_call(token, "sendMessage", chat_id=chat, text=chunk,
                        disable_web_page_preview="true")
        if not sent.get("ok"):
            print(f"[commands] ответ не доставлен: {sent.get('description')}", file=sys.stderr)
            break


def send_typing(token: str, chat: str) -> None:
    """«Печатает» в чате: сбой action не влияет на ответ и не пишется в stderr."""
    _tg_call(token, "sendChatAction", chat_id=chat, action="typing")


def _typing_keepalive(token: str, chat: str, stop_event, interval: float = TYPING_REFRESH) -> None:
    """Держать «печатает», пока агент думает: гасится через stop_event."""
    while not stop_event.wait(interval):
        send_typing(token, chat)

def _timeout_answer(exc: subprocess.TimeoutExpired) -> str:
    """Человеческий ответ на срез по времени (D5): без имени команды и цифр таймаута.

    Частичный вывод из исключения сохраняется: если агент успел что-то сказать, владелец
    это увидит, а не пустоту.
    """
    parts = []
    for chunk in (getattr(exc, "output", None), getattr(exc, "stderr", None)):
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        parts.append(str(chunk))
    partial = clean_agent_output("\n".join(parts))[:500]
    head = ("не успел ответить за отведённое время (notify.dialog.timeout): диалог разросся."
            " Начать заново — /reset")
    return f"{head} что успел сказать: {partial}" if partial else head


def ask_agent(cfg: config_mod.Config, text: str, chat: str,
              timeout: int | None = None) -> str:
    """Свободный текст владельца — в диалог с агентом профиля, сессия на чат.

    Потолки D3 держат ответ в тех же секундах, что и срез процесса: `--max-turns` и
    `--run-budget` не дают агенту уйти в проект, `-t` снимает terminal/execute_code/
    pipeline_run. `-Q` не ставится: частичный вывод нужен при срезе (D5).
    """
    settings = dialog_settings(cfg)
    if not settings["enabled"]:
        return "диалог выключен (notify.dialog.enabled)"
    limit = _as_int(timeout, settings["timeout"])
    session = dialog_session(cfg, str(chat), text)
    cmd = [_hermes(), "-p", str(cfg.driver), "chat", "-q", text, "--oneshot", "--cli",
           "--continue", session["name"], "--create-if-missing",
           "--max-turns", str(settings["max_turns"]),
           "--run-budget", str(settings["run_budget"]),
           "-t", settings["toolsets"]]
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", CLICOLOR="0")
    try:
        res = _run(cmd, None, timeout=limit, env=env)
    except subprocess.TimeoutExpired as exc:
        return _timeout_answer(exc)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return (f"агент не ответил: сбой запуска ({type(exc).__name__}). "
                "/reset — начать диалог заново.")
    out = clean_agent_output((res.stdout or "") + (res.stderr or ""))
    if not out:
        return "агент не ответил (пустой ответ). /reset — начать диалог заново."
    session["messages"] = _as_int(session.get("messages"), 0) + 2
    session["tokens"] = _as_int(session.get("tokens"), 0) + _estimate_tokens(
        text + out, settings["tokens_per_char"])
    save_dialog_session(cfg, session)
    # Длину держит `send_answer`; потолки ответа — 2-S4 (`_timeout_answer`, 240 с),
    # обрезка здесь делала разбиение фиктивным.
    return out


def poll_commands(cfg: config_mod.Config, wait: int = 0, max_updates: int = 10) -> dict:
    """Один опрос getUpdates: выполнить команды владельца и ответить в тот же чат.

    Offset хранится в state_dir/tg_offset.json, поэтому повторный опрос не выполняет
    команду дважды. Отвечаем только в настроенный чат; чужие сообщения игнорируются.
    Ответ 409 (вебхук или второй поллер) выключает приём команд одной записью маркера:
    предупреждение уходит один раз, дальше функция выходит до запроса в сеть.
    """
    result = {"ok": False, "handled": 0, "ignored": 0, "limited": 0, "reason": "",
              "disabled": False}
    if not commands_enabled(cfg):
        result["reason"] = "приём команд выключен (notify.commands.enabled)"
        return result
    off = commands_off(cfg)
    if off:
        result["disabled"] = True
        result["reason"] = (f"приём команд выключен до устранения: {off.get('reason', '')} "
                            "(снять: --commands-reset)")
        return result
    target = tg_target(cfg)
    if target is None:
        result["reason"] = "нет токена/чата в окружении"
        return result
    token, chat = target
    path = _offset_path(cfg)
    offset = _read_offset(path)
    data = _tg_call(token, "getUpdates", http_timeout=int(wait) + 20,
                    offset=offset or None, limit=max_updates, timeout=int(wait))
    if not data.get("ok"):
        code = data.get("error_code")
        if code == 409:
            reason = ("на боте стоит вебхук или второй поллер: getUpdates запрещён (409); "
                      "приём команд выключен до перезапуска")
            result.update(disabled=True, reason=reason)
            try:
                state_io.save_json(_commands_off_path(cfg), {
                    "at": time.strftime("%d.%m.%Y %H:%M:%S"), "reason": reason})
            except OSError as exc:
                print(f"[commands] маркер не записан: {exc}", file=sys.stderr)
            notify(cfg, "⛔ " + reason
                   + ". Снять вебхук и выполнить: python .pipeline/gate.py --commands-reset")
            _log_chat(cfg, "tg_conflict", reason)
            return result
        result["reason"] = str(data.get("description") or "Telegram не ответил")
        print(f"[commands] опрос не удался: {result['reason']}", file=sys.stderr)
        return result
    newest = offset
    for update in data.get("result") or []:
        newest = max(newest, int(update.get("update_id") or 0) + 1)
        message = update.get("message") or {}
        text = str(message.get("text") or "")
        sender_chat = str((message.get("chat") or {}).get("id") or "")
        sender_id = str((message.get("from") or {}).get("id") or "")
        allowed = owner_ids(cfg)
        if sender_chat != str(chat) or (allowed and sender_id and sender_id not in allowed):
            result["ignored"] += 1
            # id отправителя и чата; текст чужого сообщения в лог не пишем.
            print(f"[commands] чужой отправитель {sender_id or '?'} (чат {sender_chat or '?'}) — игнор",
                  file=sys.stderr)
            continue
        if not text.startswith("/"):
            quota = rate_allow(cfg, chat)
            if not quota["allowed"]:
                result["limited"] += 1
                if quota["warn"]:
                    settings = rate_settings(cfg)
                    send_answer(token, chat,
                                f"слишком часто: не больше {settings['max']} сообщений"
                                f" за {settings['window']} с — это сообщение не обработано.")
                print(f"[commands] слишком часто: чат {chat} — сообщение не обработано",
                      file=sys.stderr)
                continue
        started = time.monotonic()
        if not text.startswith("/"):
            send_typing(token, chat)
            stop_event = threading.Event()
            keep = threading.Thread(target=_typing_keepalive, args=(token, chat, stop_event),
                                    daemon=True)
            keep.start()
            try:
                answer = ask_agent(cfg, text, chat)
            finally:
                stop_event.set()
                keep.join(timeout=1.0)
            send_answer(token, chat, answer)
            _log_chat(cfg, "tg_dialog", text, started)
            result["handled"] += 1
            continue
        try:
            answer = handle_command(cfg, text, chat)
        except Exception as exc:  # noqa: BLE001 - команда не должна ронять тик
            answer = f"ошибка выполнения {text.split()[0]}: {exc}"
        send_answer(token, chat, answer)
        _log_chat(cfg, "tg_command", text, started)
        result["handled"] += 1
    if newest > offset:
        _write_offset(path, newest)
    result["ok"] = True
    return result


def _evidence(p: Paths, verdict: dict, remarks: int = 0, step_id: str = "") -> str:
    """Четвёртая колонка журнала: чем шаг доказан.

    `remarks` > 0 — шаг принят с замечаниями (13-S1): вердикт `REQUEST_CHANGES`, блокирующих
    находок ноль. Строка APPROVE остаётся дословной: её держит golden-эталон
    `04-review-approve`, и менять эталон закрытия APPROVE нельзя.

    `step_id` (план 9, S2) — к строке добавляется `Прогоны: 3/3, 41 с`: счётчик берётся из
    доказательства прогона, привязанного к коммиту из отчёта ревью (`verdict["commit"]`), а
    если для него файла нет — к текущему HEAD; нет ни того, ни другого — строки нет вовсе.
    У шага без пунктов «Прогон» файла доказательства нет, и golden-эталон держит именно
    такой шаг. Красный или исчерпанный по бюджету прогон сюда не попадает: такой шаг до
    журнала не доходит.
    """
    commit = f"коммит `{verdict['commit']}`" if verdict.get("commit") else "коммит не указан"
    report = p.cfg.rel(Path(verdict["path"]))
    runs_suffix = ""
    sha = str(verdict.get("commit") or "")
    proof = runs_proof_for(p, step_id, sha) if step_id else None
    counter = runs_counter(proof)
    if counter:
        runs_suffix = f"; {counter}"
    if remarks:
        return (f"ревью `{p.cfg.reviewer}` — {REMARKS_EVIDENCE}, находок вне блокирующих: "
                f"{remarks} (`{report}`, {commit}); замечания — в реестре находок плана"
                f"{runs_suffix}")
    return (f"ревью `{p.cfg.reviewer}` — APPROVE (`{report}`, {commit}); "
            f"гейты шага прогнаны ревьюером самостоятельно{runs_suffix}")


def _register_remarks(p: Paths, step: dict, verdict: dict) -> int:
    """Дописать замечания закрытого шага в реестр находок плана (§4.3). Число строк.

    Статус `не блокирует`: находка шаг не держит, она — память о том, что ревью заметило.
    Класс строки — `стиль` из словаря реестра (классов ревью в словаре нет: расширять
    формат реестра — зона S6), а класс ревью остаётся меткой в тексте находки, и ревьюер
    плана вправе уточнить его своим проходом. ID присваивает `add_findings` — следующий
    свободный по файлу. `GateError`/`OSError` не глотаются: решение закрывать шаг молча
    по неизвестному реестру не принимается (вызывающая сторона ставит стоп).
    """
    try:
        text = Path(verdict["path"]).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        text = None
    rows = [{"step": step["id"], "kind": REMARK_KIND,
             "text": f"[{item['class']}] {item['body']}",
             "action": REMARK_ACTION, "status": REMARK_STATUS}
            for item in report_remarks(text)]
    if not rows:
        return 0
    add_findings(plan_findings_path(p), rows)
    return len(rows)


def run_gate(p: Paths, execute: bool = True, create_card=_create_card,
             notify_fn=None, commit_fn=commit_and_push, ctx: dict | None = None) -> dict:
    """Один тик конвейера. Возвращает {decision, applied, detail}."""
    ctx = ctx if ctx is not None else build_ctx(p)
    say_raw = notify_fn or (lambda text: notify(p.cfg, text))
    plan_id = p.plan_id
    closed: list[dict] = []
    # Наследование ДО решения: иначе идущий план навсегда получил бы «не утверждён».
    # Только исполняющий проход — `--status` на диск не пишет.
    if execute:
        ensure_approval(p, ctx)
        # Вопросы шага (8-S5): строки вопросов из отчётов переносятся в файл ДО решения,
        # чтобы новый вопрос держал шаг уже на этом тике; затем автовыбор рекомендации.
        register_questions(p, ctx)
        closed = sweep_questions(p.cfg, p, ctx, ctx.get("state"), say_raw)
    decision = decide(ctx)
    cleared = ""
    if execute and decision.get("clear_incident"):
        ctx["stop"] = None
        cleared = decision.get("reason", "")
        clear_stop(p)
        decision = decide(ctx)
    detail = closed[-1]["message"] if closed else ""
    if closed:
        # Автовыбор закрыл вопрос: тик на этом заканчивается, карточка шага — следующим
        # тиком (требование плана, п. 5). Один тик — одно действие.
        decision = {"action": "question_auto", "step": closed[-1]["step"],
                    "question": closed[-1]["qid"],
                    "reason": f"{closed[-1]['qid']} принят автоматически: "
                              f"{closed[-1]['answer']} — карточка шага следующим тиком"}
        detail = decision["reason"]
    result = {"plan_id": plan_id, "decision": decision,
              "applied": bool(cleared) or bool(closed), "detail": detail,
              "incident_cleared": bool(cleared), "status": status_text(ctx, decision)}
    # Пауза по расписанию едет в ответе `--status`: панель берёт её из того же вызова,
    # что и решение, — отдельный запуск движка ради одного ключа не нужен.
    result["pause"] = config_mod.pause_state(p.cfg, _now())
    # Вопросы шага для панели (8-S5): окно выбора и дедлайн автовыбора берутся из того же
    # вызова движка, что и решение, — второй разбор файла панели не нужен.
    result["questions"] = questions_view(ctx)
    if not execute:
        result["metrics"] = history_metrics(p.cfg)
        return result
    # Метрика попытки шага (план 17, S1): завершённые прогоны карточек текущего шага
    # ложатся строкой в `state/metrics.jsonl`. Сбой замера вердикт не меняет и тик не
    # валит: числа нужны владельцу, а решение страж принимает по вердикту и файлам.
    # `ctx["cards"]` и шаг уже собраны `build_ctx` — второго запроса к борде нет.
    try:
        current_step = ctx.get("step") or ctx.get("next") or {}
        step_metrics.record(p.cfg, p.kanban_db, plan_id,
                            str(current_step.get("id") or ""), ctx.get("cards") or [])
    except Exception as exc:  # noqa: BLE001 - замер не имеет права валить тик
        config_mod.debug_note("step_metrics", exc)
    # Залипание инструмента (план 17, S2): строка метрики с залипанием уходит комментарием в
    # карточку шага один раз на прогон. Как и замер, это не решение стража — сбой записи в
    # борду вердикт не меняет и тик не валит.
    try:
        report_stalls(p, ctx)
    except Exception as exc:  # noqa: BLE001 - отметка о методе не имеет права валить тик
        config_mod.debug_note("tool_stall", exc)
    if closed:
        return result

    # Уведомление владельцу об открытых вопросах — до решения: вопрос уже держит шаг.
    announce_questions(p.cfg, p, ctx.get("state"), say_raw)
    result["questions"] = questions_view(ctx)

    def say(text: str) -> None:
        say_raw(text)

    if cleared:
        say(f"✅ инцидент снят ({cleared}), конвейер продолжает")
    state = ctx["state"]

    def touch(action: str) -> None:
        state["last_action"] = action
        state["last_tick"] = time.strftime("%d.%m.%Y %H:%M:%S")
        # Первое же действие снимает ожидание: сторож начинает отсчёт заново.
        for key in ("waiting_since", "waiting_reason",
                    "watchdog_notice_at", "watchdog_notice_reason"):
            state.pop(key, None)
        save_state(p, state)

    def assignee_or_stop(step: dict) -> tuple[str | None, str]:
        """Исполнитель карточки или инцидентный стоп — одна проверка на шаг и правки (S7).

        Возврат `(профиль, "")` — карточку можно ставить; `(None, причина)` — стоп уже
        записан (или уже стоял с той же причиной: второй раз не уведомляем).
        """
        try:
            return _step_assignee(p, step), ""
        except UnknownAssignee as exc:
            reason = unknown_assignee_reason(p, step, exc)
            if (load_stop(p) or {}).get("reason") == reason:
                result.update(applied=False, detail=reason)
                return None, reason
            write_stop(p, reason, "gate", kind="incident")
            result.update(applied=True, detail=reason)
            touch("stop:assignee")
            say("⛔ " + reason)
            return None, reason

    def reused_card(cid: str | None, key: str, what: str) -> bool:
        """Заказ вернул уже закрытую карточку — это зависание, а не выполненное действие.

        `hermes kanban create` идемпотентен по ключу: занятый ключ возвращает прежнюю
        карточку любого статуса. Для `done`-карточки новой работы не появляется, и без
        этой проверки тик писал «выполнено», пока план стоял (карточка правки плана,
        переутверждение после owner-stop, круг правок начинается заново).
        """
        if not cid:
            return False
        status = card_status(p, cid, key)
        if status not in TERMINAL:
            return False
        reason = (f"заказ «{what}» вернул уже закрытую карточку {cid} "
                  f"(статус {status}, ключ {key}): новой карточки нет, план не двигается")
        write_stop(p, reason, "gate", kind="incident")
        result.update(applied=True, detail=reason)
        touch(f"reused:{key}")
        attention(p.cfg, attention_text(
            plan_id, reason,
            evidence=[f"ключ идемпотентности {key} занят карточкой {cid} в статусе {status}",
                      "повторный заказ не создаёт карточку: движок собирает ключ, который уже был",
                      "журнал тиков повторяет один и тот же номер карточки"],
            todo=["посмотреть ключи карточек плана и журнал тиков (команды ниже)",
                  "обновить движок проекта до версии с уникальными ключами правок",
                  "снять стоп и дождаться следующего тика"],
            commands=[f"python .pipeline/gate.py --status --plan {plan_id}",
                      f"hermes kanban --board <борда> show {cid}",
                      "hermes pipeline setup --upgrade --project <путь>",
                      "python .pipeline/gate.py --resume"]),
            f"stall:{plan_id}", say)
        return True

    # Фаза прогонов (план 9, S2, §4 п.6): тик, решающий заказать ревью, сначала
    # исполняет пункты «Прогон» и пересчитывает решение — зелёное доказательство даёт
    # карточку ревью, красное — карточку правок, исчерпанный бюджет — стоп в том же тике.
    # Прогоны не карточка: правило «один тик — одно действие» ими не нарушается; фаза
    # живёт в ветке исполнения, поэтому `run_gate(execute=False)` (предпросмотр) прогонов
    # не запускает. `result` собирается со статусом ДО этой точки, поэтому статус и
    # решение пересчитываются вместе — иначе `--status` напечатает «create_review» при уже
    # заказанной карточке правок (F13). Сбой чтения или записи доказательства — стоп
    # инцидентный: без файла шаг не может ни в ревью, ни в правку, а молчаливый «зелёный»
    # прогон был бы враньём; пишет его общая ветка `action == "stop"`.
    if (decision.get("action") == "create_review" and ctx.get("next")
            and (ctx["next"].get("runs") or []) and ctx.get("step_runs") is None):
        runs_step = ctx["next"]
        try:
            runs_proof = run_step_runs(p, runs_step)
        except (GateError, OSError, subprocess.SubprocessError) as exc:
            reason = (f"прогоны шага {runs_step.get('id')} не исполнены: "
                      f"{type(exc).__name__}: {exc}")
            decision = {"action": "stop", "step": str(runs_step.get("id") or ""),
                        "kind": "incident", "reason": reason}
            result.update(applied=True, detail=reason)
        else:
            ctx["step_runs"] = runs_proof
            decision = decide(ctx)
            result["decision"] = decision
            result["status"] = status_text(ctx, decision)
    action = decision["action"]
    if action == "idle":
        # Простой виден только здесь (и в tick), поэтому и записывается здесь.
        if decision.get("approval_blocked"):
            # Сторож простоя для неутверждённого плана не запускается: иначе владелец
            # получит второе уведомление о том же плане.
            approval_notice(p, ctx, decision, say)
            result["watch"] = {"notice": False, "waiting_since": None}
            return result
        result["watch"] = watch_idle(p.cfg, p, ctx, decision, say)
        return result
    # ANCHOR:hold_risk_action — стабильная ссылка для спеки и ревью
    if action == "hold_risk":
        step = decision["data"]
        hold = {str(item).upper() for item in (state.get("hold_steps") or [])}
        hold.add(str(step["id"]).upper())
        state["hold_steps"] = sorted(hold)
        # Это ДЕЙСТВИЕ, а не простой: при нескольких действиях за тик (max_actions > 1)
        # hold ставится первым и не теряется. `touch` сохраняет ТОТ ЖЕ словарь состояния,
        # поэтому запись hold_steps уходит на диск вместе с отметкой о тике.
        touch(f"hold_risk:{step['id']}")
        reason = decision.get("reason", "")
        say("⏸ " + reason)
        result.update(applied=True, detail=reason)
        return result
    if action == "create_step":
        step = decision["data"]
        prompt = prompt_rel(p, step["id"])
        if prompt is None and p.cfg.require_prompt:
            # Шаг без постановки не начинается: заказ промптеру + инцидентный стоп,
            # который снимется сам, как только файл постановки появится.
            order_key = f"planloop:{plan_id}:{step['id']}:prompt"
            order_id = create_card(p, f"[постановка {plan_id} {step['id']}] {step['title']}",
                                   prompt_order_body(p, step), p.cfg.prompter, order_key,
                                   p.cfg.prompt_skills, max_runtime=p.cfg.prompt_runtime)
            where = f"карточка {order_id}" if order_id else "карточку заказа создать не удалось"
            reason = (f"нет постановки шага {step['id']}: заказана роли {p.cfg.prompter} ({where}); "
                      f"ожидается {p.cfg.rel(Path(p.cfg.prompts_dir))}/<дата>_{plan_id}-{step['id']}"
                      f"_prompt.md — шаг не начинаю")
            current = load_stop(p) or {}
            if current.get("reason") == reason:
                result.update(applied=False, detail=reason)
                return result
            write_stop(p, reason, "gate", kind="incident")
            result.update(applied=True, detail=reason)
            touch("stop:no_prompt")
            say("⛔ " + reason)
            return result
        key = f"planloop:{plan_id}:{step['id']}:step"
        # Страховка: при текущем порядке решений тот же шаг уже остановлен в `_decide_base`
        # (`assignee_error`), поэтому здесь проверка срабатывает только для правок.
        assignee, reason = assignee_or_stop(step)
        if assignee is None:
            return result
        cid = create_card(p, f"[план {plan_id} {step['id']}] {step['title']}",
                          step_card_body(p, step, prompt), assignee, key,
                          p.cfg.step_skills, max_runtime=p.cfg.step_runtime)
        if reused_card(cid, key, f"шаг {step['id']}"):
            return result
        result.update(applied=bool(cid), detail=f"карточка шага: {cid}")
        touch(f"create_step:{cid}")
        if cid:
            summary = _step_summary(step)
            lines = [f"план {plan_id}, шаг {step['id']} взят в работу — {step['title']}",
                     f"  что делаем: {summary}"]
            # Разведка закрылась исходом «готового нет»: владелец должен видеть, что план
            # идёт своим решением, а не по найденному готовому (D7).
            if ctx.get("scout_note"):
                lines.insert(0, ctx["scout_note"])
            dod = " ".join(str(step.get("dod") or "").split())
            if dod and dod[:60] != summary[:60]:
                lines.append(f"  готово, когда: {dod[:240]}")
            lines.append(f"  карточка {cid}")
            say("\n".join(lines))
            # Расхождение «отчёт говорит одно, реестр другое» уходит в карточку ревью: именно она
            # вела реестр. Шаг пишется комментарием только пока журнал открыт — после первой
            # строки журнала план уже не плановый, и ревью задним числом не догоняем.
            note = verdict_discrepancy_text(ctx)
            review_cards = ctx.get("plan_review_cards") or []
            if note and review_cards and not ctx.get("plan_started_journal"):
                if _comment(p, str(review_cards[-1]["id"]),
                            "ЗАМЕЧАНИЕ СТРАЖА\n" + note):
                    say("  замечание о расхождении вердикта — комментарием в карточку ревью "
                        f"{review_cards[-1]['id']}")
        else:
            say(f"не смог создать карточку шага {step['id']} — проверь `hermes kanban ls`")
        return result
    if action == "create_scout":
        # Разведка планового уровня: ключ без шага, `--parent` не ставится. Форма ключа та же,
        # что у соседей: `planloop:<план>:scout` (C4, C5).
        key = f"planloop:{plan_id}:{SCOUT_KEY}"
        profile = scout_profile(p.cfg)
        cid = create_card(p, f"[разведка {plan_id}] Разведка плана: готовые решения",
                          scout_card_body(p, ctx.get("steps") or []), profile, key,
                          scout_skills(p.cfg), max_runtime=scout_runtime(p.cfg))
        if reused_card(cid, key, "разведка плана"):
            return result
        result.update(applied=bool(cid),
                      detail=(f"карточка разведки: {cid} (профиль {profile})" if cid
                              else "карточку разведки создать не удалось"))
        touch(f"create_scout:{cid}")
        if cid:
            say(f"план {plan_id}: разведка готовых решений — карточка {cid} "
                f"(профиль {profile}), план ждёт её результата")
        else:
            say("карточку разведки создать не удалось, проверь `hermes kanban ls`")
        return result
    if action == "create_plan_review":
        # Ревью плана — карточка планового уровня: ключ без шага, `--parent` не ставится.
        # Номер круга живёт отдельным сегментом ключа: первый круг — как у соседей.
        round_no = int(decision.get("round") or 1)
        key = f"planloop:{plan_id}:{PLAN_REVIEW_KEY}"
        if round_no > 1:
            key += f":{round_no}"
        profile = plan_review_profile(p.cfg)
        report_rel = plan_review_rel(p)
        cid = create_card(p, f"[ревью плана {plan_id}] Ревью плана: замысел до кода",
                          plan_review_card_body(p, p.cfg.rel(Path(p.plan_file)), report_rel,
                                                profile, round_no=round_no),
                          profile, key, plan_review_skills(p.cfg),
                          max_runtime=plan_review_runtime(p.cfg))
        if reused_card(cid, key, "ревью плана"):
            return result
        result.update(applied=bool(cid), detail=f"карточка ревью плана: {cid}")
        if cid:
            # Сбой создания карточки состояние НЕ пишет: `touch` перевёл бы план в
            # «идущие» (`registry.plan_started`) и открыл бы шаги без вердикта
            # (находка 1 ревью 5-S5). Без отметки следующий тик честно повторит заказ.
            touch(f"create_plan_review:{cid}")
        if cid:
            say(f"план {plan_id}: ревью плана как отдельный шаг — карточка {cid} "
                f"(профиль {profile}, круг {round_no}); шаги не начинаются без APPROVE")
        else:
            say(f"план {plan_id}: не смог поставить ревью плана — проверь `hermes kanban ls`")
        return result
    if action == "create_plan_fix":
        # Один круг правок плана (D7): ключ с маркером круга ревью, дальше — повторное
        # ревью. Маркер — номер круга ревью, по которому пришёл отказ (T3e), он же делит
        # бюджет отказов; тело карточки по-прежнему говорит «круг 1» правок.
        review_round = int(decision.get("review_round") or 1)
        # Номер в ключе — по числу уже созданных правок, а не по кругу ревью: после
        # переутверждения план получает свежий круг ревью (`review_round` снова 1), а
        # ключ `planfix:1` занят выполненной карточкой прошлой версии. `hermes kanban
        # create` идемпотентен по ключу и вернул бы её — новых правок не появилось бы.
        key_round = next_fix_round(ctx.get("plan_fix_cards") or [], review_round)
        key = f"planloop:{plan_id}:{PLAN_FIX_KEY}:{key_round}"
        # Номер в заголовке и теле — круг эпохи (столько правок уже было при этой метке
        # переутверждения), а не `key_round`: ключ нумеруется по всем правкам за жизнь
        # плана, и заголовок читался как «круг 5 из 4» при бюджете в четыре круга.
        display_round = int(decision.get("round") or key_round)
        profile = plan_fix_profile(p.cfg)
        report_rel = plan_review_rel(p)
        # Открытые ID реестра — первыми: ими и продиктован вердикт, когда реестр сильнее отчёта.
        # Отчёт остаётся запасным путём для планов без реестра (и для шагов до 7-S1).
        findings = (open_findings_lines(ctx)
                    or _blocking_findings(plan_review_path(p), FINDINGS_LIMIT)
                    or _review_findings(plan_review_path(p), FINDINGS_LIMIT))
        cid = create_card(p, f"[правки плана {plan_id}] Правки плана по ревью (круг {display_round})",
                          plan_fix_card_body(p, p.cfg.rel(Path(p.plan_file)), report_rel,
                                             findings, round_no=display_round,
                                             max_rounds=plan_fix_budget(ctx)),
                          profile, key, plan_fix_skills(p.cfg),
                          max_runtime=plan_fix_runtime(p.cfg))
        if reused_card(cid, key, "правка плана по ревью"):
            return result
        result.update(applied=bool(cid), detail=f"карточка правок плана: {cid}")
        if cid:
            # Базовая версия плана ДО правки (S4): иначе расширение сравнивать не с чем.
            state["plan_fix_baseline"] = plan_fix_snapshot(p, key_round, ctx, findings)
            touch(f"create_plan_fix:{cid}")
            # Правит план не тот, кто пишет отчёт: расхождение вердикта адресуется автору отчёта.
            note = verdict_discrepancy_text(ctx)
            review_cards = ctx.get("plan_review_cards") or []
            if note and review_cards:
                if _comment(p, str(review_cards[-1]["id"]),
                            "ЗАМЕЧАНИЕ СТРАЖА\n" + note):
                    say("  замечание о расхождении вердикта — комментарием в карточку ревью "
                        f"{review_cards[-1]['id']}")
        if cid:
            # Находки — в сообщение, не только счётом: владелец должен видеть, что править,
            # не открывая панель (0.34.0). Порядок строк — как в реестре.
            say(plan_fix_notice(plan_id, cid, profile, key_round,
                                ctx.get("plan_findings") or []))
        else:
            say(f"план {plan_id}: не смог поставить правку плана — проверь `hermes kanban ls`")
        return result
    if action == "create_scout_fix":
        # Правка плана по разведке (0.20.0): ключ планового уровня с номером круга, как у
        # ревью плана; `--parent` не ставится. Роль та же, что у правки по ревью (D3):
        # профиль, скиллы и лимит читаются теми же владельческими ключами конфига.
        round_no = int(decision.get("round") or 1)
        key = f"planloop:{plan_id}:{PLAN_SCOUTFIX_KEY}:{round_no}"
        profile = plan_fix_profile(p.cfg)
        cid = create_card(
            p, f"[правка по разведке {plan_id}] Правка плана по разведке (круг {round_no})",
            scout_fix_card_body(p, ctx, round_no), profile, key, plan_fix_skills(p.cfg),
            max_runtime=plan_fix_runtime(p.cfg))
        if reused_card(cid, key, "правка плана по разведке"):
            return result
        result.update(applied=bool(cid), detail=f"карточка правки плана по разведке: {cid}")
        if cid:
            # Сбой создания состояние не пишет (как у ревью плана): без отметки следующий
            # тик честно повторит заказ, а план не «станет идущим» из-за промаха борды.
            touch(f"create_scout_fix:{cid}")
            say(f"план {plan_id}: разведка не учтена в плане — правку заказываю карточкой "
                f"{cid} (профиль {profile}, круг {round_no}); после правки план надо "
                f"переутвердить")
        else:
            say(f"план {plan_id}: не смог поставить правку плана по разведке — "
                f"проверь `hermes kanban ls`")
        return result
    if action == "create_prompt":
        # Постановка N+1: заказ заранее (правило 2) и заказ вместо закрытия (правило 1).
        step = decision["data"]
        step_id = step["id"]
        after_close = bool(decision.get("after_close"))
        block_reason = str(decision.get("reason") or "")
        try:
            contract = collector(p, step_id).get("next") or {}
        except SystemExit as exc:
            reason = f"постановка {step_id} не заказана: {exc}"
            write_stop(p, reason, "gate", kind="incident")
            result.update(applied=True, detail=reason)
            touch("stop:no_contract")
            say("⛔ " + reason)
            return result
        data = contract or step
        key = f"planloop:{plan_id}:{step_id}:prompt"
        cid = create_card(p, f"[постановка {plan_id} {step_id}] {data.get('title', '')}",
                          prompt_order_body(p, data), p.cfg.prompter, key,
                          p.cfg.prompt_skills, max_runtime=p.cfg.prompt_runtime)
        where = f"карточка {cid}" if cid else "карточку заказа создать не удалось"
        ordered = f"постановка {step_id} заказана заранее: {where}"
        result.update(applied=bool(cid),
                      detail=f"{block_reason}; {ordered}" if after_close else ordered)
        touch(f"create_prompt:{cid}")
        # Молчим, если это плановый предзаказ: сообщение — только про гейт закрытия.
        if after_close:
            say(f"⛔ {block_reason}. {ordered} — как только постановка появится, шаг закроется сам")
        return result
    if action == "create_review":
        step, attempt = decision["data"], decision["attempt"]
        key = f"planloop:{plan_id}:{step['id']}:review:{attempt}"
        report_rel = _report_rel(p, step["id"])
        cid = create_card(p, f"[ревью {plan_id} {step['id']} #{attempt}] {step['title']}",
                          review_card_body(p, step, attempt, report_rel,
                                           prompt_rel(p, step["id"]),
                                           runs=ctx.get("step_runs")),
                          p.cfg.reviewer, key, p.cfg.review_skills,
                          parent=decision.get("parent"),
                          max_runtime=p.cfg.review_runtime)
        result.update(applied=bool(cid), detail=f"карточка ревью: {cid}")
        touch(f"create_review:{cid}")
        if cid:
            say(f"план {plan_id}, шаг {step['id']} исполнен, отправлен на ревью "
                f"(карточка {cid})")
            # Строка прогона в том же уведомлении, что и в карточке и в журнале (S2):
            # владелец без опыта читает её как единственный факт «чего проверили».
            runs = ctx.get("step_runs")
            counter = runs_counter(runs)
            if counter:
                say(f"  {counter} — доказательство "
                    f"{p.cfg.rel(runs_proof_path(p, step['id'], str(runs.get('sha') or '')))}")
        else:
            say(f"не смог поставить ревью шага {step['id']} плана {plan_id}")
        return result
    if action == "create_prompt_review":
        step = decision["data"]
        round_no = int(decision.get("round") or 1)
        key = f"planloop:{plan_id}:{step['id']}:promptreview"
        if round_no > 1:
            key += f":{round_no}"
        report_rel = prompt_review_rel(p, step["id"])
        cid = create_card(p, f"[ревью постановки {plan_id} {step['id']}] {step['title']}",
                          prompt_review_card_body(p, step, report_rel,
                                                  ctx.get("prompt") or prompt_rel(p, step["id"]),
                                                  round_no=round_no),
                          p.cfg.reviewer, key, p.cfg.review_skills,
                          max_runtime=p.cfg.review_runtime)
        result.update(applied=bool(cid), detail=f"карточка ревью постановки: {cid}")
        if cid:
            # Опора отпечатка входа (план 12, S1): «эта редакция уходит ревьюеру». Пишется и
            # тогда, когда идемпотентный ключ вернул прежнюю карточку: ревью относится именно
            # к этой редакции, а не к той, что была на момент прошлого заказа.
            saved = state.get("prompt_input")
            saved = dict(saved) if isinstance(saved, dict) else {}
            saved[step["id"]] = {
                "recon": ctx.get("prompt_recon") or "",
                "parts": ctx.get("prompt_input_parts") or {},
                "fp": ctx.get("prompt_input_fp") or "",
                "at": time.strftime("%d.%m.%Y %H:%M:%S"),
            }
            state["prompt_input"] = saved
        touch(f"create_prompt_review:{cid}")
        if cid:
            why = "старше кода" if round_no <= 1 else f"переписана, круг {round_no}"
            say(f"план {plan_id}: постановка шага {step['id']} {why} — поставил ревью "
                f"постановки (карточка {cid}), шаг не начинаю")
        else:
            say(f"план {plan_id}: не смог поставить ревью постановки шага {step['id']}")
        return result
    if action == "create_prompt_fix":
        step = decision["data"]
        round_no = int(decision.get("round") or 1)
        stale = bool(decision.get("stale"))
        report_rel = prompt_review_rel(p, step["id"])
        if stale:
            # Номер пересборки, а не номер круга правок: серии нумеруются отдельно, иначе
            # пересбор по свежести съедает бюджет кругов (6-S8A: два круга из шести).
            rebuild = int(decision.get("rebuild") or 1)
            limit = int(decision.get("limit") or 0)
            key = f"planloop:{plan_id}:{step['id']}:promptfix-stale:{rebuild}"
            title = (f"Пересобрать протухшую постановку шага {step['id']} — {step['title']} "
                     f"(пересборка {rebuild} из {limit})")
            body = prompt_stale_card_body(p, step, decision, report_rel)
        else:
            key = f"planloop:{plan_id}:{step['id']}:promptfix:{round_no}"
            title = f"[переписать постановку {plan_id} {step['id']}] {step['title']}"
            # Пункты C7 «спека не готова» (8-S2B): круг открыт риск-гейтом, а не ревью.
            spec_items = [str(item) for item in (decision.get("spec") or []) if str(item)]
            if spec_items:
                title = f"[дописать спеку постановки {plan_id} {step['id']}] {step['title']}"
            # Открытые ID реестра — в тело карточки (12-S2): реестр решает вердикт, и правка
            # обязана закрыть именно его строки. Предел кругов — по риску шага (12-S3).
            body = prompt_fix_card_body(p, step, round_no, report_rel,
                                        findings=open_prompt_findings_lines(ctx),
                                        max_rounds=step_fix_limit(ctx, step),
                                        report_verdict=str(decision.get("report") or ""),
                                        spec=spec_items)
        cid = create_card(p, title, body, p.cfg.prompter, key, p.cfg.prompt_skills,
                          max_runtime=p.cfg.prompt_runtime)
        result.update(applied=bool(cid), detail=f"карточка переписывания постановки: {cid}")
        if cid and not stale and not decision.get("spec"):
            # База правила «0 закрытых» (12-S2): что было открыто до правки. Пишется только
            # круг правок — пересборка по свежести находок не закрывает и правило не вооружает.
            # Круг спеки (8-S2B) тоже не пишет: он открыт формой постановки, а не реестром,
            # и закрытые находки прошлого круга не должны читаться как «правка ничего не
            # закрыла».
            state["prompt_fix_baseline"] = {
                "step": step["id"],
                "round": round_no,
                "open_ids": [str(item.get("id") or "")
                             for item in (ctx.get("prompt_findings") or [])
                             if str(item.get("status") or "") == "открыто"],
                "at": time.strftime("%d.%m.%Y %H:%M:%S"),
            }
        if cid and stale:
            # Зеркало счётчика в состоянии плана: источник истины — карточки, но цифра должна
            # читаться в `--status` даже без борды.
            mirrors = state.get("stale_rebuilds")
            mirrors = dict(mirrors) if isinstance(mirrors, dict) else {}
            mirrors[step["id"]] = int(decision.get("rebuild") or 1)
            state["stale_rebuilds"] = mirrors
        touch(f"create_prompt_fix:{cid}")
        if cid:
            if stale:
                say(f"план {plan_id}: постановка шага {step['id']} протухла — "
                    f"пересобираю автоматически ({decision.get('rebuild')} из "
                    f"{decision.get('limit')}, карточка {cid})")
            elif decision.get("spec"):
                say(f"план {plan_id}: спека шага {step['id']} не готова (C7) — "
                    f"переписываю постановку автоматически, круг {round_no} (карточка {cid})")
            else:
                say(f"план {plan_id}: реестр постановки шага {step['id']} держит "
                    f"{int(decision.get('open_count') or 0)} открытых находок (отчёт: "
                    f"{decision.get('report') or '—'}) — переписываю постановку автоматически, "
                    f"круг {round_no} (карточка {cid})")
        else:
            say(f"план {plan_id}: не смог поставить переписывание постановки шага {step['id']}")
        return result
    if action == "create_rereview":
        prev_id = decision["step"]
        try:
            step_data = collector(p, prev_id).get("next") or {}
        except SystemExit as exc:
            reason = f"повторное ревью {prev_id} не поставить: {exc}"
            write_stop(p, reason, "gate", kind="incident")
            result.update(applied=True, detail=reason)
            touch("stop:no_contract")
            say("⛔ " + reason)
            return result
        key = f"planloop:{plan_id}:{prev_id}:rereview:1"
        report_rel = _report_rel(p, prev_id)
        cid = create_card(p, f"[ревью {plan_id} {prev_id} #2] {step_data.get('title', '')}",
                          review_card_body(p, step_data, 2, report_rel,
                                           prompt_rel(p, prev_id)),
                          p.cfg.reviewer, key, p.cfg.review_skills,
                          parent=decision.get("parent"), max_runtime=p.cfg.review_runtime)
        result.update(applied=bool(cid), detail=f"повторное ревью {prev_id}: {cid}")
        touch(f"create_rereview:{cid}")
        if cid:
            say(f"план {plan_id}, шаг {prev_id} закрыт в журнале, но ревью было REQUEST_CHANGES — "
                f"ставлю повторное ревью (карточка {cid})")
        else:
            say(f"не смог поставить повторное ревью шага {prev_id}")
        return result
    if action == "create_fix":
        step, round_no = decision["data"], decision["round"]
        # Красный прогон (план 9, S2, §4 п.7): карточка того же ключа `:fix:<номер>`,
        # но текст собирает `runs_fix_card_body`, реестр шага НЕ заводится (отчёта ревью
        # ещё нет), а в уведомление идут числа прогона, а не находки ревьюера.
        by_runs = bool(decision.get("runs"))
        assignee, reason = assignee_or_stop(step)
        if assignee is None:
            return result
        rows: list[dict] = []
        if not by_runs:
            # Реестр шага (13-S2, §4.5): память круга заводится стражем до карточки — у
            # ревьюера файла ещё нет, а карточка правок обязана назвать открытые строки.
            # Ошибка реестра — инцидентный стоп: шаг не продолжается без памяти кругов.
            try:
                rows = ensure_step_findings(p, step, _report_rel(p, step["id"]))
            except (GateError, OSError, ValueError) as exc:
                reason = (f"реестр находок шага {step['id']} не заведён: {exc} — карточки "
                          f"правок нет, шаг без памяти кругов не продолжается")
                write_stop(p, reason, "gate", kind="incident")
                result.update(applied=True, detail=reason)
                touch("stop:step_findings")
                say("⛔ " + reason)
                return result
        key = f"planloop:{plan_id}:{step['id']}:fix:{round_no}"
        report_rel = _report_rel(p, step["id"])
        max_rounds = step_fix_limit(ctx, step)
        proof = dict(ctx.get("step_runs") or {}) if by_runs else {}
        if by_runs:
            title = f"[прогоны {plan_id} {step['id']} #{round_no}] {step['title']}"
            body = runs_fix_card_body(p, step, round_no, proof, max_rounds)
        else:
            title = f"[правки {plan_id} {step['id']} #{round_no}] {step['title']}"
            body = fix_card_body(p, step, round_no, report_rel, max_rounds=max_rounds,
                                 report_verdict=str((ctx.get("verdict") or {})
                                                    .get("verdict") or ""))
        cid = create_card(p, title, body, assignee, key, p.cfg.step_skills,
                          parent=decision.get("parent"), max_runtime=p.cfg.step_runtime)
        result.update(applied=bool(cid), detail=f"карточка правок: {cid}")
        touch(f"create_fix:{cid}")
        if cid and by_runs:
            # id карточки — в `fix_card` доказательства (F07): без этой записи ветвь
            # «fix_card заполнен» недостижима, и тик заказал бы второй круг правок на том
            # же sha вопреки D6. Запись — до конца тика; сбой записи стоп инцидентный.
            proof["fix_card"] = str(cid)
            try:
                save_runs_proof(p, proof)
            except (GateError, OSError) as exc:
                reason = (f"карточка правок {cid} создана, но fix_card не записан в "
                          f"доказательство прогона шага {step['id']}: {exc}")
                write_stop(p, reason, "gate", kind="incident")
                result.update(applied=True, detail=reason)
                touch("stop:runs_fix_card")
                say("⛔ " + reason)
                return result
            ctx["step_runs"] = proof
            red = [row for row in proof.get("runs") or []
                   if isinstance(row, dict) and row.get("outcome") != "green"]
            msg = [f"план {plan_id}, прогоны шага {step['id']}: красных {len(red)} из "
                   f"{int(proof.get('total') or 0)} — карточка правок вместо ревью "
                   f"(круг {round_no} из {max_rounds})",
                   f"  {runs_counter(proof)}"]
            for row in red:
                msg.append(f"  {row.get('command')} → {row.get('outcome')}")
            msg.append(f"  карточка правок {cid}")
            say("\n".join(msg))
            # Комментарий карточки — тем же текстом, что тело (§4 п.7): отказ не меняет
            # result и не рвёт тик.
            _comment(p, cid, body)
        elif cid:
            findings = _review_findings(Path(p.repo) / report_rel, 3)
            msg = [f"план {plan_id}, ревью {step['id']}: нужны правки, круг {round_no} "
                   f"из {max_rounds}"]
            if findings:
                msg.append("  что нашёл ревьюер:")
                msg += [f"  - {f}" for f in findings]
            else:
                msg.append("  замечания в отчёте — разбор ниже")
            msg.append(f"  подробно: {report_rel}")
            msg.append(f"  карточка правок {cid}")
            # Реестр шага — строкой в уведомление: решение по кругу принимается по нему.
            opened = sum(1 for item in rows if str(item.get("status") or "") == "открыто")
            msg.append(f"  реестр шага: открытых {opened}, всего {len(rows)}, "
                       f"файл {step_findings_rel(p, step['id'])}")
            say("\n".join(msg))
            # Находки — в саму карточку правок: отказ комментария не меняет result и не
            # рвёт тик (в `tick` ложный `applied` обрывает цепочку действий).
            _comment(p, cid, fix_comment_body(p, step, round_no, report_rel,
                                              decision.get("parent")))
        elif by_runs:
            say(f"прогоны {step['id']} красные, но карточку правок создать не удалось")
        else:
            say(f"ревью {step['id']}: REQUEST_CHANGES, но карточку правок создать не удалось")
        return result
    if action == "create_fix_return":
        # Возврат карточки правок того же круга (13-S4, §4.5): ответ не по форме. Новой
        # карточки круга нет — ключ с суффиксом `:return`, и `fixes` от неё не растёт.
        step, round_no = decision["data"], int(decision.get("round") or 1)
        problems = [str(item) for item in (decision.get("problems") or []) if str(item)]
        fix_card_id = decision.get("parent")
        fix_card = next((card for card in (ctx.get("cards") or [])
                         if str(card.get("id") or "") == str(fix_card_id)), {})
        assignee = str(fix_card.get("assignee") or "")
        if not assignee:
            # Чужой профиль возврату не назначается: пусто в карточке — маршрут шага.
            assignee, reason = assignee_or_stop(step)
            if assignee is None:
                return result
        key = f"planloop:{plan_id}:{step['id']}:fix:{round_no}:return"
        report_rel = _report_rel(p, step["id"])
        cid = create_card(
            p, f"[возврат правок {plan_id} {step['id']} #{round_no}] {step['title']}",
            fix_return_card_body(p, step, round_no, problems, report_rel),
            assignee, key, p.cfg.step_skills,
            parent=fix_card_id, max_runtime=p.cfg.step_runtime)
        ids = ", ".join(problems)
        result.update(applied=bool(cid), detail=f"возврат карточки правок: {cid}, "
                                               f"без формы: {ids}")
        touch(f"create_fix_return:{cid}")
        if cid:
            say(f"план {plan_id}, шаг {step['id']}: ответ без формы по пунктам {ids} — "
                f"вернул карточку, круг {round_no}, возврат 1 из 1")
            # Комментарий по номерам — уведомление, а не источник факта: отказ не меняет
            # result и не рвёт тик.
            _comment(p, str(fix_card_id or ""),
                     fix_return_comment_body(p, step, round_no, problems, fix_card_id))
        else:
            say(f"план {plan_id}, шаг {step['id']}: ответ без формы по пунктам {ids}, "
                f"но карточку возврата создать не удалось")
        return result
    if action == "close_step":
        step, verdict = decision["data"], decision["verdict"]
        nxt = ctx.get("next_step")
        # Защитная проверка гейта (D10): решение такое состояние уже отсекает, но гонка
        # (постановку убрали между тиками) не должна закрыть шаг без постановки N+1.
        if (ctx.get("prefetch") and ctx.get("require_prompt") and nxt
                and ctx.get("next_prompt") is None):
            result.update(applied=False, detail=close_block_reason(step["id"], nxt["id"]))
            return result
        commit = verdict.get("commit") or ""
        if commit:
            exists = _run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], Path(p.repo))
            if exists.returncode != 0:
                reason = (f"ревью {step['id']} APPROVE ссылается на несуществующий коммит "
                          f"{commit} — конвейер остановлен")
                write_stop(p, reason, "gate")
                result.update(applied=True, detail=reason)
                touch("stop:bad_commit")
                say("⛔ " + reason)
                return result
        remarks = int(decision.get("remarks") or 0)
        added = append_journal_row(p, step["id"], step["title"], commit,
                                   _evidence(p, verdict, remarks, step["id"]))
        if added and remarks:
            # Замечания не теряются (13-S1, §4.3): строки реестра — ПОСЛЕ журнала (он —
            # единственное доказательство закрытия) и ДО коммита (реестр входит в тот же
            # коммит). Повторный тик не дописывает: `append_journal_row` вернула False.
            try:
                _register_remarks(p, step, verdict)
            except (GateError, OSError, ValueError) as exc:
                reason = (f"замечания ревью {step['id']} не попали в реестр находок плана "
                          f"({plan_findings_rel(p)}): {exc} — шаг не закрыт коммитом")
                write_stop(p, reason, "gate")
                result.update(applied=True, detail=reason)
                touch("stop:findings")
                say("⛔ " + reason)
                return result
        files = [p.cfg.rel(Path(p.plan_file)), _report_rel(p, step["id"])]
        if remarks and plan_findings_path(p).is_file():
            files.append(plan_findings_rel(p))
        subject = (f"docs(plan): закрыт шаг {step['id']} плана {plan_id} "
                   f"(принято с замечаниями, находок вне блокирующих: {remarks})"
                   if remarks else
                   f"docs(plan): закрыт шаг {step['id']} плана {plan_id} (ревью APPROVE)")
        ok, detail = commit_fn(p, files, subject)
        tail = (f"; принято с замечаниями, находок вне блокирующих: {remarks}"
                if remarks else "")
        result.update(applied=ok, detail=f"журнал: {'добавлена строка' if added else 'уже был'}; "
                                         f"коммит/пуш: {'ok ' + detail if ok else detail}{tail}")
        # sha коммита для истории: свой коммит стража из detail, иначе — коммит из ревью (D5).
        match = re.match(r"^([0-9a-f]{7,40})(?:\s|$)", detail.strip())
        result["commit"] = match.group(1) if match else commit
        touch(f"close_step:{step['id']}")
        if ok:
            closing = (f"принято с замечаниями, находок вне блокирующих: {remarks}"
                       if remarks else f"ревью APPROVE, коммит {detail}")
            # Строка прогона (план 9, S2) — та же, что в журнале и в карточке: владелец
            # читает её в момент закрытия, а не из файла журнала.
            counter = runs_counter(runs_proof_for(p, step["id"], commit))
            say(f"✅ план {plan_id}, шаг {step['id']} закрыт ({closing}). "
                + (f"{counter}. " if counter else "")
                + "Следующий шаг стартует автоматически")
        else:
            state["pending_commit"] = {"files": files, "step": step["id"], "reason": detail,
                                       "message": subject}
            save_state(p, state)
            say(f"шаг {step['id']} принят ревью, но коммит/пуш не выполнен: {detail}. "
                f"Повторю сам, как только дерево освободится")
        return result
    if action == "write_retro":
        path = Path(p.cfg.reviews_dir) / f"retro-{p.plan_id}.md"
        rel = p.cfg.rel(path)
        if path.exists():                      # идемпотентность: файл уже положен (D8)
            result.update(applied=False, detail=f"ретроспектива уже есть: {rel}")
            return result
        try:
            text = Path(p.plan_file).read_text(encoding="utf-8", errors="replace")
            # Причины кругов — из истории тиков (§4.3): ретроспектива отвечает на «что мешало»
            # числами, а не пересказом. Истории нет — счёт нулевой, строка та же: владелец
            # видит «0 кругов», а не догадывается, считали ли вообще.
            records, _total, _bad = history_lines(p.cfg)
            rounds = round_cause_counts(records, p.plan_id)
            state_io.atomic_write_text(
                path, analyze.retro_text(text, p.cfg.rel(Path(p.plan_file)), p.plan_id,
                                         _local_date(_now()), rounds))
        except OSError as exc:
            print(f"[retro] черновик не записан: {exc}", file=sys.stderr)
            result.update(applied=False, detail=f"ретроспектива не записана: {rel}")
            return result
        result.update(applied=True, detail=f"черновик ретроспективы: {rel}")
        touch(f"retro:{p.plan_id}")
        say(f"📝 план {p.plan_id} закрыт — ретроспектива: {rel} (заполняет владелец)")
        return result
    if action == "note_growth":
        # Правило понижено до замера (умолчание с 19.11.2026): цифры в состояние, работа
        # дальше. Состояние сохраняется здесь же: метка версии в базе правки не даёт писать
        # этот же замер каждый тик — без записи на диск тик повторял бы замер бесконечно.
        growth = decision.get("growth")
        detail = str(decision.get("reason") or "замер роста плана")
        if isinstance(growth, dict) and growth:
            _record_plan_growth(state, ctx, growth)
            save_state(p, state)
            touch(f"growth:{p.plan_id}")
        result.update(applied=False, detail=detail)
        return result
    if action == "stop":
        reason = decision.get("reason", "")
        # Отчёт о расширении плана (S4) — в состояние плана: цифры переживают тик, а метка
        # версии плана говорит, что по ней решение уже вынесено (иначе owner-стоп
        # повторялся бы после каждого `--resume`).
        growth = decision.get("growth")
        if isinstance(growth, dict) and growth:
            _record_plan_growth(state, ctx, growth)
        # Отметка «доложено» у правила «0 закрытых» (12-S2): owner-стоп ставится один раз,
        # иначе он повторялся бы каждый тик и после `--resume`. Касается только решения с
        # `zero_fix`: прочие стопы базу правки не трогают и правило не гасят.
        zero_fix = decision.get("zero_fix")
        if zero_fix:
            fix_baseline = state.get("prompt_fix_baseline")
            if isinstance(fix_baseline, dict) and fix_baseline:
                fix_baseline["reported_round"] = int(zero_fix)
        previous = (ctx.get("stop") or {}).get("reason")
        # `kind` из решения: эскалация владельцу (`owner`) не должна сниматься тиком сама (D6).
        write_stop(p, reason, "gate", kind=decision.get("kind") or "incident")
        # Отметка «по этой карточке уже доложено» (план 10, S2A): owner-стоп снимает только
        # владелец, и без отметки тот же стоп ставился бы после каждого `--resume`. Пишет
        # применение, а не `decide`: решение остаётся чистым и без записи на диск.
        marked = decision.get("card_deadline")
        if marked and str(state.get("card_deadline_reported") or "") != str(marked):
            state["card_deadline_reported"] = str(marked)
            save_state(p, state)
        result.update(applied=True, detail=reason)
        touch("stop")
        # Замер — комментарием в карточку правок: без него лимит выглядел бы молчаливым
        # запретом (исполнитель не узнал бы, за что получил стоп). Карточка названа в самом
        # решении: второй раз искать её перебором `ctx` незачем. Отказ `_comment` —
        # предупреждение, а не тишина: цифры остались в состоянии плана, и владелец узнаёт,
        # что исполнитель их не увидел.
        comment = decision.get("comment") or {}
        if growth and comment.get("card") and comment.get("text"):
            if _comment(p, str(comment["card"]), str(comment["text"])):
                say(f"📏 замер расширения плана — комментарием в карточку {comment['card']}")
            else:
                say(f"  замер расширения плана не ушёл комментарием "
                    f"(карточка {comment['card']}) — он остался в состоянии плана: "
                    f"plan_growth_report")
        if previous != reason:
            extra = decision.get("details") or ""
            # Owner-стоп тик не снимает сам (`decide`), поэтому не обещаем самовосстановление.
            owner_stop = (decision.get("kind") or "") == "owner"
            todo = [line.lstrip("-—•* ").strip() for line in extra.splitlines() if line.strip()]
            todo = [item for item in todo if item]
            todo.append("снять стоп, когда причина устранена: python .pipeline/gate.py --resume")
            attention(p.cfg, attention_text(
                p.plan_id, reason,
                evidence=[f"тип остановки: {'решение владельца' if owner_stop else 'инцидент'}",
                          f"стоп-флаг: {Path(p.stop_file).name}",
                          "до снятия стопа шаги плана не двигаются"],
                todo=todo,
                commands=[f"python .pipeline/gate.py --status --plan {p.plan_id}",
                          "python .pipeline/gate.py --resume"]),
                f"stall:{p.plan_id}", say)
        return result
    return result


def retry_pending(p: Paths, notify_fn=None) -> dict | None:
    """Повторить отложенный коммит/пуш (дерево было занято чужой сессией).

    Тема берётся из записи (`message`), а не собирается заново: шаг, принятый с замечаниями,
    обязан получить тему про замечания, а не «ревью APPROVE». Прежняя тема — только
    умолчание для старых записей состояния, где поля `message` ещё нет.
    """
    state = load_state(p)
    pending = state.get("pending_commit")
    if not pending:
        return None
    message = str(pending.get("message") or
                  f"docs(plan): закрыт шаг {pending['step']} плана {p.plan_id} (ревью APPROVE)")
    ok, detail = commit_and_push(p, pending["files"], message)
    if ok:
        state.pop("pending_commit", None)
        save_state(p, state)
        say = notify_fn or (lambda text: notify(p.cfg, text))
        say(f"✅ отложенный коммит шага {pending['step']} выполнен: {detail}")
        return {"applied": True, "detail": f"pending commit ok {detail}"}
    return {"applied": False, "detail": f"pending commit всё ещё ждёт: {detail}"}


# ───────────────────────────── обзор планов ─────────────────────────────

# Подписи состояний печатает панель: страница их не придумывает (план 5 S2A, R1).
STATE_LABELS = {"running": "идёт", "stopped": "остановлен",
                "closed": "закрыт", "not-driven": "не ведётся"}
#: Метка утверждения плана: своей строки состояния у него нет (`state` — про конвейер),
#: поэтому панель показывает его отдельным бейджем рядом с состоянием.
APPROVAL_LABELS = {"approved": "утверждён", "changed": "изменён после утверждения",
                   "missing": "не утверждён"}
CRON_JOB_NAME = "План: страж шага (конвейер)"
# Причина строки плана в состоянии «не ведётся». Тот же текст — телом отказа адресному
# тику (`not_driven_refusal`): номер плана ставит движок, поэтому ведущего слова «план»
# в константе нет — иначе отказ «план 12 не в driven_plans …» перестал бы содержать её.
NOT_DRIVEN_REASON = "не в driven_plans — конвейер его не ведёт"
TICK_ACTION_TEXT = "устранить причину и запустить тик"


def driven_command(cfg: config_mod.Config, plan_id: str) -> str:
    """Команда включения плана в ведение: живой `driven_plans` плюс запрошенный номер.

    Список в команде — `driven_plans` проекта как есть плюс номер в конце; значение
    `hermes pipeline config --set` читает через `json.loads`, отсюда двойные кавычки
    внутри одинарных. Собирается в одной функции, потому что строку читают двое: текст
    отказа адресному тику (`not_driven_refusal`) и поле `command` действия `drive` строки
    плана (`plan_view`) — панель показывает её подсказкой кнопки «Тик» у плана вне ведения
    (S2 плана 14). Вторая копия формы разошлась бы с первой молча.
    """
    want = registry.normalize(plan_id)
    payload = json.dumps([*cfg.driven, want], ensure_ascii=False)
    return f"hermes pipeline config --set driven_plans '{payload}'"


def not_driven_refusal(cfg: config_mod.Config, plan_id: str) -> str:
    """Текст отказа тику по адресу плана вне ведения — с командой включения.

    Содержит `NOT_DRIVEN_REASON` дословно: тем же текстом панель подписывает строку
    плана, поэтому движок и страница говорят об одном плане одинаково. Команда
    включения — `driven_command`: та же строка отдаётся полем `command` действия `drive`.
    """
    want = registry.normalize(plan_id)
    return (f"план {want} {NOT_DRIVEN_REASON}; включить: {driven_command(cfg, want)}")


def _cron_command(cfg: config_mod.Config) -> str:
    """Команда джобы стража — та же строка, что печатает мастер установки (R6)."""
    return (f'hermes -p {cfg.driver} cron create "*/10 * * * *" --name "{CRON_JOB_NAME}" '
            f'--no-agent --script pipeline_tick.py --deliver local --workdir "{cfg.repo}"')


def _blocked_card(ctx: dict) -> dict | None:
    """Карточка в статусе blocked: её владелец и разблокирует (R4)."""
    blocked = [c for c in (list(ctx.get("cards") or []) + list(ctx.get("prev_cards") or []))
               if str(c.get("status") or "") == "blocked"]
    return blocked[-1] if blocked else None


def plan_view(cfg: config_mod.Config, p: Paths, driven: set[str], cron_ok: bool) -> dict:
    """Состояние одного плана для панели: статус, причина, пометка, одно действие.

    Единственный источник статуса — решение стража (`build_ctx` + `decide`), те же вызовы,
    что у `--status`; наличие `plan_<N>_stop.json` само по себе ничего не значит (S2A).
    На диск функция ничего не пишет. Дополнительно отдаёт `next`, `risk`, `hold` и `stop`,
    чтобы `plans_rows` не запускал сборщик шагов второй раз.
    """
    run_cmd = f"python .pipeline/gate.py --plan {p.plan_id} --run"
    try:
        ctx = build_ctx(p)
        decision = decide(ctx)
    except GateError as exc:
        # Сбой движка не роняет весь `--plans`: строка с причиной полезнее пустого ответа.
        return {"state": "stopped", "state_label": STATE_LABELS["stopped"],
                "reason": str(exc), "note": "",
                "action": {"kind": "tick", "text": TICK_ACTION_TEXT, "command": run_cmd},
                "next": "—", "risk": "", "hold": [], "stop": None}
    step = ctx.get("next") or None
    sid = str(step.get("id") or "") if step else ""
    hold = sorted({str(h).upper() for h in (ctx.get("hold") or [])})
    stop = ctx.get("stop")
    kind = stop_kind(stop)
    # «Стоп держит» — это решение стража, а не файл: остаточный флаг владельца держит
    # всегда, инцидентный — пока причина на месте. Решение `stop` без файла тоже стоп:
    # карточка шага упала или заблокирована, и страж уже не идёт.
    holds = (kind == "owner"
             or (kind == "incident" and bool(decision.get("incident")))
             or (not stop and decision.get("action") == "stop"))
    # Порядок проверки жёсткий (R1): план 1 закрыт и не ведётся одновременно, а на панели
    # он обязан быть «закрыт». Сбой сборщика — не «закрыт»: это стоп с причиной.
    if not step and not ctx.get("collector_error"):
        state = "closed"
    elif p.plan_id not in driven:
        state = "not-driven"
    elif holds:
        state = "stopped"
    else:
        state = "running"

    note = ""
    if state == "running" and stop and decision.get("clear_incident"):
        note = "стоп снят (было: " + str(stop.get("reason") or "") + ")"

    if state == "closed":
        reason = str(decision.get("reason") or "")
    elif state == "not-driven":
        reason = NOT_DRIVEN_REASON
    elif state == "stopped" and kind == "owner":
        reason = str(stop.get("reason") or "")
    else:
        reason = str(decision.get("reason") or "")

    # Приоритет сверху вниз, побеждает первое совпадение (R4).
    if state == "stopped" and kind == "owner":
        action = {"kind": "resume", "text": "снять стоп: Продолжить",
                  "command": f"python .pipeline/gate.py --plan {p.plan_id} --resume"}
    elif state == "stopped":
        card = _blocked_card(ctx)
        if card:
            action = {"kind": "unblock",
                      "text": f"решение владельца по {card['id']}, затем разблокировать",
                      "command": f"hermes kanban --board {cfg.board} unblock {card['id']}"}
        else:
            action = {"kind": "tick", "text": TICK_ACTION_TEXT, "command": run_cmd}
    elif decision.get("action") == "hold_risk" or (sid and sid.upper() in set(hold)):
        action = {"kind": "unhold", "text": f"снять ручной допуск: unhold {sid}",
                  "command": f"python .pipeline/gate.py --plan {p.plan_id} --unhold {sid}"}
    elif decision.get("approval_blocked"):
        action = {"kind": "approve", "text": "утвердить план",
                  "command": f"hermes pipeline plan --accept {p.plan_id}"}
    elif state == "not-driven":
        # Подсказка — та же команда, которой отказывает адресный тик (`driven_command`):
        # у плана вне ведения панель гасит «Тик» и объясняет, чем план включить (S2 плана 14).
        action = {"kind": "drive", "text": "добавить план в driven_plans",
                  "command": driven_command(cfg, p.plan_id)}
    elif step and not stop and not hold and not cron_ok:
        action = {"kind": "cron", "text": "включить джобу стража", "command": _cron_command(cfg)}
    else:
        action = {"kind": "none",
                  "text": "закрыт, действий не требуется" if state == "closed" else "",
                  "command": ""}
    # Утверждение — отдельный факт, а не состояние конвейера: план бывает утверждён и
    # при этом «ждёт ревью» или «остановлен». Панель раньше не показывала утверждение
    # вовсе, и после нажатия «Утвердить» на экране ничего не менялось.
    approval_state = ctx.get("approval") or {}
    approval_kind = str(approval_state.get("state") or "missing")
    approval = {"state": approval_kind,
                "label": APPROVAL_LABELS.get(approval_kind, APPROVAL_LABELS["missing"]),
                "approved_at": str((approval_state.get("record") or {}).get("approved_at") or ""),
                "block": str(approval_state.get("block_kind") or ""),
                "ok": bool(approval_state.get("ok"))}
    return {"state": state, "state_label": STATE_LABELS[state], "reason": reason,
            "note": note, "action": action, "next": sid or "—", "approval": approval,
            # Шага нет — риска нет: пустая строка, а не «normal», иначе данные врут.
            "risk": str(step.get("risk") or "normal") if step else "",
            "hold": hold, "stop": ({**stop, "kind": kind} if stop else None)}


def plans_rows(cfg: config_mod.Config) -> list[dict]:
    """Машиночитаемая таблица планов (для UI и внешних вызовов)."""
    driven = set(registry.driven_ids(cfg))
    # Признак заведённой джобы стража — метка мастера установки, без вызова `hermes` (R6).
    cron_ok = (Path(cfg.repo) / ".pipeline" / "cron.json").is_file()
    out: list[dict] = []
    for spec in registry.specs(cfg):
        p = resolve_paths(cfg, spec.id)
        view = plan_view(cfg, p, driven, cron_ok)
        out.append({
            "id": spec.id,
            "file": cfg.rel(Path(spec.file)),
            "driven": spec.id in driven,
            "next": view["next"],
            "risk": view["risk"],
            "hold": view["hold"],
            "closed": len(registry.closed_steps(spec, cfg)),
            "stop": view["stop"],
            "state": view["state"],
            "state_label": view["state_label"],
            "reason": view["reason"],
            "note": view["note"],
            "action": view["action"],
            "approval": view["approval"],
        })
    return out


def plans_table(cfg: config_mod.Config) -> str:
    """Таблица планов: ведётся ли, следующий шаг, сколько закрыто, стоп-флаг."""
    rows: list[tuple[str, str, str, str, str]] = []
    for row in plans_rows(cfg):
        stop = row["stop"]
        if stop:
            flag = f"{stop['kind']}: {stop.get('reason', '')}"
        elif row["hold"]:
            flag = "hold: " + ", ".join(row["hold"])
        else:
            flag = "—"
        rows.append((row["id"], "да" if row["driven"] else "нет", str(row["next"]),
                     str(row["closed"]), flag))
    head = ("ПЛАН", "ВЕДЁТСЯ", "СЛЕДУЮЩИЙ ШАГ", "ЗАКРЫТО", "СТОП-ФЛАГ")
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) if rows else len(head[i])
              for i in range(5)]
    lines = ["  ".join(head[i].ljust(widths[i]) for i in range(4)) + "  " + head[4]]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4])
    if not rows:
        lines.append("планов с распознанным журналом не найдено")
    return "\n".join(lines)


def _step_of(decision: dict) -> str:
    """Идентификатор шага, которого касается решение (пустая строка — шага нет)."""
    step = decision.get("step")
    if not step:
        data = decision.get("data")
        step = data.get("id") if isinstance(data, dict) else None
    return str(step).upper() if step else ""


def step_key(plan_id: str, decision: dict) -> str | None:
    """Ключ шага, который трогает решение, вида `1:S1`; None — решение шага не называет.

    Чистая функция: не зависит от путей и состояния, поэтому проверяется юнит-тестом.
    Нужна инварианту «одно действие на шаг за тик» (D5): по ключу видно, что следующее
    решение адресует шаг, который в этом тике уже получал действие.
    """
    step = _step_of(decision)
    return f"{plan_id}:{step}" if step else None


# ──────────────────────── история тиков и метрики ────────────────────────

def history_lines(cfg: config_mod.Config) -> tuple[list[dict], int, int]:
    """История тиков: (валидные записи, всего непустых строк, битых строк).

    Валидная запись — объект JSON с числовым `ts` и непустым `action`. Всё остальное
    (не JSON, JSON не объект, нет `ts`/`action`) — битая строка: в подсчётах метрик она
    не участвует, но считается в `bad_lines` (D7). Файла нет или он не читается — пустая
    история, а не падение: метрики обязаны собираться на любом состоянии проекта.
    """
    path = history_path(cfg)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0, 0
    records: list[dict] = []
    total = 0
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            bad += 1
            continue
        if not isinstance(row, dict):
            bad += 1
            continue
        ts = row.get("ts")
        if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
                or not str(row.get("action") or "")):
            bad += 1
            continue
        records.append(row)
    return records, total, bad


def history_size(cfg: config_mod.Config) -> int:
    """Размер файла истории в байтах; файла нет — ноль."""
    try:
        return int(history_path(cfg).stat().st_size)
    except OSError:
        return 0


def append_history(cfg: config_mod.Config, record: dict) -> None:
    """Дописать строку истории и уложить файл в пределы ротации (D6).

    Обрезка идёт по строкам, а не по байтам: разрезанная строка JSONL — это битая строка.
    Последняя записанная строка не выбрасывается никогда, даже если одна превышает предел
    размера. Запись атомарная, через `state_io.atomic_write_text`.
    """
    path = history_path(cfg)
    try:
        old = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        old = []
    lines = [line for line in old if line.strip()]
    lines.append(json.dumps(record, ensure_ascii=False))
    if len(lines) > HISTORY_MAX_LINES:
        lines = lines[-HISTORY_MAX_LINES:]
    # Размер считаем по байтам ровно так, как их запишет `atomic_write_text`
    # (он переводит `\n` в os.linesep), и вычитаем отброшенные строки без пересчёта.
    sep = os.linesep
    size = len(sep.join(lines).encode("utf-8")) + len(sep)
    drop = 0
    while drop < len(lines) - 1 and size > HISTORY_MAX_BYTES:
        size -= len(lines[drop].encode("utf-8")) + len(sep)
        drop += 1
    lines = lines[drop:]
    state_io.atomic_write_text(path, "\n".join(lines) + "\n")


def round_cause_counts(records: list[dict], plan_id: str) -> dict:
    """Круги правок плана по причинам — по истории тиков (13-S5, §4.3).

    Считаются те же круги, что и у метрики (`stats._step_rounds`, R10): `create_fix` и
    `create_prompt_fix` без `stale`. Причина `create_fix` — поле `cause` записи (три класса
    находки), пустое или чужое значение — `unknown`; у `create_prompt_fix` причина всегда
    `prompt`: постановку переписывают не из-за класса находки, и поле её не дублирует.
    Пересборка протухшей постановки и повтор ревью в счёт не идут — бюджетов они не тратят.

    Ключи: `total` и по причине — `blocking`, `significant`, `style`, `prompt`, `unknown`.
    """
    counts = {"total": 0, "blocking": 0, "significant": 0, "style": 0,
              "prompt": 0, "unknown": 0}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("plan") or "") != str(plan_id):
            continue
        action = str(record.get("action") or "")
        if action == "create_fix":
            counts["total"] += 1
            counts[ROUND_CAUSE_KEYS.get(str(record.get("cause") or ""), "unknown")] += 1
        elif action == "create_prompt_fix" and not record.get("stale"):
            counts["total"] += 1
            counts["prompt"] += 1
    return counts


def _median(values: list[float]) -> int:
    """Медиана, округлённая до целых; чётное число — среднее двух центральных."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    value = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return int(round(value))


def _plan_metrics(p: Paths, records: list[dict]) -> dict:
    """Метрики одного плана: закрытые по журналу шаги, время шага, правки, инциденты."""
    steps_closed = 0
    try:
        lines = Path(p.plan_file).read_text(encoding="utf-8", errors="replace").splitlines()
        steps_closed = sum(1 for i in _journal_rows(p)
                           if registry.status_of_row(lines[i]).startswith("ЗАКРЫТ"))
    except (OSError, SystemExit):
        steps_closed = 0
    first_seen: dict[str, float] = {}
    closed_at: dict[str, float] = {}
    fix_rounds = 0
    incidents = 0
    for row in records:
        if str(row.get("plan") or "") != p.plan_id:
            continue
        action = str(row.get("action") or "")
        step = str(row.get("step") or "").upper()
        ts = float(row["ts"])
        if step:
            if step not in first_seen or ts < first_seen[step]:
                first_seen[step] = ts
            if action == "close_step" and step not in closed_at:
                closed_at[step] = ts
        if action == "create_fix":
            fix_rounds += 1
        elif action == "stop":
            incidents += 1
    seconds = [closed_at[step] - first_seen[step] for step in closed_at
               if step in first_seen and closed_at[step] >= first_seen[step]]
    step_seconds = {"avg": int(round(sum(seconds) / len(seconds))) if seconds else 0,
                    "median": _median(seconds) if seconds else 0,
                    "measured": len(seconds)}
    return {"steps_closed": steps_closed, "step_seconds": step_seconds,
            "fix_rounds": fix_rounds, "incidents": incidents}


def history_metrics(cfg: config_mod.Config, now: float | None = None) -> dict:
    """Посчитать метрики по истории (D8). Ничего не пишет и не бросает наружу.

    `actions_7d` — строки не старше окна по видам действия; `steps_closed` — по журналу
    плана (поэтому совпадает с ним); `step_seconds` — по шагам, у которых в истории есть
    и первое действие, и `close_step`; `fix_rounds` и `incidents` — по всей истории.
    """
    stamp = time.time() if now is None else float(now)
    records, total, bad = history_lines(cfg)
    cutoff = stamp - METRICS_DAYS * 86400
    actions: dict[str, int] = {}
    for row in records:
        if float(row["ts"]) >= cutoff:
            action = str(row.get("action") or "")
            actions[action] = actions.get(action, 0) + 1
    plans: dict[str, dict] = {}
    for spec in registry.driven_specs(cfg):
        try:
            plans[spec.id] = _plan_metrics(resolve_paths(cfg, spec.id), records)
        except Exception:  # noqa: BLE001 - сбой по одному плану не ломает остальные
            plans[spec.id] = {"steps_closed": 0,
                              "step_seconds": {"avg": 0, "median": 0, "measured": 0},
                              "fix_rounds": 0, "incidents": 0}
    return {
        "generated_at": time.strftime("%d.%m.%Y %H:%M:%S"),
        "window_days": METRICS_DAYS,
        "actions_7d": actions,
        "actions_7d_total": sum(actions.values()),
        "plans": plans,
        "history": {"lines": total, "bad_lines": bad, "size_bytes": history_size(cfg)},
    }


def remember_action(p: Paths, plan_id: str, decision: dict, detail: str,
                    duration: float = 0.0, commit: str = "") -> None:
    """Записать применённое действие в состояние плана и в историю тиков.

    В состояние (`last_actions`, последние `LAST_ACTIONS_KEPT`) — как раньше: атомарно,
    через `load_state`/`save_state`, свежим чтением (`run_gate` уже мог сохранить
    `pending_commit`, и запись из устаревшего словаря стёрла бы его). `last_action`/
    `last_tick` из `touch()` не трогаем: на `last_action` опирается сторож простоя.

    В историю (`history.jsonl`) — строка на действие: `ts` (epoch, для окна и разностей),
    `at` тем же форматом, что в состоянии, план, шаг, действие, длительность в секундах,
    `detail` (те же 200 символов), sha коммита, если он был (D4), `stale` — признак
    пересборки протухшей постановки: по нему метрика (`stats._step_rounds`) отличает правки
    от пересборок, не разбирая `detail` (12-S3), и `cause` — причина круга правок, старший
    класс находки из решения (13-S5); у действий без причины поле пустое.
    """
    at = time.strftime("%d.%m.%Y %H:%M:%S")
    action = str(decision.get("action") or "")
    step = _step_of(decision)
    text = str(detail or "")[:200]
    state = load_state(p)
    history = state.get("last_actions")
    history = list(history) if isinstance(history, list) else []
    history.append({"at": at, "plan": plan_id, "action": action, "step": step,
                    "detail": text})
    state["last_actions"] = history[-LAST_ACTIONS_KEPT:]
    save_state(p, state)
    append_history(p.cfg, {"ts": time.time(), "at": at, "plan": plan_id, "step": step,
                           "action": action, "duration_s": round(float(duration), 3),
                           "detail": text, "commit": str(commit or ""),
                           "stale": bool(decision.get("stale")),
                           "cause": str(decision.get("cause") or "")})



def note_action_streak(p: Paths, entry: dict) -> int:
    """Сколько одинаковых действий подряд дало один и тот же результат.

    Счёт живёт в состоянии плана (`action_streak`) и сбрасывается любым другим действием.
    Нужен как общий сторож: заказ может «примениться» и не создать работы (идемпотентный
    ключ занят закрытой карточкой), и без счёта тик вечно пишет «выполнено».
    """
    state = load_state(p)
    signature = f"{entry.get('action')}|{entry.get('detail')}"
    streak = state.get("action_streak")
    streak = streak if isinstance(streak, dict) else {}
    count = int(streak.get("count") or 0) + 1 if streak.get("sig") == signature else 1
    state["action_streak"] = {"sig": signature, "count": count,
                              "action": str(entry.get("action") or ""),
                              "detail": str(entry.get("detail") or ""),
                              "at": _local_stamp(_now())}
    save_state(p, state)
    return count


def tick(cfg: config_mod.Config, execute: bool = True,
         max_actions: int | None = None) -> dict:
    """Один тик по ВСЕМ ведомым планам: до `max_actions` действий, по одному на шаг.

    Предел берётся из аргумента (флаг `--max-actions`), иначе из `max_actions` в
    `pipeline.json` проекта, иначе 1 — прежнее поведение «ровно одно действие».

    Порядок: суточная сводка (сбой не валит тик), затем обход планов. Перед каждым
    следующим действием состояние перечитывается (`build_ctx`), поэтому цепочка
    «журнал → следующий шаг → заказ постановки» проезжается одним тиком. Простаивающий
    план записывает ожидание — действием это не считается, как и `retry_pending` (D5).

    Тик заканчивается, когда: работы нет ни в одном плане; предел исчерпан; действие не
    применилось; либо следующее действие адресует шаг, который в этом тике уже получал
    действие (D5). Каждое применённое действие пишется в состояние плана.
    """
    limit = max_actions if isinstance(max_actions, int) and max_actions >= 1 \
        else max_actions_of(cfg)
    status_by_plan: dict[str, str] = {}
    if execute:
        try:
            send_digest(cfg)
        except Exception as exc:  # noqa: BLE001 - сводка не повод останавливать конвейер
            print(f"[digest] сводка не отправлена: {exc}", file=sys.stderr)
    actions: list[dict] = []
    touched: set[str] = set()
    retried_any: dict | None = None
    cleared_any = False
    no_work = "во всех планах нет работы"
    last_decision: dict = {"action": "idle", "reason": no_work}
    reason = no_work
    pass_no = 0
    while len(actions) < limit:
        pass_no += 1
        acted = False
        finish = False
        for spec in registry.driven_specs(cfg):
            p = resolve_paths(cfg, spec.id)
            if execute and pass_no == 1:
                # Отложенный коммит повторяем один раз за тик: один план — одна попытка.
                retried = retry_pending(p)
                if retried is not None:
                    retried_any = retried
                if retried and retried.get("applied"):
                    return {"plan_id": spec.id, "decision": {"action": "retry_pending"},
                            "applied": True, "detail": retried.get("detail", ""),
                            "incident_cleared": False, "retried": retried,
                            "status": retried.get("detail", ""), "actions": [],
                            "actions_count": 0, "reason": "выполнен отложенный коммит"}
            ctx = build_ctx(p)
            if execute:
                # Наследование утверждения ДО решения — и для простаивающего плана: до
                # run_gate такой план не доходит (`idle` → `continue`), запись оставалась
                # со старым sha, и панель вечно показывала «изменён после утверждения»
                # без кнопки (у стартовавшего плана блокировки утверждения нет).
                ensure_approval(p, ctx)
                # Автовыбор рекомендации по таймауту (S5, п.5) — и для простаивающего плана.
                # Вопрос держит шаг, решение выходит `idle`, а `run_gate` для `idle` не
                # зовётся; пока `sweep_questions` жил только внутри него, просроченный
                # вопрос не снимался ни одним тиком и план стоял до ответа человека
                # (20.11.2026: планы встали на S9 и S10 при истёкших дедлайнах). Тик
                # закрывает вопрос и на этом заканчивается — карточка шага, как и в
                # `run_gate`, следующим тиком (требование плана, п. 5).
                closed = sweep_questions(p.cfg, p, ctx, ctx.get("state"),
                                         lambda text: notify(cfg, text))
                if closed:
                    entry = {"plan": spec.id, "action": "question_auto",
                             "step": _step_of({"step": closed[-1].get("step")}),
                             "applied": True,
                             "detail": str(closed[-1].get("message") or "")}
                    actions.append(entry)
                    last_decision = {"action": "question_auto", "step": entry["step"],
                                     "question": closed[-1].get("qid"),
                                     "reason": entry["detail"]}
                    reason = entry["detail"]
                    # Строка статуса — та же, что увидит владелец: без неё вывод тика
                    # заканчивался бы «планов с распознанным журналом не найдено».
                    status_by_plan[spec.id] = entry["detail"]
                    remember_action(p, spec.id, last_decision, entry["detail"])
                    finish = True
                    break
            decision = decide(ctx)
            status_by_plan[spec.id] = status_text(ctx, decision)
            if decision["action"] == "idle":
                if execute:
                    if decision.get("approval_blocked"):
                        approval_notice(p, ctx, decision, lambda text: notify(cfg, text))
                    else:
                        watch_idle(cfg, p, ctx, decision, lambda text: notify(cfg, text))
                continue
            if not execute:
                last_decision = decision
                reason = "показ без исполнения: действие не применялось"
                finish = True
                break
            key = step_key(spec.id, decision)
            if key and key in touched:
                # Инвариант D5: шаг за тик получает не больше одного действия.
                last_decision = decision
                reason = (f"шаг {key} уже получал действие в этом тике — "
                          f"второй раз не трогаю")
                finish = True
                break
            started = time.monotonic()
            result = run_gate(p, execute=execute, ctx=ctx)
            duration = round(time.monotonic() - started, 3)
            last_decision = result.get("decision") or decision
            entry = {"plan": spec.id, "action": str(decision.get("action") or ""),
                     "step": _step_of(decision), "applied": bool(result.get("applied")),
                     "detail": str(result.get("detail") or "")}
            actions.append(entry)
            if key:
                touched.add(key)
            cleared_any = cleared_any or bool(result.get("incident_cleared"))
            if entry["applied"]:
                remember_action(p, spec.id, decision, entry["detail"], duration=duration,
                                commit=str(result.get("commit") or ""))
            # Зависание: план не двигается, а тик исправно пишет «выполнено» (пример —
            # заказ схлопнулся в уже закрытую карточку). Счёт повторов живёт в состоянии.
            streak = note_action_streak(p, entry)
            if streak >= stuck_repeats_of(cfg):
                stuck_reason = (f"{streak} тика подряд действие «{entry['action']}» "
                                f"не сдвинуло план {spec.id}: {entry['detail']}")
                write_stop(p, stuck_reason, "gate", kind="incident")
                attention(cfg, attention_text(
                    spec.id, stuck_reason,
                    evidence=[f"повторяется: {entry['action']} — {entry['detail']}",
                              f"одинаковых тиков подряд: {streak}",
                              "журнал считает действия выполненными, но план стоит"],
                    todo=["посмотреть состояние плана и хвост журнала тиков (команды ниже)",
                          "найти повторяющуюся карточку или ключ и устранить причину",
                          "обновить движок проекта, если причина в его версии",
                          "снять стоп и дождаться следующего тика"],
                    commands=[f"python .pipeline/gate.py --status --plan {spec.id}",
                              "tail -5 .pipeline/state/history.jsonl",
                              "hermes pipeline setup --upgrade --project <путь>",
                              "python .pipeline/gate.py --resume"]),
                    f"stall:{spec.id}")
                reason = stuck_reason
                finish = True
                break
            if not entry["applied"]:
                reason = (f"действие {entry['action']} не применилось: "
                          f"{entry['detail'] or 'подробностей нет'} — второй попытки в этом тике нет")
                finish = True
                break
            acted = True
            if key is None:
                reason = f"действие {entry['action']} шага не адресует — тик завершён"
                finish = True
            break                       # состояние перечитываем с начала обхода
        if finish:
            break
        if not acted:
            reason = no_work
            break
    else:
        reason = f"предел действий за тик исчерпан (max_actions={limit})"

    detail = "; ".join(f"{a['action']}: {a['detail']}" for a in actions if a["applied"])
    return {
        "plan_id": actions[-1]["plan"] if actions else None,
        "decision": last_decision,
        "applied": any(a["applied"] for a in actions),
        "detail": detail,
        "incident_cleared": cleared_any,
        "retried": retried_any,
        "status": ("\n\n".join(status_by_plan.values())
                   or "планов с распознанным журналом не найдено"),
        "actions": actions,
        "actions_count": sum(1 for a in actions if a["applied"]),
        "reason": reason,
    }


def incident(cfg: config_mod.Config, args: argparse.Namespace, exc: BaseException) -> int:
    """Сбой движка вместо трейсбека: инцидентный стоп, уведомление, `--json` и код 1."""
    if isinstance(exc, GateError):
        reason = str(exc)
    else:
        reason = f"сбой движка: {type(exc).__name__}: {exc}"
    # Без времени и pid: по причине сравниваются повторы (тот же стоп — молчим).
    reason = reason[:300]
    plan_id = getattr(exc, "plan_id", None) or (args.plan or None)
    p: Paths | None = None
    try:
        plan_id = plan_id or registry.driven_default_id(cfg)
        p = resolve_paths(cfg, plan_id)
        plan_id = p.plan_id
    except Exception:  # noqa: BLE001 - путей нет: записывать некуда, причина остаётся в stderr
        p = None
    if args.run:
        # Стоп и уведомление — только для тика: чтение статуса состояние не меняет.
        current: dict = {}
        if p is not None:
            try:
                current = load_stop(p) or {}
            except Exception as stop_exc:  # noqa: BLE001 - сбой чтения не отменяет уведомление
                print(f"ошибка: {stop_exc}", file=sys.stderr)
        owner_stop = stop_kind(current) == "owner"
        # Стоп владельца инцидент не перезаписывает (его снимает только владелец),
        # поэтому о сбое сообщаем каждый тик: записать причину некуда.
        fresh = owner_stop or current.get("reason") != reason
        if fresh and p is not None and not owner_stop:
            try:
                write_stop(p, reason, "gate", kind="incident")
            except Exception as stop_exc:  # noqa: BLE001 - уведомление от записи не зависит
                print(f"ошибка: {stop_exc}", file=sys.stderr)
        if fresh:
            try:
                notify(cfg, attention_text(
                    plan_id or "", "сбой тика конвейера",
                    evidence=[reason],
                    todo=["посмотреть вывод тика и состояние плана (команды ниже)",
                          "устранить причину сбоя",
                          "проверить тик вручную и дождаться следующего запуска"],
                    commands=[f"python .pipeline/gate.py --status --plan {plan_id}",
                              f"python .pipeline/gate.py --run --plan {plan_id}"]))
            except Exception as notify_exc:  # noqa: BLE001 - обработчик не даёт второй трейсбек
                print(f"ошибка: уведомление не отправлено: {notify_exc}", file=sys.stderr)
    print(f"ошибка: {reason}", file=sys.stderr)
    if args.json:
        print(json.dumps({"ok": False, "error": reason, "applied": False, "incident": True,
                          "plan_id": plan_id,
                          "decision": {"action": "incident", "reason": reason}},
                         ensure_ascii=False, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    config_mod.setup_stdio()
    ap = argparse.ArgumentParser(description="Страж шага: шаг → ревью → следующий шаг")
    ap.add_argument("--plan", help="номер плана (по умолчанию из pipeline.json)")
    ap.add_argument("--plans", action="store_true",
                    help="таблица планов: следующий шаг, закрыто, стоп-флаг")
    ap.add_argument("--run", action="store_true",
                    help="выполнить тик: до max_actions действий (режим cron)")
    ap.add_argument("--pause", action="store_true",
                    help="показать паузу по расписанию: окна, действует ли сейчас")
    ap.add_argument("--max-actions", type=int, default=None,
                    help="сколько действий за тик (по умолчанию — из pipeline.json)")
    ap.add_argument("--status", action="store_true", help="только показать состояние и решение")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--plan-version-sha", metavar="ФАЙЛ",
                    help="напечатать sha256 версии плана (файл без содержимого журнала)")
    ap.add_argument("--stop", metavar="ПРИЧИНА", help="остановиться после текущего шага")
    ap.add_argument("--resume", action="store_true", help="снять стоп-флаг")
    ap.add_argument("--hold", metavar="ШАГ", help="шаг требует ручного допуска владельца")
    ap.add_argument("--unhold", metavar="ШАГ", help="снять ручной допуск")
    ap.add_argument("--answer", nargs=2, metavar=("ВОПРОС", "ВАРИАНТ"),
                    help="ответ на вопрос шага: --answer Q-1 A (план — через --plan)")
    ap.add_argument("--commands", action="store_true",
                    help="принять команды владельца из Telegram (getUpdates) и ответить в чат")
    ap.add_argument("--commands-wait", type=int, default=0,
                    help="секунды длинного опроса для --commands (0 — без ожидания)")
    ap.add_argument("--commands-reset", action="store_true",
                    help="снять маркер «приём команд выключен» (после 409 от вебхука)")
    args = ap.parse_args(argv)

    try:
        cfg = config_mod.load(repo_root())
    except config_mod.ConfigError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    # D2: флаг важнее файла; флаг задаёт предел на один запуск и в конфиг не пишется.
    limit = args.max_actions if args.max_actions is not None else max_actions_of(cfg)

    if args.pause:
        # Пауза по расписанию гасит тики, поэтому про неё спрашивают и панель, и человек:
        # ответ — та же форма, что у решения стража, без плана и без блокировки.
        state = config_mod.pause_state(cfg, _now())
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(state["reason"])
        return 0

    if args.plan_version_sha:
        # Метка `plan_sha256:` в отчёте ревью плана пишется этой командой: ревьюеру нужен
        # детерминированный способ посчитать версию плана, а не «sha256sum по файлу» (та
        # метка сбрасывалась бы от строки журнала при каждом закрытии шага).
        target = Path(args.plan_version_sha)
        if not target.is_absolute():
            target = Path(repo_root()) / target
        if not target.is_file():
            print(f"ошибка: нет файла плана: {args.plan_version_sha}", file=sys.stderr)
            return 2
        print(approval.plan_version_sha(target, approval.journal_marker_of(cfg)))
        return 0

    if args.run:
        # Тик меняет план и коммитит: на разъехавшейся копии движка он не запускается.
        ok, why = verify_engine_integrity(Path(__file__).resolve().parent)
        if not ok:
            print(f"тик остановлен: {why}", file=sys.stderr)
            print("обновите движок проекта: hermes pipeline setup --upgrade --project <путь>",
                  file=sys.stderr)
            # Без уведомления такой тик конвейер просто не ведёт, а владелец не узнаёт.
            try:
                attention(cfg, attention_text(
                    "", "тик не запускается: движок проекта не совпадает с маркером целостности",
                    evidence=[why,
                              "пока это не устранено, ни один план не двигается",
                              "причина обычно — правка файлов .pipeline/ руками или старой копией"],
                    todo=["обновить движок проекта штатной командой (ниже)",
                          "проверить, что после обновления тик выполняется вручную без ошибки",
                          "сообщение об этом придёт ещё раз, если тик снова не запустится"],
                    commands=["hermes pipeline setup --upgrade --project <путь>",
                              "python .pipeline/gate.py --run",
                              "python .pipeline/gate.py --status --plan <план>"]),
                    "engine-integrity")
            except Exception as exc:  # noqa: BLE001 - уведомление не отменяет код выхода
                print(f"уведомление не отправлено: {exc}", file=sys.stderr)
            return 3
        # Пауза по расписанию (окна системного времени): в окне тик не работает вовсе —
        # ни карточек, ни коммитов, ни сводки. Проверка стоит до блокировки и до разбора
        # плана: пауза — это «сейчас не тикаем», а не «тик не смог». Ветки `--run` и
        # `--plan N --run` (её зовут панель и бот) проходят обе через это место.
        # Команды владельца (`--stop`, `--resume`, `--hold`) паузы не слушают: снять стоп
        # или допустить шаг можно и ночью.
        pause = config_mod.pause_state(cfg, _now())
        if pause["paused"]:
            if args.json:
                # Форма ответа та же, что у обычного тика: у паузы `applied: false` и своё
                # действие в решении, иначе читатель JSON (панель, бот, агент) не отличит
                # «ничего не сделал» от «не смог».
                print(json.dumps({"ok": True, "paused": True, "plan_id": args.plan,
                                  "applied": False, "detail": pause["reason"],
                                  "status": pause["reason"], "pause": pause,
                                  "decision": {"action": "paused",
                                               "reason": pause["reason"]}},
                                 ensure_ascii=False, indent=2))
            else:
                print(pause["reason"])
            # Код 0: cron не должен считать паузу сбоем джобы и слать ошибки каждое окно.
            return 0

    try:
        if args.plans:
            if args.json:
                print(json.dumps(plans_rows(cfg), ensure_ascii=False, indent=2))
            else:
                print(plans_table(cfg))
            return 0

        if args.commands_reset:
            cleared = clear_commands_off(cfg)
            print("приём команд снова включён" if cleared
                  else "приём команд не был выключен")
            return 0

        if args.commands:
            result = poll_commands(cfg, wait=args.commands_wait)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                tail = f" ({result['reason']})" if result.get("reason") else ""
                extra = f", чужих проигнорировано: {result['ignored']}" if result.get("ignored") else ""
                if result.get("limited"):
                    extra += f", отсечено по частоте: {result['limited']}"
                print(f"команд обработано: {result['handled']}{extra}{tail}")
            # Выключенный приём команд — не сбой джобы: иначе cron будет слать ошибки,
            # пока владелец не снимет вебхук или не включит команды обратно.
            return 0 if (result.get("ok") or result.get("disabled")
                         or not commands_enabled(cfg)) else 1

        try:
            # Адресный тик слушается ведения (план 14, S1): номер вне `driven_plans` получает
            # отказ до разрешения путей — без лока, состояния, карточек и стоп-флага. Порядок
            # проверок: сначала существование плана (иначе опечатка получала бы совет
            # «включить в driven_plans»), потом ведение. Команды владельца (`--stop`,
            # `--resume`, `--answer`, `--hold`/`--unhold`) не ограничиваются (Допущение 3):
            # смотреть состояние и снимать стоп конвейер не запрещает.
            if (args.run and args.plan is not None
                    and not (args.stop is not None or args.resume or args.answer
                             or args.hold or args.unhold)):
                want = registry.normalize(args.plan)
                registry.get(cfg, want)      # неизвестный номер — прежний «план не найден»
                if want not in registry.driven_ids(cfg):
                    text = not_driven_refusal(cfg, want)
                    if args.json:
                        print(json.dumps({"ok": False, "error": text, "driven": False,
                                          "plan_id": want}, ensure_ascii=False, indent=2))
                    else:
                        print(f"ошибка: {text}", file=sys.stderr)
                    return 2
            p = resolve_paths(cfg, args.plan)
        except registry.PlanNotFound as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            return 2

        owner_cmd = ("stop" if args.stop is not None
                     else "resume" if args.resume
                     else "answer" if args.answer
                     else "hold" if (args.hold or args.unhold) else "")
        if owner_cmd:
            # Команды владельца меняют состояние и стоп-флаг (read-modify-write), поэтому
            # берут ту же блокировку, что и тик: поверх живого тика команда не применяется.
            lock = state_io.acquire(Path(cfg.state_dir), what=owner_cmd)
            if lock is None:
                who = state_io.holder(Path(cfg.state_dir)) or {}
                # Код возврата не 0: «команда не применена» — это отказ, а не успех. С кодом 0
                # панель показывала владельцу «готово» на непринятый ответ (случай 17.11.2026),
                # а `plans_api` принимал жест за исполненный.
                print(f"тик уже идёт (pid {who.get('pid', '?')}, с {who.get('at', '?')}, "
                      f"{who.get('what', '?')}) — команда {owner_cmd} не применена",
                      file=sys.stderr)
                return EXIT_BUSY
            try:
                if args.stop is not None:
                    payload = write_stop(p, args.stop or "остановка владельца", "owner")
                    notify(cfg, f"⏸ остановка после текущего шага: {payload['reason']}")
                    print(f"стоп-флаг поставлен: {p.stop_file} ({payload['reason']})")
                    return 0
                if args.resume:
                    existed = clear_stop(p)
                    notify(cfg, "▶️ стоп снят, конвейер продолжит со следующего шага")
                    print("стоп-флаг снят" if existed else "стоп-флага не было")
                    return 0
                if args.answer:
                    # Ответ владельца на вопрос шага: тот же путь, что у `/answer` и панели.
                    qid, value = str(args.answer[0]), str(args.answer[1])
                    result = answer_question(cfg, args.plan, qid, value)
                    if not result["ok"]:
                        print(f"ошибка: {result['message']}", file=sys.stderr)
                        return 2
                    print(result["message"])
                    return 0
                result = hold_state_change(cfg, p, args.hold if args.hold is not None
                                           else args.unhold, add=bool(args.hold))
                if not result["ok"]:
                    print(f"ошибка: {result['error']}", file=sys.stderr)
                    return 2
                print(result["message"])
                return 0
            finally:
                state_io.release(lock)

        if args.run:
            # Один лок на проект, не на план: tick() и retry_pending обслуживают все планы.
            lock = state_io.acquire(Path(cfg.state_dir), what="run")
            if lock is None:
                who = state_io.holder(Path(cfg.state_dir)) or {}
                detail = "тик уже идёт"
                if args.json:
                    print(json.dumps({"locked": True, "applied": False, "detail": detail,
                                      "decision": {"action": "locked", "reason": detail}},
                                     ensure_ascii=False, indent=2))
                else:
                    print(f"{detail} (pid {who.get('pid', '?')}, с {who.get('at', '?')}) — "
                          f"второй тик ничего не делает")
                return 0
            try:
                if args.plan is None:
                    result = tick(cfg, max_actions=limit)
                    if args.json:
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 0
                    acted = bool(result.get("applied") or result.get("retried"))
                    if not acted and not args.status:
                        return 0
                    print(result["status"])
                    if result.get("applied"):
                        print(f"Выполнено [{result.get('plan_id')}]: {result['detail']}")
                    return 0
                retried = retry_pending(p)
                started = time.monotonic()
                result = run_gate(p, execute=True)
                duration = round(time.monotonic() - started, 3)
                if result.get("applied") and (result.get("decision") or {}).get("action") != "idle":
                    # Ветка одного плана — тоже тик: применённое действие попадает в состояние.
                    remember_action(p, p.plan_id, result.get("decision") or {},
                                    result.get("detail", ""), duration=duration,
                                    commit=str(result.get("commit") or ""))
                result["pending_commit"] = retried
            finally:
                state_io.release(lock)
        else:
            retried = None
            result = run_gate(p, execute=False)
            result["pending_commit"] = retried
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        acted = args.run and (result["applied"] or retried)
        if not acted and not args.status:
            return 0
        print(result["status"])
        if retried:
            print(f"Отложенный коммит: {retried['detail']}")
        if args.run:
            print(f"Выполнено: {result['applied']} — {result['detail']}")
        return 0
    except GateError as exc:
        return incident(cfg, args, exc)
    except Exception as exc:  # noqa: BLE001 - любой сбой тика = инцидент, а не трейсбек
        return incident(cfg, args, exc)


def repo_root() -> str:
    """Корень проекта: env PIPELINE_REPO (предпросмотр), иначе каталог над `.pipeline/`."""
    env = os.environ.get("PIPELINE_REPO")
    if env:
        return str(Path(env).resolve())
    return str(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    raise SystemExit(main())
