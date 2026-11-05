"""HTML-страницы веб-интерфейса.

Разметка — оболочка: слева рейл с навигацией, в центре контент, снизу
плеер. Всё наполнение подгружает `static/app.js` через `/api/web/*`.

CSP важнее удобства: скрипты только с 'self', никаких inline-обработчиков
(`onclick="..."` не пройдёт). Поэтому вся логика — в `static/*.js`,
а разметка ссылается на неё через `data-*` атрибуты.

Порядок подключения скриптов важен: `dialogs.js` объявляет диалоги,
`app.js` их использует, `scan_import.js` опирается на хелперы `app.js`.
Поэтому все три идут в этом порядке и без `defer`/`async`.
"""

from __future__ import annotations

HEAD_COMMON = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="icon" href="/static/favicon.ico" sizes="any">
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="manifest" href="/static/manifest.webmanifest">
<meta name="theme-color" content="#d7e021">
<link rel="stylesheet" href="/static/theme.css">
<link rel="stylesheet" href="/static/style.css">"""


LOGIN_HTML = f"""<!DOCTYPE html>
<html lang="ru">
<head>
{HEAD_COMMON}
<title>Вход — MTR</title>
</head>
<body class="login-body">
<main class="login-card">
  <img class="login-logo" src="/static/icon.svg" alt="" width="56" height="56">
  <h1>MTR</h1>
  <form id="login-form" autocomplete="on">
    <label for="username">Пользователь</label>
    <input id="username" name="username" autocomplete="username" required autofocus>
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password" required>
    <div id="login-error" class="error" role="alert" hidden></div>
    <button type="submit" id="login-submit" class="primary">Войти</button>
     <p class="muted">© Dmtr Klpv, DKI, 2026</p>
  </form>
</main>
<script src="/static/login.js"></script>
</body>
</html>
"""


APP_HTML = f"""<!DOCTYPE html>
<html lang="ru">
<head>
{HEAD_COMMON}
<title>MTR</title>
</head>
<body>
<div class="shell">

  <aside class="rail" id="rail" aria-label="Навигация">
    <div class="rail-brand">
      <img src="/static/icon.svg" alt="" width="26" height="26">
      <span>MTR</span>
    </div>

    <div class="rail-group">
      <button class="rail-btn" data-view="library" title="Библиотека"
              aria-label="Библиотека">
        <img class="icon" src="/static/icons/home.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn" data-view="playlists" title="Плейлисты"
              aria-label="Плейлисты">
        <img class="icon" src="/static/icons/playlist.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn" data-view="starred" title="Избранное"
              aria-label="Избранное">
        <img class="icon" src="/static/icons/heart.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn" data-view="recent" title="Недавно прослушанные"
              aria-label="Недавно прослушанные">
        <img class="icon" src="/static/icons/clock.svg" alt="" aria-hidden="true">
      </button>
    </div>

    <div class="rail-sep"></div>

    <div class="rail-group rail-admin">
      <button class="rail-btn admin-only" data-view="scan" title="Сканирование"
              aria-label="Сканирование">
        <img class="icon" src="/static/icons/refresh.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn admin-only" data-view="import" title="Импорт плейлистов"
              aria-label="Импорт плейлистов">
        <img class="icon" src="/static/icons/import.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn admin-only" data-view="users" title="Пользователи"
              aria-label="Пользователи">
        <img class="icon" src="/static/icons/users.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn admin-only" data-view="server" title="Сервер"
              aria-label="Сервер">
        <img class="icon" src="/static/icons/server.svg" alt="" aria-hidden="true">
      </button>
    </div>

    <div class="rail-bottom">
      <button class="rail-btn" id="menu-toggle" title="Настройки"
              aria-label="Настройки" aria-expanded="false">
        <img class="icon" src="/static/icons/gear.svg" alt="" aria-hidden="true">
      </button>
      <button class="rail-btn" id="logout" title="Выйти" aria-label="Выйти">
        <img class="icon" src="/static/icons/logout.svg" alt="" aria-hidden="true">
      </button>
    </div>
  </aside>

  <nav class="menu" id="menu" hidden>
    <button class="menu-item" data-view="settings">Настройки</button>
    <button class="menu-item" data-view="account">Аккаунт</button>
  </nav>

  <main id="view" class="view">
    <p class="muted">Загрузка…</p>
  </main>

  <footer class="player" id="player" hidden>
    <div class="player-track">
      <img id="cover-now" class="cover-now" src="/static/placeholder.svg" alt="">
      <div class="player-meta">
        <span id="now-title">—</span>
        <span id="now-album" class="muted small"></span>
      </div>
    </div>

    <div class="player-center">
      <div class="controls">
        <button id="prev" class="ghost icon-btn" title="Предыдущий" aria-label="Предыдущий"><img class="icon" src="/static/icons/prev.svg" alt="" aria-hidden="true"></button>
        <button id="play" class="play-btn icon-btn" title="Играть" aria-label="Играть"><img class="icon" src="/static/icons/play.svg" alt="" aria-hidden="true"></button>
        <button id="next" class="ghost icon-btn" title="Следующий" aria-label="Следующий"><img class="icon" src="/static/icons/next.svg" alt="" aria-hidden="true"></button>
      </div>
      <div class="player-progress">
        <input id="seek" type="range" min="0" max="1000" value="0" step="1"
               aria-label="Позиция трека">
        <span id="time" class="muted mono tiny">0:00 / 0:00</span>
      </div>
    </div>

    <div class="player-right">
      <button id="now-heart" class="ghost icon-btn heart" title="В избранное"
              aria-label="В избранное" hidden><img class="icon" src="/static/icons/heart.svg" alt="" aria-hidden="true"></button>
      <button id="shuffle" class="ghost icon-btn" title="Перемешать" aria-label="Перемешать"><img class="icon" src="/static/icons/shuffle.svg" alt="" aria-hidden="true"></button>
      <button id="repeat" class="ghost icon-btn" title="Повтор выключен" aria-label="Повтор"><img class="icon" src="/static/icons/repeat.svg" alt="" aria-hidden="true"></button>
    </div>
    <audio id="audio" preload="none"></audio>
  </footer>

