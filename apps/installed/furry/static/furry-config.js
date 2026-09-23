// Налаштування застосунку Furry Archive.
// Ядро дає місце на сторінці конфігу, решту малює й збирає застосунок.
// Вимкнули застосунок — цієї секції в налаштуваннях просто немає.

registerAppConfig('furry', {

  render() {
    return `<div class="section-header">// E621</div>
        <div class="config-grid">
          <div class="field" style="grid-column:1/-1">
            <label class="toggle-row">
              <span class="toggle-switch"><input type="checkbox" id="cfg-e621-enabled" onchange="toggleE621Fields()"><span class="toggle-slider"></span></span>
              <span data-i18n="cfg.enabled">Увімкнено</span>
            </label>
          </div>
        </div>
        <div id="cfg-e621-fields" class="config-grid">
          <div class="field"><label>Username</label><input type="text" id="cfg-e621-user"></div>
          <div class="field"><label>API Key</label><input type="password" id="cfg-e621-key"></div>
          <div class="field"><label data-i18n="cfg.maxposts">Макс. постів</label><input type="number" id="cfg-e621-max" data-i18n-ph="cfg.nolimit" placeholder="(без ліміту)"></div>
        </div>

        <div class="section-header">// Inkbunny</div>
        <div class="config-grid">
          <div class="field" style="grid-column:1/-1">
            <label class="toggle-row">
              <span class="toggle-switch"><input type="checkbox" id="cfg-ib-enabled" onchange="toggleIbFields()"><span class="toggle-slider"></span></span>
              <span data-i18n="cfg.enabled">Увімкнено</span>
            </label>
          </div>
        </div>
        <div id="cfg-ib-fields" class="config-grid">
          <div class="field"><label>Username</label><input type="text" id="cfg-ib-user"></div>
          <div class="field"><label data-i18n="auth.password">Пароль</label><input type="password" id="cfg-ib-pass"></div>
          <div class="field"><label data-i18n="cfg.maxsubs">Макс. робіт</label><input type="number" id="cfg-ib-max" data-i18n-ph="cfg.nolimit" placeholder="(без ліміту)"></div>
        </div>

        <div class="section-header" data-i18n="cfg.download">// Завантаження</div>
        <div class="config-grid">
          <div class="field"><label data-i18n="cfg.tmpfolder">Тимчасова папка</label><input type="text" id="cfg-dl-tmp" placeholder="./temp_downloads"></div>
          <div class="field"><label data-i18n="cfg.phashthresh">pHash поріг (0–20)</label><input type="number" id="cfg-dl-phash" min="0" max="20"></div>
          <div class="field"><label data-i18n="cfg.dupcheck">Перевірка дублікатів</label>
            <select id="cfg-dl-dup"><option value="true" data-i18n="cfg.enabled">Увімкнено</option><option value="false" data-i18n="cfg.disabled">Вимкнено</option></select></div>
        </div>

        <div class="section-header">// Авто-оновлення</div>
        <div class="config-grid">
          <div class="field"><label>Година запуску (0–23)</label>
            <input type="number" id="cfg-scheduler-hour" min="0" max="23" value="4">`;
  },

  load(cfg) {
    const v = (a, b) => (cfg[a] || {})[b] ?? '';
    const set = (id, val) => { const e = document.getElementById(id); if (e) e.value = val; };
    const chk = (id, val) => { const e = document.getElementById(id); if (e) e.checked = val; };

    chk('cfg-e621-enabled', v('e621','enabled') !== false);
    set('cfg-e621-user',    v('e621','username'));
    set('cfg-e621-max',     v('e621','max_posts_per_artist') || '');
    chk('cfg-ib-enabled',   v('inkbunny','enabled') !== false);
    set('cfg-ib-user',      v('inkbunny','username'));
    set('cfg-ib-max',       v('inkbunny','max_submissions_per_artist') || '');

    // секрети не показуємо, лише позначаємо, що вони збережені
    [['cfg-e621-key', ['e621','api_key']], ['cfg-ib-pass', ['inkbunny','password']]]
      .forEach(([id, [a, b]]) => {
        const el = document.getElementById(id);
        if (!el) return;
        const val = (cfg[a] || {})[b];
        el.value = '';
        // маска '••••••••' від сервера означає "збережено"
        el.placeholder = val
          ? '\u25cf збережено (залиш пустим щоб не змінювати)'
          : '(не вказано)';
      });

    // блоки «Завантаження» і «Авто-оновлення» теж належать цьому застосунку
    set('cfg-dl-tmp',   v('download','local_temp_dir'));
    set('cfg-dl-phash', (cfg.download || {}).phash_threshold ?? 8);
    set('cfg-dl-dup',   (cfg.download || {}).check_duplicates === false ? 'false' : 'true');
    set('cfg-scheduler-hour',
        (cfg.scheduler || {}).auto_update_hour === '' || (cfg.scheduler || {}).auto_update_hour == null
          ? 4 : cfg.scheduler.auto_update_hour);

    furryToggleFields();
  },

  collect() {
    const g   = id => (document.getElementById(id)?.value || '').trim();
    const chk = id => !!document.getElementById(id)?.checked;
    const num = id => parseInt(g(id)) || 0;
    return {
      e621: {
        enabled:  chk('cfg-e621-enabled'),
        username: g('cfg-e621-user'),
        api_key:  g('cfg-e621-key') || null,
        max_posts_per_artist: num('cfg-e621-max'),
      },
      inkbunny: {
        enabled:  chk('cfg-ib-enabled'),
        username: g('cfg-ib-user'),
        password: g('cfg-ib-pass') || null,
        max_submissions_per_artist: num('cfg-ib-max'),
      },
      download: {
        local_temp_dir:   g('cfg-dl-tmp'),
        phash_threshold:  num('cfg-dl-phash'),
        check_duplicates: g('cfg-dl-dup') === 'true',
      },
      scheduler: {
        auto_update_hour: num('cfg-scheduler-hour'),
      },
    };
  },
});

function furryToggleFields() {
  const pair = [['cfg-e621-enabled','cfg-e621-fields'], ['cfg-ib-enabled','cfg-ib-fields']];
  pair.forEach(([sw, box]) => {
    const s = document.getElementById(sw), b = document.getElementById(box);
    if (s && b) b.style.display = s.checked ? '' : 'none';
  });
}

// імена, на які посилається розмітка, вирізана з ядра
function toggleE621Fields() { furryToggleFields(); }
function toggleIbFields()   { furryToggleFields(); }
