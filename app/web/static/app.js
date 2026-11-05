/* MTR web UI — намеренно без фреймворков.
   Один файл: сборка не нужна, CDN не нужен, CSP строгий.
   Стили — в style.css, разметка — в templates.py. */

const state = {
  user: null,
  role: null,
  csrf: null,
  prefs: { library_view: 'albums' },
  albums: [],
  view: 'library',
  libraryView: 'albums',   // artists | albums | playlists
  starred: new Set(),
  queue: [],
  queueIndex: -1,
  shuffle: false,
  repeat: 'off',           // off | all | one
  scanRunning: false,      // идёт скан: заглушки списков показывают это
  scanPercent: 0,
};

const $ = (sel) => document.querySelector(sel);

// Иконка как <img> из /static/icons. Все одного размера (24x24 в файле),
// фактический размер задаётся CSS. Никаких эмодзи и символьных глифов:
// они по-разному выглядят в Windows, Linux и macOS.
function icon(name, extraClass = '') {
  const node = el('img', {
    class: 'icon' + (extraClass ? ' ' + extraClass : ''),
    src: `/static/icons/${name}.svg`,
    alt: '',
    'aria-hidden': 'true',
  });
  // Путь к SVG кладём в переменную: активное состояние красит иконку
  // акцентом через mask-image, и цвет берётся из --accent, а не из файла.
  node.style.setProperty('--icon-url', `url("/static/icons/${name}.svg")`);
  return node;
}

// Кнопка-иконка: сам <img> внутри, подпись — только в title/aria-label.
function iconButton(name, { cls = 'ghost icon-btn', title = '', onClick = null, id = '' } = {}) {
  const button = el('button', {
    class: cls, title, 'aria-label': title || null, id: id || null,
    onclick: onClick,
  }, icon(name));
  // Активное состояние рисует ::after кнопки через mask — путь нужен на ней.
  button.style.setProperty('--icon-url', `url("/static/icons/${name}.svg")`);
  return button;
}

/* ---------- Построение DOM ---------- */
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'value') node.value = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
};


// safeChildren(...) — фильтр для встроенного replaceChildren.
// replaceChildren(null) вставляет ТЕКСТОВЫЙ узел "null": именно так на
// странице настроек появлялась надпись null, когда пользователь — админ
// (ветка isAdmin ? null : ...). Свой el() такие значения отбрасывает,
// а встроенный метод — нет.
function safeChildren(...children) {
  const items = [];
  const push = (value) => {
    if (value === null || value === undefined || value === false) return;
    if (Array.isArray(value)) { value.forEach(push); return; }
    items.push(value);
  };
  children.forEach(push);
  return items;
}


// fill(node, ...children) — замена replaceChildren с отбрасыванием пустых.
// Встроенный replaceChildren(null) вставляет ТЕКСТОВЫЙ узел "null": именно
// так на странице настроек появлялась надпись null (ветка isAdmin ? null : …).
function fill(node, ...children) {
  const items = [];
  const push = (value) => {
    if (value === null || value === undefined || value === false) return;
    if (Array.isArray(value)) { value.forEach(push); return; }
    items.push(value);
  };
  children.forEach(push);
  node.replaceChildren(...items);
  return node;
}


// Путь к SVG хранится в --icon-url: активное состояние красит иконку
// акцентом через mask, а цвет берётся из --accent, не из файла.
// Для иконок из HTML (рейл, плеер) переменную надо заполнить из src —
// их создаёт шаблон, а не icon().
function hydrateIconVars(root = document) {
  root.querySelectorAll('img.icon').forEach((img) => {
    if (!img.dataset.src) img.dataset.src = img.getAttribute('src') || '';
    if (!img.dataset.src) return;
    const url = `url("${img.dataset.src}")`;
    img.style.setProperty('--icon-url', url);
    // Маску рисует ::after самой КНОПКИ, поэтому переменная нужна и на ней.
    // Без этого активное состояние оставалось без картинки.
    if (img.parentElement && img.parentElement.tagName === 'BUTTON') {
      img.parentElement.style.setProperty('--icon-url', url);
    }
  });
}

// Меняет файл иконки, сохраняя переменную в согласии с src.
function setIconSrc(img, name) {
  const path = `/static/icons/${name}.svg`;
  const url = `url("${path}")`;
  img.setAttribute('src', path);
  img.dataset.src = path;
  img.style.setProperty('--icon-url', url);
  // Маска живёт на ::after кнопки: без этого она осталась бы от прежней
  // иконки (например, обычный повтор вместо «повтор одного»).
  if (img.parentElement && img.parentElement.tagName === 'BUTTON') {
    img.parentElement.style.setProperty('--icon-url', url);
  }
}

/* ---------- API ---------- */
async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (options.method && options.method !== 'GET') {
    headers['X-CSRF-Token'] = state.csrf;
  }
  // «Failed to fetch» ничего не говорит пользователю и пугает на большой базе,
  // где сервер может отвечать долго. Один повтор для GET и понятный текст ошибки.
  const isGet = !options.method || options.method === 'GET';
  let response;
  try {
    response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
  } catch (error) {
    // Отмена запроса (AbortController) — не ошибка сети, её обрабатывает вызывающий код.
    if (error && error.name === 'AbortError') throw error;
    if (!isGet) throw new Error('Сервер не ответил. Проверьте, что контейнер запущен.');
    await new Promise((resolve) => setTimeout(resolve, 700));
    try {
      response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    } catch (retryError) {
      if (retryError && retryError.name === 'AbortError') throw retryError;
      throw new Error('Сервер не ответил. Возможно, идёт сканирование — попробуйте ещё раз.');
    }
  }
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('unauthorized');
  }
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    // Сервер может вернуть errors по конкретным настройкам — показываем их.
    if (body && body.errors) {
      throw new Error(Object.values(body.errors).join('; '));
    }
    throw new Error((body && (body.detail || body.message)) || `HTTP ${response.status}`);
  }
  // Сервер отвечает конвертом {ok: true, data: ...}: разворачиваем один раз,
  // чтобы вызывающий код работал с полезной нагрузкой напрямую.
  if (body && typeof body === 'object' && 'ok' in body) return body.data;
  return body;
}

// Текст-заглушка на время запроса. Если идёт скан, честно пишем об этом:
// пустое «Загрузка…» на большой библиотеке выглядит как зависание.
function loadingNote(text = 'Загрузка…') {
  const scanning = state.scanRunning;
  return el('p', { class: 'muted' },
    scanning ? `Загрузка / Идёт сканирование… ${state.scanPercent || 0}%` : text);
}

function toast(message, kind = 'info') {
  const box = el('div', { class: `toast ${kind}`, role: 'status' }, message);
  document.body.append(box);
  setTimeout(() => box.remove(), 4000);
}

/* ---------- Обложки ---------- */
const PLACEHOLDER = '/static/placeholder.svg';

/* ---------- Коллаж 2x2 из обложек ---------- */

// Кэш подобранных обложек: {key: {signature, tiles}}.
// Без него картинки перетасовывались бы при каждой перерисовке.
const collageCache = new Map();

function collageTiles(ids, key, count = 4) {
  if (!ids || !ids.length) return [];
  const signature = `${ids.length}:${ids[0]}:${ids[ids.length - 1]}`;
  const cached = collageCache.get(key);
  if (cached && cached.signature === signature) return cached.tiles;

  const pool = ids.slice();
  const tiles = [];
  while (tiles.length < count) {
    // Треков может быть меньше четырёх — тогда берём по кругу,
    // чтобы квадрат всегда был заполнен.
    if (!pool.length) pool.push(...ids);
    tiles.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
  }
  collageCache.set(key, { signature, tiles });
  return tiles;
}

// Плитки коллажа — обложки альбомов треков. Фоновая фаза кеширует обложки альбомов (al-),
// а tr-ключей в кеше обычно нет, поэтому плитки по трекам никогда не загружались.
function coverCollage(tracks, key) {
  const albumIds = [...new Set((tracks || []).map((t) => t.album_id).filter(Boolean))];
  const tiles = collageTiles(albumIds, key);
  if (!tiles.length) {
    return el('div', { class: 'collage' },
      el('img', { class: 'collage-tile', src: PLACEHOLDER, alt: '' }));
  }
  return el('div', { class: 'collage' },
    ...tiles.map((id) => coverImg('al', id, { className: 'collage-tile', alt: '' })));
}

// Обложка одного трека в списке: обложка его альбома (кешируется фоном), иначе заглушка.
function trackCoverNode(track, className) {
  // Обложка трека = обложка его альбома: именно её кеширует фоновая фаза (al-<id>).
  // Ключи tr-<id> в кеше не появляются, поэтому обложки треков раньше не загружались.
  if (track.album_id) return coverImg('al', track.album_id, { className, alt: '' });
  return el('img', { class: className, src: PLACEHOLDER, alt: '' });
}

