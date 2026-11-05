/* Раздел «Сканирование» и «Импорт плейлистов».

   Вынесено из app.js, чтобы основной файл не разрастался: здесь своя
   логика опроса прогресса, разбора m3u8 и таблицы решений. */

/* ---------- Сканирование ---------- */
let scanPollTimer = null;

/* Таблица ошибок скана: «файл / причина».

   Показываем сразу, без раскрывающегося блока: этого просили, и это
   действительно удобнее — при сбое хочется видеть список, а не кликать.
   Прокрутка ограничена по высоте, иначе 500 строк растянут страницу. */
function scanErrorTable(errors, total) {
  if (!errors.length) {
    return el('p', { class: 'small muted' },
      `Ошибок ${total}, но их тексты не сохранены: запись сделана до обновления схемы.`);
  }

  const rows = errors.map((item) => el('tr', {},
    el('td', { class: 'mono small path-cell' }, item.path || '—'),
    el('td', { class: 'small' }, item.reason || '—'),
  ));

  return el('div', { class: 'error-table-wrap' },
    el('table', { class: 'error-table' },
      el('thead', {},
        el('tr', {},
          el('th', {}, 'Файл'),
          el('th', {}, 'Причина'),
        ),
      ),
      el('tbody', {}, ...rows),
    ),
    errors.length < total
      ? el('p', { class: 'small muted' },
          `Показаны первые ${errors.length} из ${total}.`)
      : null,
  );
}

async function renderScan(view) {
  stopScanPolling();
  const status = await api('/api/web/scan/status');
  const history = await api('/api/web/scan/history?limit=10');

  const bar = el('div', { class: 'progress' },
    el('div', { class: 'progress-fill', id: 'scan-fill' }),
  );
  const info = el('div', { class: 'scan-info', id: 'scan-info' });
  // Сюда updateScanView кладёт таблицу ошибок текущего прохода.
  const liveErrors = el('div', { class: 'scan-errors-live', id: 'scan-errors' });
  const actions = el('div', { class: 'row' });

  const startButton = (mode, label, cls) => el('button', {
    class: cls,
    onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api(`/api/web/scan?mode=${mode}`, { method: 'POST' });
        toast('Сканирование запущено', 'ok');
        // Скан стартует асинхронно: опрос включаем сразу, а не после перерисовки.
        startScanPolling(view);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
      }
    },
  }, label);

  // Кнопки работы с обложками перенесены сюда из вкладки «Сервер»:
  // всё, что относится к индексации, собрано в одном разделе.
  const coversActions = el('div', { class: 'row wrap' },
    el('button', {
      class: 'ghost',
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api('/api/web/covers/warm', { method: 'POST' });
          toast('Обложки поставлены в очередь', 'ok');
          renderScan(view);
        } catch (error) {
          toast(error.message, 'error');
          button.disabled = false;
        }
      },
    }, 'Пересканировать обложки'),
    el('button', {
      class: 'ghost danger',
      onclick: async () => {
        const ok = await confirmDialog(
          'Файлы обложек будут удалены и созданы заново при следующем запросе.',
          { title: 'Очистить кэш обложек?', okLabel: 'Очистить' });
        if (!ok) return;
        try {
          await api('/api/web/covers/purge', { method: 'POST' });
          toast('Кэш обложек очищен', 'ok');
          renderScan(view);
        } catch (error) { toast(error.message, 'error'); }
      },
    }, 'Очистить кэш обложек'),
  );

  actions.append(
    startButton('incremental', 'Сканировать (только изменения)', 'primary'),
    startButton('full', 'Полное сканирование', 'ghost'),
    el('button', {
      class: 'ghost', onclick: () => renderScan(view),
    }, 'Обновить'),
  );

  const rows = (history.items || []).map((run) => {
    const total = run.errors_total || run.failed || 0;
    const errorTable = total ? scanErrorTable(run.errors || [], total) : null;

    return el('div', { class: 'scan-run' },
      el('div', { class: 'row scan-run-head' },
        el('span', { class: 'mono small' }, `#${run.run_id}`),
        el('span', { class: 'small' }, run.mode === 'full' ? 'полный' : 'инкремент'),
        el('span', { class: 'small muted' }, run.started_at.replace('T', ' ').slice(0, 19)),
        el('span', { class: 'small' }, `+${run.added} / ~${run.updated} / -${run.removed}`),
        total ? el('span', { class: 'badge danger' }, `ошибок ${total}`) : null,
        el('span', { class: `badge status-${run.status}` }, run.status),
      ),
      errorTable,
    );
  });

  fill(view,
    el('h2', {}, 'Сканирование'),
    el('p', { class: 'muted small' },
      'Инкрементальный проход читает только новые и изменённые файлы. ' +
      'Полный заново разбирает теги у всех файлов и помечает отсутствующие.'),
    bar,
    info,
    el('h3', {}, 'Обложки'),
    coversProgress(),
    coversActions,
    liveErrors,
    actions,
    el('h3', {}, 'История'),
    rows.length ? el('div', { class: 'list' }, ...rows)
                : el('p', { class: 'muted' }, 'Запусков ещё не было'),
  );

  // Если скан уже идёт (запущен из другой вкладки) — подхватываем прогресс.
  updateCoversView(status.covers);
  if (scanIsBusy(status)) {
    updateScanView(status.current);
    startScanPolling(view);
  } else {
    updateScanView(status.last_run
      ? { ...status.last_run, percent: 100 }
      : { status: 'idle' });
  }
}

