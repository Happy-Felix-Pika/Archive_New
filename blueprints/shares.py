"""
Публічні посилання «Поділитися» — як у Nextcloud: файл або ціла тека
відкриваються за посиланням /s/<токен> без входу в архів.

Що видно за посиланням — рівно те, чим поділились: один файл або вміст
однієї теки (з її підтеками). Вийти вище неї, у службові теки чи до
решти архіву не можна. Посилання можна будь-коли відкликати.
"""

import mimetypes
import os
import secrets

from flask import Blueprint, Response, jsonify, request as flask_request, send_file

from core.auth import current_user_id, is_current_user_admin, is_current_user_moderator
from core.db import get_db, q, qq
from core.files_api import get_file, local_path
from core.storage_api import is_service_path

bp = Blueprint("shares", __name__)

IMG = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif"}
VID = {"mp4", "webm", "mov", "m4v"}
AUD = {"mp3", "ogg", "wav", "flac", "m4a"}


def public_base_url() -> str:
    """Адреса архіву ззовні: «Конфіг → Пошта → Адреса архіву», інакше — з запиту."""
    try:
        from core.auth import get_user_settings
        base = (get_user_settings().get("mail") or {}).get("base_url")
        if base:
            return base.rstrip("/")
    except Exception:
        pass
    proto = (flask_request.headers.get("X-Forwarded-Proto") or flask_request.scheme).split(",")[0].strip()
    host = (flask_request.headers.get("X-Forwarded-Host") or flask_request.host).split(",")[0].strip()
    return f"{proto}://{host}"


def _clean(rel: str) -> str | None:
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        return None
    return "/".join(parts)


def _hidden_top(rel: str) -> bool:
    if is_current_user_admin():
        return False
    try:
        from blueprints.catalog import get_hidden_catalog_folders
        top = rel.split("/", 1)[0]
        return "/" in rel and top in get_hidden_catalog_folders(current_user_id())
    except Exception:
        return False


def _out(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "path": r["rel_path"], "file_id": r.get("file_id"),
            "url": f"{public_base_url()}/s/{r['token']}", "views": r.get("views") or 0,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None}


# ── керування (для залогінених) ──────────────────────────────────────

@bp.route("/api/shares", methods=["POST"])
def api_share_create():
    d = flask_request.get_json(silent=True) or {}
    uid = current_user_id()
    pool_id = None
    if d.get("pool_id"):
        db = get_db()
        try:
            p = q(db, """SELECT p.id, p.name, a.artist_name FROM furry_pools p
                         JOIN artists a ON a.id = p.artist_id WHERE p.id=%s""", (int(d["pool_id"]),))
        except Exception:
            p = None
        finally:
            db.close()
        if not p:
            return jsonify({"error": "Пул не знайдено"}), 404
        kind, rel, fid, pool_id = "pool", f"{p['artist_name']} · {p['name']}", None, p["id"]
    elif d.get("file_id"):
        f = get_file(int(d["file_id"]))
        if not f or is_service_path(f["rel_path"]) or _hidden_top(f["rel_path"]):
            return jsonify({"error": "Файл недоступний"}), 404
        kind, rel, fid = "file", f["rel_path"], f["id"]
    else:
        rel = _clean(d.get("folder") or "")
        if not rel or is_service_path(rel + "/x"):
            return jsonify({"error": "Обери теку (увесь архів одним посиланням не віддаємо)"}), 400
        if not is_current_user_moderator():
            return jsonify({"error": "Ділитися цілою текою можуть лише модератори"}), 403
        if _hidden_top(rel + "/x"):
            return jsonify({"error": "Тека недоступна"}), 403
        if not os.path.isdir(local_path(rel)):
            return jsonify({"error": "Теку не знайдено"}), 404
        kind, fid = "folder", None
    db = get_db()
    try:
        # уже ділились цим — те саме посилання, а не нове щоразу
        find = ("SELECT * FROM share_links WHERE kind=%s AND created_by=%s AND "
                + ("pool_id=%s" if kind == "pool" else "rel_path=%s"))
        key = (kind, uid, pool_id if kind == "pool" else rel)
        row = q(db, find, key)
        if not row:
            with db.cursor() as cur:
                cur.execute("""INSERT INTO share_links (token, kind, rel_path, file_id, pool_id, created_by)
                               VALUES (%s,%s,%s,%s,%s,%s)""", (secrets.token_urlsafe(12), kind, rel, fid, pool_id, uid))
            db.commit()
            row = q(db, find, key)
    finally:
        db.close()
    return jsonify({"ok": True, **_out(row)})