// Хлебные крошки: путь «Библиотека / Альбомы / Альбом». Последний пункт — не ссылка.
// Первая крошка с иконкой стрелки дублирует привычное «назад».
function breadcrumbs(items) {
  const nodes = [];
  items.forEach((item, index) => {
    if (index > 0) nodes.push(el('span', { class: 'sep' }, '/'));
    if (item.current) {
      nodes.push(el('span', { class: 'current' }, item.label));
      return;
    }
    const isFirst = index === 0;
    nodes.push(el('button', {
      type: 'button',
      class: isFirst ? 'breadcrumb-link crumb-back' : 'breadcrumb-link',
      onclick: () => (item.action ? item.action() : setView(item.view)),
    }, ...(isFirst ? [icon('arrow-left'), item.label] : [item.label])));
  });
  return el('nav', { class: 'breadcrumbs', 'aria-label': 'Путь' }, ...nodes);
}

// Переключение вкладки библиотеки из крошек.
//
// Раньше здесь был просто setView('library'): он перерисовывает главную целиком,
// и список альбомов оказывался ниже приветствия и секций — выглядело как
// «перешли на главную». Прокрутка не помогала. Теперь для альбомов есть
// отдельный вид без главной (renderAlbumsView), остальные вкладки — как раньше.
async function switchLibraryTab(kind) {
  state.libraryView = kind;
  await setView(kind === 'albums' ? 'albums' : 'library');
}

/* ---------- Список альбомов отдельным видом ----------

   Крошка «Альбомы» вела на setView('library'), а это рисует главную целиком:
   приветствие, поиск, «Недавно добавленное». Список оказывался ниже экрана,
   и переход выглядел как возврат на главную. Здесь отдельный вид: крошки,
   переключатель и сетка — больше ничего.
*/
async function renderAlbumsView(view) {
  const switcher = librarySwitcher();
  fill(view,
    breadcrumbs([{ label: 'Библиотека', view: 'library' }, { label: 'Альбомы', current: true }]),
    el('div', { class: 'section-head' }, el('h2', {}, 'Альбомы')),
    switcher,
    el('div', { id: 'library-body' }, loadingNote()),
  );
  await renderAlbumsInto($('#library-body'));
}

// Переключатель вкладок библиотеки. Общий для главной и вида «Альбомы».
function librarySwitcher() {
  return el('div', { class: 'segmented', role: 'tablist' },
    ...['artists', 'albums', 'playlists'].map((kind) => el('button', {
      class: 'segment' + (state.libraryView === kind ? ' active' : ''),
      role: 'tab',
      'aria-selected': state.libraryView === kind ? 'true' : 'false',
      onclick: () => {
        state.libraryView = kind;
        // Список альбомов — отдельным видом; остальные вкладки остаются на главной.
        setView(kind === 'albums' ? 'albums' : 'library');
      },
    }, { artists: 'Исполнители', albums: 'Альбомы', playlists: 'Плейлисты' }[kind])),
  );
}

function coverUrl(kind, id) {
  return `/rest/getCoverArt?id=${kind}-${id}`;
}

function coverImg(kind, id, { className = '', alt = '' } = {}) {
  // onerror подставляет заглушку: у части альбомов картинки просто нет,
  // и браузер иначе рисует битую иконку.
  const node = el('img', {
    class: className,
    src: PLACEHOLDER,
    alt,
    loading: 'lazy',
    decoding: 'async',
    onerror: (event) => {
      const target = event.target;
      if (target.src.endsWith(PLACEHOLDER)) return;
      target.src = PLACEHOLDER;
    },
  });
  // Обложку не запрашиваем сами: карточка ждёт, пока пакетная проверка
  // готовности (coverReady) скажет, что обложка уже лежит в кеше.
  node.dataset.coverKey = `${kind}-${id}`;
  coverReady.track(node);
  return node;
}

/* ---------- Готовность обложек пачкой ----------

   Списки (альбомы, исполнители, плейлисты) рисуются сразу, с заглушками.
   Карточка получает src только тогда, когда сервер сообщил, что обложка уже
   в кеше. Запрос getCoverArt на промах кеша не уходит: извлечение из тегов
   делает фоновая фаза на сервере, а не открытие списка.

   * карточки собираются в пачку и проверяются одним запросом /covers/ready;
   * «ready» — подставляем src, это уже готовое превью из кеша;
   * «absent» — остаёмся на заглушке, повторно не спрашиваем;
   * нет ответа (ключа нет в ответе) — проверим позже, когда фаза дойдёт;
   * пауза, пока идёт скан: сервер в это время занят, опрос не нужен.
*/
const coverReady = (() => {
  const BATCH = 60;          // ключей в одном запросе
  const INTERVAL_MS = 1500;  // как часто проверяем ожидающие карточки
  const nodes = new Map();   // key -> Set(node)
  const absent = new Set();  // ключи, для которых обложки нет (не спрашиваем)
  let timer = null;
  let paused = false;

  function track(node) {
    const key = node.dataset.coverKey;
    if (!key || absent.has(key)) return;
    if (!nodes.has(key)) nodes.set(key, new Set());
    nodes.get(key).add(node);
    schedule();
  }

  function schedule() {
    if (timer || paused || !nodes.size) return;
    timer = setTimeout(run, INTERVAL_MS);
  }

  function apply(key, status) {
    const set = nodes.get(key);
    if (!set) return;
    if (status === 'ready') {
      set.forEach((node) => {
        if (!node.isConnected) return;
        node.src = `/rest/getCoverArt?id=${key}`;
      });
      nodes.delete(key);
    } else if (status === 'absent') {
      absent.add(key);
      nodes.delete(key);
    }
  }

  async function run() {
    timer = null;
    if (paused) return;
    // Убираем из ожидания карточки, которые уже ушли из DOM.
    for (const [key, set] of nodes) {
      for (const node of set) if (!node.isConnected) set.delete(node);
      if (!set.size) nodes.delete(key);
    }
    const keys = [...nodes.keys()].slice(0, BATCH);
    if (keys.length) {
      try {
        const data = await api(`/api/web/covers/ready?keys=${encodeURIComponent(keys.join(','))}`);
        const ready = data.ready || {};
        keys.forEach((key) => { if (ready[key]) apply(key, ready[key]); });
      } catch (error) {
        // Сеть или сервер занят: попробуем на следующем тике.
      }
    }
    schedule();
  }

  function pause() { paused = true; if (timer) { clearTimeout(timer); timer = null; } }
  function resume() { if (!paused) return; paused = false; schedule(); }

  return { track, pause, resume, pending: () => nodes.size };
})();

/* ---------- Навигация ---------- */
function setView(name) {
  state.view = name;
  // Активный пункт — в левом рейле. Разделы настроек подсвечивают
  // шестерёнку: иначе непонятно, что ты «внутри» меню.
  const inRail = ['library', 'playlists', 'starred', 'recent'].includes(name);
  document.querySelectorAll('.rail-btn').forEach((btn) => {
    const view = btn.dataset.view;
    const active = view === name || (view === 'library' && inRail === false &&
                                     btn.id === 'menu-toggle');
    btn.classList.toggle('active', Boolean(view) && view === name);
  });
  const gear = document.querySelector('#menu-toggle');
  if (gear) gear.classList.toggle('active', !inRail);
  closeMenu();
  // Панель статуса скана опрашивает сервер раз в секунду. При уходе с
  // главной опрос нужно снять, иначе таймер живёт до перезагрузки страницы.
  // scan_import.js грузится после app.js, поэтому проверяем наличие функции.
  if (typeof stopHomeScan === 'function') stopHomeScan();
  const view = $('#view');
  view.replaceChildren(loadingNote());
  const loaders = {
    library: renderLibrary,
    albums: renderAlbumsView,
    playlists: renderPlaylists,
    starred: renderStarred,
    recent: renderRecent,
    settings: renderSettings,
    users: renderUsers,
    scan: renderScan,
    import: renderImport,
    server: renderServer,
    account: renderAccount,
  };
  // Возвращаем промис: иначе нельзя дождаться отрисовки ни в тестах,
  // ни в вызывающем коде.
  return Promise.resolve()
    .then(() => (loaders[name] || renderLibrary)(view))
    .then(() => { hydrateIconVars(); })
    .catch((error) => {
      if (error.message !== 'unauthorized') {
        view.replaceChildren(el('div', { class: 'error' }, error.message));
      }
    });
}

function openMenu() {
  const menu = $('#menu');
  const toggle = $('#menu-toggle');
  menu.hidden = false;
  toggle.setAttribute('aria-expanded', 'true');
}

function closeMenu() {
  const menu = $('#menu');
  const toggle = $('#menu-toggle');
  if (!menu || !toggle) return;
  menu.hidden = true;
  toggle.setAttribute('aria-expanded', 'false');
}

