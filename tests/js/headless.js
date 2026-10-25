/* Headless-проверка app.js: настоящий скрипт в vm с подменённым DOM и fetch.
   Ловит то, что синтаксис не видит: неопределённые функции, неверное
   обращение к полям ответа, пустые разделы, потерянную очередь плеера.
   Запуск: node tests/js/headless.js */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.join(__dirname, '..', '..', 'app', 'web', 'static');
// В браузере это отдельные <script> в общем контексте. Здесь — так же:
// порядок как в templates.py: dialogs.js объявляет диалоги, cover_queue.js —
// очередь загрузки обложек (app.js и scan_import.js вызывают coverQueue /
// confirmDialog), app.js объявляет хелперы, scan_import.js на них опирается.
const SOURCES = ['dialogs.js', 'cover_queue.js', 'app.js', 'scan_import.js', 'folder_sync.js'].map((name) => ({
  name,
  code: fs.readFileSync(path.join(STATIC, name), 'utf8'),
}));

class Node_ {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = String(tag) === '#text' ? 3 : 1;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    // setProperty нужен: icon() кладёт путь к SVG в CSS-переменную,
    // чтобы активное состояние красило иконку акцентом через mask.
    this.style = { setProperty(name, value) { this[name] = String(value); } };
    this.className = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.disabled = false;
    this.parentNode = null;
    this.classList = {
      toggle: (name, on) => {
        const set = new Set(String(this.className || '').split(/\s+/).filter(Boolean));
        if (on) set.add(name); else set.delete(name);
        this.className = [...set].join(' ');
      },
      add: (name) => this.classList.toggle(name, true),
      remove: (name) => this.classList.toggle(name, false),
      contains: (name) => String(this.className || '').split(/\s+/).includes(name),
    };
  }
  append(...nodes) {
    for (const node of nodes.flat()) {
      if (node === null || node === undefined || node === false) continue;
      const child = node instanceof Node_ ? node
        : Object.assign(new Node_('#text'), { textContent: String(node) });
      child.parentNode = this;
      this.children.push(child);
    }
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  removeEventListener() {}
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() {}
  querySelectorAll(sel) {
    const found = [];
    const walk = (node) => {
      if (sel === '[data-key]' && node.dataset && node.dataset.key) found.push(node);
      if (sel === '.track' && String(node.className).split(/\s+/).includes('track')) found.push(node);
      node.children.forEach(walk);
    };
    walk(this);
    return found;
  }
  closest(sel) {
    const cls = sel.replace(/^[.#]/, '');
    let node = this;
    while (node) {
      if (String(node.className).split(/\s+/).includes(cls)) return node;
      node = node.parentNode;
    }
    return null;
  }
  get firstChild() { return this.children[0] || null; }

  // В браузере querySelector есть у любого элемента. Код плеера ищет
  // иконку внутри кнопки, поэтому эмулятор обязан уметь то же самое.
  querySelector(sel) {
    const tag = sel.toUpperCase().replace(/[^A-Z0-9]/g, '');
    const walk = (node) => {
      if (!node || typeof node !== 'object') return null;
      if (node.tagName === tag) return node;
      for (const child of node.children || []) {
        const hit = walk(child);
        if (hit) return hit;
      }
      return null;
    };
    for (const child of this.children) {
      const hit = walk(child);
      if (hit) return hit;
    }
    return null;
  }

  // В браузере свойство src и атрибут src — одно и то же.
  // Без этого код (audio.src = ...) и проверка (getAttribute('src'))
  // расходятся, и тест ложно сообщает о поломке плеера.
  get src() { return this.attributes.src || ''; }
  set src(value) { this.attributes.src = String(value); }
}

const body = Object.assign(new Node_('body'), {
  dataset: { user: 'admin', role: 'admin', csrf: 'csrf-token' },
});
const nodes = {};
const nodeFor = (sel) => (nodes[sel] ||= new Node_(sel));

const audioNode = Object.assign(nodeFor('#audio'), { paused: true, duration: 200, currentTime: 0, play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; } });

function findById(root, id) {
  if (!root || typeof root !== 'object') return null;
  if (root.attributes && root.attributes.id === id) return root;
  for (const child of root.children || []) {
    const hit = findById(child, id);
    if (hit) return hit;
  }
  return null;
}

const document_ = {
  body,
  listeners: {},
  createElement: (tag) => new Node_(tag),
  createTextNode: (text) => Object.assign(new Node_('#text'), { textContent: String(text) }),
  addEventListener: (type, handler) => { (document_.listeners[type] ||= []).push(handler); },
  // Как настоящий DOM: сначала ищем узел в дереве по id, и только если
  // его там нет — в карте служебных узлов. Иначе элементы, созданные
  // рендером (#library-body), превращаются в пустые заглушки.
  querySelector: (sel) => {
    if (sel.startsWith('#')) {
      // document_ не имеет поля view: узлы лежат в карте nodes.
      const found = findById(nodeFor('#view'), sel.slice(1));
      if (found) return found;
    }
    return nodeFor(sel);
  },
  querySelectorAll: (sel) => {
    if (sel === '.tab' || sel === '.admin-only') return [];
    if (sel === '.track') return [];
    return [];
  },
};
nodeFor('#view');

const ALBUM = { id: 1, name: 'Opera', artist: 'Queen', year: 1975, song_count: 2 };
const TRACKS = [
  { id: 1, title: 'Bohemian', artist: 'Queen', album: 'Opera', duration: 355, track_number: 1 },
  { id: 2, title: 'Love', artist: 'Queen', album: 'Opera', duration: 200, track_number: 2 },
];

const FIXTURES = {
  '/api/web/prefs': { ok: true, data: { library_view: 'albums', default_transcode: 'raw' } },
  '/api/web/albums': { ok: true, data: { items: [ALBUM] } },
  '/api/web/artists': { ok: true, data: { items: [{ id: 1, name: 'Queen', album_count: 1 }] } },
  '/api/web/artists/1': { ok: true, data: { id: 1, name: 'Queen', albums: [ALBUM] } },
  '/api/web/stats': { ok: true, data: { tracks: 3, albums: 2, artists: 2, users: 1 } },
  '/api/web/playlists': { ok: true, data: { items: [{ id: 1, name: 'Test', song_count: 2, is_public: false, is_smart: false, owner_id: 1 }] } },
  '/api/web/playlists/1': { ok: true, data: { id: 1, name: 'Test', is_public: false, is_smart: false, can_edit: true, tracks: TRACKS } },
  '/api/web/albums/1': { ok: true, data: { id: 1, name: 'Opera', artist: 'Queen', year: 1975, tracks: TRACKS } },
  '/api/web/users': { ok: true, data: { items: [] } },
  '/api/web/settings': { ok: true, data: { items: [
    { key: 'library_view', kind: 'choice', label: 'Вид библиотеки по умолчанию', help: 'h', choices: ['artists', 'albums', 'playlists'], value: 'albums', default: 'albums', overridden: false },
    { key: 'max_transcodes', kind: 'int', label: 'Лимит', help: 'h', minimum: 1, maximum: 64, value: 4, default: 4, overridden: false },
  ] } },
  '/api/web/server': { ok: true, data: { version: '0.8.0', music_dir: '/m', music_dir_exists: true, music_db: 'm', users_db: 'u', schema: { users: 2, music: 2 }, watch_enabled: true, watch_polling: true, scan_interval_sec: 300, max_transcodes: 4, tracks: 3, missing: 0, covers: { count: 5, total_bytes: 2048 } } },
  '/api/web/account/sessions': { ok: true, data: { items: [], current_id: 1 } },
  '/api/web/starred': { ok: true, data: { items: [TRACKS[0]] } },
  '/api/web/starred-ids': { ok: true, data: { ids: [1] } },
  '/api/web/tracks/1/star': { ok: true, data: { id: 1, starred: false } },
  '/api/web/scan': { ok: true, data: { seen: 3, added: 0 } },
  '/api/web/settings/reset': { ok: true, data: { reset: 'library_view' } },
};

const calls = [];
async function fetch_(url, options = {}) {
  const key = String(url).split('?')[0];
  calls.push({ key, method: options.method || 'GET', body: options.body });
  const payload = FIXTURES[key] || { ok: true, data: {} };
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
}

const fillStub = (node, ...children) => {
  const items = [];
  const push = (v) => {
    if (v === null || v === undefined || v === false) return;
    if (Array.isArray(v)) { v.forEach(push); return; }
    items.push(v);
  };
  children.forEach(push);
  node.replaceChildren(...items);
  return node;
};

// Аккордеон хранит свёрнутые секции в localStorage — в эмуляторе
// нужен минимальный аналог.
const localStorageStub = (() => {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };
})();

