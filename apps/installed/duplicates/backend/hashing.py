"""
pHash — перцептивний хеш для пошуку схожих картинок і відео.

ВАЖЛИВО: алгоритм мусить збігатися з тим, яким рахує архіватор при
скачуванні (furry_archiver.compute_phash). Інакше файли, завантажені
вручну, не порівнювались би зі скачаними.
  * картинки — imagehash.phash по RGB, число з шістнадцяткового рядка;
    це три рядки, повторено дослівно;
  * відео — через videohash і ffmpeg; цю частину беремо з архіватора,
    щоб не тримати дві копії складної логіки (дублікати й так залежать
    від furry).
"""

import io

IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp"}
VIDEO_FORMATS = {"webm", "mp4"}


def compute_phash(data: bytes, ext: str):
    ext = (ext or "").lower()
    if ext in IMAGE_FORMATS:
        try:
            import imagehash
            from PIL import Image
            return int(str(imagehash.phash(Image.open(io.BytesIO(data)).convert("RGB"))), 16)
        except Exception:
            return None
    if ext in VIDEO_FORMATS:
        try:
            from furry_archiver import _compute_videohash
            return _compute_videohash(data, ext)
        except Exception:
            return None
    return None
