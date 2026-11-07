// ═══════════════════════════════════════════════════════════════════════
//  Застосунки: динамічне меню, сторінки застосунків і розділ керування.
//
//  Підключати ПІСЛЯ dashboard.js:
//      <script src="/static/apps.js?v=1"></script>
//
//  Пункти меню й сторінки приходять з /api/apps/ui — тобто з маніфестів.
//  Ядро нічого не вписує в HTML заздалегідь: вимкнув застосунок — пункт
//  зник сам.
// ═══════════════════════════════════════════════════════════════════════

let _appsUi      = { menu: [], pages: [] };
let _appsLoaded  = {};     // які js/css уже підвантажені (щоб не двічі)

// ── підвантаження файлів застосунку на льоту ──────────────────────────
function appLoadAsset(url, kind) {
  if (_appsLoaded[url]) return Promise.resolve();
  _appsLoaded[url] = true;
  return new Promise((resolve) => {
    let el;
    if (kind === 'css') {
      el = document.createElement('link');
      el.rel = 'stylesheet';
      el.href = url;
    } else {
      el = document.createElement('script');
      el.src = url;
    }
    // Якщо файл не завантажився — не блокуємо сторінку, просто йдемо далі:
    // зламаний застосунок не повинен вішати весь інтерфейс.
    el.onload  = () => resolve();
    el.onerror = () => { console.warn('Не вдалось завантажити', url); resolve(); };
    document.head.appendChild(el);
  });
}

// ── побудова меню з маніфестів ────────────────────────────────────────
var _appsMenuNeedsRebuild = false;   // перший запит був до входу (401)

async function appsBuildMenu() {
  try {
    const lang = (typeof _lang !== 'undefined' && _lang) ? _lang : 'uk';
    const resp = await fetch(`${API}/api/apps/ui?lang=${lang}`, { credentials: 'include' });
    if (resp.status === 401) {          // ще не увійшли — збудуємо після входу
      _appsMenuNeedsRebuild = true;
      return;
    }
    _appsMenuNeedsRebuild = false;
    _appsUi = await resp.json();
  } catch (e) {
    console.warn('Не вдалось отримати меню застосунків', e);
    return;
  }

  const nav = document.querySelector('nav');
  if (!nav) return;

  // прибираємо те, що додали минулого разу
  nav.querySelectorAll('.nav-item[data-app-id]').forEach(el => el.remove());
  document.querySelectorAll('.page[data-app-page]').forEach(el => el.remove());

  (_appsUi.menu || []).forEach(item => {
    // page_id — сторінка, розмітка якої вже є в dashboard.html (напр. live, chat).
    // Такі застосунки переїхали з ядра: бекенд у них свій, а сторінка поки
    // лишається у спільному фронтенді. Тоді пункт меню просто відкриває її.
    if (item.page_id) {
      const el = document.createElement('div');
      el.className = 'nav-item';
      el.dataset.page  = item.page_id;
      el.dataset.appId = item.app_id;
      el.innerHTML = `<span class="nav-icon">${item.icon || '🧩'}</span> <span>${item.label || item.id}</span>`;
      el.addEventListener('click', () => {
        showPage(item.page_id);
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        el.classList.add('active');
        if (typeof closeSidebar === 'function') closeSidebar();
      });
      nav.appendChild(el);
      return;
    }

    const pageName = `app-${item.app_id}-${item.page || item.id}`;

    const el = document.createElement('div');
    el.className = 'nav-item';
    el.dataset.page  = pageName;
    el.dataset.appId = item.app_id;
    el.innerHTML = `<span class="nav-icon">${item.icon || '🧩'}</span> <span>${item.label || item.id}</span>`;
    el.addEventListener('click', () => {
      showPage(pageName);
      document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
      el.classList.add('active');
      if (typeof closeSidebar === 'function') closeSidebar();
    });
    nav.appendChild(el);

    // контейнер сторінки — застосунок малює всередині нього
    const page = document.createElement('div');
    page.className = 'page';
    page.id = `page-${pageName}`;
    page.dataset.appPage = item.app_id;
    page.innerHTML = `<div class="app-root" id="approot-${pageName}"></div>`;
    // Сторінки ядра живуть у #content, а не прямо в <main>: якщо покласти
    // поруч, сторінка застосунку опиниться ПІД основною областю.
    (document.querySelector('.content') || document.getElementById('content')
     || document.querySelector('main'))?.appendChild(page);
  });

  // Кошик має бути найнижчим пунктом. Пункти ядра стоять у HTML, а
  // застосунки дописують свої ПІСЛЯ них — тому просто переносимо кошик
  // у кінець, коли всі пункти вже на місці.
  const trash = nav.querySelector('.nav-item[data-page="trash"]');
  if (trash) nav.appendChild(trash);

  appsLoadStartupScripts();
}

