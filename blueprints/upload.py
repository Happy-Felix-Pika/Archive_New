"""
Завантаження файлу в архів.

Ядро приймає БУДЬ-ЯКИЙ файл — фото, документ, музику — і кладе його в
указану теку (або в корінь). Автора тут немає: ядро про авторів не знає.

Застосунки можуть вплинути на завантаження, не чіпаючи ядро:
  * фільтр  'file.upload'  — змінити теку або відмовити
                              (furry: «вказано автора → тека @автор»);
  * подія   'file.created'  — дописати своє вже після збереження
                              (furry: прив'язка до автора, дублікати: pHash).
"""

import re

from flask import Blueprint, jsonify, request as flask_request

from core import events, media
from core.auth import moderator_required
from core import modlog
from core.db import get_db
from core.storage_api import get_storage

bp = Blueprint("upload", __name__)

MAX_NAME = 80


def _safe_name(original: str) -> tuple:
    """(основа імені, розширення). Прибираємо тільки те, що ламає шлях:
    роздільники тек, керуючі символи й заборонене у Windows. Кирилиця
    та будь-які інші літери лишаються."""
    base, _, ext = original.rpartition(".")
    if not base:                      # ім'я без крапки
        base, ext = original, ""
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(". ")[:MAX_NAME] or "upload"
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:10]
    return base, ext


def _safe_folder(folder: str) -> str | None:
    """Відносна тека всередині сховища. None — якщо шлях небезпечний
    (вихід через '..' чи спроба покласти щось у кошик)."""
    parts = [p.strip() for p in (folder or "").replace("\\", "/").split("/") if p.strip()]
    if any(p in (".", "..") for p in parts):
        return None
    if parts and parts[0] == ".trash":
        return None
    parts = [re.sub(r'[:*?"<>|\x00-\x1f]', "_", p)[:120] for p in parts]
    return "/".join(parts)


@bp.route("/api/upload", methods=["POST"])
@moderator_required
def api_upload_file():
    f = flask_request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Файл не передано"}), 400
    content = f.read()
    if not content:
        return jsonify({"error": "Порожній файл"}), 400

    base, ext = _safe_name(f.filename)
    md5_hash = media.md5(content)
    width, height = media.dimensions(content, ext)
    tags_str = (flask_request.form.get("tags") or "").strip()
    form = {k: v for k, v in flask_request.form.items()}

    db = get_db()
    try:
        # Застосунки можуть змінити теку або відмовити. Ядро лише каже,
        # що завантажується і куди просили покласти.
        plan = {"folder": (flask_request.form.get("folder") or "").strip(), "error": None}
        plan = events.apply_filters("file.upload", plan, form=form, filename=f.filename,
                                    ext=ext, md5=md5_hash, db=db) or plan
        if plan.get("error"):
            return jsonify({"error": plan["error"]}), plan.get("status", 400)

        folder = _safe_folder(plan.get("folder"))
        if folder is None:
            return jsonify({"error": "Недопустима тека"}), 400

        # Точна копія в тій самій теці — не дублюємо. (Для furry тека —
        # це тека автора, тож поведінка та сама, що й раніше: "уже є в
        # цього автора".)
        with db.cursor() as cur:
            cur.execute("SELECT rel_path FROM files WHERE md5_hash=%s", (md5_hash,))
            for r in cur.fetchall():
                parent = r["rel_path"].rsplit("/", 1)[0] if "/" in r["rel_path"] else ""
                if parent == folder:
                    return jsonify({"error": "Такий файл (точна копія) уже є в цій теці"}), 409

        filename = f"{base}_{md5_hash[:8]}.{ext}" if ext else f"{base}_{md5_hash[:8]}"
        rel_path = f"{folder}/{filename}" if folder else filename

        if not get_storage().write(rel_path, content):
            return jsonify({"error": "Не вдалося зберегти файл у сховищі"}), 500

        with db.cursor() as cur:
            cur.execute("""INSERT INTO files (storage_id, rel_path, name, ext, size_bytes,
                                              width, height, md5_hash, created_at)
                           VALUES ('default',%s,%s,%s,%s,%s,%s,%s,NOW())""",
                        (rel_path, filename, ext, len(content), width, height, md5_hash))
            file_id = cur.lastrowid
            for tag in [t.strip().lower() for t in tags_str.split(",") if t.strip()]:
                cur.execute("INSERT IGNORE INTO tags (name, category) VALUES (%s,'other')", (tag,))
                cur.execute("SELECT id FROM tags WHERE name=%s", (tag,))
                trow = cur.fetchone()
                if trow:
                    cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                                (file_id, trow["id"]))
        db.commit()

        # Далі — справа застосунків: прив'язати автора, порахувати хеш...
        # Файл уже збережено; якщо хтось із них упаде, сам файл лишиться.
        events.emit("file.created", {"file_id": file_id, "rel_path": rel_path, "plan": plan,
                                     "content": content, "ext": ext, "tags": tags_str, "db": db})
        db.commit()

        try:
            from blueprints.tags import _invalidate_tags_cache   # теги — ядро
            _invalidate_tags_cache()
        except Exception:
            pass

        modlog.record("file.upload", f"«{filename}» → {folder or 'корінь'}",
                      {"file_id": file_id, "rel_path": rel_path})

        # download_id — стара назва поля, фронтенд на неї розраховує
        return jsonify({"ok": True, "id": file_id, "download_id": file_id,
                        "filename": filename, "rel_path": rel_path})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()
