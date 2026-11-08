// ── Переклади інтерфейсу ─────────────────────────────────────────────────
// Словники приходять із сервера (/i18n.js → window.I18N_BUNDLE): ядро
// (locales/*.json) + кожен застосунок (apps/installed/<id>/locales/*.json).
//
// Українська — базова: тексти пишуться в коді українською. Коли обрано іншу
// мову, сторінка сама підставляє переклади: текст у розмітці, title/
// placeholder, тости, alert/confirm/prompt, повідомлення сервера. Новий
// текст у коді = один рядок «українська фраза → переклад» у en.json.
//
//   t('nav.overview')          — переклад за ключем (як і раніше)
//   tr('Видалити теку «{0}»')  — переклад довільної фрази (з підстановкою)
//   data-no-i18n на елементі   — не чіпати вміст (імена, теги, повідомлення)

const I18N = window.I18N_BUNDLE || { uk: {} };
let _lang = (() => { try { return localStorage.getItem('lang') || 'uk'; } catch { return 'uk'; } })();
if (!I18N[_lang]) _lang = 'uk';

function t(key) {
  return (I18N[_lang] && I18N[_lang][key]) ?? (I18N.uk[key] ?? key);
}

const _I18N_CYR = /[А-Яа-яІіЇїЄєҐґ]/;
let _i18nExact = null;     // нормалізована фраза → переклад
let _i18nPats = null;      // {re, slots, out} для фраз з {0}
let _i18nPatIdx = null;    // перші 2 літери → [шаблони]
const _i18nMemo = new Map();
const _i18nMisses = new Set();

// Що на сторінці лишилось неперекладеним — у консолі браузера:
//   i18nMissing()  → готовий шматок JSON, який лишається перекласти й
//   вставити в потрібний locales/en.json (ядра чи застосунку)
function i18nMissing() {
  const out = {};
  [..._i18nMisses].sort().forEach(s => { out[s] = ''; });
  console.log(JSON.stringify(out, null, 1));
  return out;
}

function _i18nNorm(s) { return s.replace(/\s+/g, ' ').trim(); }

