"""
Ядро: єдиний інтерфейс до сховища.

Досі кожне місце, якому треба було прочитати чи видалити файл, саме
питало "локально чи Nextcloud?" і далі йшло двома різними шляхами —
близько десяти таких розгалужень по всьому коду, кожне зі своїми
дрібними відмінностями. Тут вони зведені в один клас.

Усі шляхи — ВІДНОСНІ до кореня сховища, з прямими слешами: рівно те,
що лежить у files.rel_path. Про теку автора, підпапки pool і будь-яку
іншу структуру цей рівень нічого не знає — для нього це просто шлях.

Застосунки працюватимуть саме через цей інтерфейс, а не через os.path
чи webdav напряму.
"""

import os
import shutil

from core.auth import get_user_nc_config
from core.storage import get_local_root, get_storage_mode


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


class NextcloudStorage(BaseStorage):
    """Файли лежать у Nextcloud, доступ по WebDAV."""

    def __init__(self, webdav_url: str, login: str, password: str, base_folder: str = ""):
        if not webdav_url:
            raise StorageError("Не налаштовано Nextcloud")
        self.base = (base_folder or "").strip("/")
        from webdav3.client import Client
        self._client = Client({
            "webdav_hostname": webdav_url,
            "webdav_login":    login,
            "webdav_password": password,
            "disable_check":   True,
        })

    def _remote(self, rel_path: str) -> str:
        rel = rel_path.strip("/")
        return f"{self.base}/{rel}" if self.base else rel

    def _ensure_dir(self, remote_path: str):
        """
        Створює теку для файлу, разом з усіма проміжними.
        WebDAV не створює вкладені теки автоматично: завантаження в
        неіснуючу теку (напр. до нового автора) просто провалиться.
        """
        parts = remote_path.split("/")[:-1]
        path = ""
        for p in parts:
            if not p:
                continue
            path = f"{path}/{p}" if path else p
            try:
                self._client.mkdir(path)
            except Exception:
                pass   # вже існує — нормально

    def read(self, rel_path):
        import io
        buf = io.BytesIO()
        try:
            self._client.resource(self._remote(rel_path)).write_to(buf)
            return buf.getvalue()
        except Exception:
            return None

    def write(self, rel_path, data):
        import os as _os
        import tempfile
        remote = self._remote(rel_path)
        self._ensure_dir(remote)
        # Через тимчасовий файл: upload_sync працює надійніше за потокову
        # передачу і це той самий шлях, яким користувався старий код.
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.write(data); tmp.close()
            self._client.upload_sync(remote_path=remote, local_path=tmp.name)
            return True
        except Exception:
            return False
        finally:
            try: _os.remove(tmp.name)
            except Exception: pass

    def upload(self, local_file, rel_path):
        remote = self._remote(rel_path)
        self._ensure_dir(remote)
        try:
            self._client.upload_sync(remote_path=remote, local_path=local_file)
            return True
        except Exception:
            return False

    def download(self, rel_path, local_file):
        try:
            self._client.download_sync(remote_path=self._remote(rel_path), local_path=local_file)
            return True
        except Exception:
            return False

    def move(self, src_rel, dst_rel):
        dst = self._remote(dst_rel)
        self._ensure_dir(dst)
        try:
            self._client.move(remote_path_from=self._remote(src_rel), remote_path_to=dst)
            return True
        except Exception:
            return False

    def delete(self, rel_path):
        try:
            self._client.clean(self._remote(rel_path))
            return True
        except Exception:
            return False

    def exists(self, rel_path):
        try:
            return bool(self._client.check(self._remote(rel_path)))
        except Exception:
            return False

    def mkdir(self, rel_path):
        # _ensure_dir створює теки для ФАЙЛУ — даємо йому шлях «файлу» всередині
        self._ensure_dir(self._remote(rel_path).rstrip("/") + "/_")
        return self.exists(rel_path)

    def _remove_dir(self, rel_path):
        remote = self._remote(rel_path)
        if not remote.strip("/") or remote.strip("/") == self.base:
            return False
        try:
            self._client.clean(remote)
            return True
        except Exception:
            return False

    def size(self, rel_path):
        try:
            return int(self._client.info(self._remote(rel_path)).get("size") or 0)
        except Exception:
            return None

    def list_dir(self, rel_path=""):
        remote = self._remote(rel_path) or "/"
        try:
            # get_info дає розмір і час зміни; старі версії клієнта його не
            # приймають — тоді лишаються самі імена
            items = self._client.list(remote, get_info=True)
        except TypeError:
            items = self._client.list(remote)
        except Exception:
            return []

        out = []
        here = remote.strip("/").lower()
        for item in items:
            info = item if isinstance(item, dict) else {"path": item}
            raw  = (info.get("path") or "").strip("/")
            is_dir = (info.get("isdir") if isinstance(item, dict) and "isdir" in info
                      else str(item).endswith("/"))
            name = raw.rstrip("/").split("/")[-1]
            if not name or raw.lower() == here:
                continue          # сама тека, а не її вміст
            mtime = None
            mod = info.get("modified")
            if mod:
                try:
                    from email.utils import parsedate_to_datetime
                    mtime = int(parsedate_to_datetime(mod).timestamp())
                except Exception:
                    mtime = None
            try:
                size = int(info.get("size") or 0) or None
            except (TypeError, ValueError):
                size = None
            out.append({"name": name, "is_dir": bool(is_dir),
                        "size": None if is_dir else size, "mtime": mtime})
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
    if mode == "nextcloud":
        nc = get_user_nc_config()
        return NextcloudStorage(
            webdav_url=nc.get("webdav_url", ""),
            login=nc.get("username", ""),
            password=nc.get("password", ""),
            base_folder=nc.get("base_folder", ""),
        )
    raise StorageError("Сховище не налаштовано: вкажи локальну теку або Nextcloud")
