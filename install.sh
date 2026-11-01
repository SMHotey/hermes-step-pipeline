#!/usr/bin/env bash
# Установка плагина step-pipeline в Hermes (Linux/macOS, а также git-bash на Windows).
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/hermes}"
[ -d "$HERMES_HOME" ] || HERMES_HOME="$HOME/.hermes"
SRC="$(cd "$(dirname "$0")" && pwd)/step_pipeline"
DST="$HERMES_HOME/plugins/step_pipeline"

[ -d "$SRC" ] || { echo "не найден пакет плагина: $SRC" >&2; exit 1; }

echo "1/3 копирую плагин → $DST"
mkdir -p "$(dirname "$DST")"
rm -rf "$DST"
cp -R "$SRC" "$DST"
find "$DST" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "2/3 включаю плагин"
hermes plugins enable step_pipeline

echo "3/3 готово"
echo "Дальше в каталоге проекта:  hermes pipeline setup"
echo "UI:                          hermes pipeline ui"
