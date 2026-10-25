/* Импорт плейлиста из папки и ресинк с папкой.

   Блок 1: папка (/music/Дискография/Artist1) → один плейлист со всеми файлами.
   Блок 2: проверка плейлиста, созданного из папки, и подтверждение изменений.

   Прогресс: задача на сервере + опрос раз в 500 мс. Удаление из плейлиста
   выполняется только после превью и явного подтверждения. */

let folderPollTimer = null;

function stopFolderPolling() {
  if (folderPollTimer) {
    clearInterval(folderPollTimer);
    folderPollTimer = null;
  }
}

/* Опрашивает задачу, пока она не завершится. onUpdate получает данные задачи
   или, если сеть отвалилась, объект с state: 'error'. */
function pollFolderJob(jobId, onUpdate) {
  stopFolderPolling();
  folderPollTimer = setInterval(async () => {
    try {
      const job = await api(`/api/web/playlists/folder-jobs/${jobId}`);
      onUpdate(job);
      if (job.state !== 'running') stopFolderPolling();
    } catch (error) {
      stopFolderPolling();
      onUpdate({ state: 'error', error: error.message, stage: 'Связь с сервером потеряна' });
    }
  }, 500);
}

/* Полоса прогресса и строка статуса. update(job) обновляет оба. */
function jobProgress() {
  const fill = el('div', { class: 'progress-fill running' });
  const bar = el('div', { class: 'progress' }, fill);
  const info = el('div', { class: 'scan-info' });
  const wrap = el('div', { class: 'folder-progress hidden' }, bar, info);

  const update = (job) => {
    wrap.classList.remove('hidden');
    const percent = typeof job.percent === 'number' ? job.percent : 0;
    fill.style.width = `${job.state === 'done' ? 100 : percent}%`;
    fill.classList.toggle('running', job.state === 'running');
    fill.classList.toggle('failed', job.state === 'error');
    const parts = [];
    if (job.state === 'running') parts.push(job.stage || 'Работаю…');
    else if (job.state === 'done') parts.push('Готово');
    else parts.push(job.error || 'Ошибка');
    if (job.total) parts.push(`${job.done} из ${job.total}`);
    if (typeof job.elapsed_sec === 'number') parts.push(`${job.elapsed_sec} с`);
    info.textContent = parts.join(' · ');
  };
  return { node: wrap, update };
}

/* ---------- Блок 1: сборка плейлиста из папки ---------- */
function folderScanBlock() {
  const pathInput = el('input', {
    type: 'text', class: 'wide', value: '/music/',
    placeholder: '/music/Дискографии/Artist1',
    'aria-label': 'Путь к папке артиста',
  });
  const nameInput = el('input', {
    type: 'text', placeholder: 'Имя папки (по умолчанию)',
    'aria-label': 'Имя плейлиста',
  });
  const progress = jobProgress();
  const result = el('div', { class: 'import-result' });

  const startButton = el('button', {
    class: 'primary',
    onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      fill(result);
      try {
        const job = await api('/api/web/playlists/folder-scan', {
          method: 'POST',
          body: JSON.stringify({ folder: pathInput.value.trim(), name: nameInput.value.trim() || null }),
        });
        progress.update(job);
        pollFolderJob(job.id, (current) => {
          progress.update(current);
          if (current.state === 'running') return;
          button.disabled = false;
          if (current.state === 'error') {
            fill(result, el('p', { class: 'error' }, current.error || 'Ошибка сборки'));
            return;
          }
          const r = current.result || {};
          const lines = [
            `Плейлист «${r.name}»: ${r.matched} из ${r.files_found} файлов добавлено.`,
          ];
          if (r.unmatched_count) {
            lines.push(`Не найдено в медиатеке: ${r.unmatched_count}. Запустите сканирование сервера и повторите.`);
          }
          fill(result,
            el('p', { class: 'small' }, ...lines.map((text) => el('span', { class: 'block' }, text))),
            r.unmatched && r.unmatched.length
              ? el('div', { class: 'mono small path-cell' }, ...r.unmatched.slice(0, 20).map((p) => el('div', {}, p)))
              : null,
          );
        });
      } catch (error) {
        button.disabled = false;
        fill(result, el('p', { class: 'error' }, error.message));
      }
    },
  }, 'Собрать плейлист');

  return el('section', { class: 'folder-block' },
    el('h3', {}, 'Сборка плейлиста из папки'),
    el('p', { class: 'muted small' },
      'Скрипт рекурсивно обходит папку (1-й уровень) и все её подпапки (альбомы, синглы). ' +
      'Все найденные аудиофайлы объединяются в ОДИН общий плейлист. ' +
      'Имя плейлиста по умолчанию это имя корневой папки.'),
    el('div', { class: 'form-grid' },
      el('label', {}, 'Путь к папке'), pathInput,
      el('label', {}, 'Имя плейлиста'), nameInput,
    ),
    el('div', { class: 'row' }, startButton),
    progress.node,
    result,
  );
}

/* ---------- Блок 2: ресинк плейлиста с папкой ---------- */
let resyncPlanState = null;   // { token, plan } текущего превью

