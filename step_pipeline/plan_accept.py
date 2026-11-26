# -*- coding: utf-8 -*-
"""Черновики планов и утверждение владельцем: `hermes pipeline plan ...`.

Зачем отдельный каталог: агент-планировщик пишет план в `<plans_dir>/drafts/`, а движок
читает только `<plans_dir>/*.md` без рекурсии (`engine/registry.py:165-183`). Поэтому
неутверждённый план стражу не виден, и «план написан» ещё не значит «конвейер пошёл».

Утверждение — решение человека, а не исполнителя, поэтому перенос файла и запись
`plan_<N>_approval.json` живут в одной команде: запись с sha256 читает страж (шаг S7), и
появляться она обязана ровно тогда, когда файл уже лежит в каталоге планов. Разделять
перенос и запись нельзя: «файл на месте, записи нет» — это молча сломанный план.

Проверка делегируется линтеру `plan_lint.py`, когда он есть (шаг 1-S8). Запасной путь
(линтера ещё нет) разбирает черновик движком через временный `pipeline.json`: второй
парсер шагов в плагине не заводится (решение D1), а свои проверки — минимальные и только
про журнал и контракт шага.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    from . import project as proj
except ImportError:  # pragma: no cover - отладка файлом
    import project as proj  # type: ignore

try:
    from .engine import plan_lint
except ImportError:  # шаг 1-S8 ещё не сделан — работают запасные проверки
    plan_lint = None  # type: ignore

try:
    # Модуль движка: версия плана (файл без журнала) считается там же, где её сверяет
    # страж. Второй реализации нормализации не заводится.
    from .engine import approval as engine_approval
except ImportError:  # pragma: no cover - отладка файлом, движка рядом нет
    engine_approval = None  # type: ignore

_PLANS_DEFAULT = "docs/plans"
_APPROVAL_NAME = "plan_{}_approval.json"
# Номер плана в имени файла — как engine/registry.py:30.
_PLAN_NUM_RE = re.compile(r"^(\d{1,2})-")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*?)\s*$")
_STEP_ID_REGEX = r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)"
JOURNAL_COLUMNS = 5
# Пункт «Тесты/гейты: —» считается пустым (plan_lint.py:48).
_EMPTY_GATES = ("", "—", "-", "−")


def plans_dir(project) -> Path:
    """Каталог планов из `paths.plans` (умолчание — `docs/plans`).

    Своя копия `plan_lint._plans_dir`, а не импорт: запасной путь обязан работать и
    тогда, когда линтера ещё нет.
    """
    cfg = proj.read_config(Path(project))
    rel = (cfg.get("paths") or {}).get("plans") or _PLANS_DEFAULT
    return Path(project) / str(rel)


def drafts_dir(project) -> Path:
    """Подкаталог черновиков: движок читает только верхний уровень каталога планов."""
    return plans_dir(project) / "drafts"


def approval_file(project, plan_id) -> Path:
    """Запись утверждения в каталоге состояния проекта (её читает страж, шаг S7)."""
    return proj.state_dir(Path(project)) / _APPROVAL_NAME.format(_normalize(plan_id))


def plan_number(name: str) -> str | None:
    """Номер плана из имени файла (`01-foo.md` → `1`); `None`, если префикса нет."""
    match = _PLAN_NUM_RE.match(str(name))
    return str(int(match.group(1))) if match else None


def list_drafts(project) -> list[dict]:
    """Черновики каталога `drafts/`: номер, имя файла, путь и заголовок плана.

    Список нужен панели: владелец утверждает черновик кнопкой, не набирая команду.
    Только чтение — движок эти файлы не видит (`plans_dir.glob("*.md")` без рекурсии).
    """
    try:
        files = sorted(drafts_dir(project).glob("*.md"))
    except OSError:
        return []
    rows: list[dict] = []
    for path in files:
        number = plan_number(path.name)
        if number is None:
            continue
        row = {"id": number, "name": path.name,
               "file": _shown(path, project), "title": _title(path)}
        row.update(_draft_state(project, number, path))
        rows.append(row)
    return sorted(rows, key=lambda row: int(row["id"]))


def _draft_state(project, number: str, draft: Path) -> dict:
    """Утверждён ли уже план этого черновика — то, что панель обязана показать.

    Черновик остаётся в `drafts/` после утверждения (`_accept_existing` его не переносит:
    действующим считается файл в каталоге планов). Без этих полей панель вечно рисовала
    «черновик» с кнопкой «Утвердить», а нажатие отвечало «уже утверждён — ничего не
    изменено»: состояние на экране не менялось, хотя утверждение есть.
    """
    out = {"approved": False, "approved_at": "", "changed": False, "draft_differs": False}
    record = _read_approval(project, number)
    if record is None:
        return out
    out["approved_at"] = str(record.get("approved_at") or "")
    final = plans_dir(project) / Path(draft).name
    try:
        if not final.is_file():
            return out
        final_sha = _sha256(final)
        draft_sha = _sha256(Path(draft))
    except OSError:
        return out
    approved_sha = str(record.get("sha256") or "")
    saved_version = str(record.get("version_sha256") or "")
    # Сверка — как у стража (`engine/approval.py:evaluate`): запись с `version_sha256`
    # сверяется по версии плана. На сырых байтах закрытие шага (строка журнала) выглядело
    # правкой плана: строка черновика возвращалась в «изменён после утверждения», хотя
    # план в том же снимке помечен «утверждён». Легаси-запись без поля — по сырым байтам.
    version = _version_sha256(project, final) if saved_version else ""
    same = (saved_version == version) if version else (approved_sha == final_sha)
    has_record = bool(approved_sha or saved_version)
    out["approved"] = has_record and same
    out["changed"] = has_record and not same
    out["draft_differs"] = draft_sha != final_sha
    return out


def _title(path: Path) -> str:
    """Заголовок плана из первой строки `# …`; заголовка нет — имя файла без `.md`."""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            match = _HEADING_RE.match(line)
            if match and len(match.group("hashes")) == 1:
                return match.group("title").strip()
    except OSError:
        pass
    return Path(path).stem


def now_iso() -> str:
    """Дата утверждения — отдельной функцией, чтобы тест мог её подменить.

    Иначе проверка «повторный accept не перезаписал дату первого утверждения» зависела бы
    от попадания двух вызовов в одну секунду.
    """
    return datetime.now().isoformat(timespec="seconds")


def find_draft(project, value) -> Path:
    """Черновик по номеру или по пути.

    Номер ищется только среди файлов каталога черновиков; путь принимается, если файл
    существует и оканчивается `.md`. Ошибка — `ValueError` с текстом для человека.
    """
    project = Path(project)
    text = str(value).strip()
    if text.isdecimal():
        want = _normalize(text)
        folder = drafts_dir(project)
        found = sorted(p for p in folder.glob("*.md") if plan_number(p.name) == want)
        if not found:
            raise ValueError(f"черновика с номером {want} нет в каталоге черновиков")
        if len(found) > 1:
            names = ", ".join(p.name for p in found)
            raise ValueError(f"черновиков с номером {want} несколько: {names}")
        return found[0]
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project / path
    if path.is_file() and path.suffix.lower() == ".md":
        return path
    raise ValueError(f"черновик не найден: {text}")


def find_plan(project, plan_id) -> Path | None:
    """Утверждённый файл с этим номером в каталоге планов, или `None`."""
    want = _normalize(plan_id)
    for path in sorted(plans_dir(project).glob("*.md")):
        if plan_number(path.name) == want:
            return path
    return None


def check_draft(project, draft) -> list[str]:
    """Ошибки черновика; пустой список — чисто.

    Линтер, если он есть, — единственный источник форматных проверок: свои мини-проверки
    нужны только до шага 1-S8, иначе в проекте завелись бы два расходящихся линтера.

    Исключение одно — бюджет прогонов (план 17, S5): линтер хвосты `таймаут N` намеренно
    игнорирует («битый таймаут движок просто игнорирует»), а конфиг, который сам себе
    противоречит, до шага доходить не должен. Поэтому проход `_budget_errors` идёт после
    линтера (а без него — после `_fallback_errors`) и не заменяет его находок.
    """
    project = Path(project)
    draft = Path(draft)
    if plan_lint is not None:
        problems = [str(problem) for problem in plan_lint.lint_project(project, str(draft)).problems]
    else:
        problems = _fallback_errors(project, draft)
    return problems + _budget_errors(project, draft)


#: Хвост прогона: объявленный таймаут строки «Прогон» (§5 формата плана).
_RUN_TIMEOUT_RE = re.compile(r"таймаут\s+(?P<seconds>\d+)")
_RUN_LINE_RE = re.compile(r"^[-*+]\s*Прогон", re.IGNORECASE)


def _budget_errors(project: Path, draft: Path) -> list[str]:
    """Черновик против `step_runs.budget`: одиночный таймаут и сумма по шагу (17-S5).

    Замер 23.11.2026 (план 14, S3): бюджет 600 с, а постановка объявила прогон с
    таймаутом 1800 с — прогон ушёл в `timeout`, второй пункт в `skipped`, страж встал
    инцидентом, и шаг нельзя было закрыть. Сумма проверяется наравне с одиночным
    таймаутом: два честных прогона по 900 с дают тот же исход, что один на 1800 с.

    Бюджет читается сырым словарём (`proj.read_config`): `plan --check` обязан печатать
    ошибки черновика, а не падать на конфиге. Ключа нет или он `null` — ошибок нет: авто
    по `gate.runs_budget_limit` противоречивым быть не может, оно считается по этим же
    таймаутам.
    """
    cfg = proj.read_config(project)
    budget = (cfg.get("step_runs") or {}).get("budget")
    if budget is None:
        return []
    try:
        limit = int(budget)
    except (TypeError, ValueError):
        return []
    try:
        lines = Path(draft).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    raw_regex = str(cfg.get("step_id_regex") or _STEP_ID_REGEX)
    pattern = raw_regex if raw_regex.startswith("^") else "^" + raw_regex
    try:
        step_re = re.compile(pattern)
    except re.error:
        step_re = re.compile(_STEP_ID_REGEX)
    errors: list[str] = []
    step_id = ""
    sums: dict[str, int] = {}
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            found = step_re.search(heading.group("title"))
            if found:
                step_id = found.group("id").upper()
                sums.setdefault(step_id, 0)
            continue
        if not _RUN_LINE_RE.match(line):
            continue
        declared = _RUN_TIMEOUT_RE.search(line)
        if declared is None:
            continue
        seconds = int(declared.group("seconds"))
        name = step_id or "—"
        sums[name] = sums.get(name, 0) + seconds
        if seconds > limit:
            errors.append(
                f"шаг {name}: таймаут прогона {seconds} с больше step_runs.budget={limit} с — "
                f"прогон уйдёт в `skipped`, страж встанет на инциденте; поднять бюджет "
                f"или снизить таймаут")
    for name, total in sums.items():
        if total > limit:
            errors.append(
                f"шаг {name}: сумма объявленных таймаутов прогонов {total} с больше "
                f"step_runs.budget={limit} с — последние прогоны будут пропущены")
    return errors



def check_warnings(project, draft) -> list[str]:
    """Предупреждения черновика: о них сообщают, но утверждение они не держат.

    Адрес `файл.py:строки`, уехавший после правки кода (Л21), — повод сказать человеку,
    а не блокировать план: владельцу нужен один способ снять стоп.
    """
    if plan_lint is None:
        return []
    lint = plan_lint.lint_project(Path(project), str(Path(draft)))
    return [str(warning) for warning in lint.warnings]


def approve(project, value) -> dict:
    """Утвердить план и вернуть исход без печати: общий путь CLI и панели.

    Ключи ответа — контракт панели (`POST /api/plan/accept`): `ok`, `code` (0/1/2, как
    у CLI), `plan`, `status` (`approved` / `unchanged` / `reapproved` / `errors` /
    `missing` / `bad-number` / `conflict`), `errors`, `message`, `file`,
    `approval_file`, `sha256`, `approved_at`. `message` — те же строки, что печатает
    `accept`: сообщения CLI и панели не расходятся.

    Порядок важен: сначала проверки, потом перенос, потом запись; при любой ошибке файл
    и запись не трогаются, чтобы не оставить полуутверждённый план.
    """
    project = Path(project)
    text = str(value).strip()
    if not text.isdecimal():
        return {"ok": False, "code": 2, "plan": "", "status": "bad-number",
                "errors": [f"--accept принимает номер плана, получено: {value}"],
                "message": ""}
    number = _normalize(text)
    final = find_plan(project, number)
    missing = ""
    try:
        draft: Path | None = find_draft(project, number)
    except ValueError as exc:
        draft = None
        missing = str(exc)
    if draft is None and final is None:
        return {"ok": False, "code": 1, "plan": number, "status": "missing",
                "errors": [], "message": f"ошибка: {missing}"}

    errors = check_draft(project, draft or final)
    if errors:
        return {"ok": False, "code": 1, "plan": number, "status": "errors",
                "errors": list(errors),
                "message": f"ИТОГ: ошибок {len(errors)} — файл не перенесён, "
                           f"черновик остался на месте"}

    if draft is not None and final is not None:
        result = _accept_existing(project, number, draft, final)
    elif draft is not None:
        result = _move_draft(project, number, draft)
    elif final is not None:
        result = _accept_final(project, number, final)
    else:
        # Ветка недостижима: выше уже отсеяно «ни черновика, ни плана». Оставлена, чтобы
        # типы не спорили с фактом, а отказ был внятным, если порядок проверок поменяют.
        return {"ok": False, "code": 1, "plan": number, "status": "missing", "errors": [],
                "message": f"ошибка: план {number} не найден"}
    if result.get("ok"):
        # Решение владельца 18.11.2026: утверждение плана само ставит его в ведение.
        _apply_drive(project, number, result)
    return result


def accept(project, value) -> int:
    """Утвердить план по номеру (CLI): печатает исход `approve` и возвращает код 0/1/2."""
    result = approve(project, value)
    status = str(result.get("status") or "")
    if status == "errors":
        for problem in result.get("errors") or []:
            print(f"  ✗ {problem}")
    message = str(result.get("message") or "")
    if message:
        stream = sys.stderr if status in ("missing", "bad-number") else sys.stdout
        print(message, file=stream)
    code = result.get("code")
    return int(code if code is not None else 1)


# ───────────────────────────── ветки утверждения ─────────────────────────────

def _approved(number: str, path: Path, project: Path, record: dict | None = None) -> dict:
    """Ответ успешного утверждения с теми строками, что печатает CLI."""
    record = record or {}
    shown = _shown(path, project)
    return {"ok": True, "code": 0, "plan": number, "status": "approved",
            "file": shown, "approval_file": str(approval_file(project, number)),
            "sha256": record.get("sha256") or "",
            "approved_at": record.get("approved_at") or "", "errors": [],
            "message": f"План {number} утверждён: {shown}\n"
                       f"Запись утверждения: {approval_file(project, number)}"}


def _unchanged(number: str, record: dict, path: Path, project: Path) -> dict:
    """Утверждение уже есть и совпадает: ничего не перезаписываем (дата первого важна).

    Исключение — запись, которую написал страж наследованием (`gate.inherit_approval`,
    поле `inherited`): она подтверждает содержимое файла молча, без решения владельца, и
    сохраняет прежнюю метку `review_reset_at`. Явный `--accept` по такой записи раньше
    отвечал «уже утверждён», бюджет кругов ревью не начинался заново, и при исчерпанном
    лимите владелец не мог разблокировать план ничем: переутверждение было пустышкой.
    Поэтому явное решение владельца перебивает наследование и пишет запись заново.
    """
    if record.get("inherited"):
        payload = _write_approval(project, number, path)
        return {"ok": True, "code": 0, "plan": number, "status": "reapproved",
                "file": _shown(path, project),
                "approval_file": str(approval_file(project, number)),
                "sha256": payload.get("sha256") or "",
                "approved_at": payload.get("approved_at") or "", "errors": [],
                "message": f"План {number} переутверждён владельцем: бюджет кругов "
                           f"ревью начат заново"}
    return {"ok": True, "code": 0, "plan": number, "status": "unchanged",
            "file": _shown(path, project),
            "approval_file": str(approval_file(project, number)),
            "sha256": record.get("sha256") or "",
            "approved_at": record.get("approved_at") or "", "errors": [],
            "message": f"План {number}: уже утверждён "
                       f"({record.get('approved_at', '')}) — ничего не изменено"}


# Решение владельца 18.11.2026: утверждение плана само ставит его в ведение. Ключ
# `driven_plans` остаётся ключом владельца — его пишет жест владельца (панель или `--accept`),
# а не страж; правило «страж не трогает driven_plans» не меняется.
_DRIVE_NOTES = {
    "added": "План {number} добавлен в driven_plans — страж возьмёт его сам.",
    "no-config": "Ведение не записано: pipeline.json не читается — добавь план в "
                 "driven_plans вручную.",
}


def _driven_list(raw) -> list[str] | None:
    """`driven_plans` как список строк; `None` — значение не итерируется вовсе."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return None