function stopScanPolling() {
  if (scanPollTimer) {
    clearInterval(scanPollTimer);
    scanPollTimer = null;
  }
}

function startScanPolling(view) {
  stopScanPolling();
  // Опрос раз в секунду: WebSocket может отвалиться, а состояние должно
  // быть свежим. Нагрузка мизерная — один SELECT.
  scanPollTimer = setInterval(async () => {
    try {
      const data = await api('/api/web/scan/status');
      if (state.view !== 'scan') { stopScanPolling(); return; }
      updateScanView(data.current);
      updateCoversView(data.covers);
      if (!scanIsBusy(data)) {
        stopScanPolling();
        renderScan(view);   // финальная перерисовка: история и итоги
      }
    } catch (error) {
      // Сеть на миг пропала — не бросаем опрос, пробуем на следующем тике.
      if (error && error.message === 'unauthorized') stopScanPolling();
    }
  }, 1000);
}

// Занят ли сервер: идёт скан или фаза обложек после него.
function scanIsBusy(data) {
  if (!data) return false;
  const scanning = Boolean(data.current && data.current.status === 'running');
  const covering = Boolean(data.covers && data.covers.state === 'running');
  return scanning || covering;
}

// Полоса прогресса обложек. Ids общие для раздела и главной: они никогда
// не показываются одновременно.
function coversProgress() {
  return el('div', { class: 'covers-progress' },
    el('div', { class: 'progress' }, el('div', { class: 'progress-fill', id: 'covers-fill' })),
    el('div', { class: 'scan-info', id: 'covers-info' }),
  );
}

