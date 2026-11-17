// Застосунок «Nextcloud»: лише параметри підключення та перевірка.
// Ядро архіву з Nextcloud не працює — файли лежать у локальній теці.
(function () {
  const BASE = () => `${API}/api/apps/nextcloud`;
  const esc = s => (typeof escapeHtml === 'function' ? escapeHtml(String(s ?? '')) : String(s ?? ''));
  const T = s => (typeof tr === 'function' ? tr(s) : s);

  function field(id, label, value, type = 'text', ph = '') {
    return `<div class="field"><label>${label}</label>
      <input type="${type}" id="${id}" value="${esc(value)}" placeholder="${esc(ph)}"></div>`;
  }

  async function render(root) {
    root.innerHTML = '<div class="muted">Завантаження…</div>';
    let s;
    try {
      const r = await fetch(`${BASE()}/settings`, { credentials: 'include' });
      s = await r.json();
      if (!r.ok) throw new Error(s.error || r.status);
    } catch (e) {
      root.innerHTML = `<div class="muted">Не вдалося отримати налаштування: ${esc(e.message || e)}</div>`;
      return;
    }
    const nc = s.nextcloud || {}, db = s.nextcloud_db || {};
    const keep = 'залишити без змін';
    root.innerHTML = `
      <div class="card" style="margin-bottom:14px">
        <div class="muted" style="font-size:13px;line-height:1.5">
          Архів зберігає файли в локальній теці й від Nextcloud не залежить.
          Тут — лише параметри підключення до Nextcloud.
        </div>
      </div>
      <div class="card">
        <div class="section-header">// WebDAV</div>
        <div class="config-grid">
          <div class="field" style="grid-column:1/-1"><label>Адреса WebDAV</label>
            <input type="text" id="nc-url" value="${esc(nc.webdav_url)}"
                   placeholder="https://cloud.example.com/remote.php/dav/files/user/"></div>
          ${field('nc-user', 'Користувач', nc.username)}
          ${field('nc-pass', 'Пароль', '', 'password', nc.has_password ? keep : '')}
          ${field('nc-base', 'Тека архіву', nc.base_folder, 'text', 'Archive')}
          ${field('nc-share', 'Токен публічного посилання', nc.share_token)}
          <div class="field" style="grid-column:1/-1"><label>Тека даних Nextcloud на сервері</label>
            <input type="text" id="nc-local" value="${esc(nc.local_data_path)}" placeholder="/var/www/nextcloud/data/user/files"></div>
        </div>
        <div class="section-header" style="margin-top:14px">// База Nextcloud</div>
        <div class="config-grid">
          ${field('ncdb-host', 'Хост', db.host)}
          ${field('ncdb-port', 'Порт', db.port || '', 'text', '3306')}
          ${field('ncdb-user', 'Користувач', db.user)}
          ${field('ncdb-pass', 'Пароль', '', 'password', db.has_password ? keep : '')}
          ${field('ncdb-name', 'База даних', db.database, 'text', 'nextcloud')}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">
          <button class="btn" id="nc-save">Зберегти</button>
          <button class="btn btn-ghost" id="nc-test">Перевірити підключення</button>
        </div>
        <div id="nc-result" style="margin-top:12px;font-size:13px"></div>
      </div>`;
    const v = id => root.querySelector('#' + id).value;
    const body = () => ({
      nextcloud: { webdav_url: v('nc-url'), username: v('nc-user'), password: v('nc-pass'),
                   base_folder: v('nc-base'), share_token: v('nc-share'), local_data_path: v('nc-local') },
      nextcloud_db: { host: v('ncdb-host'), port: v('ncdb-port'), user: v('ncdb-user'),
                      password: v('ncdb-pass'), database: v('ncdb-name') },
    });
    const out = root.querySelector('#nc-result');
    async function save(silent) {
      const r = await fetch(`${BASE()}/settings`, { method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body()) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { if (typeof toast === 'function') toast(T(d.error || 'Помилка'), 'err'); return false; }
      if (!silent && typeof toast === 'function') toast(T('Збережено'), 'ok');
      return true;
    }
    root.querySelector('#nc-save').onclick = () => save(false);
    root.querySelector('#nc-test').onclick = async () => {
      out.innerHTML = '<span class="muted">Перевіряю…</span>';
      if (!(await save(true))) { out.innerHTML = ''; return; }
      try {
        const d = await fetch(`${BASE()}/test`, { method: 'POST', credentials: 'include' }).then(r => r.json());
        const line = x => x ? `<div style="color:${x.ok ? 'var(--green)' : 'var(--red)'}">${x.ok ? '✓' : '✗'} ${esc(T(x.msg))}</div>` : '';
        out.innerHTML = line(d.webdav) + line(d.db);
      } catch (e) {
        out.innerHTML = `<div style="color:var(--red)">✗ ${esc(e.message || e)}</div>`;
      }
    };
  }

  window.nextcloudPageInit = render;
})();
