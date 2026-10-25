#!/bin/sh
# Полная пересборка MTR: чистка Docker и сборка с нуля.
#
# Данные (mtr_users.db, mtr_music.db, secret.key) по умолчанию НЕ трогаются.
# Их удаление — только по явному выбору: флаг --wipe-data или ответ в запросе.
#
# Флаги:
#   --data-dir PATH   каталог данных на хосте, если автопоиск по docker-compose.yml не сработал
#   --wipe-data       удалить базы (с -wal/-shm) и secret.key в каталоге данных
#   --yes             не спрашивать подтверждение (для --wipe-data)
#   -h, --help        справка
#
# Без флагов скрипт спрашивает всё сам. Ответы читаются из stdin: при пустом
# вводе (Enter) используется безопасный вариант — ничего не удалять.
#
# Запуск (из каталога проекта на NAS):
#   sh scripts/clean-rebuild.sh
#   sh scripts/clean-rebuild.sh --data-dir /share/CACHEDEV1_DATA/Container/mtr-data --wipe-data

set -eu

CONTAINER=mtr-app
DEFAULT_DATA_DIR=/share/CACHEDEV1_DATA/Container/mtr-data

DATA_DIR_HOST=""
DATA_FROM_FLAG=0
WIPE_FLAG=0
ASSUME_YES=0
ANSWER=""
WIPE=0

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

die() {
    printf '\nОШИБКА: %s\n' "$*" >&2
    exit 1
}

step() {
    printf '\n[%s] %s\n' "$1" "$2"
}

# Читает ответ из stdin. EOF или пустой ввод дают пустую строку.
ask() {
    printf '%s ' "$1"
    ANSWER=""
    read -r ANSWER || ANSWER=""
}

while [ $# -gt 0 ]; do
    case "$1" in
        --data-dir)
            [ $# -ge 2 ] || die "--data-dir требует путь"
            DATA_DIR_HOST=$2
            DATA_FROM_FLAG=1
            shift 2
            ;;
        --wipe-data) WIPE_FLAG=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage ;;
        *) die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
done

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_DIR"

# ----------------------------------------------------------------------
step 1 "Каталог данных"
# ----------------------------------------------------------------------
if [ -z "$DATA_DIR_HOST" ]; then
    DATA_DIR_HOST=$(sed -n 's#^[[:space:]]*-[[:space:]]*\([^:]*\):/data[[:space:]]*$#\1#p' \
        docker-compose.yml 2>/dev/null | head -n 1)
    [ -n "$DATA_DIR_HOST" ] || DATA_DIR_HOST=$DEFAULT_DATA_DIR
    printf '  путь из docker-compose.yml: %s\n' "$DATA_DIR_HOST"
fi

DATA_OK=0
if [ -d "$DATA_DIR_HOST" ]; then
    DATA_OK=1
else
    printf '=> каталог %s не найден\n' "$DATA_DIR_HOST"
    if [ "$DATA_FROM_FLAG" -eq 0 ]; then
        attempt=0
        while [ "$attempt" -lt 3 ]; do
            ask "Введите путь к каталогу данных (Enter — пропустить шаг с данными):"
            [ -n "$ANSWER" ] || break
            if [ -d "$ANSWER" ]; then
                DATA_DIR_HOST=$ANSWER
                DATA_OK=1
                break
            fi
            printf '  каталог не найден: %s\n' "$ANSWER"
            attempt=$((attempt + 1))
        done
    fi
fi

if [ "$DATA_OK" -eq 1 ]; then
    count=$(ls -1 "$DATA_DIR_HOST" 2>/dev/null | wc -l)
    printf '  %s: %s объектов\n' "$DATA_DIR_HOST" "$count"
    for name in mtr_users.db mtr_music.db secret.key; do
        if [ -e "$DATA_DIR_HOST/$name" ]; then
            printf '  есть: %s\n' "$name"
        else
            printf '  нет:  %s\n' "$name"
        fi
    done
else
    printf '  данные не проверяются, шаг удаления пропущен\n'
fi

# ----------------------------------------------------------------------
step 2 "Решение об удалении данных"
# ----------------------------------------------------------------------
# Решение принимаем до того, как что-либо остановлено или удалено.
if [ "$WIPE_FLAG" -eq 1 ]; then
    [ "$DATA_OK" -eq 1 ] || die "--wipe-data: каталог данных не найден, указать его: --data-dir PATH"
    if [ "$ASSUME_YES" -eq 1 ]; then
        WIPE=1
    else
        ask "Удалить базы и secret.key в $DATA_DIR_HOST? Необратимо. Введите delete для подтверждения:"
        [ "$ANSWER" = "delete" ] || die "удаление не подтверждено, данные не тронуты"
        WIPE=1
    fi