function updateCoversView(covers) {
  const fillNode = document.querySelector('#covers-fill');
  const infoNode = document.querySelector('#covers-info');
  if (!fillNode || !infoNode || !covers) return;

  const labels = {
    idle: 'Обложки: ждут скана',
    running: 'Обложки: извлечение',
    done: 'Обложки: все альбомы обработаны',
    cancelled: 'Обложки: остановлено',
    error: 'Обложки: ошибка',
  };
  const percent = covers.percent ?? 0;
  fillNode.style.width = `${covers.state === 'done' ? 100 : percent}%`;
  fillNode.classList.toggle('running', covers.state === 'running');
  fillNode.classList.toggle('failed', covers.state === 'error');

  const parts = [labels[covers.state] || covers.state];
  if (covers.total) parts.push(`${covers.done} из ${covers.total}`);
  if (covers.made) parts.push(`сделано ${covers.made}`);
  if (covers.absent) parts.push(`без картинки ${covers.absent}`);
  if (covers.failed) parts.push(`ошибок ${covers.failed}`);
  if (covers.state === 'running' && covers.current_album_sec != null && covers.current_album_sec >= 5) {
    // Альбом читается дольше 5 с — показываем, чтобы было видно, что фаза не встала.
    parts.push(`альбом ${covers.current_album} · ${Math.round(covers.current_album_sec)} с`);
  }
  if (covers.state === 'running' && covers.eta_sec != null) {
    parts.push(`осталось ~${Math.max(1, Math.round(covers.eta_sec / 60))} мин`);
  }
  if (covers.last_error) parts.push(covers.last_error);
  fill(infoNode, el('span', {}, parts.join(' · ')));

  // Блок целиком виден только когда фаза шла или идёт.
  const block = fillNode.closest('.covers-progress');
  if (block) block.classList.toggle('hidden', covers.state === 'idle' && !covers.total);
}

function updateScanView(current) {
  // Пауза очереди обложек, пока скан идёт (вызывается и на главной, и в разделе «Сканирование»).
  if (current && current.status === 'running') coverReady.pause();
  else if (current && current.status !== undefined) coverReady.resume();
  const fillNode = document.querySelector('#scan-fill');
  const infoNode = document.querySelector('#scan-info');
  const errorsNode = document.querySelector('#scan-errors');
  if (!fillNode || !infoNode) return;

  const percent = current.percent ?? 0;
  fillNode.style.width = `${percent}%`;
  fillNode.classList.toggle('running', current.status === 'running');
  fillNode.classList.toggle('failed', current.status === 'failed');

  // Пока идёт скан, вместо голого процента пишем, что именно происходит:
  // «Загрузка / Идёт сканирование… N%». Это видно только в разделе «Сканирование».
  const running = current.status === 'running';
  const stageText = current.stage === 'walk' ? 'Обход каталога' : 'Разбор тегов';
  const labels = {
    idle: 'Готов к запуску',
    running: `Загрузка / Идёт сканирование… ${percent}% · ${stageText}`,
    completed: 'Завершено',
    failed: 'Ошибка',
    cancelled: 'Отменено',
  };
  const pieces = [
    running ? labels.running : `${labels[current.status] || current.status} — ${percent}%`,
  ];
  if (current.status === 'running' || current.to_parse) {
    pieces.push(`разобрано ${current.processed || 0} из ${current.to_parse || 0}`);
  }
  if (current.seen) pieces.push(`найдено файлов ${current.seen}`);
  if (current.added) pieces.push(`новых ${current.added}`);
  if (current.updated) pieces.push(`обновлено ${current.updated}`);
  if (current.removed) pieces.push(`удалено ${current.removed}`);
  if (current.failed) pieces.push(`ошибок ${current.failed}`);
  if (current.error) pieces.push(current.error);

  fill(infoNode, el('span', {}, pieces.join(' · ')));

  // Ошибки текущего прохода — той же таблицей, что и в истории.
  if (errorsNode) {
    const errors = current.recent_errors || [];
    const total = current.failed || 0;
    if (total && errors.length) {
      fill(errorsNode,
        el('h3', {}, `Ошибки текущего прохода (${total})`),
        scanErrorTable(errors, total),
      );
    } else if (total) {
      fill(errorsNode,
        el('h3', {}, `Ошибки текущего прохода (${total})`),
        el('p', { class: 'small muted' },
          'Тексты появятся по мере разбора: список обновляется каждые 25 файлов.'),
      );
    } else {
      fill(errorsNode);
    }
  }
}

/* ---------- Активность на главной ----------

   Библиотека видна сразу: сверху показывается панель скана и фазы обложек,
   а список альбомов обновляется на ходу, пока данные меняются. Раньше главная
   целиком заменялась панелью скана до конца прохода. */

let homeScanTimer = null;