/* ---------- Библиотека ---------- */
async function renderLibrary(view) {
  // Главная открывается сразу, даже во время скана: уже отсканированные альбомы
  // видны и доступны. Панель прогресса (скан и обложки) — сверху, см. mountHomeActivity.

  const trackList = el('div', { class: 'list' });

  // Поиск с отменой предыдущего запроса.
  //
  // Сервер больше не тормозит (агрегаты считаются один раз), но запросов всё равно
  // много: по одному на паузу в наборе. Без отмены они копились в очереди, и сервер
  // выглядел зависшим. Теперь каждый новый ввод отменяет предыдущий.
  let searchController = null;
  let searchSeq = 0;

  const renderSearchResults = (results) => {
    const tracks = results.tracks || [];
    const albums = results.albums || [];
    const artists = results.artists || [];
    if (!tracks.length && !albums.length && !artists.length) {
      trackList.replaceChildren(el('p', { class: 'muted' }, 'Ничего не найдено'));
      return;
    }
    const blocks = [];
    if (tracks.length) {
      blocks.push(
        el('div', { class: 'row' },
          el('span', { class: 'muted small' }, `Треков: ${tracks.length}`),
          el('button', {
            class: 'ghost small',
            onclick: () => { enqueue(tracks); playIndex(0); },
          }, 'Играть все'),
        ),
        ...tracks.map((track, i) => trackRow(track, { queue: tracks, index: i + 1 })),
      );
    }
    if (albums.length) {
      blocks.push(
        el('h3', {}, `Альбомы (${albums.length})`),
        el('div', { class: 'album-grid' },
          ...albums.map((album) => el('button',
            { class: 'album', onclick: () => openAlbum(album.id) },
            coverImg('al', album.id, { className: 'album-cover' }),
            el('span', { class: 'album-name' }, album.name),
            el('span', { class: 'muted small' }, album.artist || ''),
          )),
        ),
      );
    }
    if (artists.length) {
      blocks.push(
        el('h3', {}, `Исполнители (${artists.length})`),
        el('div', { class: 'row wrap' },
          ...artists.map((artist) => el('button', {
            class: 'ghost small',
            onclick: () => openArtist(artist.id),
          }, artist.name)),
        ),
      );
    }
    trackList.replaceChildren(...blocks);
  };

  const search = el('input', {
    type: 'search',
    placeholder: 'Поиск по трекам, альбомам, исполнителям…',
    class: 'search',
    'aria-label': 'Поиск',
    oninput: debounce(async (event) => {
      // Поле могли перерисовать: сверяемся с текущим значением, а не с event.
      const query = event.target.value.trim();
      if (searchController) {
        searchController.abort();
        searchController = null;
      }
      if (!query) {
        trackList.replaceChildren();
        return;
      }
      const seq = ++searchSeq;
      const controller = new AbortController();
      searchController = controller;
      trackList.replaceChildren(el('p', { class: 'muted' }, 'Поиск…'));
      try {
        const results = await api(
          `/api/web/search?q=${encodeURIComponent(query)}`,
          { signal: controller.signal },
        );
        if (seq !== searchSeq) return;   // ответ устарел: пришёл после нового ввода
        renderSearchResults(results);
      } catch (error) {
        if (error && error.name === 'AbortError') return;
        if (seq !== searchSeq) return;
        trackList.replaceChildren(el('p', { class: 'error' }, error.message));
      }
    }, 300),
  });

  const summary = el('div', { class: 'cards' });
  const switcher = librarySwitcher();

  // Каркас рисуем сразу. Раньше экран ждал /stats и только потом появлялся —
  // на большой библиотеке это и выглядело как зависшее «Загрузка…».
  view.replaceChildren(
    el('h1', { class: 'greeting' }, `Привет, ${state.user}!`),
    // Кнопки сканирования живут в разделе «Сканирование»: на главной их больше нет.
    el('div', { class: 'row' }, search),
    homeActivityBox(),
    trackList,
    summary,
    el('div', { id: 'home-sections' }),
    el('div', { class: 'section-head' }, el('h2', {}, 'Библиотека')),
    switcher,
    el('div', { id: 'library-body' }, loadingNote()),
  );

  // Дальше три независимых запроса: каждый заполняет свою часть по готовности.
  // Медленный из них больше не задерживает остальные.
  const body = $('#library-body');

  loadStats(summary).catch(() => {});
  renderHomeSections($('#home-sections')).catch(() => {});

  if (state.libraryView === 'artists') await renderArtistsInto(body);
  else if (state.libraryView === 'playlists') await renderPlaylistsInto(body);
  else await renderAlbumsInto(body);

  // Панель активности: если сервер занят, показываем прогресс и опрашиваем его.
  mountHomeActivity().catch(() => {});
}

// Счётчики под приветствием: отдельный запрос, чтобы не блокировать сетку.
async function loadStats(node) {
  if (!node) return;
  const stats = await api('/api/web/stats');
  fill(node,
    card('Треков', stats.tracks ?? 0),
    card('Альбомов', stats.albums ?? 0),
    card('Исполнителей', stats.artists ?? 0),
  );
}

// Аватар исполнителя: обложка его альбома, а если её нет — инициалы
// на акцентном фоне. Обложек у исполнителей в базе нет, поэтому берём
// первую доступную от альбома (coverArt отдаёт её же по `al-<id>`).
function artistAvatar(artist) {
  const node = el('span', { class: 'artist-avatar' });
  const initials = (artist.name || '?')
    .split(/\s+/).filter(Boolean).slice(0, 2)
    .map((part) => part[0].toUpperCase()).join('');

  if (!artist.album_id) {
    node.classList.add('initials');
    node.textContent = initials;
    return node;
  }

  const img = el('img', {
    src: coverUrl('al', artist.album_id),
    alt: '', loading: 'lazy',
    style: 'width:100%;height:100%;object-fit:cover;display:block',
  });
  img.addEventListener('error', () => {
    // Обложки нет — подменяем инициалами, а не оставляем битую иконку.
    node.textContent = initials;
    node.classList.add('initials');
    img.remove();
  });
  node.append(img);
  return node;
}

// Сколько карточек тянуть за раз. 9289 альбомов одним запросом — это и
// мегабайты JSON, и столько же узлов DOM; пользователю нужны первые экраны.
const PAGE_SIZE = 60;

async function renderAlbumsInto(node) {
  fill(node, loadingNote());

  let offset = 0;
  let total = 0;
  const grid = el('div', { class: 'album-grid' });
  const footer = el('div', { class: 'row pagination' });

  const more = el('button', {
    class: 'ghost',
    onclick: () => loadPage(),
  }, 'Показать ещё');

  async function loadPage() {
    more.disabled = true;
    let data;
    try {
      data = await api(`/api/web/albums?limit=${PAGE_SIZE}&offset=${offset}`);
    } catch (error) {
      more.disabled = false;
      toast(error.message, 'error');
      return;
    }
    const items = data.items || [];
    total = data.total ?? items.length;
    offset += items.length;

    if (!items.length && offset === 0) {
      fill(node, el('p', { class: 'muted' },
        'Библиотека пуста — запустите сканирование'));
      return;
    }

    grid.append(...items.map((album) => el('button',
      { class: 'album', onclick: () => openAlbum(album.id) },
      coverImg('al', album.id, { className: 'album-cover' }),
      el('span', { class: 'album-name' }, album.name),
      el('span', { class: 'muted small ellipsis' }, album.artist || ''),
    )));

    const shown = grid.childElementCount;
    fill(footer,
      el('span', { class: 'muted small' },
        `Показано ${shown} из ${total}`),
      shown < total ? more : null,
    );
    more.disabled = false;
  }

  await loadPage();
  fill(node, grid, footer);
}

async function renderArtistsInto(node) {
  fill(node, loadingNote());

  let offset = 0;
  let total = 0;
  const grid = el('div', { class: 'artist-grid' });
  const footer = el('div', { class: 'row pagination' });

  const more = el('button', {
    class: 'ghost',
    onclick: () => loadPage(),
  }, 'Показать ещё');

  async function loadPage() {
    more.disabled = true;
    let data;
    try {
      data = await api(`/api/web/artists?limit=${PAGE_SIZE}&offset=${offset}`);
    } catch (error) {
      more.disabled = false;
      toast(error.message, 'error');
      return;
    }
    const items = data.items || [];
    total = data.total ?? items.length;
    offset += items.length;

    if (!items.length && offset === 0) {
      fill(node, el('p', { class: 'muted' }, 'Исполнителей нет'));
      return;
    }

    grid.append(...items.map((artist) => el('button', {
      class: 'artist',
      onclick: () => openArtist(artist.id),
      title: artist.name,
    },
      artistAvatar(artist),
      el('span', { class: 'artist-name' }, artist.name),
      // Исполнитель без альбома (только треки, например гость или одиночный трек):
      // подпись «N тр.» вместо «0 альб.», чтобы не выглядело как пустая плитка.
      el('span', { class: 'artist-meta' },
        artist.album_count ? `${artist.album_count} альб.` : `${artist.track_count ?? 0} тр.`),
    )));

    const shown = grid.childElementCount;
    fill(footer,
      el('span', { class: 'muted small' }, `Показано ${shown} из ${total}`),
      shown < total ? more : null,
    );
    more.disabled = false;
  }

  await loadPage();
  fill(node, grid, footer);
}