elif [ "$DATA_OK" -eq 1 ]; then
    ask "Удалить базы и secret.key в $DATA_DIR_HOST? (y/N):"
    case "$ANSWER" in
        y|Y|yes|да)
            ask "Это необратимо: пользователи, пароли, плейлисты и история пропадут. Введите delete:"
            if [ "$ANSWER" = "delete" ]; then
                WIPE=1
            else
                printf '  не подтверждено, данные не тронуты\n'
            fi
            ;;
        *) printf '  данные сохраняются\n' ;;
    esac
fi

# ----------------------------------------------------------------------
step 3 "Остановка и удаление контейнера"
# ----------------------------------------------------------------------
docker rm -f "$CONTAINER" >/dev/null 2>&1 && printf '=> контейнер удалён\n' \
    || printf '  контейнера не было\n'

# ----------------------------------------------------------------------
step 4 "Удаление данных"
# ----------------------------------------------------------------------
if [ "$WIPE" -eq 1 ]; then
    case "$DATA_DIR_HOST" in
        ""|/) die "путь данных не похож на каталог: '$DATA_DIR_HOST'" ;;
    esac
    removed=0
    for name in mtr_users.db mtr_users.db-wal mtr_users.db-shm \
                mtr_music.db mtr_music.db-wal mtr_music.db-shm secret.key; do
        f="$DATA_DIR_HOST/$name"
        if [ -e "$f" ]; then
            rm -f "$f" || die "не удалось удалить $f (нужны права root? запустите через sudo)"
            printf '=> удалён %s\n' "$name"
            removed=$((removed + 1))
        fi
    done
    printf '=> удалено файлов: %s\n' "$removed"
    printf '  каталог covers/ не тронут: кэш обложек не хранится в базах, его можно очистить вручную\n'
else
    printf '  пропущено, данные сохранены\n'
fi

# ----------------------------------------------------------------------
step 5 "Удаление старых образов MTR"
# ----------------------------------------------------------------------
for image in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^mtr-app:' || true); do
    docker rmi -f "$image" >/dev/null 2>&1 && printf '=> удалён %s\n' "$image" \
        || printf '=> не удалось удалить %s\n' "$image"
done

# ----------------------------------------------------------------------
step 6 "Очистка кеша сборки"
# ----------------------------------------------------------------------
docker builder prune -af >/dev/null 2>&1 && printf '=> кеш сборки очищен\n' \
    || printf '=> не удалось очистить кеш сборки\n'

# ----------------------------------------------------------------------
step 7 "Сборка с нуля (--no-cache --pull; на armv7 до 10 минут)"
# ----------------------------------------------------------------------
# --no-cache: слой pip install не переиспользуется со старым requirements.txt.
# --pull: базовый образ обновляется, а не берётся из локального кеша.
if ! docker compose build --no-cache --pull; then
    die "сборка упала. Если это pip install Pillow, пришлите вывод целиком"
fi

# ----------------------------------------------------------------------
step 8 "Запуск"
# ----------------------------------------------------------------------
docker compose up -d

# ----------------------------------------------------------------------
step 9 "Ожидание готовности"
# ----------------------------------------------------------------------
ready=0
i=0
while [ "$i" -lt 30 ]; do
    if docker exec "$CONTAINER" python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4533/healthz', timeout=2).status==200 else 1)" >/dev/null 2>&1; then
        printf '=> сервер отвечает\n'
        ready=1
        break
    fi
    i=$((i + 1))
    sleep 2
done
[ "$ready" -eq 1 ] || die "сервер не ответил за 60 секунд. Смотрите: docker logs $CONTAINER"

# ----------------------------------------------------------------------
step 10 "Самопроверка"
# ----------------------------------------------------------------------
docker exec "$CONTAINER" sh /app/scripts/selfcheck.sh || die "самопроверка не прошла, образ собран не полностью"

# ----------------------------------------------------------------------
step 11 "Что дальше"
# ----------------------------------------------------------------------
if [ "$WIPE" -eq 1 ]; then
    printf '=> данные чистые: откройте веб-интерфейс и создайте администратора\n'
    printf '=> затем полный скан: docker exec -it %s python -m app.cli scan --mode full\n' "$CONTAINER"
fi
printf '=> замер обложек (холодный путь): docker exec -it %s python -m app.cli bench-covers --albums 60\n' "$CONTAINER"
printf '\nГотово.\n'