@bp.route("/api/shares")
def api_share_list():
    db = get_db()
    try:
        if is_current_user_admin() and flask_request.args.get("all") == "1":
            rows = qq(db, "SELECT * FROM share_links ORDER BY created_at DESC")
        else:
            rows = qq(db, "SELECT * FROM share_links WHERE created_by=%s ORDER BY created_at DESC",
                      (current_user_id(),))
    finally:
        db.close()
    return jsonify([_out(r) for r in rows])


@bp.route("/api/shares/<int:share_id>", methods=["DELETE"])
def api_share_delete(share_id):
    db = get_db()
    try:
        with db.cursor() as cur:
            if is_current_user_admin():
                cur.execute("DELETE FROM share_links WHERE id=%s", (share_id,))
            else:
                cur.execute("DELETE FROM share_links WHERE id=%s AND created_by=%s", (share_id, current_user_id()))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


# ── публічна частина (без входу) ─────────────────────────────────────

def _share(token):
    if not token or len(token) > 64:
        return None
    db = get_db()
    try:
        return q(db, "SELECT * FROM share_links WHERE token=%s AND (expires_at IS NULL OR expires_at > NOW())",
                 (token,))
    finally:
        db.close()


def _resolve(sh, sub: str) -> str | None:
    """Шлях у сховищі для запиту всередині посилання — лише в межах того, чим поділились."""
    if sh["kind"] == "file":
        return sh["rel_path"]
    if sh["kind"] == "pool":
        return _pool_files(sh).get(str(sub))
    sub = _clean(sub)
    if sub is None:
        return None
    rel = f"{sh['rel_path']}/{sub}".strip("/") if sub else sh["rel_path"]
    if sub and is_service_path(rel):
        return None
    return rel


def _pool_files(sh) -> dict:
    """Файли пулу за порядком сторінок: {str(file_id): rel_path} (упорядкований)."""
    db = get_db()
    try:
        rows = qq(db, """SELECT f.id, f.rel_path FROM furry_pool_items i JOIN files f ON f.id = i.file_id
                         WHERE i.pool_id=%s ORDER BY i.page IS NULL, i.page, f.id""", (sh["pool_id"],))
    except Exception:
        rows = []
    finally:
        db.close()
    return {str(r["id"]): r["rel_path"] for r in rows if not is_service_path(r["rel_path"])}


def _not_found():
    return Response(_page("Посилання недійсне", "<div class='empty'>Посилання недійсне або його відкликали.</div>"),
                    status=404, content_type="text/html; charset=utf-8")


