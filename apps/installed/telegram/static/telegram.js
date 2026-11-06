// Telegram — сторінка застосунку.
// Акаунт (свій у кожного користувача) і канали, привʼязані до авторів.

const TG_API = '/api/apps/telegram';
let _tgRoot = null;
let _tgArtists = [];
let _tgIsAdmin = false;

function _tgEsc(s) { return (typeof esc === 'function') ? esc(s) : String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function _tgToast(m, k) { if (typeof toast === 'function') toast(m, k); else alert(m); }

async function _tgFetch(path, opts = {}) {
  const r = await fetch(TG_API + path, {
    credentials: 'include',
    headers: opts.body ? {'Content-Type': 'application/json'} : undefined,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

const _tgInput = 'background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-family:var(--mono);font-size:13px;outline:none;width:100%;box-sizing:border-box';
const _tgLabel = 'font-size:11px;color:var(--muted);display:block;margin-bottom:4px';

async function telegramInit(root) {
  _tgRoot = root;
  root.innerHTML = `
    <div class="card" style="margin-bottom:16px;max-width:900px">
      <div style="font-weight:600;margin-bottom:10px">✈ Мій акаунт Telegram</div>
      <div id="tg-account"><div class="muted">Завантаження…</div></div>
    </div>
    <div class="card" style="max-width:900px">
      <div style="font-weight:600;margin-bottom:10px">Канали → автори</div>
      <div id="tg-add"></div>
      <div id="tg-channels" style="margin-top:16px"></div>
    </div>`;
  await Promise.all([tgLoadAccount(), tgLoadArtists()]);
  tgRenderAddForm();
  tgLoadChannels();
}

// ── Акаунт ───────────────────────────────────────────────────────────────
async function tgLoadAccount() {
  const el = document.getElementById('tg-account');
  let a;
  try { a = await _tgFetch('/account'); }
  catch (e) { el.innerHTML = `<div style="color:var(--red)">${_tgEsc(e.message)}</div>`; return; }
  if (a.connected) {
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <span style="color:var(--green)">● підключено</span>
        <span style="font-family:var(--mono)">${_tgEsc(a.tg_name || '')} ${a.phone ? `· ${_tgEsc(a.phone)}` : ''}</span>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" onclick="tgShowLoginForm(${a.api_id || 'null'}, '${_tgEsc(a.phone || '')}', true)">Увійти знову</button>
        <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="tgLogout()">Вийти</button>
      </div>
      <div class="muted" style="font-size:12px;margin-top:8px">Канали нижче читаються цим акаунтом — закриті теж, якщо ти в них учасник.</div>`;
    return;
  }
  tgShowLoginForm(a.api_id, a.phone || '');
}

function tgShowLoginForm(apiId, phone, canCancel) {
  document.getElementById('tg-account').innerHTML = `
    <div class="muted" style="font-size:12px;margin-bottom:12px;line-height:1.6">
      Архів читає канали <b>твоїм</b> акаунтом — так видно оригінали без стиснення, відео й закриті канали.
      Потрібні <b>api_id</b> і <b>api_hash</b>: зайди на <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a>
      → «API development tools» → створи застосунок (назва будь-яка). Потім Telegram надішле код у застосунок.
      Сесія зберігається на сервері зашифрованою; вийти можна будь-коли.
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
      <div><label style="${_tgLabel}">api_id</label><input id="tg-api-id" style="${_tgInput}" value="${apiId || ''}" inputmode="numeric"></div>
      <div><label style="${_tgLabel}">api_hash${apiId ? ' (порожньо = збережений)' : ''}</label><input id="tg-api-hash" type="password" style="${_tgInput}" autocomplete="off"></div>
      <div><label style="${_tgLabel}">Телефон</label><input id="tg-phone" style="${_tgInput}" value="${_tgEsc(phone)}" placeholder="+380…"></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
      <button class="btn btn-primary btn-sm" id="tg-send-btn" onclick="tgSendCode()">Надіслати код</button>
      ${canCancel ? '<button class="btn btn-ghost btn-sm" onclick="tgLoadAccount()">Скасувати</button>' : ''}
      <span id="tg-login-status" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <div id="tg-code-step" style="display:none;margin-top:12px;gap:8px;align-items:flex-end;flex-wrap:wrap">
      <div style="min-width:200px"><label style="${_tgLabel}" id="tg-code-label">Код з Telegram</label>
        <input id="tg-code" style="${_tgInput}" autocomplete="one-time-code"></div>
      <button class="btn btn-primary btn-sm" id="tg-verify-btn" onclick="tgVerify()">Увійти</button>
    </div>`;
}

async function tgSendCode() {
  const st = document.getElementById('tg-login-status');
  const btn = document.getElementById('tg-send-btn');
  btn.disabled = true; st.style.color = 'var(--muted)'; st.textContent = 'Надсилаю…';
  try {
    await _tgFetch('/account/start', { method: 'POST', body: {
      api_id: document.getElementById('tg-api-id').value,
      api_hash: document.getElementById('tg-api-hash').value,
      phone: document.getElementById('tg-phone').value,
    }});
    st.textContent = 'Код надіслано в Telegram (у чат «Telegram» на іншому пристрої).';
    const step = document.getElementById('tg-code-step');
    step.style.display = 'flex';
    document.getElementById('tg-code').focus();
  } catch (e) {
    st.style.color = 'var(--red)'; st.textContent = e.message;
  } finally { btn.disabled = false; }
}

async function tgVerify() {
  const st = document.getElementById('tg-login-status');
  const input = document.getElementById('tg-code');
  const isPwd = input.type === 'password';
  const btn = document.getElementById('tg-verify-btn');
  btn.disabled = true; st.style.color = 'var(--muted)'; st.textContent = 'Перевіряю…';
  try {
    const d = await _tgFetch('/account/verify', { method: 'POST',
      body: isPwd ? { password: input.value } : { code: input.value.trim() } });
    if (d.stage === 'password') {
      document.getElementById('tg-code-label').textContent = 'Хмарний пароль Telegram (2FA)';
      input.value = ''; input.type = 'password'; input.focus();
      st.textContent = 'На акаунті є хмарний пароль — введи його.';
      return;
    }
    _tgToast(`Telegram підключено: ${d.tg_name || ''}`, 'ok');
    await tgLoadAccount();
    tgRenderAddForm();
  } catch (e) {
    st.style.color = 'var(--red)'; st.textContent = e.message;
  } finally { btn.disabled = false; }
}

async function tgLogout() {
  if (!confirm('Вийти з акаунта Telegram? Сесію буде закрито й стерто. Канали лишаться, але не перевірятимуться, доки не ввійдеш знову.')) return;
  try { await _tgFetch('/account', { method: 'DELETE' }); _tgToast('Вийшов з Telegram', 'ok'); }
  catch (e) { _tgToast(e.message, 'err'); }
  await tgLoadAccount();
}

// ── Автори ──────────────────────────────────────────────────────────────
async function tgLoadArtists() {
  try { _tgArtists = await _tgFetch('/artists'); } catch { _tgArtists = []; }
}

function _tgArtistPicker(prefix, selectedId) {
  const opts = _tgArtists.map(a =>
    `<option value="${a.id}" ${a.id === selectedId ? 'selected' : ''}>${_tgEsc(a.artist_name)}</option>`).join('');
  return `
    <div style="display:flex;gap:14px;font-size:12px;margin-bottom:6px">
      <label><input type="radio" name="${prefix}-mode" value="existing" ${_tgArtists.length ? 'checked' : 'disabled'}
             onchange="tgArtistMode('${prefix}')"> наявний автор</label>
      <label><input type="radio" name="${prefix}-mode" value="new" ${_tgArtists.length ? '' : 'checked'}
             onchange="tgArtistMode('${prefix}')"> новий автор</label>
    </div>
    <select id="${prefix}-artist" style="${_tgInput};${_tgArtists.length ? '' : 'display:none'}">${opts}</select>
    <input id="${prefix}-artist-new" style="${_tgInput};${_tgArtists.length ? 'display:none' : ''}" placeholder="імʼя автора — буде тека @імʼя">`;
}

function tgArtistMode(prefix) {
  const mode = document.querySelector(`input[name="${prefix}-mode"]:checked`)?.value;
  document.getElementById(`${prefix}-artist`).style.display = mode === 'existing' ? '' : 'none';
  document.getElementById(`${prefix}-artist-new`).style.display = mode === 'new' ? '' : 'none';
}

function _tgArtistBody(prefix) {
  const mode = document.querySelector(`input[name="${prefix}-mode"]:checked`)?.value;
  return mode === 'existing'
    ? { artist_id: parseInt(document.getElementById(`${prefix}-artist`).value, 10) }
    : { artist_name: document.getElementById(`${prefix}-artist-new`).value.trim() };
}

// ── Канали ──────────────────────────────────────────────────────────────
function tgRenderAddForm() {
  document.getElementById('tg-add').innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;align-items:start">
      <div>
        <label style="${_tgLabel}">Канал</label>
        <input id="tg-ref" style="${_tgInput}" placeholder="@назва, t.me/назва або t.me/+запрошення">
        <div class="muted" style="font-size:11px;margin-top:4px">Закритий канал — посилання-запрошення; ти маєш бути в ньому.</div>
      </div>
      <div>
        <label style="${_tgLabel}">Автор в архіві</label>
        ${_tgArtistPicker('tg-add')}
      </div>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-top:10px;font-size:12px">
      <label title="Не шукати цього автора на e621 та Inkbunny — лише канал"><input type="checkbox" id="tg-only"> тільки Telegram</label>
      <label title="Та сама галочка, що у вкладці «Автори»: щоденна перевірка на нове"><input type="checkbox" id="tg-auto" checked> автооновлення щодня</label>
      <span style="flex:1"></span>
      <span id="tg-add-status" style="color:var(--muted)"></span>
      <button class="btn btn-primary btn-sm" id="tg-add-btn" onclick="tgAddChannel()">＋ Додати канал</button>
    </div>`;
}

async function tgAddChannel() {
  const st = document.getElementById('tg-add-status');
  const btn = document.getElementById('tg-add-btn');
  btn.disabled = true; st.style.color = 'var(--muted)'; st.textContent = 'Шукаю канал…';
  try {
    const d = await _tgFetch('/channels', { method: 'POST', body: {
      ref: document.getElementById('tg-ref').value,
      ..._tgArtistBody('tg-add'),
      only_telegram: document.getElementById('tg-only').checked,
      auto_update: document.getElementById('tg-auto').checked,
    }});
    st.textContent = '';
    _tgToast(`Канал «${d.channel.title}» → ${d.channel.artist_name}`, 'ok');
    document.getElementById('tg-ref').value = '';
    await tgLoadArtists();
    tgRenderAddForm();
    tgLoadChannels();
  } catch (e) {
    st.style.color = 'var(--red)'; st.textContent = e.message;
  } finally { btn.disabled = false; }
}

let _tgChannels = [];

async function tgLoadChannels() {
  const el = document.getElementById('tg-channels');
  let d;
  try { d = await _tgFetch('/channels'); }
  catch (e) { el.innerHTML = `<div style="color:var(--red)">${_tgEsc(e.message)}</div>`; return; }
  _tgChannels = d.items || [];
  _tgIsAdmin = !!d.is_admin;
  if (!_tgChannels.length) {
    el.innerHTML = '<div class="muted">Каналів ще немає.</div>';
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Канал</th><th>Автор</th><th>Файлів</th><th>Перевірено</th><th></th></tr></thead>
    <tbody>${_tgChannels.map(c => `
      <tr style="${c.enabled ? '' : 'opacity:.5'}">
        <td>
          <div style="font-weight:600">${_tgEsc(c.title || c.channel_ref)}</div>
          <div class="muted" style="font-size:11px;font-family:var(--mono)">
            ${c.username ? `<a href="https://t.me/${_tgEsc(c.username)}" target="_blank" rel="noopener">@${_tgEsc(c.username)}</a>` : '🔒 закритий'}
            ${c.only_telegram ? ' · тільки Telegram' : ''}${_tgIsAdmin && !c.mine ? ' · чужий акаунт' : ''}
          </div>
        </td>
        <td>${_tgEsc(c.artist_name)}${c.auto_update ? ' <span title="автооновлення щодня">⟳</span>' : ''}</td>
        <td style="font-family:var(--mono)">${c.files}</td>
        <td style="font-size:12px">
          ${c.last_checked_at ? new Date(c.last_checked_at).toLocaleString('uk-UA', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '<span class="muted">ще ні</span>'}
          ${c.last_error ? `<div style="color:var(--red);font-size:11px;max-width:260px">${_tgEsc(c.last_error)}</div>` : ''}
        </td>
        <td style="white-space:nowrap;text-align:right">
          ${c.mine ? `<button class="btn btn-ghost btn-sm" title="Переглянути канал у вкладці «Онлайн»" onclick="tgView(${c.id})">👁</button>` : ''}
          ${_tgIsAdmin ? `<button class="btn btn-ghost btn-sm" title="Скачати нове зараз" onclick="tgCheckNow(${c.id})">▶</button>` : ''}
          <button class="btn btn-ghost btn-sm" title="Налаштування" onclick="tgEdit(${c.id})">✎</button>
          <button class="btn btn-ghost btn-sm" style="color:var(--red)" title="Відвʼязати" onclick="tgDelete(${c.id})">🗑</button>
        </td>
      </tr>
      <tr id="tg-edit-${c.id}" style="display:none"><td colspan="5"></td></tr>`).join('')}
    </tbody></table></div>`;
}

function tgEdit(id) {
  const c = _tgChannels.find(x => x.id === id);
  const row = document.getElementById(`tg-edit-${id}`);
  if (!c || !row) return;
  if (row.style.display !== 'none') { row.style.display = 'none'; return; }
  const p = `tg-e${id}`;
  row.style.display = '';
  row.firstElementChild.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;padding:8px 0">
      <div><label style="${_tgLabel}">Автор</label>${_tgArtistPicker(p, c.artist_id)}</div>
      <div style="display:flex;flex-direction:column;gap:6px;font-size:12px">
        <label><input type="checkbox" id="${p}-only" ${c.only_telegram ? 'checked' : ''}> тільки Telegram (не шукати на e621/Inkbunny)</label>
        <label><input type="checkbox" id="${p}-auto" ${c.auto_update ? 'checked' : ''}> автооновлення автора щодня</label>
        <label><input type="checkbox" id="${p}-on" ${c.enabled ? 'checked' : ''}> канал увімкнено</label>
        <label title="Наступна перевірка перегляне канал від першого повідомлення; уже скачане пропуститься"><input type="checkbox" id="${p}-rescan"> переглянути канал з початку</label>
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;padding-bottom:8px">
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('tg-edit-${id}').style.display='none'">Скасувати</button>
      <button class="btn btn-primary btn-sm" onclick="tgSaveEdit(${id})">Зберегти</button>
    </div>`;
}

async function tgSaveEdit(id) {
  const c = _tgChannels.find(x => x.id === id);
  const p = `tg-e${id}`;
  const body = {
    only_telegram: document.getElementById(`${p}-only`).checked,
    auto_update: document.getElementById(`${p}-auto`).checked,
    enabled: document.getElementById(`${p}-on`).checked,
    rescan: document.getElementById(`${p}-rescan`).checked,
  };
  const a = _tgArtistBody(p);
  if ((a.artist_id && a.artist_id !== c.artist_id) || a.artist_name) Object.assign(body, a);
  try {
    await _tgFetch(`/channels/${id}`, { method: 'PATCH', body });
    _tgToast('Збережено', 'ok');
    await tgLoadArtists();
    tgRenderAddForm();
    tgLoadChannels();
  } catch (e) { _tgToast(e.message, 'err'); }
}

async function tgDelete(id) {
  const c = _tgChannels.find(x => x.id === id);
  if (!confirm(`Відвʼязати канал «${c?.title || ''}»?\nУже скачані файли лишаться в архіві в автора.`)) return;
  try { await _tgFetch(`/channels/${id}`, { method: 'DELETE' }); _tgToast('Канал відвʼязано', 'ok'); }
  catch (e) { _tgToast(e.message, 'err'); }
  tgLoadChannels();
}

// «Перевірити зараз» — той самий запуск архіватора для одного автора, що й
// у вкладці «Онлайн»/«Автори»: e621/Inkbunny (якщо не «тільки Telegram») + канали.
async function tgCheckNow(id) {
  const c = _tgChannels.find(x => x.id === id);
  if (!c) return;
  const artist = (c.folder_name || '').startsWith('@') ? c.folder_name.slice(1) : c.artist_name;
  try {
    const r = await fetch('/api/run', {
      method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ artist })
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'Не вдалося запустити');
    _tgToast(`Перевірка «${c.artist_name}» запущена — хід у вкладці «Запуск»`, 'ok');
  } catch (e) { _tgToast(e.message, 'err'); }
}

// Переглянути канал у «Онлайн» — Telegram там звичайне джерело, як e621.
async function tgView(id) {
  const c = _tgChannels.find(x => x.id === id);
  if (!c || typeof showPage !== 'function') return;
  _liveSource = 'telegram';
  showPage('live');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === 'live'));
  if (typeof liveLoadStatus === 'function') await liveLoadStatus();
  if (typeof liveSetSource === 'function') liveSetSource('telegram');
  liveSetField('channel', c.username ? `@${c.username}` : c.channel_ref);
  liveSearchFresh();
}
