// «Пошук джерел»: кнопка в картці файлу й вкладка «🔎 Джерела» в «На перевірку».

const SF_SITE = { e621: 'e621', inkbunny: 'Inkbunny', furaffinity: 'FurAffinity', twitter: 'Twitter', weasyl: 'Weasyl', bluesky: 'Bluesky', deviantart: 'DeviantArt' };

if (typeof registerContribution === 'function') {
  registerContribution('file.actions', {
    id: 'sf-find',
    icon: '🔎',
    title: 'Знайти джерело (оригінал і теги)',
    order: 20,
    onClick: (ctx) => sourcefindOne(parseInt(ctx.file_id)),
  });
}

async function sourcefindOne(fileId) {
  toast('🔎 Шукаю джерело…', 'ok');
  const r = await fetch(`${API}/api/apps/sourcefind/file/${fileId}/find`, { method: 'POST' })
    .then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
  if (r.error) { toast(r.error, 'err'); return; }
  if (r.status === 'applied') toast(`✓ ${SF_SITE[r.site] || r.site}: посилання й теги (+${r.tags_added})`, 'ok');
  else if (r.status === 'review') toast('Є схожі, але не впевнено — дивись «На перевірку → 🔎 Джерела»', 'ok');
  else if (r.status === 'none') toast('Нічого не знайдено', 'err');
  else toast('Не вдалося перевірити', 'err');
  if (r.status === 'applied' && typeof galleryShow === 'function' && typeof _galMeta !== 'undefined') {
    delete _galMeta[fileId];
    if (_galIds[_galIdx] === fileId) galleryShow(_galIdx);
  }
}

if (typeof registerPendingTab === 'function' && typeof canModerate === 'function') {
  registerPendingTab({ id: 'sources', label: '🔎 Джерела', load: sourcefindLoadReview });
}

async function sourcefindLoadReview(el) {
  el.innerHTML = '<div class="muted">Завантаження…</div>';
  let rows;
  try { rows = await fetch(`${API}/api/apps/sourcefind/review`).then(r => r.json()); }
  catch { el.innerHTML = '<div class="muted">Не вдалося завантажити</div>'; return; }
  if (rows.error) { el.innerHTML = `<div class="muted">${esc(rows.error)}</div>`; return; }
  if (!rows.length) { el.innerHTML = '<div class="muted" style="padding:20px 0;text-align:center">Сумнівних збігів немає</div>'; return; }
  el.innerHTML = rows.map(r => `
    <div class="card" style="display:flex;gap:14px;align-items:flex-start;margin-bottom:10px" id="sf-row-${r.file_id}">
      <img src="${API}/api/gallery/stream/${r.file_id}" loading="lazy" style="width:110px;height:110px;object-fit:cover;border-radius:8px;background:var(--surface2)">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;margin-bottom:2px" data-no-i18n>${esc(r.name)}</div>
        <div class="muted" style="font-size:11px;font-family:var(--mono);margin-bottom:8px" data-no-i18n>${esc(r.rel_path)}</div>
        ${(r.candidates || []).map((c, i) => `
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:5px">
            <span class="badge badge-${esc(c.site)}">${esc(SF_SITE[c.site] || c.site)}</span>
            <a href="${esc(c.url || '#')}" target="_blank" rel="noopener" data-no-i18n>${esc(c.url || '')}</a>
            <span class="muted" style="font-size:11px">${c.method === 'iqdb' ? `схожість ${Math.round(c.score)}%` : c.method === 'fuzzy' ? `відмінність ${c.score}` : c.method === 'fluffle' ? 'Fluffle: імовірно' : 'той самий файл'}</span>
            ${(c.artists || []).length ? `<span class="muted" style="font-size:11px" data-no-i18n>· ${esc(c.artists.join(', '))}</span>` : ''}
            <button class="btn btn-primary btn-sm" onclick="sourcefindReviewApply(${r.file_id}, ${i})">✓ Це воно</button>
          </div>`).join('')}
        <button class="btn btn-ghost btn-sm" style="margin-top:4px" onclick="sourcefindReviewReject(${r.file_id})">✕ Нічого з цього</button>
      </div>
    </div>`).join('');
}

async function sourcefindReviewApply(fileId, idx) {
  const r = await fetch(`${API}/api/apps/sourcefind/review/${fileId}/apply`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idx }) })
    .then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
  if (r.error) { toast(r.error, 'err'); return; }
  toast(`✓ Посилання й теги (+${r.tags_added})`, 'ok');
  document.getElementById(`sf-row-${fileId}`)?.remove();
}

async function sourcefindReviewReject(fileId) {
  await fetch(`${API}/api/apps/sourcefind/review/${fileId}/reject`, { method: 'POST' }).catch(() => {});
  document.getElementById(`sf-row-${fileId}`)?.remove();
}