function stopHomeScan() {
  if (homeScanTimer) {
    clearInterval(homeScanTimer);
    homeScanTimer = null;
  }
}

// Панель в начале главной. Видимость внутренних блоков решает renderHomeActivity.
function homeActivityBox() {
  return el('div', { id: 'home-activity', class: 'activity hidden' },
    el('div', { class: 'activity-title' }, 'Идёт обработка библиотеки'),
    el('div', { id: 'home-scan-block' },
      el('div', { class: 'progress' }, el('div', { class: 'progress-fill', id: 'scan-fill' })),
      el('div', { class: 'scan-info', id: 'scan-info' }),
    ),
    el('div', { id: 'home-covers-block' }, coversProgress()),
  );
}

function renderHomeActivity(data) {
  const box = document.querySelector('#home-activity');
  const scanning = Boolean(data.current && data.current.status === 'running');
  // Заглушки списков («Загрузка / Идёт сканирование…») читают это состояние.
  if (typeof state !== 'undefined' && state) {
    state.scanRunning = scanning;
    state.scanPercent = data.current ? Math.round(data.current.percent || 0) : 0;
  }
  if (!box) return;
  const covering = Boolean(data.covers && data.covers.state === 'running');
  // Пока сервер занят сканом или фазой обложек, проверки готовности не шлём:
  // они не нужны, а каждый запрос — лишняя работа рядом с индексацией.
  if (scanning || covering) coverReady.pause(); else coverReady.resume();
  box.classList.toggle('hidden', !(scanning || covering));
  document.querySelector('#home-scan-block')?.classList.toggle('hidden', !scanning);
  document.querySelector('#home-covers-block')?.classList.toggle('hidden', !covering);
  if (scanning) updateScanView(data.current);
  if (covering) updateCoversView(data.covers);
}

// Перерисовывает список альбомов/исполнителей/плейлистов без сброса всей главной.
async function refreshLibraryBody() {
  const body = document.querySelector('#library-body');
  if (!body) return;
  try {
    if (state.libraryView === 'artists') await renderArtistsInto(body);
    else if (state.libraryView === 'playlists') await renderPlaylistsInto(body);
    else await renderAlbumsInto(body);
  } catch (error) {
    // Временная ошибка сети не должна ломать главную: следующий тик повторит.
  }
  loadStats(document.querySelector('.cards')).catch(() => {});
}

// Вызывается из renderLibrary. Показывает панель, если сервер занят, и
// опрашивает статус, пока есть работа.
async function mountHomeActivity() {
  stopHomeScan();
  let data;
  try {
    data = await api('/api/web/scan/status');
  } catch (error) {
    return;
  }
  renderHomeActivity(data);
  if (!scanIsBusy(data)) return;

  let lastSignature = '';
  let lastRefresh = 0;
  homeScanTimer = setInterval(async () => {
    if (state.view !== 'library') { stopHomeScan(); return; }
    let current;
    try {
      current = await api('/api/web/scan/status');
    } catch (error) {
      stopHomeScan();
      return;
    }
    renderHomeActivity(current);
    const busy = scanIsBusy(current);
    // Обновляем список, когда что-то реально изменилось, но не чаще раза в 3 с.
    const signature = [
      current.current?.processed, current.current?.added, current.covers?.done,
    ].join('|');
    const now = Date.now();
    if (signature !== lastSignature && now - lastRefresh > 3000) {
      lastSignature = signature;
      lastRefresh = now;
      refreshLibraryBody();
    }
    if (!busy) {
      stopHomeScan();
      refreshLibraryBody();   // финальный кадр: всё, что успело записаться
    }
  }, 1000);
}

