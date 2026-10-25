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
        toast('Сканирование завершено', 'ok');
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        button.disabled = false;
        renderScan(view);
      }
    },
  }, label);

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
    liveErrors,
    actions,
    el('h3', {}, 'История'),
    rows.length ? el('div', { class: 'list' }, ...rows)
                : el('p', { class: 'muted' }, 'Запусков ещё не было'),
  );

  // Если скан уже идёт (запущен из другой вкладки) — подхватываем прогресс.
  if (status.current && status.current.status === 'running') {
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
      if (data.current.status !== 'running') {
        stopScanPolling();
        renderScan(view);
      }
    } catch (error) {
      stopScanPolling();
    }
  }, 1000);
}

function updateScanView(current) {
  const fillNode = document.querySelector('#scan-fill');
  const infoNode = document.querySelector('#scan-info');
  const errorsNode = document.querySelector('#scan-errors');
  if (!fillNode || !infoNode) return;

  const percent = current.percent ?? 0;
  fillNode.style.width = `${percent}%`;
  fillNode.classList.toggle('running', current.status === 'running');
  fillNode.classList.toggle('failed', current.status === 'failed');

  const labels = {
    idle: 'Готов к запуску',
    running: current.stage === 'walk' ? 'Обход каталога…' : 'Разбор тегов…',
    completed: 'Завершено',
    failed: 'Ошибка',
    cancelled: 'Отменено',
  };
  const pieces = [`${labels[current.status] || current.status} — ${percent}%`];
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

/* ---------- Подробный статус скана на главной ----------

   Пока идёт первый (или любой ручной) скан, главная показывает прогресс,
   а не бесконечное «Загрузка…»: пока библиотека не проиндексирована,
   основному интерфейсу просто нечего рисовать. Как только проход
   завершается, панель уступает место обычной главной. */

let homeScanTimer = null;

function stopHomeScan() {
  if (homeScanTimer) {
    clearInterval(homeScanTimer);
    homeScanTimer = null;
  }
}

// Возвращает true, если главную сейчас занимает панель статуса.
async function scanGate(view) {
  stopHomeScan();
  let status;
  try {
    status = await api('/api/web/scan/status');
  } catch (error) {
    return false;   // нет доступа/сети — пусть главная попробует сама
  }
  const current = status && status.current;
  if (!current || current.status !== 'running') return false;

  const box = el('div', { class: 'scan-status', id: 'scan-status' });
  drawScanStatus(box, current, status);

  fill(view,
    el('h2', {}, 'Идёт сканирование библиотеки'),
    el('p', { class: 'muted small' },
      'Главная откроется автоматически, когда проход закончится. ' +
      'Страницу можно не закрывать.'),
    box,
    el('div', { class: 'row' }, el('button', {
      class: 'ghost', onclick: () => setView('scan'),
    }, 'Подробнее о сканере')),
  );

  homeScanTimer = setInterval(async () => {
    if (state.view !== 'library') { stopHomeScan(); return; }
    let data;
    try {
      data = await api('/api/web/scan/status');
    } catch (error) {
      stopHomeScan();
      return;
    }
    if (!data.current || data.current.status !== 'running') {
      stopHomeScan();
      setView('library');   // проход закончился — показываем основной интерфейс
      return;
    }
    const node = document.querySelector('#scan-status');
    if (!node) { stopHomeScan(); return; }
    drawScanStatus(node, data.current, data);
  }, 1000);

  return true;
}

function drawScanStatus(node, current, status) {
  const percent = current.percent ?? 0;
  const stageLabels = {
    walk: 'обход каталога и подсчёт файлов',
    walk_done: 'файлы найдены, начинается разбор',
    parsing: 'разбор тегов',
    done: 'завершено',
  };
  const modeLabels = { full: 'полный', incremental: 'инкрементальный', watch: 'по изменениям' };
  const started = current.started_at
    ? String(current.started_at).replace('T', ' ').slice(0, 19)
    : '—';

  const rows = [
    ['Режим', modeLabels[current.mode] || current.mode || '—'],
    ['Этап', stageLabels[current.stage] || current.stage || 'подготовка'],
    ['Начало', started],
    ['Найдено файлов', String(current.seen ?? 0)],
    ['Нужно разобрать', String(current.to_parse ?? 0)],
    ['Разобрано', `${current.processed ?? 0} из ${current.to_parse ?? 0}`],
    ['Новых', String(current.added ?? 0)],
    ['Обновлено', String(current.updated ?? 0)],
    ['Без изменений', String(current.unchanged ?? 0)],
    ['Удалено', String(current.removed ?? 0)],
    ['Ошибок', String(current.failed ?? 0)],
  ];

  const watcher = status && status.watcher;
  const errors = current.recent_errors || [];
  // Та же таблица, что и в истории: «файл / причина».
  const errorBlock = errors.length
    ? el('div', {},
        el('h3', {}, `Ошибки (${current.failed || errors.length})`),
        scanErrorTable(errors, current.failed || errors.length),
      )
    : null;

  fill(node,
    el('div', { class: 'progress' },
      el('div', {
        class: 'progress-fill running',
        style: `width:${percent}%`,
      }),
    ),
    el('p', { class: 'scan-info' },
      `${percent}% · ${stageLabels[current.stage] || 'работа'} · разобрано ` +
      `${current.processed ?? 0} из ${current.to_parse ?? 0}`),
    el('div', { class: 'kv' }, ...rows.flatMap(([key, value]) => [
      el('span', { class: 'muted' }, key),
      el('span', { class: 'mono break' }, value),
    ])),
    watcher && watcher.state
      ? el('p', { class: 'muted small' }, `Наблюдатель: ${watcher.state}, в очереди ${watcher.pending ?? 0}`)
      : null,
    current.error ? el('p', { class: 'error small' }, current.error) : null,
    errorBlock,
  );
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

  const previewButton = el('button', {
    class: 'primary',
    onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      fill(result, el('p', { class: 'muted' }, 'Разбираю папку…'));
      try {
        await saveSettings();
        const data = await api(
          `/api/web/playlists/import/preview?dir=${encodeURIComponent(dirInput.value.trim())}`
        );
        renderPreview(result, data, dirInput.value.trim());
      } catch (error) {
        fill(result, el('p', { class: 'error' }, error.message));
      } finally {
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

function renderPreview(container, data, dir) {
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
    const statusIcon = el('span', { class: 'import-status' }, '');
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
                ...(item.candidates || []).map((cand) => el('option',
                  { value: String(cand.track_id) },
                  `${cand.artist || '?'} — ${cand.title}`)),
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
          el('span', { class: 'muted tiny ellipsis' }, pl.path),
        ),
        statusText,
      ),
      pl.error
        ? el('p', { class: 'error small' }, pl.error)
        : el('div', { class: 'row' },
            el('span', { class: 'small' },
              `треков ${pl.matched} из ${pl.total}` +
              (pl.existing_id ? ` · уже есть: ${pl.existing_count}` : '')),
          ),
      el('div', { class: 'row' },
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
        if (icon) icon.textContent = '…';
      });

      const payload = {
        dir,
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
