"""
Ядро: єдиний інтерфейс до сховища.

Досі кожне місце, якому треба було прочитати чи видалити файл, саме
ходило в os.path напряму. Тут це зведено в один клас. Сховище ядра —
локальна тека (Nextcloud — окремий застосунок).

Усі шляхи — ВІДНОСНІ до кореня сховища, з прямими слешами: рівно те,
що лежить у files.rel_path. Про теку автора, підпапки pool і будь-яку
іншу структуру цей рівень нічого не знає — для нього це просто шлях.

Застосунки працюють саме через цей інтерфейс, а не через os.path напряму.
"""

import os
import shutil

from core.storage import get_local_root, get_storage_mode


# ── Службові файли ──────────────────────────────────────────────────
#
#  Якщо тека сховища — та сама /data, де архів тримає свої файли
#  (ключ сесій, налаштування з паролями, кеш, черга перевірки), вони не
#  мають бути видні ні в каталозі, ні в «Артах», ні підключеним архівам.
#  Прихованим вважається будь-що з крапкою на початку (.secret_key, .trash,
#  .cache…) на будь-якому рівні і службові назви в корені.

SERVICE_ROOT_NAMES = {
    "archiver_config.json", "archiver_config.json.tmp",
    "e621_tag_categories.json", "e621_tag_categories.json.tmp",
    "pending_review", "cache", "temp_downloads", "tmp_hashes", "VERSION.json",
}


def is_service_path(rel_path: str) -> bool:
    parts = [p for p in (rel_path or "").replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return False
    if any(p.startswith(".") for p in parts):
        return True
    return parts[0] in SERVICE_ROOT_NAMES


def top_folders() -> list:
    """Теки верхнього рівня сховища (без службових) — для списків доступу."""
    try:
        return [it["name"] for it in get_storage().list_dir("")
                if it["is_dir"] and not is_service_path(it["name"])]
    except Exception:
        return []


class StorageError(Exception):
    """Проблема зі сховищем (немає налаштувань, недоступне тощо)."""


class BaseStorage:
    """Спільний інтерфейс. Не використовується напряму."""

    def read(self, rel_path: str) -> bytes | None:
        raise NotImplementedError

    def write(self, rel_path: str, data: bytes) -> bool:
        raise NotImplementedError

    def upload(self, local_file: str, rel_path: str) -> bool:
        raise NotImplementedError

    def download(self, rel_path: str, local_file: str) -> bool:
        raise NotImplementedError

    def move(self, src_rel: str, dst_rel: str) -> bool:
        raise NotImplementedError

    def delete(self, rel_path: str) -> bool:
        raise NotImplementedError

    def exists(self, rel_path: str) -> bool:
        raise NotImplementedError

    def size(self, rel_path: str) -> int | None:
        raise NotImplementedError

    def list_dir(self, rel_path: str = "") -> list:
        """[{'name':.., 'is_dir':bool, 'size':int|None, 'mtime':int|None}, ...]"""
        raise NotImplementedError

    def mkdir(self, rel_path: str) -> bool:
        """Створити теку (разом з проміжними). Уже існує — теж True."""
        raise NotImplementedError

    def _remove_dir(self, rel_path: str) -> bool:
        raise NotImplementedError

    def walk_files(self, rel_path: str) -> list:
        """Усі файли всередині теки, з підтеками — відносні шляхи."""
        out = []
        for it in self.list_dir(rel_path):
            child = f"{rel_path}/{it['name']}" if rel_path else it["name"]
            if it["is_dir"]:
                out.extend(self.walk_files(child))
            else:
                out.append(child)
        return out

    def remove_empty_dir(self, rel_path: str) -> bool:
        """
        Прибрати теку, лише якщо в ній (і в підтеках) немає жодного файлу.
        Захист від випадкового стирання: файли мають іти в кошик окремо,
        через ядро, а сюди доходить уже порожня тека.
        """
        if not rel_path or self.walk_files(rel_path):
            return False
        return self._remove_dir(rel_path)


class LocalStorage(BaseStorage):
    """Файли просто лежать у теці на диску."""

    def __init__(self, root: str):
        if not root:
            raise StorageError("Не вказано шлях до локального сховища")
        self.root = root

    def _abs(self, rel_path: str) -> str:
        # rel_path завжди з прямими слешами — розбираємо вручну, інакше
        # на Windows вийде мішанина роздільників
        parts = [p for p in rel_path.split("/") if p and p not in (".", "..")]
        return os.path.join(self.root, *parts)

    def read(self, rel_path):
        p = self._abs(rel_path)
        if not os.path.isfile(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def write(self, rel_path, data):
        p = self._abs(rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return True

    def upload(self, local_file, rel_path):
        p = self._abs(rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        shutil.copy2(local_file, p)
        return True

    def download(self, rel_path, local_file):
        p = self._abs(rel_path)
        if not os.path.isfile(p):
            return False
        os.makedirs(os.path.dirname(local_file), exist_ok=True)
        shutil.copy2(p, local_file)
        return True

    def move(self, src_rel, dst_rel):
        src, dst = self._abs(src_rel), self._abs(dst_rel)
        if not os.path.exists(src):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return True

    def delete(self, rel_path):
        p = self._abs(rel_path)
        if not os.path.isfile(p):
            return False
        os.remove(p)
        return True

    def exists(self, rel_path):
        return os.path.exists(self._abs(rel_path))

    def size(self, rel_path):
        p = self._abs(rel_path)
        return os.path.getsize(p) if os.path.isfile(p) else None

    def mkdir(self, rel_path):
        os.makedirs(self._abs(rel_path), exist_ok=True)
        return True

    def _remove_dir(self, rel_path):
        p = self._abs(rel_path)
        if not os.path.isdir(p) or os.path.normpath(p) == os.path.normpath(self.root):
            return False
        shutil.rmtree(p)          # усередині лише порожні теки — перевірено вище
        return True

    def list_dir(self, rel_path=""):
        p = self._abs(rel_path) if rel_path else self.root
        if not os.path.isdir(p):
            return []
        out = []
        for name in sorted(os.listdir(p)):
            full = os.path.join(p, name)
            is_dir = os.path.isdir(full)
            out.append({
                "name":  name,
                "is_dir": is_dir,
                "size":  None if is_dir else os.path.getsize(full),
                "mtime": None if is_dir else int(os.path.getmtime(full)),
            })
        return out


def get_storage() -> BaseStorage:
    """
    Сховище за поточними налаштуваннями користувача.
    Кидає StorageError, якщо нічого не налаштовано — щоб помилка була
    зрозумілою, а не 'NoneType has no attribute'.
    """
    mode = get_storage_mode()
    if mode == "local":
        return LocalStorage(get_local_root())
    raise StorageError("Сховище не налаштовано: вкажи локальну теку (Конфіг → Сховище файлів)")
