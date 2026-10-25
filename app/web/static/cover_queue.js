/* Очередь загрузки обложек.

   Зачем. Сетка альбомов при открытии отдаёт на экран 20–60 карточек, и все
   они попадают в зону видимости разом. Каждый запрос обложки на слабом
   сервере — это чтение файла и разбор тегов. Без очереди сервер получает
   пачку параллельных запросов, и последние карточки ждут дольше первых.

   Что делает очередь:
   * держит не больше MAX_PARALLEL загрузок одновременно;
   * карточка, которая ещё не начала грузиться и ушла с экрана, из очереди
     убирается — бесполезный запрос не уходит на сервер;
   * карточка, которая уже грузится, доедет до конца: обложка до 600 px
     весит десятки килобайт, прерывать её дороже, чем дождаться.

   Почему не fetch + blob. Политика CSP разрешает img-src 'self' data:, но
   не blob:. Поэтому очередь управляет самим img.src, а не загружает данные
   вручную.

   Файл не зависит от DOM-структуры сетки: ему нужны только объект с полем
   src, события load/error и isConnected. Это позволяет тестировать его в
   tests/js/headless.js без браузера.
*/

const COVER_MAX_PARALLEL = 3;

class CoverQueue {
  constructor(maxParallel = COVER_MAX_PARALLEL) {
    this.maxParallel = Math.max(1, maxParallel);
    this.waiting = [];       // элементы {node, url}, ещё не начали грузиться
    this.active = new Set(); // элементы, чей src уже выставлен
    this.peak = 0;           // максимум одновременных загрузок (для тестов)
  }

  // Карточка появилась в зоне видимости: ставим в очередь.
  enqueue(node, url) {
    if (node.loaded) return;
    if (this.active.has(node)) return;
    if (this.waiting.some((item) => item.node === node)) return;
    this.waiting.push({ node, url });
    this._pump();
  }

  // Карточка ушла с экрана. Если ещё ждёт — удаляем; если грузится — не трогаем.
  cancel(node) {
    const before = this.waiting.length;
    this.waiting = this.waiting.filter((item) => item.node !== node);
    return this.waiting.length !== before;
  }

  _pump() {
    while (this.active.size < this.maxParallel && this.waiting.length > 0) {
      const { node, url } = this.waiting.shift();
      if (node.loaded) continue;
      this._start(node, url);
    }
    this.peak = Math.max(this.peak, this.active.size);
  }

  _start(node, url) {
    this.active.add(node);
    const finish = () => {
      if (!this.active.has(node)) return;
      this.active.delete(node);
      this._pump();
    };
    // addEventListener, а не onload/onerror: у карточки уже есть свой onerror
    // (подставляет заглушку), и присваивание свойства его бы затёрло.
    node.addEventListener('load', () => {
      node.loaded = true;
      finish();
    }, { once: true });
    node.addEventListener('error', () => {
      // Ошибка тоже освобождает слот. Заглушку ставит onerror карточки.
      finish();
    }, { once: true });
    node.src = url;
  }

  // Сколько загрузок и ожидание — для отладки и тестов.
  stats() {
    return { active: this.active.size, waiting: this.waiting.length, peak: this.peak };
  }
}

const coverQueue = new CoverQueue(COVER_MAX_PARALLEL);