async function openArtist(artistId) {
  const artist = await api(`/api/web/artists/${artistId}`);
  const albums = artist.albums || [];
  $('#view').replaceChildren(
    breadcrumbs([
      { label: 'Библиотека', view: 'library' },
      { label: 'Исполнители', action: () => switchLibraryTab('artists') },
      { label: artist.name, current: true },
    ]),
    el('h2', {}, artist.name),
    el('p', { class: 'muted' }, `${albums.length} альбомов`),
    el('div', { class: 'album-grid' },
      ...albums.map((album) => el('button',
        { class: 'album', onclick: () => openAlbum(album.id) },
        coverImg('al', album.id, { className: 'album-cover' }),
        el('span', { class: 'album-name' }, album.name),
        el('span', { class: 'muted small' }, album.year ? String(album.year) : ''),
      )),
    ),
  );
}

async function openAlbum(albumId) {
  const album = await api(`/api/web/albums/${albumId}`);
  const tracks = album.tracks || [];
  const queue = tracks;
  const total = tracks.reduce((sum, t) => sum + (t.duration || 0), 0);

  // Две равные колонки: слева обложка, справа название, метаданные и кнопки.
  const hero = el('div', { class: 'album-hero' },
    el('div', { class: 'album-hero-left' },
      coverImg('al', album.id, { className: 'album-hero-cover', alt: album.name }),
    ),
    el('div', { class: 'album-hero-info' },
      el('h1', { class: 'album-hero-title' }, album.name),
      el('p', { class: 'album-hero-artist' },
        album.artist_id
          ? el('a', {
              class: 'artist-plain-link',
              href: '#',
              title: 'Все альбомы исполнителя',
              onclick: (event) => { event.preventDefault(); openArtist(album.artist_id); },
            }, album.artist || '')
          : (album.artist || '')),
      el('p', { class: 'muted small' },
        [album.year || null, `${tracks.length} треков`, formatTime(total)]
          .filter(Boolean).join(' · ')),
      el('div', { class: 'row wrap' },
        tracks.length
          ? el('button', {
              class: 'primary',
              onclick: () => { enqueue(queue); playIndex(0); },
            }, icon('play'), 'Играть альбом')
          : null,
        tracks.length
          ? el('button', {
              class: 'ghost',
              onclick: () => {
                enqueue(queue, { replace: false });
                toast('Добавлено в очередь', 'ok');
              },
            }, 'В очередь')
          : null,
      ),
    ),
  );

  fill($('#view'),
    breadcrumbs([
      { label: 'Библиотека', view: 'library' },
      { label: 'Альбомы', action: () => switchLibraryTab('albums') },
      { label: album.name, current: true },
    ]),
    hero,
    el('div', { class: 'list' },
      ...tracks.map((track, i) => trackRow(track, { queue, index: i + 1 }))),
  );
}

/* ---------- Главная: верхние секции ---------- */


/* ---------- Сворачиваемые секции главной ---------- */

// Состояние храним в localStorage: пользователь свернул один раз —
// так и должно остаться после перезагрузки.
const COLLAPSED_KEY = 'mtr.collapsedSections';

function collapsedSet() {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY);
    return new Set(raw ? JSON.parse(raw) : ['recentlyAdded', 'recentlyPlayed']);
  } catch (error) {
    // Приватный режим или мусор в хранилище: по умолчанию всё свёрнуто.
    return new Set(['recentlyAdded', 'recentlyPlayed']);
  }
}

function saveCollapsed(set) {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]));
  } catch (error) { /* хранилище недоступно — не критично */ }
}

// Секция-аккордеон: заголовок-кнопка со стрелкой и тело, которое
// показывается только в развёрнутом состоянии.
function accordionSection({ id, title, action = null, body }) {
  const collapsed = collapsedSet();
  const isCollapsed = collapsed.has(id);

  const chevron = el('img', {
    class: 'icon chevron',
    src: '/static/icons/chevron-down.svg',
    alt: '', 'aria-hidden': 'true',
  });
  const head = el('button', {
    class: 'section-toggle',
    'aria-expanded': String(!isCollapsed),
    'aria-controls': `sec-${id}`,
    onclick: () => {
      const set = collapsedSet();
      if (set.has(id)) set.delete(id); else set.add(id);
      saveCollapsed(set);
      const nowCollapsed = set.has(id);
      panel.hidden = nowCollapsed;
      head.setAttribute('aria-expanded', String(!nowCollapsed));
      head.classList.toggle('collapsed', nowCollapsed);
    },
  },
    chevron,
    el('h2', {}, title),
  );
  head.classList.toggle('collapsed', isCollapsed);

  const panel = el('div', { class: 'section-body', id: `sec-${id}` }, body);
  panel.hidden = isCollapsed;

  return el('section', { class: 'section' },
    el('div', { class: 'section-head' }, head, action),
    panel,
  );
}

async function renderHomeSections(node) {
  if (!node) return;

  // Две независимые секции: каждая появляется, как только пришли её данные.
  // Раньше они ждали друг друга через Promise.all, и медленная история
  // прослушиваний задерживала блок «Недавно добавленные».
  const addedBox = el('div', {});
  const playedBox = el('div', {});
  fill(node, addedBox, playedBox);

  api('/api/web/recently-added?limit=12')
    .then((added) => renderRecentlyAdded(addedBox, added.albums || []))
    .catch(() => fill(addedBox));

  api('/api/web/recently-played?limit=6')
    .then((recent) => renderRecentlyPlayed(playedBox, recent.items || []))
    .catch(() => fill(playedBox));
}

function renderRecentlyAdded(node, albums) {
  const items = albums.slice(0, 8);
  if (!items.length) { fill(node); return; }
  fill(node, accordionSection({
    id: 'recentlyAdded',
    title: 'Недавно добавленные',
    body: el('div', { class: 'album-grid' },
      ...items.map((album) => el('button', {
        class: 'album', onclick: () => openAlbum(album.id),
      },
        coverImg('al', album.id, { className: 'album-cover' }),
        el('span', { class: 'album-name' }, album.name),
        el('span', { class: 'album-artist ellipsis' }, album.artist || ''),
      )),
    ),
  }));
}

function renderRecentlyPlayed(node, played) {
  if (!played.length) { fill(node); return; }
  fill(node, accordionSection({
    id: 'recentlyPlayed',
    title: 'Недавно прослушанные',
    action: el('button', {
      class: 'ghost small', onclick: () => setView('recent'),
    }, 'Все'),
    body: el('div', { class: 'list' },
      ...played.map((track, i) => trackRow(track, { queue: played, index: i + 1 })),
    ),
  }));
}

// Полная страница «Недавно прослушанные» из рейла.
async function renderRecent(view) {
  const data = await api('/api/web/recently-played?limit=100');
  const queue = data.items || [];
  if (!queue.length) {
    fill(view,
      el('h2', {}, 'Недавно прослушанные'),
      el('div', { class: 'empty-state' },
        el('p', { class: 'muted' }, 'Вы ещё ничего не слушали.'),
        el('p', { class: 'muted small' },
          'История появляется, когда играет веб-плеер или Subsonic-клиент.'),
      ),
    );
    return;
  }
  fill(view,
    el('h2', {}, 'Недавно прослушанные'),
    el('p', { class: 'muted small' }, `${queue.length} треков`),
    el('div', { class: 'row' },
      el('button', {
        class: 'primary', onclick: () => playTrack(queue[0], queue),
      }, icon('play'), 'Играть всё'),
    ),
    el('div', { class: 'list' },
      ...queue.map((track, i) => trackRow(track, { queue, index: i + 1 })),
    ),
  );
}

/* ---------- Строка трека ---------- */
function trackRow(track, context = {}) {
  const isCurrent = state.queue[state.queueIndex]?.id === track.id;
  const starred = state.starred.has(track.id);

  const heart = iconButton(starred ? 'heart-filled' : 'heart', {
    cls: 'ghost icon-btn heart' + (starred ? ' on' : ''),
    title: starred ? 'Убрать из избранного' : 'В избранное',
    onClick: async (event) => {
      event.stopPropagation();
      const next = !state.starred.has(track.id);
      try {
        await api(`/api/web/tracks/${track.id}/star`, {
          method: 'POST', body: JSON.stringify({ starred: next }),
        });
      } catch (error) { toast(error.message, 'error'); return; }
      if (next) state.starred.add(track.id); else state.starred.delete(track.id);
      const img = heart.querySelector('img');
      if (img) setIconSrc(img, next ? 'heart-filled' : 'heart');
      heart.classList.toggle('on', next);
      const label = next ? 'Убрать из избранного' : 'В избранное';
      heart.title = label;
      heart.setAttribute('aria-label', label);
    },
  });

  const actions = [heart];
  if (context.onRemove) {
    actions.push(iconButton('remove', {
      cls: 'ghost icon-btn danger',
      title: 'Убрать из плейлиста',
      onClick: (event) => { event.stopPropagation(); context.onRemove(track); },
    }));
  } else {
    actions.push(playlistMenuButton(track));
  }

  return el('div', {
    class: `row track${isCurrent ? ' playing' : ''}`,
    dataset: { trackId: String(track.id) },
    onclick: (event) => {
      if (event.target.closest('a, button')) return;
      playTrack(track, context.queue);
    },
  },
    // Номер — позиция в текущем списке, а не номер альбома: в плейлисте
    // и избранном нумерация альбома сбивала бы с толку.
    el('span', { class: 'track-index' }, context.index ? String(context.index) : ''),
    trackCoverNode(track, 'track-cover'),
    el('div', { class: 'track-main' },
      el('strong', { class: 'ellipsis' }, track.title),
      el('span', { class: 'muted small ellipsis' },
        `${track.artist || ''}${track.album ? ' — ' + track.album : ''}`),
    ),
    el('span', { class: 'muted mono small hide-sm' }, formatTime(track.duration)),
    ...actions,
    el('a', {
      class: 'ghost icon-btn hide-sm',
      href: `/rest/download?id=${track.id}`,
      title: 'Скачать', 'aria-label': 'Скачать',
      onclick: (event) => event.stopPropagation(),
    }, icon('download')),
  );
}