// ── відкриття сторінки застосунку ─────────────────────────────────────
// Викликається з showPage() (див. правку в dashboard.js нижче).
async function appsOpenPage(pageName) {
  const parts = pageName.replace(/^app-/, '').split('-');
  const menuItem = (_appsUi.menu || []).find(
    m => `app-${m.app_id}-${m.page || m.id}` === pageName);
  if (!menuItem) return false;

  const page = (_appsUi.pages || []).find(
    p => p.app_id === menuItem.app_id && p.id === (menuItem.page || menuItem.id));
  const root = document.getElementById(`approot-${pageName}`);

  if (!page) {
    if (root) root.innerHTML = '<div class="muted">Ця сторінка не має вмісту.</div>';
    return true;
  }

  if (page.css) await appLoadAsset(page.css, 'css');
  if (page.js)  await appLoadAsset(page.js, 'js');

  const fn = page.init && window[page.init];
  if (typeof fn === 'function') {
    try {
      fn(root);
    } catch (e) {
      console.error(e);
      if (root) root.innerHTML = `<div class="muted">Застосунок не зміг намалювати сторінку: ${e}</div>`;
    }
  } else if (root && !root.innerHTML.trim()) {
    root.innerHTML = '<div class="muted">Застосунок не надав обробника сторінки.</div>';
  }
  return true;
}

// ── розділ «Застосунки» ───────────────────────────────────────────────
const APP_STATUS_LABEL = {
  enabled:      ['увімкнено',    'var(--green)'],
  disabled:     ['вимкнено',     'var(--muted)'],
  error:        ['помилка',      'var(--red)'],
  incompatible: ['несумісний',   'var(--yellow, #d90)'],
};

async function loadAppsPage() {
  const box = document.getElementById('apps-list');
  if (!box) return;
  box.innerHTML = '<div class="muted">Завантаження…</div>';

  let apps = [];
  try {
    apps = await fetch(`${API}/api/apps`, { credentials: 'include' }).then(r => r.json());
  } catch (e) {
    box.innerHTML = '<div class="muted">Не вдалося отримати список застосунків.</div>';
    return;
  }
  if (!apps.length) {
    box.innerHTML = '<div class="muted">Жодного застосунку не встановлено. '
                  + 'Поклади теку застосунку в <code>apps/installed/</code> і перезапусти архів.</div>';
    return;
  }

  box.innerHTML = apps.map(a => {
    const [label, color] = APP_STATUS_LABEL[a.status] || [a.status, 'var(--muted)'];
    const deps = Object.keys(a.dependencies || {});
    const on   = a.status === 'enabled';
    return `
      <div class="card" style="margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <div style="font-weight:600">${escapeHtml(a.name)}</div>
          <div class="muted" style="font-family:var(--mono);font-size:11px">v${escapeHtml(a.version || '')}</div>
          <div style="color:${color};font-size:12px">● ${label}</div>
          <div style="margin-left:auto">
            <button class="btn btn-sm ${on ? 'btn-ghost' : ''}"
                    onclick="appsToggle('${a.id}', ${on ? 'false' : 'true'})">
              ${on ? 'Вимкнути' : 'Увімкнути'}
            </button>
          </div>
        </div>
        ${a.description ? `<div class="muted" style="margin-top:6px">${escapeHtml(a.description)}</div>` : ''}
        ${a.author ? `<div class="muted" style="font-size:11px;margin-top:4px">Автор: ${escapeHtml(a.author)}</div>` : ''}
        ${deps.length ? `<div class="muted" style="font-size:11px;margin-top:4px">Залежить від: ${deps.join(', ')}</div>` : ''}
        ${(a.dependents || []).length ? `<div class="muted" style="font-size:11px;margin-top:4px">Від нього залежать: ${a.dependents.join(', ')}</div>` : ''}
        ${a.error ? `<div style="color:var(--red);font-size:11px;margin-top:6px;font-family:var(--mono)">${escapeHtml(a.error)}</div>` : ''}
      </div>`;
  }).join('');
}

async function appsToggle(appId, enable) {
  const action = enable ? 'enable' : 'disable';
  try {
    const r = await fetch(`${API}/api/apps/${appId}/${action}`,
                          { method: 'POST', credentials: 'include' });
    const j = await r.json();
    if (!j.ok) {
      alert(j.message || 'Не вдалося змінити стан застосунку');
    } else if (j.message) {
      alert(j.message);   // «набуде чинності після перезапуску» — краще сказати прямо
    }
  } catch (e) {
    alert('Помилка: ' + e);
  }
  loadAppsPage();
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── старт ─────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // трохи пізніше за dashboard.js: йому треба встигнути дізнатись користувача
  setTimeout(appsBuildMenu, 300);
});


// ═══ Налаштування застосунків ═══════════════════════════════════════
// Застосунок реєструє свою секцію, ядро її малює й питає при збереженні.
// Вимкнули застосунок — секції просто немає, ядро про неї нічого не знає.
//
//   registerAppConfig('furry', {
//     render()        -> HTML своєї секції
//     load(cfg)       -> заповнити поля з конфігу
//     collect()       -> {ключ: значення} для відправки на сервер
//   });

