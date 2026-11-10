# -*- coding: utf-8 -*-
"""Утверждение плана: одна проверка «можно ли начинать шаги» и привязка ревью к версии.

Вопрос «можно ли начинать шаги» решается здесь и только здесь. Две части ответа:

    1. запись утверждения `plan_<НОМЕР>_approval.json` (её пишет владелец командой
       `hermes pipeline plan --accept N`) сверяется с sha256 файла плана;
    2. вердикт ревью плана из `docs/reviews/plan-<N>.md` привязан к тому же sha256
       меткой `plan_sha256:` в отчёте. Правка плана после APPROVE не отменяет
       утверждение владельца, но сбрасывает ревью: состояние `stale` означает
       «нужно повторное ревью», а отчёт без метки (`unbound`) вердиктом не считается.

Порядок состояний `review_status` намеренно жёсткий: `skipped` (план старого формата,
строки `Конституция:` нет) → `missing` (отчёта нет) → `invalid` (вердикт не распознан) →
`unbound` (нет метки) → `unreferenced` (вердикт привязан, но ревью не сослалось на пункты
конституции) → `stale` (метка не равна sha плана) → `fresh`. Ровно одно из них
попадает в причину, и причина сама называет, чего не хватает.

Модуль самодостаточен: только stdlib (`hashlib`, `json`, `re`, `pathlib`), импортов
движка нет — файл копируется в проект списком `ENGINE_FILES` и обязан работать без
плагина. Объект `cfg` читается как структурный: нужны `state_dir`, `reviews_dir` и
`rel()`.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

APPROVAL_TEMPLATE = "plan_{}_approval.json"

# Разбор файла вердикта — один шаблон на весь движок: ревью шага, ревью постановки и
# ревью плана читают одну строку `## ВЕРДИКТ: APPROVE|REQUEST_CHANGES` одинаково.
VERDICT_RE = re.compile(r"^#{0,4}\s*ВЕРДИКТ\s*:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                        re.IGNORECASE | re.MULTILINE)
COMMIT_RE = re.compile(r"коммит\S*\s*`([0-9a-f]{7,40})`", re.IGNORECASE)
# Метка привязки вердикта к версии плана: `plan_sha256: <64 hex>` в отчёте ревью.
PLAN_SHA_RE = re.compile(r"plan_sha256\s*[:=]\s*`?([0-9a-f]{64})", re.IGNORECASE)
# Ссылка отчёта ревью на пункты конституции (§5a, решение D4 шага 5-S9): строка
# начинается с `Конституция:` (допускается маркер списка) и содержит хотя бы одну цифру
# в той же строке. Признак — названный номер пункта: «план соответствует конституции»
# без номера ссылкой не считается, и проверить находку нечем.
CONSTITUTION_REF_RE = re.compile(r"^\s*(?:[-*+]\s*)?Конституция\s*:[^\n]*\d",
                                 re.IGNORECASE | re.MULTILINE)

# Строка шапки плана действующего формата (`docs/PLAN-FORMAT.md:196`). Дискриминатор,
# а не украшение: планы 2-4 и фикстуры тестов строки не имеют и ревью плана не требуют.
CONSTITUTION_MARK = "Конституция:"

# Состояния вердикта ревью плана. `fresh` — единственное, что открывает шаги; `skipped`
# открывает их потому, что плану старого формата ревью не назначается вовсе.
REVIEW_FRESH = "fresh"
REVIEW_SKIPPED = "skipped"
REVIEW_MISSING = "missing"
REVIEW_INVALID = "invalid"
REVIEW_UNBOUND = "unbound"
REVIEW_UNREFERENCED = "unreferenced"
REVIEW_STALE = "stale"


def _rel(cfg, path: Path) -> str:
    """Путь отчёта для сообщений: относительно корня проекта, если конфиг умеет."""
    rel = getattr(cfg, "rel", None)
    if callable(rel):
        try:
            return str(rel(path))
        except (OSError, ValueError):
            pass
    return str(path)


def approval_file_for(cfg, plan_id) -> Path:
    """Путь записи утверждения плана в каталоге состояния проекта."""
    return Path(cfg.state_dir) / APPROVAL_TEMPLATE.format(plan_id)


def sha256_file(path) -> str:
    """sha256 сырых байтов файла плана (без перекодирования: CRLF не мешает)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_approval(cfg, plan_id) -> dict | None:
    """Прочитать запись утверждения; нет файла, он битый или не словарь — `None`."""
    try:
        data = json.loads(approval_file_for(cfg, plan_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def has_constitution(plan_file) -> bool:
    """План действующего формата: строка `Конституция:` в шапке до первого `## `.

    Файла нет или он не читается — `False`, без исключения: отсутствие плана решает
    реестр, а не эта проверка.
    """
    try:
        text = Path(plan_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return False
        if stripped.startswith(CONSTITUTION_MARK):
            return True
    return False


def verdict_at(path) -> dict | None:
    """Разбор файла вердикта по пути: нужен отчёту ревью шага, постановки и плана.

    `verdict=NONE` — строки вердикта нет; `AMBIGUOUS` — строк несколько или они разные.
    """
    path = Path(path)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [m.group(1).upper() for m in VERDICT_RE.finditer(text)]
    unique = sorted(set(found))
    if not found:
        verdict = "NONE"
    elif len(unique) == 1:
        verdict = unique[0]
    else:
        verdict = "AMBIGUOUS"
    commit = COMMIT_RE.search(text)
    return {
        "path": str(path),
        "verdict": verdict,
        "occurrences": len(found),
        "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "commit": commit.group(1) if commit else None,
    }


def plan_sha_of(text: str) -> str | None:
    """sha256 плана из метки `plan_sha256:` в тексте отчёта; метки нет — `None`."""
    found = PLAN_SHA_RE.search(text or "")
    return found.group(1).lower() if found else None


def review_path(cfg, plan_id) -> Path:
    """Отчёт ревью плана: `docs/reviews/plan-<N>.md` — один файл на план."""
    return Path(cfg.reviews_dir) / f"plan-{plan_id}.md"


def review_status(cfg, spec) -> dict:
    """Привязан ли вердикт ревью к текущей версии плана.

    Возвращает `{state, reason, path, verdict, report_sha, label_sha, file_sha}`:
    `state` — одно из `REVIEW_*`, `reason` — текст для владельца (пустой у `fresh` и
    `skipped`), `report_sha` — поле `sha` из `verdict_at`, `label_sha` — sha из метки.
    """
    path = review_path(cfg, spec.id)
    file_sha = sha256_file(spec.file)
    result = {"state": REVIEW_FRESH, "reason": "", "path": str(path), "verdict": None,
              "report_sha": None, "label_sha": None, "file_sha": file_sha}
    if not has_constitution(spec.file):
        result["state"] = REVIEW_SKIPPED
        return result
    rel = _rel(cfg, path)
    parsed = verdict_at(path)
    if parsed is None:
        result["state"] = REVIEW_MISSING
        result["reason"] = f"ревью плана {spec.id} не проводилось: нет отчёта {rel}"
        return result
    result["verdict"] = parsed["verdict"]
    result["report_sha"] = parsed["sha"]
    if parsed["verdict"] not in ("APPROVE", "REQUEST_CHANGES"):
        result["state"] = REVIEW_INVALID
        result["reason"] = (f"вердикт ревью плана {spec.id} не распознан "
                            f"({parsed['verdict']}): {rel}")
        return result
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    label = plan_sha_of(text)
    result["label_sha"] = label
    if not label:
        result["state"] = REVIEW_UNBOUND
        result["reason"] = (f"в отчёте {rel} нет метки plan_sha256: вердикт ревью плана "
                            f"{spec.id} не привязан к версии плана")
        return result
    if not CONSTITUTION_REF_RE.search(text):
        # Вердикт привязан к версии плана, но соответствие принципам не проверено (§5a):
        # шаги не открываются, пока ревьюер не назовёт номер пункта конституции.
        result["state"] = REVIEW_UNREFERENCED
        result["reason"] = (
            f"в отчёте {rel} нет ссылки на пункты конституции: нужна строка "
            f"«Конституция: п.N — …»; ревью плана {spec.id} обязано назвать пункты "
            f"конституции проекта")
        return result
    if label != file_sha:
        result["state"] = REVIEW_STALE
        result["reason"] = (f"план {spec.id} правился после APPROVE: sha256 {label[:12]} "
                            f"не совпал с файлом {file_sha[:12]} — нужно повторное ревью "
                            f"({rel})")
        return result
    return result


def evaluate(cfg, spec) -> dict:
    """Полный ответ «можно ли начинать шаги»: запись утверждения плюс вердикт ревью.

    `state` — `approved` (sha записи совпал), `missing` (записи нет) или `changed`
    (запись есть, но sha не тот). `ok` — можно начинать шаги. `block_kind` говорит, чего
    именно не хватает: `approval` (утверждение владельца) или `review` (вердикт ревью
    плана); пустая строка — не хватает ничего. `review` — вложенный `review_status`.
    """
    fact = sha256_file(spec.file)
    record = read_approval(cfg, spec.id)
    review = review_status(cfg, spec)
    result = {"state": "approved", "reason": "", "ok": False, "block_kind": "",
              "record": record, "sha256": fact, "review": review, "report": review["path"]}
    if record is None:
        result["state"] = "missing"
        result["block_kind"] = "approval"
        result["reason"] = (
            f"план {spec.id} не утверждён: нет записи "
            f"{APPROVAL_TEMPLATE.format(spec.id)} (утверждает владелец: "
            f"hermes pipeline plan --accept {spec.id})")
        return result
    saved = str(record.get("sha256") or "")
    if saved != fact:
        result["state"] = "changed"
        result["block_kind"] = "approval"
        result["reason"] = (
            f"план {spec.id} изменён после утверждения — переутверди: "
            f"hermes pipeline plan --accept {spec.id} (sha256 {fact[:12]} не совпал "
            f"с записью {saved[:12] or '—'}, потом повторное ревью плана)")
        return result
    if review["state"] in (REVIEW_FRESH, REVIEW_SKIPPED):
        result["ok"] = True
        return result
    result["block_kind"] = "review"
    result["reason"] = review["reason"]
    return result
