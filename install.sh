#!/bin/sh
# Подключает скиллы проекта к Claude Code: симлинк на каждый каталог skills/<имя>
# в ~/.claude/skills. Повторный запуск безопасен — ссылки перезаписываются.
# Сторонние pcb-review и freecad-skills не подключаются: они лежат для справки.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$DST"
for s in kicad-schematic kicad-pcb; do
    ln -sfn "$HERE/skills/$s" "$DST/$s"
    echo "$DST/$s -> $HERE/skills/$s"
done
