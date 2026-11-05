#!/bin/sh
# Самопроверка образа: версия кода, Pillow, команды CLI.
#
# Зачем: несколько раз ловилась ситуация «версия 0.16.0, а команды и Pillow
# от 0.14.0». Это значит, что образ собран из смеси файлов. Скрипт проверяет
# три вещи сразу и говорит прямо, всё ли на месте.
#
# Запуск:
#   docker exec mtr-app sh /app/scripts/selfcheck.sh
#   sh scripts/selfcheck.sh          (локально, из каталога проекта)

set -u

ok=0
fail=0

check() {
    desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        printf '  OK    %s\n' "$desc"
        ok=$((ok + 1))
    else
        printf '  СБОЙ  %s\n' "$desc"
        fail=$((fail + 1))
    fi
}

echo "MTR — самопроверка"

version=$(python -c "import app; print(app.__version__)" 2>/dev/null || echo "нет")
printf '  версия кода: %s\n' "$version"

# Команда covers появилась в 0.15.0, bench-covers — в 0.16.0.
check "команда covers в CLI" sh -c \
    "python -m app.cli --help 2>&1 | grep -q 'covers'"
check "команда bench-covers в CLI" sh -c \
    "python -m app.cli --help 2>&1 | grep -q 'bench-covers'"

# Pillow нужен для уменьшения обложек до 600px. Без него оригиналы
# отдаются как есть — это и есть причина медленной сетки.
check "модуль PIL (Pillow)" python -c "import PIL"
pillow=$(python -c "import PIL; print(PIL.__version__)" 2>/dev/null || echo "нет")
printf '  Pillow: %s\n' "$pillow"

# Ресайз на месте — проверяем не импорт, а реальную работу.
check "уменьшение обложки работает" python -c "
import io
from PIL import Image
from app.covers.service import make_thumbnail
buf = io.BytesIO()
Image.new('RGB', (1200, 1200), (120, 90, 60)).save(buf, 'JPEG', quality=90)
out = make_thumbnail(buf.getvalue())
assert out is not None, 'make_thumbnail вернул None — ресайз не работает'
assert Image.open(io.BytesIO(out[0])).size == (600, 600), 'не 600x600'
"

# WebP: без поддержки в Pillow обложки WebP отдаются как есть (не ошибка).
# Проверка честная — реальное декодирование, а не флаг features.check.
webp=$(python -c "from app.covers.service import webp_support; print('да' if webp_support() else 'нет')" 2>/dev/null || echo "нет")
if [ "$webp" = "да" ]; then
    printf '  OK    WebP-декодер Pillow работает\n'
    ok=$((ok + 1))
else
    printf '  ВНИМАНИЕ  WebP-декодер Pillow не работает: обложки WebP не уменьшаются (без libwebp в образе)\n'
fi

# ffmpeg нужен аудиоядру, а не обложкам, но его отсутствие ломает стриминг.
check "ffmpeg доступен" sh -c "command -v ffmpeg"

# Монтирования существуют только внутри контейнера: /music и /data
# привязываются Docker'ом из docker-compose.yml. На хосте или в песочнице
# этих путей нет по определению, поэтому там проверка НЕ считается провалом,
# а честно помечается как пропущенная. Внутри контейнера отсутствие — ошибка.
if [ -f /.dockerenv ]; then
    check "каталог музыки смонтирован" sh -c "test -d \${MUSIC_DIR:-/music}"
    check "каталог состояния смонтирован" sh -c "test -d \${DATA_DIR:-/data}"
else
    skipped=0
    for path_var in MUSIC_DIR DATA_DIR; do
        eval "value=\${$path_var:-}"
        if [ -n "$value" ] && [ -d "$value" ]; then
            printf '  OK    %s задан и существует: %s\n' "$path_var" "$value"
            ok=$((ok + 1))
        else
            printf '  ПРОПУЩЕНО  %s: вне контейнера монтирования не проверяются\n' "$path_var"
            skipped=$((skipped + 1))
        fi
    done
fi

echo
if [ -n "${skipped:-}" ] && [ "$skipped" -gt 0 ]; then
    echo "Пропущено проверок вне контейнера: $skipped (запустите внутри mtr-app, чтобы проверить монтирования)"
fi
if [ "$fail" -eq 0 ]; then
    echo "Всё на месте ($ok проверок)."
    exit 0
fi
echo "Проблем: $fail из $((ok + fail)). Образ собран не полностью — пересоберите:"
echo "  docker compose build --no-cache && docker compose up -d"
exit 1
