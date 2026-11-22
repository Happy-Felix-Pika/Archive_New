// Застосунок «Пошук джерел»: налаштування, запуск, хід пошуку.
(function () {
  const BASE = () => `${API}/api/apps/sourcefind`;
  let _root = null, _poll = null;

  window.sourcefindPageInit = async function (root) {
    _root = root;
    root.innerHTML = `
      <div class="card" style="margin-bottom:14px">
        <div style="font-weight:600;margin-bottom:6px">🔎 Пошук джерел</div>
        <div class="muted" style="font-size:12.5px;line-height:1.5">
          Для картинок без посилання на оригінал архів сам шукає, звідки вони:
          спершу точний збіг файлу на e621, потім пошук за картинкою — Fluffle (e621, Inkbunny,
          FurAffinity, Weasyl…; без ключа), FuzzySearch (якщо є ключ) і пошук e621. Знайдене впевнено — одразу ставиться посилання на
          оригінал і теги (з e621 чи Inkbunny). Сумнівне — у «На перевірку → 🔎 Джерела».
          Файли нікуди не переносяться.
        </div>
      </div>
      <div class="card" style="margin-bottom:14px" id="sf-status"><div class="muted">Завантаження…</div></div>
      <div class="card" style="margin-bottom:14px">
        <div style="font-weight:600;margin-bottom:10px">Запуск</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <input type="text" id="sf-folder" placeholder="Тека (порожньо — увесь архів)"
                 style="flex:1;min-width:220px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-family:var(--mono);font-size:13px">
          <label style="font-size:12px;display:flex;gap:6px;align-items:center"><input type="checkbox" id="sf-recheck"> ще раз і ті, що не знайшлись</label>
          <button class="btn btn-primary" id="sf-run" onclick="sourcefindRun()">🔎 Шукати</button>
          <button class="btn btn-ghost" id="sf-cancel" style="display:none" onclick="sourcefindCancel()">■ Зупинити</button>
        </div>
        <div id="sf-job" style="margin-top:12px"></div>
      </div>
      <div class="card">
        <div style="font-weight:600;margin-bottom:10px">Налаштування</div>
        <div class="field">
          <label>API-ключ FuzzySearch <span class="muted" style="font-weight:400">(необов'язково — ключ видає автор сервісу, Syfaro; без нього працюють Fluffle й e621)</span></label>
          <div style="display:flex;gap:8px">
            <input type="password" id="sf-key" placeholder="ключ" autocomplete="off" style="flex:1">
            <button class="btn btn-ghost btn-sm" onclick="sourcefindSaveKey()">Зберегти</button>
          </div>
          <div id="sf-key-state" class="muted" style="font-size:11.5px;margin-top:4px"></div>
        </div>
        <label style="font-size:13px;display:flex;gap:8px;align-items:center;margin-top:6px">
          <input type="checkbox" id="sf-fluffle" onchange="sourcefindSaveOpt('use_fluffle', this.checked)"> шукати через Fluffle (без ключа: e621, Inkbunny, FurAffinity, Weasyl, Bluesky)
        </label>
        <label style="font-size:13px;display:flex;gap:8px;align-items:center;margin-top:6px">
          <input type="checkbox" id="sf-iqdb" onchange="sourcefindSaveIqdb(this.checked)"> шукати схожі й у власному пошуку e621 (IQDB)
        </label>
        <div class="muted" style="font-size:11.5px;margin-top:8px">Логін і ключі e621 / Inkbunny беруться з «Конфігу».</div>
      </div>`;
    await sourcefindRefresh();
  };

  window.sourcefindRefresh = async function () {
    if (!_root || !document.body.contains(_root)) { clearInterval(_poll); _poll = null; return; }
    let s;
    try { s = await fetch(`${BASE()}/status`).then(r => r.json()); } catch { return; }
    if (s.error) { document.getElementById('sf-status').innerHTML = `<div style="color:var(--red)">${esc(s.error)}</div>`; return; }
    const c = s.counts || {};
    document.getElementById('sf-status').innerHTML = `
      <div style="display:flex;gap:22px;flex-wrap:wrap;font-size:13px">
        <div><div class="muted" style="font-size:11px">ЩЕ НЕ ПЕРЕВІРЕНО</div><div style="font-size:20px;font-weight:600">${s.todo == null ? '<span class="muted" style="font-size:13px">рахую…</span>' : s.todo.toLocaleString('uk-UA')}</div></div>
        <div><div class="muted" style="font-size:11px">ЗНАЙДЕНО</div><div style="font-size:20px;font-weight:600;color:var(--green)">${(c.applied || 0).toLocaleString('uk-UA')}</div></div>
        <div><div class="muted" style="font-size:11px">НА ПЕРЕВІРКУ</div><div style="font-size:20px;font-weight:600;color:var(--yellow)">${(c.review || 0).toLocaleString('uk-UA')}</div></div>
        <div><div class="muted" style="font-size:11px">НЕ ЗНАЙДЕНО</div><div style="font-size:20px;font-weight:600">${(c.none || 0).toLocaleString('uk-UA')}</div></div>
        ${c.error ? `<div><div class="muted" style="font-size:11px">ПОМИЛКИ</div><div style="font-size:20px;font-weight:600;color:var(--red)">${c.error}</div></div>` : ''}
      </div>
      ${c.review ? `<div style="margin-top:10px"><a href="#" onclick="showPage('pending');pendingSetTab('sources');return false">→ Переглянути сумнівні</a></div>` : ''}`;
    document.getElementById('sf-key-state').textContent = s.has_fuzzy_key ? '✓ ключ збережено' : 'ключ не вказано';
    document.getElementById('sf-iqdb').checked = !!s.use_iqdb;
    document.getElementById('sf-fluffle').checked = s.use_fluffle !== false;
    const j = s.job, running = j && (j.status === 'running' || j.status === 'pending');
    document.getElementById('sf-run').disabled = running;
    document.getElementById('sf-cancel').style.display = running ? '' : 'none';
    document.getElementById('sf-cancel').dataset.jobId = j?.id || '';
    const box = document.getElementById('sf-job');
    if (j) {
      const res = (() => { try { return typeof j.result === 'string' ? JSON.parse(j.result) : j.result; } catch { return null; } })();
      box.innerHTML = running
        ? `<div style="display:flex;align-items:center;gap:10px"><div class="gallery-spinner" style="width:16px;height:16px;margin:0"></div>
             <div style="font-weight:600;font-size:13px">Пошук іде — ${j.progress || 0}%</div></div>
           <div style="height:8px;background:var(--surface2);border-radius:4px;overflow:hidden;margin-top:8px"><div style="height:100%;width:${Math.max(2, j.progress || 0)}%;background:var(--accent);transition:width .4s"></div></div>
           <div class="muted" style="font-size:12px;margin-top:6px">${esc(j.message || 'Готуюсь…')}</div>`
        : `<div class="muted" style="font-size:12px">Останній запуск: ${esc(j.status)}${res ? ` — знайдено ${res.applied || 0}, на перевірку ${res.review || 0}, не знайдено ${res.none || 0}` : ''}${j.error ? ` — ${esc(j.error)}` : ''}</div>`;
    } else box.innerHTML = '';
    // поки йде пошук або ще рахується «не перевірено» — оновлюємо сторінку саму
    const want = running || s.todo == null;
    if (want && !_poll) _poll = setInterval(sourcefindRefresh, 3000);
    if (!want && _poll) { clearInterval(_poll); _poll = null; }
  };

  window.sourcefindRun = async function () {
    const r = await fetch(`${BASE()}/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder: document.getElementById('sf-folder').value, recheck: document.getElementById('sf-recheck').checked }) })
      .then(x => x.json()).catch(() => ({ error: 'Помилка мережі' }));
    if (r.error) { toast(r.error, 'err'); return; }
    toast('🔎 Пошук запущено', 'ok');
    sourcefindRefresh();
  };

  window.sourcefindCancel = async function () {
    const id = document.getElementById('sf-cancel').dataset.jobId;
    if (id) await fetch(`${API}/api/jobs/${id}/cancel`, { method: 'POST' }).catch(() => {});
    sourcefindRefresh();
  };

  window.sourcefindSaveKey = async function () {
    const v = document.getElementById('sf-key').value.trim();
    await fetch(`${BASE()}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fuzzy_key: v }) });
    document.getElementById('sf-key').value = '';
    toast(v ? 'Ключ збережено' : 'Ключ прибрано', 'ok');
    sourcefindRefresh();
  };

  window.sourcefindSaveOpt = async function (key, on) {
    await fetch(`${BASE()}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: on }) });
  };

  window.sourcefindSaveIqdb = async function (on) {
    await fetch(`${BASE()}/settings`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_iqdb: on }) });
  };
})();