/* ---------- Импорт плейлистов ---------- */
async function renderImport(view) {
  stopFolderPolling();
  const settings = await api('/api/web/settings');
  const items = settings.items || [];
  const dirItem = items.find((item) => item.key === 'playlists_dir');
  const fromItem = items.find((item) => item.key === 'playlist_prefix_from');
  const toItem = items.find((item) => item.key === 'playlist_prefix_to');

  const dirInput = el('input', {
    type: 'text', class: 'wide', value: dirItem?.value || '',
    placeholder: '/music', 'aria-label': 'Папка с плейлистами',
  });
  const fromInput = el('input', {
    type: 'text', value: fromItem?.value || '',
    placeholder: 'M:/Music', 'aria-label': 'Старый префикс',
  });
  const toInput = el('input', {
    type: 'text', value: toItem?.value || '/music',
    placeholder: '/music', 'aria-label': 'Новый префикс',
  });

  const hint = el('div', { class: 'muted small', id: 'dir-hint' });
  const result = el('div', { class: 'import-result' });

  // Проверка папки по вводу: сразу видно, существует ли она.
  const checkDir = debounce(async () => {
    const path = dirInput.value.trim();
    const data = await api(`/api/web/playlists/import/check?dir=${encodeURIComponent(path)}`);
    hint.className = `small ${data.exists ? 'muted' : 'error'}`;
    hint.textContent = data.message;
  }, 400);
  dirInput.addEventListener('input', checkDir);
  checkDir();

  const saveSettings = async () => {
    await api('/api/web/settings', {
      method: 'POST',
      body: JSON.stringify({
        values: {
          playlists_dir: dirInput.value.trim(),
          playlist_prefix_from: fromInput.value.trim(),
          playlist_prefix_to: toInput.value.trim(),
        },
      }),
    });
  };

  // Импорт: разбор идёт фоновой задачей, интерфейс опрашивает прогресс.
  //
  // Раньше здесь был один синхронный запрос: пока шёл разбор, на экране висело
  // «Разбираю папку…» без единой цифры. На большой библиотеке это минуты, и
  // выглядело как зависание. Теперь сверху общий прогресс, ниже — по плейлистам.
  let pollTimer = null;
  let currentJobId = null;

  const stopPolling = () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };

  const drawProgress = (job) => {
    const stageText = {
      walk: 'Обход папки',
      playlists: `Разбор плейлистов ${job.done} из ${job.total}`,
      done: 'Готово',
      error: 'Ошибка',
    }[job.stage] || job.stage;

    const bar = el('div', { class: 'progress' },
      el('div', { class: 'progress-fill running', style: `width:${job.percent}%` }));

    // Строки по каждому плейлисту: жёлтый вопрос — ещё не обработан,
    // далее «разбираю»/«сопоставляю», в конце галочка или крестик.
    const rows = (job.items || []).map((item) => {
      const cls = item.state === 'done' ? 'created'
        : item.state === 'error' ? 'error' : 'pending';
      const label = item.state === 'error' ? item.error
        : item.state === 'done' ? `${item.matched} из ${item.total} треков`
        : item.state === 'parsing' ? 'Разбираю…'
        : item.state === 'matching' ? 'Сопоставляю…'
        : 'В очереди';
      return el('div', { class: 'import-card' },
        el('div', { class: 'row' },
          el('span', { class: `import-status ${cls}`,
                       title: item.state === 'done' ? 'Готово'
                            : item.state === 'error' ? 'Ошибка' : 'Ещё не обработан' }, ''),
          el('div', { class: 'track-main' },
            el('strong', { class: 'ellipsis' }, item.name),
            el('span', { class: 'muted tiny ellipsis' }, label),
          ),
        ),
      );
    });

    return [
      el('div', { class: 'import-progress' },
        el('div', { class: 'row' },
          el('span', { class: 'small' }, `${stageText} · ${job.percent}%`),
          job.current ? el('span', { class: 'muted small ellipsis' }, job.current) : null,
        ),
        bar,
      ),
      rows.length ? el('div', { class: 'import-list' }, ...rows) : null,
    ].filter(Boolean);
  };

  const previewButton = el('button', {
    class: 'primary',
    onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      stopPolling();
      fill(result, el('p', { class: 'muted' }, 'Запускаю разбор…'));
      try {
        await saveSettings();
        const job = await api('/api/web/playlists/import/start', {
          method: 'POST',
          body: JSON.stringify({ dir: dirInput.value.trim() }),
        });
        currentJobId = job.id;
        fill(result, ...drawProgress(job));

        pollTimer = setInterval(async () => {
          try {
            const state = await api(`/api/web/playlists/import/jobs/${job.id}`);
            fill(result, ...drawProgress(state));
            if (state.state === 'done') {
              stopPolling();
              renderPreview(result, state.result, dirInput.value.trim(), job.id);
              button.disabled = false;
            } else if (state.state === 'error') {
              stopPolling();
              fill(result, el('p', { class: 'error' }, state.error || 'разбор не удался'));
              button.disabled = false;
            }
          } catch (error) {
            stopPolling();
            fill(result, el('p', { class: 'error' }, error.message));
            button.disabled = false;
          }
        }, 700);
      } catch (error) {
        fill(result, el('p', { class: 'error' }, error.message));
        button.disabled = false;
      }
    },
  }, 'Импортировать');

  fill(view,
    el('h2', {}, 'Импорт плейлистов'),
    ...folderSyncSections(),
    el('p', { class: 'muted small' },
      'Находит .m3u и .m3u8 во всех подпапках указанного каталога. ' +
      'Пустое поле — поиск по всей библиотеке.'),
    el('div', { class: 'form-grid' },
      el('label', {}, 'Папка с плейлистами'), dirInput,
      el('label', {}, 'Заменять префикс: было'), fromInput,
      el('label', {}, 'Заменять префикс: стало'), toInput,
    ),
    hint,
    el('div', { class: 'row' }, previewButton),
    result,
  );
}

