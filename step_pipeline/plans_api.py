# -*- coding: utf-8 -*-
"""Жесты владельца над планом из панели — одна точка правил (план 6 S3, S3A).

Три жеста: `POST /api/plan/start` (добавить номер в `driven_plans` и снять стоп
адресованной командой движка), `POST /api/plan/stop` (`--plan N --stop "причина"`) и
`POST /api/plan/delete` (убрать файл плана и его следы, отказав, пока у плана есть
карточки в работе). Правила записи ключа владельца `driven_plans` и вызова движка живут
здесь, а `ui_server` держит только маршруты: иначе каждый следующий жест копировал бы ту
же правку списка.

Движок как модуль не импортируется — только `proj.run_engine`, как в `ui_server._run_engine`:
плагин и движок живут раздельно (движок копируется в проект и обязан работать без плагина).

Жесты запуска и остановки адресные (`--plan N`): без адреса движок берёт план по умолчанию
(`registry.default_plan_id`), а при пустом `default_plan` — наименьший номер, и нажатие на
плане 2 сняло бы стоп плана 1.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import plan_accept
    from . import project as proj
    from . import setup_flow
    from . import snapshot as snap
except ImportError:  # pragma: no cover
    import plan_accept  # type: ignore[no-redef]
    import project as proj  # type: ignore[no-redef]
    import setup_flow  # type: ignore[no-redef]
    import snapshot as snap  # type: ignore[no-redef]

# Порядок источников каталога состояния — как у движка (`engine/config.py:337`):
# `PIPELINE_STATE_DIR` → `paths.state` из `pipeline.json` → `.pipeline/state`.
DEFAULT_STATE_DIR = ".pipeline/state"
ENGINE_TIMEOUT = 120       # `--resume`/`--stop` не ждут блокировку тика, 120 с с запасом
NO_PLAN = "нужно поле plan: номер плана"


def _plan_id(value: Any) -> str:
    """Номер плана в каноническом виде: `02` → `2`, ` 31 ` → `31` (как `registry.normalize`)."""
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        raise ValueError(NO_PLAN)
    return str(int(text))


def _state_dir(project: Path) -> Path:
    """Каталог состояния проекта в порядке движка.

    Переменная окружения стоит выше файла не для красоты: при заданном `PIPELINE_STATE_DIR`
    движок пишет стоп-флаг туда, и панель, читающая только `paths.state`, ответила бы
    «стоп снят», не тронув флаг.
    """
    root = Path(project)
    raw = os.environ.get("PIPELINE_STATE_DIR") or (proj.read_config(root).get("paths") or {}).get("state")
    path = Path(str(raw or DEFAULT_STATE_DIR))
    return path if path.is_absolute() else root / path


def stop_flag(project: Path, value: Any) -> Path:
    """Путь стоп-флага плана: `plan_N_stop.json` в каталоге состояния (номер канонический)."""
    return _state_dir(project) / f"plan_{_plan_id(value)}_stop.json"


def read_stop(project: Path, value: Any) -> dict | None:
    """Payload стоп-флага; файла нет или он битый/не словарь → None."""
    path = stop_flag(project, value)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _flag_stamp(project: Path, value: Any) -> tuple[bool, int, int]:
    """Отметка стоп-флага для сверки «до/после»: `(файл есть, метка записи, индекс)`.

    Признак «команда применилась» строится по событию записи файла, а не по равенству
    содержимого: `at` у движка секундный (`engine/gate.py:336`), а причина у жеста без
    поля `reason` всегда одна — повторный `stop` в ту же секунду перезаписал бы флаг
    теми же байтами, и верный стоп выглядел бы неприменённым.

    В отметке две пары глаз: `st_mtime_ns` и `st_ino`. На Windows метка времени
    обновляется с шагом системных часов, а `os.replace` (`engine/state_io.py:44-66`)
    отдаёт цели индекс нового временного файла — второй признак различает две записи,
    попавшие в один шаг часов.
    """
    try:
        st = stop_flag(project, value).stat()
    except OSError:
        return (False, 0, 0)
    return (True, st.st_mtime_ns, st.st_ino)


def _written(before: tuple[bool, int, int], after: tuple[bool, int, int]) -> bool:
    """Флаг записан командой: его не было — появился, был — отметка сменилась."""
    return after[0] and (not before[0] or after != before)


def _removed(before: tuple[bool, int, int], after: tuple[bool, int, int]) -> bool:
    """Флаг снят командой: до команды файл был, после — нет."""
    return before[0] and not after[0]


def _driven(project: Path) -> list[str]:
    """`driven_plans` как список строк — в том виде, в каком его прочитает движок.

    Отсутствующий ключ — `["*"]`: движок сливает файл с `DEFAULTS` (`config.py:60`) и ведёт
    все планы. Явный `null` — пустой список: `_deep_merge` оставляет значение файла, и
    `raw.get("driven_plans") or []` даёт ноль ведомых планов. Строка делится по запятым —
    так её присылает форма панели, и так же её разбирает валидатор конфига `ui_server`.
    Число и объект привести к ведомым планам нельзя: обе формы движок не итерирует.
    """
    cfg = proj.read_config(project)
    if "driven_plans" not in cfg:
        return ["*"]
    raw = cfg.get("driven_plans")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _write_config(project: Path, data: dict) -> None:
    """Записать `pipeline.json` атомарно: временный файл рядом, fsync, `os.replace`.

    `proj.write_config` пишет `write_text` без `os.replace`: оборванная запись оставила бы
    владельца без конфигурации. Порядок ключей сохраняется — `json` читает в dict, а dict
    помнит порядок вставки.
    """
    path = Path(project) / proj.MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _engine(project: Path, *args: str) -> dict:
    """Вызвать движок проекта и вернуть разобранный результат, без бросания исключений."""
    res = proj.run_engine(project, "gate.py", *args, timeout=ENGINE_TIMEOUT)
    return {"ok": res.returncode == 0, "code": res.returncode,
            "stdout": (res.stdout or "").strip(), "stderr": (res.stderr or "").strip()}


def _engine_error(result: dict) -> dict:
    """Отказ движка: `stdout`/`stderr` отдаются как есть, разбором прозы код не занимается."""
    error = result["stderr"] or result["stdout"] or f"gate.py вышел с {result['code']}"
    return {"ok": False, "status": "engine_error", "error": error,
            "stdout": result["stdout"], "stderr": result["stderr"]}


def _busy_cards(project: Path, plan: str, board: str | None = None) -> list[dict]:
    """Карточки плана в не-терминальном статусе — по ним удаление отказывает.

    Борда берётся в порядке движка: явная борда панели (`--board`), затем
    `PIPELINE_BOARD`, затем `board` из `pipeline.json` (`proj.board_db`; при явной борде
    он переменную окружения не читает). Читатель — тот же, что у панели (`snap.cards`),
    второй не заводится. Префикс с двоеточием (`planloop:2:`) — поэтому номер 2 не поймает
    карточки плана 20. `limit=500` вместо окна «последние 30»: старая карточка шага не
    должна выпасть из выборки.

    `archived` отбрасывается отдельно — тем же правилом, каким его отбрасывает движок до
    сравнения статусов (`engine/gate.py`): иначе архивная карточка навсегда запретила бы
    удаление плана. Список терминальных статусов берётся из плагина (`setup_flow.TERMINAL`,
    зеркало `engine/gate.py:TERMINAL`), своей копии здесь нет.
    """
    db = proj.board_db(Path(project), board or os.environ.get("PIPELINE_BOARD") or None)
    busy = []
    for card in snap.cards(db, limit=500, key_prefix=f"planloop:{plan}:"):
        status = str(card.get("status") or "")
        if status in setup_flow.TERMINAL or status == "archived":
            continue
        busy.append({"id": str(card.get("id") or ""), "status": status,
                     "key": str(card.get("idempotency_key") or "")})
    return busy


def _traces(project: Path, plan: str) -> list[Path | None]:
    """Пять файлов-следов плана в порядке удаления; первый может быть `None`.

    `None` — файла плана в каталоге нет: это отсутствующий след, а не ошибка
    (`None.is_file()` дал бы 500). Запись утверждения берётся у `plan_accept` (тот же путь,
    куда пишет `_write_approval`), остальные три — из каталога состояния плагина: порядок
    движка (`PIPELINE_STATE_DIR` выше `paths.state`) держит только `_state_dir`.
    """
    state = _state_dir(project)
    return [plan_accept.find_plan(project, plan),
            plan_accept.approval_file(project, plan),
            stop_flag(project, plan),
            state / f"plan_{plan}_state.json",
            state / f"plan_{plan}_rounds.json"]


def delete(project: Path, value: Any, board: str | None = None) -> dict:
    """Удалить план и все его следы; отказ, пока у плана есть карточки в работе.

    Порядок шагов обязателен, и охрана стоит раньше любой записи: иначе отказ пришёл бы
    после удаления половины следов. Ведение снимается **до** файлов: падение процесса
    между шагами оставляет план без ведения и файлы-остатки, которые доберёт повторный
    жест, — обратный порядок оставил бы ведомый план без файла, и страж поставил бы стоп
    вместо работы. Жест идемпотентен: отсутствующий след не ошибка, `missing` — только
    когда удалять нечего вовсе.

    `["*"]` и отсутствующий ключ перечислением не заменяются (правило шага S3): при них
    номер не числится, файлы удаляются, `changed: False` — замена `*` на список молча
    сняла бы ведение остальных планов.
    """
    try:
        plan = _plan_id(value)
    except ValueError:
        return {"ok": False, "status": "errors", "error": NO_PLAN}
    project = Path(project)
    cfg = proj.read_config(project)
    if not cfg:
        return {"ok": False, "status": "errors", "plan": plan,
                "error": f"pipeline.json не читается: {project / proj.MARKER}"}

    busy = _busy_cards(project, plan, board)
    if busy:
        first = busy[0]
        return {"ok": False, "status": "busy", "busy": True, "plan": plan, "cards": busy,
                "error": f"план {plan} не удалён: карточка {first['id']} в статусе "
                         f"{first['status']} — сначала закройте её"}

    traces = _traces(project, plan)
    driven = _driven(project)
    present = [path for path in traces if path is not None and path.is_file()]
    has_number = plan in {str(int(item)) for item in driven if item.isdigit()}
    if not present and not has_number:
        return {"ok": False, "status": "missing", "plan": plan,
                "error": f"плана {plan} нет: ни файла в каталоге планов, ни следов в состоянии"}

    changed = False
    if has_number:
        # Сравнение по каноническому номеру: `["02"]` движок читает как план 2.
        cfg["driven_plans"] = [item for item in driven
                               if not (item.isdigit() and str(int(item)) == plan)]
        _write_config(project, cfg)
        driven = [str(item) for item in cfg["driven_plans"]]
        changed = True

    removed = []
    for path in present:
        path.unlink()
        removed.append(plan_accept._shown(path, project))
    return {"ok": True, "status": "deleted", "plan": plan, "removed": removed,
            "driven": driven, "changed": changed,
            "message": f"план {plan} удалён: файлов и записей {len(removed)}"
                       + (", номер убран из driven_plans" if changed
                          else ", driven_plans не менялся")}


def start(project: Path, value: Any) -> dict:
    """Запустить план в работу: `--plan N --resume`, затем номер в `driven_plans`.

    Движок зовётся первым: неизвестный номер он отвергает кодом 2, и ключ владельца после
    отказа остаётся нетронутым — неудавшийся жест не портит конфигурацию. Обратный порядок
    записал бы «план ведётся» у несуществующего плана.

    Стоп-флаг сверяется **до и после** команды по отметке записи (`_flag_stamp`): при
    занятом тике движок печатает «тик уже идёт … команда resume не применена» и выходит
    с кодом 0, оставляя флаг на месте. Снимок «до» в одиночку доложил бы «стоп снят»
    и записал бы `driven_plans` поверх неудавшегося жеста.
    """
    try:
        plan = _plan_id(value)
    except ValueError:
        return {"ok": False, "status": "errors", "error": NO_PLAN}
    project = Path(project)
    cfg = proj.read_config(project)
    if not cfg:
        return {"ok": False, "status": "errors",
                "error": f"pipeline.json не читается: {project / proj.MARKER}"}

    before = _flag_stamp(project, plan)

    engine = _engine(project, "--plan", plan, "--resume")
    if not engine["ok"]:
        return _engine_error(engine)
    after = _flag_stamp(project, plan)
    if before[0] and not _removed(before, after):
        # Флаг был и остался на месте, хотя движок вышел с кодом 0 — это занятый тик.
        detail = engine["stdout"] or "движок не снял стоп-флаг"
        return {"ok": False, "status": "engine_error", "plan": plan, "error": detail,
                "stdout": engine["stdout"], "stderr": engine["stderr"],
                "message": f"план {plan} не запущен: {detail}"}
    cleared = _removed(before, after)

    driven = _driven(project)
    changed = False
    if "*" in driven:
        # `*` — правило «все распознанные планы»: замена перечислением молча сняла бы
        # ведение остальных, поэтому файл не переписывается.
        message = "все планы уже ведутся (driven_plans: *)"
    elif plan in {str(int(item)) for item in driven if item.isdigit()}:
        # Сравнение по каноническому номеру: `["02"]` движок читает как план 2, и повторный
        # жест не должен оставлять семантический дубликат `["02", "2"]`.
        message = f"план {plan} уже в driven_plans"
    else:
        driven = [*driven, plan]
        cfg["driven_plans"] = driven
        _write_config(project, cfg)
        changed = True
        message = f"план {plan} добавлен в driven_plans"

    if cleared:
        message += "; стоп снят"
    return {"ok": True, "status": "started", "plan": plan, "changed": changed,
            "cleared": cleared, "driven": driven, "stdout": engine["stdout"],
            "message": message}


def stop(project: Path, value: Any, reason: Any = "") -> dict:
    """Остановить план после текущего шага: `--plan N --stop "причина"`.

    Порядок аргументов именно такой: `--stop` берёт ровно одно значение, пара
    `--stop --plan 2` не разбирается — причиной стал бы `--plan`. `driven_plans` не
    трогается: остановленный план остаётся ведомым, как и после `--stop` в терминале.

    Флаг сверяется **до и после** команды по отметке записи, а не по содержимому: при
    занятом тике движок печатает «тик уже идёт … команда stop не применена» и выходит с
    кодом 0, оставляя прежний флаг, — проверка «флаг есть» вернула бы `ok: true` со
    **старой** причиной. Равенство payload признаком быть не может: `at` секундный, и
    повторный жест той же причиной переписывает флаг теми же байтами (`_flag_stamp`).
    """
    try:
        plan = _plan_id(value)
    except ValueError:
        return {"ok": False, "status": "errors", "error": NO_PLAN}
    project = Path(project)
    reason = str(reason or "").strip() or "остановка владельца"
    before = _flag_stamp(project, plan)
    engine = _engine(project, "--plan", plan, "--stop", reason)
    if not engine["ok"]:
        return _engine_error(engine)
    after = _flag_stamp(project, plan)
    payload = read_stop(project, plan)
    if payload is None or not _written(before, after):
        # Файла нет либо движок его не переписал (занятый тик) — жест не применился.
        detail = engine["stdout"] or "движок не поставил стоп-флаг"
        return {"ok": False, "status": "engine_error", "plan": plan, "error": detail,
                "stdout": engine["stdout"], "stderr": engine["stderr"],
                "message": f"план {plan} не остановлен: {detail}"}
    return {"ok": True, "status": "stopped", "plan": plan, "stop": payload,
            "stdout": engine["stdout"],
            "message": f"план {plan} остановлен после текущего шага: {payload.get('reason', reason)}"}
