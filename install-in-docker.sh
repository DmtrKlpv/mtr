#!/bin/sh
# MTR Music Server — установка и пересборка в Docker одним скриптом.
#
# Объединяет прежние scripts/clean-rebuild.sh и scripts/selfcheck.sh:
#   1) проверка минимальных требований (Docker, Compose v2, память, диск, архитектура);
#   2) вопрос про удаление кэша обложек (covers) — отдельно от баз;
#   3) сборка образа с нуля;
#   4) запуск контейнера;
#   5) самопроверка уже внутри контейнера (Pillow, WebP, ffmpeg, CLI, монтирования).
#
# Данные (mtr_users.db, mtr_music.db, secret.key) по умолчанию НЕ трогаются.
# Их удаление — только по явному выбору (--wipe-data или ответ в запросе).
#
# Флаги:
#   --data-dir PATH    каталог данных на хосте, если автопоиск по docker-compose.yml не сработал
#   --wipe-data        удалить базы (с -wal/-shm) и secret.key в каталоге данных
#   --wipe-covers      удалить каталог covers (кэш обложек) без вопроса
#   --keep-covers      не спрашивать про covers и оставить его
#   --yes              не задавать вопросы, применять безопасные ответы по умолчанию
#   --skip-checks      не проверять минимальные требования
#   -h, --help         эта справка
#
# Запуск (из каталога проекта с docker-compose.yml):
#   sh install-in-docker.sh
#   sh install-in-docker.sh --data-dir /share/CACHEDEV1_DATA/Container/mtr-data
#   sh install-in-docker.sh --wipe-data --wipe-covers --yes
#
# Ответы читаются из stdin. Пустой ответ (Enter) — безопасный вариант:
# ничего не удалять, установку продолжать.

set -eu

CONTAINER=mtr-app
IMAGE_TAG_PREFIX=mtr-app
DEFAULT_DATA_DIR=/share/CACHEDEV1_DATA/Container/mtr-data

# Минимальные требования. Проверяются мягко: при несоответствии скрипт
# спрашивает, продолжать ли (y/n), а не падает.
MIN_MEM_MB=512
MIN_DISK_MB=1024
MIN_COMPOSE_MAJOR=2

DATA_DIR_HOST=""
DATA_FROM_FLAG=0
WIPE_DATA=0
WIPE_COVERS=0
COVERS_ANSWERED=0
ASSUME_YES=0
SKIP_CHECKS=0
ANSWER=""

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

die() {
    printf '\nОШИБКА: %s\n' "$*" >&2
    exit 1
}

note() { printf '%s\n' "$*"; }
head2() { printf '\n== %s ==\n' "$*"; }

# ask "вопрос" — читает ответ в ANSWER. Пустой ввод безопасен.
ask() {
    if [ "$ASSUME_YES" = "1" ]; then
        ANSWER=""
        return 0
    fi
    printf '%s ' "$1"
    if ! IFS= read -r ANSWER; then
        ANSWER=""
    fi
}

yes_answer() {
    case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
        y|yes|д|да) return 0 ;;
        *) return 1 ;;
    esac
}

# ----------------------------------------------------------------------
# Разбор флагов
# ----------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --data-dir)
            [ $# -ge 2 ] || die "--data-dir требует путь"
            DATA_DIR_HOST="$2"
            DATA_FROM_FLAG=1
            shift 2
            ;;
        --wipe-data) WIPE_DATA=1; shift ;;
        --wipe-covers) WIPE_COVERS=1; COVERS_ANSWERED=1; shift ;;
        --keep-covers) WIPE_COVERS=0; COVERS_ANSWERED=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --skip-checks) SKIP_CHECKS=1; shift ;;
        -h|--help) usage ;;
        *) die "неизвестный флаг: $1 (см. --help)" ;;
    esac
done

cd "$(dirname "$0")"

# ----------------------------------------------------------------------
# Баннер
#
# Арт из трёх букв по 10 символов, между буквами по 2 пробела — итого 34.
# Раньше строки были разной длины (у R верхние были короче на 2), из-за чего
# в терминале каждая строка уезжала на символ. Теперь ширина у всех одна,
# а версия центрируется по той же ширине.
#
# Цвет включается только при выводе в терминал: в `docker logs` и в CI
# escape-последовательности не нужны.
# ----------------------------------------------------------------------
BANNER_WIDTH=34