@bp.route("/s/<token>")
def share_page(token):
    sh = _share(token)
    if not sh:
        return _not_found()
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE share_links SET views=views+1 WHERE id=%s", (sh["id"],))
        db.commit()
    finally:
        db.close()
    name = sh["rel_path"] if sh["kind"] == "pool" else (sh["rel_path"].rsplit("/", 1)[-1] or "Архів")
    if sh["kind"] == "file":
        if not os.path.isfile(local_path(sh["rel_path"])):
            return _not_found()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        raw = f"/s/{token}/raw"
        if ext in IMG:
            media = f"<img class='one' src='{raw}' alt=''>"
        elif ext in VID:
            media = f"<video class='one' src='{raw}' controls autoplay loop playsinline></video>"
        elif ext in AUD:
            media = f"<audio src='{raw}' controls autoplay></audio>"
        else:
            media = "<div class='empty'>📄</div>"
        body = f"""<div class='bar'><div class='title'>{_e(name)}</div>
                   <a class='btn' href='{raw}?dl=1'>⬇ Завантажити</a></div>
                   <div class='viewer'>{media}</div>"""
        return Response(_page(name, body), content_type="text/html; charset=utf-8")
    if sh["kind"] == "folder" and not os.path.isdir(local_path(sh["rel_path"])):
        return _not_found()
    body = f"""<div class='bar'><div class='title' id='crumbs'>{_e(name)}</div></div>
               <div class='grid' id='grid'></div>
               <div id='lb' class='lb' onclick='if(event.target===this)closeLb()'>
                 <button class='nav prev' onclick='step(-1)'>‹</button><div id='lbm'></div>
                 <button class='nav next' onclick='step(1)'>›</button>
                 <div class='lbbar'><span id='lbn'></span><a class='btn' id='lbdl'>⬇ Завантажити</a>
                 <button class='btn' onclick='closeLb()'>✕</button></div></div>
               <script>{_FOLDER_JS.replace("__TOKEN__", token).replace("__ROOT__", _js(name))}</script>"""
    return Response(_page(name, body), content_type="text/html; charset=utf-8")


@bp.route("/s/<token>/list")
def share_list(token):
    sh = _share(token)
    if not sh or sh["kind"] not in ("folder", "pool"):
        return jsonify({"error": "not found"}), 404
    if sh["kind"] == "pool":
        files = [{"name": rel.rsplit("/", 1)[-1], "p": fid} for fid, rel in _pool_files(sh).items()]
        return jsonify({"path": "", "folders": [], "files": files})
    sub = flask_request.args.get("p", "")
    rel = _resolve(sh, sub)
    if rel is None:
        return jsonify({"error": "not found"}), 404
    try:
        entries = sorted(os.scandir(local_path(rel)), key=lambda e: e.name.lower())
    except OSError:
        return jsonify({"error": "not found"}), 404
    folders = [e.name for e in entries if e.is_dir() and not e.name.startswith(".")]
    base = _clean(sub) or ""
    files = [{"name": e.name, "size": e.stat().st_size, "p": f"{base}/{e.name}".strip("/")} for e in entries
             if e.is_file() and not e.name.startswith(".")]
    return jsonify({"path": _clean(sub) or "", "folders": folders, "files": files})


@bp.route("/s/<token>/raw")
def share_raw(token):
    sh = _share(token)
    if not sh:
        return jsonify({"error": "not found"}), 404
    rel = _resolve(sh, flask_request.args.get("p", ""))
    if rel is None:
        return jsonify({"error": "not found"}), 404
    path = local_path(rel)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    name = rel.rsplit("/", 1)[-1]
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    resp = send_file(path, mimetype=mime, conditional=True,
                     as_attachment=flask_request.args.get("dl") == "1", download_name=name)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@bp.route("/s/<token>/thumb")
def share_thumb(token):
    sh = _share(token)
    if not sh:
        return jsonify({"error": "not found"}), 404
    rel = _resolve(sh, flask_request.args.get("p", ""))
    if rel is None:
        return jsonify({"error": "not found"}), 404
    from blueprints.catalog import api_catalog_thumb
    return api_catalog_thumb(external=True, rel_path=rel)