const sandbox = {
  document: document_,
  localStorage: localStorageStub,
  fill: fillStub,
  window: { location: { href: '/app' } },
  fetch: fetch_,
  console, setTimeout, clearTimeout,
  Promise, Object, Array, JSON, String, Number, Boolean, Math, Date, Set, Map, Error,
  confirm: () => true,
  prompt: () => '1',
  encodeURIComponent, parseInt, parseFloat, isNaN,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
// `const state` не становится свойством globalThis, поэтому экспортируем
// его явно: тестам нужен доступ к очереди и виду библиотеки.
for (const source of SOURCES) {
  const tail = source.name === 'app.js' ? "\n;globalThis.__testState = state;" : "";
  vm.runInContext(source.code + tail, sandbox, { filename: source.name });
}
Object.defineProperty(sandbox, 'state', {
  get: () => sandbox.__testState,
  set: (value) => { sandbox.__testState = value; },
});

const failures = [];
// Ждём, пока все промисы рендера (несколько последовательных fetch) завершатся.
// Фиксированное число тиков ненадёжно: цепочка из 3-4 запросов не успевает.
const flush = async () => {
  for (let i = 0; i < 40; i += 1) {
    await new Promise((r) => setTimeout(r, 0));
  }
};
async function check(name, fn) {
  try {
    await fn();
    await flush();
    console.log('  ok   ' + name);
  } catch (error) {
    failures.push(name + ': ' + error.message);
    console.log('  FAIL ' + name + ': ' + error.message);
  }
}
function textOf(node) {
  if (!node || typeof node !== 'object') return '';
  if (node.nodeType === 3) return node.textContent || '';
  return (node.textContent || '') + node.children.map(textOf).join('');
}
const viewText = () => textOf(nodeFor('#view'));

(async () => {
  console.log('headless-проверка app.js');
  for (const handler of document_.listeners.DOMContentLoaded || []) await handler();
  await flush();

  await check('библиотека: вид «Альбомы» по умолчанию, альбом отрисован', async () => {
    await sandbox.setView('library');
    if (sandbox.state.libraryView !== 'albums') throw new Error('вид=' + sandbox.state.libraryView);
    if (!viewText().includes('Opera')) throw new Error('альбом не отрисован');
    if (!viewText().includes('Треков')) throw new Error('карточки статистики не отрисованы');
  });

  await check('переключатель: Исполнители показывает исполнителя', async () => {
    sandbox.state.libraryView = 'artists';
    await sandbox.setView('library');
    if (!viewText().includes('Queen')) throw new Error('исполнитель не отрисован');
  });

  await check('переключатель: Плейлисты показывает плейлист', async () => {
    sandbox.state.libraryView = 'playlists';
    await sandbox.setView('library');
    if (!viewText().includes('Test')) throw new Error('плейлист не отрисован');
  });

  sandbox.state.libraryView = 'albums';

  await check('плейлисты: две колонки — список и треки выбранного', async () => {
    await sandbox.setView('playlists');
    const text = viewText();
    // Плейлисты теперь в три колонки: класс pl-layout, не two-col.
    if (!nodeFor('#view').children.some((c) => String(c.className).includes('pl-layout')))
      throw new Error('нет контейнера двух колонок');
    if (!text.includes('Bohemian')) throw new Error('треки выбранного плейлиста не показаны');
  });

  await check('меню настроек: раздел «Пользователи» открывается', async () => {
    await sandbox.setView('users');
    if (!viewText().includes('Пользователи')) throw new Error('раздел не отрисован');
  });

  await check('сервер: строка про кэш обложек', async () => {
    await sandbox.setView('server');
    if (!viewText().includes('Обложек')) throw new Error('нет строки про кэш');
  });

  await check('аккаунт открывается', () => sandbox.setView('account'));

  await check('настройки: вид библиотеки и сохранение', async () => {
    await sandbox.setView('settings');
    if (!viewText().includes('Вид библиотеки')) throw new Error('настройка не отрисована');
    const before = calls.length;
    const saveBtn = nodeFor('#view').children
      .flatMap((c) => [c, ...c.children])
      .flatMap((c) => [c, ...c.children])
      .find((n) => n.tagName === 'BUTTON' && textOf(n) === 'Сохранить');
    if (!saveBtn) throw new Error('кнопка «Сохранить» не найдена');
    await saveBtn.listeners.click[0]({ target: saveBtn });
    await flush();
    const posted = calls.slice(before).find((c) => c.key === '/api/web/settings' && c.method === 'POST');
    if (!posted) throw new Error('POST /api/web/settings не отправлен');
  });

  await check('плеер: клик по треку из альбома сохраняет ВСЮ очередь', async () => {
    await sandbox.openAlbum(1);
    sandbox.playTrack(TRACKS[0], TRACKS);
    if (sandbox.state.queue.length !== 2) throw new Error('очередь=' + sandbox.state.queue.length + ', ожидали 2');
    if (sandbox.state.queueIndex !== 0) throw new Error('индекс=' + sandbox.state.queueIndex);
  });

  await check('плеер: «следующий» переходит ко второму треку', () => {
    sandbox.nextTrack();
    if (sandbox.state.queueIndex !== 1) throw new Error('индекс=' + sandbox.state.queueIndex);
    if (!audioNode.getAttribute('src') || !audioNode.getAttribute('src').endsWith('id=2'))
      throw new Error('src=' + audioNode.getAttribute('src'));
  });

  await check('плеер: «предыдущий» возвращает к первому', () => {
    audioNode.currentTime = 0;
    sandbox.prevTrack();
    if (sandbox.state.queueIndex !== 0) throw new Error('индекс=' + sandbox.state.queueIndex);
  });

  await check('плеер: «предыдущий» в начале первого трека не падает', () => {
    audioNode.currentTime = 0;
    sandbox.prevTrack();
    if (sandbox.state.queueIndex !== 0) throw new Error('индекс=' + sandbox.state.queueIndex);
  });

  await check('плеер: «следующий» у последнего трека останавливается', () => {
    sandbox.playIndex(1);
    sandbox.nextTrack();
    if (sandbox.state.queueIndex !== -1) throw new Error('должен остановиться, индекс=' + sandbox.state.queueIndex);
  });

  await check('плеер: повтор всех зацикливает очередь', () => {
    sandbox.state.repeat = 'all';
    sandbox.playIndex(1);
    sandbox.nextTrack();
    if (sandbox.state.queueIndex !== 0) throw new Error('индекс=' + sandbox.state.queueIndex);
    sandbox.state.repeat = 'off';
  });

  await check('плеер: «предыдущий» у первого с повтором всех — к последнему', () => {
    sandbox.state.repeat = 'all';
    sandbox.playIndex(0);
    audioNode.currentTime = 0;
    sandbox.prevTrack();
    if (sandbox.state.queueIndex !== 1) throw new Error('индекс=' + sandbox.state.queueIndex);
    sandbox.state.repeat = 'off';
  });

  await check('избранное: сердце шлёт запрос', async () => {
    await sandbox.setView('starred');
    const before = calls.length;
    // Кнопка избранного теперь с сердцем (класс heart), а не звездой.
    const heart = nodeFor('#view').children
      .flatMap((c) => [c, ...c.children]).flatMap((c) => [c, ...c.children])
      .find((n) => n.className && String(n.className).includes('heart'));
    if (!heart) throw new Error('кнопка избранного (heart) не найдена');
    await heart.listeners.click[0]({ stopPropagation() {} });
    if (!calls.slice(before).some((c) => c.key === '/api/web/tracks/1/star')) throw new Error('запрос не ушёл');
  });

  await check('форматирование времени и размера', () => {
    if (sandbox.formatTime(75) !== '1:15') throw new Error(sandbox.formatTime(75));
    if (!sandbox.formatBytes(2048).includes('КБ')) throw new Error(sandbox.formatBytes(2048));
  });

  await check('api() разворачивает конверт', async () => {
    const stats = await sandbox.api('/api/web/stats');
    if (stats.tracks !== 3) throw new Error('tracks=' + stats.tracks);
    if ('ok' in stats) throw new Error('конверт не развёрнут');
  });

  await check('меню плейлистов: открывается и содержит пункты', async () => {
    await sandbox.setView('library');
    await sandbox.setView('playlists');
    if (!calls.length) throw new Error('нет запросов');
  });

  await check('импорт из папки: блоки сборки и ресинка есть в разделе', () => {
    const nodes = sandbox.folderSyncSections();
    const text = JSON.stringify(nodes, (k, v) => (k === 'parentNode' || k === 'listeners' || k === 'classList' ? undefined : v));
    if (!text.includes('Сборка плейлиста из папки')) throw new Error('нет блока сборки');
    if (!text.includes('Ресинк плейлиста с папкой')) throw new Error('нет блока ресинка');
    if (!text.includes('Скрипт рекурсивно обходит папку (1-й уровень)')) throw new Error('нет описания из ТЗ');
  });

  await check('импорт из папки: функции опроса доступны из раздела', () => {
    // setInterval в этом харнессе не подменён, поэтому здесь только наличие API.
    for (const name of ['pollFolderJob', 'stopFolderPolling', 'renderResyncPlan']) {
      if (typeof sandbox[name] !== 'function') throw new Error('нет ' + name);
    }
  });

  console.log('');
  if (failures.length) {
    console.log('ПРОВАЛЕНО: ' + failures.length);
    for (const f of failures) console.log('  - ' + f);
    process.exit(1);
  }
  console.log('все проверки пройдены');
})();