function renderResyncPlan(container, plan, token, playlistId) {
  resyncPlanState = { token, plan, playlistId };

  const summary = el('p', { class: 'small' },
    `Без изменений: ${plan.kept}. Перемещено: ${plan.relocate.length}. ` +
    `Добавится: ${plan.add.length}. К удалению: ${plan.remove.length}. ` +
    `Неоднозначно (не тронуто): ${plan.ambiguous.length}.`);

  const relocateList = plan.relocate.length
    ? el('div', { class: 'mono small path-cell' }, ...plan.relocate.map((item) =>
        el('div', {}, `#${item.position + 1}  ${item.old_path}  →  ${item.new_path}`)))
    : el('p', { class: 'muted small' }, 'Перемещённых треков нет.');

  const addList = plan.add.length
    ? el('div', { class: 'mono small path-cell' }, ...plan.add.map((item) =>
        el('div', {}, `+ ${item.path}`)))
    : el('p', { class: 'muted small' }, 'Новых файлов нет.');

  // Удаление: по умолчанию ничего не отмечено. Пользователь выбирает сам.
  const removeChecks = plan.remove.map((item) => {
    const box = el('input', { type: 'checkbox', value: String(item.link_id) });
    return { box, node: el('label', { class: 'check-row small' }, box,
      el('span', { class: 'mono' }, `#${item.position + 1} ${item.artist || ''} — ${item.title}`),
      el('span', { class: 'muted' }, ` (${item.old_path})`)) };
  });
  const removeBlock = plan.remove.length
    ? el('div', { class: 'remove-list' },
        el('p', { class: 'small error' },
          'Эти треки не найдены в медиатеке. Отметьте те, которые нужно удалить из плейлиста:'),
        ...removeChecks.map((c) => c.node))
    : el('p', { class: 'muted small' }, 'Треков для удаления нет.');

  const applyButton = el('button', {
    class: 'danger-solid',
    onclick: async (event) => {
      const selected = removeChecks.filter((c) => c.box.checked).map((c) => Number(c.box.value));
      const message = selected.length
        ? `Удалить из плейлиста ${selected.length} трек(ов)? Файлы на диске не изменятся.`
        : 'Применить перемещения и добавления? Удаления выполнены не будут.';
      const ok = await confirmDialog(message, { title: 'Применить изменения', okLabel: 'Применить' });
      if (!ok) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const data = await api(`/api/web/playlists/${playlistId}/resync/confirm`, {
          method: 'POST',
          body: JSON.stringify({ token, remove_link_ids: selected }),
        });
        toast(`Перемещено ${data.relocated}, добавлено ${data.added}, удалено ${data.removed}`, 'ok');
        fill(container, el('p', { class: 'small' },
          `Применено: перемещено ${data.relocated}, добавлено ${data.added}, удалено ${data.removed}.`));
        resyncPlanState = null;
      } catch (error) {
        button.disabled = false;
        toast(error.message, 'error');
      }
    },
  }, 'Применить');

  fill(container,
    summary,
    el('h4', {}, 'Перемещены в другую папку'), relocateList,
    el('h4', {}, 'Новые файлы в папке'), addList,
    el('h4', {}, 'Удаление'), removeBlock,
    plan.ambiguous.length
      ? el('p', { class: 'muted small' },
          `Неоднозначные совпадения не изменены: ${plan.ambiguous.map((a) => a.title).join(', ')}`)
      : null,
    el('div', { class: 'row' }, applyButton),
  );
}

function resyncBlock() {
  const select = el('select', { 'aria-label': 'Плейлист из папки' },
    el('option', { value: '' }, 'Загрузка…'));
  const progress = jobProgress();
  const result = el('div', { class: 'import-result' });

  const loadList = async () => {
    try {
      const data = await api('/api/web/folder-synced-playlists');
      const items = data.items || [];
      fill(select, ...(items.length
        ? items.map((p) => el('option', { value: String(p.id) }, `${p.name} — ${p.origin_folder_path}`))
        : [el('option', { value: '' }, 'Нет плейлистов, созданных из папки')]));
    } catch (error) {
      fill(select, el('option', { value: '' }, 'Не удалось загрузить список'));
    }
  };
  loadList();

  const checkButton = el('button', {
    class: 'primary',
    onclick: async (event) => {
      const playlistId = select.value;
      if (!playlistId) return;
      const button = event.currentTarget;
      button.disabled = true;
      fill(result);
      try {
        const job = await api(`/api/web/playlists/${playlistId}/resync/preview`, { method: 'POST' });
        progress.update(job);
        pollFolderJob(job.id, (current) => {
          progress.update(current);
          if (current.state === 'running') return;
          button.disabled = false;
          if (current.state === 'error') {
            // library_stale приходит с готовым текстом для пользователя.
            fill(result, el('p', { class: 'error' }, current.error || 'Ошибка проверки'));
            return;
          }
          renderResyncPlan(result, current.result.plan, current.result.token, Number(playlistId));
        });
      } catch (error) {
        button.disabled = false;
        fill(result, el('p', { class: 'error' }, error.message));
      }
    },
  }, 'Проверить изменения');

  return el('section', { class: 'folder-block' },
    el('h3', {}, 'Ресинк плейлиста с папкой'),
    el('p', { class: 'muted small' },
      'Сверяет плейлист с его папкой. Пропавшие треки сначала ищутся в медиатеке по новому пути, ' +
      'и только если их там нет — предлагаются к удалению.'),
    el('div', { class: 'form-grid' },
      el('label', {}, 'Плейлист'), select),
    el('div', { class: 'row' }, checkButton),
    progress.node,
    result,
  );
}

/* Блоки вставляются в раздел «Импорт плейлистов» перед существующим импортом m3u8. */
function folderSyncSections() {
  return [
    folderScanBlock(),
    resyncBlock(),
    el('h3', {}, 'Импорт .m3u / .m3u8'),
  ];
}