banner_version() {
    # На хосте, где только Docker, python может отсутствовать: падать из-за
    # этого нельзя, поэтому две попытки и честное «нет».
    v=$(python3 -c "import app; print(app.__version__)" 2>/dev/null) \
        || v=$(python -c "import app; print(app.__version__)" 2>/dev/null) \
        || v=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/__init__.py 2>/dev/null | head -n 1) \
        || v=""
    [ -n "$v" ] || v="нет"
    printf '%s' "$v"
}

center() {
    # $1 — текст, $2 — ширина. Центрируем по символам, а не на глаз.
    text="$1"
    pad=$(( (${2:-$BANNER_WIDTH} - ${#text}) / 2 ))
    [ "$pad" -gt 0 ] || pad=0
    printf '%*s%s' "$pad" "" "$text"
}

print_banner() {
    version="$(banner_version)"
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        accent=$(printf '\033[36m'); dim=$(printf '\033[2m'); off=$(printf '\033[0m')
    else
        accent=""; dim=""; off=""
    fi
    printf '\n%s\n' "$accent"
    printf '%s\n' \
"██      ██  ██████████  ██████████" \
"██      ██  ██████████  ██████████" \
"████  ████      ██      ██      ██" \
"████  ████      ██      ██      ██" \
"██  ██  ██      ██      ██████████" \
"██  ██  ██      ██      ██████████" \
"██      ██      ██      ██    ██  " \
"██      ██      ██      ██    ██  " \
"██      ██      ██      ██      ██" \
"██      ██      ██      ██      ██"
    printf '%s' "$off$dim"
    printf '%s\n' "$(center "версия $version")"
    printf '%s\n\n' "$off"
}

print_banner

# ----------------------------------------------------------------------
# 1. Минимальные требования
# ----------------------------------------------------------------------
failures=0
warnings=0

check_soft() {
    # check_soft "описание" "команда..."
    desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        printf '  OK       %s\n' "$desc"
    else
        printf '  ВНИМАНИЕ %s\n' "$desc"
        failures=$((failures + 1))
    fi
}

check_arch() {
    arch=$(uname -m 2>/dev/null || echo unknown)
    case "$arch" in
        x86_64|amd64|aarch64|arm64|armv7l|armv7|armhf)
            printf '  OK       архитектура: %s\n' "$arch" ;;
        *)
            printf '  ВНИМАНИЕ архитектура %s не проверялась (ожидались amd64, arm64, arm/v7)\n' "$arch"
            failures=$((failures + 1)) ;;
    esac
}

check_memory() {
    if [ -r /proc/meminfo ]; then
        kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
        mb=$((kb / 1024))
    elif command -v sysctl >/dev/null 2>&1; then
        bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        mb=$((bytes / 1024 / 1024))
    else
        mb=0
    fi
    if [ "$mb" -ge "$MIN_MEM_MB" ]; then
        printf '  OK       память: %s МБ\n' "$mb"
    else
        printf '  ВНИМАНИЕ память: %s МБ (рекомендуется не меньше %s МБ)\n' "$mb" "$MIN_MEM_MB"
        failures=$((failures + 1))
    fi
}

check_disk() {
    avail_mb=$(df -Pm . 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
    avail_mb=${avail_mb:-0}
    if [ "$avail_mb" -ge "$MIN_DISK_MB" ]; then
        printf '  OK       свободно на диске: %s МБ\n' "$avail_mb"
    else
        printf '  ВНИМАНИЕ свободно на диске: %s МБ (нужно не меньше %s МБ)\n' "$avail_mb" "$MIN_DISK_MB"
        failures=$((failures + 1))
    fi
}

check_compose() {
    if docker compose version >/dev/null 2>&1; then
        ver=$(docker compose version --short 2>/dev/null || echo "?")
        major=$(printf '%s' "$ver" | cut -d. -f1)
        case "$major" in
            ''|*[!0-9]*) printf '  OK       docker compose: %s\n' "$ver" ;;
            *)
                if [ "$major" -ge "$MIN_COMPOSE_MAJOR" ]; then
                    printf '  OK       docker compose: %s\n' "$ver"
                else
                    printf '  ВНИМАНИЕ docker compose %s старше v%s\n' "$ver" "$MIN_COMPOSE_MAJOR"
                    failures=$((failures + 1))
                fi ;;
        esac
    elif command -v docker-compose >/dev/null 2>&1; then
        printf '  ВНИМАНИЕ найден docker-compose (v1). Нужен Compose v2: "docker compose"\n'
        failures=$((failures + 1))
    else
        printf '  ВНИМАНИЕ docker compose не найден\n'
        failures=$((failures + 1))
    fi
}

if [ "$SKIP_CHECKS" != "1" ]; then
    head2 "Проверка требований"
    check_soft "Docker установлен" docker --version
    check_soft "Docker отвечает" docker info
    check_compose
    check_memory
    check_disk
    check_arch

    if [ "$failures" -gt 0 ]; then
        printf '\nНе выполнено условий: %s\n' "$failures"
        printf 'Установка может не запуститься или работать медленно.\n'
        ask "Продолжить установку несмотря на это? (y/N):"
        if ! yes_answer "$ANSWER"; then
            note ""
            note "Установка остановлена. Устраните замечания и запустите снова."
            note "Пропустить проверку: sh install-in-docker.sh --skip-checks"
            exit 1
        fi
        note "Продолжаю по вашему выбору."
    else
        note "Все требования выполнены."
    fi
fi

# ----------------------------------------------------------------------
# 2. Каталог данных
# ----------------------------------------------------------------------
head2 "Данные"

# Путь из docker-compose.yml: строка вида "- /host/path:/data"
if [ "$DATA_FROM_FLAG" = "0" ] && [ -f docker-compose.yml ]; then
    detected=$(sed -n 's|^[[:space:]]*-[[:space:]]*\([^:]*\):/data.*$|\1|p' docker-compose.yml | head -n 1)
    if [ -n "$detected" ]; then
        DATA_DIR_HOST="$detected"
        note "Каталог данных из docker-compose.yml: $DATA_DIR_HOST"
    fi
fi

if [ -z "$DATA_DIR_HOST" ] && [ -d "$DEFAULT_DATA_DIR" ]; then
    DATA_DIR_HOST="$DEFAULT_DATA_DIR"
    note "Каталог данных по умолчанию: $DATA_DIR_HOST"
fi

if [ -z "$DATA_DIR_HOST" ]; then
    note "Каталог данных не найден автоматически."
    ask "Введите путь к каталогу данных (Enter — пропустить шаг с данными):"
    DATA_DIR_HOST="$ANSWER"
fi

if [ -n "$DATA_DIR_HOST" ] && [ ! -d "$DATA_DIR_HOST" ]; then
    note "Каталог $DATA_DIR_HOST ещё не существует — он будет создан Docker при запуске."
fi

# ----------------------------------------------------------------------
# 3. Кэш обложек (covers) — отдельный вопрос, отдельно от баз
# ----------------------------------------------------------------------
if [ -n "$DATA_DIR_HOST" ] && [ -d "$DATA_DIR_HOST/covers" ] && [ "$COVERS_ANSWERED" = "0" ]; then
    head2 "Кэш обложек"
    size=$(du -sh "$DATA_DIR_HOST/covers" 2>/dev/null | awk '{print $1}' || echo "?")
    count=$(find "$DATA_DIR_HOST/covers" -type f 2>/dev/null | wc -l | tr -d ' ')
    note "Найдено: $DATA_DIR_HOST/covers (файлов: $count, объём: $size)"
    note "Удаление безопасно: обложки создаются заново из тегов при первом запросе."
    note "Нужно, если обложки «залипли» или кэш разросся."
    ask "Удалить папку covers? (y/N):"
    if yes_answer "$ANSWER"; then
        WIPE_COVERS=1
    fi
fi
COVERS_ANSWERED=1

# ----------------------------------------------------------------------
# 4. Данные: базы и ключ. Только по явному подтверждению.
# ----------------------------------------------------------------------
if [ -n "$DATA_DIR_HOST" ] && [ "$WIPE_DATA" = "0" ] && [ "$ASSUME_YES" != "1" ]; then
    if [ -e "$DATA_DIR_HOST/mtr_users.db" ] || [ -e "$DATA_DIR_HOST/mtr_music.db" ]; then
        head2 "Базы данных"
        note "Найдены базы в $DATA_DIR_HOST."
        ask "Удалить базы и secret.key? Это необратимо: пользователи, плейлисты и история пропадут. (y/N):"
        if yes_answer "$ANSWER"; then
            WIPE_DATA=1
        fi
    fi
fi

delete_path() {
    target="$1"
    [ -e "$target" ] || return 0
    if rm -rf "$target" 2>/dev/null; then
        note "  удалено: $target"
    else
        if command -v sudo >/dev/null 2>&1; then
            sudo rm -rf "$target" && note "  удалено (sudo): $target" || \
                note "  НЕ УДАЛЕНО: $target (нет прав)"
        else
            note "  НЕ УДАЛЕНО: $target (нет прав)"
        fi
    fi
}

if [ "$WIPE_COVERS" = "1" ] && [ -n "$DATA_DIR_HOST" ]; then
    note "Удаляю кэш обложек…"
    delete_path "$DATA_DIR_HOST/covers"
fi

if [ "$WIPE_DATA" = "1" ] && [ -n "$DATA_DIR_HOST" ]; then
    note "Удаляю базы и ключ…"
    for f in mtr_users.db mtr_users.db-wal mtr_users.db-shm \
             mtr_music.db mtr_music.db-wal mtr_music.db-shm secret.key; do
        delete_path "$DATA_DIR_HOST/$f"
    done
fi

# ----------------------------------------------------------------------
# 5. Сборка и запуск
# ----------------------------------------------------------------------
head2 "Сборка"

note "Останавливаю и удаляю контейнер $CONTAINER (данные не трогаются)…"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Теги прошлых версий копятся и занимают место: убираем всё, кроме текущего образа.
note "Удаляю старые образы ${IMAGE_TAG_PREFIX}:* …"
docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null |
    grep -E "^${IMAGE_TAG_PREFIX}:" |
    while IFS= read -r image; do
        docker rmi -f "$image" >/dev/null 2>&1 || true
    done

note "Собираю образ (на arm/v7 это может занять ~10 минут из-за apt-get ffmpeg)…"
if ! docker compose build --no-cache; then
    die "сборка образа не удалась. Смотрите вывод выше."
fi

note "Запускаю контейнер…"
if ! docker compose up -d; then
    die "не удалось запустить контейнер. Проверьте docker-compose.yml (пути в volumes)."
fi

# Ждём, пока приложение ответит на /healthz: контейнер может быть ещё не готов.
note "Жду готовности приложения…"
i=0
while [ "$i" -lt 60 ]; do
    if docker exec "$CONTAINER" python -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4533/healthz', timeout=2).status == 200 else 1)" \
        >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 2
done

if [ "$i" -ge 60 ]; then
    note "Приложение не ответило за 120 с. Последние строки журнала:"
    docker logs --tail 30 "$CONTAINER" 2>&1 || true
    die "контейнер запущен, но приложение не отвечает."
fi
note "Приложение отвечает."

# ----------------------------------------------------------------------
# 6. Самопроверка внутри контейнера (бывший selfcheck.sh)
# ----------------------------------------------------------------------
head2 "Самопроверка"

if ! docker exec "$CONTAINER" sh /app/scripts/selfcheck.sh; then
    note ""
    note "Самопроверка нашла проблемы. Приложение может работать частично."
    note "Подробности выше. Повторить: docker exec $CONTAINER sh /app/scripts/selfcheck.sh"
fi

# Worker-процесс: скан и обложки живут в нём отдельно от веб-сервера.
head2 "Служба сканирования"
if docker exec "$CONTAINER" sh -c 'test -f /data/worker_status.json'; then
    age=$(docker exec "$CONTAINER" python -c "
import json, time, sys
try:
    data = json.load(open('/data/worker_status.json'))
except Exception:
    print(999); sys.exit(0)
print(int(time.time() - float(data.get('heartbeat', 0))))
" 2>/dev/null || echo 999)
    if [ "${age:-999}" -lt 15 ]; then
        note "  OK       worker работает (heartbeat ${age} с назад)"
    else
        note "  ВНИМАНИЕ worker не отвечает (heartbeat ${age} с назад). Смотрите: docker logs $CONTAINER"
    fi
else
    note "  ВНИМАНИЕ файла статуса worker-а нет. Смотрите: docker logs $CONTAINER"
fi

head2 "Готово"
note "Откройте в браузере: http://<адрес-сервера>:4533"
note "Первый вход создаёт администратора на странице /setup."
note "Журнал: docker logs -f $CONTAINER"