function _i18nPrepare() {
  if (_i18nExact) return;
  _i18nExact = new Map(); _i18nPats = []; _i18nPatIdx = new Map();
  const dict = I18N[_lang] || {};
  for (const [k, v] of Object.entries(dict)) {
    if (!_I18N_CYR.test(k)) continue;
    const nk = _i18nNorm(k);
    _i18nExact.set(nk, v);
    if (!/\{\d+\}/.test(nk)) continue;
    const slots = [];
    const src = nk.split(/(\{\d+\})/).map(part => {
      const m = part.match(/^\{(\d+)\}$/);
      if (m) { slots.push(m[1]); return '(.*?)'; }
      return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('');
    const pat = { re: new RegExp('^' + src + '$'), slots, out: v };
    const lead = nk.match(/^[^{]*/)[0];
    const key = lead.length >= 2 ? lead.slice(0, 2) : '*';
    if (!_i18nPatIdx.has(key)) _i18nPatIdx.set(key, []);
    _i18nPatIdx.get(key).push(pat);
  }
}

// Розділити на «краї» (емодзі, розділові знаки, пробіли) і саму фразу —
// так само, як їх ріже збирач фраз у словник.
function _i18nEdges(s) {
  const m = s.match(/^([^\p{L}\p{N}{]*)([\s\S]*?)([^\p{L}\p{N}}]*)$/u);
  if (!m) return ['', s, ''];
  let [, pre, core, suf] = m;
  for (const [o, c] of [['«', '»'], ['(', ')'], ['"', '"']]) {
    const cnt = ch => core.split(ch).length - 1;
    if (o !== c && cnt(o) > cnt(c)) {
      if (suf.startsWith(c)) { core += c; suf = suf.slice(1); }
    }
  }
  return [pre, core, suf];
}

function _i18nPattern(core) {
  const cands = (_i18nPatIdx.get(core.slice(0, 2)) || []).concat(_i18nPatIdx.get('*') || []);
  for (const p of cands) {
    const m = core.match(p.re);
    if (!m) continue;
    const vals = {};
    p.slots.forEach((slot, i) => { vals[slot] = m[i + 1]; });
    return p.out.replace(/\{(\d+)\}/g, (_, n) => {
      const v = vals[n] ?? '';
      return _I18N_CYR.test(v) ? _i18nCore(v) : v;
    });
  }
  return null;
}

// Окремі слова/звороти (напр. «12 файлів», «Не вдалося зберегти: …»)
function _i18nRuns(s) {
  return s.replace(/[А-Яа-яІіЇїЄєҐґ'ʼ’][А-Яа-яІіЇїЄєҐґ'ʼ’()-]*(?:[  ]+[А-Яа-яІіЇїЄєҐґ'ʼ’][А-Яа-яІіЇїЄєҐґ'ʼ’()-]*)*/g,
    run => _i18nExact.get(run) ?? run);
}

function _i18nCore(core) {
  const exact = _i18nExact.get(core);
  if (exact !== undefined) return exact;
  const byPat = _i18nPattern(core);
  if (byPat !== null) return byPat;
  // кілька речень/частин, склеєних у коді — перекладаємо по частинах
  const parts = core.split(/(\s*[.?!:;]\s+|\s+[—–]\s+|\s*\n+\s*)/);
  if (parts.length > 1) {
    return parts.map((p, i) => {
      if (i % 2 === 1 || !_I18N_CYR.test(p)) return p;
      const [a, c, b] = _i18nEdges(p);
      return a + (_i18nExact.get(c) ?? _i18nPattern(c) ?? _i18nRuns(c)) + b;
    }).join('');
  }
  return _i18nRuns(core);
}

// Перекласти довільний текст на поточну мову (українська → як є).
function tr(text, ...args) {
  let s = text == null ? '' : String(text);
  if (args.length) s = s.replace(/\{(\d+)\}/g, (m, n) => (args[n] ?? m));
  if (_lang === 'uk' || !_I18N_CYR.test(s)) return s;
  _i18nPrepare();
  const hit = _i18nMemo.get(s);
  if (hit !== undefined) return hit;
  let out;
  if (s.includes('\n')) {
    out = s.split('\n').map(line => _I18N_CYR.test(line) ? tr(line) : line).join('\n');
  } else {
    const [, lead, body, tail] = s.match(/^(\s*)([\s\S]*?)(\s*)$/);
    const full = _i18nNorm(body);
    const [pre, core, suf] = _i18nEdges(full);
    const exact = _i18nExact.get(core);
    let mid;
    if (exact !== undefined) mid = pre + exact + suf;
    else {
      // фрази на зразок «Змінити роль на «{0}»?{1}» тримають знак усередині
      mid = pre + _i18nCore(core) + suf;
      if (_I18N_CYR.test(mid)) {
        const whole = _i18nPattern(full);
        if (whole !== null && !_I18N_CYR.test(whole)) mid = whole;
      }
    }
    out = lead + mid + tail;
  }
  if (_I18N_CYR.test(out)) _i18nMisses.add(s.trim());
  if (_i18nMemo.size > 5000) _i18nMemo.clear();
  _i18nMemo.set(s, out);
  return out;
}

// ── Автопереклад сторінки ────────────────────────────────────────────────
const _I18N_SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'CODE', 'PRE', 'NOSCRIPT']);
const _I18N_ATTRS = ['title', 'placeholder', 'aria-label', 'alt', 'data-tip'];

function _i18nSkip(el) {
  for (let e = el; e && e !== document.body; e = e.parentElement) {
    if (_I18N_SKIP.has(e.tagName) || e.isContentEditable || (e.hasAttribute && e.hasAttribute('data-no-i18n'))) return true;
  }
  return false;
}

function _i18nTextNode(n) {
  const v = n.nodeValue;
  if (!v || !_I18N_CYR.test(v)) return;
  if (_i18nSkip(n.parentElement)) return;
  const out = tr(v);
  if (out !== v) n.nodeValue = out;
}

function _i18nAttrs(el) {
  for (const a of _I18N_ATTRS) {
    const v = el.getAttribute(a);
    if (v && _I18N_CYR.test(v)) {
      const out = tr(v);
      if (out !== v) el.setAttribute(a, out);
    }
  }
  if (el.tagName === 'INPUT' && (el.type === 'button' || el.type === 'submit') && _I18N_CYR.test(el.value)) {
    el.value = tr(el.value);
  }
}

function i18nTranslateTree(root) {
  if (_lang === 'uk' || !root) return;
  if (root.nodeType === 3) { _i18nTextNode(root); return; }
  if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
  if (root.nodeType === 1) {
    if (_i18nSkip(root)) return;
    _i18nAttrs(root);
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (n.nodeType === 1) {
        if (_I18N_SKIP.has(n.tagName) || n.hasAttribute('data-no-i18n') || n.isContentEditable) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
      return _I18N_CYR.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    }
  });
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeType === 1) _i18nAttrs(n);
    else { const out = tr(n.nodeValue); if (out !== n.nodeValue) n.nodeValue = out; }
  }
}

function _i18nStart() {
  if (_lang === 'uk') return;
  document.documentElement.lang = _lang;
  i18nTranslateTree(document.body);
  if (_I18N_CYR.test(document.title)) document.title = tr(document.title);
  new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === 'childList') m.addedNodes.forEach(i18nTranslateTree);
      else if (m.type === 'characterData') _i18nTextNode(m.target);
      else if (m.type === 'attributes' && m.target.nodeType === 1 && !_i18nSkip(m.target)) _i18nAttrs(m.target);
    }
  }).observe(document.body, {
    childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: _I18N_ATTRS.concat(['value']),
  });
  const titleEl = document.querySelector('title');
  if (titleEl) new MutationObserver(() => {
    if (_I18N_CYR.test(document.title)) document.title = tr(document.title);
  }).observe(titleEl, { childList: true, characterData: true, subtree: true });

  // діалоги браузера не потрапляють у DOM — перекладаємо їхній текст тут
  const _alert = window.alert.bind(window), _confirm = window.confirm.bind(window), _prompt = window.prompt.bind(window);
  window.alert = msg => _alert(tr(msg));
  window.confirm = msg => _confirm(tr(msg));
  window.prompt = (msg, def) => _prompt(tr(msg), def);
}

if (document.body) _i18nStart();
else document.addEventListener('DOMContentLoaded', _i18nStart);