def drive_plan(project, number: str) -> str:
    """Добавить номер плана в `driven_plans`: `added` / `already` / `all` / `no-config`.

    `all` — список содержит `*` или ключа нет вовсе: и то и другое движок читает как «все
    планы ведомы» (`engine/config.py`, `DEFAULTS`), править нечего. `no-config` — файла нет
    или он битый: утверждение не падает из-за ведения, владельцу сказано, что делать.
    """
    project = Path(project)
    try:
        cfg = proj.read_config(project)
    except Exception:  # noqa: BLE001 — диагноз вместо падения утверждения
        return "no-config"
    if not cfg:
        return "no-config"
    if "driven_plans" not in cfg:
        return "all"
    driven = _driven_list(cfg.get("driven_plans"))
    if driven is None:
        return "no-config"
    if "*" in driven:
        return "all"
    if number in driven:
        return "already"
    driven.append(number)
    driven.sort(key=lambda value: (0, int(value)) if value.isdecimal() else (1, 0))
    cfg["driven_plans"] = driven
    _atomic_write_text(project / proj.MARKER,
                       json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return "added"


def _apply_drive(project: Path, number: str, result: dict) -> None:
    """Дописать в ответ утверждения исход ведения и строку для человека."""
    outcome = drive_plan(project, number)
    result["driven"] = outcome
    note = _DRIVE_NOTES.get(outcome)
    if note:
        head = str(result.get("message") or "").rstrip()
        result["message"] = f"{head}\n{note.format(number=number)}".strip()


def _refused(number: str, message: str) -> dict:
    """Отказ без переноса и без записи: файл остаётся где был."""
    return {"ok": False, "code": 1, "plan": number, "status": "conflict",
            "errors": [], "message": message}


def _accept_existing(project: Path, number: str, draft: Path, final: Path) -> dict:
    """Черновик и утверждённый файл уже есть: утверждаем действующий план.

    Действующим считается файл в `docs/plans/`: он и есть план, который ведёт страж,
    а черновик рядом — остаток прошлого цикла (он не переносится и не перезаписывает
    план). Раньше расхождение содержимого давало отказ «файл уже есть и отличается
    содержимым», и переутвердить правленый план командой `--accept N` было нельзя —
    именно эту команду просит страж после правки плана по разведке (0.20.0).
    """
    if final.name != draft.name:
        return _refused(number, f"ошибка: с номером {number} уже есть другой файл: {final.name}")
    return _accept_final(project, number, final)


def _move_draft(project: Path, number: str, draft: Path) -> dict:
    """Перенести черновик в каталог планов и записать утверждение."""
    target = plans_dir(project) / draft.name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(draft, target)
    payload = _write_approval(project, number, target)
    return _approved(number, target, project, payload)


def _accept_final(project: Path, number: str, final: Path) -> dict:
    """Черновика нет, утверждённый файл есть: сверить хэш с записью."""
    record = _read_approval(project, number)
    if record is None:
        payload = _write_approval(project, number, final)
        return _approved(number, final, project, payload)
    if record.get("sha256") == _sha256(final):
        return _unchanged(number, record, final, project)
    payload = _write_approval(project, number, final)
    return {"ok": True, "code": 0, "plan": number, "status": "reapproved",
            "file": _shown(final, project),
            "approval_file": str(approval_file(project, number)),
            "sha256": payload.get("sha256") or "",
            "approved_at": payload.get("approved_at") or "", "errors": [],
            "message": f"План {number} переутверждён (содержимое изменилось)"}


# ───────────────────────────── запись утверждения ─────────────────────────────

def _write_approval(project: Path, number: str, path: Path) -> dict:
    """Записать `plan_<N>_approval.json`: ключи фиксированы контрактом шага S7."""
    payload = {
        "plan": str(number),
        "file": _shown(path, project),
        "sha256": _sha256(path),
        # Версия плана без содержимого журнала (0.22.1): по ней страж сверяет утверждение,
        # иначе строка журнала при закрытии шага выглядела правкой плана. `sha256` остаётся
        # сырым хэшем файла — по нему работают старые записи и внешние проверки.
        "version_sha256": _version_sha256(project, path),
        "approved_at": now_iso(),
        "profile": os.environ.get("HERMES_PROFILE") or "default",
        # Момент явного решения владельца: по нему страж считает бюджет кругов ревью
        # заново. Без этого «правим план ещё раз» упиралось в исчерпанные два круга и
        # тик снова ставил owner-stop — правка плана не открывала шаги.
        "review_reset_at": int(time.time()),
    }
    _atomic_write_text(approval_file(project, number),
                       json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _read_approval(project: Path, number: str) -> dict | None:
    """Прочитать запись утверждения; нет файла или он битый — `None`."""
    path = approval_file(project, number)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_text(path: Path, text: str) -> None:
    """Атомарная запись: временный файл рядом, fsync, `os.replace`.

    Копия структуры `plan_mode._atomic_write_text` сознательная: плагин не импортирует
    движок как модуль (`project.py:1-6`), а общий помощник плагина ради одной записи
    заводить незачем.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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


# ───────────────────────────── запасные проверки ─────────────────────────────

def _fallback_errors(project: Path, draft: Path) -> list[str]:
    """Проверки без линтера: журнал, разбор движком, контракт каждого шага."""
    try:
        lines = draft.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"файл не читается: {exc}"]

    journal_errors, fatal = _journal_errors(project, lines)
    if fatal:
        return journal_errors
    return journal_errors + _engine_errors(project, draft)


def _journal_errors(project: Path, lines: list[str]) -> tuple[list[str], bool]:
    """Мини-разбор журнала: пять колонок, шаг во второй, без повторов.

    Второй аргумент — «дальше не идти»: битая регулярка шага делает бессмысленным и
    журнал, и вызов движка (движок упадёт на том же конфиге).
    """
    cfg = proj.read_config(project)
    marker = str((cfg.get("markers") or {}).get("journal") or "журнал").lower()
    raw = str(cfg.get("step_id_regex") or _STEP_ID_REGEX)
    pattern = raw if raw.startswith("^") else "^" + raw
    try:
        step_re = re.compile(pattern)
    except re.error:
        return [f"step_id_regex не компилируется: {pattern}"], True
    if "id" not in step_re.groupindex:
        return [f"step_id_regex не компилируется: {pattern}"], True

    found = _headings(lines)
    rows = _journal_rows(lines, found, marker)
    if rows is None:
        return [f"нет раздела-журнала со словом «{marker}»"], False

    errors: list[str] = []
    steps: set[str] = set()
    for index in rows:
        cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
        if cells and set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0].lower() in ("дата", "date"):
            continue
        if len(cells) != JOURNAL_COLUMNS:
            errors.append(f"строка журнала без {JOURNAL_COLUMNS} колонок (нашлось {len(cells)})")
        if len(cells) < 2:
            errors.append("строка журнала без шага во второй колонке")
            continue
        match = step_re.match(cells[1])
        if match is None:
            errors.append("строка журнала без шага во второй колонке")
            continue
        step_id = match.group("id").upper()
        if len(cells) < JOURNAL_COLUMNS:
            continue
        if step_id in steps:
            errors.append(f"шаг {step_id} повторяется в журнале")
            continue
        steps.add(step_id)
    if not steps:
        errors.append("в журнале нет ни одной строки шага")
    return errors, False


def _engine_errors(project: Path, draft: Path) -> list[str]:
    """Разбор черновика движком и контракт каждого шага (решение D1).

    Движок видит только каталог планов, поэтому черновик показывается ему через временный
    `pipeline.json` с абсолютным `paths.plans`: `PIPELINE_CONFIG` поддержан с 0.1.0, и
    вторая копия движка не нужна.
    """
    number = plan_number(draft.name)
    if number is None:
        return [f"черновик без номера в имени: {draft.name}"]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = proj.read_config(project)
        paths = dict(cfg.get("paths") or {})
        paths["plans"] = str(draft.resolve().parent)
        cfg["paths"] = paths
        cfg_path = Path(tmp) / "pipeline.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        env = {"PIPELINE_CONFIG": str(cfg_path)}
        try:
            package = proj.engine_json(project, "next_step.py", "--plan", number, extra_env=env)
        except RuntimeError as exc:
            return [f"движок не разобрал черновик: {exc}"]
        if not isinstance(package, dict):
            return ["движок не разобрал черновик: пустой ответ"]
        if not package.get("steps_total"):
            return ["раздел шагов не распознан (заголовки «### S1. Название»)"] + _contract_errors(
                project, number, env, [])
        return _contract_errors(project, number, env, package.get("steps") or [])


def _contract_errors(project: Path, number: str, env: dict[str, str],
                     steps: list[dict]) -> list[str]:
    """Контракт каждого шага: файлы, гейты, критерий приёмки."""
    errors: list[str] = []
    seen: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").upper()
        if not step_id:
            continue
        if step_id in seen:
            errors.append(f"шаг {step_id} повторяется в разделе шагов")
            continue
        seen.add(step_id)
        try:
            package = proj.engine_json(project, "next_step.py", "--plan", number,
                                       "--step", step_id, extra_env=env)
        except RuntimeError as exc:
            errors.append(f"движок не разобрал шаг {step_id}: {exc}")
            continue
        contract = (package or {}).get("next") if isinstance(package, dict) else None
        if not isinstance(contract, dict):
            errors.append(f"шаг {step_id}: движок не отдал контракт")
            continue
        if not contract.get("files"):
            errors.append(f"шаг {step_id}: нет пункта «Файл»")
        gates = contract.get("gates") or []
        if not gates or all(str(gate).strip() in _EMPTY_GATES for gate in gates):
            errors.append(f"шаг {step_id}: нет пункта «Тесты/гейты»")
        if not str(contract.get("dod") or "").strip():
            errors.append(f"шаг {step_id}: нет критерия «Готово =»")
    return errors


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(индекс строки, уровень, заголовок) — как registry.py:82-89."""
    found: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            found.append((index, len(match.group("hashes")), match.group("title")))
    return found


def _section_end(found: list[tuple[int, int, str]], pos: int, limit: int) -> int:
    """Первый заголовок того же или более высокого уровня — граница раздела."""
    level = found[pos][1]
    for index in range(pos + 1, len(found)):
        if found[index][1] <= level:
            return found[index][0]
    return limit


def _journal_rows(lines: list[str], found: list[tuple[int, int, str]],
                  marker: str) -> list[int] | None:
    """Индексы строк таблицы раздела-журнала; `None` — раздела с маркером нет."""
    chosen: list[int] | None = None
    for pos, (index, level, title) in enumerate(found):
        if level < 2 or marker not in title.lower():
            continue
        end = _section_end(found, pos, len(lines))
        rows = [i for i in range(index + 1, end) if lines[i].strip().startswith("|")]
        if rows:
            return rows
        if chosen is None:
            chosen = rows
    return chosen


# ───────────────────────────── мелочи ─────────────────────────────

def _normalize(plan_id) -> str:
    """Номер в каноническом виде: `01` → `1` (как registry.normalize)."""
    text = str(plan_id).strip()
    if not text.isdecimal():
        raise ValueError(f"номер плана должен быть числом, получено: {plan_id!r}")
    return str(int(text))


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _version_sha256(project: Path, path: Path) -> str:
    """sha256 версии плана (файл без содержимого журнала), как её считает страж.

    Считает модуль движка (`engine/approval.py`): вторая реализация нормализации
    разошлась бы с ним при первой же правке. Модуль не импортируется (отладка файлом,
    движка рядом нет) — остаётся пустая строка, и запись работает по сырому `sha256`.
    """
    if engine_approval is None:  # pragma: no cover - отладочный запуск файлом
        return ""
    marker = ((proj.read_config(Path(project)).get("markers") or {}).get("journal")
              or "журнал")
    return engine_approval.plan_version_sha(path, str(marker))


def _shown(path: Path, project: Path) -> str:
    """Путь для показа: относительно проекта в posix-виде, как plan_lint._shown."""
    try:
        return Path(path).resolve().relative_to(Path(project).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()