/* ---------- Выпадающий список плейлистов ---------- */

// Кнопка «добавить в плейлист» с выпадающим списком. Плейлистов может быть
// много, поэтому в списке есть поиск и прокрутка: prompt на сотню пунктов
// бесполезен, а ввод позволяет отфильтровать по имени.
function playlistMenuButton(track) {
  const wrapper = el('div', { class: 'menu-anchor' });
  const button = iconButton('plus', {
    cls: 'ghost icon-btn',
    title: 'Добавить в плейлист',
    onClick: (event) => {
      event.stopPropagation();
      togglePlaylistMenu(wrapper, track);
    },
  });
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  wrapper.append(button);
  return wrapper;
}

function closePlaylistMenus() {
  document.querySelectorAll('.menu-anchor.open').forEach((node) => {
    node.classList.remove('open');
    const pop = node.querySelector('.playlist-pop');
    if (pop) pop.remove();
    const trigger = node.querySelector('button');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
  });
}

async function togglePlaylistMenu(wrapper, track) {
  const wasOpen = wrapper.classList.contains('open');
  closePlaylistMenus();
  if (wasOpen) return;

  wrapper.classList.add('open');
  const trigger = wrapper.querySelector('button');
  if (trigger) trigger.setAttribute('aria-expanded', 'true');

  const pop = el('div', { class: 'playlist-pop', role: 'menu' });
  const search = el('input', {
    type: 'search', class: 'playlist-pop-search',
    placeholder: 'Поиск плейлиста…', 'aria-label': 'Поиск плейлиста',
  });
  const list = el('div', { class: 'playlist-pop-list' });
  pop.append(search, list);
  wrapper.append(pop);
  search.focus();

  let items = [];
  try {
    const data = await api('/api/web/playlists');
    items = (data.items || []).filter((playlist) => !playlist.is_smart);
  } catch (error) {
    list.replaceChildren(el('p', { class: 'muted small' }, error.message));
    return;
  }

  if (!items.length) {
    list.replaceChildren(el('p', { class: 'muted small' },
      'Нет плейлистов. Создайте во вкладке «Плейлисты».'));
    return;
  }

  const render = (query) => {
    const text = query.trim().toLowerCase();
    const filtered = text
      ? items.filter((playlist) => playlist.name.toLowerCase().includes(text))
      : items;
    if (!filtered.length) {
      list.replaceChildren(el('p', { class: 'muted small' }, 'Не найдено'));
      return;
    }
    list.replaceChildren(...filtered.map((playlist) => el('button', {
      class: 'playlist-pop-item', role: 'menuitem',
      onclick: async (event) => {
        event.stopPropagation();
        try {
          await api(`/api/web/playlists/${playlist.id}/tracks`, {
            method: 'POST', body: JSON.stringify({ track_ids: [track.id] }),
          });
          toast(`Добавлено в «${playlist.name}»`, 'ok');
        } catch (error) {
          toast(error.message, 'error');
        }
        closePlaylistMenus();
      },
    },
      el('span', { class: 'ellipsis' }, playlist.name),
      el('span', { class: 'muted small' }, String(playlist.song_count ?? '')),
    )));
  };

  render('');
  search.addEventListener('input', () => render(search.value));
  // Клик вне меню закрывает его, как обычный выпадающий список.
  setTimeout(() => {
    const away = (event) => {
      if (!wrapper.contains(event.target)) {
        closePlaylistMenus();
        document.removeEventListener('click', away);
      }
    };
    document.addEventListener('click', away);
  }, 0);
}


/* ---------- Плейлисты: две колонки ---------- */
let selectedPlaylistId = null;

async function renderPlaylists(view) {
  const data = await api('/api/web/playlists');
  const items = data.items || [];
  if (!items.length) selectedPlaylistId = null;
  else if (!items.some((p) => p.id === selectedPlaylistId)) {
    selectedPlaylistId = items[0].id;
  }

  const nameInput = el('input', {
    placeholder: 'Название нового плейлиста', 'aria-label': 'Название нового плейлиста',
  });
  const createForm = el('form', {
    class: 'row',
    onsubmit: async (event) => {
      event.preventDefault();
      const name = nameInput.value.trim();
      if (!name) return;
      const created = await api('/api/web/playlists', {
        method: 'POST', body: JSON.stringify({ name }),
      });
      selectedPlaylistId = created.id;
      toast('Плейлист создан', 'ok');
      setView('playlists');
    },
  }, nameInput, el('button', { type: 'submit', class: 'primary' }, 'Создать'));

  const sidebar = el('div', { class: 'pl-list', id: 'pl-list' },
    ...items.map((playlist) => el('button', {
      class: 'pl-item' + (playlist.id === selectedPlaylistId ? ' active' : ''),
      onclick: () => {
        selectedPlaylistId = playlist.id;
        setView('playlists');
      },
    },
      el('span', { class: 'ellipsis' }, playlist.name),
      el('span', { class: 'pl-meta' },
        el('span', { class: 'muted tiny' }, String(playlist.song_count)),
        // Публичный помечаем земным шаром: словом «публ.» список пестрел
        // сокращениями и читался хуже.
        playlist.is_public
          ? el('img', {
              class: 'icon globe', src: '/static/icons/globe.svg',
              alt: 'Публичный', title: 'Публичный',
            })
          : null,
        playlist.is_smart
          ? el('span', { class: 'muted tiny' }, 'смарт')
          : null,
      ),
    )),
  );

  const detail = el('div', { class: 'pl-detail', id: 'pl-detail' },
    // Пустое состояние: без него после удаления остаётся «Выберите плейлист»
    // при нулевом списке — выглядит как зависший интерфейс.
    items.length
      ? el('p', { class: 'muted' }, 'Выберите плейлист')
      : el('div', { class: 'empty-state' },
          el('p', { class: 'muted' }, 'Плейлистов пока нет.'),
          el('p', { class: 'muted small' },
            'Создайте плейлист выше или импортируйте из папки с .m3u/.m3u8.'),
        ),
  );

  const bulkActions = el('div', { class: 'row wrap' + (items.length ? '' : ' hidden') },
    el('button', {
      class: 'ghost danger',
      onclick: async () => {
        const ok = await confirmDialog(
          `Будут удалены ВСЕ ваши плейлисты (${items.length}). ` +
          'Сами треки в библиотеке останутся.',
          { title: 'Удалить все плейлисты?', okLabel: 'Удалить все' });
        if (!ok) return;
        try {
          const result = await api('/api/web/playlists/purge', {
            method: 'POST', body: JSON.stringify({ scope: 'mine' }),
          });
          selectedPlaylistId = null;
          toast(`Удалено плейлистов: ${result.deleted}`, 'ok');
          setView('playlists');
        } catch (error) {
          toast(error.message, 'error');
        }
      },
    }, 'Удалить все'),
    el('button', {
      class: 'ghost',
      onclick: async () => {
        const empties = items.filter((p) => p.song_count === 0).length;
        if (!empties) { toast('Пустых плейлистов нет', 'ok'); return; }
        const ok = await confirmDialog(
          `Будет удалено пустых плейлистов: ${empties}. ` +
          'Обычно это следы неудачного импорта.',
          { title: 'Удалить пустые?', okLabel: 'Удалить пустые' });
        if (!ok) return;
        try {
          const result = await api('/api/web/playlists/purge', {
            method: 'POST', body: JSON.stringify({ scope: 'empty' }),
          });
          toast(`Удалено пустых: ${result.deleted}`, 'ok');
          setView('playlists');
        } catch (error) {
          toast(error.message, 'error');
        }
      },
    }, 'Удалить пустые'),
  );

  const totalTracks = items.reduce((sum, p) => sum + (p.song_count || 0), 0);
  const pageHead = el('div', { class: 'pl-page-head' },
    el('div', { class: 'pl-page-left' },
      el('h2', { class: 'pl-page-title' }, 'Плейлисты'),
      el('span', { class: 'pl-page-sub' },
        `${items.length} плейлистов · ${totalTracks} треков`),

    ),
    el('div', { class: 'pl-create' }, createForm),
  );

  view.replaceChildren(
    pageHead,
    // Три колонки: список (15%), коллаж обложек (45%), содержимое (40%).
    el('div', { class: 'pl-layout' },
      // Левая колонка: коллаж выбранного плейлиста, затем список и
      // массовые действия под ним.
      el('div', { class: 'pl-col-list' },
        el('div', { class: 'pl-col-art', id: 'pl-art' }),
        sidebar,
        el('div', { class: 'pl-list-actions' }, bulkActions),
      ),
      el('div', { class: 'pl-col-detail' }, detail),
    ),
  );

  if (selectedPlaylistId !== null) {
    const playlist = await renderPlaylistDetailInto(detail, selectedPlaylistId);
    const art = $('#pl-art');
    if (playlist && art) {
      fill(art, coverCollage(playlist.tracks || [], `pl-${selectedPlaylistId}`));
    }
  }
}