def _e(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&#39;").replace('"', "&quot;"))


def _js(s: str) -> str:
    import json
    return json.dumps(str(s)).replace("</", "<\\/")


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{_e(title)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#0d0f14;color:#e6e8ee;font:14px/1.4 system-ui,sans-serif}}
.bar{{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid #232733;position:sticky;top:0;background:#0d0f14;z-index:2}}
.title{{flex:1;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.title a{{color:#a78bfa;text-decoration:none}}
.btn{{background:#1b1f2a;border:1px solid #2c3242;color:#e6e8ee;border-radius:8px;padding:7px 12px;text-decoration:none;cursor:pointer;font:inherit}}
.btn:hover{{background:#262b38}}
.viewer{{display:flex;justify-content:center;align-items:center;min-height:calc(100vh - 60px);padding:12px}}
.one{{max-width:100%;max-height:calc(100vh - 90px);border-radius:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;padding:14px}}
.tile{{background:#161a23;border-radius:8px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column}}
.tile .th{{aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:40px;background:#0f1218}}
.tile img{{width:100%;height:100%;object-fit:cover}}
.tile .nm{{font-size:11.5px;padding:5px 7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#aab}}
.empty{{text-align:center;color:#778;padding:60px 0;font-size:16px}}
.lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:9;align-items:center;justify-content:center}}
.lb.on{{display:flex}}#lbm img,#lbm video{{max-width:94vw;max-height:84vh;border-radius:6px}}
.lbbar{{position:fixed;bottom:0;left:0;right:0;display:flex;gap:10px;align-items:center;justify-content:center;padding:12px}}
.nav{{position:fixed;top:50%;transform:translateY(-50%);background:rgba(20,22,30,.7);border:none;color:#fff;font-size:34px;width:48px;height:64px;border-radius:8px;cursor:pointer}}
.prev{{left:10px}}.next{{right:10px}}
</style></head><body>{body}</body></html>"""


_FOLDER_JS = r"""
const T='__TOKEN__', ROOT=__ROOT__;
const IMG=['jpg','jpeg','png','gif','webp','bmp','avif'], VID=['mp4','webm','mov','m4v'];
let cur='', files=[], idx=0;
const ext=n=>(n.split('.').pop()||'').toLowerCase();
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const P=x=>encodeURIComponent(x.p);
async function load(p){
  cur=p||''; const r=await fetch(`/s/${T}/list?p=${encodeURIComponent(cur)}`).then(x=>x.json());
  if(r.error){document.getElementById('grid').innerHTML='<div class="empty">Не знайдено</div>';return;}
  const parts=cur?cur.split('/'):[]; let acc='';
  document.getElementById('crumbs').innerHTML=`<a href="#" onclick="load('');return false">${esc(ROOT)}</a>`+
    parts.map(x=>{acc=acc?acc+'/'+x:x;const a=acc;return ` / <a href="#" onclick='load(${JSON.stringify(a)});return false'>${esc(x)}</a>`}).join('');
  files=r.files;
  const f=r.folders.map(n=>`<div class="tile" onclick='load(${JSON.stringify(cur?cur+"/"+n:n)})'><div class="th">📁</div><div class="nm">${esc(n)}</div></div>`).join('');
  const g=r.files.map((x,i)=>{const e=ext(x.name);const th=(IMG.includes(e)||VID.includes(e))?`<img loading="lazy" src="/s/${T}/thumb?p=${P(x)}" onerror="this.replaceWith('📄')">`:'📄';
    return `<div class="tile" onclick="openLb(${i})"><div class="th">${th}</div><div class="nm">${esc(x.name)}</div></div>`}).join('');
  document.getElementById('grid').innerHTML=(f+g)||'<div class="empty">Тут порожньо</div>';
}
function openLb(i){
  idx=i; const x=files[i], e=ext(x.name), src=`/s/${T}/raw?p=${P(x)}`;
  document.getElementById('lbm').innerHTML=IMG.includes(e)?`<img src="${src}">`:VID.includes(e)?`<video src="${src}" controls autoplay loop playsinline></video>`:`<div class="empty">📄 ${esc(x.name)}</div>`;
  document.getElementById('lbn').textContent=`${i+1} / ${files.length} · ${x.name}`;
  document.getElementById('lbdl').href=src+'&dl=1';
  document.getElementById('lb').classList.add('on');
}
function closeLb(){document.getElementById('lb').classList.remove('on');document.getElementById('lbm').innerHTML='';}
function step(d){if(files.length)openLb((idx+d+files.length)%files.length);}
document.addEventListener('keydown',e=>{if(!document.getElementById('lb').classList.contains('on'))return;
  if(e.key==='Escape')closeLb();if(e.key==='ArrowLeft')step(-1);if(e.key==='ArrowRight')step(1);});
load('');
"""
