// Telegram — усе у вкладці «Онлайн», коли обрано джерело Telegram:
// рядок з каналами (клік — переглянути пости) і розгортуване керування —
// акаунт (свій у кожного користувача) та привʼязка каналів до авторів.
// Файл вантажиться при старті (manifest → scripts) і малює себе в зоні
// ядра 'live.panel' — окремого пункту меню немає.

const TG_API = '/api/apps/telegram';
let _tgRoot = null;
let _tgArtists = [];
let _tgIsAdmin = false;
let _tgCanRun = false;

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

let _tgConnected = false;

if (typeof registerContribution === 'function') {
  registerContribution('live.panel', {
    id: 'telegram',
    mount: (el, ctx) => { if (ctx.source === 'telegram') telegramInit(el); else el.innerHTML = ''; },
  });
}

async function telegramInit(root) {
  _tgRoot = root;
  root.innerHTML = `
    <div class="card" style="margin-top:12px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-weight:600">✈ Мої канали</span>
        <span id="tg-status-mini" class="muted" style="font-size:12px"></span>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" id="tg-manage-btn" onclick="tgToggleManage()">⚙ Акаунт і канали</button>
      </div>
      <div id="tg-chips" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px"></div>
      <div id="tg-manage" style="display:none;margin-top:14px;border-top:1px solid var(--border);padding-top:14px">
        <div style="font-weight:600;margin-bottom:10px">Мій акаунт Telegram</div>
        <div id="tg-account"><div class="muted">Завантаження…</div></div>
        <div style="font-weight:600;margin:18px 0 10px">Канали → автори</div>
        <div id="tg-add"></div>
        <div id="tg-channels" style="margin-top:16px"></div>
      </div>
    </div>`;
  await Promise.all([tgLoadAccount(), tgLoadArtists()]);
  tgRenderAddForm();
  await tgLoadChannels();
  // нема акаунта чи каналів — одразу показуємо керування
  if (!_tgConnected || !_tgChannels.length) tgToggleManage(true);
}

function tgToggleManage(force) {
  const box = document.getElementById('tg-manage');
  if (!box) return;
  const open = force === undefined ? box.style.display === 'none' : !!force;
  box.style.display = open ? '' : 'none';
  document.getElementById('tg-manage-btn').textContent = open ? '✕ Сховати керування' : '⚙ Акаунт і канали';
}

function tgRenderChips() {
  const el = document.getElementById('tg-chips');
  if (!el) return;
  const mine = _tgChannels.filter(c => c.mine && c.enabled);
  el.innerHTML = mine.length
    ? mine.map(c => `<button class="filter-btn" onclick="tgView(${c.id})"
          title="${_tgEsc(c.username ? '@' + c.username : 'закритий канал')} → ${_tgEsc(c.artist_name)}">
          ${c.username ? '' : '🔒 '}${_tgEsc(c.title || c.channel_ref)}</button>`).join('')
    : `<span class="muted" style="font-size:12px">Каналів ще немає — додай у «Акаунт і канали». Або впиши будь-який канал у поле пошуку вище.</span>`;
}