async function renderPlaylistsInto(node) {
  // Тот же список, но компактно, внутри «Библиотеки».
  const data = await api('/api/web/playlists');
  const items = data.items || [];
  if (!items.length) {
    node.replaceChildren(el('p', { class: 'muted' }, 'Плейлистов пока нет'));
    return;
  }
  node.replaceChildren(el('div', { class: 'pl-list' },
    ...items.map((playlist) => el('button', {
      class: 'pl-item',
      onclick: () => { selectedPlaylistId = playlist.id; setView('playlists'); },
    },
      el('span', { class: 'ellipsis' }, playlist.name),
      el('span', { class: 'muted tiny' }, `${playlist.song_count} треков`),
    )),
  ));
}

async function renderPlaylistDetailInto(node, playlistId) {
  const playlist = await api(`/api/web/playlists/${playlistId}`);
  const queue = playlist.tracks || [];

  const actions = [];
  if (queue.length) {
    actions.push(el('button', {
      class: 'primary', onclick: () => playTrack(queue[0], queue),
    }, icon('play'), 'Играть всё'));
  }
  if (playlist.can_edit) {
    actions.push(el('button', {
      class: 'ghost',
      onclick: async () => {
        const name = await promptDialog('Новое название плейлиста',
                                        playlist.name, { title: 'Переименовать' });
        if (!name) return;
        await api(`/api/web/playlists/${playlistId}/rename`, {
          method: 'POST', body: JSON.stringify({ name }),
        });
        setView('playlists');
      },
    }, 'Переименовать'));
    actions.push(el('button', {
      class: 'ghost',
      onclick: async () => {
        await api(`/api/web/playlists/${playlistId}/public`, {
          method: 'POST', body: JSON.stringify({ is_public: !playlist.is_public }),
        });
        setView('playlists');
      },
    }, playlist.is_public ? 'Скрыть' : 'Опубликовать'));
    actions.push(el('button', {
      class: 'ghost danger',
      onclick: async () => {
        const ok = await confirmDialog(
          `Плейлист «${playlist.name}» и его состав будут удалены безвозвратно.`,
          { title: 'Удалить плейлист?' });
        if (!ok) return;
        await api(`/api/web/playlists/${playlistId}`, { method: 'DELETE' });
        // selectedPlaylistId сбрасываем: иначе рендер попытается открыть
        // удалённый плейлист, получит 404 и покажет ошибку вместо списка.
        selectedPlaylistId = null;
        toast('Плейлист удалён', 'ok');
        setView('playlists');
      },
    }, 'Удалить'));
  }

  node.replaceChildren(
    el('h1', { class: 'album-hero-title pl-hero-title' }, playlist.name),
    el('p', { class: 'muted small' },
      `${queue.length} треков${playlist.is_smart ? ' · умный' : ''}` +
      `${playlist.is_public ? ' · публичный' : ''}`),
    el('div', { class: 'row wrap' }, ...actions),
    queue.length
      ? el('div', { class: 'list' }, ...queue.map((track, i) => trackRow(track, {
          queue,
          index: i + 1,
          onRemove: playlist.can_edit
            ? async (item) => {
                await api(`/api/web/playlists/${playlistId}/tracks/${item.id}`,
                          { method: 'DELETE' });
                toast('Убрано из плейлиста', 'ok');
                renderPlaylistDetailInto(node, playlistId);
              }
            : null,
        })))
      : el('p', { class: 'muted' }, 'В плейлисте пока нет треков'),
  );

  // Возвращаем данные: вызывающему нужен состав для коллажа обложек.
  return playlist;
}

async function renderStarred(view) {
  const data = await api('/api/web/starred');
  const queue = data.items || [];
  state.starred = new Set(queue.map((track) => track.id));
  const total = queue.reduce((sum, t) => sum + (t.duration || 0), 0);

  // Как страница альбома: слева «обложка» — коллаж из четырёх случайных
  // обложек избранного, справа название и кнопки. Треки — ниже.
  const hero = el('div', { class: 'album-hero' },
    el('div', { class: 'album-hero-left' },
      coverCollage(queue, 'starred'),
    ),
    el('div', { class: 'album-hero-info' },
      el('h1', { class: 'album-hero-title' }, 'Избранное'),
      el('p', { class: 'album-hero-artist' },
        `${queue.length} треков · ${formatTime(total)}`),
      el('div', { class: 'row wrap' },
        queue.length
          ? el('button', {
              class: 'primary',
              onclick: () => playTrack(queue[0], queue),
            }, icon('play'), 'Играть всё')
          : null,
      ),
    ),
  );

  fill(view,
    el('h2', {}, 'Избранное'),
    hero,
    queue.length
      ? el('div', { class: 'list' },
          ...queue.map((track, i) => trackRow(track, { queue, index: i + 1 })))
      : el('div', { class: 'empty-state' },
          el('p', { class: 'muted' }, 'В избранном пока пусто.'),
          el('p', { class: 'muted small' },
            'Нажмите на сердце рядом с треком, чтобы добавить его сюда.'),
        ),
  );
}
/* ---------- Настройки ---------- */
async function renderSettings(view) {
  const data = await api('/api/web/settings');
  const items = data.items || [];
  const isAdmin = state.role === 'admin';

  const rows = items.map((item) => {
    let control;
    if (item.kind === 'bool') {
      control = el('input', { type: 'checkbox', checked: item.value ? 'checked' : null });
      control.dataset.key = item.key;
    } else if (item.kind === 'choice') {
      control = el('select', {},
        ...item.choices.map((choice) => el('option',
          { value: choice, selected: choice === item.value ? 'selected' : null },
          choiceLabel(choice))),
      );
      control.dataset.key = item.key;
    } else {
      control = el('input', {
        type: 'number', value: String(item.value),
        min: item.minimum ?? null, max: item.maximum ?? null,
      });
      control.dataset.key = item.key;
    }
    control.disabled = !isAdmin;

    return el('div', { class: 'setting' },
      el('div', { class: 'setting-text' },
        el('strong', {}, item.label),
        el('span', { class: 'muted small' }, item.help),
        ...safeChildren(
          item.overridden ? el('span', { class: 'badge' }, 'изменено') : null,
          // Такие ключи читаются при старте: обещать мгновенный эффект нельзя.
          item.restart ? el('span', { class: 'badge' }, 'после перезапуска') : null,
        ),
      ),
      el('div', { class: 'setting-control' }, control),
    );
  });

  const saveButton = isAdmin ? el('button', {
    class: 'primary',
    onclick: async (event) => {
      const button = event.target;
      const values = {};
      view.querySelectorAll('[data-key]').forEach((control) => {
        const key = control.dataset.key;
        values[key] = control.type === 'checkbox' ? control.checked : control.value;
      });
      button.disabled = true;
      try {
        await api('/api/web/settings', {
          method: 'POST', body: JSON.stringify({ values }),
        });
        toast('Настройки сохранены', 'ok');
        // Вид библиотеки применяем сразу, без перезагрузки страницы.
        if (values.library_view) state.libraryView = values.library_view;
        setView('settings');
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    },
  }, 'Сохранить') : null;

  // Параметры запуска из docker-compose.yml: только чтение, видны здесь для проверки.
  const readonly = (data.readonly || []).map((item) => el('div', { class: 'setting' },
    el('div', { class: 'setting-text' },
      el('strong', {}, item.label),
      el('span', { class: 'muted small' }, item.help),
      el('span', { class: 'badge' }, 'docker-compose.yml'),
    ),
    el('div', { class: 'setting-control' },
      el('span', { class: 'mono small break' }, String(item.value))),
  ));

  view.replaceChildren(
    el('h2', {}, 'Настройки'),
    ...safeChildren(isAdmin ? null : el('p', { class: 'muted' }, 'Изменять настройки может только администратор.')),
    el('p', { class: 'muted small' },
      'Сохранённые здесь значения главнее переменных окружения. ' +
      'Пункты с пометкой «после перезапуска» вступают в силу после перезапуска контейнера.'),
    el('div', { class: 'settings' }, ...rows),
    el('div', { class: 'row' }, saveButton),
    el('h3', {}, 'Параметры запуска'),
    el('p', { class: 'muted small' },
      'Задаются в docker-compose.yml. Меняются только перезапуском контейнера.'),
    el('div', { class: 'settings' }, ...readonly),
  );
}

function choiceLabel(choice) {
  return {
    artists: 'Исполнители', albums: 'Альбомы', playlists: 'Плейлисты',
    raw: 'Без перекодирования', flac: 'FLAC', alac: 'ALAC',
    DEBUG: 'DEBUG (подробно)', INFO: 'INFO', WARNING: 'WARNING',
    ERROR: 'ERROR (только ошибки)',
  }[choice] || choice;
}

/* ---------- Пользователи (admin) ---------- */
async function renderUsers(view) {
  if (state.role !== 'admin') {
    view.replaceChildren(el('p', { class: 'error' }, 'Только для администратора'));
    return;
  }
  const data = await api('/api/web/users');
  const items = data.items || [];

  const username = el('input', { placeholder: 'Имя', 'aria-label': 'Имя' });
  const password = el('input', { placeholder: 'Пароль', type: 'password', 'aria-label': 'Пароль' });
  const role = el('select', { 'aria-label': 'Роль' },
    el('option', { value: 'user' }, 'Пользователь'),
    el('option', { value: 'admin' }, 'Администратор'),
  );

  const createForm = el('form', {
    class: 'row wrap',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        await api('/api/web/users', {
          method: 'POST',
          body: JSON.stringify({
            username: username.value.trim(),
            password: password.value,
            role: role.value,
          }),
        });
        toast('Пользователь создан', 'ok');
        setView('users');
      } catch (error) { toast(error.message, 'error'); }
    },
  }, username, password, role, el('button', { type: 'submit', class: 'primary' }, 'Создать'));

  view.replaceChildren(
    el('h2', {}, 'Пользователи'),
    createForm,
    el('div', { class: 'list' }, ...items.map((user) => el('div', { class: 'row wrap' },
      el('div', { class: 'track-main' },
        el('strong', {}, user.username),
        el('span', { class: 'muted small' },
          [user.role, user.is_active ? 'активен' : 'заблокирован'].join(' · ')),
      ),
      el('button', {
        class: 'ghost small',
        onclick: async () => {
          try {
            await api(`/api/web/users/${user.id}/active`, {
              method: 'POST',
              body: JSON.stringify({ is_active: !user.is_active }),
            });
            setView('users');
          } catch (error) { toast(error.message, 'error'); }
        },
      }, user.is_active ? 'Заблокировать' : 'Разблокировать'),
      el('button', {
        class: 'ghost small',
        onclick: async () => {
          const next = user.role === 'admin' ? 'user' : 'admin';
          try {
            await api(`/api/web/users/${user.id}/role`, {
              method: 'POST', body: JSON.stringify({ role: next }),
            });
            setView('users');
          } catch (error) { toast(error.message, 'error'); }
        },
      }, user.role === 'admin' ? 'Снять админа' : 'Сделать админом'),
      el('button', {
        class: 'ghost small',
        onclick: async () => {
          const value = await promptDialog(
            `Новый пароль для «${user.username}». Все его сессии будут завершены.`,
            '', { title: 'Сменить пароль' });
          if (!value) return;
          try {
            await api(`/api/web/users/${user.id}/password`, {
              method: 'POST', body: JSON.stringify({ password: value }),
            });
            toast('Пароль изменён, сессии отозваны', 'ok');
          } catch (error) { toast(error.message, 'error'); }
        },
      }, 'Пароль'),
      el('button', {
        class: 'ghost danger small',
        onclick: async () => {
          const ok = await confirmDialog(
            `Учётная запись «${user.username}» будет удалена безвозвратно.`,
            { title: 'Удалить пользователя?' });
          if (!ok) return;
          try {
            await api(`/api/web/users/${user.id}`, { method: 'DELETE' });
            setView('users');
          } catch (error) { toast(error.message, 'error'); }
        },
      }, 'Удалить'),
    ))),
  );
}