function renderPreview(container, data, dir, jobId = null) {
  const playlists = data.playlists || [];
  if (!playlists.length) {
    fill(container, el('p', { class: 'muted' }, 'Плейлистов не найдено'));
    return;
  }

  // Состояние решений по каждому файлу.
  const decisions = new Map();
  playlists.forEach((pl) => {
    decisions.set(pl.path, {
      action: pl.existing_id ? 'merge' : 'create',
      is_public: false,
      resolved: {},
    });
  });

  const summary = el('div', { class: 'row' },
    el('span', { class: 'small' },
      `Файлов: ${data.total_files} · сопоставлено треков: ${data.total_matched} · ` +
      `не найдено: ${data.total_unmatched}`),
  );

  const rows = playlists.map((pl) => {
    const decision = decisions.get(pl.path);
    // «Ещё не обработан» — жёлтый знак вопроса (CSS .import-status.pending).
    const statusIcon = el('span', { class: 'import-status pending', title: 'Ещё не обработан' }, '');
    const statusText = el('span', { class: 'muted small' }, '');

    const actionSelect = el('select', {
      'aria-label': `Действие для ${pl.name}`,
      onchange: (event) => { decision.action = event.target.value; },
    },
      pl.existing_id
        ? el('option', { value: 'merge' }, 'Дополнить существующий')
        : null,
      pl.existing_id ? el('option', { value: 'replace' }, 'Заменить (удалить старый)') : null,
      el('option', { value: 'create' }, 'Создать новый'),
      el('option', { value: 'skip' }, 'Пропустить'),
    );
    if (pl.existing_id) actionSelect.value = 'merge';

    const publicBox = el('input', { type: 'checkbox' });
    publicBox.addEventListener('change', () => {
      decision.is_public = publicBox.checked;
    });

    // Таблица несопоставленных: выбор трека вручную.
    const unmatchedBlock = pl.unmatched_items?.length
      ? el('details', { class: 'unmatched' },
          el('summary', {},
            `Не найдено: ${pl.unmatched} — выбрать вручную`),
          el('div', { class: 'list' },
            ...pl.unmatched_items.map((item) => {
              const select = el('select', {
                'aria-label': `Трек для ${item.raw_path}`,
                onchange: (event) => {
                  if (event.target.value) {
                    decision.resolved[item.raw_path] = Number(event.target.value);
                  } else {
                    delete decision.resolved[item.raw_path];
                  }
                },
              },
                el('option', { value: '' }, '— не добавлять —'),
                // Причину совпадения показываем в подписи: иначе список из
                // похожих строк непонятно чем отличается.
                ...(item.candidates || []).map((cand) => el('option',
                  { value: String(cand.track_id) },
                  `${cand.artist || '?'} — ${cand.title}${cand.note ? ` (${cand.note})` : ''}`)),
              );
              return el('div', { class: 'row unmatched-row' },
                el('div', { class: 'track-main' },
                  el('span', { class: 'small ellipsis' }, item.raw_path.split('/').pop()),
                  el('span', { class: 'muted tiny ellipsis' }, item.reason),
                ),
                select,
              );
            }),
          ),
        )
      : null;

    return el('div', { class: 'import-card', dataset: { path: pl.path } },
      el('div', { class: 'row' },
        statusIcon,
        el('div', { class: 'track-main' },
          el('strong', { class: 'ellipsis' }, pl.name),
          // Метаданные в одну строку с путём: карточка ниже, читается так же.
          el('span', { class: 'muted tiny ellipsis' },
            pl.error
              ? pl.error
              : `${pl.matched} из ${pl.total} треков` +
                (pl.existing_id ? ` · уже есть: ${pl.existing_count}` : '') +
                ` · ${pl.path}`),
        ),
        statusText,
      ),
      pl.error ? null : el('div', { class: 'row import-controls' },
        el('label', { class: 'inline' }, 'Действие'), actionSelect,
        el('label', { class: 'inline' }, publicBox, 'публичный'),
      ),
      unmatchedBlock,
    );
  });

  const applyButton = el('button', {
    class: 'primary',
    onclick: async () => {
      applyButton.disabled = true;
      // Галочка/крестик по каждому файлу, как просили: во время работы
      // loader, после — результат.
      const cards = [...container.querySelectorAll('.import-card')];
      cards.forEach((card) => {
        const icon = card.querySelector('.import-status');
        if (icon) {
          icon.className = 'import-status pending';
          icon.title = 'Обрабатывается';
          icon.textContent = '';
        }
      });

      const payload = {
        dir,
        // job_id: применяем уже разобранные планы, не разбирая папку заново.
        job_id: jobId,
        items: playlists.map((pl) => ({
          path: pl.path,
          ...decisions.get(pl.path),
        })),
      };

      try {
        const data = await api('/api/web/playlists/import/apply', {
          method: 'POST', body: JSON.stringify(payload),
        });
        const byPath = new Map(data.results.map((r) => [r.path, r]));
        cards.forEach((card) => {
          const item = byPath.get(card.dataset.path);
          const icon = card.querySelector('.import-status');
          const text = card.querySelector('.muted.small');
          if (!icon) return;
          if (!item) { icon.textContent = '×'; return; }
          icon.textContent = item.status === 'error' ? '×'
            : item.status === 'skipped' ? '—' : '✓';
          icon.className = `import-status ${item.status}`;
          if (text) {
            text.textContent = item.status === 'error'
              ? item.error
              : item.status === 'merged' ? `добавлено ${item.added}`
              : item.tracks !== undefined ? `треков ${item.tracks}` : '';
          }
        });
        toast(`Готово: создано ${data.created}, дополнено ${data.merged}, ` +
              `ошибок ${data.failed}`, data.failed ? 'error' : 'ok');
      } catch (error) {
        cards.forEach((card) => {
          const icon = card.querySelector('.import-status');
          if (icon) { icon.textContent = '×'; icon.className = 'import-status error'; }
        });
        toast(error.message, 'error');
      } finally {
        applyButton.disabled = false;
      }
    },
  }, 'Применить');

  fill(container,
    summary,
    ...(data.warnings?.length
      ? [el('div', { class: 'warn small' }, data.warnings.join('; '))]
      : []),
    el('div', { class: 'import-list' }, ...rows),
    el('div', { class: 'row' }, applyButton),
  );
}