// ── Акаунт ───────────────────────────────────────────────────────────────
async function tgLoadAccount() {
  const el = document.getElementById('tg-account');
  let a;
  try { a = await _tgFetch('/account'); }
  catch (e) { el.innerHTML = `<div style="color:var(--red)">${_tgEsc(e.message)}</div>`; return; }
  _tgConnected = !!a.connected;
  const mini = document.getElementById('tg-status-mini');
  if (mini) mini.innerHTML = a.connected
    ? `<span style="color:var(--green)">●</span> ${_tgEsc(a.tg_name || '')}`
    : '<span style="color:var(--red)">●</span> акаунт не підключено';
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
      → «API development tools» → створи застосунок (назва будь-яка). Далі — <b>QR-код</b> (найнадійніше) або код,
      який Telegram надсилає в застосунок, у чат «Telegram».
      Сесія зберігається на сервері зашифрованою; вийти можна будь-коли.
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
      <div><label style="${_tgLabel}">api_id</label><input id="tg-api-id" style="${_tgInput}" value="${apiId || ''}" inputmode="numeric"></div>
      <div><label style="${_tgLabel}">api_hash${apiId ? ' (порожньо = збережений)' : ''}</label><input id="tg-api-hash" type="password" style="${_tgInput}" autocomplete="off"></div>
      <div><label style="${_tgLabel}">Телефон</label><input id="tg-phone" style="${_tgInput}" value="${_tgEsc(phone)}" placeholder="+380…"></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap">
      <button class="btn btn-primary btn-sm" id="tg-qr-btn" onclick="tgQrStart()"
              title="Надійніше за код: скануєш QR телефоном, де вже є Telegram">📷 Увійти за QR-кодом</button>
      <button class="btn btn-ghost btn-sm" id="tg-send-btn" onclick="tgSendCode()">Надіслати код</button>
      ${canCancel ? '<button class="btn btn-ghost btn-sm" onclick="tgLoadAccount()">Скасувати</button>' : ''}
      <span id="tg-login-status" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <div id="tg-qr-step" style="display:none;margin-top:12px;gap:16px;align-items:flex-start;flex-wrap:wrap">
      <img id="tg-qr-img" alt="QR" style="width:220px;height:220px;background:#fff;border-radius:8px;padding:6px;box-sizing:border-box">
      <div style="font-size:13px;line-height:1.7;max-width:420px">
        На телефоні, де ти вже в Telegram:<br>
        <b>Налаштування → Пристрої → Підключити пристрій</b><br>
        і наведи камеру на цей код. Код оновлюється сам кожні ~30 с.
        <div id="tg-qr-pass" style="display:none;margin-top:10px">
          <label style="${_tgLabel}">Хмарний пароль Telegram (2FA)</label>
          <div style="display:flex;gap:8px"><input id="tg-qr-pw" type="password" style="${_tgInput}">
          <button class="btn btn-primary btn-sm" onclick="tgQrPassword()">Увійти</button></div>
        </div>
      </div>
    </div>
    <div id="tg-code-step" style="display:none;margin-top:12px;gap:8px;align-items:flex-end;flex-wrap:wrap">
      <div style="min-width:200px"><label style="${_tgLabel}" id="tg-code-label">Код з Telegram</label>
        <input id="tg-code" style="${_tgInput}" autocomplete="one-time-code"></div>
      <button class="btn btn-primary btn-sm" id="tg-verify-btn" onclick="tgVerify()">Увійти</button>
    </div>`;
}

// ── Вхід за QR ─────────────────────────────────────────────────────────
let _tgQrPoll = null;

async function tgQrStart() {
  const st = document.getElementById('tg-login-status');
  st.style.color = 'var(--muted)'; st.textContent = 'Готую QR…';
  try {
    await _tgFetch('/account/qr/start', { method: 'POST', body: {
      api_id: document.getElementById('tg-api-id').value,
      api_hash: document.getElementById('tg-api-hash').value,
    }});
  } catch (e) { st.style.color = 'var(--red)'; st.textContent = e.message; return; }
  document.getElementById('tg-code-step').style.display = 'none';
  document.getElementById('tg-qr-step').style.display = 'flex';
  let lastUrl = null;
  if (_tgQrPoll) clearInterval(_tgQrPoll);
  _tgQrPoll = setInterval(async () => {
    const box = document.getElementById('tg-qr-step');
    if (!box) { clearInterval(_tgQrPoll); return; }
    let d;
    try { d = await _tgFetch('/account/qr/status'); } catch { return; }
    if (d.state === 'waiting' && d.url && d.url !== lastUrl) {
      lastUrl = d.url;
      document.getElementById('tg-qr-img').src = `${TG_API}/account/qr.png?t=${Date.now()}`;
      st.textContent = 'Скануй QR телефоном.';
    } else if (d.state === 'password') {
      document.getElementById('tg-qr-pass').style.display = '';
      st.textContent = 'QR прийнято — на акаунті є хмарний пароль, введи його.';
      clearInterval(_tgQrPoll);
    } else if (d.state === 'done') {
      clearInterval(_tgQrPoll);
      _tgToast(`Telegram підключено: ${d.tg_name || ''}`, 'ok');
      await tgLoadAccount(); tgRenderAddForm(); tgLoadChannels();
    } else if (['error', 'expired', 'cancelled'].includes(d.state)) {
      clearInterval(_tgQrPoll);
      box.style.display = 'none';
      st.style.color = 'var(--red)';
      st.textContent = d.state === 'expired' ? 'Час на сканування вийшов — натисни ще раз.' : (d.error || 'Не вийшло');
    }
  }, 2000);
}

async function tgQrPassword() {
  const st = document.getElementById('tg-login-status');
  try {
    const d = await _tgFetch('/account/qr/password', { method: 'POST',
      body: { password: document.getElementById('tg-qr-pw').value } });
    _tgToast(`Telegram підключено: ${d.tg_name || ''}`, 'ok');
    await tgLoadAccount(); tgRenderAddForm(); tgLoadChannels();
  } catch (e) { st.style.color = 'var(--red)'; st.textContent = e.message; }
}

async function tgSendCode() {
  const st = document.getElementById('tg-login-status');
  const btn = document.getElementById('tg-send-btn');
  btn.disabled = true; st.style.color = 'var(--muted)'; st.textContent = 'Надсилаю…';
  try {
    const d = await _tgFetch('/account/start', { method: 'POST', body: {
      api_id: document.getElementById('tg-api-id').value,
      api_hash: document.getElementById('tg-api-hash').value,
      phone: document.getElementById('tg-phone').value,
    }});
    tgShowCodeSent(d);
    document.getElementById('tg-qr-step').style.display = 'none';
    const step = document.getElementById('tg-code-step');
    step.style.display = 'flex';
    document.getElementById('tg-code').focus();
  } catch (e) {
    st.style.color = 'var(--red)'; st.textContent = e.message;
  } finally { btn.disabled = false; }
}

// Куди Telegram відправив код + кнопка «надіслати інакше» (SMS/дзвінок),
// якщо Telegram дозволяє, — з відліком, скільки чекати.
let _tgResendTimer = null;
function tgShowCodeSent(d) {
  const st = document.getElementById('tg-login-status');
  st.style.color = 'var(--muted)';
  st.innerHTML = `Код надіслано <b>${_tgEsc(d.where || 'у Telegram')}</b>.` +
    (d.next ? ` <span id="tg-resend-wrap"></span>` : '');
  if (_tgResendTimer) clearInterval(_tgResendTimer);
  if (!d.next) return;
  let left = parseInt(d.timeout, 10) || 0;
  const draw = () => {
    const w = document.getElementById('tg-resend-wrap');
    if (!w) { clearInterval(_tgResendTimer); return; }
    w.innerHTML = left > 0
      ? `Не прийшов? Через ${left} с можна попросити ${_tgEsc(d.next)}.`
      : `<a href="#" onclick="tgResend();return false">Не прийшов — надіслати ${_tgEsc(d.next)}</a>`;
  };
  draw();
  _tgResendTimer = setInterval(() => { left--; draw(); if (left <= 0) clearInterval(_tgResendTimer); }, 1000);
}

async function tgResend() {
  const st = document.getElementById('tg-login-status');
  try {
    tgShowCodeSent(await _tgFetch('/account/resend', { method: 'POST' }));
  } catch (e) {
    st.style.color = 'var(--red)'; st.textContent = e.message;
  }
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
  const sel = _tgArtists.find(a => a.id === selectedId);
  // Наявного автора можна і вписати (з підказками), і вибрати зі списку:
  // вписане імʼя фільтрує список, вибір у списку підставляє імʼя.
  return `
    <div style="display:flex;gap:14px;font-size:12px;margin-bottom:6px">
      <label><input type="radio" name="${prefix}-mode" value="existing" ${_tgArtists.length ? 'checked' : 'disabled'}
             onchange="tgArtistMode('${prefix}')"> наявний автор</label>
      <label><input type="radio" name="${prefix}-mode" value="new" ${_tgArtists.length ? '' : 'checked'}
             onchange="tgArtistMode('${prefix}')"> новий автор</label>
    </div>
    <div id="${prefix}-existing" style="${_tgArtists.length ? '' : 'display:none'}">
      <input id="${prefix}-artist-type" list="${prefix}-artist-list" style="${_tgInput};margin-bottom:6px"
             placeholder="впиши імʼя автора…" value="${_tgEsc(sel ? sel.artist_name : '')}" autocomplete="off"
             oninput="tgArtistTyped('${prefix}')">
      <datalist id="${prefix}-artist-list">${_tgArtists.map(a => `<option value="${_tgEsc(a.artist_name)}">`).join('')}</datalist>
      <select id="${prefix}-artist" size="5" style="${_tgInput}" onchange="tgArtistPicked('${prefix}')">${opts}</select>
    </div>
    <input id="${prefix}-artist-new" style="${_tgInput};${_tgArtists.length ? 'display:none' : ''}" placeholder="імʼя автора — буде тека @імʼя">`;
}

function tgArtistMode(prefix) {
  const mode = document.querySelector(`input[name="${prefix}-mode"]:checked`)?.value;
  document.getElementById(`${prefix}-existing`).style.display = mode === 'existing' ? '' : 'none';
  document.getElementById(`${prefix}-artist-new`).style.display = mode === 'new' ? '' : 'none';
}

// Вписане імʼя: фільтруємо список і, якщо збіг точний, одразу вибираємо.
function tgArtistTyped(prefix) {
  const q = document.getElementById(`${prefix}-artist-type`).value.trim().toLowerCase();
  const sel = document.getElementById(`${prefix}-artist`);
  let exact = null, firstVisible = null;
  for (const o of sel.options) {
    const name = o.textContent.toLowerCase();
    const show = !q || name.includes(q);
    o.hidden = !show;
    if (show && !firstVisible) firstVisible = o;
    if (name === q) exact = o;
  }
  if (exact) sel.value = exact.value;
  else if (firstVisible && q) sel.value = firstVisible.value;
}

function tgArtistPicked(prefix) {
  const sel = document.getElementById(`${prefix}-artist`);
  const opt = sel.options[sel.selectedIndex];
  if (opt) document.getElementById(`${prefix}-artist-type`).value = opt.textContent;
}

function _tgArtistBody(prefix) {
  const mode = document.querySelector(`input[name="${prefix}-mode"]:checked`)?.value;
  if (mode !== 'existing') return { artist_name: document.getElementById(`${prefix}-artist-new`).value.trim() };
  // вписане імʼя важливіше за підсвічений рядок списку: шукаємо точний збіг
  const typed = document.getElementById(`${prefix}-artist-type`).value.trim().toLowerCase();
  if (typed) {
    const a = _tgArtists.find(x => x.artist_name.toLowerCase() === typed ||
                                   (x.folder_name || '').toLowerCase() === '@' + typed.replace(/^@/, ''));
    if (!a) throw new Error(`Автора «${typed}» немає. Обери зі списку або перемкни на «новий автор».`);
    return { artist_id: a.id };
  }
  const id = parseInt(document.getElementById(`${prefix}-artist`).value, 10);
  if (!id) throw new Error('Обери автора');
  return { artist_id: id };
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
  _tgCanRun = !!d.can_run;
  tgRenderChips();
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
          ${_tgCanRun ? `<button class="btn btn-ghost btn-sm" title="Скачати нове зараз" onclick="tgCheckNow(${c.id})">▶</button>` : ''}
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
  try {
    const a = _tgArtistBody(p);
    if ((a.artist_id && a.artist_id !== c.artist_id) || a.artist_name) Object.assign(body, a);
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
// Переглянути пости каналу — ми вже у «Онлайн»: підставити канал у пошук.
function tgView(id) {
  const c = _tgChannels.find(x => x.id === id);
  if (!c) return;
  liveSetField('channel', c.username ? `@${c.username}` : c.channel_ref);
  liveSearchFresh();
  document.querySelectorAll('#tg-chips .filter-btn').forEach(b =>
    b.classList.toggle('active', b.getAttribute('onclick') === `tgView(${id})`));
}