/* ---------- Сервер (admin) ---------- */
async function renderServer(view) {
  if (state.role !== 'admin') {
    view.replaceChildren(el('p', { class: 'error' }, 'Только для администратора'));
    return;
  }
  const info = await api('/api/web/server');

  const rows = [
    ['Версия', info.version],
    ['Каталог музыки', info.music_dir],
    ['Каталог найден', info.music_dir_exists ? 'да' : 'НЕТ'],
    ['База музыки', info.music_db],
    ['База пользователей', info.users_db],
    ['Схема', JSON.stringify(info.schema)],
    ['Наблюдение', info.watch_enabled ? `вкл (poll=${info.watch_polling})` : 'выкл'],
    ['Интервал скана', info.scan_interval_sec ? `${info.scan_interval_sec} с` : 'выкл'],
    ['Лимит транскодов', info.max_transcodes],
    ['Обложек в кэше', `${info.covers.count} (${formatBytes(info.covers.total_bytes)})`],
    ['Треков', info.tracks],
    ['Отсутствующих', info.missing],
    // Раньше это была плашка на главной; теперь строка здесь, рядом с остальными цифрами.
    ['Пользователей', info.users ?? 0],
  ];

  view.replaceChildren(
    el('h2', {}, 'Сервер'),
    el('div', { class: 'kv' }, ...rows.flatMap(([key, value]) => [
      el('span', { class: 'muted' }, key),
      el('span', { class: 'mono break' }, String(value)),
    ])),
    // Работа с обложками живёт в разделе «Сканирование»: там же, где сам скан.
    // Здесь дублировать кнопки не нужно.
  );
}

/* ---------- Аккаунт ---------- */
async function renderAccount(view) {
  const sessions = await api('/api/web/account/sessions');
  const current = el('input', { type: 'password', placeholder: 'Текущий пароль' });
  const next = el('input', { type: 'password', placeholder: 'Новый пароль' });

  view.replaceChildren(
    el('h2', {}, 'Аккаунт'),
    el('p', {}, 'Пользователь: ', el('strong', {}, state.user),
       el('span', { class: 'muted' }, ` (${state.role})`)),
    el('h3', {}, 'Сменить пароль'),
    el('form', {
      class: 'row wrap',
      onsubmit: async (event) => {
        event.preventDefault();
        try {
          await api('/api/web/account/password', {
            method: 'POST',
            body: JSON.stringify({ current: current.value, next: next.value }),
          });
          toast('Пароль изменён. Другие устройства разлогинены.', 'ok');
          current.value = next.value = '';
        } catch (error) { toast(error.message, 'error'); }
      },
    }, current, next, el('button', { type: 'submit', class: 'primary' }, 'Сменить')),
    el('h3', {}, 'Активные сессии'),
    el('div', { class: 'list' }, ...(sessions.items || []).map((session) => el('div', { class: 'row wrap' },
      el('div', { class: 'track-main' },
        el('span', { class: 'ellipsis' }, session.user_agent || 'неизвестное устройство'),
        el('span', { class: 'muted small' },
          `${session.ip || '—'} · активна ${session.last_seen_at}`),
      ),
      el('button', {
        class: 'ghost danger small',
        onclick: async () => {
          await api(`/api/web/account/sessions/${session.id}`, { method: 'DELETE' });
          setView('account');
        },
      }, 'Завершить'),
    ))),
  );
}

/* ---------- Плеер ---------- */
function enqueue(tracks, { replace = true } = {}) {
  if (!tracks || !tracks.length) return;
  if (replace || !state.queue.length) {
    state.queue = tracks.slice();
    state.queueIndex = -1;
  } else {
    state.queue.push(...tracks);
  }
}

function playIndex(index) {
  if (!state.queue.length) return;
  // Зацикливание: у последнего трека при repeat=off дальше не идём.
  if (index >= state.queue.length) {
    if (state.repeat === 'all') index = 0;
    else { stopPlayback(); return; }
  }
  if (index < 0) index = 0;

  state.queueIndex = index;
  const track = state.queue[index];
  const player = $('#player');
  const audio = $('#audio');

  player.hidden = false;
  $('#now-title').textContent = `${track.title} — ${track.artist || ''}`;
  $('#now-album').textContent = track.album || '';
  const cover = $('#cover-now');
  cover.src = coverUrl('tr', track.id);
  audio.src = `/stream?id=${track.id}`;
  audio.play().catch(() => toast('Не удалось воспроизвести', 'error'));
  // Отмечаем прослушивание: без этого «Недавно прослушанные» не увидит
  // треки, игравшие в браузере (Subsonic-клиенты пишут историю сами).
  // Ошибку записи не показываем — она не должна мешать воспроизведению.
  api('/api/web/play-event', {
    method: 'POST',
    body: JSON.stringify({ track_id: track.id, played_ms: track.duration * 1000 }),
  }).catch(() => {});
  setPlayIcon(true);
  highlightCurrent();
  syncNowHeart();
}

