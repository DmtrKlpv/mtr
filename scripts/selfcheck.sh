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

# ffmpeg нужен аудиоядру, а не обложкам, но его отсутствие ломает стриминг.
check "ffmpeg доступен" sh -c "command -v ffmpeg"
check "каталог музыки смонтирован" sh -c "test -d \${MUSIC_DIR:-/music}"
check "каталог состояния смонтирован" sh -c "test -d \${DATA_DIR:-/data}"

echo
if [ "$fail" -eq 0 ]; then
    echo "Всё на месте ($ok проверок)."
    exit 0
fi
echo "Проблем: $fail из $((ok + fail)). Образ собран не полностью — пересоберите:"
echo "  docker compose build --no-cache && docker compose up -d"
exit 1
