#!/usr/bin/env bash
# Установка плагина step_pipeline в Hermes (Linux/macOS, а также git-bash на Windows).
set -euo pipefail

NAME="step_pipeline"
HERMES_HOME="${HERMES_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/hermes}"
[ -d "$HERMES_HOME" ] || HERMES_HOME="$HOME/.hermes"
SRC="$(cd "$(dirname "$0")" && pwd)/step_pipeline"
DST="$HERMES_HOME/plugins/$NAME"

[ -d "$SRC" ] || { echo "не найден пакет плагина: $SRC" >&2; exit 1; }

# Сторож «одного имени»: name в манифесте обязан совпадать с каталогом и командами ниже,
# иначе `plugins update`/`remove` ищут один каталог, а `plugins list` показывает другое имя.
MANIFEST_NAME="$(sed -n 's/^name:[[:space:]]*//p' "$SRC/plugin.yaml" | head -1 | tr -d '[:space:]')"
if [ "$MANIFEST_NAME" != "$NAME" ]; then
  echo "манифест $SRC/plugin.yaml называет плагин «$MANIFEST_NAME», а каталог и команды ждут «$NAME»" >&2
  echo "поправьте name: в plugin.yaml — расхождение ломает plugins update/remove" >&2
  exit 1
fi

echo "1/3 копирую плагин → $DST"
mkdir -p "$(dirname "$DST")"
rm -rf "$DST"
cp -R "$SRC" "$DST"
find "$DST" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

echo "2/3 включаю плагин"
hermes plugins enable "$NAME"

echo "3/3 готово"
echo "Дальше в каталоге проекта:  hermes pipeline setup"
echo "UI:                          hermes pipeline ui"
echo "Удаление:                    hermes plugins disable $NAME; hermes plugins remove $NAME"
