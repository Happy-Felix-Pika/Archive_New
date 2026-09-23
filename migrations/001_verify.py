#!/usr/bin/env python3
"""
Перевірка міграції 001_files_split.sql на РЕАЛЬНІЙ базі.

Запускати з кореня проєкту ПІСЛЯ виконання 001_files_split.sql:
    python migrations/001_verify.py

Нічого не змінює — тільки читає й рахує. Якщо в кінці написано
"ВСЕ ПЕРЕВІРКИ ПРОЙДЕНО" — можна йти далі.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_db  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def scalar(db, sql):
    with db.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return list(row.values())[0] if row else None


def main():
    db = get_db()

    print("\n1. Кількість рядків")
    n_dl = scalar(db, "SELECT COUNT(*) FROM downloads WHERE filename IS NOT NULL AND filename <> ''")
    n_f = scalar(db, "SELECT COUNT(*) FROM files")
    n_p = scalar(db, "SELECT COUNT(*) FROM furry_posts")
    n_ph_src = scalar(db, "SELECT COUNT(*) FROM downloads WHERE phash IS NOT NULL")
    n_ph = scalar(db, "SELECT COUNT(*) FROM file_phash")
    extra = n_f - n_dl
    check("files >= downloads", n_f >= n_dl, f"{n_f} vs {n_dl}")
    if extra > 0:
        print(f"    ℹ у files на {extra} рядків більше — це файли без автора "
              f"(додані напряму в ядро). Так і має бути.")
    check("furry_posts = downloads", n_p == n_dl, f"{n_p} vs {n_dl}")
    check("file_phash = downloads з phash", n_ph == n_ph_src, f"{n_ph} vs {n_ph_src}")

    print("\n2. Нічого не загублено")
    lost = scalar(db, """SELECT COUNT(*) FROM downloads d LEFT JOIN files f ON f.id = d.id
                         WHERE f.id IS NULL AND d.filename IS NOT NULL AND d.filename <> ''""")
    orph = scalar(db, "SELECT COUNT(*) FROM files f LEFT JOIN downloads d ON d.id = f.id WHERE d.id IS NULL")
    check("downloads без files", lost == 0, str(lost))
    print(f"  ℹ files без downloads: {orph} — не помилка, це файли поза furry-частиною")

    print("\n3. rel_path збігається з тим, що будує local_file_path() у коді")
    with db.cursor() as cur:
        cur.execute("""SELECT d.id, a.folder_name, d.subfolder, d.filename, f.rel_path
                       FROM downloads d
                       JOIN artists a ON a.id = d.artist_id
                       JOIN files f   ON f.id = d.id""")
        rows = cur.fetchall()
    bad = []
    for r in rows:
        base = r["filename"] if not r["subfolder"] else f"{r['subfolder']}/{r['filename']}"
        expected = base if r["folder_name"] == "_root" else f"{r['folder_name']}/{base}"
        if expected != r["rel_path"]:
            bad.append((r["id"], expected, r["rel_path"]))
    check("шляхи збігаються", not bad, f"розбіжностей: {len(bad)}")
    for b in bad[:10]:
        print(f"      id={b[0]}\n        очікувалось: {b[1]}\n        у базі:      {b[2]}")

    print("\n4. Старі зв'язки цілі")
    for table, col in [("download_tags", "download_id"), ("user_favorites", "download_id"),
                       ("duplicate_scan_results", "download_id_a"),
                       ("duplicate_ignore_pairs", "download_id_a")]:
        broken = scalar(db, f"""SELECT COUNT(*) FROM {table} x
                                LEFT JOIN files f ON f.id = x.{col} WHERE f.id IS NULL""")
        check(f"{table}", broken == 0, f"без files: {broken}")

    print("\n5. Дублікати шляхів (файли, що вказують на те саме місце)")
    dups = scalar(db, """SELECT COUNT(*) FROM (
                             SELECT storage_id, path_hash FROM files
                             GROUP BY storage_id, path_hash HAVING COUNT(*) > 1) x""")
    if dups:
        print(f"  ℹ шляхів-дублікатів: {dups} — не помилка зараз, але їх треба буде "
              f"розрулити перед кроком 4. Подивитись які:")
        print("     SELECT rel_path, COUNT(*) c FROM files GROUP BY storage_id, path_hash "
              "HAVING c > 1 ORDER BY c DESC;")
    else:
        check("дублікатів шляхів немає", True, "0")

    print("\n6. AUTO_INCREMENT files не перетинається з downloads")
    nxt = scalar(db, """SELECT AUTO_INCREMENT FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'files'""")
    mx = scalar(db, "SELECT IFNULL(MAX(id), 0) FROM downloads")
    check("наступний id вільний", (nxt or 0) > (mx or 0), f"next={nxt}, max(downloads.id)={mx}")

    db.close()

    print()
    if failures:
        print(f"✗ ПРОВАЛЕНО ПЕРЕВІРОК: {len(failures)}")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("✓ ВСЕ ПЕРЕВІРКИ ПРОЙДЕНО")


if __name__ == "__main__":
    main()