const _appConfigSections = {};

function registerAppConfig(appId, section) {
  _appConfigSections[appId] = section;
}

// Малює секції всіх увімкнених застосунків. Викликається ядром при
// відкритті сторінки конфігу.
async function appsConfigRender() {
  const box = document.getElementById('config-app-sections');
  if (!box) return;

  // підвантажуємо config-скрипти застосунків (один раз)
  for (const p of (_appsUi.pages || [])) {
    if (p.config_js) await appLoadAsset(p.config_js, 'js');
  }

  box.innerHTML = Object.entries(_appConfigSections)
    .map(([id, s]) => {
      try { return s.render ? s.render() : ''; }
      catch (e) { console.error('config-секція', id, e); return ''; }
    }).join('');
}

// Заповнити поля секцій значеннями з конфігу
function appsConfigLoad(cfg) {
  Object.entries(_appConfigSections).forEach(([id, s]) => {
    try { if (s.load) s.load(cfg); }
    catch (e) { console.error('config.load', id, e); }
  });
}

// Зібрати те, що застосунки хочуть зберегти
function appsConfigCollect() {
  const out = {};
  Object.entries(_appConfigSections).forEach(([id, s]) => {
    try { Object.assign(out, (s.collect && s.collect()) || {}); }
    catch (e) { console.error('config.collect', id, e); }
  });
  return out;
}

// ═══ Зони розширення ══════════════════════════════════════════════════
// Ядро оголошує кілька місць, куди застосунок може додати кнопку чи будь-
// який свій елемент. Застосунок робить це зі свого файлу — ядро при цьому
// не змінюється, а вимкнений застосунок просто зникає разом зі своїми
// кнопками.
//
//   registerContribution('file.actions', {
//     id: 'myapp-send', icon: '📤', title: 'Надіслати', order: 20,
//     onClick: (ctx) => myappSend(ctx.file_id),
//   });
//
// Зони ядра:
//   file.actions     — кнопки в картці файлу.        ctx: {file_id}
//   gallery.toolbar  — панель над «Артами».          ctx: {}
//   catalog.toolbar  — панель каталогу.              ctx: {path}
//   live.panel       — під пошуком «Онлайн», для джерела, яке має своє
//                      керування (Telegram: акаунт і канали).  ctx: {source}
//
// Замість кнопки внесок може намалювати свій блок: mount(el, ctx) отримує
// порожній div і робить у ньому що треба (зона перемальовується, коли
// змінюється контекст, напр. обране джерело).
//
// Свідомо небагато: кожна зона — це обіцянка не ламати її надалі.
// Додати нову зону легко, прибрати — вже ні.

const _contributions = {};

function registerContribution(zone, item) {
  if (!item || !item.id) { console.warn('Внесок без id у зону', zone); return; }
  (_contributions[zone] ||= []).push(item);
  // якщо зону вже намальовано — домальовуємо, не чекаючи наступного разу
  document.querySelectorAll(`[data-zone="${zone}"]`).forEach(el => renderZone(el));
}

function renderZone(el) {
  const zone = el.dataset.zone;
  const items = (_contributions[zone] || []).slice().sort((a, b) => (a.order ?? 50) - (b.order ?? 50));
  el.innerHTML = items.map(i => {
    if (i.mount) return `<div data-contrib-mount="${esc(i.id)}"></div>`;
    if (i.html) return i.html;               // повна свобода, якщо треба
    return `<button class="btn btn-ghost btn-sm" data-contrib="${esc(i.id)}"
                    title="${esc(i.title || '')}">${i.icon || ''} ${esc(i.label || '')}</button>`;
  }).join('');
  // контекст зони беремо з самого елемента: data-* атрибути
  const ctx = {...el.dataset};
  delete ctx.zone;
  el.querySelectorAll('[data-contrib]').forEach(btn => {
    const item = items.find(i => i.id === btn.dataset.contrib);
    if (item && item.onClick) btn.addEventListener('click', () => item.onClick(ctx));
  });
  el.querySelectorAll('[data-contrib-mount]').forEach(box => {
    const item = items.find(i => i.id === box.dataset.contribMount);
    try { item && item.mount(box, ctx); }
    catch (e) { console.error('zone mount', zone, item && item.id, e); }
  });
}

// Малює всі зони на сторінці. Ядро кличе це після того, як намалювало
// свій вміст (напр. картку файлу, яка щоразу створюється наново).
function renderZones(root) {
  (root || document).querySelectorAll('[data-zone]').forEach(el => renderZone(el));
}

// Файли застосунків, які треба завантажити ОДРАЗУ, а не при відкритті
// їхньої сторінки: саме вони реєструють внески в чужі зони.
async function appsLoadStartupScripts() {
  for (const p of (_appsUi.pages || [])) {
    for (const url of (p.scripts || [])) await appLoadAsset(url, 'js');
  }
  renderZones();
}
