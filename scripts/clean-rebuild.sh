#!/bin/sh
# Полная пересборка MTR: чистка Docker и сборка с нуля.
# С возможностью очистки mtr-data и ручным вводом пути.

set -eu

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_DIR"

CONTAINER=mtr-app
DATA_DIR_HOST=/share/CACHEDEV1_DATA/Container/mtr-data

printf '\n[0] Конфигурация очистки данных\n'
printf 'Хотите ПОЛНОСТЬЮ удалить данные (базы и secret.key) перед сборкой? (y/N): '
read -r CONFIRM_CLEAR
case "$CONFIRM_CLEAR" in
    [Yy]* ) CLEAR_DATA=true ;;
    * ) CLEAR_DATA=false ;;
esac

printf '\n[1] Проверка каталога данных\n'
# Если каталог по умолчанию отсутствует, запрашиваем путь вручную
if [ ! -d "$DATA_DIR_HOST" ]; then
    printf '=> каталог %s не найден — проверьте путь в docker-compose.yml\n' "$DATA_DIR_HOST"
    while true; do
        printf 'Введите корректный путь к mtr-data (или Ctrl+C для отмены): '
        read -r USER_PATH
        if [ -n "$USER_PATH" ] && [ -d "$USER_PATH" ]; then
            DATA_DIR_HOST=$(cd "$USER_PATH" && pwd)
            break
        else
            printf 'Ошибка: Каталог "%s" не существует. Попробуйте еще раз.\n' "$USER_PATH"
        fi
    done
fi

# Логика удаления или проверки содержимого
if [ "$CLEAR_DATA" = true ]; then
    printf '=> Удаление содержимого %s...\n' "$DATA_DIR_HOST"
    # Удаляем файлы внутри папки, но саму папку оставляем, чтобы не сломать права Docker bind-mount
    rm -rf "${DATA_DIR_HOST:?}"/* "${DATA_DIR_HOST:?}"/.* 2>/dev/null || true
    printf '=> Каталог данных полностью очищен.\n'
else
    count=$(ls -1 "$DATA_DIR_HOST" 2>/dev/null | wc -l)
    printf '  %s: %s объектов (не трогаем)\n' "$DATA_DIR_HOST" "$count"
    if [ -f "$DATA_DIR_HOST/secret.key" ]; then
        printf '=> secret.key на месте — пароли останутся читаемыми\n'
    else
        printf '=> ВНИМАНИЕ: secret.key нет. Если это не первый запуск,\n'
        printf '=> сохранённые пароли будут нечитаемы. Проверьте путь.\n'
    fi
fi

printf '\n[2] Остановка и удаление контейнера\n'
docker rm -f "$CONTAINER" 2>/dev/null && printf '=> удалён\n' || printf '  контейнера не было\n'

printf '\n[3] Удаление старых образов MTR\n'
for image in $(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^mtr-app:' || true); do
    docker rmi -f "$image" >/dev/null 2>&1 && printf '=> удалён %s\n' "$image" || printf '=> не удалось удалить %s\n' "$image"
done

printf '\n[4] Чистка кеша сборки\n'
docker builder prune -af >/dev/null 2>&1 && printf '=> кеш сборки очищен\n' || printf '=> не удалось очистить кеш\n'

printf '\n[5] Сборка (no cache)\n'
if ! docker compose build --no-cache; then
    printf '\nСБОРКА УПАЛА. Последние строки выше показывают причину.\n'
    printf 'Если это pip install Pillow.\n'
    exit 1
fi

printf '\n[6] Запуск\n'
docker compose up -d

printf '\n[7] Ожидание готовности\n'
i=0
while [ "$i" -lt 30 ]; do
    if docker exec "$CONTAINER" python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4533/healthz', timeout=2).status==200 else 1)" 2>/dev/null; then
        printf '=> сервер отвечает\n'
        break
    fi
    i=$((i + 1))
    sleep 2
done

printf '\n[8] Самопроверка\n'
docker exec "$CONTAINER" sh /app/scripts/selfcheck.sh || {
    printf '\nОбраз собран не полностью.\n'
    exit 1
}

printf '\n[9] Что дальше\n'
printf '=> Замер обложек (кеш пуст после чистки? очистите отдельно):\n'
printf 'docker exec -it %s python -m app.cli covers --purge\n' "$CONTAINER"
printf 'docker exec -it %s sh -c '"'"'S=$(date +%%s); python -m app.cli covers --limit 60; echo "прошло $(( $(date +%%s) - S )) с"'"'"'\n' "$CONTAINER"
printf '\nГотово.\n'
