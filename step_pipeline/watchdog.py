# -*- coding: utf-8 -*-
"""Сторож конвейеров: один проход, который не даёт им встать молча.

Живёт в плагине, а не в профиле: запускается кроном (`--no-agent`) и из CLI
(`hermes pipeline watchdog`), поэтому не зависит от сессии, профиля и рабочей машины.

Что делает по каждому проекту из реестра `projects.json` (идемпотентно, один тик = одно
действие):
1. снимает зависший замок тика, если процесс из `state/tick.lock` уже мёртв;
2. делает тик `hermes pipeline run --project <путь>`;
3. если тик отказал по целостности движка (`нет файла движка` / `движок изменён`),
   переустанавливает движок `hermes pipeline setup --upgrade` и повторяет тик один раз;
4. собирает план-таблицу `gate.py --plans` и вытаскивает стоп-флаги (incident / hold / owner);
5. чинит механические стопы: бюджет прогонов меньше объявленного таймаута, живой прогон
   перед убийством по лимиту, шапка реестра находок не по шаблону;
6. пишет строку в `logs/pipeline_watchdog.log` и отчёт `logs/pipeline_status.md`.

Проект можно вывести из-под сторожа флагом в его `pipeline.json`:
`"watchdog": {"enabled": false}` — так остаётся остановленным конвейер, который владелец
погасил сам.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def _root_home() -> Path:
    """Корень установки Hermes, а не профиль: отчёт сторожа принадлежит установке.

    В кроне `HERMES_HOME` привязан к профилю (`<корень>/profiles/<имя>`); писать отчёт туда
    значит снова привязать сторожа к профилю — ровно то, от чего уходим. Поэтому профильный
    путь схлопывается до корня, а сам корень берётся из `HERMES_HOME` (если он не профиль),
    иначе — из `LOCALAPPDATA`/`~/.hermes`.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        path = Path(env)
        if path.parent.name == "profiles":
            return path.parent.parent
        return path
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "hermes"
    return Path.home() / ".hermes"


HERMES_HOME = _root_home()
HERMES = HERMES_HOME / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
if not HERMES.exists():
    _found = shutil.which("hermes")
    HERMES = Path(_found) if _found else HERMES
LOG = HERMES_HOME / "logs" / "pipeline_watchdog.log"
REPORT = HERMES_HOME / "logs" / "pipeline_status.md"

# Если реестра нет (свежая машина) — минимальный список: сам плагин и два известных проекта.
DEFAULT_PROJECTS = [("hermes-step-pipeline", "C:/Users/malik/PycharmProjects/hermes-step-pipeline")]

ENGINE_BROKEN = ("нет файла движка", "движок изменён после установки", "нет файла движка:")
TIMEOUT = 240


