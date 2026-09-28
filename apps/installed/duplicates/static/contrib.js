// Кнопка «Знайти схожі» в картці файлу.
// Файл завантажується при старті (manifest → scripts) і реєструє кнопку
// в зоні ядра 'file.actions'. Ядро про цю кнопку нічого не знає;
// вимкнули застосунок — кнопка зникла разом із ним.

registerContribution('file.actions', {
  id: 'dup-similar',
  icon: '🔍',
  title: 'Знайти схожі файли',
  order: 10,
  onClick: (ctx) => dupShowSimilar(ctx.file_id),
});

async function dupShowSimilar(fileId) {
  let data;
  try {
    data = await fetch(`/api/duplicates/similar/${fileId}`, {credentials: 'include'}).then(r => r.json());
  } catch (e) { alert('Не вдалося пошукати: ' + e); return; }

  const items = data.similar || [];
  const old = document.getElementById('dup-similar-modal');
  if (old) old.remove();

  const box = document.createElement('div');
  box.id = 'dup-similar-modal';
  box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);display:flex;align-items:center;justify-content:center;z-index:200;padding:20px';
  box.innerHTML = `
    <div class="card" style="width:min(640px,100%);max-height:85vh;overflow:auto">
      <div style="font-weight:600;margin-bottom:10px">Схожі файли</div>
      ${items.length ? items.map(i => `
        <div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border)">
          <img src="/api/gallery/stream/${i.id}" style="width:54px;height:54px;object-fit:cover;border-radius:5px;background:var(--surface2)">
          <div style="min-width:0">
            <div style="font-size:13px">${i.name}</div>
            <div class="muted" style="font-size:11px;font-family:var(--mono)">${i.rel_path}</div>
          </div>
          <div style="margin-left:auto;text-align:right;white-space:nowrap">
            <div style="font-size:12px;color:${i.exact ? 'var(--red)' : 'var(--muted)'}">
              ${i.exact ? 'точна копія' : 'відмінність ' + i.distance}
            </div>
          </div>
        </div>`).join('')
       : '<div class="muted">Схожих не знайдено</div>'}
      <div style="text-align:right;margin-top:12px">
        <button class="btn btn-sm" onclick="document.getElementById('dup-similar-modal').remove()">Закрити</button>
      </div>
    </div>`;
  box.addEventListener('click', e => { if (e.target === box) box.remove(); });
  document.body.appendChild(box);
}
