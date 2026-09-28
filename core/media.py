"""
Ядро: що можна дізнатись про сам файл — без жодних застосунків.

Раніше розміри рахувались функцією з furry_archiver.py, і без архіватора
ядро не могло прийняти навіть звичайну фотографію. Тепер це власні засоби
ядра: Pillow для зображень, ffprobe для відео. Якщо визначити не вдалося —
(None, None), а не помилка: невідомі розміри не причина відмовити у збереженні.
"""

import hashlib
import io
import json
import os
import subprocess
import tempfile

IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp"}
VIDEO_FORMATS = {"webm", "mp4"}


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def dimensions(data: bytes, ext: str) -> tuple:
    """(width, height) у пікселях, або (None, None)."""
    ext = (ext or "").lower()

    if ext in IMAGE_FORMATS:
        try:
            from PIL import Image
            return Image.open(io.BytesIO(data)).size
        except Exception:
            return (None, None)

    if ext in VIDEO_FORMATS:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                f.write(data)
                tmp = f.name
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", tmp],
                capture_output=True, text=True, timeout=15)
            streams = json.loads(r.stdout or "{}").get("streams", [])
            if streams:
                return streams[0].get("width"), streams[0].get("height")
        except Exception as e:
            print(f"  ⚠ ffprobe: {e}")
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    return (None, None)