</div>

<script src="/static/dialogs.js"></script>
<script src="/static/cover_queue.js"></script>
<script src="/static/app.js"></script>
<script src="/static/scan_import.js"></script>
<script src="/static/folder_sync.js"></script>
</body>
</html>
"""


SETUP_HTML = f"""<!DOCTYPE html>
<html lang="ru">
<head>
{HEAD_COMMON}
<title>Первый запуск — MTR</title>
</head>
<body class="login-body">
<main class="login-card setup-card">
  <div class="setup-head">
    <img class="login-logo" src="/static/icon.svg" alt="" width="56" height="56">
    <h1>MTR</h1>
    <p class="muted">Первый запуск: создайте администратора</p>
  </div>
  <form id="setup-form" autocomplete="on">
    <div class="field">
      <label for="username">Имя пользователя</label>
      <input id="username" name="username" autocomplete="username"
             value="admin" required autofocus>
    </div>
    <div class="field">
      <label for="password">Пароль</label>
      <input id="password" name="password" type="password"
             autocomplete="new-password" minlength="8" required>
      <span class="field-hint">Не короче 8 символов</span>
    </div>
    <div class="field">
      <label for="password2">Пароль ещё раз</label>
      <input id="password2" name="password2" type="password"
             autocomplete="new-password" minlength="8" required>
    </div>
    <div id="setup-error" class="error" role="alert" hidden></div>
    <button type="submit" id="setup-submit" class="primary">Создать и войти</button>
  </form>
  <p class="muted small" style="margin-top:14px">
    <!-- Пароль хранится только в базе, в виде хеша. В файлах конфигурации его нет. -->
  </p>
</main>
<script src="/static/setup.js"></script>
</body>
</html>
"""


def render_login(error: str | None = None) -> str:
    """Логин-страница. Ошибку показывает JS, но можно передать сразу."""
    return LOGIN_HTML


def render_setup() -> str:
    """Страница первого запуска: создание администратора.

    Нужна, чтобы не держать пароль в файлах: пока в базе нет ни одной учётной
    записи, вход открывает именно эта форма.
    """
    return SETUP_HTML


def render_app(username: str, role: str, csrf: str) -> str:
    """Оболочка приложения с внедрёнными данными сессии.

    Данные передаются через `data-` атрибуты, а не через inline-скрипт:
    CSP без 'unsafe-inline' для script-src это запрещает.
    """
    return APP_HTML.replace(
        '<body>',
        f'<body data-user="{username}" data-role="{role}" data-csrf="{csrf}">',
        1,
    )