def projects() -> list[tuple[str, str]]:
    """(имя, путь) всех конвейеров из реестра, кроме выключенных флагом `watchdog.enabled`.

    Реестр — тот же `projects.json`, что у панели и ролей (`PIPELINE_PROJECTS_FILE` или
    `<home профиля>/projects.json`). Один список на всю установку: добавил проект в реестр —
    сторож его видит, ничего не ставя в самом проекте.
    """
    path = Path(os.environ.get("PIPELINE_PROJECTS_FILE") or (HERMES_HOME / "projects.json"))
    if not path.exists():
        # Проект мог положить реестр в свой профиль — читаем его, но корень в приоритете.
        fallback = Path(os.environ.get("HERMES_HOME") or "") / "projects.json"
        if fallback.exists():
            path = fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(DEFAULT_PROJECTS)
    out: list[tuple[str, str]] = []
    for item in data.get("projects") or []:
        if not isinstance(item, dict):
            continue
        root = str(item.get("root") or "").strip()
        if not root:
            continue
        name = str(item.get("name") or item.get("id") or root)
        try:
            cfg = json.loads((Path(root) / "pipeline.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        flag = cfg.get("watchdog")
        if isinstance(flag, dict) and flag.get("enabled") is False:
            continue
        out.append((name, root))
    return out or list(DEFAULT_PROJECTS)


def _force_utf8() -> None:
    """Консоль Windows по умолчанию cp1251 — эмодзи в отчёте её ломают."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "buffer"):
            continue
        if (stream.encoding or "").lower().replace("-", "") == "utf8":
            continue
        try:
            setattr(sys, stream_name,
                    io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
        except (AttributeError, ValueError):
            pass


def run(cmd: list[str], cwd: str | None = None, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    env = dict(os.environ, HERMES_HOME=str(HERMES_HOME), PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"таймаут {timeout} с"
    except OSError as exc:
        return 127, "", f"не запустить {cmd[0]}: {exc}"


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        return str(pid) in out
    except Exception:
        return True  # не смогли проверить — считаем живым, чужой тик не рушим


def clear_stale_lock(project: Path) -> str:
    lock = project / ".pipeline" / "state" / "tick.lock"
    if not lock.exists():
        return ""
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        return ""
    pid = int(data.get("pid") or 0)
    if pid_alive(pid):
        return f"тик уже идёт (pid {pid}, с {data.get('at')})"
    try:
        lock.unlink()
    except OSError as exc:
        return f"замок не снять: {exc}"
    return f"снят зависший замок тика (pid {pid} мёртв, с {data.get('at')})"


def last_action(stdout: str) -> str:
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("Выполнено") or ln.startswith("[notify]") or "вопрос" in ln.lower():
            return ln
    return lines[-1] if lines else ""


def plan_flags(project: Path) -> tuple[list[str], list[str]]:
    """(строки активных планов, стоп-флаги) из `gate.py --plans`.

    Стоп-флаги берутся только по ведомым планам: нерешимые флаги давно закрытых планов
    (incident плана 25 у DoorSystem, hold плана 28) — исторический шум, конвейер они не держат.
    """
    gate = project / ".pipeline" / "gate.py"
    if not gate.exists():
        return [], ["нет движка (.pipeline/gate.py)"]
    rc, out, err = run([sys.executable, str(gate), "--plans"], cwd=str(project), timeout=180)
    if rc != 0:
        return [], [f"--plans: rc={rc} {err.strip()[:160]}"]
    active, flags = [], []
    for ln in out.splitlines():
        ln = ln.rstrip()
        if not ln or ln.startswith("ПЛАН") or ln.startswith("---"):
            continue
        parts = ln.split()
        if len(parts) < 3:
            continue
        plan_id, driven, nxt = parts[0], parts[1], parts[2]
        tail = " ".join(parts[3:])
        if driven != "да":
            continue
        active.append(f"{plan_id}:{nxt}" if nxt != "—" else f"{plan_id}:закрыт")
        if "incident:" in tail:
            what = tail.split("incident:", 1)[1].strip()
            flags.append(f"план {plan_id} инцидент: {what[:150]}")
        elif "owner:" in tail:
            flags.append(f"план {plan_id} держит владелец: {tail.split('owner:',1)[1].strip()[:120]}")
        elif "hold:" in tail:
            flags.append(f"план {plan_id} ручной допуск: {tail.split('hold:',1)[1].strip()[:80]}")
    return active, flags


def stall_hours(project: Path, active: list[str]) -> list[str]:
    """Планы, у которых последнее действие старше 3 ч, а открытый шаг есть."""
    hist = project / ".pipeline" / "state" / "history.jsonl"
    if not hist.exists():
        return []
    last: dict[str, float] = {}
    try:
        for ln in hist.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            pid = str(rec.get("plan") or "")
            ts = float(rec.get("ts") or 0)
            if pid and ts > last.get(pid, 0):
                last[pid] = ts
    except OSError:
        return []
    out = []
    now = time.time()
    for item in active:
        pid = item.split(":", 1)[0]
        if item.endswith(":закрыт"):
            continue
        age_h = (now - last.get(pid, 0)) / 3600 if last.get(pid) else None
        if age_h is None or age_h >= 3:
            out.append(f"план {pid} без движения " + (f"{age_h:.0f} ч" if age_h is not None else "ни разу"))
    return out



def diagnose_tick(rc: int, err: str, out: str) -> str:
    text = (err + " " + out).lower()
    if rc == 0:
        return ""
    if "нет файла движка" in text:
        return "движок проекта неполный"
    if "движок изменён после установки" in text:
        return "маркер целостности движка разошёлся"
    if "тик уже идёт" in text or "locked" in text:
        return "тик уже идёт"
    return f"тик отказал rc={rc}"


def self_heal(project: Path) -> str:
    rc, out, err = run([str(HERMES), "pipeline", "setup", "--upgrade", "--project", str(project)],
                       cwd=str(project), timeout=420)
    ver = ""
    for ln in (out + err).splitlines():
        if "Движок:" in ln:
            ver = ln.strip("─ ").strip()[:120]
            break
    return f"переустановлен движок: {ver or ('rc=' + str(rc))}"


def fix_findings_header(project: Path) -> str:
    """Шапка реестра находок: движок ждёт дословно «Что сделать».

    Замер 23.11.2026: постановщик написал «Что сделано» — колонки те же, смысл тот же, а
    страж держал план 3 S12B тринадцать часов («реестр находок не читается»). Правка узкая:
    меняем только строку шапки в `docs/reviews/*findings*.md`, тело таблицы не трогаем.
    """
    header = "| ID | Класс | Находка | Что сделать | Статус |"
    changed: list[str] = []
    reviews = project / "docs" / "reviews"
    if not reviews.is_dir():
        return ""
    for path in sorted(reviews.glob("*findings*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cols) != 5 or cols[0] != "ID" or cols[2] != "Находка" or cols[4] != "Статус":
                continue
            if cols[3] == "Что сделать":
                break
            lines[index] = header
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            changed.append(path.name)
            break
    if not changed:
        return ""
    _git(project, "add", "-A", "docs/reviews")
    _git(project, "commit", "-m", "конвейер: шапка реестра находок приведена к шаблону движка "
                                  "(«Что сделать»)" )
    _git(project, "push")
    return "шапка реестра находок поправлена: " + ", ".join(changed)


def fix_run_budget(project: Path) -> str:
    """Поднять `step_runs.budget`, если он меньше объявленных таймаутов прогонов.

    Находка 23.11.2026 (план 14, S3): бюджет по умолчанию 600 с, а постановка объявила
    прогон с таймаутом 1800 с. Полный `pytest` упал в `timeout`, второй пункт ушёл в
    `skipped`, страж выдал инцидент «бюджет прогонов исчерпан» и остановил план — шаг
    нельзя было закрыть, пока бюджет не поднят руками. Правило: бюджет не меньше суммы
    объявленных таймаутов с полуторным запасом (и не меньше суток/60).

    Ключа `step_runs.budget` нет — законное «авто» (план 17, S5): движок сам считает
    полтора объявленных таймаута шага, и файл не правится вовсе. Иначе сторож на
    ближайшем часу вписал бы в конфиг фиксированное число и отменил авто-умолчание —
    ровно ту находку, ради которой ключ и перестал быть числом.
    """
    cfg_path = project / "pipeline.json"
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        cfg = json.loads(raw)
    except (OSError, ValueError) as exc:
        return f"конфиг не прочитать: {exc}"
    if (cfg.get("step_runs") or {}).get("budget") is None:
        return ""
    plans_dir = project / "docs" / "plans"
    declared = 0
    max_timeout = 0
    for md in sorted(plans_dir.glob("*.md")) if plans_dir.is_dir() else []:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^-\s*Прогон:.*?таймаут\s+(\d+)", text, re.M):
            value = int(m.group(1))
            declared += value
            max_timeout = max(max_timeout, value)
    current = int(((cfg.get("step_runs") or {}).get("budget")) or 600)
    needed = max(max_timeout + 600, 3600)
    if current >= needed:
        return ""
    block = cfg.get("step_runs") or {}
    block["budget"] = needed
    block.setdefault("timeout", 240)
    cfg["step_runs"] = block
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for args in (("add", "pipeline.json"),
                 ("commit", "-m", f"конвейер: бюджет прогонов шага {current} -> {needed} с "
                                  f"(макс. объявленный таймаут {max_timeout} с)"),
                 ("push",)):
        rc, out = _git(project, *args)
        if rc != 0:
            return f"бюджет прогонов {current} -> {needed} с, но git {args[0]}: {out[:100]}"
    return (f"бюджет прогонов шага {current} -> {needed} с "
            f"(макс. объявленный таймаут {max_timeout} с), коммит и пуш")


def extend_running_cards(project: Path) -> list[str]:
    """Поднять `max_runtime_seconds` живому прогону, у которого осталось меньше 15 минут.

    Правило владельца 22.11.2026: лимит шага поднимаем точечно в `kanban.db`, когда шаг
    живой и работает. Без этого длинный шаг убивается на самом интересном месте, а
    перезапуск сжигает прогон из бюджета.
    """
    try:
        cfg = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    board = str(cfg.get("board") or "")
    db = HERMES_HOME / "kanban" / "boards" / board / "kanban.db"
    if not board or not db.exists():
        return []
    notes: list[str] = []
    try:
        con = sqlite3.connect(str(db), timeout=30)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id, title, started_at, max_runtime_seconds, last_heartbeat_at "
            "from tasks where status='running'").fetchall()
        now = time.time()
        for row in rows:
            started = float(row["started_at"] or 0)
            limit = int(row["max_runtime_seconds"] or 0)
            beat = float(row["last_heartbeat_at"] or 0)
            if not started or not limit:
                continue
            elapsed = now - started
            if beat and now - beat > 900:
                continue                      # прогон мёртв — это не наш случай
            if limit - elapsed > 900:
                continue
            new_limit = int(elapsed) + 7200
            con.execute("update tasks set max_runtime_seconds=? where id=?",
                        (new_limit, row["id"]))
            notes.append(f"{row['id']}: лимит {limit // 60} -> {new_limit // 60} мин "
                         f"(идёт {elapsed / 60:.0f} мин)")
        con.commit()
        con.close()
    except sqlite3.Error as exc:
        return [f"карточки не прочитать: {exc}"]
    return notes


def _git(project: Path, *args: str) -> tuple[int, str]:
    rc, out, err = run(["git", *args], cwd=str(project), timeout=120)
    return rc, (out + err).strip()


def plan_decisions(project: Path, ids: list[str]) -> list[tuple[str, str, str]]:
    """(план, действие, причина) по каждому активному плану — короткий JSON-статус стража."""
    gate = project / ".pipeline" / "gate.py"
    out: list[tuple[str, str, str]] = []
    for pid in ids[:8]:
        rc, so, se = run([sys.executable, str(gate), "--status", "--plan", pid, "--json"],
                         cwd=str(project), timeout=120)
        if rc != 0:
            out.append((pid, "error", (se or so).strip()[:120]))
            continue
        try:
            start = so.find("{")
            dec = json.loads(so[start:]).get("decision") or {}
            out.append((pid, str(dec.get("action") or ""), str(dec.get("reason") or "")))
        except Exception as exc:
            out.append((pid, "error", f"статус не разобрать: {exc}"))
    return out


def handle(name: str, path: str, report: list[str], fixes: list[str]) -> None:
    project = Path(path)
    line: list[str] = []
    note = clear_stale_lock(project)
    if note.startswith("снят"):
        fixes.append(f"{name}: {note}")

    rc, out, err = run([str(HERMES), "pipeline", "run", "--project", str(project)], cwd=str(project),
                       timeout=1800)
    reason = diagnose_tick(rc, err, out)
    if rc == 3 and ("движок" in (err + out).lower()):
        fixes.append(f"{name}: {self_heal(project)}")
        rc, out, err = run([str(HERMES), "pipeline", "run", "--project", str(project)],
                           cwd=str(project), timeout=1800)
        reason = diagnose_tick(rc, err, out)

    if rc == 0:
        act = last_action(out)
        if "тик уже идёт" in act or "конвейер ждёт" in act:
            line.append("тик ok (ждал: шаг/ревью/стоп)"
                        if "конвейер ждёт" in act else "тик уже идёт — его держит другой процесс")
        else:
            line.append("тик ok" + (f": {act[:150]}" if act else " (ждал, действий нет)"))
    else:
        line.append(f"ТИК УПАЛ: {reason or rc}")
        if err.strip():
            line.append("stderr: " + " ".join(err.split())[:160])

    active, flags = plan_flags(project)
    if active:
        line.append("планы " + ", ".join(active[:8]))
    for f in flags[:4]:
        line.append("⚠ " + f[:170])

    # Две починки, которые 22–23.11 делались руками: ложный инцидент бюджета прогонов и
    # лимит живого прогона. Обе пишут след в отчёт и в лог, обе идемпотентны.
    if any("бюджет прогонов исчерпан" in f for f in flags):
        note_fix = fix_run_budget(project)
        if note_fix:
            fixes.append(f"{name}: {note_fix}")
        else:
            line.append("🔴 бюджет прогонов исчерпан, а объявленные таймауты меньше бюджета — "
                        "нужно решение владельца")
    for note_card in extend_running_cards(project):
        fixes.append(f"{name}: {note_card}")
    held = {f.split("план ", 1)[1].split(" ", 1)[0] for f in flags if "держит владелец" in f}
    for f in stall_hours(project, [a for a in active if a.split(":", 1)[0] not in held])[:3]:
        line.append("⏳ " + f)

    # Починка 23.11.2026: шапка реестра находок, где четвёртая колонка названа по-другому
    # («Что сделано» вместо «Что сделать»), держала план 3 S12B 13 часов. Колонки те же —
    # правим шапку сами, страж перечитает файл на ближайшем тике.
    if any("реестр находок" in f for f in flags):
        note_head = fix_findings_header(project)
        if note_head:
            fixes.append(f"{name}: {note_head}")
        else:
            line.append("🔴 реестр находок: шапка не подходит, а починить нечего — смотреть файл")

    # Конвейеру нечего делать: все планы закрыты или стоят на стопе. Это не «работает»,
    # а простой, и владелец должен увидеть его сразу, а не по факту к вечеру.
    open_ids = [a for a in active if not a.endswith(":закрыт")]
    if not open_ids:
        line.append("🟡 открытых планов нет — конвейер простаивает: нужен новый план или "
                    "снятие стопа")

    # Разбор решений стража: два класса починки/эскалации.
    ids = [item.split(":", 1)[0] for item in active if not item.endswith(":закрыт")]
    for pid, action, why in plan_decisions(project, ids):
        if "переутверди" in why:
            rc2, so2, se2 = run([str(HERMES), "pipeline", "plan", "--accept", pid],
                                cwd=str(project), timeout=180)
            ok = "переутверждён" in (so2 + se2)
            if ok:
                fixes.append(f"{name}: план {pid} переутверждён после правки плана")
                run([str(HERMES), "pipeline", "run", "--project", str(project)],
                    cwd=str(project), timeout=1800)
            else:
                line.append(f"⚠ план {pid}: переутверждение не прошло ({(so2 + se2).strip()[:100]})")
        elif action == "idle" and ("blocked" in why or "инцидент" in why):
            line.append(f"🔴 план {pid}: НУЖНО РЕШЕНИЕ — {why[:140]}")
        elif "лимит" in why and "круг" in why:
            line.append(f"🔴 план {pid}: {why[:140]}")

    report.append("• " + name + " — " + "; ".join(line))


    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {name} rc={rc}\n")
        fh.write(out[-4000:] + ("\n--stderr--\n" + err[-2000:] if err.strip() else ""))


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print("usage: hermes pipeline watchdog\n\n"
              "Один проход сторожа по всем конвейерам реестра `projects.json`: тик каждого,\n"
              "снятие зависшего замка, переустановка движка при расхождении, механические\n"
              "починки и отчёт в <HOME>/logs/pipeline_status.md. Флаг `watchdog.enabled: false`\n"
              "в pipeline.json проекта выводит его из-под сторожа.")
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    report: list[str] = []
    fixes: list[str] = []
    for name, path in projects():
        try:
            handle(name, path, report, fixes)
        except Exception as exc:  # один проект не должен убивать отчёт по остальным
            report.append(f"• {name} — ОШИБКА КОНТРОЛЯ: {type(exc).__name__}: {exc}")

    print("🕐 Конвейеры " + time.strftime("%d.%m %H:%M"))
    for ln in report:
        print(ln)
    if fixes:
        print("Починено: " + "; ".join(fixes))

    # Копия отчёта файлом: Telegram с этого хоста периодически недоступен (DPI), и тогда
    # единственный след контроля — этот файл. Путь один для всех профилей: отчёт принадлежит
    # плагину, а не тому профилю, чей крон его запустил.
    try:
        card = REPORT
        card.parent.mkdir(parents=True, exist_ok=True)
        with card.open("w", encoding="utf-8") as fh:
            fh.write(f"# Конвейеры, состояние на {time.strftime('%d.%m.%Y %H:%M')}\n\n")
            for ln in report:
                fh.write("- " + ln.lstrip("• ").strip() + "\n")
            if fixes:
                fh.write("\n**Починено:** " + "; ".join(fixes) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
