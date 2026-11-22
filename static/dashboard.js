let _wasRunning = false;
let _archiverAbsent = false;
let _archiverAbsentPools = false;   // furry вимкнено — серій у своїй бібліотеці немає
const API = '';
let currentPage = 'overview';
let sseSource = null;
let _anyUsersExist = true;

// ── i18n: словники й t()/tr() — у static/i18n.js (переклади в locales/) ──

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-backdrop').classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}

function setLang(lang) {
  if (lang === _lang) return;
  try { localStorage.setItem('lang', lang); } catch {}
  // автопереклад не вміє «повертати» текст назад — простіше перезавантажити
  location.reload();
}

function applyI18n() {
  document.documentElement.lang = _lang;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    el.placeholder = t(el.dataset.i18nPh);
  });
  const ukBtn = document.getElementById('lang-uk'), enBtn = document.getElementById('lang-en');
  if (ukBtn && enBtn) {
    ukBtn.classList.toggle('btn-primary', _lang === 'uk');
    enBtn.classList.toggle('btn-primary', _lang === 'en');
  }
  const title = document.getElementById('topbar-title');
  if (title) title.textContent = t(`page.${currentPage}`) || currentPage;
  const viewBtn = document.getElementById('catalog-view-toggle');
  if (viewBtn) viewBtn.textContent = t(_catalogView === 'grid' ? 'view.grid' : 'view.list');
  if (typeof catalogRender === 'function' && currentPage === 'catalog') catalogRender();
}

// ── Nav ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => {
    showPage(el.dataset.page);
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    el.classList.add('active');
    closeSidebar();
  });
});

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  const el = document.getElementById('page-' + name);
  if (!el) return;
  el.classList.add('active');
  currentPage = name;
  // Сторінки застосунків мають назву виду app-<id>-<page> і власну назву
  // в меню — для них підпис беремо звідти, а не з перекладів ядра.
  const appItem = (typeof _appsUi !== 'undefined')
      ? (_appsUi.menu || []).find(m => `app-${m.app_id}-${m.page || m.id}` === name)
      : null;
  document.getElementById('topbar-title').textContent =
      appItem ? appItem.label : (t(`page.${name}`) || name);
  refreshCurrent();
}

function refreshCurrent() {
  if (currentPage === 'overview')       loadOverview();
  else if (currentPage === 'artists')   loadArtists();
  else if (currentPage === 'recent')    loadRecent();
  else if (currentPage === 'pending')   { pendingRefreshCurrentTab(); }
  else if (currentPage === 'gallery')   { galPopulateSourceOptions(); }
  else if (currentPage === 'config')    loadConfig();
  else if (currentPage === 'access-codes') { loadUsersFull(); loadExternalKeys(); loadExternalArchives(); }
  else if (currentPage === 'chat') { chatBackToList(); loadChatConversations(); }
  else if (currentPage === 'catalog')   { catalogPopulateSourceOptions(); catalogLoadExcluded(); catalogLoad(_catalogPath); }
  else if (currentPage === 'live')      { liveLoadStatus(); liveCheckBookmark(); }
  else if (currentPage === 'apps')      { if (typeof loadAppsPage === 'function') loadAppsPage(); }
  else if (currentPage === 'trash')     { loadTrashPage(); }
  else if (currentPage === 'modlog')    { modlogLoad(0); }
  else if (currentPage.startsWith('app-')) { if (typeof appsOpenPage === 'function') appsOpenPage(currentPage); }
}


// ── Overview ──────────────────────────────────────────────────────────────
async function loadOverview() {
  const errBanner = document.getElementById('db-err');
  try {
    const s = await fetch(`${API}/api/stats`).then(r => r.json());

    if (s.db_status !== 'online') {
      errBanner.textContent = `✗ ${t('ov.dbunavailable')}: ${s.error || ''}`;
      errBanner.classList.add('show');
      document.getElementById('db-dot').className = 'db-dot offline';
      document.getElementById('db-label').textContent = t('ov.dboffline');
      return;
    }
    errBanner.classList.remove('show');
    document.getElementById('db-dot').className = 'db-dot online';
    document.getElementById('db-label').textContent = 'MariaDB online';

    // Свої числа ядра
    document.getElementById('s-total').textContent = fmt(s.db_total);
    document.getElementById('s-24h').textContent   = fmt(s.db_last24h);

    // Картки застосунків: скільки прийшло, стільки й малюємо. Вимкнули
    // застосунок — його картки просто немає (а не «0», що вводило б в оману).
    const extra = document.getElementById('stats-app-cards');
    if (extra) {
      const cards = (s.cards || []).slice().sort((x, y) => (x.order ?? 99) - (y.order ?? 99));
      extra.innerHTML = cards.map(c => `
        <div class="stat-card">
          <div class="stat-label">${esc(pickLang(c.label))}</div>
          <div class="stat-value ${esc(c.color || '')}">${fmt(c.value)}</div>
          ${c.hint ? `<div class="stat-hint">${esc(pickLang(c.hint))}</div>` : ''}
        </div>`).join('');
    }
    const pendingCard = (s.cards || []).find(c => c.id === 'dup-pending');
    updatePendingBadge(pendingCard ? pendingCard.value : 0);

    // Блоки-таблиці застосунків (напр. «Топ авторів»)
    const blocksBox = document.getElementById('stats-app-blocks');
    if (blocksBox) {
      const blocks = s.blocks || [];
      blocksBox.innerHTML = blocks.map(bl => {
        const max = bl.rows && bl.rows.length ? Math.max(...bl.rows.map(r => r.value)) || 1 : 1;
        return `<div class="card">
          <div class="card-title">${esc(pickLang(bl.title))}</div>
          <table class="data-table"><tbody>${(bl.rows || []).map(r => `
            <tr><td style="width:100%">
              <div style="display:flex;justify-content:space-between;margin-bottom:3px">
                <span style="color:var(--text)">${esc(r.label)}</span>
                <span class="c-accent">${fmt(r.value)}</span>
              </div>
              <div style="height:4px;background:var(--surface2);border-radius:2px;overflow:hidden">
                <div style="height:100%;width:${Math.round(r.value / max * 100)}%;background:var(--accent)"></div>
              </div>
            </td></tr>`).join('') || '<tr><td class="muted">—</td></tr>'}</tbody></table>
        </div>`;
      }).join('');
    }

    // Формати файлів — дані ядра
    const fmts   = s.formats || [];
    const maxFmt = fmts.length ? Math.max(...fmts.map(f => f.cnt)) || 1 : 1;
    document.getElementById('formats-table').innerHTML = fmts.length
      ? fmts.map(f => `<tr><td style="width:100%">
          <div style="display:flex;justify-content:space-between;margin-bottom:3px">
            <span style="color:var(--text)">.${esc(f.ext)}</span>
            <span style="color:var(--muted)">${fmt(f.cnt)}</span>
          </div>
          <div class="mini-bar-wrap"><div class="mini-bar" style="width:${Math.round(f.cnt/maxFmt*100)}%;background:var(--blue)"></div></div>
        </td></tr>`).join('')
      : `<tr><td class="empty-row">${t('ov.nodata')}</td></tr>`;

  } catch(e) {
    errBanner.textContent = `✗ ${t('ov.connerror')}: ${e.message}`;
    errBanner.classList.add('show');
    document.getElementById('db-dot').className = 'db-dot offline';
    document.getElementById('db-label').textContent = t('ov.dboffline');
  }

  // Recent
  try {
    const rows = await fetch(`${API}/api/recent?limit=10`).then(r => r.json());
    document.getElementById('overview-recent').innerHTML = rows.map(r => `<tr>
      <td class="furry-col">${esc(r.artist_name ?? "")}</td>
      <td class="furry-col" style="color:var(--muted)">${r.post_id ? esc(r.post_id) : '—'}</td>
      <td class="furry-col">${r.source ? `<span class="badge badge-${r.source}">${r.source}</span>` : '<span class="muted">—</span>'}</td>
      <td>${r.filename||'—'}</td>
      <td style="color:var(--muted)">${fmtDate(r.downloaded_at)}</td>
    </tr>`).join('') || `<tr><td colspan="5" class="empty-row">${t('empty.records')}</td></tr>`;
  } catch {}
}

function updatePendingBadge(n) {
  // Значок висить на вкладці "На перевірку", а її дає застосунок
  // дублікатів. Немає застосунку — немає вкладки й значка, і це
  // не привід валити весь огляд (раніше саме через це з'являлось
  // "Помилка зʼєднання: Cannot set properties of null" і БД
  // показувалась як offline, хоча з нею все гаразд).
  const b = document.getElementById('pending-badge');
  if (!b) return;
  b.textContent = n > 99 ? '99+' : n;
  b.classList.toggle('show', n > 0);
}

// ── Artists ───────────────────────────────────────────────────────────────
let _artistsSort = 'files';
let _lastArtistsData = [];
async function artistsBulkAutoUpdate(enabled) {
  const label = enabled ? 'увімкнути авто-оновлення для ВСІХ авторів' : 'вимкнути авто-оновлення для ВСІХ авторів';
  if (!confirm(`Точно ${label}?`)) return;
  try {
    const resp = await fetch(`${API}/api/artists/auto-update-bulk`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled })
    }).then(r => r.json());
    if (resp.error) { toast(resp.error, 'err'); return; }
    toast(`${enabled ? 'Увімкнено' : 'Вимкнено'} для ${resp.count} авторів`, 'ok');
    loadArtists();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

// ── ♥ Улюблені автори (у кожного користувача свої) ──────────────────
// ★ — улюблений арт, ♥ — улюблений автор. Улюблені автори завжди вгорі:
// у каталозі, у вкладці «Автори», у виборі автора в «Артах».
let _favArtistFolders = new Set();

async function loadFavArtists() {
  try {
    const r = await fetch(`${API}/api/favorite-artists`);
    if (!r.ok) return;
    const rows = await r.json();
    _favArtistFolders = new Set((rows || []).map(a => a.folder_name));
  } catch {}
}

function favArtistHeart(on, attrs = '') {
  return `<span class="fav-artist${on ? ' on' : ''}" ${attrs} title="${on ? 'Улюблений автор — клік, щоб прибрати' : 'Зробити улюбленим автором'}">${on ? '♥' : '♡'}</span>`;
}

async function toggleFavArtist({ id, folder }, on) {
  try {
    const r = await fetch(id ? `${API}/api/artists/${id}/favorite` : `${API}/api/favorite-artists`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(id ? { fav: on } : { folder, fav: on })
    });
    const d = await r.json();
    if (!r.ok || d.error) { toast(d.error || 'Помилка', 'err'); return null; }
    toast(d.fav ? '♥ Улюблений автор' : 'Прибрано з улюблених авторів', 'ok');
    await loadFavArtists();
    // перемальовуємо все, де видно автора
    if (currentPage === 'catalog' && typeof catalogRender === 'function') catalogRender();
    if (currentPage === 'artists') loadArtists();
    if (typeof _galArtists !== 'undefined' && _galArtists.length) {
      const a = _galArtists.find(x => x.id === id || x.folder_name === folder);
      if (a) a.is_fav = d.fav;
      _galArtists.sort((x, y) => (y.is_fav === true) - (x.is_fav === true));
      galRenderArtistList(document.getElementById('gal-artist-search')?.value || '');
    }
    return d.fav;
  } catch { toast('Помилка мережі', 'err'); return null; }
}

async function galToggleFavArtist(id, el) {
  const on = !el.classList.contains('on');
  const res = await toggleFavArtist({ id }, on);
  if (res === null) return;
  el.classList.toggle('on', res); el.textContent = res ? '♥' : '♡';
  Object.values(_galMeta || {}).forEach(m => { if (m && m.artist_id === id) m.artist_fav = res; });
}

async function loadArtists() {
  const data = await fetch(`${API}/api/artists?sort=${_artistsSort}`).then(r => r.json()).catch(() => []);
  _lastArtistsData = data;
  document.getElementById('artists-tbody').innerHTML = (Array.isArray(data) ? data : []).map(a => `<tr data-artist-row-id="${a.id}" data-artist-row-name="${esc(a.artist_name)}"${a.is_fav ? ' class="fav-artist-row"' : ''}>
    <td style="font-weight:500;cursor:pointer">${favArtistHeart(a.is_fav, `data-fav-artist="${a.id}"`)} ${esc(a.artist_name)}${(a.aliases || []).length ? `<div class="muted" style="font-size:11px;font-weight:400" title="Інші назви автора">також: ${esc(a.aliases.join(', '))}</div>` : ''}</td>
    <td style="color:var(--muted)">${esc(a.folder_name)}</td>
    <td><span class="badge badge-e621">${a.e621_files}</span></td>
    <td><span class="badge badge-inkbunny">${a.ib_files}</span></td>
    <td class="c-accent">${a.total_files}</td>
    <td style="color:var(--muted)">${fmtSize(a.total_size)}</td>
    <td>${a.pending_count > 0 ? `<span class="c-red">⏳ ${a.pending_count}</span>` : '<span style="color:var(--muted)">—</span>'}</td>
    <td style="color:var(--muted)">${fmtDate(a.last_download)}</td>
    <td style="text-align:center" title="Авто-оновлення щодня"><input type="checkbox" data-auto-update-id="${a.id}" ${a.auto_update ? 'checked' : ''} style="cursor:pointer;width:16px;height:16px"></td>
    <td data-admin><span class="catalog-crumb" data-merge-id="${a.id}" data-merge-name="${esc(a.artist_name)}" title="Об'єднати з іншим автором">🔗</span>
        <span class="catalog-crumb" data-forget-id="${a.id}" data-forget-name="${esc(a.artist_name)}" title="Прибрати з БД, файли лишити на диску (для помилково проіндексованих папок)" style="margin-left:6px">🧹</span></td>
  </tr>`).join('') || `<tr><td colspan="10" class="empty-row">${t('empty.artists')}</td></tr>`;
}
function artistsSetSort(sort) {
  _artistsSort = sort;
  document.querySelectorAll('#artists-sort-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.sort === sort));
  loadArtists();
}
document.getElementById('artists-tbody')?.addEventListener('click', async e => {
  const heart = e.target.closest('[data-fav-artist]');
  if (heart) {
    e.stopPropagation();
    toggleFavArtist({ id: parseInt(heart.dataset.favArtist) }, !heart.classList.contains('on'));
    return;
  }
  const toggle = e.target.closest('[data-auto-update-id]');
  if (toggle) {
    e.stopPropagation();
    try {
      const resp = await fetch(`${API}/api/artists/${toggle.dataset.autoUpdateId}/auto-update`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled: toggle.checked })
      }).then(r => r.json());
      if (resp.error) { toast(resp.error, 'err'); toggle.checked = !toggle.checked; return; }
      toast(toggle.checked ? 'Авто-оновлення увімкнено' : 'Авто-оновлення вимкнено', 'ok');
    } catch {
      toast('Помилка мережі', 'err');
      toggle.checked = !toggle.checked;
    }
    return;
  }
  const mergeBtn = e.target.closest('[data-merge-id]');
  if (mergeBtn) { artistsShowMergeModal(mergeBtn.dataset.mergeId, mergeBtn.dataset.mergeName); return; }
  const forgetBtn = e.target.closest('[data-forget-id]');
  if (forgetBtn) {
    e.stopPropagation();
    artistForgetFromDb(forgetBtn.dataset.forgetId, forgetBtn.dataset.forgetName);
    return;
  }
  const row = e.target.closest('[data-artist-row-id]');
  if (row) loadArtistDetail(row.dataset.artistRowId, row.dataset.artistRowName);
});

async function artistForgetFromDb(artistId, artistName) {
  if (!confirm(`Прибрати "${artistName}" з БД? Файли на диску НЕ видаляються — тільки записи в архіві. Для випадково проіндексованих особистих папок.`)) return;
  try {
    const r = await fetch(`${API}/api/artists/${artistId}/forget`, { method: 'DELETE' }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    toast(`"${r.removed}" прибрано з БД`, 'ok');
    loadArtists();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

let _mergeSourceId = null;

function artistsShowMergeModal(sourceId, sourceName) {
  _mergeSourceId = String(sourceId);
  document.getElementById('merge-source-name').textContent = sourceName;
  document.getElementById('merge-search').value = '';
  document.getElementById('merge-status').textContent = '';
  artistsRenderMergeList('');
  document.getElementById('merge-modal').style.display = 'flex';
}

function artistsRenderMergeList(search) {
  const list = document.getElementById('merge-target-list');
  const qq = (search || '').toLowerCase();
  const candidates = _lastArtistsData.filter(a =>
    String(a.id) !== _mergeSourceId && a.artist_name.toLowerCase().includes(qq)
  ).slice(0, 30);
  list.innerHTML = candidates.length
    ? candidates.map(a => `<div class="artist-opt" data-target-id="${a.id}" data-target-name="${esc(a.artist_name)}">
        <span>${esc(a.artist_name)}</span><span style="color:var(--muted);font-size:11px">${a.total_files} файлів</span>
      </div>`).join('')
    : `<div style="color:var(--muted);font-size:12px;padding:8px">Нікого не знайдено</div>`;
}

document.getElementById('merge-search')?.addEventListener('input', e => artistsRenderMergeList(e.target.value));

async function artistsShowMergeLog(artistId) {
  try {
    const log = await fetch(`${API}/api/artists/${artistId}/merge-log`).then(r => r.json());
    if (!log.length) { toast('Журнал порожній', 'ok'); return; }
    const list = document.getElementById('merge-target-list');
    list.innerHTML = `<div style="font-size:11px;color:var(--muted);margin-bottom:6px">Залишено ← Прибрано (дублікат)</div>` +
      log.map(l => `<div style="padding:6px 8px;font-family:var(--mono);font-size:11px;border-bottom:1px solid var(--border)">
        <span style="color:var(--green)">${esc(l.kept_post_id)}</span> ← <span style="color:var(--red)">${esc(l.removed_post_id)}</span>
        <span style="color:var(--muted);float:right">d=${l.hamming_distance}</span>
      </div>`).join('');
    document.getElementById('merge-search').style.display = 'none';
  } catch {
    toast('Помилка завантаження журналу', 'err');
  }
}

document.getElementById('merge-target-list')?.addEventListener('click', async e => {
  const opt = e.target.closest('[data-target-id]');
  if (!opt) return;
  const targetId = opt.dataset.targetId;
  const targetName = opt.dataset.targetName;
  const sourceName = document.getElementById('merge-source-name').textContent;
  const asPool = document.getElementById('merge-as-pool').value.trim();
  const confirmMsg = asPool
    ? `Перенести "${sourceName}" як pool "${asPool}" всередину "${targetName}"? Поточний запис автора буде видалено. Це незворотньо.`
    : `Об'єднати "${sourceName}" в "${targetName}"? Усі файли перенесуться, поточний запис буде видалено. Це незворотньо.`;
  if (!confirm(confirmMsg)) return;

  const statusEl = document.getElementById('merge-status');
  statusEl.style.color = 'var(--muted)';
  statusEl.textContent = 'Переношу файли…';
  try {
    const r = await fetch(`${API}/api/artists/${_mergeSourceId}/merge`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ target_artist_id: targetId, target_subfolder: asPool || undefined })
    });
    const d = await r.json();
    if (!r.ok) { statusEl.style.color = 'var(--red)'; statusEl.textContent = d.error || 'Помилка'; return; }
    statusEl.style.color = 'var(--green)';
    statusEl.innerHTML = `✓ Перенесено ${d.moved} файлів${d.failed ? `, ${d.failed} з помилкою` : ''}` +
      (d.skipped_dup ? `<br>🔍 Знайдено ${d.skipped_dup} візуальних дублікатів (той самий арт під різними ID) — залишено по одній копії. <span class="catalog-crumb" onclick="artistsShowMergeLog(${targetId})">Показати журнал</span>` : '');
    toast('Автори об\'єднані', 'ok');
    setTimeout(() => { document.getElementById('merge-modal').style.display = 'none'; loadArtists(); }, 1200);
  } catch {
    statusEl.style.color = 'var(--red)';
    statusEl.textContent = 'Помилка мережі';
  }
});

let _artistDetailId = null;
let _artistDetailName = null;

async function showPoolEditor() {
  if (!_artistDetailId) return;
  document.getElementById('pool-editor-modal').style.display = 'flex';
  await poolEditorLoadPoolNames();
  poolEditorLoad();
}

async function poolEditorLoadPoolNames() {
  const sel = document.getElementById('pool-editor-select');
  const currentVal = sel.value;
  try {
    const names = await fetch(`${API}/api/artists/${_artistDetailId}/pool-names`).then(r => r.json());
    Array.from(sel.querySelectorAll('option[data-pool]')).forEach(o => o.remove());
    if (Array.isArray(names)) {
      names.forEach(n => {
        const opt = document.createElement('option');
        opt.value = n; opt.dataset.pool = '1'; opt.textContent = `📖 ${n}`;
        sel.appendChild(opt);
      });
    }
    if (currentVal) sel.value = currentVal;
  } catch {}
}

function poolEditorNewPool() {
  const name = prompt('Назва нового pool:');
  if (!name) return;
  document.getElementById('pool-editor-target').value = name.trim();
  toast('Тепер обери роботи нижче й тисни "Перемістити в pool"', 'ok');
}

async function poolEditorLoad() {
  const subfolder = document.getElementById('pool-editor-select').value;
  const grid = document.getElementById('pool-editor-grid');
  grid.innerHTML = `<div style="color:var(--muted);font-size:12px">${t('common.loading')}</div>`;
  try {
    const files = await fetch(`${API}/api/artists/${_artistDetailId}/pool-editor?subfolder=${encodeURIComponent(subfolder)}`).then(r => r.json());
    if (!Array.isArray(files) || !files.length) {
      grid.innerHTML = `<div style="color:var(--muted);font-size:12px">Тут порожньо</div>`;
      return;
    }
    grid.style.display = 'grid';
    grid.innerHTML = files.map(f => `
      <div style="background:var(--surface2);border-radius:8px;padding:6px;display:flex;flex-direction:column;gap:4px">
        <div style="position:relative">
          <img src="${API}${f.stream_url}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;cursor:zoom-in" onclick="galOpenFullscreen('${API}${f.stream_url}')">
          <input type="checkbox" data-pool-file-id="${f.id}" style="position:absolute;top:4px;left:4px;width:16px;height:16px">
        </div>
        <div style="font-size:9.5px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(f.filename)}${f.linked ? ' — лежить в іншому пулі, сюди привʼязаний' : ''}">${f.linked ? '🔗 ' : ''}${esc(f.filename)}</div>
        <div style="display:flex;align-items:center;gap:4px">
          <span style="font-size:10px;color:var(--muted)">стор.</span>
          <input type="number" value="${f.pool_order ?? ''}" data-pool-order-id="${f.id}" onchange="poolEditorSetPage(${f.id}, this.value)"
                 style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:3px 6px;color:var(--text);font-family:var(--mono);font-size:11px">
        </div>
      </div>`).join('');
  } catch {
    grid.innerHTML = `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
}

async function poolEditorSetPage(downloadId, value) {
  try {
    await fetch(`${API}/api/downloads/${downloadId}/set-page`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ pool_order: value ? parseInt(value) : null,
                             pool: document.getElementById('pool-editor-select').value || null })
    });
    toast('Номер сторінки збережено', 'ok');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function poolEditorMoveSelected() {
  const poolName = document.getElementById('pool-editor-target').value.trim();
  if (!poolName) { toast('Впиши назву pool', 'err'); return; }
  const checked = Array.from(document.querySelectorAll('[data-pool-file-id]:checked')).map(el => parseInt(el.dataset.poolFileId));
  if (!checked.length) { toast('Обери хоч одну роботу', 'err'); return; }
  const status = document.getElementById('pool-editor-status');
  status.style.color = 'var(--muted)';
  status.textContent = 'Переношу файли…';
  try {
    const r = await fetch(`${API}/api/artists/${_artistDetailId}/pool-assign`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ download_ids: checked, pool_name: poolName })
    }).then(x => x.json());
    if (r.error) { status.style.color = 'var(--red)'; status.textContent = r.error; return; }
    status.style.color = 'var(--green)';
    status.textContent = `✓ Перенесено ${r.moved} файл(ів)`;
    document.getElementById('pool-editor-target').value = '';
    await poolEditorLoadPoolNames();
    document.getElementById('pool-editor-select').value = poolName;
    poolEditorLoad();
  } catch {
    status.style.color = 'var(--red)'; status.textContent = 'Помилка мережі';
  }
}

async function artistRunDuplicateScan() {
  if (!_artistDetailId) return;
  const btn = document.getElementById('artist-dup-scan-btn');
  const status = document.getElementById('artist-dup-scan-status');
  btn.disabled = true;
  status.textContent = 'Запускаю сканування…';
  try {
    const r = await fetch(`${API}/api/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ artist: _artistDetailName, find_duplicates: true })
    });
    const d = await r.json();
    if (!r.ok) { status.textContent = d.error || 'Помилка запуску'; btn.disabled = false; return; }
    status.innerHTML = `Сканування йде у фоні — стеж за прогресом у <span style="cursor:pointer;color:var(--accent2)" onclick="showPage('runner')">"Запуск"</span>, потім тисни ще раз, щоб оновити список нижче.`;
  } catch {
    status.textContent = 'Помилка мережі';
  }
  btn.disabled = false;
}

// «Перевірити пули» для одного автора: наявні файли — у пули, автоматичні
// відмови «вже є з іншого сайту» забуваються, і автор перекачується з
// перевагою сторінок пулів. Ручні рішення («Дублікат») лишаються.
async function artistRecheckPools() {
  if (!_artistDetailName) return;
  if (!confirm(`Перевірити пули автора «${_artistDetailName}»?\n\n` +
      `1. Наявні файли, що на сайті входять у пул, переїдуть у його теку.\n` +
      `2. Пости, які архіватор раніше сам відкинув як копію з іншого сайту, буде перевірено знову ` +
      `(їх доведеться скачати ще раз): сторінки пулів стануть на місце, звичайні копії знову відкинуться.\n\n` +
      `Те, що ти відхилив сам кнопкою «Дублікат», не чіпається. ` +
      `(Крім старих відмов, записаних до цього оновлення: там причина невідома, тож вони теж перевіряться — частина може знову з'явитись «На перевірку».)`)) return;
  const btn = document.getElementById('artist-pool-recheck-btn');
  const status = document.getElementById('artist-dup-scan-status');
  btn.disabled = true;
  status.textContent = 'Запускаю перевірку пулів…';
  try {
    const r = await fetch(`${API}/api/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ artist: _artistDetailName, recheck_pools: true })
    });
    const d = await r.json();
    if (!r.ok) { status.textContent = d.error || 'Помилка запуску'; btn.disabled = false; return; }
    status.innerHTML = `Перевірка пулів іде у фоні — прогрес у <span style="cursor:pointer;color:var(--accent2)" onclick="showPage('runner')">"Запуск"</span>. Заміни на користь пулів зробляться самі після завершення.`;
  } catch {
    status.textContent = 'Помилка мережі';
  }
  btn.disabled = false;
}

async function artistLoadDuplicates(id) {
  try {
    const rows = await fetch(`${API}/api/artists/${id}/duplicate-scan`).then(r => r.json());
    const panel = document.getElementById('artist-dup-results');
    if (!Array.isArray(rows) || !rows.length) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    panel.innerHTML = `<div style="font-weight:600;margin-bottom:8px">🔍 Знайдені пари-кандидати (${rows.length})</div>` +
      rows.map(r => `
        <div style="display:flex;align-items:center;gap:12px;padding:8px 10px;background:var(--surface2);border-radius:8px;margin-bottom:6px;font-family:var(--mono);font-size:12px">
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.filename_a)}${r.subfolder_a?` (${esc(r.subfolder_a)})`:''}</span>
          <span style="color:var(--muted)">d=${r.hamming_distance}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.filename_b)}${r.subfolder_b?` (${esc(r.subfolder_b)})`:''}</span>
          <button class="btn btn-ghost btn-sm" onclick="artistResolveDuplicate(${r.id}, 'delete_a')" title="Видалити перший, лишити другий">🗑 A</button>
          <button class="btn btn-ghost btn-sm" onclick="artistResolveDuplicate(${r.id}, 'delete_b')" title="Видалити другий, лишити перший">🗑 B</button>
          <button class="btn btn-ghost btn-sm" onclick="artistResolveDuplicate(${r.id}, 'dismiss')" title="Це не дублікат — лишити обидва">✕</button>
        </div>`).join('');
  } catch {}
}

async function artistResolveDuplicate(resultId, action) {
  if (action !== 'dismiss' && !confirm('Видалити цей файл остаточно (з диску й БД)? Це незворотньо.')) return;
  try {
    const r = await fetch(`${API}/api/duplicate-scan/${resultId}/resolve`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    toast('Готово', 'ok');
    artistLoadDuplicates(_artistDetailId);
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function artistLoadAliases(id) {
  const box = document.getElementById('artist-aliases');
  if (!box) return;
  let rows = [];
  try { rows = await fetch(`${API}/api/artists/${id}/aliases`).then(r => r.json()); } catch {}
  const mod = canModerate();
  box.innerHTML = `<span class="muted" style="font-size:12px">Інші назви:</span> ` +
    ((Array.isArray(rows) && rows.length) ? rows.map(a => `<span class="tag-chip" style="margin-right:4px">${esc(a.alias_name)}${mod ?
      ` <span style="cursor:pointer;opacity:.7" title="Прибрати" onclick="artistRemoveAlias(${id}, ${a.id}, '${esc(a.alias_name)}')">✕</span>` : ''}</span>`).join('')
      : '<span class="muted" style="font-size:12px">немає</span>') +
    (mod ? ` <span class="catalog-crumb" style="font-size:12px" onclick="artistAddAlias(${id})" title="Той самий автор під іншим ніком (на іншому сайті)">+ додати</span>` : '');
}

async function artistAddAlias(id) {
  const name = prompt('Інша назва цього автора (нік на іншому сайті):');
  if (!name || !name.trim()) return;
  const r = await fetch(`${API}/api/artists/${id}/aliases`, { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name: name.trim() }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || 'Не вдалося', 'err'); return; }
  toast('Назву додано', 'ok');
  artistLoadAliases(id);
}

async function artistRemoveAlias(id, aliasId, name) {
  if (!confirm(`Прибрати «${name}» з інших назв автора? Архіватор більше не шукатиме роботи під цим ніком.`)) return;
  await fetch(`${API}/api/artists/${id}/aliases/${aliasId}`, { method: 'DELETE' });
  artistLoadAliases(id);
}

async function loadArtistDetail(id, name, page=1) {
  showPage('artist-detail');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='artists'));
  _artistDetailId = id;
  _artistDetailName = name;
  document.getElementById('artist-detail-header').innerHTML = `
    <div class="card-title">${t('th.artist')}</div>
    <div style="font-size:18px;font-weight:600;font-family:var(--mono);color:var(--accent2)">${esc(name)}</div>
    <div id="artist-aliases" style="margin-top:8px"></div>`;
  artistLoadAliases(id);
  artistLoadDuplicates(id);
  const data = await fetch(`${API}/api/artist/${id}/downloads?page=${page}&limit=100`).then(r=>r.json()).catch(()=>({items:[],total:0}));
  document.getElementById('artist-detail-tbody').innerHTML = data.items.map(r => `<tr>
    <td class="furry-col" style="color:var(--muted)">${r.post_id ? esc(r.post_id) : '—'}</td>
    <td class="furry-col">${r.source ? `<span class="badge badge-${r.source}">${r.source}</span>` : '<span class="muted">—</span>'}</td>
    <td>${r.filename||'—'}</td>
    <td>${r.page_num}</td>
    <td><a href="${r.post_url}" target="_blank">↗</a></td>
    <td style="color:var(--muted)">${fmtDate(r.downloaded_at)}</td>
  </tr>`).join('') || `<tr><td colspan="6" class="empty-row">${t('empty.files')}</td></tr>`;
  const pages = Math.ceil(data.total/100);
  const pager = document.getElementById('artist-detail-pager');
  pager.dataset.artistId   = id;
  pager.dataset.artistName = name;
  pager.innerHTML = pages > 1
    ? `${t('pager.page')} ${page}/${pages}&nbsp;&nbsp;`
      + (page>1?`<span style="cursor:pointer;color:var(--accent2)" data-pager-page="${page-1}">${t('pager.prev')}</span> `:'')
      + (page<pages?`<span style="cursor:pointer;color:var(--accent2)" data-pager-page="${page+1}">${t('pager.next')}</span>`:'')
    : `${t('pager.total')}: ${data.total} ${t('pager.files')}`;
}
document.getElementById('artist-detail-pager')?.addEventListener('click', e => {
  const el = e.target.closest('[data-pager-page]');
  const pager = document.getElementById('artist-detail-pager');
  if (el) loadArtistDetail(pager.dataset.artistId, pager.dataset.artistName, parseInt(el.dataset.pagerPage, 10));
});

// ── Recent ────────────────────────────────────────────────────────────────
async function loadRecent() {
  const data = await fetch(`${API}/api/recent?limit=200`).then(r=>r.json()).catch(()=>[]);
  document.getElementById('recent-tbody').innerHTML = data.map(r => `<tr>
    <td class="furry-col">${esc(r.artist_name ?? "")}</td>
    <td class="furry-col" style="color:var(--muted)">${r.post_id ? esc(r.post_id) : '—'}</td>
    <td class="furry-col">${r.source ? `<span class="badge badge-${r.source}">${r.source}</span>` : '<span class="muted">—</span>'}</td>
    <td>${r.filename||'—'}</td>
    <td>${r.page_num}</td>
    <td><a href="${r.post_url}" target="_blank">↗</a></td>
    <td style="color:var(--muted)">${fmtDate(r.downloaded_at)}</td>
  </tr>`).join('') || `<tr><td colspan="7" class="empty-row">${t('empty.records')}</td></tr>`;
}

// ── Pending ───────────────────────────────────────────────────────────────
let _pendingTab = 'files';

function pendingSetTab(tab) {
  _pendingTab = tab;
  document.querySelectorAll('#pending-tab-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  document.getElementById('pending-list').style.display      = tab === 'files' ? '' : 'none';
  document.getElementById('duplicates-list').style.display   = tab === 'duplicates' ? '' : 'none';
  document.getElementById('tagsugg-list').style.display      = tab === 'tags' ? '' : 'none';
  document.getElementById('btn-reject-all').style.display    = tab === 'files' ? '' : 'none';
  const nearBtn = document.getElementById('btn-reject-near');
  if (nearBtn) nearBtn.style.display = (tab === 'files') ? '' : 'none';
  const scanBtn = document.getElementById('btn-scan-all');
  if (scanBtn) scanBtn.style.display = (tab === 'duplicates') ? '' : 'none';
  const autoBtn = document.getElementById('btn-auto-resolve');
  if (autoBtn) autoBtn.style.display = (tab === 'duplicates') ? '' : 'none';
  document.getElementById('btn-clear-dup').style.display     = tab === 'duplicates' ? '' : 'none';
  pendingRefreshCurrentTab();
}

function pendingRefreshCurrentTab() {
  if (_pendingTab === 'duplicates') loadDuplicatesTab();
  else if (_pendingTab === 'tags') loadTagSuggestions();
  else loadPending();
  tagSuggUpdateCount();
}

// ── Пропозиції тегів (вкладка «🏷 Теги») ─────────────────────────────────
const TAGSUGG_VERB = { add: '➕ додати', remove: '➖ прибрати', delete_tag: '🗑 видалити тег з архіву' };

async function tagSuggUpdateCount() {
  const el = document.getElementById('tagsugg-count');
  if (!el || !canModerate()) return;
  try {
    const d = await fetch(`${API}/api/tag-suggestions/count`).then(r => r.json());
    el.textContent = d.count ? `(${d.count})` : '';
  } catch {}
}

async function loadTagSuggestions() {
  const box = document.getElementById('tagsugg-list');
  box.innerHTML = `<div class="muted" style="padding:16px">${t('common.loading')}</div>`;
  let items = [];
  try { items = (await fetch(`${API}/api/tag-suggestions`).then(r => r.json())).items || []; }
  catch { box.innerHTML = `<div class="muted">${t('catalog.error')}</div>`; return; }
  document.getElementById('pending-count').textContent = items.length ? `${items.length} пропозицій` : '';
  if (!items.length) { box.innerHTML = '<div class="muted" style="padding:16px">Пропозицій немає.</div>'; return; }
  box.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th></th><th>Пропозиція</th><th>Файл</th><th>Хто</th><th></th></tr></thead>
    <tbody>${items.map(s => {
      const isImg = s.file_id && ['png','jpg','jpeg','gif','webp'].includes((s.file_ext||'').toLowerCase());
      return `<tr id="tagsugg-${s.id}">
        <td style="width:64px">${isImg ? `<img src="${API}/api/gallery/stream/${s.file_id}" loading="lazy" style="width:56px;height:56px;object-fit:cover;border-radius:6px;cursor:zoom-in" onclick="galOpenFullscreen(this.src)">` : ''}</td>
        <td>${TAGSUGG_VERB[s.action] || s.action} <b>${esc(s.tag_name)}</b>
            ${s.action === 'delete_tag' ? `<div class="muted" style="font-size:11px">стоїть на ${s.tag_usage||0} файл(ах)</div>` : ''}</td>
        <td style="font-family:var(--mono);font-size:12px">${s.file_id ? esc(s.file_name || ('#' + s.file_id)) : '—'}</td>
        <td style="font-size:12px">${esc(s.by)}<div class="muted" style="font-size:11px">${fmtDate(s.created_at)}</div></td>
        <td style="white-space:nowrap;text-align:right">
          ${s.can_decide
            ? `<button class="btn btn-approve btn-sm" onclick="tagSuggDecide(${s.id}, 'accept')">✓ Прийняти</button>
               <button class="btn btn-reject btn-sm" onclick="tagSuggDecide(${s.id}, 'reject')">✕ Відхилити</button>`
            : '<span class="muted" style="font-size:11px">вирішує адмін</span>'}
        </td></tr>`;
    }).join('')}</tbody></table></div>`;
}

async function tagSuggDecide(id, verdict) {
  try {
    const r = await fetch(`${API}/api/tag-suggestions/${id}/${verdict}`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.error || t('toast.error'), 'err'); return; }
    document.getElementById(`tagsugg-${id}`)?.remove();
    toast(verdict === 'accept' ? 'Прийнято' : 'Відхилено', 'ok');
    tagSuggUpdateCount();
    galLoadAllTags();
  } catch { toast('Помилка мережі', 'err'); }
}

let _dupTabAllRows = [];
let _dupTabShown = 0;
const DUP_TAB_PAGE_SIZE = 15;

async function loadDuplicatesTab() {
  const panel = document.getElementById('duplicates-list');
  panel.innerHTML = `<div style="color:var(--muted);font-family:var(--mono);font-size:13px;padding:20px 0">${t('common.loading')}</div>`;
  try {
    const rows = await fetch(`${API}/api/duplicate-scan`).then(r => r.json());
    if (_pendingTab === 'duplicates') document.getElementById('pending-count').textContent = `${Array.isArray(rows)?rows.length:0} ${t('pending.duppairslabel')}`;
    if (!Array.isArray(rows) || !rows.length) {
      panel.innerHTML = `<div class="pending-empty">
        <div class="pending-empty-icon">✓</div>
        <div class="pending-empty-title">${t('pending.allclean')}</div>
        <div class="pending-empty-sub">${t('pending.nodup')}</div>
      </div>`;
      return;
    }
    _dupTabAllRows = rows;
    _dupTabShown = 0;
    panel.innerHTML = '';
    dupTabRenderMore();
  } catch {
    panel.innerHTML = `<div style="opacity:.6;padding:20px">${t('catalog.error')}</div>`;
  }
}

function dupTabCardHtml(r) {
  const d   = r.hamming_distance ?? '?';
  const cls = d === 0 ? 'h0' : d <= 4 ? 'hlo' : 'hhi';
  const lbl = d === 0 ? t('pending.match100') : d <= 4 ? t('pending.verysimilar') : t('pending.similar');
  const isVidA = ['webm','mp4'].includes((r.ext_a||'').toLowerCase());
  const isVidB = ['webm','mp4'].includes((r.ext_b||'').toLowerCase());
  const urlA = `${API}/api/gallery/stream/${r.download_id_a}`;
  const urlB = `${API}/api/gallery/stream/${r.download_id_b}`;
  const mimeA = isVidA ? `video/${r.ext_a}` : `image/${r.ext_a}`;
  const mimeB = isVidB ? `video/${r.ext_b}` : `image/${r.ext_b}`;
  // Той самий preview-box + data-src/data-mime, що й у "Файли" — саме це
  // дає навігацію стрілочками між A/B у повноекранному перегляді
  // (openOverlayFromBox/overlayNav вже й так уміють це для парних боксів).
  const mediaA = isVidA
    ? `<video controls preload="none" src="${urlA}" style="max-width:100%;max-height:340px"></video>`
    : `<img loading="lazy" src="${urlA}" style="max-width:100%;max-height:340px;object-fit:contain;cursor:zoom-in" onclick="openOverlayFromBox(this.parentElement)">`;
  const mediaB = isVidB
    ? `<video controls preload="none" src="${urlB}" style="max-width:100%;max-height:340px"></video>`
    : `<img loading="lazy" src="${urlB}" style="max-width:100%;max-height:340px;object-fit:contain;cursor:zoom-in" onclick="openOverlayFromBox(this.parentElement)">`;
  return `<div class="review-card" id="dup-card-${r.id}">
    <div class="review-header">
      <div class="review-header-info">
        <div class="review-artist">↳ ${esc(r.artist_name)}</div>
        <div class="review-post-id">
          <span class="badge badge-${r.source_a}">${r.source_a}</span>&nbsp;${r.post_id_a}&nbsp;<a href="${r.post_url_a}" target="_blank" style="font-size:11px">↗</a>
          &nbsp;·&nbsp;
          <span class="badge badge-${r.source_b}">${r.source_b}</span>&nbsp;${r.post_id_b}&nbsp;<a href="${r.post_url_b}" target="_blank" style="font-size:11px">↗</a>
        </div>
      </div>
      <span class="hamming-badge ${cls}">d = ${d} &nbsp;·&nbsp; ${lbl}</span>
      <div class="review-header-actions">
        <button class="btn btn-approve" onclick="dupTabResolve(${r.id}, 'delete_b')" title="Лишити A, видалити B">✓ ${t('pending.keepa')}</button>
        <button class="btn btn-ghost"   onclick="dupTabResolve(${r.id}, 'delete_a')" title="Лишити B, видалити A" style="color:var(--yellow);border-color:rgba(251,191,36,.3)">↑ ${t('pending.keepb')}</button>
        <button class="btn btn-reject"  onclick="dupTabResolve(${r.id}, 'dismiss')" title="Це не дублікат">✕ ${t('pending.notdup')}</button>
      </div>
    </div>
    <div class="review-body">
      <div class="review-side">
        <div class="review-side-label label-new">A</div>
        <div class="preview-box" id="dupbox-a-${r.id}" data-src="${urlA}" data-mime="${mimeA}">${mediaA}</div>
        <div class="review-meta">
          <div class="review-meta-row"><span class="review-meta-key">${t('pending.file')}</span><span class="review-meta-val">${esc(r.filename_a)}</span></div>
          <div class="review-meta-row"><span class="review-meta-key">${t('pending.format')}</span><span class="review-meta-val">.${r.ext_a||'?'}</span></div>
          ${r.width_a ? `<div class="review-meta-row"><span class="review-meta-key">${t('pending.resolution')||'Розмір'}</span><span class="review-meta-val">${r.width_a}×${r.height_a}</span></div>` : ''}
          <div class="review-meta-row"><span class="review-meta-key">${t('pending.size')}</span><span class="review-meta-val">${r.size_a ? fmtSize(r.size_a) : '—'}</span></div>
          ${r.subfolder_a ? `<div class="review-meta-row"><span class="review-meta-key">Pool</span><span class="review-meta-val">${esc(r.subfolder_a)}</span></div>` : ''}
        </div>
      </div>
      <div class="review-side">
        <div class="review-side-label label-match">B</div>
        <div class="preview-box" id="dupbox-b-${r.id}" data-src="${urlB}" data-mime="${mimeB}">${mediaB}</div>
        <div class="review-meta">
          <div class="review-meta-row"><span class="review-meta-key">${t('pending.file')}</span><span class="review-meta-val">${esc(r.filename_b)}</span></div>
          <div class="review-meta-row"><span class="review-meta-key">${t('pending.format')}</span><span class="review-meta-val">.${r.ext_b||'?'}</span></div>
          ${r.width_b ? `<div class="review-meta-row"><span class="review-meta-key">${t('pending.resolution')||'Розмір'}</span><span class="review-meta-val">${r.width_b}×${r.height_b}</span></div>` : ''}
          <div class="review-meta-row"><span class="review-meta-key">${t('pending.size')}</span><span class="review-meta-val">${r.size_b ? fmtSize(r.size_b) : '—'}</span></div>
          ${r.subfolder_b ? `<div class="review-meta-row"><span class="review-meta-key">Pool</span><span class="review-meta-val">${esc(r.subfolder_b)}</span></div>` : ''}
        </div>
      </div>
    </div>
  </div>`;
}

function dupTabRenderMore() {
  const panel = document.getElementById('duplicates-list');
  document.getElementById('dup-load-more')?.remove();
  const next = _dupTabAllRows.slice(_dupTabShown, _dupTabShown + DUP_TAB_PAGE_SIZE);
  panel.insertAdjacentHTML('beforeend', next.map(dupTabCardHtml).join(''));
  _dupTabShown += next.length;
  if (_dupTabShown < _dupTabAllRows.length) {
    panel.insertAdjacentHTML('beforeend',
      `<div id="dup-load-more" style="text-align:center;padding:16px">
        <button class="btn btn-ghost" onclick="dupTabRenderMore()">↓ Показати ще (${_dupTabAllRows.length - _dupTabShown})</button>
      </div>`);
  }
}

async function dupTabClearAll() {
  if (!_dupTabAllRows.length) return;
  if (!confirm(`Очистити весь список (${_dupTabAllRows.length} пар)? Файли НЕ видаляються — це просто прибирає список кандидатів. Якщо ті самі пари підтвердяться повторним скануванням, вони можуть з'явитись знову.`)) return;
  try {
    const r = await fetch(`${API}/api/duplicate-scan/clear-all`, { method: 'POST' }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    toast(`Очищено ${r.count} пар`, 'ok');
    loadDuplicatesTab();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function dupTabResolve(resultId, action) {
  if (action !== 'dismiss' && !confirm('Видалити цей файл остаточно (з диску й БД)? Це незворотньо.')) return;
  try {
    const r = await fetch(`${API}/api/duplicate-scan/${resultId}/resolve`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    toast('Готово', 'ok');
    // Прибираємо тільки цю картку — не перезавантажуємо весь список, щоб
    // не втрачати позицію скролу й прогрес "показати ще"
    document.getElementById(`dup-card-${resultId}`)?.remove();
    _dupTabAllRows = _dupTabAllRows.filter(x => x.id !== resultId);
    if (_pendingTab === 'duplicates') {
      document.getElementById('pending-count').textContent = `${_dupTabAllRows.length} ${t('pending.duppairslabel')}`;
    }
    if (!_dupTabAllRows.length) {
      document.getElementById('duplicates-list').innerHTML = `<div class="pending-empty">
        <div class="pending-empty-icon">✓</div>
        <div class="pending-empty-title">${t('pending.allclean')}</div>
        <div class="pending-empty-sub">${t('pending.nodup')}</div>
      </div>`;
    }
  } catch {
    toast('Помилка мережі', 'err');
  }
}

// ── Черга на перевірку ────────────────────────────────────────────────
// Вантажиться сторінками. Раніше бралась уся черга одним запитом і
// малювалась цілком; на великій черзі це вішало браузер ще до того,
// як користувач встигав щось натиснути.
const PENDING_PAGE = 20;
let _pendingOffset = 0;
let _pendingTotal  = 0;
let _pendingBusy   = false;

// Пули файлу в картці перегляду. Одна робота може бути в кількох пулах
// (таблиця сторінок пулів) — показуємо всі; той, що зараз гортаєш, — першим.
function _galFilePools(d) {
  return Array.isArray(d?.pools) && d.pools.length ? d.pools.slice()
    : (d?.subfolder ? [{ name: d.subfolder, order: d.pool_order, total: d.pool_total, artist_id: d.artist_id }] : []);
}

// Куди веде P: не в пулі — у перший; уже в одному з пулів роботи — у наступний.
function galNextPool(d) {
  const pools = _galFilePools(d);
  if (!pools.length) return null;
  const cur = typeof _galSelectedPool !== 'undefined' ? _galSelectedPool : '';
  const i = pools.findIndex(p => p.name === cur);
  return pools[(i + 1) % pools.length];
}

function galPoolBadges(d) {
  const pools = _galFilePools(d);
  if (!pools.length) return '';
  const cur = typeof _galSelectedPool !== 'undefined' ? _galSelectedPool : '';
  const nextName = galNextPool(d)?.name;
  pools.sort((a, b) => (b.name === cur) - (a.name === cur));
  return pools.map((p, i) => `<div class="pool-progress-badge" data-pool="${esc(p.name)}" data-artist-id="${p.artist_id || d.artist_id}" data-artist-name="${esc(d.artist)}" title="${t('gal.filterbypool')}">
        <i>${p.linked ? '🔗' : '📖'}</i>
        <span class="pool-progress-title">${esc(p.name)}</span>
        <div class="pool-progress-bar"><div class="pool-progress-fill" style="width:${p.total ? Math.min(100, Math.round(((p.order||1)/p.total)*100)) : 0}%"></div></div>
        <span class="pool-progress-count">${t('gal.page')} ${p.order||'?'}${(p.order || 0) <= (p.total || 0) ? ` ${t('gal.of')} ${p.total}` : ''}${p.name === nextName && (pools.length > 1 || p.name !== cur) ? ' <span style="opacity:.5">(P)</span>' : ''}</span>
      </div>`).join('');
}

// Pool (серія) у парі: новий файл — сторінка pool на сайті, збережений лежить
// у pool в архіві. Від цього залежить, що станеться при кожній дії.
function _pendingPoolLabel(name, order) {
  return `«${esc(name)}»${order ? ` · стор. ${order}` : ''}`;
}

function pendingPoolNote(item) {
  const np = item.pool_folder, op = item.matched_subfolder;
  if (!np && !op) return '';
  const notes = [];
  if (item.pool_priority) {
    notes.push(`⇄ Новий файл — сторінка пулу ${_pendingPoolLabel(np, item.pool_order)}, а збережений лежить поза пулом. ` +
               `Заміниться автоматично після завершення архіватора (або натисни «${t('pending.replaceold')}»).`);
  } else {
    if (op) notes.push(`⚠ Збережений файл — сторінка пулу ${_pendingPoolLabel(op, item.matched_pool_order)}. ` +
                       `«${t('pending.replaceold')}» поставить новий на його місце в пулі.`);
    const inPools = item.matched_pools || (op ? [op] : []);
    if (np && !inPools.includes(np) && item.matched_file_id) {
      // одна робота — кілька пулів: при «Дублікат» збережений файл іде й у цей пул
      notes.push(`🔗 Новий файл — сторінка пулу ${_pendingPoolLabel(np, item.pool_order)} на сайті. ` +
                 `«${t('pending.duplicate')}» не втратить цю сторінку: збережений файл додасться й у цей пул, без копії.`);
    } else if (np && np !== op && !inPools.includes(np)) {
      notes.push(`⚠ Новий файл — сторінка пулу ${_pendingPoolLabel(np, item.pool_order)} на сайті. ` +
                 `Якщо позначити його як дублікат, цієї сторінки в пулі не буде.`);
    }
  }
  return `<div class="review-pool-note">${notes.map(n => `<div>${n}</div>`).join('')}</div>`;
}

function pendingCardHtml(item) {
    const d   = item.hamming_distance ?? '?';
    const cls = d === 0 ? 'h0' : d <= 4 ? 'hlo' : 'hhi';
    const lbl = d === 0 ? t('pending.match100') : d <= 4 ? t('pending.verysimilar') : t('pending.similar');
    return `<div class="review-card" id="rcard-${item.id}">
      <div class="review-header">
        <div class="review-header-info">
          <div class="review-artist">↳ ${esc(item.artist_name)}</div>
          <div class="review-post-id">
            <span class="badge badge-${item.source}">${item.source}</span>&nbsp;${item.post_id}${item.page_num>1?` · ${t('pager.page').toLowerCase()}.${item.page_num}`:''}&nbsp;<a href="${item.post_url}" target="_blank" style="font-size:11px">↗</a>
          </div>
        </div>
        <span class="hamming-badge ${cls}">d = ${d} &nbsp;·&nbsp; ${lbl}</span>
        <div class="review-header-actions">
          <button class="btn btn-approve" onclick="approveItem(${item.id})" title="${t('pending.title.approve')}">✓ ${t('pending.savenew')}</button>
          <button class="btn btn-ghost"   onclick="replaceItem(${item.id})" title="${t('pending.title.replace')}" style="color:var(--yellow);border-color:rgba(251,191,36,.3)">↑ ${t('pending.replaceold')}</button>
          <button class="btn btn-reject"  onclick="rejectItem(${item.id})" title="${t('pending.title.reject')}">✕ ${t('pending.duplicate')}</button>
        </div>
      </div>
      ${pendingPoolNote(item)}
      <div class="review-body">
        <div class="review-side">
          <div class="review-side-label label-new">⬆ ${t('pending.newfile')}</div>
          <div class="preview-box" id="pbox-new-${item.id}" data-id="${item.id}" data-which="new">
            <div class="preview-loading"><div class="gallery-spinner" style="width:24px;height:24px;border-width:2px"></div></div>
          </div>
          <div class="review-meta" id="meta-new-${item.id}">
            <div class="review-meta-row"><span class="review-meta-key">${t('pending.file')}</span><span class="review-meta-val">${item.filename||'—'}</span></div>
            <div class="review-meta-row"><span class="review-meta-key">${t('pending.format')}</span><span class="review-meta-val">.${item.file_ext||'?'}</span></div>
            <div class="review-meta-row"><span class="review-meta-key">${t('pending.size')}</span><span class="review-meta-val" id="size-new-${item.id}">…</span></div>
            ${item.pool_folder ? `<div class="review-meta-row"><span class="review-meta-key">📚 пул</span><span class="review-meta-val review-pool">${_pendingPoolLabel(item.pool_folder, item.pool_order)}</span></div>` : ''}
          </div>
        </div>
        <div class="review-side">
          <div class="review-side-label label-match">≈ ${t('pending.alreadysaved')}</div>
          <div class="preview-box" id="pbox-match-${item.id}" data-id="${item.id}" data-which="matched">
            <div class="preview-loading"><div class="gallery-spinner" style="width:24px;height:24px;border-width:2px"></div></div>
          </div>
          <div class="review-meta" id="meta-match-${item.id}">
            <div class="review-meta-row"><span class="review-meta-key">${t('th.folder')}</span><span class="review-meta-val">${esc(item.matched_folder_name || item.folder_name || '—')}</span></div>
            <div class="review-meta-row"><span class="review-meta-key">${t('pending.file')}</span><span class="review-meta-val">${item.matched_filename||'—'}</span></div>
            <div class="review-meta-row"><span class="review-meta-key">post</span><span class="review-meta-val">${item.matched_post_id||'—'}</span></div>
            <div class="review-meta-row"><span class="review-meta-key">${t('pending.size')}</span><span class="review-meta-val" id="size-match-${item.id}">…</span></div>
            ${item.matched_subfolder ? `<div class="review-meta-row"><span class="review-meta-key">📚 пул</span><span class="review-meta-val review-pool">${_pendingPoolLabel(item.matched_subfolder, item.matched_pool_order)}</span></div>` : ''}
            ${(item.matched_pools || []).filter(n => n !== item.matched_subfolder).map(n => `<div class="review-meta-row"><span class="review-meta-key">🔗 пул</span><span class="review-meta-val review-pool">«${esc(n)}»</span></div>`).join('')}
          </div>
        </div>
      </div>
    </div>`;
}

function pendingRenderInto(list, items, append) {
  const html = items.map(pendingCardHtml).join('');
  if (append) list.insertAdjacentHTML('beforeend', html);
  else        list.innerHTML = html;
  // Прев'ю тягнемо тільки для щойно доданих карток.
  items.forEach(item => {
    loadPreview(item.id, 'new',     document.getElementById(`pbox-new-${item.id}`));
    loadPreview(item.id, 'matched', document.getElementById(`pbox-match-${item.id}`));
  });
}

function pendingUpdateMoreBtn(list) {
  const old = document.getElementById('pending-more');
  if (old) old.remove();
  const left = _pendingTotal - _pendingOffset;
  if (left <= 0) return;
  const wrap = document.createElement('div');
  wrap.id = 'pending-more';
  wrap.style.cssText = 'padding:16px 0;text-align:center';
  wrap.innerHTML = `<button class="btn" onclick="pendingLoadMore()">
      ↓ Показати ще ${Math.min(PENDING_PAGE, left)} (лишилось ${left})
    </button>`;
  list.appendChild(wrap);
}

async function pendingFetchPage(offset) {
  const r = await fetch(`${API}/api/pending?limit=${PENDING_PAGE}&offset=${offset}`,
                        {credentials: 'include'}).then(x => x.json()).catch(() => null);
  if (!r) return {items: [], total: 0};
  // Старий сервер віддає плаский масив — не ламаємось на ньому.
  if (Array.isArray(r)) return {items: r.slice(0, PENDING_PAGE), total: r.length};
  if (r.error) { toast('✗ ' + r.error, 'err'); return {items: [], total: 0}; }
  return {items: r.items || [], total: r.total || 0};
}

async function loadPending() {
  const list    = document.getElementById('pending-list');
  const counter = document.getElementById('pending-count');
  list.innerHTML = `<div style="color:var(--muted);font-family:var(--mono);font-size:13px;padding:20px 0">${t('common.loading')}</div>`;

  _pendingOffset = 0;
  const {items, total} = await pendingFetchPage(0);
  _pendingTotal  = total;
  _pendingOffset = items.length;

  updatePendingBadge(total);
  counter.textContent = `${total} ${t('pending.countlabel')}`;
  document.getElementById('btn-reject-all').style.display = total ? '' : 'none';
  { const b = document.getElementById('btn-reject-near'); if (b) b.style.display = total ? '' : 'none'; }

  if (!total) {
    list.innerHTML = `<div class="pending-empty">
      <div class="pending-empty-icon">✓</div>
      <div class="pending-empty-title">${t('pending.allclean')}</div>
      <div class="pending-empty-sub">${t('pending.nodupes')}</div>
    </div>`;
    return;
  }

  pendingRenderInto(list, items, false);
  pendingUpdateMoreBtn(list);
}

async function pendingLoadMore() {
  if (_pendingBusy) return;
  _pendingBusy = true;
  const btn = document.querySelector('#pending-more button');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Вантажу…'; }
  try {
    const list = document.getElementById('pending-list');
    const {items, total} = await pendingFetchPage(_pendingOffset);
    if (total) _pendingTotal = total;
    _pendingOffset += items.length;
    if (!items.length) { _pendingTotal = _pendingOffset; }
    document.getElementById('pending-more')?.remove();
    pendingRenderInto(list, items, true);
    pendingUpdateMoreBtn(list);
  } finally {
    _pendingBusy = false;
  }
}

async function loadPreview(id, which, box) {
  if (!box) return;
  box.innerHTML = '<div class="preview-loading"><div class="gallery-spinner" style="width:24px;height:24px;border-width:2px"></div></div>';
  try {
    // Сервер віддає лише посилання й опис — не сам файл. Раніше тут
    // приходив увесь файл у base64: 40 файлів на сторінку, відео по десятки
    // мегабайт — і комп'ютер починав гальмувати.
    const r = await fetch(`${API}/api/pending/${id}/preview/${which}`);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      box.innerHTML = `<div class="preview-error">✗ ${esc(data.error||'Помилка')}<br><small style="color:var(--muted)">Файл може бути відсутній на диску</small></div>`;
      return;
    }
    const src = data.url ? `${API}${data.url}`
              : data.data ? `data:${data.mime};base64,${data.data}` : null;   // старий сервер
    if (!src) {
      box.innerHTML = `<div class="preview-error">✗ ${t('preview.nodata')}</div>`;
      return;
    }
    const isVideo = !!(data.mime && data.mime.startsWith('video/'));
    box.dataset.src   = src;
    box.dataset.mime  = data.mime || '';
    box.dataset.thumb = data.thumb_url ? `${API}${data.thumb_url}` : '';
    box.dataset.loaded = '1';

    if (isVideo) {
      // Відео: спершу кадр-мініатюра з ▶. Сам ролик вантажиться лише після
      // натискання — інакше 20 карток означали б 20–40 роликів одразу.
      box.innerHTML = `<div class="pending-video-poster" onclick="pendingPlayVideo(this.parentElement)" title="Відтворити">
          ${box.dataset.thumb ? `<img src="${box.dataset.thumb}" loading="lazy" onerror="this.remove()">` : ''}
          <span class="pending-play">▶</span>
        </div>`;
    } else {
      box.innerHTML = `<img src="${src}" loading="lazy" decoding="async" style="max-width:100%;max-height:340px;object-fit:contain;cursor:zoom-in" onclick="openOverlayFromBox(this.parentElement)">`;
    }

    const prefix = which === 'new' ? 'new' : 'match';
    const sizeEl = document.getElementById(`size-${prefix}-${id}`);
    if (sizeEl) sizeEl.textContent = data.file_size ? fmtSize(data.file_size) : '—';
  } catch(e) {
    box.innerHTML = `<div class="preview-error">✗ ${esc(e.message)}</div>`;
  }
}

function pendingPlayVideo(box) {
  if (!box || !box.dataset.src) return;
  box.innerHTML = `<video controls autoplay preload="metadata" src="${box.dataset.src}"
                          ${box.dataset.thumb ? `poster="${box.dataset.thumb}"` : ''}
                          style="max-width:100%;max-height:340px"></video>`;
}

function fmtSize(bytes) {
  if (!bytes) return '—';
  if (bytes < 1024)        return `${bytes} B`;
  if (bytes < 1024*1024)   return `${(bytes/1024).toFixed(1)} KB`;
  return `${(bytes/1024/1024).toFixed(2)} MB`;
}

let _overlayBoxes = [];   // [box-new, box-matched] для поточної pending карточки
let _overlayBoxIdx = 0;

function openOverlayFromBox(box) {
  if (!box.dataset.src) return;

  // Визначаємо чи це pending box — шукаємо парний
  const idMatch = box.id?.match(/^pbox-(new|match)-(\d+)$/);
  // Або картка дублікатів (Арти → На перевірку → Дублікати) — той самий
  // принцип, просто інші id/назви
  const dupMatch = box.id?.match(/^dupbox-(a|b)-(\d+)$/);
  if (idMatch) {
    const pendingId = idMatch[2];
    const boxNew   = document.getElementById(`pbox-new-${pendingId}`);
    const boxMatch = document.getElementById(`pbox-match-${pendingId}`);
    _overlayBoxes = [boxNew, boxMatch].filter(b => b?.dataset.src);
    _overlayBoxIdx = _overlayBoxes.indexOf(box);
    if (_overlayBoxIdx < 0) _overlayBoxIdx = 0;
  } else if (dupMatch) {
    const dupId = dupMatch[2];
    const boxA = document.getElementById(`dupbox-a-${dupId}`);
    const boxB = document.getElementById(`dupbox-b-${dupId}`);
    _overlayBoxes = [boxA, boxB].filter(b => b?.dataset.src);
    _overlayBoxIdx = _overlayBoxes.indexOf(box);
    if (_overlayBoxIdx < 0) _overlayBoxIdx = 0;
  } else {
    _overlayBoxes = [box];
    _overlayBoxIdx = 0;
  }

  _showOverlayBox(_overlayBoxes[_overlayBoxIdx]);
}

function _showOverlayBox(box) {
  if (!box?.dataset.src) return;
  const ov    = document.getElementById('overlay');
  const img   = document.getElementById('overlay-img');
  const vid   = document.getElementById('overlay-vid');
  const audio = document.getElementById('overlay-audio');
  img.style.display = 'none'; vid.style.display = 'none'; if (audio) audio.style.display = 'none';
  if (box.dataset.mime?.startsWith('video/')) {
    vid.style.display = ''; vid.src = box.dataset.src;
  } else if (box.dataset.mime?.startsWith('audio/') && audio) {
    audio.style.display = ''; audio.src = box.dataset.src;
  } else {
    img.style.display = ''; img.src = box.dataset.src;
  }
  // Підпис
  const lbl = document.getElementById('overlay-label');
  if (lbl && _overlayBoxes.length > 1) {
    if (box.isCatalog) {
      lbl.textContent = `${_overlayBoxIdx + 1} / ${_overlayBoxes.length}`;
    } else {
      const isDup = box.id?.startsWith('dupbox-');
      lbl.textContent = isDup
        ? (_overlayBoxIdx === 0 ? 'A' : 'B')
        : (_overlayBoxIdx === 0 ? '⬆ Новий файл' : '≈ Вже збережений');
    }
    lbl.style.display = 'block';
  } else if (lbl) {
    lbl.style.display = 'none';
  }
  ov.classList.add('open');
  _updateOverlayNav();
  catalogOverlayInfo(box);
}

function overlayNav(delta) {
  if (_overlayBoxes.length < 2) return;
  _overlayBoxIdx = (_overlayBoxIdx + delta + _overlayBoxes.length) % _overlayBoxes.length;
  _showOverlayBox(_overlayBoxes[_overlayBoxIdx]);
}

function _updateOverlayNav() {
  const prev = document.getElementById('overlay-nav-prev');
  const next = document.getElementById('overlay-nav-next');
  const show = _overlayBoxes.length > 1;
  if (prev) prev.style.display = show ? 'flex' : 'none';
  if (next) next.style.display = show ? 'flex' : 'none';
}

function closeOverlay() {
  document.getElementById('overlay').classList.remove('open');
  document.getElementById('overlay-vid').src = '';
  const audio = document.getElementById('overlay-audio');
  if (audio) audio.src = '';
  document.getElementById('overlay-nav-prev').style.display = 'none';
  document.getElementById('overlay-nav-next').style.display = 'none';
  const lbl = document.getElementById('overlay-label');
  if (lbl) lbl.style.display = 'none';
  const info = document.getElementById('overlay-info');
  if (info) { info.style.display = 'none'; info.innerHTML = ''; }
  _overlayBoxes = [];
}

async function replaceItem(id) {
  const card = document.getElementById(`rcard-${id}`);
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  card.style.opacity = '.5';
  const r = await fetch(`${API}/api/pending/${id}/replace`, {method:'POST'});
  if (r.ok) {
    const d = await r.json();
    toast(`↑ ${t('toast.replaced')}: ${d.replaced || '—'} → ${d.filename}` +
          (d.pool ? ` (пул «${d.pool}»${d.pool_order ? `, стор. ${d.pool_order}` : ''})` : ''), 'ok');
    fadeRemove(card);
    adjustCounter(-1);
  } else {
    const e = await r.json();
    toast(`✗ ${e.error}`, 'err');
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    card.style.opacity = '1';
  }
}

async function approveItem(id) {
  const card = document.getElementById(`rcard-${id}`);
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  card.style.opacity = '.5';
  const r = await fetch(`${API}/api/pending/${id}/approve`, {method:'POST'});
  if (r.ok) {
    const d = await r.json();
    toast(`✓ ${t('toast.saved')}: ${d.filename}`, 'ok');
    fadeRemove(card);
    adjustCounter(-1);
  } else {
    const e = await r.json();
    toast(`✗ ${e.error}`, 'err');
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    card.style.opacity = '1';
  }
}

async function rejectItem(id) {
  const card = document.getElementById(`rcard-${id}`);
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  card.style.opacity = '.35';
  const r = await fetch(`${API}/api/pending/${id}/reject`, {method:'POST'});
  if (r.ok) {
    const d = await r.json().catch(() => ({}));
    toast(d.linked_pool ? `${t('toast.rejected')} · 🔗 + пул «${d.linked_pool}»` : t('toast.rejected'), 'ok');
    fadeRemove(card);
    adjustCounter(-1);
  } else {
    const e = await r.json();
    toast(`✗ ${e.error}`, 'err');
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    card.style.opacity = '1';
  }
}

// ── Масове відхилення черги за відстанню ──────────────────────────────
// «Відхилити всі» рубає чергу наосліп. Тут спершу показуємо, з чого
// вона складається, і відхиляємо тільки однозначну частину.
async function rejectNearDuplicates() {
  let dist;
  try {
    dist = await fetch(`${API}/api/pending/distances`, {credentials:'include'}).then(r => r.json());
  } catch (e) { toast('✗ ' + e, 'err'); return; }
  if (dist.error) { toast('✗ ' + dist.error, 'err'); return; }
  if (!dist.length) { toast('Черга порожня', 'ok'); return; }

  let running = 0;
  const lines = dist.map(d => {
    running += d.count;
    const what = d.distance === 0 ? 'точна копія'
               : d.distance <= 2  ? 'те саме, інший сайт'
               : d.distance <= 4  ? 'майже те саме'
               : 'просто схоже';
    return `  ${d.distance}  —  ${String(d.count).padStart(6)}  (${what}), разом до цієї межі: ${running}`;
  }).join('\n');

  const ans = prompt(
    'Що зараз у черзі, за відстанню pHash:\n\n' + lines +
    '\n\nВідхилити все до якої відстані включно?\n' +
    'Файли ще не в архіві — вони лежать у pending_review, тож архів не постраждає.',
    '0');
  if (ans === null) return;
  const maxDistance = parseInt(ans);
  if (isNaN(maxDistance) || maxDistance < 0 || maxDistance > 20) { toast('Потрібне число 0–20', 'err'); return; }

  const pre = await fetch(`${API}/api/pending/reject-below`, {
    method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({max_distance: maxDistance, dry_run: true})
  }).then(r => r.json());
  if (pre.error) { toast('✗ ' + pre.error, 'err'); return; }
  const poolNote = pre.skipped_pool
    ? `\n\n${pre.skipped_pool} — сторінки пулів: їх не чіпаю, перевір вручну (позначені 📚).` : '';
  if (!pre.count) { toast('Під цю межу нічого не підпадає' + (pre.skipped_pool ? ` (крім ${pre.skipped_pool} сторінок пулів)` : ''), 'ok'); return; }
  if (!confirm(`Відхилити ${pre.count} з черги (відстань ≤ ${maxDistance})?${poolNote}`)) return;

  const r = await fetch(`${API}/api/pending/reject-below`, {
    method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({max_distance: maxDistance})
  }).then(x => x.json());
  if (r.error) { toast('✗ ' + r.error, 'err'); return; }
  toast(`Відхилено ${r.deleted}`, 'ok');
  loadPending();
}

async function rejectAll() {
  if (!confirm(t('confirm.rejectall'))) return;
  const r = await fetch(`${API}/api/pending/reject-all`, {method:'POST'});
  if (r.ok) { const d = await r.json(); toast(`${t('toast.rejectedn')} ${d.deleted}`, 'ok'); loadPending(); }
  else toast(`✗ ${t('toast.error')}`, 'err');
}

function fadeRemove(el) {
  el.style.transition = 'opacity .3s, max-height .4s, margin .4s, padding .4s';
  el.style.overflow   = 'hidden';
  el.style.opacity    = '0';
  el.style.maxHeight  = el.offsetHeight + 'px';
  setTimeout(() => { el.style.maxHeight = '0'; el.style.marginBottom = '0'; el.style.padding = '0'; }, 300);
  setTimeout(() => el.remove(), 700);
}

function adjustCounter(delta) {
  // Картку розібрали — вона зникла і зі сторінки, і з черги на сервері.
  // Тримаємо лічильники сторінкування в тому ж стані, інакше кнопка
  // «показати ще» починає брехати про залишок.
  if (typeof _pendingTotal === 'number')  _pendingTotal  = Math.max(0, _pendingTotal + delta);
  if (typeof _pendingOffset === 'number') _pendingOffset = Math.max(0, _pendingOffset + delta);
  const el = document.getElementById('pending-count');
  const m  = el.textContent.match(/\d+/);
  if (m) {
    const n = Math.max(0, parseInt(m[0]) + delta);
    el.textContent = `${n} файлів на перевірку`;
    updatePendingBadge(n);
    document.getElementById('btn-reject-all').style.display = n > 0 ? '' : 'none';
    { const b = document.getElementById('btn-reject-near'); if (b) b.style.display = n > 0 ? '' : 'none'; }
  }
}

// ── Gallery ───────────────────────────────────────────────────────────────
let _galIds      = [];
let _galIdx      = 0;
let _galMeta     = {};
let _galFilter   = 'all';
let _galArtistId = '';
let _galTagIds   = new Set();
let _allTags     = [];

function galSetFilter(f) {
  _galFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.filter === f));
  galLoadArtists(f);   // оновлюємо список авторів під поточний фільтр
  galShuffleFresh();   // і саму галерею — інакше показувало б стару сесію
}

let _galArtists = [];
let _galSelectedArtistId = '';

async function galLoadArtists(filter) {
  const f = filter || _galFilter || 'all';
  if (_galSource && _galSource !== 'local') {
    // підключений архів: його автори (номери — того архіву)
    const wrap = document.getElementById('gal-artist-btn')?.parentElement;
    if (_galSource === 'combined') { if (wrap) wrap.style.display = 'none'; return; }
    try {
      const r = await fetch(`${API}/api/external-archives/${_galSource}/artists?filter=${encodeURIComponent(f)}`).then(x => x.json());
      _galArtists = (r.unsupported || r.error) ? [] : (r.artists || []);
      if (wrap) wrap.style.display = _galArtists.length ? '' : 'none';
    } catch { _galArtists = []; }
    galRenderArtistList('');
    return;
  }
  try {
    const params = new URLSearchParams({ filter: f });
    _galTagIds.forEach(id => params.append('tag_id', id));
    // Автори — дані застосунку. Без нього фільтр за автором просто ховаємо.
    const resp = await fetch(`${API}/api/artists?${params}`);
    if (resp.status === 404) {
      _galArtists = [];
      // Ховаємо рівно кнопки авторів і серій (разом із їхніми випадайками),
      // а не весь рядок фільтрів: решта галереї — ядро, вона працює й так.
      ['gal-artist-btn', 'gal-pool-btn'].forEach(id => {
        const wrap = document.getElementById(id)?.parentElement;
        if (wrap) wrap.style.display = 'none';
      });
      return;
    }
    _galArtists = await resp.json();
    // Скидаємо вибір якщо вибраного автора немає в новому списку
    if (_galSelectedArtistId && !_galArtists.find(a => String(a.id) === _galSelectedArtistId)) {
      galSelectArtist('', t('gal.allartists.plain'));
    }
    galRenderArtistList('');
  } catch {}
}

function galRenderArtistList(search) {
  const list = document.getElementById('gal-artist-list');
  if (!list) return;
  const filtered = _galArtists.filter(a =>
    a.artist_name.toLowerCase().includes(search.toLowerCase())
  );
  const totalFiles = _galArtists.reduce((s,a)=>s+a.total_files,0);
  const favs = _galArtists.filter(a => a.is_fav);
  const favFiles = favs.reduce((s,a)=>s+a.total_files,0);
  list.innerHTML = `<div class="artist-opt ${_galSelectedArtistId===''?'active':''}" data-artist-id="" data-artist-label="${esc(t('gal.allartists.plain'))}">
    <span>${t('gal.allartists.plain')}</span><span style="color:var(--muted);font-size:11px;font-family:var(--mono)">${fmt(totalFiles)}</span>
  </div>` + (favs.length ? `<div class="artist-opt fav-all ${_galSelectedArtistId==='fav'?'active':''}" data-artist-id="fav" data-artist-label="♥ Улюблені автори">
    <span>♥ Улюблені автори <span class="muted">(${favs.length})</span></span><span style="color:var(--muted);font-size:11px;font-family:var(--mono)">${fmt(favFiles)}</span>
  </div>` : '') + filtered.map(a => `
    <div class="artist-opt ${String(_galSelectedArtistId)===String(a.id)?'active':''}${a.is_fav ? ' is-fav' : ''}" data-artist-id="${a.id}" data-artist-label="${esc(a.artist_name)}">
      <span>${favArtistHeart(a.is_fav, `data-fav-artist="${a.id}"`)} ${esc(a.artist_name)}</span>
      <span style="color:var(--muted);font-size:11px;font-family:var(--mono)">${fmt(a.total_files)}</span>
    </div>`).join('');
}
document.getElementById('gal-artist-list')?.addEventListener('click', e => {
  const heart = e.target.closest('[data-fav-artist]');
  if (heart) {
    e.stopPropagation();
    toggleFavArtist({ id: parseInt(heart.dataset.favArtist) }, !heart.classList.contains('on'));
    return;
  }
  const box = e.target.closest('[data-artist-id]');
  if (box) galSelectArtist(box.dataset.artistId, box.dataset.artistLabel);
});

function galSelectArtist(id, label) {
  _galSelectedArtistId = String(id);
  document.getElementById('gal-artist-label').textContent = id === 'fav' ? label : (id ? `👤 ${label}` : t('gal.allartists'));
  document.getElementById('gal-artist-picker').style.display = 'none';
  galRenderArtistList('');
  galSelectPool('', false);  // скидаємо пул — список пулів залежить від автора
  if (_galSource === 'local') galleryInit();
  else { galLoadPools(); galExternalShuffle(); }
}

function galToggleArtistPicker() {
  const p = document.getElementById('gal-artist-picker');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
  if (p.style.display === 'block') {
    document.getElementById('gal-artist-search').focus();
    galRenderArtistList('');
  }
}
document.addEventListener('click', e => {
  const p = document.getElementById('gal-artist-picker');
  const b = document.getElementById('gal-artist-btn');
  if (p && !p.contains(e.target) && e.target !== b && !b?.contains(e.target))
    p.style.display = 'none';
});
document.addEventListener('input', e => {
  if (e.target.id === 'gal-artist-search') galRenderArtistList(e.target.value);
});

// ── Пул (pool) ────────────────────────────────────────────────────────────
let _galPools = [];
let _galSelectedPool = '';
let _galExtPoolsToken = 0, _galExtListToken = 0, _galExtShowToken = 0;
let _galFavOnly = false;

async function galLoadPools() {
  // Чужа бібліотека: серії беремо в того архіву.
  if (_galSource && _galSource !== 'local' && _galSource !== 'combined') {
    const btnWrap = document.getElementById('gal-pool-btn')?.parentElement;
    const token = ++_galExtPoolsToken;
    try {
      const r = await fetch(`${API}/api/external-archives/${_galSource}/pools`).then(x => x.json());
      if (token !== _galExtPoolsToken) return;     // уже прийшов новіший запит
      if (r.unsupported || r.error) {
        // Стара версія на тому боці не вміє віддавати серії — ховаємо кнопку
        _galPools = [];
        if (btnWrap) btnWrap.style.display = 'none';
      } else {
        _galPools = r.pools || [];
        if (btnWrap) btnWrap.style.display = '';
      }
    } catch { _galPools = []; }
    galRenderPoolList('');
    return;
  }
  const localWrap = document.getElementById('gal-pool-btn')?.parentElement;
  if (localWrap && !_archiverAbsentPools) localWrap.style.display = '';
  try {
    const params = new URLSearchParams();
    if (_galSelectedArtistId) params.set('artist_id', _galSelectedArtistId);
    _galTagIds.forEach(id => params.append('tag_id', id));
    const pr = await fetch(`${API}/api/pools?${params}`);
    if (pr.status === 404) { _archiverAbsentPools = true; _galPools = []; if (localWrap) localWrap.style.display = 'none'; return; }
    _galPools = await pr.json();
    if (_galSelectedPool && !_galPools.find(p => p.name === _galSelectedPool)) {
      galSelectPool('');
    }
    galRenderPoolList('');
  } catch {}
}

function galRenderPoolList(search) {
  const list = document.getElementById('gal-pool-list');
  if (!list) return;
  const filtered = _galPools.filter(p =>
    p.name.toLowerCase().includes((search||'').toLowerCase())
  );
  if (!filtered.length && !search) {
    list.innerHTML = `<div style="padding:8px;color:var(--muted);font-family:var(--mono);font-size:12px">${t('gal.nopools')}</div>`;
    return;
  }
  const totalFiles = _galPools.reduce((s,p)=>s+p.cnt,0);
  const showArtist = !_galSelectedArtistId; // без фільтра автора — пули можуть збігатись назвою в різних авторів
  list.innerHTML = `<div class="pool-card ${_galSelectedPool===''?'active':''}" data-pool="" data-artist-id="">
    <i class="pool-card-icon">📚</i>
    <div class="pool-card-body">
      <div class="pool-card-title">${t('gal.allpools.plain')}</div>
      <div class="pool-card-sub">${fmt(totalFiles)} ${t('gal.pagesword')}</div>
    </div>
  </div>` + filtered.map(p => `
    <div class="pool-card ${_galSelectedPool===p.name && (!showArtist || _galSelectedArtistId==p.artist_id)?'active':''}" data-pool="${esc(p.name)}" data-artist-id="${p.artist_id}" data-artist-name="${esc(p.artist_name||'')}">
      <i class="pool-card-icon">📖</i>
      <div class="pool-card-body">
        <div class="pool-card-title">${esc(p.name)}</div>
        <div class="pool-card-sub">${fmt(p.cnt)} ${t('gal.pagesword')}${showArtist ? ` · ${esc(p.artist_name)}` : ''}</div>
      </div>
    </div>`).join('');
}
document.getElementById('gal-pool-list')?.addEventListener('click', e => {
  const box = e.target.closest('[data-pool]');
  if (box) galSelectPool(box.dataset.pool, true, box.dataset.artistId || null, box.dataset.artistName || '');
});
document.addEventListener('click', e => {
  const badge = e.target.closest('.pool-progress-badge');
  if (badge) galEnterPool(badge.dataset.pool, badge.dataset.artistId, badge.dataset.artistName);
});

let _galHistory = [];

let _galSource   = 'local';
let _galExtItems = [];
let _galExtIdx   = 0;

async function galPopulateSourceOptions() {
  const sel = document.getElementById('gal-source-select');
  if (!sel) return;
  const currentVal = sel.value;
  try {
    const archives = await fetch(`${API}/api/external-archives`).then(r => r.json());
    Array.from(sel.querySelectorAll('option[data-archive]')).forEach(o => o.remove());
    if (Array.isArray(archives)) {
      archives.forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.id;
        opt.dataset.archive = '1';
        opt.textContent = `🔗 ${a.label}`;
        sel.insertBefore(opt, sel.querySelector('option[value="combined"]'));
      });
    }
    if (currentVal) sel.value = currentVal;
  } catch {}
}

function galSourceChange() {
  _galSource = document.getElementById('gal-source-select').value;
  // Автор і серія належать тому архіву, де їх обрали: номери авторів у
  // кожної бази свої. Переходячи в інше джерело, скидаємо їх — інакше
  // чужому серверу пішов би номер нашого автора і фільтр вийшов би хибним.
  _galSelectedPool = '';
  _galSelectedArtistId = '';
  _galArtistId = '';
  const pl = document.getElementById('gal-pool-label');   if (pl) pl.textContent = t('gal.allpools');
  const al = document.getElementById('gal-artist-label'); if (al) al.textContent = t('gal.allartists');
  // теги, автори й серії — того архіву, який дивимось (номери в кожного свої)
  _galTagIds = new Set();
  _galHideTagIds = new Set();
  galRenderSelectedTagChips();
  galLoadAllTags();
  galLoadArtists(_galFilter);
  galLoadPools();
  if (_galSource === 'local') galleryInit();
  else galExternalShuffle();
}

async function galExternalShuffle() {
  const wrap   = document.getElementById('gallery-wrap');
  const status = document.getElementById('gallery-status');
  wrap.innerHTML = `<div class="gallery-loading"><div class="gallery-spinner"></div>${t('common.loading')}</div>`;
  status.textContent = '';
  // ID тегів різні в кожній базі — тому для зовнішніх архівів передаємо
  // НАЗВИ обраних тегів (перекладені з наших локальних ID через кеш
  // _allTags), а хост уже сам резолвить їх у свої власні ID.
  const tagNames     = [..._galTagIds].map(id => _allTags.find(t=>t.id===id)?.name).filter(Boolean).join(',');
  const hideTagNames = [..._galHideTagIds].map(id => _allTags.find(t=>t.id===id)?.name).filter(Boolean).join(',');
  const token = ++_galExtListToken;
  try {
    let items;
    if (_galSource === 'combined') {
      const params = new URLSearchParams();
      _galTagIds.forEach(id => params.append('tag_id', id));
      _galHideTagIds.forEach(id => params.append('hide_tag_id', id));
      if (tagNames)     params.set('tag_names', tagNames);
      if (hideTagNames) params.set('hide_tag_names', hideTagNames);
      const r = await fetch(`${API}/api/gallery/ids-combined?${params}`).then(r => r.json());
      items = r.items || [];
    } else {
      const params = new URLSearchParams();
      if (tagNames)     params.set('tag_names', tagNames);
      if (hideTagNames) params.set('hide_tag_names', hideTagNames);
      // Тип файлу (фото/відео/флеш) і серію теж передаємо — раніше вони
      // не відправлялись, і чужа бібліотека показувала все купою.
      params.set('filter', _galFilter || 'all');
      if (_galSelectedPool) params.set('subfolder', _galSelectedPool);
      // id автора — ЧУЖОГО архіву (з його списку авторів чи з серії)
      if (_galSelectedArtistId && _galSelectedArtistId !== 'fav') params.set('artist_id', _galSelectedArtistId);
      const r = await fetch(`${API}/api/external-archives/${_galSource}/gallery/ids?${params}`).then(r => r.json());
      if (token !== _galExtListToken) return;      // поки чекали, вибрали інше
      if (r.error) { wrap.innerHTML = `<div class="gallery-empty">✗ ${esc(r.error)}</div>`; return; }
      items = (r.ids || []).map(id => ({ source: _galSource, id }));
    }
    if (token !== _galExtListToken) return;
    if (!items.length) { wrap.innerHTML = `<div class="gallery-empty">${t('gal.nofiles')}</div>`; return; }
    _galExtItems = items;
    status.textContent = `${items.length.toLocaleString('uk-UA')} файлів`;
    galExternalShow(0);
  } catch (e) {
    wrap.innerHTML = `<div class="gallery-empty">✗ ${esc(e.message)}</div>`;
  }
}

async function galExternalShow(idx) {
  _galExtIdx = idx;
  const item = _galExtItems[idx];
  const wrap = document.getElementById('gallery-wrap');
  const token = ++_galExtShowToken;
  wrap.innerHTML = `<div class="gallery-loading"><div class="gallery-spinner"></div>${t('common.loading')}</div>
    <div class="gallery-nav prev" onclick="galExternalNav(-1)">‹</div>
    <div class="gallery-nav next" onclick="galExternalNav(1)">›</div>`;
  try {
    const metaUrl = item.source === 'local'
      ? `${API}/api/gallery/meta/${item.id}`
      : `${API}/api/external-archives/${item.source}/gallery/meta/${item.id}`;
    const d = await fetch(metaUrl).then(r => r.json());
    if (item.source !== 'local' && Array.isArray(d.tags)) _extTagsMerge(item.source, d.tags);   // теги — в кеш
    if (token !== _galExtShowToken) return;
    if (d.error) { wrap.innerHTML = `<div class="gallery-empty">✗ ${esc(d.error)}</div>`; return; }
    const isVideo  = ['webm','mp4'].includes((d.file_ext||'').toLowerCase());
    const mediaUrl = item.source === 'local' ? (d.share_url || d.stream_url) : `${API}${d.stream_url}`;
    const mediaEl  = isVideo
      ? `<video controls autoplay class="gallery-media" src="${mediaUrl}"></video>`
      : `<img class="gallery-media" src="${mediaUrl}" style="cursor:zoom-in" onclick="galOpenFullscreen('${mediaUrl.replace(/'/g,"\\'")}')">`;
    const favEl = item.source === 'local'
      ? `<span class="catalog-crumb" style="font-size:15px" onclick="galToggleFavorite(${item.id})" id="gal-fav-${item.id}">${d.is_favorite ? '★' : '☆'}</span>`
      : `<span class="catalog-crumb" style="font-size:15px" onclick="galExternalToggleFav('${item.source}', ${item.id}, this)">${(await _extFavIds(item.source)).has(item.id) ? '★' : '☆'}</span>`;
    wrap.innerHTML = `
      <div class="gallery-img-box">
        ${mediaEl}
        <div class="gallery-bottom-overlay">
          <div class="gallery-info-primary">
            <div class="gallery-info-left">
              ${favEl}
              <span class="gallery-artist-name">${esc(d.artist||'')}</span>
              ${item.source !== 'local' ? `<span class="badge" style="background:var(--accent2)">🔗 зовнішній</span>` : ''}
            </div>
            ${d.post_url ? `<a href="${d.post_url}" target="_blank" class="gallery-info-link">↗ ${t('gal.original')}</a>` : ''}
          </div>
          <div class="gallery-info-secondary">
            <span class="gallery-filename">${esc(d.filename)}</span>
            <span class="muted" style="font-size:11px">${idx + 1} / ${_galExtItems.length.toLocaleString('uk-UA')}</span>
          </div>
          ${item.source !== 'local' && item.source !== 'combined' ? galPoolBadges(d) : ''}
          ${d.tags?.length ? `<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">${d.tags.slice(0,30).map(tg=>`<span class="tag-chip">${esc(tg?.name ?? tg)}</span>`).join('')}</div>` : ''}
        </div>
      </div>
      <div class="gallery-nav prev" onclick="galExternalNav(-1)">‹</div>
      <div class="gallery-nav next" onclick="galExternalNav(1)">›</div>`;
  } catch (e) {
    wrap.innerHTML = `<div class="gallery-empty">✗ ${esc(e.message)}</div>`;
  }
}

function galExternalNav(delta) {
  const next = _galExtIdx + delta;
  if (next < 0 || next >= _galExtItems.length) return;
  galExternalShow(next);
}

const _extFavCache = {};
async function _extFavIds(archiveId) {
  if (!_extFavCache[archiveId]) {
    _extFavCache[archiveId] = fetch(`${API}/api/external-archives/${archiveId}/favorite-ids`)
      .then(r => r.json()).then(a => new Set(Array.isArray(a) ? a : [])).catch(() => new Set());
  }
  return _extFavCache[archiveId];
}

async function galExternalToggleFav(archiveId, remoteId, el) {
  try {
    const r = await fetch(`${API}/api/external-archives/${archiveId}/favorite`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ download_id: remoteId })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    if (el) el.textContent = r.is_favorite ? '★' : '☆';
    const set = await _extFavIds(archiveId);
    r.is_favorite ? set.add(remoteId) : set.delete(remoteId);
    toast(r.is_favorite ? '★ Додано в улюблене' : '☆ Прибрано з улюбленого', 'ok');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

function galShuffleFresh() {
  _galHistory = [];
  if (_galSource === 'local') galleryInit();
  else galExternalShuffle();
}

function galSnapshotState() {
  return {
    filter: _galFilter, artistId: _galSelectedArtistId, pool: _galSelectedPool,
    favOnly: _galFavOnly, tagIds: new Set(_galTagIds), hideTagIds: new Set(_galHideTagIds),
    ids: _galIds, idx: _galIdx,
  };
}

function galEnterPool(poolName, artistId = null, artistName = '') {
  _galHistory.push(galSnapshotState());
  galSelectPool(poolName, true, artistId, artistName);
}

function galGoBack() {
  const prev = _galHistory.pop();
  if (!prev) return;
  _galFilter = prev.filter;
  _galSelectedArtistId = prev.artistId;
  _galArtistId = prev.artistId;
  _galSelectedPool = prev.pool;
  _galFavOnly = prev.favOnly;
  _galTagIds = prev.tagIds;
  _galHideTagIds = prev.hideTagIds;
  _galTagsBeforePool = null;  // повертаємось повз pool-контекст — резервна копія більше не актуальна
  const lbl = document.getElementById('gal-pool-label');
  if (lbl) lbl.textContent = prev.pool ? `📖 ${prev.pool}` : t('gal.allpools');
  const favBtn = document.getElementById('gal-fav-filter-btn');
  if (favBtn) { favBtn.textContent = _galFavOnly ? '★' : '☆'; favBtn.classList.toggle('btn-primary', _galFavOnly); }
  _galIds = prev.ids;
  _galIdx = prev.idx;
  galleryShow(prev.idx);
}

let _galTagsBeforePool = null;  // резервна копія тегів на момент входу в pool

function galSelectPool(name, refresh = true, artistId = null, artistName = '') {
  // Скидати теги треба на БУДЬ-ЯКІЙ зміні пулу, а не лише на першому
  // вході. Раніше умова була `!_galSelectedPool`, тож перехід з одного
  // пулу в інший лишав старі теги активними — і новий pool показувався
  // наполовину порожнім, бо частина його робіт тегам не відповідає.
  const prevPool     = _galSelectedPool;
  const changingPool = !!name && name !== prevPool;
  const exitingPool  = !name && !!prevPool;

  _galSelectedPool = name;
  // Якщо обираємо конкретний pool зі списку "всі автори" (без фільтра автора) —
  // одразу застосовуємо і фільтр автора цього pool, інакше назви пулів можуть
  // збігатись у різних авторів, і галерея показала б суміш замість конкретного.
  // У підключеному архіві список серій — усіх авторів разом, тож автор
  // береться з обраної серії завжди (інакше лишався автор попередньої
  // серії, і нова показувалась порожньою).
  if (_galSource !== 'local' && artistId && name) {
    _galSelectedArtistId = String(artistId);
    const albl = document.getElementById('gal-artist-label');
    if (albl) albl.textContent = artistName ? `👤 ${artistName}` : t('gal.allartists');
  }
  if (name && artistId && !_galSelectedArtistId) {
    _galSelectedArtistId = String(artistId);
    _galArtistId = String(artistId);
    const albl = document.getElementById('gal-artist-label');
    if (albl) albl.textContent = artistName ? `👤 ${artistName}` : t('gal.allartists');
  }
  // Вхід у конкретний pool показує ВСЕ, що в ньому є — попередньо обрані
  // теги інакше лишались би активними і ховали частину робіт pool, які їм
  // не відповідають, хоча вони там реально є. Спрацьовує незалежно від
  // того, звідки обрано pool (бейдж, клавіша P, чи список пулів).
  if (changingPool) {
    // Резервну копію робимо лише при ПЕРШОМУ вході з загального перегляду:
    // інакше перехід між пулами затер би її порожнім набором, і вийти
    // назад з відновленими тегами вже не вийшло б.
    if (!prevPool) {
      _galTagsBeforePool = { tagIds: new Set(_galTagIds), hideTagIds: new Set(_galHideTagIds) };
    }
    _galTagIds = new Set();
    _galHideTagIds = new Set();
    galRenderSelectedTagChips();
    galRenderTagPicker();   // знімаємо й галочки у списку, не лише чипи
  } else if (exitingPool && _galTagsBeforePool) {
    // Повертаємось із pool назад у загальний перегляд — відновлюємо ті
    // теги, які були активні ДО входу (незалежно від того, вийшов ти через
    // Backspace чи явно обравши "Всі пули" в списку).
    _galTagIds = _galTagsBeforePool.tagIds;
    _galHideTagIds = _galTagsBeforePool.hideTagIds;
    _galTagsBeforePool = null;
    galRenderSelectedTagChips();
    galRenderTagPicker();
  }
  document.getElementById('gal-pool-label').textContent = name ? `📦 ${name}` : t('gal.allpools');
  document.getElementById('gal-pool-picker').style.display = 'none';
  galRenderPoolList('');
  if (refresh) {
    if (_galSource === 'local') galleryInit();
    else galExternalShuffle();   // серія чужого архіву — перезавантажуємо саме його
  }
}

function galTogglePoolPicker() {
  const p = document.getElementById('gal-pool-picker');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
  if (p.style.display === 'block') {
    document.getElementById('gal-pool-search').focus();
    galLoadPools();
  }
}
document.addEventListener('click', e => {
  const p = document.getElementById('gal-pool-picker');
  const b = document.getElementById('gal-pool-btn');
  if (p && !p.contains(e.target) && e.target !== b && !b?.contains(e.target))
    p.style.display = 'none';
});
document.addEventListener('input', e => {
  if (e.target.id === 'gal-pool-search') galRenderPoolList(e.target.value);
});

// Кеш тегів підключених архівів: {id архіву: Map(назва → тег)}. Наповнюється
// списком з того архіву, а якщо той старої версії й списку не віддає — тегами
// з кожного переглянутого файлу. Фільтр туди однаково йде НАЗВАМИ.
const _extTagCache = {};

function _extTagsMerge(archiveId, tags) {
  const m = _extTagCache[archiveId] || (_extTagCache[archiveId] = new Map());
  let added = false;
  for (const tg of tags || []) {
    const name = typeof tg === 'string' ? tg : tg?.name;
    if (!name) continue;
    const old = m.get(name);
    if (!old) {
      m.set(name, { id: `x:${name}`, name, category: tg.category || 'other', usage_count: tg.usage_count || 0 });
      added = true;
    } else if (tg.usage_count && tg.usage_count > (old.usage_count || 0)) {
      old.usage_count = tg.usage_count;
    }
  }
  if (added && _galSource === archiveId) {
    // вибрані теги лишаються вибраними: id = «x:назва», не залежить від порядку
    _allTags = [...m.values()];
    galRenderTagPicker();
  }
}

async function galLoadAllTags() {
  try {
    if (_galSource && _galSource !== 'local' && _galSource !== 'combined') {
      const aid = _galSource;
      _allTags = [...(_extTagCache[aid]?.values() || [])];        // що вже знаємо — одразу
      galRenderTagPicker();
      const r = await fetch(`${API}/api/external-archives/${aid}/tags`).then(x => x.json()).catch(() => ({}));
      if (!(r.unsupported || r.error)) _extTagsMerge(aid, r.tags || []);
      if (_galSource === aid) _allTags = [...(_extTagCache[aid]?.values() || [])];
    } else {
      _allTags = await fetch(`${API}/api/tags`).then(r => r.json());
    }
    galRenderTagPicker();
    galRenderAllTagsList();
  } catch {}
}

const TAG_CATS = ['character','species','general','other'];
function tagCatLabel(cat) { return t(`tagcat.${cat}`); }
const TAG_CAT_LABELS = new Proxy({}, { get: (_, cat) => tagCatLabel(cat) });

// Колапс груп — зберігаємо стан
const _galCatCollapsed = {character:false, species:false, general:false, other:false};
// Режим тегів: 'show' = показати тільки ці, 'hide' = виключити ці
let _galTagMode = 'show';
// Теги для виключення
let _galHideTagIds = new Set();

function galSetTagMode(mode) {
  _galTagMode = mode;
  document.getElementById('tag-mode-show').classList.toggle('active', mode === 'show');
  document.getElementById('tag-mode-hide').classList.toggle('active', mode === 'hide');
  galRenderTagPicker();
}

function galClearAllTags() {
  _galTagIds.clear();
  _galHideTagIds.clear();
  galRenderTagPicker();
  toast(t('toast.tagsreset'), 'ok');
}

function galToggleCat(cat) {
  _galCatCollapsed[cat] = !_galCatCollapsed[cat];
  galRenderTagPicker();
}

function tagChipHtml(t) {
  const sel = _galTagIds.has(t.id);
  const hideSel = _galTagMode === 'hide' && _galHideTagIds.has(t.id);
  const showSel = _galTagMode === 'show' && sel;
  const anySelected = showSel || hideSel;
  const bg  = showSel ? 'var(--accent)' : hideSel ? 'var(--red)' : 'transparent';
  const col = anySelected ? '#fff' : 'var(--muted)';
  const brd = showSel ? 'var(--accent)' : hideSel ? 'var(--red)' : 'var(--border)';
  const cnt = t.usage_count > 0 ? `<span style="opacity:.45;font-size:10px;margin-left:3px">${t.usage_count}</span>` : '';
  return `<span class="tag-chip ${anySelected?'selected':''}"
    style="background:${bg};color:${col};border-color:${brd}"
    data-tag-id="${esc(String(t.id))}" onclick="galToggleTag(isNaN(this.dataset.tagId) ? this.dataset.tagId : +this.dataset.tagId)">${esc(t.name)}${cnt}</span>`;
}

function galRenderTagPicker() {
  const search = (document.getElementById('gal-tag-search')?.value || '').toLowerCase().trim();
  const list   = document.getElementById('gal-tag-list');
  if (!list) return;

  // Не рендеримо ВЕСЬ список тегів одразу (це й гальмувало при великій їх
  // кількості) — тільки коли щось вводиш, показуємо кілька найкращих збігів
  if (!search) {
    list.innerHTML = `<div style="color:var(--muted);font-family:var(--mono);font-size:11px;padding:6px 2px">${t('gal.typetosearch')}</div>`;
  } else {
    const matches = _allTags
      .filter(tg => tg.name.includes(search))
      .sort((a,b) => (b.usage_count||0) - (a.usage_count||0))
      .slice(0, 25);
    list.innerHTML = matches.length
      ? `<div style="display:flex;flex-wrap:wrap;gap:5px">${matches.map(tg => tagChipHtml(tg)).join('')}</div>`
      : `<span style="color:var(--muted);font-family:var(--mono);font-size:12px">${t('gal.notagsfound')}</span>`;
  }
  galRenderSelectedTagChips();
}

function galRenderSelectedTagChips() {
  const el = document.getElementById('gal-tag-selected');
  if (!el) return;
  const ids = [..._galTagIds, ..._galHideTagIds];
  const chosen = ids.map(id => _allTags.find(tg => tg.id === id)).filter(Boolean);
  el.innerHTML = chosen.map(tg => tagChipHtml(tg)).join('');

  const total = _galTagIds.size + _galHideTagIds.size;
  const cnt = document.getElementById('gal-tag-count');
  if (cnt) cnt.textContent = total ? `(${total})` : '';
}

let _galTagDebounce = null;
function galToggleTag(id) {
  if (_galTagMode === 'show') {
    _galHideTagIds.delete(id); // не може бути в обох
    if (_galTagIds.has(id)) _galTagIds.delete(id);
    else _galTagIds.add(id);
  } else {
    _galTagIds.delete(id);
    if (_galHideTagIds.has(id)) _galHideTagIds.delete(id);
    else _galHideTagIds.add(id);
  }
  galRenderTagPicker();
  // Дебаунс — якщо клацаєш кілька тегів підряд, реальні запити на автора/
  // пул йдуть тільки один раз, після того як клацання припинились, а не
  // по два запити на КОЖЕН окремий клік (це й давало відчуття гальмування)
  clearTimeout(_galTagDebounce);
  _galTagDebounce = setTimeout(() => {
    galLoadArtists();  // автори без файлів під обрані теги зникають зі списку
    galLoadPools();    // так само і пули
  }, 350);
}

function galToggleTagPicker() {
  const p = document.getElementById('gal-tag-picker');
  const isOpen = p.style.display !== 'none';
  if (isOpen) {
    p.style.display = 'none';
  } else {
    p.style.display = 'block';
    document.getElementById('gal-tag-search').focus();
    galRenderTagPicker();
  }
}
// Закриваємо пікер тільки при кліку ПОЗА ним (mousedown щоб не конфліктувало з re-render)
document.addEventListener('mousedown', e => {
  const picker = document.getElementById('gal-tag-picker');
  const btn    = document.getElementById('gal-tag-btn');
  if (!picker || picker.style.display === 'none') return;
  if (!picker.contains(e.target) && !btn?.contains(e.target))
    picker.style.display = 'none';
});
// Зупиняємо propagation кліків всередині пікера щоб не закривалось при re-render
document.addEventListener('input', e => {
  if (e.target.id === 'gal-tag-search') galRenderTagPicker();
});

async function galleryInit() {
  const wrap   = document.getElementById('gallery-wrap');
  const status = document.getElementById('gallery-status');
  // Зберігаємо стан пікера щоб не закрився після перемішування
  const picker     = document.getElementById('gal-tag-picker');
  const pickerOpen = picker && picker.style.display !== 'none';

  wrap.innerHTML = `<div class="gallery-loading"><div class="gallery-spinner"></div>${t('common.loading')}</div>`;
  status.textContent = '';
  _galIds  = [];
  _galMeta = {};
  _galIdx  = 0;
  _galArtistId = _galSelectedArtistId;
  const params = new URLSearchParams({ filter: _galFilter });
  if (_galArtistId) params.set('artist_id', _galArtistId);
  if (_galSelectedPool) params.set('subfolder', _galSelectedPool);
  if (_galFavOnly) params.set('favorites', '1');
  _galTagIds.forEach(id => params.append('tag_id', id));
  _galHideTagIds.forEach(id => params.append('hide_tag_id', id));
  try {
    const r = await fetch(`${API}/api/gallery/ids?${params}`).then(r => r.json());
    if (!r.ids?.length) { wrap.innerHTML = `<div class="gallery-empty">${t('gal.nofiles')}</div>`; return; }
    _galIds = r.ids;
    const label = _galFilter==='image'?t('gal.photo.plain'):_galFilter==='video'?t('gal.video.plain'):t('gal.files.plain');
    status.textContent = `${_galIds.length.toLocaleString('uk-UA')} ${label}`;
    galleryShow(0);
  } catch(e) { wrap.innerHTML = `<div class="gallery-empty">✗ ${esc(e.message)}</div>`; }

  // Відновлюємо стан пікера
  if (pickerOpen && picker) picker.style.display = 'block';
}

async function galGetMeta(id) {
  if (_galMeta[id]) return _galMeta[id];
  const d = await fetch(`${API}/api/gallery/meta/${id}`).then(r => r.json());
  if (d.error) throw new Error(d.error);
  _galMeta[id] = d;
  return d;
}

async function galleryShow(idx) {
  if (!_galIds.length) return;
  idx = ((idx % _galIds.length) + _galIds.length) % _galIds.length;
  _galIdx = idx;
  const wrap = document.getElementById('gallery-wrap');
  const id   = _galIds[idx];
  if (!_galMeta[id]) {
    wrap.innerHTML = `<div class="gallery-img-box">
      <div class="gallery-counter">${idx+1} / ${_galIds.length.toLocaleString('uk-UA')}</div>
      <div class="gallery-loading"><div class="gallery-spinner"></div>Завантаження…</div>
      <div class="gallery-nav prev" onclick="galleryGo(-1)" style="position:absolute;left:-64px;top:calc(50% - 60px)">‹</div>
      <div class="gallery-nav next" onclick="galleryGo(1)"  style="position:absolute;right:-64px;top:calc(50% - 60px)">›</div>
    </div>`;
    try { await galGetMeta(id); }
    catch(e) {
      _galIds.splice(idx,1);
      if (_galIds.length) galleryShow(Math.min(idx,_galIds.length-1));
      else wrap.innerHTML=`<div class="gallery-empty">${t('gal.unavailable')}</div>`;
      return;
    }
  }
  const d       = _galMeta[id];
  const isVideo = d.is_video;
  const isFlash = d.file_ext === 'swf';
  const mediaSrc = d.share_url || d.stream_url;
  const mediaEl = isFlash
    ? `<div id="ruffle-container" class="gallery-media" style="background:#000;display:flex;align-items:center;justify-content:center;flex:1 1 auto;min-height:0;max-width:calc(100vw - 400px);width:min(920px, calc(100vw - 400px));height:min(600px, calc(100vh - 200px));border-radius:8px"></div>`
    : isVideo
    ? `<video src="${mediaSrc}" controls autoplay loop class="gallery-media" onvolumechange="galSaveVol(this)"></video>`
    : `<img src="${mediaSrc}" alt="${esc(d.filename)}" class="gallery-media" style="cursor:zoom-in" onclick="galOpenFullscreen('${mediaSrc.replace(/'/g,"\\'")}')">`;
  const tagsHtml = (() => {
    const tags = d.tags || [];
    if (!tags.length) return '';
    const groups = {};
    tags.forEach(t => {
      const cat = t.category || 'other';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(t);
    });
    return TAG_CATS.filter(c => groups[c]?.length).map(cat =>
      `<div class="tag-group" style="text-align:center">
        <div class="tag-group-label cat-${cat}">${TAG_CAT_LABELS[cat]}</div>
        <div class="tag-group-chips" style="justify-content:center">${groups[cat].map(t =>
          `<span class="tag-inline" onclick="galRemoveTag(${id},${t.id},this)">${esc(t.name)}<span style="margin-left:4px;opacity:.6">✕</span></span>`
        ).join('')}</div>
      </div>`
    ).join('');
  })();
  wrap.innerHTML = `<div class="gallery-img-box">
    <div class="gallery-backdrop" id="gal-backdrop"></div>
    <div class="gallery-counter">${idx+1} / ${_galIds.length.toLocaleString('uk-UA')}</div>
    ${mediaEl}
    <div class="gallery-bottom-overlay">
      <div class="gallery-info-primary">
        <div class="gallery-info-left">
          <span class="catalog-crumb" style="font-size:17px" onclick="galToggleFavorite(${id})" id="gal-fav-${id}" title="${t('gal.favorite')}">${d.is_favorite ? '★' : '☆'}</span>
          <span class="gallery-artist-name">${esc(d.artist)}</span>
          ${d.artist_id ? favArtistHeart(d.artist_fav, `onclick="galToggleFavArtist(${d.artist_id}, this)"`) : ''}
          ${d.source ? `<span class="badge badge-${d.source}">${d.source}</span>` : ''}
        </div>
        ${d.post_url ? `<a href="${d.post_url}" target="_blank" class="gallery-info-link">↗ ${t('gal.original')}</a>` : ''}
      </div>
      <div class="gallery-info-secondary">
        <span class="gallery-filename" title="${esc(d.filename)}">${esc(d.filename)}</span>
        ${isVideo?`<span class="gallery-info-hint">${t('gal.videohint')}</span>`:''}
        <span class="gallery-info-actions">
          ${canModerate() ? `<span class="catalog-crumb" onclick="galShowMetaEdit(${id})" title="${t('gal.edit')}">✎</span>` : ''}
          <span data-zone="file.actions" data-file_id="${id}"></span>
          ${_currentUser?.is_admin ? `<span class="catalog-crumb" onclick="galKeyAccess(${id})" title="Доступ для зовнішніх ключів">🔓</span>` : ''}
          ${canModerate() ? `<span class="catalog-crumb" style="color:var(--red)" onclick="galDeleteFile(${id})" title="${t('gal.delete')}">🗑</span>` : ''}
        </span>
      </div>
      ${galPoolBadges(d)}
      <div class="gallery-tags-toggle" onclick="galToggleTagsPanel(${id})">
        <span>🏷 ${(d.tags||[]).length} ${t('gal.tagscount')}</span>
        <span class="gal-tags-chevron">▾</span>
      </div>
      <div class="gallery-tags tags-collapsed" id="gal-tags-${id}">
        ${tagsHtml}
        <span class="tag-chip" style="background:transparent;color:var(--muted);border-color:var(--border)" onclick="galShowInlineTagInput(${id})">+ ${t('gal.tag')}</span>
      </div>
    </div>
    <div class="gallery-nav prev" onclick="galleryGo(-1)" style="position:absolute;left:-64px;top:calc(50% - 60px)">‹</div>
    <div class="gallery-nav next" onclick="galleryGo(1)"  style="position:absolute;right:-64px;top:calc(50% - 60px)">›</div>
  </div>`;
  if ((d.tag_suggestions || []).length) galRefreshFileTags(id);   // показати пропозиції тегів
  if (isFlash) {
    try {
      window.RufflePlayer = window.RufflePlayer || {};
      const ruffle = window.RufflePlayer.newest();
      const player = ruffle.createPlayer();
      player.style.width = '100%';
      player.style.height = '100%';
      const container = document.getElementById('ruffle-container');
      if (container) {
        container.appendChild(player);
        // Ruffle читає байти файлу через JS (не просто рендерить, як
        // <img>/<video>) — тому потребує CORS. Тому примусово через наш сервер
        player.load(d.stream_url);
      }
    } catch (e) {
      const container = document.getElementById('ruffle-container');
      if (container) container.innerHTML = `<span style="color:var(--muted);font-size:13px">Ruffle не завантажився: ${esc(String(e.message||e))}</span>`;
    }
  }
  if (!isVideo && !isFlash) {
    const bd = document.getElementById('gal-backdrop');
    if (bd) {
      const img = new Image();
      img.onload = () => { bd.style.backgroundImage = `url(${mediaSrc})`; bd.classList.add('loaded'); };
      img.src = mediaSrc;
    }
  }
  if (isVideo) {
    const vol=parseFloat(localStorage.getItem('gal_vol')??'1');
    const muted=localStorage.getItem('gal_muted')==='true';
    const vid=wrap.querySelector('video');
    if(vid){vid.volume=vol;vid.muted=muted;}
  }
  galPrefetchMeta(idx+1);
  galPrefetchMeta(idx-1);
  // картку щоразу малюють наново — домальовуємо кнопки застосунків
  if (typeof renderZones === 'function') renderZones(wrap.parentElement || document);
}

function galShowMetaEdit(downloadId) {
  const box = document.querySelector('.gallery-img-box');
  if (!box || box.querySelector('.gal-meta-edit')) return;
  const d = _galMeta[downloadId] || {};

  const wrap = document.createElement('div');
  wrap.className = 'gal-meta-edit';
  wrap.style.cssText = 'background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:10px;display:flex;flex-direction:column;gap:10px;max-width:420px;margin-left:auto;margin-right:auto';
  wrap.innerHTML = `
    <div style="font-family:var(--mono);font-size:12px;color:var(--muted)">${t('meta.title')}</div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:11px;color:var(--muted)">${t('th.source')}</label>
      <select id="meta-edit-source" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none">
        <option value="">— ${t('meta.unknown')} —</option>
        <option value="e621">e621</option>
        <option value="inkbunny">inkbunny</option>
        <option value="telegram">telegram</option>
      </select>
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:11px;color:var(--muted)">${t('meta.origurl')}</label>
      <input id="meta-edit-url" type="text" placeholder="https://…" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none">
    </div>
    <div style="display:flex;flex-direction:column;gap:4px">
      <label style="font-size:11px;color:var(--muted)">${t('meta.author')} <small style="opacity:.7">${t('meta.author.hint')}</small></label>
      <input id="meta-edit-artist" type="text" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-ghost btn-sm" onclick="this.closest('.gal-meta-edit').remove()">${t('meta.cancel')}</button>
      <button class="btn btn-primary btn-sm" onclick="galSaveMetaEdit(${downloadId})">${t('btn.save')}</button>
    </div>
  `;
  box.appendChild(wrap);
  document.getElementById('meta-edit-source').value = d.source || '';
  document.getElementById('meta-edit-url').value    = d.post_url || '';
  document.getElementById('meta-edit-artist').value = d.artist || '';
}

async function galSaveMetaEdit(downloadId) {
  const source = document.getElementById('meta-edit-source').value;
  const url    = document.getElementById('meta-edit-url').value.trim();
  const artist = document.getElementById('meta-edit-artist').value.trim();
  try {
    const r = await fetch(`${API}/api/download/${downloadId}/meta`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ source, post_url: url, artist_name: artist })
    });
    if (!r.ok) { toast(t('toast.saveerr'), 'err'); return; }
    delete _galMeta[downloadId];
    document.querySelector('.gal-meta-edit')?.remove();
    toast(t('toast.saved'), 'ok');
    await galGetMeta(downloadId);
    galleryShow(_galIdx);
  } catch (e) {
    toast(t('toast.saveerr'), 'err');
  }
}

async function galDeleteFile(id) {
  if (!confirm(t('gal.confirmdelete'))) return;
  try {
    const r = await fetch(`${API}/api/download/${id}`, {method:'DELETE'});
    if (!r.ok) { toast(t('toast.error'), 'err'); return; }
    toast(t('gal.deleted'), 'ok');
    const pos = _galIds.indexOf(id);
    if (pos !== -1) _galIds.splice(pos, 1);
    delete _galMeta[id];
    if (!_galIds.length) {
      document.getElementById('gallery-wrap').innerHTML = `<div class="gallery-empty">${t('gal.empty')}</div>`;
      return;
    }
    galleryShow(pos >= _galIds.length ? 0 : pos);
  } catch {
    toast(t('toast.error'), 'err');
  }
}

async function galToggleFavorite(id) {
  try {
    const r = await fetch(`${API}/api/download/${id}/favorite`, {method:'POST'}).then(r=>r.json());
    const el = document.getElementById(`gal-fav-${id}`);
    if (el) el.textContent = r.is_favorite ? '★' : '☆';
  } catch {}
}

function galToggleFavFilter() {
  _galFavOnly = !_galFavOnly;
  const btn = document.getElementById('gal-fav-filter-btn');
  if (btn) {
    btn.textContent = _galFavOnly ? '★' : '☆';
    btn.classList.toggle('btn-primary', _galFavOnly);
  }
  galleryInit();
}

function galToggleTagsPanel(id) {
  document.getElementById(`gal-tags-${id}`)?.classList.toggle('tags-collapsed');
}

async function galShowInlineTagInput(downloadId) {
  const container = document.getElementById(`gal-tags-${downloadId}`);
  if (!container || container.querySelector('input')) return;
  const inp = document.createElement('input');
  inp.placeholder=t('gal.tagellipsis');
  inp.style.cssText='background:var(--surface2);border:1px solid var(--accent);border-radius:12px;padding:3px 10px;font-family:var(--mono);font-size:11px;color:var(--text);outline:none;width:100px';
  const suggest = document.createElement('div');
  suggest.style.cssText='position:absolute;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:4px;z-index:60;min-width:140px;box-shadow:0 4px 12px rgba(0,0,0,.4);display:none';
  const wrap = document.createElement('div');
  wrap.style.cssText='position:relative;display:inline-block';
  wrap.appendChild(inp); wrap.appendChild(suggest);
  container.insertBefore(wrap, container.lastElementChild);
  inp.focus();
  inp.addEventListener('input', () => {
    const v = inp.value.trim().toLowerCase();
    if (!v) { suggest.style.display='none'; return; }
    const matches = _allTags.filter(t => t.name.includes(v)).slice(0,8);
    suggest.innerHTML = matches.map(t =>
      `<div style="padding:5px 8px;cursor:pointer;font-family:var(--mono);font-size:12px;border-radius:4px;display:flex;align-items:center;gap:6px"
        onmousedown="galAddTagToFile(${downloadId},'${t.name.replace(/'/g,"\\'")}','${t.color}');wrap_cleanup_${downloadId}()"
        onmouseover="this.style.background='var(--surface2)'" onmouseout="this.style.background=''">
        <span style="width:10px;height:10px;border-radius:50%;background:${t.color};display:inline-block"></span>${esc(t.name)}</div>`
    ).join('') + (v && !matches.find(t=>t.name===v)
      ? `<div style="padding:5px 8px;cursor:pointer;font-family:var(--mono);font-size:12px;border-radius:4px;color:var(--accent2)"
          onmousedown="galAddTagToFile(${downloadId},'${v}','#7c6af7');wrap_cleanup_${downloadId}()"
          onmouseover="this.style.background='var(--surface2)'" onmouseout="this.style.background=''">+ ${t('gal.createquoted')} «${esc(v)}»</div>` : '');
    suggest.style.display = 'block';
  });
  window[`wrap_cleanup_${downloadId}`] = () => wrap.remove();
  inp.addEventListener('keydown', async e => {
    if (e.key==='Enter') { const v=inp.value.trim().toLowerCase(); if(v) await galAddTagToFile(downloadId,v,'#7c6af7'); wrap.remove(); }
    if (e.key==='Escape') wrap.remove();
  });
  inp.addEventListener('blur', () => setTimeout(()=>wrap.remove(),200));
}

async function galAddTagToFile(downloadId, name, color) {
  try {
    const tag = await fetch(`${API}/api/download/${downloadId}/tags/add`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})
    }).then(r=>r.json());
    if (tag.error) { toast(`✗ ${tag.error}`, 'err'); return; }
    if (tag.suggested) {
      // користувач лише пропонує — модератор вирішить
      const m = _galMeta[downloadId];
      if (m) { m.tag_suggestions = m.tag_suggestions || []; if (!m.tag_suggestions.find(x => x.id === tag.suggestion.id)) m.tag_suggestions.push(tag.suggestion); }
      galRefreshFileTags(downloadId);
      toast(`Запропоновано «${name}» — модератор вирішить`, 'ok');
      return;
    }
    if (_galMeta[downloadId]) {
      _galMeta[downloadId].tags = _galMeta[downloadId].tags||[];
      if (!_galMeta[downloadId].tags.find(t=>t.id===tag.id)) _galMeta[downloadId].tags.push(tag);
    }
    await galLoadAllTags();
    galRefreshFileTags(downloadId);
    toast(`+ ${name}`,'ok');
  } catch(e) { toast(`✗ ${e.message}`,'err'); }
}

async function galRemoveTag(downloadId, tagId, el) {
  el.closest('.tag-inline').style.opacity='.3';
  try {
    const d = await fetch(`${API}/api/download/${downloadId}/tags/${tagId}`,{method:'DELETE'}).then(r => r.json());
    if (d.error) { toast(`✗ ${d.error}`, 'err'); el.closest('.tag-inline').style.opacity='1'; return; }
    if (d.suggested) {
      const m = _galMeta[downloadId];
      if (m) { m.tag_suggestions = m.tag_suggestions || []; if (!m.tag_suggestions.find(x => x.id === d.suggestion.id)) m.tag_suggestions.push(d.suggestion); }
      galRefreshFileTags(downloadId);
      toast('Запропоновано прибрати тег — модератор вирішить', 'ok');
      return;
    }
    if (_galMeta[downloadId]?.tags) _galMeta[downloadId].tags=_galMeta[downloadId].tags.filter(t=>t.id!==tagId);
    galRefreshFileTags(downloadId);
    toast(t('toast.tagdeleted'),'ok');
  } catch(e) { toast(`✗ ${e.message}`,'err'); el.closest('.tag-inline').style.opacity='1'; }
}

function galRefreshFileTags(downloadId) {
  const container = document.getElementById(`gal-tags-${downloadId}`);
  if (!container) return;
  const d = _galMeta[downloadId]; if (!d) return;
  const rtags = d.tags || [];
  const rgroups = {};
  rtags.forEach(t => { const c=t.category||'other'; if(!rgroups[c])rgroups[c]=[]; rgroups[c].push(t); });
  const rgroupHtml = TAG_CATS.filter(c=>rgroups[c]?.length).map(cat=>
    `<div class="tag-group" style="text-align:center">
      <div class="tag-group-label cat-${cat}">${TAG_CAT_LABELS[cat]}</div>
      <div class="tag-group-chips" style="justify-content:center">${rgroups[cat].map(t=>
        `<span class="tag-inline" onclick="galRemoveTag(${downloadId},${t.id},this)">${esc(t.name)}<span style="margin-left:4px;opacity:.6">✕</span></span>`
      ).join('')}</div>
    </div>`
  ).join('');
  // Невирішені пропозиції — пунктиром; свою можна відкликати кліком
  const sugg = (d.tag_suggestions || []).filter(s => s.action === 'add' || s.action === 'remove');
  const myId = _currentUser?.user_id;
  const suggHtml = sugg.length ? `<div class="tag-group" style="text-align:center">
      <div class="tag-group-label">Запропоновано</div>
      <div class="tag-group-chips" style="justify-content:center">${sugg.map(s =>
        `<span class="tag-inline tag-suggested" title="${s.suggested_by === myId ? 'Твоя пропозиція — клік, щоб відкликати' : 'Чекає рішення модератора'}"
               ${s.suggested_by === myId ? `onclick="galWithdrawSuggestion(${downloadId}, ${s.id})"` : ''}>${s.action === 'add' ? '+' : '−'} ${esc(s.tag_name)}</span>`
      ).join('')}</div></div>` : '';
  container.innerHTML = rgroupHtml + suggHtml + `<span class="tag-chip" style="background:transparent;color:var(--muted);border-color:var(--border)" onclick="galShowInlineTagInput(${downloadId})">+ ${canModerate() ? t('gal.tag') : 'запропонувати тег'}</span>`;
}

async function galWithdrawSuggestion(downloadId, sid) {
  try {
    const r = await fetch(`${API}/api/tag-suggestions/${sid}`, { method: 'DELETE' });
    if (!r.ok) { toast((await r.json()).error || t('toast.error'), 'err'); return; }
    const m = _galMeta[downloadId];
    if (m) m.tag_suggestions = (m.tag_suggestions || []).filter(x => x.id !== sid);
    galRefreshFileTags(downloadId);
    toast('Пропозицію відкликано', 'ok');
  } catch { toast('Помилка мережі', 'err'); }
}

function galManageTags() { document.getElementById('tag-modal').style.display='flex'; galRenderAllTagsList(); }

function galShowUploadModal() {
  document.getElementById('upload-file-input').value = '';
  document.getElementById('upload-artist-input').value = '';
  document.getElementById('upload-tags-input').value = '';
  document.getElementById('upload-status').textContent = '';
  document.getElementById('upload-modal').style.display = 'flex';
}

// Завантаження кількох файлів по черзі. Сервер приймає файл за запит —
// так простіше показувати прогрес і помилку саме того файлу, що не пройшов
// (дубль у теці, недопустима тека), а решта все одно завантажується.
// Спільне для модалки «Арти» і drag&drop у каталозі.
async function uploadFilesSequential(files, opts = {}, onProgress = () => {}) {
  const res = { ok: [], failed: [] };
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    onProgress(i, files.length, file.name);
    const fd = new FormData();
    fd.append('file', file);
    if (opts.artist) fd.append('artist_name', opts.artist);
    if (opts.folder) fd.append('folder', opts.folder);
    fd.append('tags', opts.tags || '');
    try {
      const r = await fetch(`${API}/api/upload`, { method: 'POST', body: fd });
      const d = await r.json().catch(() => ({}));
      if (r.ok) res.ok.push(d.filename || file.name);
      else res.failed.push({ name: file.name, error: d.error || `HTTP ${r.status}` });
    } catch (e) {
      res.failed.push({ name: file.name, error: 'Помилка мережі' });
    }
  }
  onProgress(files.length, files.length, '');
  return res;
}

async function galSubmitUpload() {
  const fileInput   = document.getElementById('upload-file-input');
  const artistInput = document.getElementById('upload-artist-input');
  const tagsInput   = document.getElementById('upload-tags-input');
  const statusEl    = document.getElementById('upload-status');
  const btn         = document.getElementById('upload-submit-btn');

  const files  = Array.from(fileInput.files || []);
  // Автор — справа застосунку furry і НЕ обов'язковий: без нього файл іде
  // у вказану теку або в корінь. Поля автора може й не бути, якщо furry вимкнено.
  const artist = artistInput ? artistInput.value.trim() : '';
  const folder = (document.getElementById('upload-folder-input')?.value || '').trim();
  if (!files.length) { statusEl.textContent = 'Обери файли'; statusEl.style.color = 'var(--red)'; return; }

  btn.disabled = true;
  statusEl.style.color = 'var(--muted)';
  try {
    const res = await uploadFilesSequential(files, { artist, folder, tags: tagsInput.value.trim() },
      (i, n, name) => { if (name) statusEl.textContent = `Завантаження ${i + 1} / ${n}: ${name}`; });
    const lines = [`✓ Збережено: ${res.ok.length} з ${files.length}`];
    res.failed.forEach(f => lines.push(`✗ ${f.name}: ${f.error}`));
    statusEl.textContent = lines.join('\n');
    statusEl.style.color = res.failed.length ? (res.ok.length ? 'var(--yellow)' : 'var(--red)') : 'var(--green)';
    if (res.ok.length) toast(`Додано файлів: ${res.ok.length}`, res.failed.length ? 'err' : 'ok');
    if (!res.failed.length) {
      setTimeout(() => { document.getElementById('upload-modal').style.display = 'none'; }, 1200);
    }
  } finally {
    btn.disabled = false;
  }
}

function galRenderAllTagsList() {
  const el = document.getElementById('all-tags-list'); if (!el) return;
  if (!_allTags.length) { el.innerHTML=`<span style="color:var(--muted);font-family:var(--mono);font-size:12px">${t('gal.notagsyet')}</span>`; return; }
  const groups = {};
  _allTags.forEach(t => { const c=t.category||'other'; if(!groups[c])groups[c]=[]; groups[c].push(t); });
  el.innerHTML = TAG_CATS.filter(c=>groups[c]?.length).map(cat => `
    <div style="width:100%;margin-bottom:12px">
      <div class="tag-group-label cat-${cat}" style="display:block;margin-bottom:6px">${TAG_CAT_LABELS[cat]} (${groups[cat].length})</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${groups[cat].map(t=>`
        <div style="display:inline-flex;align-items:center;gap:6px;background:var(--surface2);border-radius:8px;padding:4px 10px">
          <span style="font-family:var(--mono);font-size:12px">${esc(t.name)}</span>
          <span style="font-size:10px;color:var(--muted);font-family:var(--mono)">${t.usage_count||0}×</span>
          <button data-mod onclick="deleteTag(${t.id})" title="${isAdminRole() ? 'Видалити тег з архіву' : 'Запропонувати видалити (вирішує адмін)'}" style="background:none;border:none;color:var(--red);cursor:pointer;font-size:13px;padding:0;line-height:1">✕</button>
        </div>`).join('')}
      </div>
    </div>`).join('');
}

async function createTag() {
  const name     = document.getElementById('new-tag-name').value.trim().toLowerCase();
  const category = document.getElementById('new-tag-cat').value;
  if (!name) return;
  const r = await fetch(`${API}/api/tags`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,category})}).then(x => x.json());
  if (r.error) { toast(r.error, 'err'); return; }
  document.getElementById('new-tag-name').value='';
  await galLoadAllTags();
  toast(`${t('gal.tagcreated')} «${name}»`,'ok');
}

async function deleteTag(id) {
  const msg = isAdminRole() ? t('confirm.deletetag')
    : 'Запропонувати адміну видалити цей тег з архіву (з усіх файлів)?';
  if (!confirm(msg)) return;
  const r = await fetch(`${API}/api/tags/${id}`,{method:'DELETE'}).then(x => x.json()).catch(() => ({}));
  if (r.error) { toast(r.error, 'err'); return; }
  if (r.suggested) { toast('Запропоновано — вирішить адмін', 'ok'); return; }
  await galLoadAllTags();
  toast(t('toast.tagdeleted'),'ok');
}

function galleryGo(delta) {
  if (_galSource !== 'local') { galExternalNav(delta); return; }
  galleryShow(_galIdx+delta);
}

// ── Свайп для гортання арту на тачскрінах ──────────────────────────────
(function setupSwipe(wrapId, goFn) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  let startX = 0, startY = 0, tracking = false;
  wrap.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    tracking = true;
  }, { passive: true });
  wrap.addEventListener('touchend', e => {
    if (!tracking) return;
    tracking = false;
    const dx = e.changedTouches[0].clientX - startX;
    const dy = e.changedTouches[0].clientY - startY;
    if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
    goFn(dx < 0 ? 1 : -1);
  }, { passive: true });
})('gallery-wrap', galleryGo);
function galSaveVol(el) { localStorage.setItem('gal_vol',el.volume); localStorage.setItem('gal_muted',el.muted); }
function galOpenFullscreen(url) {
  document.getElementById('overlay-vid').style.display='none';
  document.getElementById('overlay-img').style.display='';
  document.getElementById('overlay-img').src=url;
  document.getElementById('overlay').classList.add('open');
}
async function galPrefetchMeta(idx) {
  if (!_galIds.length) return;
  idx=((idx%_galIds.length)+_galIds.length)%_galIds.length;
  const id=_galIds[idx]; if (_galMeta[id]) return;
  try { await galGetMeta(id); } catch {}
}

document.addEventListener('keydown', e => {
  // Overlay навігація (pending дублікати, Каталог) — працює на будь-якій сторінці
  if (document.getElementById('overlay').classList.contains('open')) {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      overlayNav(e.key === 'ArrowRight' ? 1 : -1);
      return;
    }
    if (e.key === 'Escape') { closeOverlay(); return; }
    // Керування відео/аудіо в оверлеї (той самий набір клавіш, що й в
    // "Арти") — раніше працювало лише на currentPage==='gallery', тому
    // з "Каталогу" (де відео теж відкривається через цей самий оверлей)
    // пробіл/[]/M/гучність не спрацьовували.
    const ovVid = document.getElementById('overlay-vid');
    const ovAud = document.getElementById('overlay-audio');
    const media = (ovVid && ovVid.style.display !== 'none') ? ovVid
                : (ovAud && ovAud.style.display !== 'none') ? ovAud : null;
    if (media) {
      if (e.key==='ArrowUp')   { e.preventDefault(); media.volume=Math.min(1,+(media.volume+.1).toFixed(1)); media.muted=false; galShowVolHud(media.volume); return; }
      if (e.key==='ArrowDown') { e.preventDefault(); media.volume=Math.max(0,+(media.volume-.1).toFixed(1)); galShowVolHud(media.volume); return; }
      if (e.key==='['||e.key==='j'||e.key==='J') { e.preventDefault(); media.currentTime=Math.max(0,media.currentTime-10); galShowSeekHud(-10); return; }
      if (e.key===']'||e.key==='l'||e.key==='L') { e.preventDefault(); media.currentTime=Math.min(media.duration||0,media.currentTime+10); galShowSeekHud(+10); return; }
      if (e.key==='m'||e.key==='M') { e.preventDefault(); media.muted=!media.muted; galShowVolHud(media.muted?0:media.volume); return; }
      if (e.key===' ') { e.preventDefault(); media.paused?media.play():media.pause(); return; }
    }
  }

  if (currentPage === 'live') {
    if (['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;
    const viewerOpen = document.getElementById('live-viewer').style.display !== 'none';
    if (!viewerOpen) {
      // Дивимось сітку результатів — стрілки перемикають сторінки
      const pagerVisible = document.getElementById('live-pager').style.display !== 'none';
      if (!pagerVisible) return;
      if (e.key==='ArrowRight') { e.preventDefault(); liveChangePage(1);  return; }
      if (e.key==='ArrowLeft')  { e.preventDefault(); liveChangePage(-1); return; }
      return;
    }
    if (e.key==='ArrowRight') { e.preventDefault(); liveGo(1);  return; }
    if (e.key==='ArrowLeft')  { e.preventDefault(); liveGo(-1); return; }
    if (e.key==='Escape')     { e.preventDefault(); liveBackToResults(); return; }
    if (e.key==='p'||e.key==='P') {
      e.preventDefault();
      const cur = _liveResults[_liveIdx];
      if (cur?.pool_id) liveSearchPool(cur.pool_id, cur.pool_name);
      return;
    }
    if (e.key==='Backspace' && _liveHistory.length) { e.preventDefault(); liveGoBack(); return; }
    const vid = document.querySelector('#live-viewer video');
    if (!vid) return;
    if (e.key==='ArrowUp')   { e.preventDefault(); vid.volume=Math.min(1,+(vid.volume+.1).toFixed(1)); vid.muted=false; galSaveVol(vid); galShowVolHud(vid.volume); return; }
    if (e.key==='ArrowDown') { e.preventDefault(); vid.volume=Math.max(0,+(vid.volume-.1).toFixed(1)); galSaveVol(vid); galShowVolHud(vid.volume); return; }
    if (e.key==='['||e.key==='j'||e.key==='J') { e.preventDefault(); vid.currentTime=Math.max(0,vid.currentTime-10); galShowSeekHud(-10); }
    if (e.key===']'||e.key==='l'||e.key==='L') { e.preventDefault(); vid.currentTime=Math.min(vid.duration||0,vid.currentTime+10); galShowSeekHud(+10); }
    if (e.key==='m'||e.key==='M') { e.preventDefault(); vid.muted=!vid.muted; galSaveVol(vid); galShowVolHud(vid.muted?0:vid.volume); }
    if (e.key===' ') { e.preventDefault(); vid.paused?vid.play():vid.pause(); }
    return;
  }

  if (currentPage !== 'gallery') return;
  if (['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;
  if (e.key==='ArrowRight') { e.preventDefault(); galleryGo(1);  return; }
  if (e.key==='ArrowLeft')  { e.preventDefault(); galleryGo(-1); return; }
  if (e.key==='Escape')     { closeOverlay(); return; }
  if (e.key==='p'||e.key==='P') {
    e.preventDefault();
    const curId = _galIds[_galIdx];
    const meta  = _galMeta[curId];
    // P — у пул; ще раз P — наступний пул цієї роботи (якщо їх кілька)
    const next = galNextPool(meta);
    if (next) { galEnterPool(next.name, next.artist_id || meta.artist_id, meta.artist); return; }
    // Раніше тут була мовчанка: ні пулу, ні пояснення. Якщо в картці
    // немає subfolder — файл не прив'язаний до поста (нема рядка в
    // furry_posts), і галерея просто не знає, до якого pool він
    // належить. Кажемо це прямо, бо ззовні виглядає як зламана клавіша.
    if (!meta)              toast('Картка ще вантажиться — спробуй за мить', 'err');
    else if (!meta.artist)  toast('Цей файл не прив\'язаний до автора — пулу в нього немає', 'err');
    else                    toast('Цей файл не входить у жоден pool', 'err');
    return;
  }
  if (e.key==='Backspace' && _galHistory.length) { e.preventDefault(); galGoBack(); return; }
  const vid = document.querySelector('#gallery-wrap video');
  if (!vid) return;
  if (e.key==='ArrowUp')   { e.preventDefault(); vid.volume=Math.min(1,+(vid.volume+.1).toFixed(1)); vid.muted=false; galSaveVol(vid); galShowVolHud(vid.volume); return; }
  if (e.key==='ArrowDown') { e.preventDefault(); vid.volume=Math.max(0,+(vid.volume-.1).toFixed(1)); galSaveVol(vid); galShowVolHud(vid.volume); return; }
  if (e.key==='['||e.key==='j'||e.key==='J') { e.preventDefault(); vid.currentTime=Math.max(0,vid.currentTime-10); galShowSeekHud(-10); }
  if (e.key===']'||e.key==='l'||e.key==='L') { e.preventDefault(); vid.currentTime=Math.min(vid.duration||0,vid.currentTime+10); galShowSeekHud(+10); }
  if (e.key==='m'||e.key==='M') { e.preventDefault(); vid.muted=!vid.muted; galSaveVol(vid); galShowVolHud(vid.muted?0:vid.volume); }
  if (e.key===' ') { e.preventDefault(); vid.paused?vid.play():vid.pause(); }
});

let _hudTimer;
function galShowVolHud(v)  { galShowHud(v===0?'🔇 Mute':`🔊 ${Math.round(v*100)}%`); }
function galShowSeekHud(s) { const u=t('gal.secabbr'); galShowHud(s>0?`⏩ +${s}${u}`:`⏪ ${s}${u}`); }
function galShowHud(text) {
  let h=document.getElementById('gal-hud');
  if (!h) { h=document.createElement('div'); h.id='gal-hud'; h.style.cssText='position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.75);color:#fff;font-family:var(--mono);font-size:22px;padding:14px 28px;border-radius:12px;pointer-events:none;z-index:100;transition:opacity .3s'; document.body.appendChild(h); }
  h.textContent=text; h.style.opacity='1';
  clearTimeout(_hudTimer); _hudTimer=setTimeout(()=>{h.style.opacity='0';},800);
}


// ── Runner ────────────────────────────────────────────────────────────────
let _logLineCount = 0;   // скільки рядків вже відображено — для дедупу при reconnect

function connectSSE() {
  if (sseSource) return;
  // Потік логів дає застосунок архіватора. Немає застосунку — немає й
  // потоку: EventSource інакше нескінченно перепідключався б до 404.
  if (_archiverAbsent) return;
  // Передаємо серверу скільки рядків вже маємо — він пропустить їх при replay
  sseSource = new EventSource(`${API}/api/logs/stream`);
  let isFirstBatch = true;
  sseSource.onmessage = e => {
    const line = JSON.parse(e.data);
    // Перший пакет — це можлива replay-історія. Пропускаємо вже показані рядки.
    if (isFirstBatch) {
      const term = document.getElementById('log-terminal');
      const already = term.children.length;
      // Якщо термінал порожній або показує тільки placeholder — очищаємо і показуємо
      if (already <= 1 && (term.textContent.includes('Очікування') || term.textContent.includes(tr('Очікування')))) {
        term.innerHTML = '';
        _logLineCount = 0;
      }
    }
    isFirstBatch = false;
    appendLog(line);
  };
  sseSource.onerror = () => {
    // Перевіряємо, чи ендпоінт узагалі існує: якщо ні — застосунок
    // вимкнений, і перепідключатись нема куди.
    fetch(`${API}/api/status`).then(r => {
      if (r.status === 404) {
        _archiverAbsent = true;
        if (sseSource) { sseSource.close(); sseSource = null; }
      }
    }).catch(() => {});

    sseSource.close();
    sseSource = null;
    setTimeout(connectSSE, 2000);
  };
}

function appendLog(line) {
  const term = document.getElementById('log-terminal');
  const div  = document.createElement('div');
  let cls = 'log-line';
  if (line.includes('✓') || line.includes('ЗАВЕРШЕНО')) cls = 'log-ok';
  else if (line.includes('✗'))   cls = 'log-err';
  else if (line.includes('⚠') || line.includes('⊘') || line.includes('⏳')) cls = 'log-warn';
  else if (line.includes('──') || line.includes('DRY RUN') || line.includes('===')) cls = 'log-info';
  div.className = cls;
  div.textContent = line;
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;
  while (term.children.length > 2000) term.removeChild(term.firstChild);
}
async function clearLog() {
  await fetch(`${API}/api/logs/clear`, {method:'POST'}).catch(()=>{});
  document.getElementById('log-terminal').innerHTML = `<div class="log-line" style="color:var(--muted)">${t('log.cleared')}</div>`;
}

async function runArchiver(dryRun=false, migrate=false) {
  const r = await fetch(`${API}/api/run`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:dryRun,migrate})});
  if (r.ok) {
    toast(t('toast.archiverstarted'), 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else { const d=await r.json(); toast(d.error||t('toast.starterr'),'err'); }
}
async function runSync() {
  if (!confirm(t('confirm.syncall'))) return;
  const r = await fetch(`${API}/api/run`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ sync_all: true })
  });
  if (r.ok) {
    toast(t('toast.syncstarted'), 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else {
    const d = await r.json();
    toast(d.error || t('toast.starterr'), 'err');
  }
}
async function runCleanup(dryRun) {
  const msg = dryRun ? t('confirm.cleanupdry') : t('confirm.cleanup');
  if (!dryRun && !confirm(msg)) return;
  const r = await fetch(`${API}/api/run`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cleanup: true, cleanup_dry: dryRun })
  });
  if (r.ok) {
    toast(dryRun ? t('toast.checkstarted') : t('toast.cleanupstarted'), 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else {
    const d = await r.json();
    toast(d.error || t('toast.error'), 'err');
  }
}

async function runMigrateTags() {
  const r = await fetch(`${API}/api/run`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({migrate_tags: true})
  });
  if (r.ok) {
    toast(t('toast.migratetagsstarted'), 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else {
    const d = await r.json();
    toast(d.error || t('toast.starterr'), 'err');
  }
}

async function runFindDuplicates() {
  if (!confirm('Просканувати ВСІХ авторів на дублікати? На великих бібліотеках це може зайняти чимало часу.')) return;
  const r = await fetch(`${API}/api/run`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({find_duplicates: true})
  });
  if (r.ok) {
    toast('Сканування дублікатів почалось', 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else {
    const d = await r.json();
    toast(d.error || t('toast.starterr'), 'err');
  }
}

async function runRecheckPoolsAll() {
  if (!confirm('Перевірити пули ВСІХ авторів?\n\nНаявні файли розкладуться по пулах, а роботи, що входять у кілька пулів, додадуться й у решту (без копій). Нічого не скачується й не видаляється. На великому архіві це довго — кожного автора треба спитати в e621/Inkbunny.')) return;
  const r = await fetch(`${API}/api/run`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({recheck_pools: true})
  });
  if (r.ok) {
    toast('Перевірка пулів почалась', 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else {
    const d = await r.json();
    toast(d.error || t('toast.starterr'), 'err');
  }
}

async function runBackfillSizes() {
  const r = await fetch(`${API}/api/run`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({backfill_sizes: true})
  });
  if (r.ok) {
    toast('Довантаження ваги почалось', 'ok');
    showPage('runner');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
  } else {
    const d = await r.json();
    toast(d.error || t('toast.starterr'), 'err');
  }
}

async function stopArchiver() {
  const r = await fetch(`${API}/api/stop`,{method:'POST'});
  r.ok ? toast(t('toast.stopped'),'ok') : toast((await r.json()).error||t('toast.error'),'err');
}
async function updateStatus() {
  // Запуск архіватора — це застосунок. Якщо його немає, ендпоінта теж
  // немає: ховаємо віджет і припиняємо опитування, замість того щоб
  // раз на секунду ловити 404 і лякати користувача "помилкою зʼєднання".
  try {
    const resp = await fetch(`${API}/api/status`);
    if (resp.status === 404) {
      _archiverAbsent = true;
      const w = document.getElementById('proc-widget');
      if (w) w.style.display = 'none';
      const dot = document.getElementById('proc-dot'), lbl = document.getElementById('proc-label');
      if (dot) dot.style.display = 'none';
      if (lbl) lbl.style.display = 'none';
      return;
    }
    const s = await resp.json();
    const dot = document.getElementById('proc-dot'), lbl = document.getElementById('proc-label');
    if (dot) { dot.style.background = s.running ? 'var(--green)' : 'var(--muted)';
               dot.style.boxShadow  = s.running ? '0 0 6px var(--green)' : 'none'; }
    if (lbl) lbl.textContent = s.running ? `Запущено (PID ${s.pid})` : 'Зупинено';
    ['btn-run','btn-dry','btn-sync','btn-migrate-tags','btn-cleanup'].forEach(id => {
      const b = document.getElementById(id); if (b) b.disabled = s.running;
    });
    const stop = document.getElementById('btn-stop'); if (stop) stop.disabled = !s.running;

    if (_wasRunning && !s.running && typeof refreshCurrent === 'function') refreshCurrent();
    _wasRunning = !!s.running;
  } catch (e) {}
}

// ── Config ────────────────────────────────────────────────────────────────
// ── Зовнішні архіви: ключі доступу (сторона хоста) ─────────────────────────

async function loadExternalKeys() {
  const list = document.getElementById('external-keys-list');
  if (!list) return;
  try {
    const keys = await fetch(`${API}/api/external-access/keys`).then(r => r.json());
    if (!Array.isArray(keys) || !keys.length) {
      list.innerHTML = `<div style="color:var(--muted);font-size:12px">Ключів ще нема</div>`;
      return;
    }
    list.innerHTML = keys.map(k => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--surface2);border-radius:8px;margin-bottom:6px;font-family:var(--mono);font-size:12px;flex-wrap:wrap">
        <span style="flex:1;min-width:100px">${esc(k.label)}</span>
        <span style="color:var(--muted);font-size:11px;flex-shrink:0">${k.last_used_from ? `останнє: ${esc(k.last_used_from)} · ${fmtDate(k.last_used_at)}` : 'ще не використано'}</span>
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
          <input type="checkbox" ${k.is_active ? 'checked' : ''} onchange="toggleExternalKey(${k.id}, this.checked)" title="Активний">
          <button class="btn btn-ghost btn-sm" onclick="showKeyFoldersModal(${k.id}, '${esc(k.label)}')" title="Які папки бачить цей ключ">📁</button>
          <button class="btn btn-ghost btn-sm" onclick="deleteExternalKey(${k.id})">🗑</button>
        </div>
      </div>`).join('');
  } catch {}
}

let _keyFoldersKeyId = null;

async function showKeyFoldersModal(keyId, keyLabel) {
  _keyFoldersKeyId = keyId;
  document.getElementById('key-folders-key-label').textContent = keyLabel;
  document.getElementById('key-folders-modal').style.display = 'flex';
  await keyFoldersLoad();
}

async function keyFoldersLoad() {
  const list = document.getElementById('key-folders-list');
  list.innerHTML = `<div style="color:var(--muted);font-size:12px">${t('common.loading')}</div>`;
  try {
    const folders = await fetch(`${API}/api/external-access/keys/${_keyFoldersKeyId}/folders`).then(r => r.json());
    if (folders.error) { list.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(folders.error)}</div>`; return; }
    if (!folders.length) { list.innerHTML = `<div style="color:var(--muted);font-size:12px">Авторів ще немає</div>`; return; }
    list.innerHTML = folders.map(f => `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--surface2);border-radius:6px;font-family:var(--mono);font-size:12px;cursor:pointer">
        <input type="checkbox" ${f.hidden ? '' : 'checked'} onchange="keyFolderToggle('${esc(f.folder_name)}', !this.checked)">
        <span>${esc(f.folder_name)}</span>
      </label>`).join('');
  } catch {
    list.innerHTML = `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
}

async function keyFolderToggle(folderName, hide) {
  try {
    await fetch(`${API}/api/external-access/keys/${_keyFoldersKeyId}/folders/toggle`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ folder_name: folderName, hide })
    });
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function keyFoldersBulk(action) {
  try {
    await fetch(`${API}/api/external-access/keys/${_keyFoldersKeyId}/folders/bulk`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action })
    });
    toast(action === 'show_all' ? 'Усе показано' : 'Усе приховано', 'ok');
    keyFoldersLoad();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function genExternalKey() {
  const label = document.getElementById('newkey-label').value.trim();
  if (!label) { toast('Впиши, кому цей ключ', 'err'); return; }
  try {
    const r = await fetch(`${API}/api/external-access/keys`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ label })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    document.getElementById('newkey-label').value = '';
    prompt('Ключ створено! Скопіюй його зараз — вдруге він ніде не покажеться:', r.key);
    loadExternalKeys();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function toggleExternalKey(id, enabled) {
  try {
    await fetch(`${API}/api/external-access/keys/${id}/toggle`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ enabled })
    });
    toast(enabled ? 'Ключ активовано' : 'Ключ деактивовано — доступ закрито', 'ok');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function deleteExternalKey(id) {
  if (!confirm('Видалити цей ключ остаточно?')) return;
  try {
    await fetch(`${API}/api/external-access/keys/${id}`, { method: 'DELETE' });
    loadExternalKeys();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

// ── Зовнішні архіви: підключення (сторона гостя) ────────────────────────────

async function loadExternalArchives() {
  const list = document.getElementById('external-archives-list');
  if (!list) return;
  try {
    const archives = await fetch(`${API}/api/external-archives`).then(r => r.json());
    if (!Array.isArray(archives) || !archives.length) {
      list.innerHTML = `<div style="color:var(--muted);font-size:12px">Нічого не підключено</div>`;
      return;
    }
    list.innerHTML = archives.map(a => {
      const isOwner = a.added_by_user_id === _currentUser?.user_id;
      return `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--surface2);border-radius:8px;margin-bottom:6px;font-family:var(--mono);font-size:12px;flex-wrap:wrap">
        <span style="flex:1;min-width:100px">${esc(a.label)}${!isOwner ? ' <span style="color:var(--muted);font-size:10px">(дав доступ хтось інший)</span>' : ''}</span>
        <span id="ext-status-${a.id}" style="color:var(--muted);font-size:11px;flex-shrink:0">перевірка…</span>
        <label style="display:flex;align-items:center;gap:4px;font-size:10.5px;color:var(--muted);cursor:pointer;flex-shrink:0" title="Включати в 'Усі разом'">
          <input type="checkbox" ${a.include_in_combined ? 'checked' : ''} onchange="toggleArchiveCombined('${a.id}', this.checked)">
          🌐
        </label>
        <div style="display:flex;gap:6px;flex-shrink:0">
          ${isOwner ? `<button class="btn btn-ghost btn-sm" onclick="showExternalAccessModal('${a.id}')" title="Хто бачить цей архів">🔑</button>` : ''}
          ${isOwner ? `<button class="btn btn-ghost btn-sm" onclick="deleteExternalArchive('${a.id}')">🗑</button>` : ''}
        </div>
      </div>`;
    }).join('');
    archives.forEach(a => checkExternalStatus(a.id));
  } catch {}
}

async function toggleArchiveCombined(archiveId, enabled) {
  try {
    await fetch(`${API}/api/external-archives/${archiveId}/toggle-combined`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ enabled })
    });
    toast(enabled ? 'Додано в "Усі разом"' : 'Виключено з "Усі разом"', 'ok');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function checkExternalStatus(id) {
  const el = document.getElementById(`ext-status-${id}`);
  if (!el) return;
  try {
    const r = await fetch(`${API}/api/external-archives/${id}/status`).then(x => x.json());
    if (r.active) {
      el.textContent = '✓ активний';
      el.style.color = 'var(--green)';
    } else {
      // Ключ видалили/деактивували на тому боці — підключення тут стало
      // "мертвим". Не видаляємо автоматично (могла бути й тимчасова
      // мережева проблема), але даємо швидко прибрати одним кліком.
      el.innerHTML = `<span style="color:var(--red)">✕ ${esc(r.error || 'недоступний')}</span>
        <span class="catalog-crumb" style="color:var(--red);margin-left:4px" onclick="deleteExternalArchive('${id}')" title="Прибрати мертве підключення">🗑</span>`;
    }
  } catch {
    el.textContent = '✕ помилка мережі';
  }
}

async function addExternalArchive() {
  const status = document.getElementById('ext-add-status');
  const body = {
    label:       document.getElementById('ext-label').value.trim(),
    base_url:    document.getElementById('ext-base-url').value.trim(),
    access_key:  document.getElementById('ext-key').value.trim(),
    my_identity: document.getElementById('ext-my-identity').value.trim(),
  };
  status.style.color = 'var(--muted)';
  status.textContent = 'Перевіряю підключення…';
  try {
    const r = await fetch(`${API}/api/external-archives`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    }).then(x => x.json());
    if (r.error) { status.style.color = 'var(--red)'; status.textContent = r.error; return; }
    status.style.color = 'var(--green)';
    status.textContent = '✓ Підключено';
    toast('Архів підключено', 'ok');
    setTimeout(() => { document.getElementById('add-external-modal').style.display = 'none'; loadExternalArchives(); }, 900);
  } catch {
    status.style.color = 'var(--red)';
    status.textContent = 'Помилка мережі';
  }
}

// ── Зовнішні архіви: підвкладка "Переглянути" ───────────────────────────────

function extSetTab(tab) {
  document.querySelectorAll('#ext-tab-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.exttab === tab));
  document.getElementById('ext-tab-connections').style.display = tab === 'connections' ? '' : 'none';
  document.getElementById('ext-tab-browse').style.display      = tab === 'browse' ? '' : 'none';
  if (tab === 'browse') extBrowseLoadArchiveOptions();
}

async function extBrowseLoadArchiveOptions() {
  const sel = document.getElementById('ext-browse-select');
  const prevVal = sel.value;
  try {
    const archives = await fetch(`${API}/api/external-archives`).then(r => r.json());
    sel.innerHTML = `<option value="">— обери архів —</option>` +
      (Array.isArray(archives) ? archives.map(a => `<option value="${a.id}">${esc(a.label)}</option>`).join('') : '');
    if (prevVal) sel.value = prevVal;
  } catch {}
}

let _extBrowseIds = [];
let _extBrowseIdx = 0;
let _extBrowseArchiveId = '';

function extBrowseSelectArchive() {
  _extBrowseArchiveId = document.getElementById('ext-browse-select').value;
  document.getElementById('ext-browse-viewer').innerHTML = '';
  _extBrowseIds = [];
}

async function extBrowseShuffle() {
  if (!_extBrowseArchiveId) { toast('Спершу обери архів', 'err'); return; }
  const viewer = document.getElementById('ext-browse-viewer');
  viewer.innerHTML = `<div style="color:var(--muted);font-family:var(--mono);font-size:13px;padding:16px 0">${t('common.loading')}</div>`;
  try {
    const r = await fetch(`${API}/api/external-archives/${_extBrowseArchiveId}/gallery/ids`).then(x => x.json());
    if (r.error) { viewer.innerHTML = `<div style="color:var(--red);font-size:13px">${esc(r.error)}</div>`; return; }
    _extBrowseIds = r.ids || [];
    if (!_extBrowseIds.length) { viewer.innerHTML = `<div style="color:var(--muted);font-size:13px">Порожньо</div>`; return; }
    _extBrowseIdx = 0;
    extBrowseShow();
  } catch {
    viewer.innerHTML = `<div style="color:var(--red);font-size:13px">${t('catalog.error')}</div>`;
  }
}

async function extBrowseShow() {
  const viewer = document.getElementById('ext-browse-viewer');
  const id = _extBrowseIds[_extBrowseIdx];
  viewer.innerHTML = `<div style="color:var(--muted);font-family:var(--mono);font-size:13px;padding:16px 0">${t('common.loading')}</div>`;
  try {
    const d = await fetch(`${API}/api/external-archives/${_extBrowseArchiveId}/gallery/meta/${id}`).then(r => r.json());
    if (d.error) { viewer.innerHTML = `<div style="color:var(--red);font-size:13px">${esc(d.error)}</div>`; return; }
    const isVideo = ['webm','mp4'].includes((d.file_ext||'').toLowerCase());
    const mediaUrl = `${API}${d.stream_url}`;
    const mediaEl = isVideo
      ? `<video controls src="${mediaUrl}" style="max-width:100%;max-height:420px"></video>`
      : `<img src="${mediaUrl}" style="max-width:100%;max-height:420px;object-fit:contain" onerror="this.style.display='none';this.nextElementSibling.style.display='block'">
         <div style="display:none;color:var(--muted);font-size:12px;padding:20px 0">Не вдалося завантажити файл</div>`;
    viewer.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <button class="btn btn-ghost btn-sm" onclick="extBrowseNav(-1)">‹</button>
        <span style="font-family:var(--mono);font-size:12px;color:var(--muted)">${_extBrowseIdx+1} / ${_extBrowseIds.length}</span>
        <button class="btn btn-ghost btn-sm" onclick="extBrowseNav(1)">›</button>
        <span style="flex:1"></span>
        <button class="btn btn-primary btn-sm" id="ext-fav-btn" onclick="extBrowseToggleFavorite(${id})">☆ Улюблене</button>
      </div>
      <div style="background:var(--surface2);border-radius:10px;padding:16px;text-align:center">${mediaEl}</div>
      <div style="margin-top:10px;font-family:var(--mono);font-size:12px;color:var(--muted)">
        <div>${esc(d.artist)} · ${esc(d.filename)}</div>
        ${d.tags?.length ? `<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">${d.tags.slice(0,20).map(tg=>`<span class="tag-chip">${esc(tg?.name ?? tg)}</span>`).join('')}</div>` : ''}
      </div>`;
  } catch {
    viewer.innerHTML = `<div style="color:var(--red);font-size:13px">${t('catalog.error')}</div>`;
  }
}

function extBrowseNav(delta) {
  const next = _extBrowseIdx + delta;
  if (next < 0 || next >= _extBrowseIds.length) return;
  _extBrowseIdx = next;
  extBrowseShow();
}

async function extBrowseToggleFavorite(remoteId) {
  try {
    const r = await fetch(`${API}/api/external-archives/${_extBrowseArchiveId}/favorite`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ download_id: remoteId })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    const btn = document.getElementById('ext-fav-btn');
    if (btn) btn.textContent = r.is_favorite ? '★ В улюбленому' : '☆ Улюблене';
    toast(r.is_favorite ? '★ Додано в улюблене' : '☆ Прибрано з улюбленого', 'ok');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

let _extAccessArchiveId = null;

let _catalogAccessFolder = null;

async function showCatalogFolderAccessModal(folderName) {
  _catalogAccessFolder = folderName;
  const list = document.getElementById('catalog-access-list');
  list.innerHTML = `<div style="color:var(--muted);font-size:12px">${t('common.loading')}</div>`;
  document.getElementById('catalog-access-modal').style.display = 'flex';
  try {
    const [users, access] = await Promise.all([
      fetch(`${API}/api/users/list`).then(r => r.json()),
      fetch(`${API}/api/catalog/folder-access?folder=${encodeURIComponent(folderName)}`).then(r => r.json()),
    ]);
    if (access.error) { list.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(access.error)}</div>`; return; }
    const hidden = new Set(access.hidden_user_ids || []);
    list.innerHTML = users.filter(u => !u.is_admin).map(u => `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--surface2);border-radius:6px;font-family:var(--mono);font-size:12px;cursor:pointer">
        <input type="checkbox" ${hidden.has(u.id) ? '' : 'checked'}
               onchange="toggleCatalogFolderAccess(${u.id}, !this.checked)">
        <span>${esc(u.display_name || u.email)}</span>
      </label>`).join('') || `<div style="color:var(--muted);font-size:12px">Немає інших юзерів (окрім адмінів — вони завжди все бачать)</div>`;
  } catch {
    list.innerHTML = `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
}

async function toggleCatalogFolderAccess(userId, hide) {
  try {
    await fetch(`${API}/api/catalog/folder-access`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ folder: _catalogAccessFolder, user_id: userId, hide })
    });
    toast(hide ? 'Приховано від юзера' : 'Показано юзеру', 'ok');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function showExternalAccessModal(archiveId) {
  _extAccessArchiveId = archiveId;
  const list = document.getElementById('ext-access-list');
  list.innerHTML = `<div style="color:var(--muted);font-size:12px">${t('common.loading')}</div>`;
  document.getElementById('ext-access-modal').style.display = 'flex';
  try {
    const [users, access] = await Promise.all([
      fetch(`${API}/api/users/list`).then(r => r.json()),
      fetch(`${API}/api/external-archives/${archiveId}/access`).then(r => r.json()),
    ]);
    if (access.error) { list.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(access.error)}</div>`; return; }
    const granted = new Set(access.granted_user_ids || []);
    list.innerHTML = users.map(u => `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:var(--surface2);border-radius:6px;font-family:var(--mono);font-size:12px;cursor:pointer">
        <input type="checkbox" ${granted.has(u.id) ? 'checked' : ''} ${u.id === _currentUser?.user_id ? 'disabled' : ''}
               onchange="toggleExternalAccess(${u.id}, this.checked)">
        <span>${esc(u.display_name || u.email)}</span>
        ${u.id === _currentUser?.user_id ? '<span style="color:var(--muted);font-size:10px">(ти)</span>' : ''}
      </label>`).join('');
  } catch {
    list.innerHTML = `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
}

async function toggleExternalAccess(userId, granted) {
  try {
    if (granted) {
      await fetch(`${API}/api/external-archives/${_extAccessArchiveId}/access`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ user_id: userId })
      });
      toast('Доступ надано', 'ok');
    } else {
      await fetch(`${API}/api/external-archives/${_extAccessArchiveId}/access/${userId}`, { method: 'DELETE' });
      toast('Доступ забрано', 'ok');
    }
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function deleteExternalArchive(id) {
  if (!confirm('Відключити цей архів?')) return;
  try {
    await fetch(`${API}/api/external-archives/${id}`, { method: 'DELETE' });
    loadExternalArchives();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

// ── Версія архіву ────────────────────────────────────────────────────
function _verLabel(v) {
  if (!v || v.build === 'dev') return v && v.short ? `розробка · ${v.short}` : 'розробка';
  return `збірка #${v.build}` + (v.short ? ` · ${v.short}` : '');
}
function _verDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString(_lang === 'en' ? 'en-GB' : 'uk-UA',
    { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}
async function loadVersionBadge() {
  try {
    const v = await fetch(`${API}/api/version`).then(r => r.ok ? r.json() : null);
    const el = document.getElementById('logo-version');
    if (v && el) { el.textContent = _verLabel(v); el.title = _verDate(v.date); }
  } catch (e) {}
}
async function checkVersion(force) {
  const txt = document.getElementById('cfg-version-text');
  const st  = document.getElementById('cfg-version-status');
  if (!txt) return;
  if (force) st.innerHTML = '<span class="muted">Перевіряю…</span>';
  try {
    const v = await fetch(`${API}/api/version/check${force ? '?force=1' : ''}`).then(r => r.json());
    txt.textContent = _verLabel(v) + (v.date ? ` · ${_verDate(v.date)}` : '');
    const l = v.latest || {};
    if (l.status === 'current') {
      st.innerHTML = '<span style="color:var(--green)">✓ Актуальна версія</span>';
    } else if (l.status === 'outdated') {
      st.innerHTML = `<span style="color:var(--yellow, #d90)">⬆ Є новіша збірка від ${escapeHtml(_verDate(l.latest_date))} — онови контейнер</span>`;
    } else {
      st.innerHTML = `<span class="muted">${escapeHtml(l.reason || 'Не вдалося перевірити')}</span>`;
    }
  } catch (e) {
    st.innerHTML = '<span class="muted">Не вдалося перевірити</span>';
  }
}

async function loadConfig() {
  checkVersion(false);
  const cfg = await fetch(`${API}/api/config`).then(r=>r.json()).catch(()=>({}));
  const v = (...p) => { let o=cfg; for(const k of p)o=o?.[k]; return o??''; };

  document.getElementById('cfg-local-path').value  = v('local','path')||'';
  document.getElementById('cfg-mail-host').value = v('mail','host');
  document.getElementById('cfg-mail-port').value = v('mail','port');
  document.getElementById('cfg-mail-user').value = v('mail','user');
  document.getElementById('cfg-mail-pass').value = '';
  document.getElementById('cfg-mail-pass').placeholder = v('mail','password') ? '● збережено (залиш пустим)' : '';
  document.getElementById('cfg-mail-from').value = v('mail','from_addr');
  document.getElementById('cfg-mail-security').value = v('mail','security') || 'starttls';
  document.getElementById('cfg-mail-base').value = v('mail','base_url');
  document.getElementById('cfg-db-host').value     = v('database','host');
  document.getElementById('cfg-db-port').value     = v('database','port');
  document.getElementById('cfg-db-user').value     = v('database','user');
  document.getElementById('cfg-db-name').value     = v('database','database');
  const titleEl = document.getElementById('cfg-ui-title');
  if (titleEl) titleEl.value = (cfg.ui || {}).title || 'Archive';

  // Налаштування застосунків малюють і заповнюють вони самі.
  if (typeof appsConfigRender === 'function') {
    await appsConfigRender();
    appsConfigLoad(cfg);
  }

  // Секрети — завжди порожні, placeholder показує статус
  const secrets = [
    ['cfg-db-pass',   ['database','password']],
  ];
  secrets.forEach(([id, path]) => {
    const el  = document.getElementById(id);
    if (!el) return;   // поле могло належати вимкненому застосунку
    const val = v(...path);
    el.value  = '';
    // Сервер ніколи не віддає секрет — тільки маску '••••••••', і сама
    // її наявність означає, що значення збережене. Раніше умова була
    // перевернута, тож збережений пароль показувався як "(не вказано)".
    el.placeholder = val
      ? '● збережено (залиш пустим щоб не змінювати)'
      : '(не вказано)';
  });
}

async function saveConfig(e) {
  e.preventDefault();
  const g   = id => document.getElementById(id).value.trim();
  const sec = id => g(id) || null;  // null → сервер не чіпає поле
  const bool = id => document.getElementById(id).value === 'true';
  const chk  = id => document.getElementById(id).checked;

  const r = await fetch(`${API}/api/config`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      local: {
        path: g('cfg-local-path') || null,
      },
      database: {
        host:     g('cfg-db-host'),
        port:     parseInt(g('cfg-db-port'))||3306,
        user:     g('cfg-db-user'),
        password: sec('cfg-db-pass'),
        database: g('cfg-db-name'),
      },
      ui: { title: (document.getElementById('cfg-ui-title')?.value || '').trim() },
      mail: _mailCollect(),
      ...(typeof appsConfigCollect === 'function' ? appsConfigCollect() : {}),
    })
  });
  if (r.ok) {
    toast(t('toast.savedcfg'), 'ok');
    loadConfig();
  } else {
    toast(t('toast.saveerr'), 'err');
  }
  if (typeof applyBranding === 'function') applyBranding();
}

// ── Коди допуску ─────────────────────────────────────────────────────────
function codesSetTab(tab) {
  document.querySelectorAll('#codes-tab-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.codestab === tab));
  document.getElementById('codes-tab-keys').style.display     = tab === 'keys' ? '' : 'none';
  document.getElementById('codes-tab-external').style.display = tab === 'external' ? '' : 'none';
  document.getElementById('codes-tab-users').style.display    = tab === 'users' ? '' : 'none';
  if (tab === 'users') loadUsersFull();
}

// ── Чат ──────────────────────────────────────────────────────────────────

let _chatCurrentPeer = null; // {peer_local_user_id} | {peer_url, peer_identity}
let _chatPollTimer = null;
let _chatPickTarget = 'chat';   // куди піде файл з архіву: 'chat' | 'notes'
let _chatPickTimer = null;

function _chatShowView(v) {
  for (const id of ['chat-list-view', 'chat-thread-view', 'chat-notes-view'])
    document.getElementById(id).style.display = id === v ? '' : 'none';
}

function chatBackToList() {
  _chatShowView('chat-list-view');
  _chatCurrentPeer = null;
  if (_chatPollTimer) { clearInterval(_chatPollTimer); _chatPollTimer = null; }
  loadChatConversations();
}

function _chatPeerParams(p) {
  const params = new URLSearchParams();
  if (p.peer_url) { params.set('peer_url', p.peer_url); params.set('peer_identity', p.peer_identity || ''); }
  else params.set('peer_local_user_id', p.peer_local_user_id);
  return params;
}

function _chatAttHtml(a) {
  if (!a?.url) return '';
  const ext = (a.ext || '').toLowerCase();
  if (['jpg','jpeg','png','gif','webp','bmp','avif'].includes(ext))
    return `<a href="${esc(a.url)}" target="_blank" rel="noopener"><img src="${esc(a.url)}" loading="lazy" alt="" style="display:block;max-width:260px;max-height:260px;border-radius:6px;margin-top:4px"></a>`;
  if (['mp4','webm','mov','m4v'].includes(ext))
    return `<video src="${esc(a.url)}" controls preload="metadata" style="display:block;max-width:300px;border-radius:6px;margin-top:4px"></video>`;
  if (['mp3','ogg','wav','flac','m4a'].includes(ext))
    return `<audio src="${esc(a.url)}" controls preload="none" style="display:block;margin-top:4px"></audio>`;
  return `<a href="${esc(a.url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline">📎 ${esc(a.name || 'файл')}</a>`;
}

async function loadChatConversations() {
  _chatRenderNotifyBtn();
  const list = document.getElementById('chat-conversations-list');
  list.innerHTML = `<div style="color:var(--muted);font-size:12px">${t('common.loading')}</div>`;
  const notesRow = `
      <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--surface2);border-radius:8px;cursor:pointer;border:1px dashed var(--border)" onclick="chatOpenNotes()">
        <div style="flex:1;min-width:0">
          <div style="font-weight:500;font-size:13px">📝 ${t('chat.notes')}</div>
          <div style="color:var(--muted);font-size:11.5px">${t('chat.notes_hint')}</div>
        </div>
      </div>`;
  try {
    const convos = await fetch(`${API}/api/chat/conversations`).then(r => r.json());
    if (!Array.isArray(convos) || !convos.length) {
      list.innerHTML = notesRow + `<div style="color:var(--muted);font-size:12px;padding:20px 0;text-align:center">Ще немає розмов — натисни "✎ Нове повідомлення"</div>`;
      return;
    }
    list.innerHTML = notesRow + convos.map(c => `
      <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--surface2);border-radius:8px;cursor:pointer"
           onclick='chatOpenThread(${JSON.stringify({peer_local_user_id:c.peer_local_user_id, peer_url:c.peer_url, peer_identity:c.peer_identity, label:c.peer_label, blocked:!!c.blocked}).replace(/'/g,"&apos;")})'>
        <div style="flex:1;min-width:0">
          <div style="font-weight:500;font-size:13px" data-no-i18n>${c.peer_url ? '🌐 ' : ''}${esc(c.peer_label)}${c.blocked ? ' <span class="muted" style="font-size:11px">· заблоковано</span>' : ''}</div>
          <div style="color:var(--muted);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" data-no-i18n>${esc(c.last_body)}</div>
        </div>
        ${c.unread > 0 ? `<span style="background:var(--accent);color:#fff;border-radius:10px;padding:2px 7px;font-size:10.5px">${c.unread}</span>` : ''}
      </div>`).join('');
  } catch {
    list.innerHTML = notesRow + `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
  chatUpdateUnreadBadge();
}

let _chatNotifiedId = null;            // останнє вхідне, про яке вже сповістили
const _chatBaseTitle = document.title;

async function chatUpdateUnreadBadge() {
  try {
    // Чат — застосунок, його може не бути встановлено. Тоді просто
    // ховаємо лічильник, а не сиплемо помилками щосекунди.
    const resp = await fetch(`${API}/api/chat/unread-count`);
    if (resp.status === 404) { const b=document.getElementById('chat-unread-badge'); if (b) b.style.display='none'; return; }
    const r = await resp.json();
    const badge = document.getElementById('chat-unread-badge');
    if (badge) {
      if (r.count > 0) { badge.textContent = r.count; badge.style.display = ''; }
      else { badge.style.display = 'none'; }
    }
    document.title = r.count > 0 ? `(${r.count}) ${_chatBaseTitle}` : _chatBaseTitle;
    _chatMaybeNotify(r.latest);
  } catch {}
}

function _chatIsOpenThread(m) {
  const p = _chatCurrentPeer;
  if (!p || currentPage !== 'chat' || document.hidden) return false;
  return m.peer_url ? (p.peer_url === m.peer_url && (p.peer_identity || '') === (m.peer_identity || ''))
                    : p.peer_local_user_id == m.peer_local_user_id;
}

function _chatMaybeNotify(m) {
  if (!m) return;
  let seen = _chatNotifiedId;
  if (seen === null) { try { seen = parseInt(localStorage.getItem('chatNotifiedId') || '0'); } catch { seen = 0; } }
  if (m.id <= seen) { _chatNotifiedId = seen; return; }
  _chatNotifiedId = m.id;
  try { localStorage.setItem('chatNotifiedId', String(m.id)); } catch {}
  if (_chatIsOpenThread(m)) return;                       // і так дивишся в цю розмову
  const text = `💬 ${m.peer_label}: ${m.body}`;
  toast(text, 'ok');
  try {
    if ('Notification' in window && Notification.permission === 'granted' && document.hidden) {
      const n = new Notification(`💬 ${m.peer_label}`, { body: m.body, tag: 'chat-' + m.id, icon: '/static/icons/icon-192.png' });
      n.onclick = () => {
        window.focus(); n.close();
        showPage('chat');
        chatOpenThread({ peer_local_user_id: m.peer_local_user_id, peer_url: m.peer_url,
                         peer_identity: m.peer_identity, label: m.peer_label });
      };
    }
  } catch {}
}

function _chatRenderNotifyBtn() {
  const b = document.getElementById('chat-notify-btn');
  if (!b) return;
  b.style.display = ('Notification' in window && Notification.permission === 'default') ? '' : 'none';
}

async function chatEnableNotifications() {
  try { await Notification.requestPermission(); } catch {}
  _chatRenderNotifyBtn();
  if (Notification.permission === 'granted') toast('🔔 Сповіщення увімкнено', 'ok');
}

function chatOpenThread(peer) {
  if (!peer.peer_url && !peer.peer_local_user_id) { toast('Ця розмова більше не підтримується', 'err'); return; }
  _chatCurrentPeer = peer;
  _chatShowView('chat-thread-view');
  document.getElementById('chat-thread-title').textContent = peer.label;
  _chatRenderBlockBtn();
  document.getElementById('chat-thread-messages').innerHTML = '';
  chatLoadMessages();
  if (_chatPollTimer) clearInterval(_chatPollTimer);
  _chatPollTimer = setInterval(chatLoadMessages, 5000);
}

let _chatLastSig = '';
async function chatLoadMessages() {
  if (!_chatCurrentPeer) return;
  try {
    const msgs = await fetch(`${API}/api/chat/messages?${_chatPeerParams(_chatCurrentPeer)}`).then(r => r.json());
    const wrap = document.getElementById('chat-thread-messages');
    if (!Array.isArray(msgs)) return;
    const sig = msgs.length + ':' + (msgs.at(-1)?.id || '') + ':' + msgs.filter(m => m.read_at).length;
    if (sig === _chatLastSig && wrap.childElementCount) { chatUpdateUnreadBadge(); return; }  // без мерехтіння відео
    _chatLastSig = sig;
    wrap.innerHTML = msgs.map(m => {
      const out = m.direction === 'out';
      return `
      <div class="chat-msg" style="align-self:${out?'flex-end':'flex-start'};max-width:75%;background:${out?'var(--accent)':'var(--surface2)'};color:${out?'#fff':'var(--text)'};border-radius:10px;padding:8px 12px;font-size:13px;position:relative">
        ${m.body ? `<span data-no-i18n style="white-space:pre-wrap">${esc(m.body)}</span>` : ''}
        ${_chatAttHtml(m.attachment)}
        <div style="font-size:9.5px;opacity:.7;margin-top:3px;display:flex;gap:8px;align-items:center">
          <span>${fmtDate(m.created_at)}</span>
          ${out ? `<span title="${m.read_at ? t('chat.read') + ' ' + fmtDate(m.read_at) : t('chat.sent')}" style="font-weight:700;letter-spacing:-2px;${m.read_at ? 'color:#fff;opacity:1;text-shadow:0 0 4px #38bdf8' : 'opacity:.75'}">${m.read_at ? '✓✓' : '✓'}</span>` : ''}
          <span class="chat-msg-pin" title="${t('chat.to_notes')}" style="cursor:pointer;margin-left:auto" onclick="chatMsgToNotes(${m.id})">📌</span>
        </div>
      </div>`; }).join('');
    wrap.scrollTop = wrap.scrollHeight;
    chatUpdateUnreadBadge();
  } catch {}
}

async function chatMsgToNotes(id) {
  const r = await fetch(`${API}/api/chat/notes`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ message_id: id }) }).then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
  if (r.error) { toast(r.error, 'err'); return; }
  toast(t('chat.saved_to_notes'), 'ok');
}

async function chatSendMessage(att) {
  const input = document.getElementById('chat-input');
  const body = input.value.trim();
  if ((!body && !att) || !_chatCurrentPeer) return;
  input.value = '';
  try {
    const payload = { body, ...(att || {}) };
    if (_chatCurrentPeer.peer_url) { payload.peer_url = _chatCurrentPeer.peer_url; payload.peer_identity = _chatCurrentPeer.peer_identity || ''; }
    else payload.peer_local_user_id = _chatCurrentPeer.peer_local_user_id;
    const r = await fetch(`${API}/api/chat/send`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    if (r.warning) toast(r.warning, 'err');
    if (r.peer_url && _chatCurrentPeer.peer_url) {
      _chatCurrentPeer.peer_url = r.peer_url;                       // нормалізована адреса
      if (r.peer_identity) {                                        // «власник» → його справжній нік
        _chatCurrentPeer.peer_identity = r.peer_identity;
        _chatCurrentPeer.label = `${r.peer_identity}@${r.peer_url.replace(/^https?:\/\//, '')}`;
        document.getElementById('chat-thread-title').textContent = _chatCurrentPeer.label;
      }
    }
    chatLoadMessages();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function chatDeleteConversation() {
  const p = _chatCurrentPeer;
  if (!p || !confirm(t('chat.delete_confirm'))) return;
  const r = await fetch(`${API}/api/chat/conversation?${_chatPeerParams(p)}`, { method: 'DELETE' })
    .then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
  if (r.error) { toast(r.error, 'err'); return; }
  toast(t('chat.deleted'), 'ok');
  chatBackToList();
}

// ── Нотатки ──
function chatOpenNotes() {
  _chatCurrentPeer = null;
  if (_chatPollTimer) { clearInterval(_chatPollTimer); _chatPollTimer = null; }
  _chatShowView('chat-notes-view');
  chatLoadNotes();
}

async function chatLoadNotes() {
  const wrap = document.getElementById('chat-notes-list');
  try {
    const notes = await fetch(`${API}/api/chat/notes`).then(r => r.json());
    if (!Array.isArray(notes) || !notes.length) {
      wrap.innerHTML = `<div class="muted" style="font-size:12px;padding:20px 0;text-align:center">${t('chat.notes_empty')}</div>`;
      return;
    }
    wrap.innerHTML = notes.map(n => `
      <div style="background:var(--surface2);border-radius:10px;padding:8px 12px;font-size:13px">
        ${n.body ? `<span data-no-i18n style="white-space:pre-wrap">${esc(n.body)}</span>` : ''}
        ${_chatAttHtml(n.attachment)}
        <div style="font-size:9.5px;opacity:.7;margin-top:3px;display:flex;gap:8px;align-items:center">
          <span>${fmtDate(n.created_at)}</span>
          ${n.source_label ? `<span data-no-i18n>· ${esc(n.source_label)}</span>` : ''}
          <span title="${t('common.delete')}" style="cursor:pointer;margin-left:auto" onclick="chatDeleteNote(${n.id})">🗑</span>
        </div>
      </div>`).join('');
    wrap.scrollTop = wrap.scrollHeight;
  } catch {
    wrap.innerHTML = `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
}

async function chatAddNote(att) {
  const input = document.getElementById('chat-note-input');
  const body = input.value.trim();
  if (!body && !att) return;
  const r = await fetch(`${API}/api/chat/notes`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ body, ...(att || {}) }) }).then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
  if (r.error) { toast(r.error, 'err'); return; }
  input.value = '';
  chatLoadNotes();
}

async function chatDeleteNote(id) {
  if (!confirm(t('chat.note_delete_confirm'))) return;
  await fetch(`${API}/api/chat/notes/${id}`, { method: 'DELETE' }).catch(() => {});
  chatLoadNotes();
}

// ── Файл з архіву / з ПК ──
let _chatFileMode = 'folder', _chatFilePath = '', _chatFileOffset = 0;

function chatOpenFilePicker(target) {
  _chatPickTarget = target;
  document.getElementById('chat-file-modal').style.display = 'flex';
  chatFileTab(_chatFileMode);
}

function chatFileTab(mode) {
  _chatFileMode = mode;
  document.querySelectorAll('#chat-file-tabs .filter-btn').forEach(b => b.classList.toggle('active', b.dataset.cftab === mode));
  const q = document.getElementById('chat-file-q');
  q.style.display = mode === 'search' ? '' : 'none';
  document.getElementById('chat-file-pc').style.display = mode === 'pc' ? 'flex' : 'none';
  document.getElementById('chat-file-scroll').style.display = mode === 'pc' ? 'none' : '';
  document.getElementById('chat-file-crumbs').style.display = mode === 'folder' ? 'flex' : 'none';
  if (mode === 'search') q.focus();
  if (mode !== 'pc') chatLoadFiles(false);
}

function _unused_chatSearchFilesDebounced() {
  clearTimeout(_chatPickTimer);
  _chatPickTimer = setTimeout(() => chatLoadFiles(false), 300);
}

function chatOpenFolder(path) { _chatFilePath = path; chatLoadFiles(false); }

function _chatRenderCrumbs() {
  const parts = _chatFilePath ? _chatFilePath.split('/') : [];
  let acc = '';
  const links = [`<a href="#" onclick="chatOpenFolder('');return false">🏠 Архів</a>`];
  for (const p of parts) {
    acc = acc ? `${acc}/${p}` : p;
    links.push(`<span class="muted">/</span><a href="#" onclick='chatOpenFolder(${JSON.stringify(acc).replace(/'/g,"&apos;")});return false'>${esc(p)}</a>`);
  }
  document.getElementById('chat-file-crumbs').innerHTML = links.join('');
}

async function chatLoadFiles(more) {
  const grid = document.getElementById('chat-file-results');
  const moreBtn = document.getElementById('chat-file-more');
  if (!more) { _chatFileOffset = 0; grid.innerHTML = `<div class="muted" style="font-size:12px">${t('common.loading')}</div>`; }
  const params = new URLSearchParams({ mode: _chatFileMode, offset: _chatFileOffset });
  if (_chatFileMode === 'folder') { params.set('path', _chatFilePath); _chatRenderCrumbs(); }
  if (_chatFileMode === 'search') {
    const q = document.getElementById('chat-file-q').value.trim();
    if (!q) { grid.innerHTML = `<div class="muted" style="font-size:12px">Впиши частину назви файлу</div>`; moreBtn.style.display = 'none'; return; }
    params.set('q', q);
  }
  try {
    const r = await fetch(`${API}/api/chat/files?${params}`).then(x => x.json());
    if (r.error) { grid.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(r.error)}</div>`; return; }
    const img = ['jpg','jpeg','png','gif','webp','bmp','avif'];
    const tile = (inner, label, onclick, title) => `
      <div onclick='${onclick}' title="${esc(title)}"
           style="cursor:pointer;background:var(--surface2);border-radius:6px;overflow:hidden;display:flex;flex-direction:column">
        <div style="aspect-ratio:1;display:flex;align-items:center;justify-content:center;background:var(--bg);font-size:30px">${inner}</div>
        <div style="font-size:10.5px;padding:4px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" data-no-i18n>${esc(label)}</div>
      </div>`;
    const folderHtml = (r.folders || []).map(f => {
      const p = _chatFilePath ? `${_chatFilePath}/${f}` : f;
      return tile('📁', f, `chatOpenFolder(${JSON.stringify(p).replace(/'/g,"&apos;")})`, f);
    }).join('');
    const fileHtml = (r.files || []).map(f => tile(
      img.includes(f.ext) ? `<img src="${API}/api/gallery/stream/${f.id}" loading="lazy" alt="" style="width:100%;height:100%;object-fit:cover" onerror="this.replaceWith('📄')">`
        : (['mp4','webm','mov','m4v'].includes(f.ext) ? '🎞️' : '📄'),
      f.name, `chatPickFile(${f.id})`, (f.folder ? f.folder + '/' : '') + f.name)).join('');
    if (more) grid.insertAdjacentHTML('beforeend', fileHtml);
    else grid.innerHTML = (folderHtml + fileHtml) || `<div class="muted" style="font-size:12px">Тут порожньо</div>`;
    _chatFileOffset = r.next_offset ?? (_chatFileOffset + (r.files || []).length);
    moreBtn.style.display = r.has_more ? '' : 'none';
  } catch {
    grid.innerHTML = `<div style="color:var(--red);font-size:12px">${t('catalog.error')}</div>`;
  }
}

function chatPickFile(id) {
  document.getElementById('chat-file-modal').style.display = 'none';
  if (_chatPickTarget === 'notes') chatAddNote({ file_id: id });
  else chatSendMessage({ file_id: id });
}

async function chatUploadFromPc(file) {
  if (!file) return;
  if (file.size > 100 * 1024 * 1024) { toast('Файл завеликий (до 100 МБ)', 'err'); return; }
  const fd = new FormData();
  fd.append('file', file);
  toast(`Завантажую ${file.name}…`, 'ok');
  const r = await fetch(`${API}/api/chat/upload`, { method: 'POST', body: fd }).then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
  if (r.error) { toast(r.error, 'err'); return; }
  document.getElementById('chat-file-modal').style.display = 'none';
  if (_chatPickTarget === 'notes') chatAddNote({ upload_id: r.upload_id });
  else chatSendMessage({ upload_id: r.upload_id });
}

function chatNewSetTab(tab) {
  document.querySelectorAll('#chat-new-tab-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.chatnewtab === tab));
  document.getElementById('chat-new-local').style.display = tab === 'local' ? '' : 'none';
  document.getElementById('chat-new-url').style.display   = tab === 'url' ? '' : 'none';
}

function _chatNormUrl(raw) {
  raw = (raw || '').trim();
  if (!raw) return '';
  if (!/^https?:\/\//i.test(raw)) {
    // без схеми: локальна адреса — http, решта — https (сервер перевірить так само)
    const host = raw.split('/')[0].split(':')[0];
    const local = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host) || /\.(local|lan)$/.test(host);
    raw = (local ? 'http://' : 'https://') + raw;
  }
  try { const u = new URL(raw); return `${u.protocol}//${u.host}`.toLowerCase(); } catch { return ''; }
}

function _chatRenderBlockBtn() {
  const b = document.getElementById('chat-block-btn');
  if (!b) return;
  const p = _chatCurrentPeer || {};
  b.style.display = p.peer_url ? '' : 'none';
  b.textContent = p.blocked ? '✓ Розблокувати' : '🚫 Заблокувати архів';
}

async function chatToggleBlock() {
  const p = _chatCurrentPeer;
  if (!p?.peer_url) return;
  const block = !p.blocked;
  if (block && !confirm(`Не приймати повідомлень з ${p.peer_url}?`)) return;
  const r = await fetch(`${API}/api/chat/block`, { method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ peer_url: p.peer_url, block }) }).then(x => x.json()).catch(() => ({}));
  if (r.error) { toast(r.error, 'err'); return; }
  p.blocked = block;
  _chatRenderBlockBtn();
  toast(block ? 'Архів заблоковано' : 'Архів розблоковано', 'ok');
}

async function chatShowNewMessageModal() {
  document.getElementById('chat-new-modal').style.display = 'flex';
  try {
    const users = await fetch(`${API}/api/users/list`).then(r => r.json());
    const sel = document.getElementById('chat-new-local-select');
    sel.innerHTML = `<option value="">— обери юзера —</option>` +
      users.filter(u => u.id !== _currentUser?.user_id).map(u => `<option value="${u.id}" data-label="${esc(u.display_name||u.email)}">${esc(u.display_name||u.email)}</option>`).join('');
  } catch {}
}

function chatStartConversation() {
  const activeTab = document.querySelector('#chat-new-tab-toggle .filter-btn.active').dataset.chatnewtab;
  document.getElementById('chat-new-modal').style.display = 'none';
  if (activeTab === 'url') {
    const url = _chatNormUrl(document.getElementById('chat-new-url-input').value);
    const nick = document.getElementById('chat-new-url-nick').value.trim();
    if (!url) { toast('Впиши адресу архіву', 'err'); return; }
    chatOpenThread({ peer_url: url, peer_identity: nick, label: `${nick || 'власник'}@${url.replace(/^https?:\/\//, '')}` });
    return;
  }
  const sel = document.getElementById('chat-new-local-select');
  if (!sel.value) { toast('Обери юзера', 'err'); return; }
  const label = sel.options[sel.selectedIndex].dataset.label;
  chatOpenThread({ peer_local_user_id: parseInt(sel.value), label });
}

async function loadUsersFull() {
  loadPasswordRequests();
  const tbody = document.getElementById('users-full-tbody');
  tbody.innerHTML = `<tr><td colspan="7" class="empty-row">${t('common.loading')}</td></tr>`;
  try {
    const [users, codes] = await Promise.all([
      fetch(`${API}/api/users/list-full`).then(r => r.json()),
      fetch(`${API}/api/access-codes`).then(r => r.json()).catch(() => []),
    ]);
    if (!Array.isArray(users) || !users.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-row">—</td></tr>`;
      return;
    }
    // вільні коди: ще ніхто не використав і не відкликано
    const freeCodes = (Array.isArray(codes) ? codes : []).filter(c => !c.used_at && !c.is_revoked);
    // групами за роллю; кожну можна згорнути (запамʼятовується в браузері)
    const groups = [
      ['admin',     '👑 Адміни',     u => u.role === 'admin' || u.is_owner],
      ['moderator', '🛡 Модератори', u => u.role === 'moderator' && !u.is_owner],
      ['user',      '👤 Користувачі', u => u.role !== 'admin' && u.role !== 'moderator' && !u.is_owner],
    ];
    const collapsed = _usersCollapsed();
    tbody.innerHTML = groups.map(([key, label, test]) => {
      const list = users.filter(test);
      if (!list.length) return '';
      const closed = collapsed.includes(key);
      return `<tr class="user-group-head${closed ? ' closed' : ''}" onclick="usersToggleGroup('${key}')">
          <td colspan="7"><span class="user-group-chevron">▾</span> ${label} <span class="muted">(${list.length})</span></td>
        </tr>` + list.map(u => _userRowHtml(u, key, closed)).join('');
    }).join('') + _freeCodesHtml(freeCodes, collapsed.includes('codes'));
  } catch {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">${t('catalog.error')}</td></tr>`;
  }
}

// Коди реєстрації, якими ще ніхто не скористався, — окремою групою внизу.
// Використаний код видно в рядку того, хто ним зареєструвався.
function _freeCodesHtml(codes, closed) {
  const head = `<tr class="user-group-head${closed ? ' closed' : ''}" onclick="usersToggleGroup('codes')">
      <td colspan="7"><span class="user-group-chevron">▾</span> 🎟 Вільні коди <span class="muted">(${codes.length})</span></td>
    </tr>`;
  const hide = closed ? ' style="display:none"' : '';
  if (!codes.length) return head + `<tr data-group="codes"${hide}><td colspan="7" class="empty-row">Вільних кодів немає — «+ Створити код» угорі</td></tr>`;
  return head + codes.map(c => `
      <tr data-group="codes"${hide}>
        <td class="muted">—</td>
        <td class="muted">—</td>
        <td><span class="reg-code" onclick="copyAccessCode('${esc(c.code)}')" title="Скопіювати">${esc(c.code)} ⧉</span></td>
        <td class="muted">вільний</td>
        <td style="color:var(--muted)" title="Створено">${fmtDate(c.created_at)}</td>
        <td class="muted">—</td>
        <td><span class="catalog-crumb" style="color:var(--red)" onclick="revokeAccessCode(${c.id})" title="Відкликати цей код">🗑</span></td>
      </tr>`).join('');
}

function copyAccessCode(code) {
  try { navigator.clipboard.writeText(code); toast(`Скопійовано: ${code}`, 'ok'); }
  catch { prompt('Код:', code); }
}

function _usersCollapsed() {
  try { return JSON.parse(localStorage.getItem('users.collapsed') || '[]'); } catch { return []; }
}

function usersToggleGroup(key) {
  const c = _usersCollapsed();
  const i = c.indexOf(key);
  if (i >= 0) c.splice(i, 1); else c.push(key);
  try { localStorage.setItem('users.collapsed', JSON.stringify(c)); } catch {}
  const closed = c.includes(key);
  document.querySelectorAll(`#users-full-tbody tr[data-group="${key}"]`).forEach(tr => tr.style.display = closed ? 'none' : '');
  document.querySelectorAll('#users-full-tbody .user-group-head').forEach(tr => {
    if (tr.getAttribute('onclick') === `usersToggleGroup('${key}')`) tr.classList.toggle('closed', closed);
  });
}

function _userRowHtml(u, group, closed) {
    return `
      <tr data-group="${group}"${closed ? ' style="display:none"' : ''}>
        <td style="font-weight:500">${esc(u.display_name || '—')}</td>
        <td style="color:var(--muted)">${esc(u.email)}</td>
        <td>${u.reg_code ? `<span class="reg-code" title="Код, за яким зареєструвався">${esc(u.reg_code)}</span>` : '<span class="muted">—</span>'}</td>
        <td>${userRoleCell(u)}</td>
        <td style="color:var(--muted)">${fmtDate(u.created_at)}</td>
        <td style="color:var(--muted)">${u.last_login_at ? fmtDate(u.last_login_at) : '— ще не заходив —'}</td>
        <td style="white-space:nowrap">${u.id !== _currentUser?.user_id && !u.is_owner && (u.role !== 'admin' || _currentUser?.is_owner) ? `<span class="catalog-crumb" onclick="adminSetPassword(${u.id}, '${esc(u.display_name || u.email)}')" title="Задати новий пароль">🔑</span> <span class="catalog-crumb" style="color:var(--red)" onclick="deleteUserAccount(${u.id}, '${esc(u.display_name || u.email)}')" title="Видалити обліковий запис">🗑</span>` : ''}</td>
      </tr>`;
}

const ROLE_LABELS = { user: 'користувач', moderator: 'модератор', admin: 'адмін' };

// Хто кому може змінити роль:
//   Творець (id 1) — завжди адмін, його роль не змінює ніхто;
//   лише Творець призначає й знімає адмінів;
//   звичайний адмін — лише між «користувач» і «модератор».
function userRoleCell(u) {
  const me = _currentUser || {};
  if (u.is_owner) return `<span class="badge badge-owner" title="Власник архіву: завжди адмін, лише він призначає й знімає адмінів">👑 Творець</span>${u.id === me.user_id ? ' <span class="muted" style="font-size:11px">(ти)</span>' : ''}`;
  if (u.id === me.user_id) return `<span class="badge badge-e621">${ROLE_LABELS[u.role] || u.role}</span> <span class="muted" style="font-size:11px">(ти)</span>`;
  if (u.role === 'admin' && !me.is_owner)
    return `<span class="badge badge-e621" title="Знімати адмінів може лише Творець">${ROLE_LABELS.admin}</span>`;
  const roles = me.is_owner ? ['user', 'moderator', 'admin'] : ['user', 'moderator'];
  return `<select class="role-select" onchange="setUserRole(${u.id}, this)" data-prev="${u.role}">
            ${roles.map(r => `<option value="${r}" ${u.role === r ? 'selected' : ''}>${ROLE_LABELS[r]}</option>`).join('')}
          </select>`;
}

async function setUserRole(userId, sel) {
  const role = sel.value, prev = sel.dataset.prev;
  const warn = role === 'admin' ? '\n\nАдмін може все: налаштування, користувачі, остаточне видалення.' :
               role === 'moderator' ? '\n\nМодератор: завантаження, переміщення, кошик (без остаточного видалення), теги, черга «На перевірку». Усе пишеться в журнал.' : '';
  if (!confirm(`Змінити роль на «${ROLE_LABELS[role]}»?${warn}`)) { sel.value = prev; return; }
  try {
    const r = await fetch(`${API}/api/users/${userId}/role`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ role })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); sel.value = prev; return; }
    sel.dataset.prev = role;
    toast(`Роль: ${ROLE_LABELS[role]}`, 'ok');
    loadUsersFull();
  } catch { toast('Помилка мережі', 'err'); sel.value = prev; }
}

// ── Журнал модераторів ────────────────────────────────────────────────────
const MODLOG_PAGE = 100;
let _modlogOffset = 0;

async function modlogLoad(offset = 0) {
  _modlogOffset = offset;
  const box = document.getElementById('modlog-list');
  if (!box) return;
  if (!offset) box.innerHTML = `<div class="muted">${t('common.loading')}</div>`;
  const params = new URLSearchParams({ limit: MODLOG_PAGE, offset });
  const uid = document.getElementById('modlog-user').value;
  if (uid) params.set('user_id', uid);
  if (document.getElementById('modlog-active').checked) params.set('active', '1');
  let d;
  try { d = await fetch(`${API}/api/modlog?${params}`).then(r => r.json()); }
  catch { box.innerHTML = `<div class="muted">${t('catalog.error')}</div>`; return; }
  if (d.error) { box.innerHTML = `<div class="muted">${esc(d.error)}</div>`; return; }
  const sel = document.getElementById('modlog-user');
  const cur = sel.value;
  sel.innerHTML = '<option value="">усі модератори</option>' +
    (d.moderators || []).map(m => `<option value="${m.id}" ${String(m.id) === cur ? 'selected' : ''}>${esc(m.name)}</option>`).join('');
  document.getElementById('modlog-summary').textContent = `записів: ${d.total}`;
  const rows = (d.items || []).map(e => `
    <div class="modlog-row${e.undone_at ? ' undone' : ''}" id="modlog-${e.id}">
      <div class="modlog-when">${fmtDate(e.created_at)}</div>
      <div class="modlog-main">
        <div class="modlog-what"><b>${esc(e.user)}</b> · ${esc(e.label)}</div>
        <div class="modlog-detail">${esc(e.summary || '')}</div>
        ${e.undone_at ? `<div class="modlog-detail" style="color:var(--green)">↩ відмінено ${fmtDate(e.undone_at)}${e.undone_by ? ' · ' + esc(e.undone_by) : ''}</div>` : ''}
        ${e.undo_error && !e.undone_at ? `<div class="modlog-detail" style="color:var(--red)">✗ ${esc(e.undo_error)}</div>` : ''}
      </div>
      <div>${!e.undone_at && e.undoable
        ? `<button class="btn btn-ghost btn-sm" onclick="modlogUndo(${e.id})">↩ Відмінити</button>`
        : (!e.undoable ? '<span class="muted" style="font-size:11px">лише запис</span>' : '')}</div>
    </div>`).join('');
  const more = d.total > offset + MODLOG_PAGE
    ? `<div style="text-align:center;padding:12px"><button class="btn" onclick="modlogLoad(${offset + MODLOG_PAGE})">↓ Ще</button></div>` : '';
  if (offset) {
    document.getElementById('modlog-more')?.remove();
    box.insertAdjacentHTML('beforeend', rows + (more ? `<div id="modlog-more">${more}</div>` : ''));
  } else {
    box.innerHTML = (rows || '<div class="muted">Модератори ще нічого не робили.</div>') + (more ? `<div id="modlog-more">${more}</div>` : '');
  }
}

async function modlogUndo(id) {
  if (!confirm('Відмінити цю дію?')) return;
  try {
    const r = await fetch(`${API}/api/modlog/${id}/undo`, { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.error || t('toast.error'), 'err'); modlogLoad(0); return; }
    toast(`Відмінено${d.note ? ': ' + d.note : ''}`, 'ok');
    modlogLoad(0);
  } catch { toast('Помилка мережі', 'err'); }
}

async function deleteUserAccount(userId, name) {
  if (!confirm(`Видалити обліковий запис "${name}"? Це незворотньо.`)) return;
  try {
    const r = await fetch(`${API}/api/users/${userId}`, { method: 'DELETE' }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    toast(`"${r.removed}" видалено`, 'ok');
    loadUsersFull();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

// Коди реєстрації тепер живуть у списку користувачів (Коди допуску → Користувачі)
function loadAccessCodes() { return loadUsersFull(); }

async function generateAccessCode() {
  try {
    const r = await fetch(`${API}/api/access-codes`, {method: 'POST'});
    const d = await r.json();
    if (!r.ok) { toast(d.error || t('toast.codecreateerr'), 'err'); return; }
    toast(`${t('toast.codecreated')}: ${d.code}`, 'ok');
    try { navigator.clipboard.writeText(d.code); } catch {}
    // новий код має бути видно — розгортаємо групу вільних кодів
    const c = _usersCollapsed().filter(k => k !== 'codes');
    try { localStorage.setItem('users.collapsed', JSON.stringify(c)); } catch {}
    loadUsersFull();
  } catch (e) {
    toast(t('toast.codecreateerr'), 'err');
  }
}

async function revokeAccessCode(id) {
  if (!confirm(t('confirm.revokecode'))) return;
  try {
    const r = await fetch(`${API}/api/access-codes/${id}`, {method: 'DELETE'});
    if (!r.ok) { toast(t('toast.revokeerr'), 'err'); return; }
    toast(t('toast.coderevoked'), 'ok');
    loadUsersFull();
  } catch (e) {
    toast(t('toast.revokeerr'), 'err');
  }
}

// ── Каталог ───────────────────────────────────────────────────────────────
// ── Онлайн (live e621/Inkbunny пошук) ───────────────────────────────────────
let _liveSource = 'e621';
let _liveShowingFavorites = false;
let _liveInPool = false;
let _liveFavsUsername = '';
let _liveResults = [];

// Посилання на файл/превʼю результату «Онлайн». Сторонні CDN (e621,
// Inkbunny) йдуть через наш проксі; джерело, яке віддає файли САМЕ через
// наш сервер (Telegram: /api/apps/telegram/media/...), — напряму.
function liveMediaUrl(u) {
  if (!u) return '';
  return u.startsWith('/') ? `${API}${u}` : `${API}/api/live/proxy?url=${encodeURIComponent(u)}`;
}

function liveSetSource(src) {
  _liveSource = src;
  document.querySelectorAll('#live-source-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.source === src));
  // Панель джерела від застосунку (напр. Telegram: акаунт і канали)
  const panel = document.getElementById('live-source-panel');
  if (panel && panel.dataset.source !== src) {
    panel.dataset.source = src;
    if (typeof renderZone === 'function') renderZone(panel);
  }
  // Набір полів і підписи до них — справа джерела, не ядра.
  liveRenderFields();
}

let _liveSources = [];

async function liveLoadStatus() {
  // Джерела описують себе самі (/api/live/sources): назва, потрібні ключі
  // і що вміють. Сторінка одна на всіх і будується з цього опису — тому
  // нове джерело (хоч застосунок, хоч додане через форму) з'являється тут
  // без жодних змін у фронтенді.
  const el = document.getElementById('live-status');
  el.textContent = t('common.loading');
  try {
    _liveSources = await fetch(`${API}/api/live/sources`, {credentials:'include'}).then(r => r.json());
    const toggle = document.getElementById('live-source-toggle');
    if (toggle) {
      toggle.innerHTML = _liveSources.map(s =>
        `<button class="filter-btn${s.id === _liveSource ? ' active' : ''}" data-source="${esc(s.id)}"
                 onclick="liveSetSource('${esc(s.id)}')">${esc(s.label)}${s.custom ? ' •' : ''}</button>`).join('');
    }
    if (!_liveSources.length) {
      el.textContent = 'Джерел немає — увімкни e621 чи Inkbunny у «Застосунках» або додай своє кнопкою ➕ Джерело';
      document.getElementById('live-setup-notice').style.display = 'none';
      return;
    }
    if (!_liveSources.find(s => s.id === _liveSource)) liveSetSource(_liveSources[0].id);

    el.textContent = _liveSources.map(s => {
      if (!s.credential_fields.length) return `${s.label}: ✓ готове`;
      return `${s.label}: ${s.configured ? '✓ ключ вказано' : '🔑 ключ не вказано'}`;
    }).join('   |   ');
    document.getElementById('live-setup-notice').style.display =
      _liveSources.some(s => s.credential_fields.length && !s.configured) ? 'block' : 'none';
    liveRenderFields();
  } catch {
    el.textContent = t('toast.error');
  }
}

// ── Поля пошуку ─────────────────────────────────────────────────────
// Ядро не знає, які поля потрібні конкретному джерелу. Воно бере опис
// із /api/live/sources (search_fields) і малює рівно його. Прибрати
// «автора» в e621 чи додати своє поле — правка в застосунку джерела,
// сюди лізти не треба.
const LIVE_INPUT_CSS = 'background:var(--surface2);border:1px solid var(--border);' +
  'border-radius:6px;padding:8px 12px;font-family:var(--mono);font-size:13px;' +
  'color:var(--text);outline:none';

function liveCurrentFields() {
  const s = _liveSources.find(x => x.id === _liveSource);
  return (s && s.search_fields) || [];
}

function liveField(id)    { return document.getElementById(`live-f-${id}`); }
function liveFieldVal(id) { const e = liveField(id); return e ? e.value.trim() : ''; }
function liveSetField(id, v) { const e = liveField(id); if (e) e.value = v ?? ''; }

// Усі заповнені поля одним об'єктом — саме це й їде в запит пошуку.
function liveCollectFields() {
  const out = {};
  for (const f of liveCurrentFields()) {
    const v = liveFieldVal(f.id);
    if (v) out[f.id] = v;
  }
  return out;
}

function liveRenderFields() {
  const box = document.getElementById('live-fields');
  if (!box) return;
  // Значення, які юзер уже ввів, переносимо в нову розмітку: при зміні
  // джерела однойменні поля (теги) не повинні очищатись.
  const keep = {};
  for (const f of liveCurrentFields()) keep[f.id] = liveFieldVal(f.id);

  box.innerHTML = liveCurrentFields().map(f => {
    if (f.type === 'select') {
      const opts = (f.options || []).map(o =>
        `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
      return `<select id="live-f-${esc(f.id)}" style="${LIVE_INPUT_CSS};padding:8px 10px;font-size:12px">${opts}</select>`;
    }
    const w = f.width ? `min-width:${parseInt(f.width) || 180}px` : 'min-width:180px';
    const inp = `<input id="live-f-${esc(f.id)}" autocomplete="off" placeholder="${esc(f.placeholder || '')}" style="${LIVE_INPUT_CSS};${w}">`;
    // Поле з автодоповненням потребує обгортки під випадний список
    return f.autocomplete
      ? `<div style="position:relative">${inp}<div id="live-f-${esc(f.id)}-suggest" class="tag-suggest-box" style="display:none"></div></div>`
      : inp;
  }).join('');

  for (const f of liveCurrentFields()) {
    if (keep[f.id]) liveSetField(f.id, keep[f.id]);
    if (f.autocomplete === 'tags') {
      liveSetupTagAutocomplete(`live-f-${f.id}`, `live-f-${f.id}-suggest`);
    }
    // Enter у будь-якому текстовому полі = натиснути «Шукати»
    const el = liveField(f.id);
    if (el && f.type !== 'select') {
      el.addEventListener('keydown', e => { if (e.key === 'Enter') liveSearchFresh(); });
    }
  }

  // Кнопка «Переглянути чиєсь улюблене» має сенс лише з відповідним полем
  const viewBtn = document.getElementById('live-view-favs-btn');
  if (viewBtn) viewBtn.style.display = liveField('favs_username') ? '' : 'none';
}



async function liveCopyCredsFromConfig() {
  try {
    const r = await fetch(`${API}/api/live/credentials/copy-from-config`, { method: 'POST' }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    toast('Скопійовано з Конфігу', 'ok');
    liveShowCredentialsModal();  // перезавантажуємо модалку, щоб показати оновлений статус
  } catch {
    toast('Помилка мережі', 'err');
  }
}

async function liveShowCredentialsModal() {
  const box = document.getElementById('creds-fields');
  if (!_liveSources.length) {
    try { _liveSources = await fetch(`${API}/api/live/sources`, {credentials:'include'}).then(r => r.json()); } catch {}
  }
  const withKeys = _liveSources.filter(s => s.credential_fields.length);
  box.innerHTML = withKeys.length ? withKeys.map(s => `
    <div style="margin-bottom:16px">
      <div style="font-weight:600;font-size:12.5px;margin-bottom:8px">${esc(s.label)}</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${s.credential_fields.map(f => `
          <input type="${f.type === 'password' ? 'password' : 'text'}"
                 data-src="${esc(s.id)}" data-key="${esc(f.key)}"
                 placeholder="${esc(f.label)}${f.filled ? ' — збережено, лиши пустим щоб не міняти' : ''}"
                 value=""
                 style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none">`).join('')}
      </div>
    </div>`).join('') : '<div class="muted">Жодне з джерел не потребує ключів.</div>';
  document.getElementById('live-creds-modal').style.display = 'flex';
}


async function liveSaveCredentials() {
  // Збираємо те, що намальовано: скільки джерел, стільки й блоків
  const payload = {};
  document.querySelectorAll('#creds-fields input[data-src]').forEach(inp => {
    const v = inp.value.trim();
    if (!v) return;                       // порожнє = не міняти
    (payload[inp.dataset.src] ||= {})[inp.dataset.key] = v;
  });
  try {
    const r = await fetch(`${API}/api/live/credentials`, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    if (!r.ok) throw new Error((await r.json()).error || 'помилка');
    document.getElementById('live-creds-modal').style.display = 'none';
    toast('Ключі збережено', 'ok');
    liveLoadStatus();
  } catch (e) { toast('Не вдалося зберегти: ' + e.message, 'err'); }
}


let _livePage = 1;

function liveSearchFresh() {
  _liveHistory = [];
  liveUpdateBackBtn();
  _liveCurrentArtistName = '';
  _liveShowingFavorites = false;
  _liveInPool = false;
  liveSearch(1);
}

async function liveSearch(page = 1) {
  // Вписано чийсь юзернейм у «чиєсь улюблене» — Enter і «Шукати» відкривають
  // саме його улюблене (з тими самими тегами й сортуванням). Раніше це
  // робила лише кнопка «👁 Переглянути», а Enter запускав звичайний пошук.
  const favUser = (typeof liveFieldVal === 'function') ? (liveFieldVal('favs_username') || '').trim() : '';
  if (favUser) { liveShowFavorites(page, favUser); return; }
  _livePage = page;
  liveBackToResults();
  const grid = document.getElementById('live-grid');
  grid.innerHTML = `<div style="opacity:.6">${t('common.loading')}</div>`;
  document.getElementById('live-download-btn').style.display =
    (canModerate() && liveGetCurrentArtist()) ? '' : 'none';
  document.getElementById('live-attach-btn').style.display =
    (canModerate() && liveGetCurrentArtist()) ? '' : 'none';

  // id поля = ім'я параметра. Ядро не перелічує поля поіменно —
  // бере те, що оголосило джерело.
  const params = new URLSearchParams({ source: _liveSource, page });
  for (const [k, v] of Object.entries(liveCollectFields())) params.set(k, v);

  try {
    const d = await fetch(`${API}/api/live/search?${params}`).then(r => r.json());
    if (d.error && !d.results?.length) {
      grid.innerHTML = `<div style="opacity:.6">${esc(d.error)}</div>`;
      document.getElementById('live-pager').style.display = 'none';
      return;
    }
    _liveResults = d.results || [];
    liveRenderResults();
    const pager = document.getElementById('live-pager');
    pager.style.display = !d.pool_name ? '' : 'none';
    liveRenderPager(d.total);
  } catch (e) {
    grid.innerHTML = `<div style="opacity:.6">${t('catalog.error')}</div>`;
  }
}

let _liveTotalPages = null;

// «Сторінка [N] з M»: M — якщо джерело знає (Inkbunny — точно; e621 — для
// запиту з одного тегу, приблизно). Невідомо — лише поле для номера.
function liveRenderPager(total) {
  _liveTotalPages = total?.pages || null;
  const inp = document.getElementById('live-page-input');
  inp.value = _livePage;
  if (_liveTotalPages) inp.max = _liveTotalPages; else inp.removeAttribute('max');
  const tot = document.getElementById('live-page-total');
  if (_liveTotalPages) {
    const cnt = total.count ? ` · ${total.approx ? '≈' : ''}${Number(total.count).toLocaleString('uk-UA')} ${t('live.works')}` : '';
    tot.textContent = `${t('pager.of')} ${total.approx ? '≈' : ''}${_liveTotalPages.toLocaleString('uk-UA')}${cnt}`;
  } else tot.textContent = '';
  const isLast = !!total?.last;
  document.getElementById('live-first-btn').disabled = _livePage <= 1;
  document.getElementById('live-prev-btn').disabled  = _livePage <= 1;
  document.getElementById('live-next-btn').disabled  = isLast;
  const lastBtn = document.getElementById('live-last-btn');
  lastBtn.style.display = _liveTotalPages ? '' : 'none';
  lastBtn.disabled = isLast;
}

function liveGoToPage(n) {
  if (!Number.isFinite(n) || n < 1) { document.getElementById('live-page-input').value = _livePage; return; }
  if (_liveTotalPages && n > _liveTotalPages) n = _liveTotalPages;
  if (n === _livePage) return;
  if (_liveShowingFavorites) liveShowFavorites(n, _liveFavsUsername);
  else liveSearch(n);
  document.getElementById('live-grid')?.scrollIntoView({ block: 'start' });
}

function liveChangePage(delta) { liveGoToPage(_livePage + delta); }

// ── Автодоповнення тегів (e621) ─────────────────────────────────────────
function liveSetupTagAutocomplete(inputId, boxId) {
  const input = document.getElementById(inputId);
  const box   = document.getElementById(boxId);
  let debounceTimer;

  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    const raw = input.value;
    // Автодоповнюємо тільки останнє слово (теги розділені пробілом)
    const parts = raw.split(' ');
    const last  = parts[parts.length - 1].replace(/^-/, '').trim();
    if (last.length < 2) { box.style.display = 'none'; return; }
    debounceTimer = setTimeout(async () => {
      try {
        const tags = await fetch(`${API}/api/live/tag-autocomplete?q=${encodeURIComponent(last)}&source=${_liveSource}`).then(r => r.json());
        if (!Array.isArray(tags) || !tags.length) { box.style.display = 'none'; return; }
        box.innerHTML = tags.map(tg => `
          <div class="tag-suggest-item" data-tag="${esc(tg.name)}">
            <span>${esc(tg.name)}</span>
            <span class="tag-suggest-count">${fmt(tg.post_count)}</span>
          </div>`).join('');
        box.style.display = 'block';
      } catch { box.style.display = 'none'; }
    }, 250);
  });

  box.addEventListener('click', e => {
    const item = e.target.closest('[data-tag]');
    if (!item) return;
    const parts = input.value.split(' ');
    parts[parts.length - 1] = item.dataset.tag;
    input.value = parts.join(' ') + ' ';
    box.style.display = 'none';
    input.focus();
  });

  document.addEventListener('mousedown', e => {
    if (!box.contains(e.target) && e.target !== input) box.style.display = 'none';
  });
}
// Автодоповнення навішується в liveRenderFields() на ті поля, які
// самі про це попросили (autocomplete: "tags") — фіксованих id більше немає.

function liveRenderResults() {
  const grid = document.getElementById('live-grid');
  if (!_liveResults.length) {
    grid.innerHTML = `<div style="opacity:.6">${t('catalog.empty')}</div>`;
    return;
  }
  grid.innerHTML = _liveResults.map((r, idx) => {
    const isImg = !r.is_video;
    const thumb = (r.in_library && r.download_id)
      ? `${API}/api/gallery/stream/${r.download_id}`
      : liveMediaUrl(r.thumb_url);
    const inner = isImg
      ? `<img class="catalog-thumb" src="${thumb}" loading="lazy">`
      : `<div class="catalog-icon">🎬</div>`;
    return `<div class="catalog-item${r.in_library ? ' live-in-lib' : ''}" data-live-idx="${idx}"${r.in_library ? ` title="${r.library_copy ? 'Вже в архіві (копія з іншого сайту)' : 'Вже в архіві'}"` : ''}>
      ${inner}
      ${r.in_library ? '<span class="live-lib-badge">✓</span>' : ''}
      <div class="catalog-name">${_liveArtistsHtml(r) || esc(r.source)}${r.pool_id ? ' 📦' : ''}</div>
    </div>`;
  }).join('');
}

let _liveIdx = -1;

document.getElementById('live-grid')?.addEventListener('click', e => {
  const box = e.target.closest('[data-live-idx]');
  if (!box) return;
  liveShowItem(parseInt(box.dataset.liveIdx, 10));
});

let _pendingBookmarkIdx = null;

function liveSaveBookmark(idx) {
  if (!_liveResults[idx]) return;
  _pendingBookmarkIdx = idx;
  const input = document.getElementById('bookmark-name-input');
  const r = _liveResults[idx];
  input.value = (r.artists||[]).join(', ') || r.source || '';
  document.getElementById('bookmark-name-modal').style.display = 'flex';
  setTimeout(() => input.select(), 50);
}

async function liveConfirmSaveBookmark() {
  const name = document.getElementById('bookmark-name-input').value.trim();
  if (!name) { toast('Впиши назву закладки', 'err'); return; }
  const r = _liveResults[_pendingBookmarkIdx];
  if (!r) { document.getElementById('bookmark-name-modal').style.display = 'none'; return; }
  const bookmark = {
    name,
    source:   _liveSource,
    mode:     _liveInPool ? 'pool' : (_liveShowingFavorites ? 'favorites' : (_liveCurrentArtistName ? 'artist' : 'search')),
    fields:   liveCollectFields(),   // усі поля джерела, якими б вони не були
    tags:     liveFieldVal('tags'),     // ці три — для сумісності зі старими
    exclude:  liveFieldVal('exclude'),  // закладками, збереженими до переходу
    sort:     liveFieldVal('sort'),     // на описані джерелом поля
    artist:   _liveCurrentArtistName,
    favsUsername: _liveFavsUsername,
    poolId:   r.pool_id || null,
    poolName: r.pool_name || null,
    page:     _livePage,
    item_id:      r.id,
    item_source:  r.source,
    thumb_url:    r.thumb_url,
    artists_label: (r.artists||[]).join(', ') || r.source,
  };
  try {
    const resp = await fetch(`${API}/api/live/bookmarks`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(bookmark)
    }).then(x => x.json());
    if (resp.error) { toast(resp.error, 'err'); return; }
    toast('🔖 Закладку збережено', 'ok');
    document.getElementById('bookmark-name-modal').style.display = 'none';
    liveUpdateBookmarksCount();
  } catch {
    toast('Помилка мережі при збереженні закладки', 'err');
  }
}

async function liveUpdateBookmarksCount() {
  try {
    const list = await fetch(`${API}/api/live/bookmarks`).then(r => r.json());
    const el = document.getElementById('live-bookmarks-count');
    if (el) el.textContent = Array.isArray(list) && list.length ? `(${list.length})` : '';
  } catch {}
}

// Залишено для сумісності виклику при відкритті вкладки "Онлайн"
function liveCheckBookmark() {
  liveUpdateBookmarksCount();
}

let _bookmarksCache = [];

async function liveShowBookmarksModal() {
  document.getElementById('bookmarks-list-modal').style.display = 'flex';
  liveRenderBookmarkSourceButtons();
  document.querySelectorAll('#bookmarks-source-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.bsource === ''));
  await liveRenderBookmarksList('');
}

// Кнопки фільтра — з переліку джерел, а не вписані в розмітку.
// Додався новий застосунок-джерело — кнопка з'явилась сама.
function liveRenderBookmarkSourceButtons() {
  const box = document.getElementById('bookmarks-source-toggle');
  if (!box) return;
  const all = box.querySelector('[data-bsource=""]');
  box.innerHTML = '';
  if (all) box.appendChild(all);
  for (const s of _liveSources) {
    const b = document.createElement('button');
    b.className = 'filter-btn';
    b.dataset.bsource = s.id;
    b.textContent = s.label || s.id;
    b.onclick = () => liveRenderBookmarksList(s.id);
    box.appendChild(b);
  }
}

async function liveRenderBookmarksList(sourceFilter) {
  document.querySelectorAll('#bookmarks-source-toggle .filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.bsource === sourceFilter));
  const body = document.getElementById('bookmarks-list-body');
  body.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:8px 0">${t('common.loading')}</div>`;
  try {
    const params = sourceFilter ? `?source=${sourceFilter}` : '';
    const list = await fetch(`${API}/api/live/bookmarks${params}`).then(r => r.json());
    _bookmarksCache = Array.isArray(list) ? list : [];
    liveUpdateBookmarksCount();
    if (!_bookmarksCache.length) {
      body.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:8px 0">Закладок ще немає</div>`;
      return;
    }
    body.innerHTML = _bookmarksCache.map(bm => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;background:var(--surface2);border-radius:8px">
        <span class="badge badge-${bm.source}" style="flex-shrink:0">${bm.source}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(bm.name)}</div>
          <div style="font-size:10.5px;color:var(--muted)">${esc(bm.artists_label||'')} · ${bm.mode==='favorites'?'улюблене':bm.mode==='pool'?'pool':bm.mode==='artist'?'автор':'пошук'}</div>
        </div>
        <button class="btn btn-primary btn-sm" onclick="liveResumeBookmark(${bm.id})">▶</button>
        <button class="btn btn-ghost btn-sm" onclick="liveDeleteBookmark(${bm.id})" title="Видалити закладку">🗑</button>
      </div>`).join('');
  } catch {
    body.innerHTML = `<div style="color:var(--red);font-size:12px;padding:8px 0">${t('catalog.error')}</div>`;
  }
}

async function liveResumeBookmark(bookmarkId) {
  const bm = _bookmarksCache.find(b => b.id === bookmarkId);
  if (!bm) return;
  document.getElementById('bookmarks-list-modal').style.display = 'none';
  if (bm.source && bm.source !== _liveSource) liveSetSource(bm.source);
  // Нові закладки несуть усі поля; старі — лише ці три.
  const saved = bm.fields || { tags: bm.tags, exclude: bm.exclude, sort: bm.sort };
  Object.entries(saved).forEach(([k, v]) => liveSetField(k, v || ''));

  const jumpToItem = () => {
    const foundIdx = _liveResults.findIndex(r => String(r.id) === String(bm.item_id) && r.source === bm.item_source);
    if (foundIdx !== -1) liveShowItem(foundIdx);
    else toast('Роботу з закладки вже не знайдено в цих результатах — показано перші', 'err');
  };

  // Відтворюємо саме той пошук, яким ти реально користувався (теги/автор/
  // улюблене/pool) — pool відновлюється, лише якщо ти реально ГОРТАВ pool,
  // а не просто тому, що закладена робота випадково туди належала.
  if (bm.mode === 'pool' && bm.poolId) {
    await liveSearchPool(bm.poolId, bm.poolName);
    jumpToItem();
  } else if (bm.mode === 'favorites') {
    await liveShowFavorites(bm.page || 1, bm.favsUsername || '');
    jumpToItem();
  } else if (bm.mode === 'artist' && bm.artist) {
    liveSetField('artist', bm.artist);
    await liveSearchArtist(bm.artist, bm.source);
    jumpToItem();
  } else {
    await liveSearch(bm.page || 1);
    jumpToItem();
  }
}

async function liveDeleteBookmark(bookmarkId) {
  try {
    await fetch(`${API}/api/live/bookmarks/${bookmarkId}`, { method: 'DELETE' });
    toast('Закладку видалено', 'ok');
    const activeBtn = document.querySelector('#bookmarks-source-toggle .filter-btn.active');
    liveRenderBookmarksList(activeBtn?.dataset.bsource || '');
  } catch {
    toast('Помилка мережі', 'err');
  }
}

function liveShowItem(idx) {
  if (!_liveResults.length) return;
  idx = ((idx % _liveResults.length) + _liveResults.length) % _liveResults.length;
  _liveIdx = idx;
  const r = _liveResults[idx];
  const url = (r.in_library && r.download_id)
    ? `${API}/api/gallery/stream/${r.download_id}`
    : liveMediaUrl(r.file_url);
  const mediaEl = r.is_flash
    ? `<div id="ruffle-container-live" class="gallery-media" style="background:#000;display:flex;align-items:center;justify-content:center;flex:1 1 auto;min-height:0;max-width:calc(100vw - 400px);width:min(920px, calc(100vw - 400px));height:min(600px, calc(100vh - 200px));border-radius:8px"></div>`
    : r.is_video
    ? `<video src="${url}" controls autoplay class="gallery-media"></video>`
    : `<img src="${url}" class="gallery-media">`;
  const artistsHtml = (r.artists||[]).filter(Boolean).map(a =>
    `<span class="catalog-crumb" data-artist-link="${esc(a)}" data-artist-source="${esc(r.source)}">${esc(a)}</span>`
  ).join(', ') || '—';
  const tagsHtml = (r.tags||[]).slice(0, 20).map(tg => `<span class="tag-chip">${esc(tg?.name ?? tg)}</span>`).join('');

  document.getElementById('live-grid').style.display = 'none';
  const viewer = document.getElementById('live-viewer');
  viewer.style.display = '';
  viewer.innerHTML = `<div class="gallery-img-box">
    <div class="gallery-backdrop" id="live-backdrop"></div>
    <div class="gallery-counter">${idx+1} / ${_liveResults.length}</div>
    ${mediaEl}
    <div class="gallery-bottom-overlay">
      <div class="gallery-info-primary">
        <div class="gallery-info-left">
          <span class="catalog-crumb" style="font-size:17px" onclick="liveToggleFavorite(${idx})" id="live-fav-${idx}" title="${t('gal.favorite')}">${r.is_favorite ? '★' : '☆'}</span>
          <span class="gallery-artist-name">${artistsHtml}</span>
          <span class="badge badge-${r.source}">${r.source}</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="catalog-crumb" style="font-size:15px" onclick="liveSaveBookmark(${idx})" title="Зберегти закладку — продовжити звідси пізніше">🔖</span>
          <a href="${r.post_url}" target="_blank" class="gallery-info-link">↗ ${t('gal.original')}</a>
        </div>
      </div>
      ${r.in_library ? `<div class="gallery-info-secondary"><span style="color:var(--green)">✓ ${t('live.inlibrary')}${r.library_copy ? ' (копія з іншого сайту)' : ''}</span></div>` : ''}
      ${Object.keys(r.artists_in_archive || {}).length ? `<div class="gallery-info-secondary"><span style="color:var(--green)">✓ Автор в архіві: ${
          Object.entries(r.artists_in_archive).map(([n, a]) => n.toLowerCase() === a.toLowerCase() ? `«${esc(a)}»` : `«${esc(a)}» (тут — ${esc(n)})`).join(', ')}</span></div>` : ''}
      ${r.pool_id ? `<div class="pool-progress-badge" data-pool-link="${r.pool_id}" data-pool-name="${esc(r.pool_name||'')}" title="${t('live.viewpool')}">
        <i>📖</i>
        <span class="pool-progress-title">${esc(r.pool_name||'')}</span>
        ${_liveInPool ? `<div class="pool-progress-bar"><div class="pool-progress-fill" style="width:${Math.round(((idx+1)/_liveResults.length)*100)}%"></div></div>
        <span class="pool-progress-count">${t('gal.page')} ${idx+1} ${t('gal.of')} ${_liveResults.length}</span>` : `<span class="pool-progress-count">(${t('live.pkey')})</span>`}
      </div>` : ''}
      ${tagsHtml ? `<div class="gallery-tags-toggle" onclick="this.nextElementSibling.classList.toggle('tags-collapsed')">
        <span>🏷 ${(r.tags||[]).length} ${t('gal.tagscount')}</span>
        <span class="gal-tags-chevron">▾</span>
      </div>
      <div class="gallery-tags tags-collapsed">${tagsHtml}</div>` : ''}
    </div>
    <div style="display:flex;gap:8px;justify-content:center;margin-top:10px">
      <button class="btn btn-ghost" onclick="liveBackToResults()">← ${t('live.backtoresults')}</button>
      ${canModerate() && ['e621','inkbunny'].includes(r.source) && !r.in_library ? `<button class="btn btn-primary" onclick="liveDownloadOnePost()" title="Лише цю роботу — у теку автора, без решти його бібліотеки">⬇ Лише цей арт</button>` : ''}
      ${canModerate() ? `<button class="btn btn-ghost" onclick="liveDownloadArtistFromViewer()">⬇ ${t('live.downloadthisartist')}</button>
        <button class="btn btn-ghost" onclick="liveAttachArtistFromViewer()" title="Той самий автор уже є в архіві під іншою назвою">🔗 Приєднати до автора…</button>` : ''}
    </div>
    <div class="gallery-nav prev" onclick="liveGo(-1)" style="position:absolute;left:-64px;top:calc(50% - 60px)">‹</div>
    <div class="gallery-nav next" onclick="liveGo(1)"  style="position:absolute;right:-64px;top:calc(50% - 60px)">›</div>
  </div>`;
  if (r.is_flash) {
    try {
      window.RufflePlayer = window.RufflePlayer || {};
      const ruffle = window.RufflePlayer.newest();
      const player = ruffle.createPlayer();
      player.style.width = '100%';
      player.style.height = '100%';
      const container = document.getElementById('ruffle-container-live');
      if (container) {
        container.appendChild(player);
        player.load(url);  // той самий проксі — CORS-безпечно, той самий домен
      }
    } catch (e) {
      const container = document.getElementById('ruffle-container-live');
      if (container) container.innerHTML = `<span style="color:var(--muted);font-size:13px">Ruffle не завантажився: ${esc(String(e.message||e))}</span>`;
    }
  }
  if (!r.is_video && !r.is_flash) {
    const bd = document.getElementById('live-backdrop');
    if (bd) {
      const img = new Image();
      img.onload = () => { bd.style.backgroundImage = `url(${url})`; bd.classList.add('loaded'); };
      img.src = url;
    }
  }
  if (r.is_video) {
    const vol   = parseFloat(localStorage.getItem('gal_vol') ?? '1');
    const muted = localStorage.getItem('gal_muted') === 'true';
    const vid   = viewer.querySelector('video');
    if (vid) { vid.volume = vol; vid.muted = muted; }
  }
}
document.getElementById('live-viewer')?.addEventListener('click', e => {
  const artistEl = e.target.closest('[data-artist-link]');
  if (artistEl) { liveSearchArtist(artistEl.dataset.artistLink, artistEl.dataset.artistSource); return; }
  const poolEl = e.target.closest('[data-pool-link]');
  if (poolEl) { liveSearchPool(poolEl.dataset.poolLink, poolEl.dataset.poolName); return; }
});

async function liveShowFavorites(page = 1, username = '') {
  _liveHistory.push(liveSnapshotState());
  liveUpdateBackBtn();
  liveBackToResults();
  const grid = document.getElementById('live-grid');
  grid.innerHTML = `<div style="opacity:.6">${t('common.loading')}</div>`;
  _liveShowingFavorites = true;
  _liveFavsUsername = username;
  _liveInPool = false;
  _livePage = page;
  const params = new URLSearchParams({ source: _liveSource, page });
  for (const [k, v] of Object.entries(liveCollectFields())) {
    if (k !== 'favs_username') params.set(k, v);
  }
  if (username) params.set('username', username);
  try {
    const d = await fetch(`${API}/api/live/favorites?${params}`).then(r => r.json());
    if (d.error && !d.results?.length) {
      grid.innerHTML = `<div style="opacity:.6">${esc(d.error)}</div>`;
      document.getElementById('live-pager').style.display = 'none';
      return;
    }
    _liveResults = d.results || [];
    liveRenderResults();
    const pager = document.getElementById('live-pager');
    pager.style.display = '';
    liveRenderPager(d.total);
  } catch (e) {
    grid.innerHTML = `<div style="opacity:.6">${t('catalog.error')}</div>`;
  }
}

function liveShowFavoritesOfUser() {
  const username = liveFieldVal('favs_username');
  if (!username) { toast('Впиши юзернейм', 'err'); return; }
  liveShowFavorites(1, username);
}

function liveGo(delta) { liveShowItem(_liveIdx + delta); }

async function liveToggleFavorite(idx) {
  const r = _liveResults[idx];
  if (!r) return;
  try {
    const resp = await fetch(`${API}/api/live/favorite`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        source: r.source, id: r.id, artist: (r.artists||[])[0] || '',
        thumb_url: r.thumb_url, file_url: r.file_url, post_url: r.post_url,
        is_video: r.is_video, ext: r.ext,
      })
    }).then(x => x.json());
    if (resp.error) { toast(resp.error, 'err'); return; }
    r.is_favorite = resp.is_favorite;
    const el = document.getElementById(`live-fav-${idx}`);
    if (el) el.textContent = r.is_favorite ? '★' : '☆';
    toast(r.is_favorite ? '★ Додано в улюблене' : '☆ Прибрано з улюбленого', 'ok');
  } catch (e) {
    toast('Помилка мережі при зміні улюбленого', 'err');
  }
}

(function setupSwipe(wrapId, goFn) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  let startX = 0, startY = 0, tracking = false;
  wrap.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    tracking = true;
  }, { passive: true });
  wrap.addEventListener('touchend', e => {
    if (!tracking) return;
    tracking = false;
    const dx = e.changedTouches[0].clientX - startX;
    const dy = e.changedTouches[0].clientY - startY;
    if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
    goFn(dx < 0 ? 1 : -1);
  }, { passive: true });
})('live-viewer', liveGo);

let _liveHistory = [];

function liveSnapshotState() {
  return {
    results: _liveResults, idx: _liveIdx, page: _livePage, source: _liveSource,
    fields:  liveCollectFields(),
  };
}

function liveUpdateBackBtn() {
  const btn = document.getElementById('live-back-btn');
  if (btn) btn.style.display = _liveHistory.length ? '' : 'none';
}

function liveGoBack() {
  const prev = _liveHistory.pop();
  if (!prev) return;
  liveCurrentFields().forEach(f => liveSetField(f.id, (prev.fields || {})[f.id] || ''));
  _liveResults = prev.results;
  _livePage    = prev.page;
  liveRenderResults();
  liveUpdateBackBtn();
  if (prev.idx >= 0) liveShowItem(prev.idx);
  else liveBackToResults();
}

function liveBackToResults() {
  document.getElementById('live-viewer').style.display = 'none';
  document.getElementById('live-viewer').innerHTML = '';
  document.getElementById('live-grid').style.display = '';
}

function liveSearchArtist(name, source) {
  _liveHistory.push(liveSnapshotState());
  liveUpdateBackBtn();
  liveBackToResults();
  _liveShowingFavorites = false;
  _liveInPool = false;
  _liveCurrentArtistName = name;
  if (source && source !== _liveSource) liveSetSource(source);
  liveSetField('favs_username', '');      // роботи автора, а не чиєсь улюблене
  if ((source || _liveSource) === 'inkbunny') {
    liveSetField('artist', name);
    liveSetField('tags', '');
  } else {
    liveSetField('artist', '');
    liveSetField('tags', name);
  }
  return liveSearch();
}

async function liveSearchPool(poolId, poolName) {
  _liveHistory.push(liveSnapshotState());
  liveUpdateBackBtn();
  liveBackToResults();
  _liveShowingFavorites = false;
  _liveInPool = true;
  liveSetField('artist', '');
  // НЕ пишемо назву pool у поле тегів — воно призначене для реального
  // пошукового запиту і читається в інших місцях (завантаження автора,
  // закладки), тому напис пула там ставав би "фантомним" тегом.
  liveSetField('tags', '');
  const grid = document.getElementById('live-grid');
  grid.innerHTML = `<div style="opacity:.6">${t('common.loading')}</div>`;
  try {
    const d = await fetch(`${API}/api/live/search?source=${_liveSource}&pool_id=${poolId}`).then(r => r.json());
    if (d.error && !d.results?.length) {
      grid.innerHTML = `<div style="opacity:.6">${esc(d.error)}</div>`;
      return;
    }
    _liveResults = d.results || [];
    liveRenderResults();
    if (_liveResults.length) liveShowItem(0);
  } catch (e) {
    grid.innerHTML = `<div style="opacity:.6">${t('catalog.error')}</div>`;
  }
}

// «Лише цей арт»: одна робота в теку автора (новий автор — без автооновлення)
async function liveDownloadOnePost() {
  const r = _liveResults[_liveIdx];
  if (!r) return;
  const names = (r.artists || []).filter(Boolean);
  // автор, який уже є в архіві (зокрема під іншою назвою), — у його теку
  const known = r.artists_in_archive || {};
  let name = names.find(n => known[n]) ? known[names.find(n => known[n])] : names[0];
  if (names.length > 1 && !names.some(n => known[n])) {
    name = prompt(`У роботи кілька авторів: ${names.join(', ')}\nУ чию теку покласти?`, names[0]);
    if (!name) return;
  }
  if (!name) name = prompt('Автора не вказано. У яку теку автора покласти?', '');
  if (!name) return;
  try {
    const resp = await fetch(`${API}/api/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ artist: name.trim(), single_post: `${r.source}:${r.id}` })
    });
    const d = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast(d.error || t('toast.starterr'), 'err'); return; }
    toast(`Качаю цей арт у «${name}» — хід видно у вкладці «Запуск»`, 'ok');
  } catch { toast(t('toast.starterr'), 'err'); }
}

function liveDownloadArtistFromViewer() {
  const r = _liveResults[_liveIdx];
  const names = (r.artists||[]).filter(Boolean);
  if (!names.length) { toast(t('live.noartist'), 'err'); return; }
  if (names.length === 1) {
    liveDownloadArtistByName(names[0], r.source);
    return;
  }
  liveShowArtistPicker(names, r.source);
}

function liveDownloadArtistByName(name, source) {
  if (source === 'inkbunny') {
    liveSetField('artist', name);
  } else {
    liveSetField('tags', name);
  }
  _liveCurrentArtistName = name;
  liveDownloadArtist();
}

function liveShowArtistPicker(names, source) {
  const list = document.getElementById('artist-picker-list');
  list.innerHTML = names.map(n => `<div class="artist-opt" data-picker-name="${esc(n)}">${esc(n)}</div>`).join('');
  document.getElementById('artist-picker-modal').dataset.source = source;
  document.getElementById('artist-picker-modal').style.display = 'flex';
}
document.getElementById('artist-picker-list')?.addEventListener('click', e => {
  const opt = e.target.closest('[data-picker-name]');
  if (!opt) return;
  const source = document.getElementById('artist-picker-modal').dataset.source;
  document.getElementById('artist-picker-modal').style.display = 'none';
  liveDownloadArtistByName(opt.dataset.pickerName, source);
});


let _liveCurrentArtistName = '';
function liveGetCurrentArtist() {
  if (_liveSource === 'inkbunny') {
    return liveFieldVal('artist');
  }
  const tagsVal = liveFieldVal('tags');
  // Якщо це один простий тег без операторів e621 (rating:, order:, пробіли,
  // -виняток) — вважаємо це кандидатом на ім'я автора для кнопки завантаження
  if (tagsVal && !tagsVal.includes(' ') && !tagsVal.includes(':') && !tagsVal.startsWith('-')) {
    return tagsVal;
  }
  return _liveCurrentArtistName || '';
}

async function liveDownloadArtist() {
  const artist = liveGetCurrentArtist();
  if (!artist) return;

  // Перевіряємо, чи цей нік уже відомий у бібліотеці (можливий дубль-акаунт)
  try {
    const check = await fetch(`${API}/api/artists/check-name?name=${encodeURIComponent(artist)}`).then(r => r.json());
    if (check.exists) {
      // нік — це вже автор в архіві (сам або його інша назва): оновлюємо його
      if (!confirm(`«${artist}» — це автор «${check.artist_name}» в архіві.\n\n` +
                   `Завантажити нове в «${check.artist_name}»? Шукатиме за всіма його назвами, копії відсіються.`)) return;
      _liveRunArtist(check.artist_name);
      return;
    }
  } catch {}

  if (!confirm(`${t('live.confirmdl')} «${artist}»?`)) return;
  _liveRunArtist(artist);
}

// Автори картки: той, що вже є в архіві (сам чи під іншою назвою), — з ✓
function _liveArtistsHtml(r) {
  const known = r.artists_in_archive || {};
  return (r.artists || []).map(a => known[a]
    ? `<span class="live-artist-known" title="В архіві: ${esc(known[a])}">${esc(a)} ✓</span>`
    : esc(a)).join(', ');
}

// ── Приєднати нік до автора, який уже є в архіві ─────────────────────
function liveAttachArtist(name) {
  name = name || liveGetCurrentArtist();
  if (!name) return;
  _attachPickArtist(name);
}

function liveAttachArtistFromViewer() {
  const r = _liveResults[_liveIdx];
  const names = (r.artists || []).filter(Boolean);
  if (!names.length) { toast(t('live.noartist'), 'err'); return; }
  const name = names.length === 1 ? names[0]
    : prompt(`Який нік приєднати?\n${names.join(', ')}`, names[0]);
  if (name) _attachPickArtist(name.trim());
}

async function _attachPickArtist(name) {
  let artists = [];
  try { artists = await fetch(`${API}/api/artists?filter=all`).then(r => r.json()); } catch {}
  if (!Array.isArray(artists)) artists = [];
  document.getElementById('attach-modal')?.remove();
  const m = document.createElement('div');
  m.id = 'attach-modal';
  m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;display:flex;align-items:center;justify-content:center;padding:16px';
  m.innerHTML = `
    <div class="card" style="width:420px;max-width:100%;max-height:85vh;display:flex;flex-direction:column">
      <div style="font-weight:600;margin-bottom:4px">🔗 Приєднати «${esc(name)}» до автора</div>
      <div class="muted" style="font-size:12px;margin-bottom:10px">Обери, хто це в архіві. Нік запишеться як інша назва автора,
        і архіватор шукатиме нові роботи й під ним — у ту саму теку, копії відсіюються.</div>
      <input type="text" id="attach-search" placeholder="Пошук автора…" style="margin-bottom:8px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 10px;color:var(--text)">
      <div id="attach-list" style="overflow:auto;flex:1;min-height:120px"></div>
      <div style="display:flex;justify-content:flex-end;margin-top:10px">
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('attach-modal').remove()">Скасувати</button>
      </div>
    </div>`;
  document.body.appendChild(m);
  const render = () => {
    const q = document.getElementById('attach-search').value.trim().toLowerCase();
    const rows = artists.filter(a => !q || a.artist_name.toLowerCase().includes(q)
                                     || (a.aliases || []).some(x => x.toLowerCase().includes(q))).slice(0, 200);
    document.getElementById('attach-list').innerHTML = rows.map(a => `
      <div class="artist-opt" data-attach-id="${a.id}" data-attach-name="${esc(a.artist_name)}">
        ${a.is_fav ? '♥ ' : ''}${esc(a.artist_name)} <span class="muted" style="font-size:11px">${a.total_files}${(a.aliases || []).length ? ' · також ' + esc(a.aliases.join(', ')) : ''}</span>
      </div>`).join('') || '<div class="muted" style="padding:8px">Нікого не знайдено</div>';
  };
  document.getElementById('attach-search').addEventListener('input', render);
  document.getElementById('attach-list').addEventListener('click', e => {
    const opt = e.target.closest('[data-attach-id]');
    if (opt) _attachConfirm(name, +opt.dataset.attachId, opt.dataset.attachName);
  });
  render();
  document.getElementById('attach-search').focus();
}

async function _attachConfirm(name, artistId, artistName) {
  const r = await fetch(`${API}/api/artists/${artistId}/aliases`, { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || 'Не вдалося', 'err'); return; }
  document.getElementById('attach-modal')?.remove();
  toast(`«${name}» — тепер інша назва автора «${artistName}»`, 'ok');
  if (confirm(`Завантажити зараз нове для «${artistName}» (разом з роботами під «${name}»)?\n\nУже наявне не качається, копії з іншого сайту відсіюються.`)) {
    _liveRunArtist(artistName);
  } else if (typeof liveSearch === 'function' && document.getElementById('live-grid')) {
    liveSearch(_livePage || 1);       // оновити позначки ✓
  }
}

async function _liveRunArtist(artist) {
  try {
    const r = await fetch(`${API}/api/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ artist })
    });
    if (r.ok) {
      toast(t('toast.archiverstarted'), 'ok');
      showPage('runner');
      document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page==='runner'));
    } else {
      const d = await r.json();
      toast(d.error || t('toast.starterr'), 'err');
    }
  } catch {
    toast(t('toast.starterr'), 'err');
  }
}


let _catalogPath  = '';
let _catalogItems = [];
let _catalogView  = localStorage.getItem('catalog_view') || 'grid';
const _catalogViewBtn = document.getElementById('catalog-view-toggle');
if (_catalogViewBtn) _catalogViewBtn.textContent = t(_catalogView === 'grid' ? 'view.grid' : 'view.list');
const CATALOG_IMAGE_EXT = ['png','jpg','jpeg','gif','webp','bmp','svg','ico'];
const CATALOG_VIDEO_EXT = ['webm','mp4','mov','mkv','avi','m4v','ogv',
                           'flv','wmv','mpg','mpeg','ts','m2ts','mts','3gp','asf','divx'];
const CATALOG_AUDIO_EXT = ['mp3','wav','ogg','flac','m4a','aac'];

let _catalogSource = 'local';

function catalogSourceChange() {
  _catalogSource = document.getElementById('catalog-source-select').value;
  catalogLoad('');
}

async function catalogPopulateSourceOptions() {
  const sel = document.getElementById('catalog-source-select');
  if (!sel) return;
  const currentVal = sel.value;
  try {
    const archives = await fetch(`${API}/api/external-archives`).then(r => r.json());
    Array.from(sel.querySelectorAll('option[data-archive]')).forEach(o => o.remove());
    if (Array.isArray(archives)) {
      archives.forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.id; opt.dataset.archive = '1'; opt.textContent = `🔗 ${a.label}`;
        sel.appendChild(opt);
      });
    }
    if (currentVal) sel.value = currentVal;
  } catch {}
}

function _catalogListUrl(path) {
  return _catalogSource === 'local'
    ? `${API}/api/catalog/list?path=${encodeURIComponent(path)}`
    : `${API}/api/external-archives/${_catalogSource}/catalog/list?path=${encodeURIComponent(path)}`;
}

function _catalogFileUrl(path) {
  return _catalogSource === 'local'
    ? `${API}/api/catalog/file?path=${encodeURIComponent(path)}`
    : `${API}/api/external-archives/${_catalogSource}/catalog/file?path=${encodeURIComponent(path)}`;
}

function _catalogThumbUrl(path) {
  return _catalogSource === 'local'
    ? `${API}/api/catalog/thumb?path=${encodeURIComponent(path)}`
    : `${API}/api/external-archives/${_catalogSource}/catalog/thumb?path=${encodeURIComponent(path)}`;
}

// Мініатюра не вийшла — або сервер старий і ендпоінта не знає, або кадр
// не дістався. Для картинки відкочуємось на сам файл (як було раніше),
// для відео показуємо іконку.
function catalogThumbFailed(img) {
  const full = img.dataset.full;
  if (full && img.src !== full) { img.src = full; img.removeAttribute('data-full'); return; }
  // Кадру немає — лишаємо саму іконку, без ▶ поверх неї: позначка
  // «це відео» на іконці 🎬 нічого не додає, тільки заважає.
  img.parentElement?.querySelector('.catalog-play')?.remove();
  img.outerHTML = `<div class="catalog-icon">${img.dataset.icon || '📄'}</div>`;
}

async function catalogLoad(path = '') {
  if ((path || '') !== _catalogPath) _catalogSelected.clear();
  _catalogPath = path || '';
  const grid = document.getElementById('catalog-grid');
  grid.innerHTML = `<div style="opacity:.6">${t('catalog.loading')}</div>`;
  try {
    if (!_catalogPath) await loadFavArtists();     // у корені — улюблені автори першими
    const d = await fetch(_catalogListUrl(_catalogPath)).then(r=>r.json());
    if (d.error) {
      grid.innerHTML = `<div style="opacity:.6">${esc(d.error)}</div>`;
      renderCatalogBreadcrumb();
      return;
    }
    renderCatalogBreadcrumb();
    _catalogItems = [
      ...d.folders.map(f => ({...f, type:'folder'})),
      ...d.files.map(f => ({...f, type:'file'})),
    ];
    // вибране, якого вже немає в теці (перемістили/видалили), — прибираємо
    const names = new Set(_catalogItems.filter(i => i.type === 'file').map(i => i.name));
    [..._catalogSelected].forEach(n => { if (!names.has(n)) _catalogSelected.delete(n); });
    catalogRender();
  } catch (e) {
    grid.innerHTML = `<div style="opacity:.6">${t('catalog.error')}</div>`;
  }
  catalogUpdateActionBar();
}

function catalogSortedFiltered() {
  const q = (document.getElementById('catalog-search')?.value || '').trim().toLowerCase();
  const sortVal = document.getElementById('catalog-sort')?.value || 'name-asc';
  let items = _catalogItems;
  if (q) items = items.filter(i => i.name.toLowerCase().includes(q));

  const isFav = i => !_catalogPath && _favArtistFolders.has(i.name);
  const folders = items.filter(i => i.type === 'folder')
    .sort((a,b) => (isFav(b) - isFav(a)) || (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1));
  const files = items.filter(i => i.type === 'file');

  const [field, dir] = sortVal.split('-');
  files.sort((a, b) => {
    let av, bv;
    if (field === 'date')      { av = a.mtime || 0; bv = b.mtime || 0; }
    else if (field === 'size') { av = a.size  || 0; bv = b.size  || 0; }
    else                       { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
    if (av < bv) return dir === 'asc' ? -1 : 1;
    if (av > bv) return dir === 'asc' ? 1 : -1;
    return 0;
  });
  return [...folders, ...files];
}

let _catalogRenderItems = [];  // items з поточного рендера, для click-хендлера по індексу

let _catalogExcluded = [];

async function catalogLoadExcluded() {
  try {
    _catalogExcluded = await fetch(`${API}/api/catalog/excluded-folders`).then(r => r.json());
    if (!Array.isArray(_catalogExcluded)) _catalogExcluded = [];
  } catch { _catalogExcluded = []; }
}

async function catalogToggleExclude(folderName, exclude) {
  try {
    const r = await fetch(`${API}/api/catalog/toggle-exclude`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ folder: folderName, exclude })
    }).then(x => x.json());
    if (r.error) { toast(r.error, 'err'); return; }
    _catalogExcluded = r.excluded || [];
    toast(exclude ? `"${folderName}" виключено з Арти/синхронізації` : `"${folderName}" знову бере участь у синхронізації`, 'ok');
    catalogRender();
  } catch {
    toast('Помилка мережі', 'err');
  }
}

function catalogRender() {
  const grid  = document.getElementById('catalog-grid');
  const items = catalogSortedFiltered();
  _catalogRenderItems = items;
  grid.className = _catalogView === 'list' ? 'catalog-list-view' : 'catalog-grid-view';

  if (!items.length) {
    grid.innerHTML = `<div style="opacity:.6">${t('catalog.empty')}</div>`;
    return;
  }

  const isRoot = !_catalogPath;

  grid.innerHTML = items.map((item, idx) => {
    const metaHtml = (_catalogView === 'list' && item.type === 'file')
      ? `<div class="catalog-meta">${item.size!=null ? fmtSize(item.size) : ''}${item.mtime ? ' · '+fmtDate(new Date(item.mtime*1000).toISOString()) : ''}</div>`
      : '';

    if (item.type === 'folder') {
      const isExcluded = isRoot && _catalogExcluded.includes(item.name);
      const excludeBtn = (isRoot && _catalogSource === 'local' && isAdminRole())
        ? `<span class="catalog-exclude-toggle" data-folder="${esc(item.name)}" data-excluded="${isExcluded?1:0}"
             title="${isExcluded ? 'Не сканується в Арти — клік, щоб знову включити' : 'Виключити з Арти/синхронізації (напр. особисті файли)'}"
             style="position:absolute;top:6px;right:6px;font-size:14px;cursor:pointer;opacity:${isExcluded?1:0.35}">${isExcluded ? '🚫' : '👁'}</span>`
        : '';
      const accessBtn = (isRoot && _catalogSource === 'local' && _currentUser?.is_admin)
        ? `<span class="catalog-access-toggle" data-folder="${esc(item.name)}"
             title="Кому видно цю папку"
             style="position:absolute;top:6px;left:6px;font-size:14px;cursor:pointer;opacity:.5">🔑</span>`
        : '';
      const folderPath = _catalogPath ? `${_catalogPath}/${item.name}` : item.name;
      const delBtn = _catalogCanEdit()
        ? `<span class="catalog-folder-del" title="Видалити теку (файли — у кошик)">🗑</span>` : '';
      // тека автора в корені — ♥ (улюблені завжди першими й виділені)
      const favOn = isRoot && _favArtistFolders.has(item.name);
      const heartBtn = (isRoot && item.name.startsWith('@'))
        ? favArtistHeart(favOn, `data-fav-folder="${esc(item.name)}" style="position:absolute;bottom:6px;left:8px"`) : '';
      return `<div class="catalog-item${favOn ? ' catalog-fav' : ''}" data-idx="${idx}" data-drop-folder="${esc(folderPath)}" style="position:relative;${isExcluded?'opacity:.55':''}">
        ${excludeBtn}
        ${accessBtn}
        ${delBtn}
        ${heartBtn}
        <div class="catalog-icon">📁</div>
        <div class="catalog-name">${esc(item.name)}${isExcluded ? ' <span style="font-size:10px;color:var(--red)">(виключено)</span>' : ''}</div>
      </div>`;
    }

    const itemPath = _catalogPath ? `${_catalogPath}/${item.name}` : item.name;
    const ext      = (item.ext || '').toLowerCase();
    const fileUrl  = _catalogFileUrl(itemPath);
    const isImage  = CATALOG_IMAGE_EXT.includes(ext);
    const isVideo  = CATALOG_VIDEO_EXT.includes(ext);
    const isAudio  = CATALOG_AUDIO_EXT.includes(ext);
    const icon = isVideo ? '🎬' : isAudio ? '🎵' : ext === 'pdf' ? '📕' : ['txt','md'].includes(ext) ? '📝' : '📄';
    // Прев'ю тепер і для відео: сервер віддає кадр. Для картинок це теж
    // мініатюра, а не повний файл — тека з сотнями PNG більше не тягне
    // сотні мегабайт заради сітки.
    const thumbUrl = _catalogThumbUrl(itemPath);
    const thumb = (isImage || isVideo)
      ? `<img class="catalog-thumb" src="${thumbUrl}" loading="lazy" draggable="false"
              data-icon="${icon}" ${isImage ? `data-full="${fileUrl}"` : ''}
              onerror="catalogThumbFailed(this)">`
      : `<div class="catalog-icon">${icon}</div>`;
    const playBadge = isVideo
      ? `<span class="catalog-play">▶</span>`
      : '';
    const canEdit = _catalogCanEdit();
    const selected = _catalogSelected.has(item.name);
    const check = canEdit
      ? `<span class="catalog-check${selected ? ' on' : ''}" title="Обрати">${selected ? '✓' : ''}</span>`
      : '';
    const favMark = item.id
      ? `<span class="catalog-fav-toggle${item.is_favorite ? ' on' : ''}" data-fav-file="${item.id}" title="${t('gal.favorite')}">${item.is_favorite ? '★' : '☆'}</span>`
      : '';
    return `<div class="catalog-item${selected ? ' selected' : ''}" data-idx="${idx}" ${canEdit ? 'draggable="true"' : ''} style="position:relative">
      ${check}${favMark}${thumb}${playBadge}
      <div class="catalog-name">${esc(item.name)}</div>
      ${metaHtml}
    </div>`;
  }).join('');
}

document.getElementById('catalog-grid')?.addEventListener('click', e => {
  const star = e.target.closest('[data-fav-file]');
  if (star) {
    e.stopPropagation();
    catalogOverlayFav(parseInt(star.dataset.favFile));
    return;
  }
  const heart = e.target.closest('[data-fav-folder]');
  if (heart) {
    e.stopPropagation();
    toggleFavArtist({ folder: heart.dataset.favFolder }, !heart.classList.contains('on'));
    return;
  }
  const excludeToggle = e.target.closest('.catalog-exclude-toggle');
  if (excludeToggle) {
    e.stopPropagation();
    const isExcluded = excludeToggle.dataset.excluded === '1';
    catalogToggleExclude(excludeToggle.dataset.folder, !isExcluded);
    return;
  }
  const accessToggle = e.target.closest('.catalog-access-toggle');
  if (accessToggle) {
    e.stopPropagation();
    showCatalogFolderAccessModal(accessToggle.dataset.folder);
    return;
  }
  const folderDel = e.target.closest('.catalog-folder-del');
  if (folderDel) {
    e.stopPropagation();
    const fbox = folderDel.closest('.catalog-item');
    if (fbox?.dataset.dropFolder) catalogDeleteFolder(fbox.dataset.dropFolder);
    return;
  }
  const box = e.target.closest('.catalog-item');
  if (!box) return;
  const idx  = parseInt(box.dataset.idx, 10);
  const item = _catalogRenderItems[idx];
  if (!item) return;
  // Вибір: галочка, або Ctrl/⌘-клік по файлу
  if (item.type === 'file' && _catalogCanEdit() &&
      (e.target.closest('.catalog-check') || e.ctrlKey || e.metaKey)) {
    e.stopPropagation();
    catalogToggleSelect(item.name);
    return;
  }
  if (item.type === 'folder') {
    const newPath = _catalogPath ? `${_catalogPath}/${item.name}` : item.name;
    catalogLoad(newPath);
  } else {
    const ext      = (item.ext || '').toLowerCase();
    const itemPath = _catalogPath ? `${_catalogPath}/${item.name}` : item.name;
    const fileUrl  = _catalogFileUrl(itemPath);
    if (CATALOG_IMAGE_EXT.includes(ext) || CATALOG_VIDEO_EXT.includes(ext) || CATALOG_AUDIO_EXT.includes(ext)) {
      catalogOpenFile(fileUrl, ext);
    } else {
      // Текст, PDF, mp3-плейлисти чи що завгодно інше — не намагаємось
      // силоміць впхнути в оверлей, а віддаємо браузеру нативно
      window.open(fileUrl, '_blank');
    }
  }
});

function renderCatalogBreadcrumb() {
  const el = document.getElementById('catalog-breadcrumb');
  el.innerHTML = '';
  const parts = _catalogPath ? _catalogPath.split('/') : [];

  const rootCrumb = document.createElement('span');
  rootCrumb.className = 'catalog-crumb';
  rootCrumb.textContent = t('catalog.root');
  rootCrumb.dataset.dropFolder = '';   // «крихти» приймають файли — так їх можна підняти вище
  rootCrumb.onclick = () => catalogLoad('');
  el.appendChild(rootCrumb);

  let acc = '';
  parts.forEach(p => {
    acc = acc ? `${acc}/${p}` : p;
    el.appendChild(document.createTextNode(' / '));
    const c = document.createElement('span');
    c.className = 'catalog-crumb';
    c.textContent = p;
    const pathCopy = acc;
    c.dataset.dropFolder = acc;
    c.onclick = () => catalogLoad(pathCopy);
    el.appendChild(c);
  });
}

function catalogGoUp() {
  if (!_catalogPath) return;
  const parts = _catalogPath.split('/');
  parts.pop();
  catalogLoad(parts.join('/'));
}

function catalogToggleView() {
  _catalogView = _catalogView === 'grid' ? 'list' : 'grid';
  localStorage.setItem('catalog_view', _catalogView);
  document.getElementById('catalog-view-toggle').textContent = t(_catalogView === 'grid' ? 'view.grid' : 'view.list');
  catalogRender();
}

function catalogOpenFile(url, clickedExt) {
  // Та сама навігація стрілочками, що й в "Арти" — але тепер тільки з
  // файлів, які реально можна показати в оверлеї (картинки/відео/аудіо).
  // Текст/PDF/інше відкриваються окремо, у новій вкладці — тому в цю
  // навігацію не потрапляють, інакше зайняли б позицію з "поламаним" mime.
  const files = _catalogRenderItems.filter(it => {
    if (it.type !== 'file') return false;
    const e = (it.ext || '').toLowerCase();
    return CATALOG_IMAGE_EXT.includes(e) || CATALOG_VIDEO_EXT.includes(e) || CATALOG_AUDIO_EXT.includes(e);
  });
  _overlayBoxes = files.map(it => {
    const p    = _catalogPath ? `${_catalogPath}/${it.name}` : it.name;
    const ext  = (it.ext || '').toLowerCase();
    const fUrl = _catalogFileUrl(p);
    const mime = CATALOG_VIDEO_EXT.includes(ext) ? `video/${ext}`
               : CATALOG_AUDIO_EXT.includes(ext) ? `audio/${ext}`
               : `image/${ext}`;
    return { dataset: { src: fUrl, mime }, isCatalog: true,
             catalogPath: p, catalogName: it.name, fileId: it.id || null };
  });
  _overlayBoxIdx = _overlayBoxes.findIndex(b => b.dataset.src === url);
  if (_overlayBoxIdx < 0) _overlayBoxIdx = 0;

  _showOverlayBox(_overlayBoxes[_overlayBoxIdx] || { dataset: { src: url, mime: `${CATALOG_VIDEO_EXT.includes(clickedExt)?'video':CATALOG_AUDIO_EXT.includes(clickedExt)?'audio':'image'}/${clickedExt}` }, isCatalog: true });
}

// ── Каталог: вибір, дії з файлами, drag&drop ─────────────────────────────
//
// Усе це — лише для свого сховища й лише адміну. Кожна дія йде на сервер,
// а він міняє і сховище, і базу: видалення — у кошик, переміщення — з новим
// шляхом файлу в БД, тож «Арти» й архіватор далі бачать файл на новому місці.

const _catalogSelected = new Set();   // імена файлів у поточній теці

function _catalogCanEdit() {
  return _catalogSource === 'local' && canModerate();
}

function _catalogJoin(name) {
  return _catalogPath ? `${_catalogPath}/${name}` : name;
}

function catalogToggleSelect(name) {
  if (_catalogSelected.has(name)) _catalogSelected.delete(name);
  else _catalogSelected.add(name);
  catalogRender();
  catalogUpdateActionBar();
}

function catalogSelectAll() {
  const visible = (_catalogRenderItems || []).filter(i => i.type === 'file').map(i => i.name);
  const allOn = visible.length && visible.every(n => _catalogSelected.has(n));
  if (allOn) visible.forEach(n => _catalogSelected.delete(n));
  else visible.forEach(n => _catalogSelected.add(n));
  catalogRender();
  catalogUpdateActionBar();
}

function catalogClearSelection() {
  _catalogSelected.clear();
  catalogRender();
  catalogUpdateActionBar();
}

function catalogUpdateActionBar() {
  const bar = document.getElementById('catalog-actions');
  if (!bar) return;
  const canEdit = _catalogCanEdit();
  bar.style.display = canEdit ? 'flex' : 'none';
  if (!canEdit) return;
  const n = _catalogSelected.size;
  document.getElementById('catalog-sel-count').textContent =
    n ? `Обрано: ${n}` : 'Перетягни файли з комп\'ютера сюди, щоб завантажити в цю теку';
  bar.querySelectorAll('[data-needs-selection]').forEach(b => b.style.display = n ? '' : 'none');
}

function _catalogShowResult(d, verb) {
  const done = (d.done || []).length;
  const errs = d.errors || [];
  if (done) toast(`${verb}: ${done}`, errs.length ? 'err' : 'ok');
  if (errs.length) {
    const msg = errs.slice(0, 5).map(e => `• ${e.path.split('/').pop()}: ${e.error}`).join('\n');
    alert(`Не вдалося (${errs.length}):\n${msg}${errs.length > 5 ? '\n…' : ''}`);
  }
}

async function catalogDeletePaths(paths) {
  if (!paths.length) return false;
  const what = paths.length === 1 ? `«${paths[0].split('/').pop()}»` : `${paths.length} файл(ів)`;
  if (!confirm(`Перемістити ${what} у кошик?\nЗвідти можна відновити разом з тегами й автором.`)) return false;
  try {
    const d = await fetch(`${API}/api/catalog/delete`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths })
    }).then(r => r.json());
    if (d.error) { toast(d.error, 'err'); return false; }
    _catalogShowResult(d, 'У кошику');
    await catalogLoad(_catalogPath);
    return (d.done || []).length > 0;
  } catch {
    toast('Помилка мережі', 'err');
    return false;
  }
}

async function catalogMovePaths(paths, dest) {
  if (!paths.length) return false;
  dest = (dest || '').replace(/\\/g, '/').split('/').map(s => s.trim()).filter(Boolean).join('/');
  try {
    const d = await fetch(`${API}/api/catalog/move`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths, dest })
    }).then(r => r.json());
    if (d.error) { toast(d.error, 'err'); return false; }
    _catalogShowResult(d, `Переміщено в «${dest || 'корінь'}»`);
    await catalogLoad(_catalogPath);
    return (d.done || []).length > 0;
  } catch {
    toast('Помилка мережі', 'err');
    return false;
  }
}

async function catalogCreateFolder() {
  const name = (prompt(`Нова тека в «${_catalogPath || 'корені'}»\n(можна одразу вкладену: Документи/2026)`) || '').trim();
  if (!name) return;
  try {
    const r = await fetch(`${API}/api/catalog/mkdir`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: _catalogJoin(name) })
    });
    const d = await r.json();
    if (!r.ok) { toast(d.error || t('toast.error'), 'err'); return; }
    toast(`Теку «${name}» створено`, 'ok');
    await catalogLoad(_catalogPath);
  } catch { toast('Помилка мережі', 'err'); }
}

async function catalogDeleteFolder(path) {
  const call = (body) => fetch(`${API}/api/catalog/delete-folder`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(async r => ({ ok: r.ok, d: await r.json().catch(() => ({})) }));
  try {
    // спершу питаємо, що зачепить: сервер рахує файли з підтеками й каже,
    // якщо теку видаляти не можна (напр. тека автора)
    const pre = await call({ path, dry_run: true });
    if (!pre.ok) { alert(pre.d.error || t('toast.error')); return; }
    const name = path.split('/').pop();
    const msg = pre.d.files
      ? `Видалити теку «${name}»?\n\n${pre.d.files} файл(ів) усередині (з підтеками) підуть у кошик — звідти їх можна відновити. Сама тека зникне.`
      : `Видалити порожню теку «${name}»?`;
    if (!confirm(msg)) return;
    const res = await call({ path });
    if (!res.ok && res.d.error) { alert(res.d.error); return; }
    const errs = res.d.errors || [];
    if (errs.length) {
      alert(`У кошику: ${res.d.trashed || 0}. Не вдалося (${errs.length}):\n` +
            errs.slice(0, 5).map(e => `• ${e.path.split('/').pop()}: ${e.error}`).join('\n') +
            (errs.length > 5 ? '\n…' : '') + '\n\nТеку з рештою файлів залишено.');
    } else {
      toast(`Теку «${name}» видалено${res.d.trashed ? `, файлів у кошику: ${res.d.trashed}` : ''}`, 'ok');
    }
    // видалили теку, в якій стояли (чи її батька) — піднімаємось вище
    if (_catalogPath === path || _catalogPath.startsWith(path + '/')) {
      await catalogLoad(path.split('/').slice(0, -1).join('/'));
    } else {
      await catalogLoad(_catalogPath);
    }
  } catch { toast('Помилка мережі', 'err'); }
}

function catalogDeleteSelected() {
  catalogDeletePaths([..._catalogSelected].map(_catalogJoin));
}

// ── Вікно «Перемістити»: маленький браузер тек ──────────────────────────
let _catalogMovePaths = [];
let _catalogMoveDest  = '';

function catalogMoveSelected() {
  catalogShowMoveModal([..._catalogSelected].map(_catalogJoin));
}

function catalogShowMoveModal(paths) {
  if (!paths.length) return;
  _catalogMovePaths = paths;
  document.getElementById('catalog-move-title').textContent =
    paths.length === 1 ? `Перемістити «${paths[0].split('/').pop()}»` : `Перемістити ${paths.length} файл(ів)`;
  document.getElementById('catalog-move-modal').style.display = 'flex';
  catalogMoveBrowse(_catalogPath);
}

async function catalogMoveBrowse(path) {
  _catalogMoveDest = path || '';
  document.getElementById('catalog-move-dest').value = _catalogMoveDest;
  const list = document.getElementById('catalog-move-list');
  list.innerHTML = `<div style="opacity:.6">${t('catalog.loading')}</div>`;
  try {
    const d = await fetch(`${API}/api/catalog/list?path=${encodeURIComponent(_catalogMoveDest)}`).then(r => r.json());
    if (d.error) { list.innerHTML = `<div style="opacity:.6">${esc(d.error)}</div>`; return; }
    const up = _catalogMoveDest
      ? `<div class="catalog-move-row" data-path="${esc(_catalogMoveDest.split('/').slice(0, -1).join('/'))}">⬆ ..</div>` : '';
    list.innerHTML = up + (d.folders.length
      ? d.folders.map(f => `<div class="catalog-move-row" data-path="${esc(_catalogMoveDest ? `${_catalogMoveDest}/${f.name}` : f.name)}">📁 ${esc(f.name)}</div>`).join('')
      : `<div style="opacity:.5;padding:6px">Підтек немає</div>`);
  } catch {
    list.innerHTML = `<div style="opacity:.6">${t('catalog.error')}</div>`;
  }
}

document.getElementById('catalog-move-list')?.addEventListener('click', e => {
  const row = e.target.closest('.catalog-move-row');
  if (row) catalogMoveBrowse(row.dataset.path);
});

async function catalogMoveConfirm() {
  const dest = document.getElementById('catalog-move-dest').value;
  const btn = document.getElementById('catalog-move-btn');
  btn.disabled = true;
  try {
    if (await catalogMovePaths(_catalogMovePaths, dest)) {
      document.getElementById('catalog-move-modal').style.display = 'none';
    }
  } finally {
    btn.disabled = false;
  }
}

// ── Drag&drop ────────────────────────────────────────────────────────────
//  * файли з комп'ютера → завантаження в поточну теку (або в теку-плитку,
//    на яку їх кинули);
//  * плитки файлів каталогу → на плитку теки чи «крихту» шляху = переміщення.
const CATALOG_DND_TYPE = 'application/x-archive-catalog';

function _dndHasOsFiles(e) {
  // перетягування плитки з картинкою браузер теж позначає як 'Files' —
  // тому «файли з комп'ютера» лише тоді, коли це не наша плитка
  const types = Array.from(e.dataTransfer?.types || []);
  return types.includes('Files') && !types.includes(CATALOG_DND_TYPE);
}
function _dndHasCatalog(e) {
  return Array.from(e.dataTransfer?.types || []).includes(CATALOG_DND_TYPE);
}
function _catalogDropFolderOf(el) {
  const t = el.closest('[data-drop-folder]');
  return t ? t.dataset.dropFolder : null;
}

(function setupCatalogDnd() {
  const page = document.getElementById('page-catalog');
  const grid = document.getElementById('catalog-grid');
  const hint = document.getElementById('catalog-drop-hint');
  if (!page || !grid) return;

  let depth = 0;
  const clearHi = () => document.querySelectorAll('.catalog-drop-target')
    .forEach(el => el.classList.remove('catalog-drop-target'));
  const hideHint = () => { depth = 0; if (hint) hint.style.display = 'none'; clearHi(); };

  grid.addEventListener('dragstart', e => {
    const box = e.target.closest('.catalog-item');
    if (!box || !_catalogCanEdit()) return;
    const item = _catalogRenderItems[parseInt(box.dataset.idx, 10)];
    if (!item || item.type !== 'file') { e.preventDefault(); return; }
    // тягнемо обрану плитку — разом з усіма обраними; інакше лише її
    const names = _catalogSelected.has(item.name) ? [..._catalogSelected] : [item.name];
    e.dataTransfer.setData(CATALOG_DND_TYPE, JSON.stringify(names.map(_catalogJoin)));
    e.dataTransfer.effectAllowed = 'move';
  });

  page.addEventListener('dragenter', e => {
    if (!_catalogCanEdit() || !_dndHasOsFiles(e)) return;
    depth++;
    if (hint) {
      hint.textContent = `⬆ Відпусти, щоб завантажити в «${_catalogPath || 'корінь'}»`;
      hint.style.display = 'flex';
    }
  });
  page.addEventListener('dragleave', e => {
    if (!_dndHasOsFiles(e)) return;
    if (--depth <= 0) hideHint();
  });

  page.addEventListener('dragover', e => {
    if (!_catalogCanEdit()) return;
    const osFiles = _dndHasOsFiles(e), internal = _dndHasCatalog(e);
    if (!osFiles && !internal) return;
    const folder = _catalogDropFolderOf(e.target);
    if (internal && folder === null) return;      // переміщення — лише на теку
    e.preventDefault();
    e.dataTransfer.dropEffect = internal ? 'move' : 'copy';
    clearHi();
    const t = e.target.closest('[data-drop-folder]');
    if (t) t.classList.add('catalog-drop-target');
    if (hint && osFiles) hint.textContent = `⬆ Відпусти, щоб завантажити в «${(folder ?? _catalogPath) || 'корінь'}»`;
  });

  page.addEventListener('drop', async e => {
    if (!_catalogCanEdit()) return;
    const folder = _catalogDropFolderOf(e.target);
    if (_dndHasCatalog(e)) {
      e.preventDefault(); hideHint();
      if (folder === null) return;
      let paths = [];
      try { paths = JSON.parse(e.dataTransfer.getData(CATALOG_DND_TYPE) || '[]'); } catch {}
      // у ту саму теку — нічого не робимо
      paths = paths.filter(p => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '') !== folder);
      if (paths.length) catalogMovePaths(paths, folder);
      return;
    }
    if (!_dndHasOsFiles(e)) return;
    e.preventDefault(); hideHint();
    const files = Array.from(e.dataTransfer.files || []).filter(f => f.size > 0 || f.type);
    if (!files.length) { toast('Теки перетягувати не можна — лише файли', 'err'); return; }
    catalogUploadFiles(files, folder ?? _catalogPath);
  });
})();

async function catalogUploadFiles(files, folder) {
  const status = document.getElementById('catalog-sel-count');
  const res = await uploadFilesSequential(files, { folder },
    (i, n, name) => { if (status && name) status.textContent = `Завантаження ${i + 1} / ${n}: ${name}`; });
  if (res.ok.length) toast(`Завантажено: ${res.ok.length} з ${files.length}`, res.failed.length ? 'err' : 'ok');
  if (res.failed.length) {
    const msg = res.failed.slice(0, 5).map(f => `• ${f.name}: ${f.error}`).join('\n');
    alert(`Не завантажено (${res.failed.length}):\n${msg}${res.failed.length > 5 ? '\n…' : ''}`);
  }
  await catalogLoad(_catalogPath);
}

// ── Інформація про файл у перегляді з каталогу ──────────────────────────
//  Те саме, що й в «Артах»: автор, ★, посилання на оригінал — якщо файл
//  є в архіві (має id). Файл поза архівом показує лише ім'я.
let _overlayInfoToken = 0;

async function catalogOverlayInfo(box) {
  const el = document.getElementById('overlay-info');
  if (!el) return;
  if (!box?.isCatalog) { el.style.display = 'none'; el.innerHTML = ''; return; }
  if (_catalogSource !== 'local') {       // чужий архів: лише ★ у моє улюблене
    const it = _catalogItems.find(i => i.id === box.fileId);
    el.style.display = 'block';
    el.innerHTML = `<div class="overlay-info-row">
      ${box.fileId ? `<span class="catalog-crumb" style="font-size:17px" id="ov-fav-${box.fileId}" title="${t('gal.favorite')}"
            onclick="catalogOverlayFav(${box.fileId})">${it?.is_favorite ? '★' : '☆'}</span>` : ''}
      <span class="overlay-info-name" title="${esc(box.catalogName)}">${esc(box.catalogName)}</span></div>`;
    return;
  }
  const token = ++_overlayInfoToken;
  const isAdmin = canModerate();
  const delBtn = isAdmin
    ? `<span class="catalog-crumb" style="color:var(--red)" title="${t('gal.delete')}" onclick="catalogOverlayDelete()">🗑</span>` : '';
  const moveBtn = isAdmin
    ? `<span class="catalog-crumb" title="Перемістити" onclick="catalogOverlayMove()">➜</span>` : '';
  const base = (inner) => `<div class="overlay-info-row">${inner}
      <span class="overlay-info-name" title="${esc(box.catalogName)}">${esc(box.catalogName)}</span>
      ${moveBtn}${delBtn}</div>`;
  el.style.display = 'block';
  el.innerHTML = base('');
  if (!box.fileId) return;
  try {
    const d = await fetch(`${API}/api/gallery/meta/${box.fileId}`).then(r => r.json());
    if (token !== _overlayInfoToken || d.error) return;
    el.innerHTML = base(`
      <span class="catalog-crumb" style="font-size:17px" id="ov-fav-${box.fileId}"
            title="${t('gal.favorite')}" onclick="catalogOverlayFav(${box.fileId})">${d.is_favorite ? '★' : '☆'}</span>
      ${d.artist ? `<span class="gallery-artist-name">${esc(d.artist)}</span>` : ''}
      ${d.source ? `<span class="badge badge-${esc(d.source)}">${esc(d.source)}</span>` : ''}
      ${d.post_url ? `<a href="${esc(d.post_url)}" target="_blank" rel="noopener" class="gallery-info-link">↗ ${t('gal.original')}</a>` : ''}`);
  } catch {}
}

async function catalogOverlayFav(id) {
  try {
    const r = _catalogSource === 'local'
      ? await fetch(`${API}/api/download/${id}/favorite`, {method:'POST'}).then(r => r.json())
      : await fetch(`${API}/api/external-archives/${_catalogSource}/favorite`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ download_id: id })
        }).then(r => r.json());
    if (r.error) { toast(r.error, 'err'); return; }
    const el = document.getElementById(`ov-fav-${id}`);
    if (el) el.textContent = r.is_favorite ? '★' : '☆';
    const item = _catalogItems.find(i => i.id === id);
    if (item) { item.is_favorite = !!r.is_favorite; catalogRender(); }
    toast(r.is_favorite ? '★ Додано в улюблене' : '☆ Прибрано з улюбленого', 'ok');
  } catch { toast(t('toast.error'), 'err'); }
}

async function catalogOverlayDelete() {
  const box = _overlayBoxes[_overlayBoxIdx];
  if (!box?.catalogPath) return;
  if (await catalogDeletePaths([box.catalogPath])) {
    _overlayBoxes.splice(_overlayBoxIdx, 1);
    if (!_overlayBoxes.length) { closeOverlay(); return; }
    if (_overlayBoxIdx >= _overlayBoxes.length) _overlayBoxIdx = 0;
    _showOverlayBox(_overlayBoxes[_overlayBoxIdx]);
  }
}

function catalogOverlayMove() {
  const box = _overlayBoxes[_overlayBoxIdx];
  if (!box?.catalogPath) return;
  closeOverlay();
  catalogShowMoveModal([box.catalogPath]);
}

// ── Helpers ───────────────────────────────────────────────────────────────
function fmt(n) { return n==null?'—':Number(n).toLocaleString('uk-UA'); }
function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('uk-UA',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
let _tt;
function toast(msg,type='ok') {
  const el=document.getElementById('toast'); el.textContent=msg; el.className='show '+type;
  clearTimeout(_tt); _tt=setTimeout(()=>{el.className=type;},3200);
}

// ── Пауза відео при прихованій вкладці ──────────────────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    const vid = document.querySelector('#gallery-wrap video');
    if (vid && !vid.paused) {
      vid._wasPlaying = true;
      vid.pause();
    }
  } else {
    const vid = document.querySelector('#gallery-wrap video');
    if (vid && vid._wasPlaying) {
      vid._wasPlaying = false;
      vid.play().catch(() => {});
    }
  }
});

// ── Auth & App Init ───────────────────────────────────────────────────────

let _currentUser = null;

// Ролі: user / moderator / admin. Модератор — завантаження, переміщення,
// видалення в кошик, теги, черга «На перевірку»; без остаточного видалення,
// масових дій і налаштувань.
function userRole() {
  if (!_currentUser) return 'user';
  return _currentUser.role || (_currentUser.is_admin ? 'admin' : 'user');
}
function canModerate() { return ['moderator', 'admin'].includes(userRole()); }
function isAdminRole() { return userRole() === 'admin'; }

// Перехоплюємо fetch глобально — якщо сесія злетіла (401) посеред роботи,
// повертаємо користувача на екран логіну замість показу битих даних.
const _origFetch = window.fetch;
window.fetch = async function(...args) {
  const res = await _origFetch.apply(this, args);
  if (res.status === 401 && !String(args[0]).includes('/api/auth/')) {
    showAuthScreen();
  }
  return res;
};

function authSwitchTab(tab) {
  document.getElementById('tab-login').classList.toggle('active', tab === 'login' || tab === 'forgot');
  document.getElementById('tab-register').classList.toggle('active', tab === 'register');
  document.getElementById('form-login').style.display    = tab === 'login'    ? 'block' : 'none';
  document.getElementById('form-register').style.display = tab === 'register' ? 'block' : 'none';
  document.getElementById('form-forgot').style.display   = tab === 'forgot'   ? 'block' : 'none';
  document.getElementById('form-reset').style.display    = tab === 'reset'    ? 'block' : 'none';
  document.querySelector('.auth-tabs').style.display     = tab === 'reset'    ? 'none'  : '';
  document.getElementById('auth-error').classList.remove('show');
}

// ── Відновлення пароля ───────────────────────────────────────────────
function authShowForgot() {
  authSwitchTab('forgot');
  const em = document.getElementById('login-email').value.trim();
  if (em) document.getElementById('forgot-email').value = em;
  document.getElementById('forgot-done').style.display = 'none';
  document.getElementById('btn-forgot').disabled = false;
}

async function authForgotSubmit() {
  const email = document.getElementById('forgot-email').value.trim();
  const btn = document.getElementById('btn-forgot');
  btn.disabled = true;
  try {
    const r = await fetch('/api/auth/forgot', { method: 'POST', headers: {'Content-Type': 'application/json'},
                                               body: JSON.stringify({ email }) });
    const d = await r.json();
    if (!r.ok) { authShowError(d.error || 'Помилка'); btn.disabled = false; return; }
    const done = document.getElementById('forgot-done');
    done.textContent = d.message;
    done.style.display = '';
  } catch { authShowError('Сервер недоступний'); btn.disabled = false; }
}

let _resetToken = null;
async function authOpenReset(token) {
  _resetToken = token;
  showAuthScreen();
  authSwitchTab('reset');
  const info = document.getElementById('reset-for');
  try {
    const d = await fetch(`/api/auth/reset/check?token=${encodeURIComponent(token)}`).then(r => r.json());
    if (!d.ok) {
      info.textContent = '';
      authShowError(d.error || 'Посилання недійсне');
      document.getElementById('btn-reset').disabled = true;
      return;
    }
    info.textContent = `Новий пароль для ${d.email}`;
  } catch { authShowError('Сервер недоступний'); }
}

async function authResetSubmit() {
  const p1 = document.getElementById('reset-password').value;
  const p2 = document.getElementById('reset-password2').value;
  if (p1 !== p2) { authShowError('Паролі не збігаються'); return; }
  try {
    const r = await fetch('/api/auth/reset', { method: 'POST', headers: {'Content-Type': 'application/json'},
                                              body: JSON.stringify({ token: _resetToken, password: p1 }) });
    const d = await r.json();
    if (!r.ok) { authShowError(d.error || 'Помилка'); return; }
    history.replaceState(null, '', location.pathname);
    _resetToken = null;
    authSwitchTab('login');
    toast('Пароль змінено — увійди з новим', 'ok');
  } catch { authShowError('Сервер недоступний'); }
}

// ── Свій пароль ──────────────────────────────────────────────────────
function openChangePassword() {
  let m = document.getElementById('chpwd-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'chpwd-modal';
    m.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;padding:16px';
    m.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px;width:360px;max-width:100%">
        <div style="font-weight:600;margin-bottom:14px">🔑 Змінити пароль</div>
        <div class="field"><label>Поточний пароль</label><input type="password" id="chpwd-old" autocomplete="current-password"></div>
        <div class="field"><label>Новий пароль</label><input type="password" id="chpwd-new" autocomplete="new-password" placeholder="Щонайменше 8 символів"></div>
        <div class="field"><label>Ще раз</label><input type="password" id="chpwd-new2" autocomplete="new-password"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:6px">
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('chpwd-modal').style.display='none'">Скасувати</button>
          <button class="btn btn-primary btn-sm" onclick="changePasswordSubmit()">Зберегти</button>
        </div>
      </div>`;
    document.body.appendChild(m);
  }
  ['chpwd-old', 'chpwd-new', 'chpwd-new2'].forEach(id => document.getElementById(id).value = '');
  m.style.display = 'flex';
  document.getElementById('chpwd-old').focus();
}

async function changePasswordSubmit() {
  const old_password = document.getElementById('chpwd-old').value;
  const new_password = document.getElementById('chpwd-new').value;
  if (new_password !== document.getElementById('chpwd-new2').value) { toast('Паролі не збігаються', 'err'); return; }
  const r = await fetch(`${API}/api/auth/change-password`, { method: 'POST', headers: {'Content-Type': 'application/json'},
                                                            body: JSON.stringify({ old_password, new_password }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || 'Помилка', 'err'); return; }
  document.getElementById('chpwd-modal').style.display = 'none';
  toast('Пароль змінено', 'ok');
}

// ── Адмін: новий пароль користувачу, запити на відновлення ───────────
function _genPassword() {
  const abc = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const a = new Uint32Array(12); crypto.getRandomValues(a);
  return Array.from(a, x => abc[x % abc.length]).join('');
}

function _askNewPassword(who) {
  const pwd = prompt(`Новий пароль для ${who}\n(запропоновано випадковий — можна змінити; щонайменше 8 символів):`, _genPassword());
  if (pwd === null) return null;
  if (pwd.length < 8) { toast('Пароль має містити щонайменше 8 символів', 'err'); return null; }
  return pwd;
}

function _passwordDone(who, pwd) {
  try { navigator.clipboard.writeText(pwd); } catch {}
  alert(`Пароль для ${who} змінено.\n\nНовий пароль: ${pwd}\n(скопійовано в буфер обміну)\n\nПередай його особисто й попроси змінити на свій (🔑 внизу меню).`);
}

async function adminSetPassword(userId, who) {
  const pwd = _askNewPassword(who);
  if (!pwd) return;
  const r = await fetch(`${API}/api/users/${userId}/password`, { method: 'POST', headers: {'Content-Type': 'application/json'},
                                                                body: JSON.stringify({ password: pwd }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || 'Помилка', 'err'); return; }
  _passwordDone(who, pwd);
  loadPasswordRequests();
}

async function loadPasswordRequests() {
  const box = document.getElementById('pwd-requests');
  if (!box) return;
  let rows = [];
  try { rows = await fetch(`${API}/api/password-requests`).then(r => r.ok ? r.json() : []); } catch {}
  if (!Array.isArray(rows) || !rows.length) { box.innerHTML = ''; return; }
  box.innerHTML = `
    <div class="card" style="margin-bottom:14px;border-color:var(--yellow, #d90)">
      <div style="font-weight:600;margin-bottom:8px">🔑 Запити на відновлення пароля (${rows.length})</div>
      ${rows.map(r => `
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;border-top:1px solid var(--border)">
          <div><b>${esc(r.display_name || '—')}</b> <span class="muted">${esc(r.email)}</span></div>
          <div class="muted" style="font-size:12px">${fmtDate(r.created_at)}</div>
          <div style="margin-left:auto;display:flex;gap:6px">
            <button class="btn btn-primary btn-sm" onclick="resolvePasswordRequest(${r.id}, 'set', '${esc(r.display_name || r.email)}')">Задати новий пароль</button>
            <button class="btn btn-ghost btn-sm" onclick="resolvePasswordRequest(${r.id}, 'reject')">Відхилити</button>
          </div>
        </div>`).join('')}
    </div>`;
}

async function resolvePasswordRequest(id, action, who) {
  const body = { action };
  if (action === 'set') {
    const pwd = _askNewPassword(who);
    if (!pwd) return;
    body.password = pwd;
  } else if (!confirm('Відхилити запит? Пароль лишиться старим.')) return;
  const r = await fetch(`${API}/api/password-requests/${id}`, { method: 'POST', headers: {'Content-Type': 'application/json'},
                                                               body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || 'Помилка', 'err'); loadPasswordRequests(); return; }
  if (action === 'set') _passwordDone(who, body.password); else toast('Запит відхилено', 'ok');
  loadPasswordRequests();
}

async function mailTest() {
  // спершу зберігаємо поля пошти, щоб тест ішов з тим, що зараз у формі
  await fetch(`${API}/api/config`, { method: 'POST', headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({ mail: _mailCollect() }) });
  const r = await fetch(`${API}/api/mail/test`, { method: 'POST' });
  const d = await r.json().catch(() => ({}));
  if (r.ok) toast(`Тестовий лист надіслано на ${d.to}`, 'ok'); else toast(d.error || 'Помилка', 'err');
}

function _mailCollect() {
  const g = id => (document.getElementById(id)?.value || '').trim();
  return { host: g('cfg-mail-host'), port: g('cfg-mail-port'), user: g('cfg-mail-user'),
           password: g('cfg-mail-pass') || null, from_addr: g('cfg-mail-from'),
           security: g('cfg-mail-security') || 'starttls', base_url: g('cfg-mail-base') };
}

function authShowError(msg) {
  const el = document.getElementById('auth-error');
  el.textContent = msg;
  el.classList.add('show');
}

async function authLogin() {
  const email = document.getElementById('login-email').value.trim();
  const pwd   = document.getElementById('login-password').value;
  if (!email || !pwd) { authShowError(t('err.emailpwd')); return; }

  const btn = document.getElementById('btn-login');
  btn.disabled = true;
  try {
    const r = await _origFetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password: pwd})
    });
    const d = await r.json();
    if (!r.ok) { authShowError(d.error || t('err.loginfailed')); return; }
    await onAuthSuccess(d);
  } catch (e) {
    authShowError(t('err.noconnect'));
  } finally {
    btn.disabled = false;
  }
}

async function authRegister() {
  const name  = document.getElementById('reg-name').value.trim();
  const email = document.getElementById('reg-email').value.trim();
  const pwd   = document.getElementById('reg-password').value;
  const code  = document.getElementById('reg-code').value.trim();
  if (!email || !pwd) { authShowError(t('err.emailpwd')); return; }
  if (pwd.length < 8) { authShowError(t('err.pwdshort')); return; }
  if (_anyUsersExist && !code) {
    authShowError(t('err.needcode')); return;
  }

  const btn = document.getElementById('btn-register');
  btn.disabled = true;
  try {
    const r = await _origFetch('/api/auth/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password: pwd, display_name: name, access_code: code})
    });
    const d = await r.json();
    if (!r.ok) { authShowError(d.error || t('err.regfailed')); return; }
    await onAuthSuccess(d);
  } catch (e) {
    authShowError(t('err.noconnect'));
  } finally {
    btn.disabled = false;
  }
}

async function authLogout() {
  await _origFetch('/api/auth/logout', {method: 'POST'});
  _currentUser = null;
  document.getElementById('login-password').value = '';
  showAuthScreen();
}

async function onAuthSuccess(data) {
  _currentUser = data;
  loadVersionBadge();
  if (!data.setup_done) {
    showSetupScreen();
  } else {
    await showAppShell();
    // Меню застосунків будується одразу після завантаження сторінки — якщо
    // в ту мить ще не було входу, сервер відповів 401 і пунктів застосунків
    // (Автори, Онлайн, На перевірку…) не було аж до перезавантаження.
    if (typeof _appsMenuNeedsRebuild !== 'undefined' && _appsMenuNeedsRebuild && typeof appsBuildMenu === 'function') appsBuildMenu();
  }
}

function showAuthScreen() {
  document.getElementById('db-setup-screen').style.display = 'none';
  document.getElementById('auth-screen').style.display  = 'flex';
  document.getElementById('setup-screen').style.display  = 'none';
  document.getElementById('app-shell').style.display     = 'none';
  document.getElementById('auth-error').classList.remove('show');
}

function showSetupScreen() {
  document.getElementById('db-setup-screen').style.display = 'none';
  document.getElementById('auth-screen').style.display  = 'none';
  document.getElementById('setup-screen').style.display  = 'flex';
  document.getElementById('app-shell').style.display     = 'none';
}

async function showAppShell() {
  document.getElementById('db-setup-screen').style.display = 'none';
  document.getElementById('auth-screen').style.display  = 'none';
  document.getElementById('setup-screen').style.display  = 'none';
  document.getElementById('app-shell').style.display     = 'flex';

  // Вхід пройшов — отже, база відповідає (сервер щойно читав з неї
  // користувача). Не чекаємо на важку статистику «Огляду», щоб це показати.
  const dbDot = document.getElementById('db-dot'), dbLabel = document.getElementById('db-label');
  if (dbDot && dbLabel && !dbDot.classList.contains('offline')) {
    dbDot.className = 'db-dot online'; dbLabel.textContent = 'MariaDB online';
  }

  const label = document.getElementById('current-user-label');
  if (label && _currentUser) {
    label.textContent = _currentUser.email || _currentUser.display_name || '';
    label.title = label.textContent;
  }

  // Вкладки: звичайний користувач бачить тільки "Арти", адмін — усе + "Коди допуску"
  const isAdmin = isAdminRole();
  document.body.dataset.role = userRole();           // CSS ховає data-admin / data-mod
  document.querySelectorAll('.nav-item[data-admin-only]').forEach(el => {
    el.style.display = isAdmin ? '' : 'none';
  });
  document.querySelectorAll('.nav-item[data-mod-only]').forEach(el => {
    el.style.display = canModerate() ? '' : 'none';
  });
  const uploadBtn = document.getElementById('gal-upload-btn');
  if (uploadBtn) uploadBtn.style.display = canModerate() ? '' : 'none';
  if (canModerate()) { tagSuggUpdateCount(); setInterval(tagSuggUpdateCount, 60000); }

  // Каталог тепер потрібен незалежно від типу сховища — там же живе
  // виключення папок зі сканування (особисті файли поруч з архівом),
  
  // Пікер тегів — зупиняємо bubbling щоб не закривався при кліку всередині
  document.getElementById('gal-tag-picker').addEventListener('mousedown', e => e.stopPropagation());
  document.getElementById('gal-artist-picker').addEventListener('mousedown', e => e.stopPropagation());

  connectSSE();

  if (isAdmin) {
    loadOverview();
    updateStatus();
    setInterval(updateStatus, 3000);
    setInterval(() => { if (currentPage === 'overview') loadOverview(); }, 20000);
  } else {
    // Звичайний користувач одразу потрапляє на "Арти"
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector('.nav-item[data-page="gallery"]').classList.add('active');
    showPage('gallery');
  }
  setInterval(chatUpdateUnreadBadge, 15000);
  chatUpdateUnreadBadge();
  galLoadArtists();
  galLoadAllTags();
}

// ── Setup wizard ──────────────────────────────────────────────────────────

function setupShowError(msg) {
  const el = document.getElementById('setup-error');
  el.textContent = msg;
  el.classList.add('show');
}

async function setupFinish() {
  const btn = document.getElementById('btn-setup-finish');
  document.getElementById('setup-error').classList.remove('show');

  const path = document.getElementById('setup-local-path').value.trim();
  if (!path) { setupShowError(t('err.needpath')); return; }

  btn.disabled = true;
  try {
    // Сховище ядра — локальна тека
    const cfgBody = { storage_mode: 'local', local: { path } };

    await _origFetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfgBody)
    });

    const r = await _origFetch('/api/auth/complete-setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    if (!r.ok) { setupShowError(t('err.setupfail')); return; }

    if (_currentUser) _currentUser.setup_done = true;
    await showAppShell();
  } catch (e) {
    setupShowError(t('err.setupsaveerr'));
  } finally {
    btn.disabled = false;
  }
}

// ── DB Setup ──────────────────────────────────────────────────────────────

function showDbSetupScreen() {
  document.getElementById('db-setup-screen').style.display = 'flex';
  document.getElementById('auth-screen').style.display     = 'none';
  document.getElementById('setup-screen').style.display    = 'none';
  document.getElementById('app-shell').style.display       = 'none';
}

function dbSetupShowError(msg) {
  const el = document.getElementById('db-setup-error');
  el.textContent = msg;
  el.style.display = 'block';
}

async function dbSetupSubmit() {
  const btn = document.getElementById('btn-dbsetup');
  const errEl = document.getElementById('db-setup-error');
  errEl.style.display = 'none';

  const host     = document.getElementById('dbsetup-host').value.trim();
  const port     = document.getElementById('dbsetup-port').value.trim() || '3306';
  const user     = document.getElementById('dbsetup-user').value.trim();
  const password = document.getElementById('dbsetup-password').value;
  const database = document.getElementById('dbsetup-database').value.trim();

  if (!host || !user || !database) {
    dbSetupShowError(t('err.dbfillfields'));
    return;
  }

  btn.disabled = true;
  btn.textContent = t('err.connecting');
  try {
    const r = await fetch('/api/db/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({host, port: parseInt(port), user, password, database})
    });
    const d = await r.json();
    if (!r.ok) { dbSetupShowError(d.error || t('err.dbconnectfail')); return; }

    // Успіх — переходимо до звичайного auth flow. Якщо сервер
    // перезапускається (щоб підхопити застосунки) — чекаємо, поки підніметься.
    if (d.restarting) {
      dbSetupShowError('База підключена. Архів перезапускається…');
      for (let i = 0; i < 30; i++) {
        await new Promise(res => setTimeout(res, 2500));
        try { if ((await fetch('/api/db/status')).ok) break; } catch {}
      }
      location.reload();
      return;
    }
    await startAuthFlow();
  } catch (e) {
    dbSetupShowError(t('err.dbserverconn'));
  } finally {
    btn.disabled = false;
    btn.textContent = t('auth.connect');
  }
}

// ── Entry point ───────────────────────────────────────────────────────────

async function startAuthFlow() {
  try {
    const r = await _origFetch('/api/auth/me');
    const d = await r.json();
    if (d.authenticated) {
      await onAuthSuccess(d);
    } else {
      _anyUsersExist = !!d.any_users_exist;
      const optHint = document.getElementById('reg-code-optional');
      const hint    = document.getElementById('first-user-hint');
      if (!_anyUsersExist) {
        authSwitchTab('register');
        optHint.style.display = 'inline';
        hint.style.display    = 'block';
      } else {
        optHint.style.display = 'none';
        hint.style.display    = 'none';
      }
      showAuthScreen();
    }
  } catch (e) {
    showAuthScreen();
  }
}

(async function initApp() {
  applyI18n();

  // Спочатку перевіряємо чи взагалі підключена БД —
  // без цього навіть реєстрація неможлива
  try {
    const r = await fetch('/api/db/status');
    const d = await r.json();
    if (!d.configured) {
      showDbSetupScreen();
      if (d.env_locked) {
        dbSetupShowError('БД має бути налаштована через змінні оточення контейнера (DB_HOST тощо) — зверніться до адміністратора сервера');
        document.getElementById('btn-dbsetup').disabled = true;
      }
      return; // не йдемо далі, поки БД не підключена
    }
  } catch (e) {
    // Якщо навіть /api/db/status не відповідає — сервер взагалі недоступний
    showDbSetupScreen();
    dbSetupShowError('Сервер недоступний. Перевірте що dashboard_server.py запущено.');
    return;
  }

  // посилання з листа «відновлення пароля»
  const resetToken = new URLSearchParams(location.search).get('reset');
  if (resetToken) { authOpenReset(resetToken); return; }

  await startAuthFlow();

  // Enter в полях логіну/реєстрації
  document.getElementById('login-password')?.addEventListener('keydown', e => { if (e.key === 'Enter') authLogin(); });
  document.getElementById('reg-password')?.addEventListener('keydown', e => { if (e.key === 'Enter') authRegister(); });
  document.getElementById('dbsetup-password')?.addEventListener('keydown', e => { if (e.key === 'Enter') dbSetupSubmit(); });
})();


// ── Пошук дублікатів по всьому архіву ─────────────────────────────────
// Ендпоінт дає застосунок дублікатів; якщо його немає — кнопки теж немає.
async function dupScanWholeArchive() {
  const btn = document.getElementById('btn-scan-all');
  if (!confirm('Шукати дублікати по ВСЬОМУ архіву? На великому архіві це займе кілька хвилин, пошук іде у фоні.')) return;
  try {
    const r = await fetch(`${API}/api/duplicate-scan/global`, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({threshold: 8})
    });
    if (r.status === 404) { alert('Застосунок пошуку дублікатів вимкнено'); return; }
    const j = await r.json();
    if (!j.ok) { alert(j.error || 'Не вдалося запустити'); return; }
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Шукаю…'; }
    dupPollScanJob(j.job_id, btn);
  } catch (e) { alert('Помилка: ' + e); }
}

// ── Масовий розбір знайдених пар ──────────────────────────────────────
// Спершу завжди пробний прохід: він нічого не чіпає, лише показує, що
// саме поїде в кошик. Видалення — тільки після окремого підтвердження.
async function dupAutoResolve() {
  const btn = document.getElementById('btn-auto-resolve');
  const ans = prompt(
    'До якої відстані розбирати?\n\n' +
    '0 — тільки точні копії (безпечно)\n' +
    '2 — та сама робота з іншого сайту\n' +
    '4 — майже те саме, вже з ризиком\n\n' +
    'Лишається файл кращої якості, другий — у кошик.',
    '0');
  if (ans === null) return;
  const maxDistance = parseInt(ans);
  if (isNaN(maxDistance) || maxDistance < 0 || maxDistance > 20) { alert('Потрібне число 0–20'); return; }

  const run = async (dryRun) => {
    const r = await fetch(`${API}/api/duplicate-scan/auto-resolve`, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({max_distance: maxDistance, dry_run: dryRun})
    });
    if (r.status === 404) { alert('Застосунок пошуку дублікатів вимкнено'); return null; }
    const j = await r.json();
    if (!j.ok) { alert(j.error || 'Не вдалося запустити'); return null; }
    return j.job_id;
  };

  try {
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Рахую…'; }
    const jobId = await run(true);
    if (!jobId) { if (btn) { btn.disabled = false; btn.textContent = '⚡ Розібрати оптом'; } return; }
    const res = await dupWaitJob(jobId);
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Розібрати оптом'; }
    if (!res) return;

    const ok = confirm(
      'Пробний прохід:\n\n' + (res.result || '') +
      '\n\nВиконати насправді? Файли підуть у КОШИК, звідки їх можна повернути.');
    if (!ok) return;

    if (btn) { btn.disabled = true; btn.textContent = '⏳ Розбираю…'; }
    const realId = await run(false);
    if (!realId) { if (btn) { btn.disabled = false; btn.textContent = '⚡ Розібрати оптом'; } return; }
    const done = await dupWaitJob(realId);
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Розібрати оптом'; }
    alert(done ? ('Готово:\n\n' + (done.result || '')) : 'Задача не завершилась');
    if (typeof pendingRefreshCurrentTab === 'function') pendingRefreshCurrentTab();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Розібрати оптом'; }
    alert('Помилка: ' + e);
  }
}

function dupWaitJob(jobId) {
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const jobs = await fetch(`${API}/api/jobs?limit=50`, {credentials: 'include'}).then(r => r.json());
        const job  = (jobs || []).find(j => j.id === jobId);
        if (job && job.status === 'done')   return resolve(job);
        if (job && job.status === 'failed') { alert('Не вдалось: ' + (job.error || '')); return resolve(null); }
        setTimeout(tick, 2000);
      } catch (e) { resolve(null); }
    };
    tick();
  });
}

async function dupPollScanJob(jobId, btn) {
  try {
    const jobs = await fetch(`${API}/api/jobs?limit=50`, {credentials: 'include'}).then(r => r.json());
    const job  = (jobs || []).find(j => j.id === jobId);
    if (job && (job.status === 'done' || job.status === 'failed')) {
      if (btn) { btn.disabled = false; btn.textContent = '🔎 Шукати по всьому архіву'; }
      alert(job.status === 'done'
        ? 'Пошук завершено: ' + (job.result || '')
        : 'Пошук не вдався: ' + (job.error || ''));
      if (typeof pendingRefreshCurrentTab === 'function') pendingRefreshCurrentTab();
      return;
    }
    if (btn && job && job.message) btn.textContent = '⏳ ' + job.message.slice(0, 40);
    setTimeout(() => dupPollScanJob(jobId, btn), 2000);
  } catch { setTimeout(() => dupPollScanJob(jobId, btn), 3000); }
}


// ── Точковий доступ до файлу для зовнішніх ключів ─────────────────────
// Тека може бути прихована від ключа, але окремий файл з неї можна
// відкрити. Показуємо тільки ті ключі, для яких тека справді прихована —
// для решти файл і так видно, перемикати нічого.
async function galKeyAccess(fileId) {
  let info;
  try {
    info = await fetch(`${API}/api/files/${fileId}/key-access`, {credentials:'include'}).then(r => r.json());
  } catch (e) { alert('Не вдалося отримати доступи: ' + e); return; }

  const keys = info.keys || [];
  if (!keys.length) { alert('Ключів доступу ще немає. Створи їх у «Коди допуску» → «Ключі мого архіву».'); return; }

  const relevant = keys.filter(k => k.folder_hidden);
  if (!relevant.length) {
    alert(`Тека «${info.folder}» не прихована ні для одного ключа — файл і так видно всім.`);
    return;
  }

  const old = document.getElementById('key-access-modal');
  if (old) old.remove();
  const box = document.createElement('div');
  box.id = 'key-access-modal';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9999';
  box.innerHTML = `
    <div class="card" style="min-width:340px;max-width:480px">
      <div style="font-weight:600;margin-bottom:4px">Доступ для зовнішніх ключів</div>
      <div class="muted" style="font-size:12px;margin-bottom:12px">
        Тека <b>${esc(info.folder)}</b> прихована для цих ключів.
        Можна відкрити саме цей файл — решта теки лишиться закритою.
      </div>
      ${relevant.map(k => `
        <label style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
          <input type="checkbox" ${k.exception ? 'checked' : ''}
                 onchange="galToggleKeyException(${k.key_id}, ${fileId}, this.checked)">
          <span>${esc(k.label || ('ключ #' + k.key_id))}</span>
          ${k.active ? '' : '<span class="muted" style="font-size:11px">(вимкнений)</span>'}
        </label>`).join('')}
      <div style="text-align:right;margin-top:12px">
        <button class="btn btn-sm" onclick="document.getElementById('key-access-modal').remove()">Готово</button>
      </div>
    </div>`;
  box.addEventListener('click', e => { if (e.target === box) box.remove(); });
  document.body.appendChild(box);
}

async function galToggleKeyException(keyId, fileId, open) {
  try {
    const r = await fetch(`${API}/api/external-access/keys/${keyId}/exceptions/${fileId}`,
                          {method: open ? 'POST' : 'DELETE', credentials: 'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (e) {
    alert('Не вдалося змінити доступ: ' + e);
  }
}


// ── Назва архіву ──────────────────────────────────────────────────────
// Береться з налаштувань (Конфіг → Назва архіву). За замовчуванням "Archive".
async function applyBranding() {
  try {
    const b = await fetch(`${API}/api/branding`).then(r => r.json());
    const title = (b.title || 'Archive').trim();
    document.title = title;
    document.querySelectorAll('[data-brand]').forEach(el => el.textContent = title.toUpperCase());
  } catch {}
}
document.addEventListener('DOMContentLoaded', applyBranding);

// Встановлення як застосунок: без service worker браузер не пропонує
// «Встановити» / «На головний екран». Сам він нічого не кешує (static/sw.js).
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(e => console.warn('service worker:', e));
  });
}


// ── Кошик ─────────────────────────────────────────────────────────────
// Видалені файли потрапляють сюди, а не зникають одразу. Звідси їх можна
// повернути на старе місце з тегами, автором і всім іншим — або прибрати
// назавжди.
function pickLang(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  return v[_lang] || v.uk || v.en || Object.values(v)[0] || '';
}

function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['Б','КБ','МБ','ГБ']; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}

// ── Кошик ─────────────────────────────────────────────────────────────
// Сітка з прев'ю: видно, що саме видаляєш. Клік по картинці відкриває
// файл повністю. Показуємо порціями — після «Розібрати оптом» у кошику
// можуть бути тисячі файлів.
const TRASH_PAGE = 60;
let _trashItems = [], _trashShown = 0, _trashOrphans = [];
let _trashEntries = [];      // що показується: файли й видалені теки (тека — один запис)

function _trashThumb(src, fullUrl, ext, missing) {
  const vid = /^(webm|mp4|mov|mkv|avi|m4v|ogv|flv|wmv|mpg|mpeg|ts|m2ts|mts|3gp|asf|divx)$/i.test(ext || '');
  if (missing) return `<div class="trash-thumb trash-thumb-empty" title="Файлу на диску вже немає">⚠<span>файлу немає</span></div>`;
  return `<a class="trash-thumb" href="${fullUrl}" target="_blank" rel="noopener" title="Відкрити повністю">
      <img src="${src}" loading="lazy" alt=""
           onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'trash-ext',textContent:'${String(ext||'файл').replace(/[^\wа-яіїєґ]/gi,'').toUpperCase()}'}))">
      ${vid ? '<span class="trash-play">▶</span>' : ''}
    </a>`;
}

function _trashCard(x) {
  return `
    <div class="trash-card${x.missing ? ' is-missing' : ''}" id="trash-card-${x.id}">
      ${_trashThumb(`${API}/api/trash/${x.id}/thumb`, `${API}/api/trash/${x.id}/file`, x.ext, x.missing)}
      <div class="trash-info">
        <div class="trash-name" title="${esc(x.name || '')}">${esc(x.name || '')}</div>
        <div class="trash-from" title="${esc(x.original_rel_path || '')}">${esc(x.original_rel_path || '')}</div>
        <div class="trash-meta">${fmtBytes(x.size_bytes)} · ${x.deleted_at ? new Date(x.deleted_at).toLocaleString('uk-UA') : '—'}</div>
      </div>
      <div class="trash-actions">
        ${x.missing ? '' : `<button class="btn btn-sm" onclick="trashRestore(${x.id})">↩ Відновити</button>`}
        <button class="btn btn-sm btn-danger" data-admin onclick="trashPurge(${x.id})">${x.missing ? 'Прибрати запис' : 'Видалити назавжди'}</button>
      </div>
    </div>`;
}

// Файли видаленої теки йдуть одним записом-текою на місці першого з них
function _trashBuildEntries() {
  const byBatch = new Map();
  _trashEntries = [];
  for (const x of _trashItems) {
    if (x.folder_batch) {
      let f = byBatch.get(x.folder_batch);
      if (!f) {
        f = { folder: true, batch: x.folder_batch, folder_rel: x.folder_rel || '', items: [], deleted_at: x.deleted_at };
        byBatch.set(x.folder_batch, f);
        _trashEntries.push(f);
      }
      f.items.push(x);
    } else {
      _trashEntries.push(x);
    }
  }
}

function _trashFolderInner(f) {
  const size = f.items.reduce((s, x) => s + (Number(x.size_bytes) || 0), 0);
  const name = f.folder_rel.split('/').pop() || f.folder_rel;
  const parent = f.folder_rel.includes('/') ? f.folder_rel.slice(0, f.folder_rel.lastIndexOf('/')) : '(корінь)';
  return `
      <div class="trash-thumb trash-folder-thumb" onclick="trashFolderToggle('${f.batch}')" title="Показати файли">
        <div style="font-size:46px;line-height:1">📁</div>
        <div class="muted" style="font-size:12px;margin-top:4px">${f.items.length} файл(ів)</div>
      </div>
      <div class="trash-info">
        <div class="trash-name" title="${esc(f.folder_rel)}">${esc(name)}</div>
        <div class="trash-from" title="${esc(f.folder_rel)}">${esc(parent)}</div>
        <div class="trash-meta">тека · ${fmtBytes(size)} · ${f.deleted_at ? new Date(f.deleted_at).toLocaleString('uk-UA') : '—'}</div>
      </div>
      <div class="trash-actions">
        <button class="btn btn-sm" onclick="trashFolderRestore('${f.batch}')">↩ Відновити теку</button>
        <button class="btn btn-sm btn-ghost" onclick="trashFolderToggle('${f.batch}')">Файли</button>
        <button class="btn btn-sm btn-danger" data-admin onclick="trashFolderPurge('${f.batch}')">Видалити назавжди</button>
      </div>`;
}

function _trashEntryHtml(e) {
  if (!e.folder) return _trashCard(e);
  return `<div class="trash-card trash-folder" id="trash-folder-${e.batch}">${_trashFolderInner(e)}</div>`;
}

function trashFolderToggle(batch) {
  const open = document.getElementById(`trash-folder-files-${batch}`);
  if (open) { open.remove(); return; }
  const f = _trashEntries.find(e => e.folder && e.batch === batch);
  const card = document.getElementById(`trash-folder-${batch}`);
  if (!f || !card) return;
  card.insertAdjacentHTML('afterend', `
    <div class="trash-folder-files" id="trash-folder-files-${batch}">
      <div class="muted" style="font-size:12px;margin-bottom:8px">📁 ${esc(f.folder_rel)} — файли окремо можна відновити чи видалити й поодинці</div>
      <div class="trash-grid">${f.items.map(_trashCard).join('')}</div>
    </div>`);
}

async function trashFolderRestore(batch) {
  const f = _trashEntries.find(e => e.folder && e.batch === batch);
  const r = await fetch(`${API}/api/trash/folder/${batch}/restore`, {method: 'POST', credentials: 'include'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { toast(j.error || 'Не вдалося відновити', 'err'); return; }
  if (j.errors && j.errors.length) {
    alert(`Теку «${f?.folder_rel || ''}» відновлено частково: ${j.restored} з ${j.restored + j.errors.length}.\n\n` +
          j.errors.slice(0, 5).join('\n'));
  } else {
    toast(`Теку відновлено (${j.restored} файл(ів))`, 'ok');
  }
  loadTrashPage();
}

async function trashFolderPurge(batch) {
  const f = _trashEntries.find(e => e.folder && e.batch === batch);
  if (!confirm(`Видалити назавжди теку «${f?.folder_rel || ''}» (${f?.items.length || 0} файл(ів))? Відновити її вже не вийде.`)) return;
  const r = await fetch(`${API}/api/trash/folder/${batch}`, {method: 'DELETE', credentials: 'include'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { toast(j.error || 'Не вдалося видалити', 'err'); return; }
  if (j.errors && j.errors.length) toast(`Видалено ${j.removed}, не вдалося: ${j.errors.length}`, 'err');
  else toast('Теку видалено назавжди', 'ok');
  loadTrashPage();
}

function _trashRenderMore() {
  const grid = document.getElementById('trash-grid');
  if (!grid) return;
  const next = _trashEntries.slice(_trashShown, _trashShown + TRASH_PAGE);
  grid.insertAdjacentHTML('beforeend', next.map(_trashEntryHtml).join(''));
  _trashShown += next.length;
  const more = document.getElementById('trash-more');
  if (more) {
    const left = _trashEntries.length - _trashShown;
    more.style.display = left > 0 ? '' : 'none';
    more.textContent = `Показати ще ${Math.min(left, TRASH_PAGE)} (залишилось ${left})`;
  }
}

async function loadTrashPage() {
  const box = document.getElementById('trash-list');
  if (!box) return;
  box.innerHTML = `<div class="muted">${t('common.loading')}</div>`;
  try {
    const [items, orphans] = await Promise.all([
      fetch(`${API}/api/trash`, {credentials:'include'}).then(r => r.json()),
      fetch(`${API}/api/trash/orphans`, {credentials:'include'}).then(r => r.ok ? r.json() : []).catch(() => []),
    ]);
    _trashItems = Array.isArray(items) ? items : [];
    _trashOrphans = Array.isArray(orphans) ? orphans : [];
  } catch { box.innerHTML = '<div class="muted">Не вдалося завантажити кошик</div>'; return; }

  const items = _trashItems;
  document.getElementById('btn-trash-empty').style.display = items.length ? '' : 'none';
  const total = items.reduce((s, x) => s + (Number(x.size_bytes) || 0), 0);
  const missing = items.filter(x => x.missing).length;
  document.getElementById('trash-summary').textContent =
    items.length ? `${items.length} файлів · ${fmtBytes(total)}${missing ? ` · без файлу на диску: ${missing}` : ''}` : '';

  let html = '';
  if (_trashOrphans.length) {
    const osize = _trashOrphans.reduce((s, x) => s + (Number(x.size_bytes) || 0), 0);
    const canBack = _trashOrphans.filter(o => o.restore_to).length;
    html += `
      <div class="trash-orphans">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <div style="font-weight:600">⚠ Загублені файли в .trash: ${_trashOrphans.length} · ${fmtBytes(osize)}</div>
          ${canBack ? `<button class="btn btn-sm" onclick="trashOrphansRestoreAll()">↩ Повернути на місце всі, що можна (${canBack})</button>` : ''}
        </div>
        <div class="muted" style="font-size:12px;margin:4px 0 10px">
          Лежать у теці кошика без запису — у списку нижче їх не видно, але місце вони займають.
          Здебільшого це файли, які «зникли» з архіву, коли скінчилось місце на диску:
          такі можна повернути на старе місце.
        </div>
        <div class="trash-grid">${_trashOrphans.map((o, i) => `
          <div class="trash-card">
            ${_trashThumb(`${API}/api/trash/orphans/thumb?name=${encodeURIComponent(o.name)}`,
                          `${API}/api/trash/orphans/file?name=${encodeURIComponent(o.name)}`, o.ext, false)}
            <div class="trash-info">
              <div class="trash-name" title="${esc(o.real_name)}">${esc(o.real_name)}</div>
              <div class="trash-from" title="${esc(o.restore_to || '')}">${o.restore_to ? '→ ' + esc(o.restore_to) : 'куди повертати — невідомо'}</div>
              <div class="trash-meta">${fmtBytes(o.size_bytes)}</div>
            </div>
            <div class="trash-actions">
              ${o.restore_to ? `<button class="btn btn-sm" onclick="trashOrphanRestore(_trashOrphans[${i}].name)">↩ На місце</button>` : ''}
              <button class="btn btn-sm btn-danger" onclick="trashOrphanPurge(_trashOrphans[${i}].name)">Видалити назавжди</button>
            </div>
          </div>`).join('')}
        </div>
      </div>`;
  }

  if (!items.length) {
    box.innerHTML = html + '<div class="muted">Кошик порожній</div>';
    return;
  }
  box.innerHTML = html + `
    <div class="trash-grid" id="trash-grid"></div>
    <div style="text-align:center;margin-top:12px">
      <button class="btn btn-sm" id="trash-more" onclick="_trashRenderMore()" style="display:none"></button>
    </div>`;
  _trashShown = 0;
  _trashBuildEntries();
  _trashRenderMore();
}

// Після дії прибираємо одну картку, а не перемальовуємо весь кошик:
// інакше на тисячах файлів сторінка стрибала б угору після кожного кліку.
function _trashDrop(id) {
  const it = _trashItems.find(x => x.id === id);
  _trashItems = _trashItems.filter(x => x.id !== id);
  document.getElementById(`trash-card-${id}`)?.remove();
  const f = it && it.folder_batch && _trashEntries.find(e => e.folder && e.batch === it.folder_batch);
  if (f) {
    // файл з видаленої теки — тека лишається, у ній на один менше
    f.items = f.items.filter(x => x.id !== id);
    const card = document.getElementById(`trash-folder-${f.batch}`);
    if (!f.items.length) {
      _trashEntries = _trashEntries.filter(e => e !== f);
      _trashShown = Math.max(0, _trashShown - 1);
      card?.remove();
      document.getElementById(`trash-folder-files-${f.batch}`)?.remove();
    } else if (card) {
      card.innerHTML = _trashFolderInner(f);
    }
  } else {
    _trashEntries = _trashEntries.filter(e => e.folder || e.id !== id);
    _trashShown = Math.max(0, _trashShown - 1);
  }
  const total = _trashItems.reduce((s, x) => s + (Number(x.size_bytes) || 0), 0);
  const sum = document.getElementById('trash-summary');
  if (sum) sum.textContent = _trashItems.length ? `${_trashItems.length} файлів · ${fmtBytes(total)}` : '';
  if (!_trashItems.length) loadTrashPage();
}

async function trashRestore(id) {
  const r = await fetch(`${API}/api/trash/${id}/restore`, {method:'POST', credentials:'include'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { toast(j.error || 'Не вдалося відновити', 'err'); return; }
  toast('Відновлено', 'ok');
  _trashDrop(id);
}

async function trashPurge(id) {
  const x = _trashItems.find(i => i.id === id);
  if (!confirm(`Видалити назавжди «${x?.name || 'файл'}»? Відновити його вже не вийде.`)) return;
  const r = await fetch(`${API}/api/trash/${id}`, {method:'DELETE', credentials:'include'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { toast(j.error || 'Не вдалося видалити', 'err'); return; }
  toast('Видалено назавжди', 'ok');
  _trashDrop(id);
}

async function trashEmpty() {
  if (!confirm(`Очистити кошик? Усі ${_trashItems.length} файлів буде видалено назавжди.`)) return;
  const r = await fetch(`${API}/api/trash/empty`, {method:'POST', credentials:'include'});
  const j = await r.json().catch(() => ({}));
  if (j.failed) toast(`Видалено: ${j.removed ?? 0}, не вдалося: ${j.failed} (див. лог)`, 'err');
  else toast(`Видалено назавжди: ${j.removed ?? 0}`, 'ok');
  loadTrashPage();
}

async function trashOrphanRestore(name, quiet) {
  const r = await fetch(`${API}/api/trash/orphans/restore?name=${encodeURIComponent(name)}`, {method:'POST', credentials:'include'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { if (!quiet) toast(j.error || 'Не вдалося повернути', 'err'); return false; }
  if (!quiet) { toast('Повернуто на місце', 'ok'); loadTrashPage(); }
  return true;
}

async function trashOrphansRestoreAll() {
  const list = _trashOrphans.filter(o => o.restore_to);
  if (!confirm(`Повернути ${list.length} файлів на їхні старі місця?`)) return;
  let ok = 0, bad = 0;
  for (const o of list) (await trashOrphanRestore(o.name, true)) ? ok++ : bad++;
  toast(`Повернуто: ${ok}${bad ? `, не вдалося: ${bad}` : ''}`, bad ? 'err' : 'ok');
  loadTrashPage();
}

async function trashOrphanPurge(name) {
  if (!confirm('Видалити цей файл з диска назавжди?')) return;
  const r = await fetch(`${API}/api/trash/orphans?name=${encodeURIComponent(name)}`, {method:'DELETE', credentials:'include'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { toast(j.error || 'Не вдалося видалити', 'err'); return; }
  toast('Видалено назавжди', 'ok');
  loadTrashPage();
}


// Чи встановлено застосунок furry. Від цього залежать поле «Автор» у
// формі завантаження й колонки автора/поста/джерела в таблицях: без
// застосунку вони порожні й тільки заважають.
async function detectFurry() {
  try {
    const r = await fetch(`${API}/api/artists?filter=all`, {credentials:'include'});
    const absent = (r.status === 404);
    document.body.classList.toggle('furry-absent', absent);
    const row = document.getElementById('upload-artist-row');
    if (row) row.style.display = absent ? 'none' : '';
  } catch {}
}
document.addEventListener('DOMContentLoaded', () => setTimeout(detectFurry, 800));

// ═══ Власні джерела: додати сайт без написання застосунку ═════════════
let _customKinds = {};

async function liveShowSourcesModal() {
  let data;
  try {
    data = await fetch(`${API}/api/live/custom-sources`, {credentials:'include'}).then(r => r.json());
  } catch { toast('Не вдалося отримати список', 'err'); return; }
  _customKinds = data.kinds || {};

  const old = document.getElementById('custom-src-modal');
  if (old) old.remove();
  const box = document.createElement('div');
  box.id = 'custom-src-modal';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:200;padding:20px';
  box.innerHTML = `
    <div class="card" style="width:min(680px,100%);max-height:88vh;overflow:auto">
      <div style="font-weight:600;margin-bottom:4px">Власні джерела</div>
      <div class="muted" style="font-size:12px;margin-bottom:14px">
        Сайт, для якого немає готового застосунку, можна підключити самому —
        якщо він віддає дані одним зі звичних способів. Пошук працюватиме як у решти
        джерел; улюблене для таких джерел зберігається локально в архіві.
      </div>

      <div id="custom-src-list" style="margin-bottom:16px">${(data.sources || []).length
        ? data.sources.map(s => `
            <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
              <div><b>${esc(s.label)}</b> <span class="muted" style="font-family:var(--mono);font-size:11px">${esc(s.id)} · ${esc(s.kind)}</span></div>
              <button class="btn btn-sm btn-danger" style="margin-left:auto" onclick="liveDeleteCustomSource('${esc(s.id)}')">Видалити</button>
            </div>`).join('')
        : '<div class="muted">Поки жодного</div>'}
      </div>

      <div style="font-weight:600;margin-bottom:8px">Додати</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <input id="cs-id" placeholder="ідентифікатор (латиницею, напр. e926)" class="input"
               style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-family:var(--mono)">
        <input id="cs-label" placeholder="назва для кнопки (напр. e926)" class="input"
               style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text)">
        <select id="cs-kind" onchange="liveRenderKindFields()"
                style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text)">
          ${Object.entries(_customKinds).map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join('')}
        </select>
        <div class="muted" id="cs-hint" style="font-size:11px"></div>
        <div id="cs-fields" style="display:flex;flex-direction:column;gap:8px"></div>
      </div>

      <input id="cs-probe-query" placeholder="приклад запиту для перевірки (тег або ім'я користувача)"
             style="margin-top:10px;width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-family:var(--mono);font-size:12px">
      <div id="cs-probe" style="font-size:12px;margin-top:10px"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button class="btn btn-sm" onclick="document.getElementById('custom-src-modal').remove()">Закрити</button>
        <button class="btn btn-sm" onclick="liveProbeCustomSource()">Перевірити</button>
        <button class="btn btn-primary btn-sm" onclick="liveSaveCustomSource()">Зберегти</button>
      </div>
    </div>`;
  box.addEventListener('click', e => { if (e.target === box) box.remove(); });
  document.body.appendChild(box);
  liveRenderKindFields();
}

function liveRenderKindFields() {
  const kind = document.getElementById('cs-kind').value;
  const k = _customKinds[kind] || {fields: [], hint: ''};
  document.getElementById('cs-hint').textContent = k.hint || '';
  document.getElementById('cs-fields').innerHTML = (k.fields || []).map(f => `
    <input data-cfg="${esc(f.key)}" type="${f.type === 'password' ? 'password' : 'text'}"
           placeholder="${esc(f.label)}${f.required ? ' *' : ''}${f.placeholder ? ' — ' + esc(f.placeholder) : ''}"
           style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-family:var(--mono);font-size:12px">`).join('');
}

function liveCollectCustomSource() {
  const config = {};
  document.querySelectorAll('#cs-fields input[data-cfg]').forEach(i => {
    if (i.value.trim()) config[i.dataset.cfg] = i.value.trim();
  });
  return {
    id:    (document.getElementById('cs-id').value || '').trim().toLowerCase(),
    label: (document.getElementById('cs-label').value || '').trim(),
    kind:  document.getElementById('cs-kind').value,
    config,
  };
}

async function liveProbeCustomSource() {
  const body = liveCollectCustomSource();
  const out = document.getElementById('cs-probe');
  out.textContent = 'Перевіряю…'; out.style.color = 'var(--muted)';
  try {
    const r = await fetch(`${API}/api/live/custom-sources/probe`, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...body, query: (document.getElementById('cs-probe-query')?.value || '').trim()})
    }).then(x => x.json());
    if (r.ok) {
      out.style.color = 'var(--green)';
      out.innerHTML = `✓ Працює, знайдено ${r.found} шт. ` +
        (r.sample || []).map(u => `<img src="${esc(u)}" style="height:40px;border-radius:4px;margin-left:6px;vertical-align:middle">`).join('');
    } else {
      out.style.color = 'var(--red)';
      out.textContent = '✗ ' + (r.error || 'не вдалося');
    }
  } catch (e) { out.style.color = 'var(--red)'; out.textContent = '✗ ' + e; }
}

async function liveSaveCustomSource() {
  const body = liveCollectCustomSource();
  const r = await fetch(`${API}/api/live/custom-sources`, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) { toast(j.error || 'Не вдалося зберегти', 'err'); return; }
  toast('Джерело додано', 'ok');
  document.getElementById('custom-src-modal').remove();
  liveLoadStatus();
}

async function liveDeleteCustomSource(id) {
  if (!confirm(`Видалити джерело «${id}»? Збережені з нього файли лишаться.`)) return;
  await fetch(`${API}/api/live/custom-sources/${encodeURIComponent(id)}`, {method:'DELETE', credentials:'include'});
  liveShowSourcesModal();
  liveLoadStatus();
}