function playTrack(track, queue = null) {
  // ВАЖНО: очередь из контекста (альбом, плейлист, избранное). Без неё
  // «следующий» видел бы только один трек — это и была причина того,
  // что переключение не работало.
  if (queue && queue.length) {
    const index = queue.findIndex((item) => item.id === track.id);
    enqueue(queue);
    playIndex(index >= 0 ? index : 0);
    return;
  }
  const existing = state.queue.findIndex((item) => item.id === track.id);
  if (existing >= 0) { playIndex(existing); return; }
  enqueue([track]);
  playIndex(0);
}

// Иконка кнопки «играть/пауза». Меняем src у <img>, а не текст:
// символьные глифы выглядят по-разному в разных ОС и шрифтах.

// Сердце в плеере показывает состояние текущего трека. Кнопка скрыта,
// пока ничего не играет: пустое сердце без трека только путает.
function syncNowHeart() {
  const button = $('#now-heart');
  if (!button) return;
  const track = state.queue[state.queueIndex];
  if (!track) {
    button.hidden = true;
    return;
  }
  button.hidden = false;
  const starred = state.starred.has(track.id);
  button.classList.toggle('on', starred);
  const img = button.querySelector('img');
  if (img) setIconSrc(img, starred ? 'heart-filled' : 'heart');
  const label = starred ? 'Убрать из избранного' : 'В избранное';
  button.title = label;
  button.setAttribute('aria-label', label);
}

function setPlayIcon(isPlaying) {
  const button = $('#play');
  if (!button) return;
  const img = button.querySelector('img');
  if (img) setIconSrc(img, isPlaying ? 'pause' : 'play');
  button.title = isPlaying ? 'Пауза' : 'Играть';
  button.setAttribute('aria-label', button.title);
}

// Повтор: off → all → one. Иконка меняется на repeat / repeat-one.
function setRepeatIcon() {
  const button = $('#repeat');
  if (!button) return;
  const img = button.querySelector('img');
  if (img) setIconSrc(img, state.repeat === 'one' ? 'repeat-one' : 'repeat');
  button.classList.toggle('active', state.repeat !== 'off');
  const label = { off: 'Повтор выключен', all: 'Повтор всех', one: 'Повтор одного' }[state.repeat];
  button.title = label;
  button.setAttribute('aria-label', label);
}

function stopPlayback() {
  const audio = $('#audio');
  audio.pause();
  audio.removeAttribute('src');
  state.queueIndex = -1;
  setPlayIcon(false);
  syncNowHeart();
  $('#now-title').textContent = '—';
  $('#now-album').textContent = '';
  highlightCurrent();
}

function nextTrack() {
  if (!state.queue.length) return;
  if (state.repeat === 'one') { playIndex(state.queueIndex); return; }
  if (state.shuffle && state.queue.length > 1) {
    let index = state.queueIndex;
    while (index === state.queueIndex) {
      index = Math.floor(Math.random() * state.queue.length);
    }
    playIndex(index);
    return;
  }
  playIndex(state.queueIndex + 1);
}

function prevTrack() {
  const audio = $('#audio');
  // Как в обычных плеерах: повторное нажатие в начале трека — предыдущий.
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (state.queueIndex <= 0) {
    if (state.repeat === 'all') { playIndex(state.queue.length - 1); }
    else { audio.currentTime = 0; }
    return;
  }
  playIndex(state.queueIndex - 1);
}

function highlightCurrent() {
  const current = state.queue[state.queueIndex];
  document.querySelectorAll('.track').forEach((node) => {
    const isCurrent = Boolean(current) && node.dataset.trackId === String(current.id);
    node.classList.toggle('playing', isCurrent);
  });
}

/* ---------- Утилиты ---------- */
function formatTime(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, '0')}`;
}

function formatBytes(bytes) {
  if (!bytes) return '0 Б';
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function card(label, value) {
  return el('div', { class: 'card' },
    el('div', { class: 'value' }, String(value)),
    el('div', { class: 'label' }, label),
  );
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

/* ---------- Инициализация ---------- */
document.addEventListener('DOMContentLoaded', async () => {
  state.user = document.body.dataset.user;
  state.role = document.body.dataset.role;
  state.csrf = document.body.dataset.csrf;
  // Иконки рейла и плеера пришли из HTML — заполняем им --icon-url,
  // иначе активное состояние не сможет покрасить их акцентом.
  hydrateIconVars();

  document.querySelectorAll('.admin-only').forEach((node) => {
    if (state.role !== 'admin') node.hidden = true;
  });

  // Меню настроек: шестерёнка открывает список разделов.
  const toggle = $('#menu-toggle');
  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = $('#menu');
    if (menu.hidden) openMenu(); else closeMenu();
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#menu, #menu-toggle')) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenu();
  });
  $('#menu').addEventListener('click', (event) => {
    const item = event.target.closest('.menu-item');
    if (!item) return;
    if (item.id === 'logout') return;
    if (item.dataset.view) setView(item.dataset.view);
  });

  // Навигация — левый рейл. #tabs больше нет: все разделы переехали сюда,
  // в шапке осталось только имя пользователя.
  $('#rail').addEventListener('click', (event) => {
    const btn = event.target.closest('.rail-btn');
    if (!btn) return;
    if (btn.dataset.action === 'search') {
      // Кнопка поиска фокусирует поле на текущей странице, а не уводит
      // в отдельный раздел: поиск есть только в «Библиотеке».
      const box = document.querySelector('.search');
      if (box) { box.focus(); box.select(); }
      return;
    }
    if (btn.dataset.view) setView(btn.dataset.view);
  });

  $('#now-heart').addEventListener('click', async () => {
    const track = state.queue[state.queueIndex];
    if (!track) return;
    const next = !state.starred.has(track.id);
    try {
      await api(`/api/web/tracks/${track.id}/star`, {
        method: 'POST', body: JSON.stringify({ starred: next }),
      });
    } catch (error) {
      toast(error.message, 'error');
      return;
    }
    if (next) state.starred.add(track.id); else state.starred.delete(track.id);
    syncNowHeart();
    // Строка этого трека в списке тоже должна обновиться.
    highlightCurrent();
    document.querySelectorAll(`.track[data-track-id="${track.id}"] .icon-btn.heart`)
      .forEach((button) => {
        button.classList.toggle('on', next);
        const img = button.querySelector('img');
        if (img) setIconSrc(img, next ? 'heart-filled' : 'heart');
      });
  });

  $('#logout').addEventListener('click', async () => {
    await api('/api/web/logout', { method: 'POST' });
    window.location.href = '/login';
  });

  // Обложка в плеере: заглушка при ошибке (inline onerror запрещён CSP).
  $('#cover-now').addEventListener('error', (event) => {
    const node = event.target;
    if (!node.src.endsWith(PLACEHOLDER)) node.src = PLACEHOLDER;
  });

  const audio = $('#audio');

  audio.addEventListener('timeupdate', () => {
    if (!audio.duration) return;
    $('#seek').value = String((audio.currentTime / audio.duration) * 1000);
    $('#time').textContent = `${formatTime(audio.currentTime)} / ${formatTime(audio.duration)}`;
  });
  audio.addEventListener('ended', () => {
    if (state.repeat === 'one') { audio.currentTime = 0; audio.play(); return; }
    nextTrack();
  });
  audio.addEventListener('error', () => {
    if (!audio.getAttribute('src')) return;
    toast('Ошибка воспроизведения', 'error');
    setPlayIcon(false);
  });

  $('#seek').addEventListener('input', (event) => {
    if (!audio.duration) return;
    audio.currentTime = (event.target.value / 1000) * audio.duration;
  });

  $('#play').addEventListener('click', () => {
    if (!state.queue.length) return;
    if (!audio.getAttribute('src')) { playIndex(0); return; }
    if (audio.paused) { audio.play(); setPlayIcon(true); }
    else { audio.pause(); setPlayIcon(false); }
  });

  $('#prev').addEventListener('click', prevTrack);
  $('#next').addEventListener('click', nextTrack);

  $('#shuffle').addEventListener('click', (event) => {
    state.shuffle = !state.shuffle;
    event.currentTarget.classList.toggle('active', state.shuffle);
  });

  $('#repeat').addEventListener('click', () => {
    state.repeat = state.repeat === 'off' ? 'all' : state.repeat === 'all' ? 'one' : 'off';
    setRepeatIcon();
  });

  // Горячие клавиши: пробел — пауза, стрелки — переключение.
  document.addEventListener('keydown', (event) => {
    const typing = event.target.closest('input, select, textarea');
    if (typing || !state.queue.length) return;
    if (event.code === 'Space') { event.preventDefault(); $('#play').click(); }
    else if (event.code === 'ArrowRight' && event.shiftKey) nextTrack();
    else if (event.code === 'ArrowLeft' && event.shiftKey) prevTrack();
  });

  try {
    const [prefs, starred] = await Promise.all([
      api('/api/web/prefs'),
      api('/api/web/starred-ids'),
    ]);
    state.prefs = prefs || state.prefs;
    state.libraryView = state.prefs.library_view || 'albums';
    state.starred = new Set(starred.ids || []);
  } catch (error) {
    if (error.message === 'unauthorized') return;
  }

  await setView('library');
});
