# Как писать приложения для Archive

Документ для разработчика (человека или нейросети), который делает приложение
для этого архива. Всё описанное проверено на работающей системе.

---

## 1. Что такое ядро и что такое приложение

**Ядро** — универсальный файловый архив. Оно умеет: хранить файлы, показывать
каталог, загружать и отдавать файлы, искать по имени, вести пользователей и
права, запускать фоновые задачи, устанавливать приложения. Ядро **ничего не
знает** про авторов, внешние сайты, дубликаты, чат — всё это приложения.

**Приложение** — папка в `apps/installed/<id>/`. Оно добавляет свои эндпоинты,
свои таблицы, свои пункты меню и свои фоновые задачи. Приложение можно
выключить: его вкладки исчезнут из интерфейса, эндпоинты начнут отдавать 404,
данные останутся в базе.

Технологии: Python 3.12, Flask, MySQL/MariaDB, чистый JavaScript без сборщика.

---

## 2. Структура приложения

```
apps/installed/myapp/
├── manifest.json          обязательный
├── backend/
│   └── __init__.py        обязательный, точка входа с функцией register(core)
├── migrations/
│   ├── 001_init.sql       необязательно
│   └── 002_add_field.sql
├── static/
│   ├── myapp.js
│   └── myapp.css
├── locales/
│   └── en.json            необязательно: переводы надписей приложения
└── README.md
```

Имя папки **обязано совпадать** с полем `id` в манифесте, иначе приложение
получит статус `incompatible`.

---

## 3. manifest.json

```json
{
  "id": "myapp",
  "name": "Моё приложение",
  "version": "1.0.0",
  "description": "Что оно делает",
  "author": "Имя",
  "homepage": "https://...",
  "icon": "📦",

  "core_api_version": 1,
  "min_core_version": "1.0.0",
  "dependencies": {"furry": "1.0.0"},
  "permissions": ["database", "storage", "files", "users", "jobs"],

  "backend": "backend",
  "table_prefix": "myapp_",
  "legacy_tables": [],

  "menu": [
    {"id": "main",
     "label": {"uk": "Моє", "ru": "Моё", "en": "Mine"},
     "icon": "📦",
     "order": 50,
     "page": "main",
     "admin_only": false}
  ],

  "pages": [
    {"id": "main", "js": "static/myapp.js", "css": "static/myapp.css", "init": "myappInit"}
  ],

  "settings_schema": [
    {"key": "limit", "type": "int", "default": 50, "label": {"uk": "Ліміт"}}
  ]
}
```

### Обязательные поля

| Поле | Смысл |
|---|---|
| `id` | только `a-z0-9_-`, совпадает с именем папки |
| `name` | отображаемое название |
| `version` | своя версия приложения |
| `core_api_version` | целое число. Сейчас **1**. Другое значение → приложение не загрузится |
| `min_core_version` | минимальная версия ядра, сейчас `"1.0.0"` |

### Остальные поля

- **`dependencies`** — `{"id_другого_приложения": "минимальная_версия"}`. Ядро
  само расставит порядок загрузки и не даст выключить то, от чего зависят.
- **`permissions`** — декларация. Ядро не изолирует приложения по-настоящему
  (см. раздел 11), но поле обязательно заполнять честно.
- **`table_prefix`** — префикс имён таблиц. По умолчанию `<id>_`.
- **`legacy_tables`** — список таблиц с историческими именами вне префикса.
  Нужен только приложениям, которые переехали из ядра.
- **`menu`** — пункты меню. `order` определяет позицию. `admin_only: true` —
  пункт виден только администратору.
- **`pages`** — страницы и их фронтенд-файлы.
- **`settings_schema`** — описание настроек. Пока только документация;
  автоматической формы ядро не рисует.
- **`config`** — `{"js": "static/myapp-config.js"}`, своя секция на странице
  настроек (см. раздел 7б).
- **`scripts`** — файлы, которые грузятся сразу при старте: в них приложение
  регистрирует свои кнопки в зонах ядра (см. раздел 7е).

### Два способа задать страницу в пункте меню

- **`"page": "main"`** — обычный случай. Ядро само создаст пустой контейнер и
  вызовет функцию из `pages[].init`, передав в неё корневой элемент.
- **`"page_id": "live"`** — страница уже есть в `dashboard.html`. Так сделано у
  приложений, которые переехали из ядра. Для новых приложений **не используйте**.

---

## 4. Точка входа

`backend/__init__.py` обязан определять функцию `register(core)`:

```python
from flask import Blueprint, jsonify, request

from . import views          # отдельные модули приложения — обычные относительные импорты

bp = Blueprint("myapp", __name__)
core = None                  # сохраняем объект ядра


@bp.route("/items", methods=["GET"])
def items():
    db = core.db
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id, title FROM myapp_items ORDER BY id DESC")
            return jsonify(cur.fetchall())
    finally:
        db.close()


def register(core_api):
    global core
    core = core_api
    core.api.add_routes(bp)
```

`backend/` подключается **как пакет**, поэтому относительные импорты
(`from . import views`) работают.

Если `register()` выбросит исключение — приложение получит статус `error` с
текстом ошибки, но **сервер не упадёт** и остальные приложения загрузятся.

---

## 5. Объект core — всё, что даёт ядро

**Главное правило: приложение НЕ импортирует ничего из `core.*` напрямую.**
Всё приходит через объект `core` в `register()`. Пока правило соблюдается,
внутренности ядра можно менять, не ломая приложения.

### core.api — эндпоинты

```python
core.api.add_routes(bp)                  # → /api/apps/myapp/...
core.api.add_routes(bp, at_root=True)    # адреса как написаны в blueprint
core.api.url_prefix                      # "/api/apps/myapp"
```

`at_root=True` — только для приложений, переехавших из ядра и обязанных
сохранить старые адреса. Новые приложения используют обычный вариант.

### core.db — база данных

```python
db = core.db                 # новое соединение
try:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM myapp_items WHERE id=%s", (item_id,))
        row = cur.fetchone()          # dict, а не кортеж
    db.commit()                        # обязателен для INSERT/UPDATE/DELETE
finally:
    db.close()                         # обязательно
```

Курсоры возвращают словари. Автокоммита нет — про `db.commit()` легко забыть.
Соединение обязательно закрывать.

### core.storage — файлы

```python
st = core.storage
st.read(rel_path)                # bytes или None
st.write(rel_path, data)         # папки создаются сами
st.upload(local_file, rel_path)
st.download(rel_path, local_file)
st.move(src_rel, dst_rel)
st.delete(rel_path)              # False, если файла не было
st.exists(rel_path)
st.size(rel_path)                # байты или None
st.list_dir(rel_path)            # [{"name","is_dir","size","mtime"}, ...]
```

Пути **относительные от корня хранилища**, всегда через прямой слеш:
`@author/Название серии/file.png`. Локальное это хранилище или Nextcloud —
приложению знать не нужно. Выйти за пределы корня через `..` нельзя.

### core.files — файлы в базе

```python
core.files.get_file(file_id)            # строка из таблицы files
core.files.local_path(rel_path)         # абсолютный путь (только локальное хранилище)
core.files.file_local_path(file_id)
```

### core.jobs — фоновые задачи

```python
def sync(job, params):
    job.progress(10, "начинаю")
    for i, item in enumerate(items):
        if job.cancelled():             # длинные задачи обязаны проверять
            return {"stopped_at": i}
        job.progress(int(i / len(items) * 100), f"обработано {i}")
    return {"files": 42}                 # уйдёт в поле result

core.jobs.register("sync", sync)                       # тип станет "myapp.sync"
job_id = core.jobs.create("sync", {"a": 1}, user_id=5)
core.jobs.list("sync", limit=20)
```

Тип задачи автоматически получает префикс приложения, поэтому два приложения
не могут перехватить задачи друг друга. Очередь обрабатывается одним рабочим
потоком. Отмена — это **просьба**: задачу никто не убивает силой, она должна
сама проверять `job.cancelled()`.

### core.events — события

```python
def on_file_created(payload):
    file_id = payload["file_id"]

core.events.on("file.created", on_file_created)
core.events.emit("myapp.something", {"x": 1})     # своё событие
```

Вызовы синхронные. Падение одного подписчика не ломает остальных и не ломает
того, кто событие отправил.

**Какие события ядро отправляет сейчас:**

| Событие | Когда | Данные |
|---|---|---|
| `file.created` | файл загружен | `file_id`, `rel_path`, `plan`, `content` (байты), `ext`, `tags`, `db` |
| `file.deleted` | файл ушёл в корзину | `file_id`, `trash_id` |
| `file.restored` | файл вернули из корзины | `file_id`, `snapshot`, `db` |

`file.updated`, `user.created`, `job.completed` зарезервированы, но пока не
отправляются — не рассчитывайте на них.

### core.events — фильтры (дописать свои поля в ответ ядра)

Событие говорит «что-то случилось». Фильтр говорит «вот данные — дополни их».
Так приложение добавляет свои поля в ответ ядра, а ядро про эти поля ничего
не знает.

```python
def add_my_fields(data, file_id, db, **_):
    with db.cursor() as cur:
        cur.execute("SELECT note FROM myapp_notes WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
    if row:
        data["my_note"] = row["note"]
    return data            # обязательно вернуть данные

core.events.add_filter("file.meta", add_my_fields)
```

Фильтры ядра:

| Фильтр | Где | Контекст |
|---|---|---|
| `file.meta` | карточка файла `/api/gallery/meta/<id>` | `file_id`, `db` |
| `file.trash` | перед отправкой файла в корзину — сохранить свои данные | `file_id`, `db` |
| `file.upload` | перед сохранением загружаемого файла — изменить папку или отказать | `form`, `filename`, `ext`, `md5`, `db` |

Правила:

- Фильтр **обязан вернуть** данные. Вернули `None` — данные идут дальше без изменений.
- Используйте переданный `db`, не открывайте своё соединение и **не закрывайте** чужое.
- Всегда принимайте `**_` в конце: ядро может начать передавать новые поля контекста.
- Если фильтр упадёт, данные пойдут дальше без изменений — сломанное приложение
  не ломает ответ ядра. Но ошибку будет видно в консоли.
- Файл может не относиться к вашему приложению — тогда просто верните данные как есть.

Именно так приложение furry дописывает в карточку автора, источник, ссылку на
пост и номер страницы серии.
Имена `file.updated`, `file.deleted`, `user.created`, `job.completed`
зарезервированы, но пока не отправляются — не рассчитывайте на них.

### core.services — API для інших застосунків

Застосунок не імпортує код іншого застосунку. Якщо одному треба
скористатись можливостями іншого — той оголошує сервіс, а цей бере його
через ядро.

```python
# застосунок online
core.services.provide("online.sources", registry)

# застосунок e621 — у маніфесті "dependencies": {"online": "1.0.0"}
registry = core.services.get("online.sources")
registry.register(build(registry.base))
```

Правила:

- Хто бере сервіс, **обов'язково** вказує того, хто його дає, у
  `dependencies`. Тоді ядро завантажить їх у правильному порядку, а якщо
  постачальника вимкнули — залежний застосунок отримає статус «несумісний»
  замість незрозумілої помилки.
- `get()` для відсутнього сервісу кидає помилку з підказкою про
  залежності. `has()` — перевірити, не падаючи.
- Базові класи теж передаються через сервіс (`registry.base`), а не
  імпортом. Тому джерело e621 описане функцією `build(LiveSourceBase)`,
  яка повертає клас.

Живий приклад — застосунки `online`, `e621` і `inkbunny`: «Онлайн» дає
реєстр джерел, а кожне джерело — окремий застосунок, який у нього
реєструється. Нове джерело (Weasyl, FurAffinity) — це новий застосунок,
у самому «Онлайн» нічого міняти не треба.

### core.settings — настройки приложения

```python
core.settings.get("limit", 50)
core.settings.set("limit", 100)
core.settings.all()
```

Хранятся в таблице `app_settings`, у каждого приложения свой угол. Значения
проходят через JSON, поэтому списки и словари тоже можно.

### core.current_user — пользователь

```python
user = core.current_user()       # dict: id, email, display_name, is_admin
uid  = core.current_user_id()
```

Работает только внутри обработки HTTP-запроса.

---

## 6. Миграции

Файлы `migrations/*.sql` выполняются по порядку имён, один раз. Выполненные
записываются в `app_migrations`.

```sql
CREATE TABLE IF NOT EXISTS myapp_items (
    id      BIGINT AUTO_INCREMENT PRIMARY KEY,
    file_id BIGINT,
    title   VARCHAR(255),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### Правила

1. **Все таблицы — с префиксом из манифеста.** Загрузчик проверяет имена в
   `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`. Таблица вне префикса → приложение
   не загрузится. Исключение — имена, перечисленные в `legacy_tables`.
2. Всегда `IF NOT EXISTS` — миграция может выполняться на базе, где таблица уже есть.
3. Внешние ключи на таблицы ядра (`files`, `users`) — можно и нужно:
   `ON DELETE CASCADE` избавит от мусора.
4. Миграции **не откатываются**. Удаление приложения таблицы не трогает.
5. Менять уже применённый файл бессмысленно — он не выполнится повторно.
   Создавайте новый.

---

## 7. Фронтенд

```javascript
// static/myapp.js — ядро загрузит файл и вызовет эту функцию
async function myappInit(root) {
  root.innerHTML = `
    <div class="card">
      <div style="font-weight:600;margin-bottom:8px">Мои записи</div>
      <div id="myapp-list"></div>
    </div>`;
  const items = await fetch('/api/apps/myapp/items', {credentials: 'include'})
                      .then(r => r.json());
  document.getElementById('myapp-list').innerHTML =
    items.map(i => `<div>${i.title}</div>`).join('');
}
```

- Имя функции указывается в `pages[].init`, функция должна быть **глобальной**.
- `root` — пустой `div`, внутри которого приложение рисует что хочет.
- Файлы отдаются по адресу `/apps/myapp/static/myapp.js`, выйти за пределы
  папки приложения нельзя.
- Доступны стили ядра: `card`, `btn`, `btn-sm`, `btn-ghost`, `input`, `muted`,
  и переменные `var(--green)`, `var(--red)`, `var(--muted)`, `var(--border)`,
  `var(--mono)`.
- Все запросы — с `credentials: 'include'`, иначе не будет сессии.
- Сборщика нет. Пишите обычный JavaScript, без импортов и JSX.

---

## 7б. Своя секция на странице настроек

Приложение может добавить свой блок в «Конфіг». Выключили приложение — блок
исчезает вместе с ним, а сохранённые значения остаются в файле конфигурации.

В манифесте:

```json
"config": {"js": "static/myapp-config.js"}
```

В `static/myapp-config.js`:

```javascript
registerAppConfig('myapp', {

  // HTML своей секции. Ядро вставит её в общую форму настроек.
  render() {
    return `
      <div class="section-header">// МОЁ ПРИЛОЖЕНИЕ</div>
      <div class="field">
        <label>Лимит</label>
        <input type="number" id="cfg-myapp-limit">
      </div>`;
  },

  // Заполнить поля из конфига (приходит весь объект настроек)
  load(cfg) {
    const el = document.getElementById('cfg-myapp-limit');
    if (el) el.value = (cfg.myapp || {}).limit ?? 50;
  },

  // Что отправить на сервер при сохранении — сливается с остальным конфигом
  collect() {
    return { myapp: { limit: parseInt(document.getElementById('cfg-myapp-limit')?.value) || 0 } };
  },
});
```

Правила:

- Все три метода необязательны, но без `render()` секции не будет видно.
- В `load()` и `collect()` **обязательно** проверяйте элемент на существование:
  метод может быть вызван, когда секция ещё не нарисована.
- Секреты сервер наружу не отдаёт — вместо значения приходит маска из точек.
  Её наличие означает «сохранено». Отправляйте `null`, если поле пустое, тогда
  старое значение не затрётся.
- Ядро ничего не знает о ваших ключах конфига — сохранит то, что вернёт
  `collect()`.

---

## 7в. Длинная операция с показом хода

Реальный пример из приложения дубликатов: поиск по всему архиву идёт минутами,
поэтому он оформлен как фоновая задача, а кнопка опрашивает её состояние.

Бэкенд:

```python
def _scan(job, params):
    job.progress(5, "ищу точные копии")
    ...
    for n, group in enumerate(groups):
        if n % 200 == 0:
            if job.cancelled():
                return {"stopped": True}
            job.progress(25 + int(n / total * 60), f"обработано {n} из {total}")
    return {"найдено": len(pairs)}


def register(core):
    core.jobs.register("scan-all", _scan)      # тип станет myapp.scan-all
```

Фронтенд: кнопка вызывает свой эндпоинт, получает `job_id`, затем раз в две
секунды читает `/api/jobs?limit=50` и находит там свою задачу по id. Поле
`message` показывает текущий шаг, `status` станет `done` или `failed`.

Проверяйте `job.cancelled()` **не в каждой итерации**, а раз в несколько сотен:
проверка ходит в базу.


---

## 7г. Корзина: чтобы ваши данные пережили удаление

Удалённый файл уходит в корзину (`.trash` внутри хранилища). Его строка в
`files` удаляется, и **всё, что ссылается на неё внешним ключом, удаляется
каскадом** — в том числе таблицы вашего приложения. Если ничего не сделать,
после восстановления файл вернётся без ваших данных.

Поэтому перед удалением ядро собирает снимок, а приложение дописывает в него
своё. При восстановлении ядро отдаёт снимок обратно.

```python
def snapshot(snap, file_id, db, **_):
    with db.cursor() as cur:
        cur.execute("SELECT note FROM myapp_notes WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
    if row:
        snap["myapp"] = {"note": row["note"]}   # ключ — id вашего приложения
    return snap


def restore(payload):
    data = payload["snapshot"].get("apps", {}).get("myapp")
    if not data:
        return
    with payload["db"].cursor() as cur:           # соединение даёт ядро, не закрывайте
        cur.execute("INSERT IGNORE INTO myapp_notes (file_id, note) VALUES (%s,%s)",
                    (payload["file_id"], data["note"]))


def register(core):
    core.events.add_filter("file.trash", snapshot)
    core.events.on("file.restored", restore)
```

Правила:

- В снимок кладите **только сериализуемое в JSON**: даты — через `.isoformat()`.
- Файл восстанавливается с **тем же id**, поэтому ссылки на него остаются верными.
- При восстановлении проверяйте, что связанное ещё существует: автора, тег или
  пользователя могли удалить, пока файл лежал в корзине. Лучше вернуть файл
  без части данных, чем упасть.
- Если ваш обработчик упадёт, файл всё равно восстановится — без ваших данных.
- Удалять файлы из своего кода — только через корзину ядра, а не `DELETE FROM files`,
  иначе пользователь не сможет их вернуть.

Живой пример — приложения `furry` (автор, пост, серия) и `duplicates` (pHash).

---

## 7д. Загрузка файлов: как приложению в неё вмешаться

Ядро принимает **любой** файл — фото, документ, музыку — и кладёт его в
указанную папку (поле `folder`) или в корень. Ядро не знает ни про авторов,
ни про хеши. Приложения влияют на загрузку в два этапа.

**До сохранения — фильтр `file.upload`.** Получает план `{"folder": ..., "error": None}`
и все поля формы. Может сменить папку или отказать:

```python
def plan(p, form, md5, db, **_):
    if form.get("project"):
        p["folder"] = f"Проекты/{form['project']}"
        p["myapp"] = {"project": form["project"]}   # запомнить для второго этапа
    if form.get("secret") == "no":
        p["error"] = "Нельзя"          # отказ; p["status"] = 403 — свой код ответа
    return p

core.events.add_filter("file.upload", plan)
```

**После сохранения — событие `file.created`.** Файл уже в хранилище и в `files`.
Здесь дописывают своё: связь с сущностями приложения, хеши, превью.

```python
def created(payload):
    data = payload["plan"].get("myapp")        # то, что положили в план выше
    if data:
        with payload["db"].cursor() as cur:    # соединение ядра, не закрывайте
            cur.execute("INSERT INTO myapp_links (file_id, project) VALUES (%s,%s)",
                        (payload["file_id"], data["project"]))

core.events.on("file.created", created)
```

В `payload["content"]` — байты файла: не нужно перечитывать его из хранилища.

Живые примеры: `furry` (поле «Автор» → папка `@автор` и привязка к автору),
`duplicates` (считает pHash каждого нового изображения и видео).

Ядро само отказывает, если **точная копия** (тот же md5) уже лежит в той же папке,
и не пускает в `..` и `.trash`.

---

## 7е. Зоны расширения: своя кнопка на чужой странице

Приложение может добавить кнопку не только на свою страницу, но и в те места
ядра, которые объявлены как **зоны**. Ядро при этом не меняется, а вместе с
выключенным приложением исчезают и его кнопки.

В манифесте укажите файл, который грузится **сразу при старте** (обычный
`pages[].js` грузится только при открытии страницы приложения — для кнопок в
чужих местах этого мало):

```json
"scripts": ["static/contrib.js"]
```

В самом файле:

```javascript
registerContribution('file.actions', {
  id: 'myapp-send',          // обязателен и уникален
  icon: '📤',
  title: 'Надіслати',        // всплывающая подсказка
  order: 20,                 // меньше — левее
  onClick: (ctx) => myappSend(ctx.file_id),
});
```

Зоны ядра:

| Зона | Где | Контекст (`ctx`) |
|---|---|---|
| `file.actions` | кнопки в карточке файла | `{file_id}` |
| `gallery.toolbar` | панель над «Артами» | `{}` |
| `catalog.toolbar` | панель каталога | `{path}` |

Правила:

- Вместо кнопки можно отдать свою разметку: `{id, html, order}` — ядро вставит
  её как есть. Обработчики в этом случае вешайте сами.
- Зон намеренно немного: каждая — обещание не ломать её дальше. Добавить новую
  зону легко, убрать — уже нет. Нужна ещё одна — скажите, какая и зачем.
- Расширять можно объявленные места, а не перерисовывать чужие страницы: иначе
  одно приложение могло бы сломать вид всего архива.

Живой пример — приложение `duplicates`: кнопка «🔍 Знайти схожі» в карточке
файла (`static/contrib.js` плюс свой эндпоинт `/api/duplicates/similar/<id>`).
## 7ж. Переводы (locales/)

Украинский — базовый язык: надписи пишутся прямо в коде по-украински
(в JS, в HTML-шаблонах, в сообщениях `jsonify({"error": ...})`). Для
другого языка у приложения есть своя папка `locales/` с файлом на язык:

```
apps/installed/myapp/locales/en.json
{
  "Мої нотатки": "My notes",
  "Видалити нотатку «{0}»?": "Delete note «{0}»?",
  "Збережено: {0} з {1}": "Saved: {0} of {1}"
}
```

- Ключ — украинская фраза ровно как в коде; `{0}`, `{1}`… — изменяемые
  части (из `${...}` в JS или `{...}` в f-строке). В переводе их можно
  переставлять.
- Эмодзи, знаки и пробелы по краям в ключ не пишутся: `'✓ Готово!'` →
  ключ `Готово`.
- Ядро склеивает `locales/` ядра и всех приложений в `/i18n.js`, а
  страница сама подставляет перевод: текст, `title`/`placeholder`, тосты,
  `alert/confirm/prompt`, ответы сервера. Ничего вызывать не нужно.
- Где нужен перевод в коде явно: `tr('Збережено: {0} з {1}', a, b)`.
  Старые ключи ядра (`t('nav.overview')`) тоже работают.
- Пользовательские данные (имена, теги, сообщения) обёрните элементом с
  атрибутом `data-no-i18n`, чтобы их не трогать.
- Что осталось без перевода: включите English, пройдитесь по страницам
  приложения и выполните в консоли браузера `i18nMissing()` — получите
  готовый кусок JSON, останется вписать переводы.
- Новый язык (например `pl.json`) кладётся рядом; кнопку языка в
  интерфейсе добавляет ядро.

## 7з. Тяжёлые сводки: считать заранее

Если страница показывает сводку, которую долго считать (счётчики по всей
базе), не считайте её на каждый запрос — используйте `core/warmcache.py`:

```python
from core.warmcache import WarmCache

def _compute(db):                 # своё соединение, закрывать не нужно
    return qq(db, "SELECT ... GROUP BY ...")

summary_cache = WarmCache("myapp-summary", _compute)   # на уровне модуля

@core.api.route("/summary")
def summary():
    return jsonify(summary_cache.get())
```

Считается в фоне на старте сервера, пересчитывается после любого успешного
POST/PUT/PATCH/DELETE и событий `file.*`, плюс раз в минуту (то, что
делает архиватор — отдельный процесс). Хуже, чем «отстало на минуту»,
не бывает; после собственного действия пользователь видит свежие числа.

---

## 8. Что уже есть в ядре

### Таблицы

| Таблица | Содержит |
|---|---|
| `files` | файлы: `id`, `rel_path`, `name`, `ext`, `size_bytes`, `width`, `height`, `md5_hash`, `created_at` |
| `tags`, `download_tags` | теги файлов (`download_tags.download_id` → `files.id`) |
| `users` | пользователи: `id`, `email`, `display_name`, `is_admin` |
| `user_favorites` | избранное пользователя |
| `trash_items` | корзина: что удалено, откуда и снимок данных для восстановления |
| `jobs` | фоновые задачи |
| `apps`, `app_migrations`, `app_settings` | система приложений |
| `catalog_folder_hidden` | скрытые папки каталога |
| `external_*`, `key_hidden_folders` | внешние архивы и ключи доступа |

Историческое имя колонки `download_tags.download_id` и
`user_favorites.download_id` ссылается на `files.id` — переименование отложено.

### Эндпоинты ядра

`/api/catalog/list`, `/api/catalog/file`, `/api/gallery/ids`,
`/api/gallery/meta/<id>`, `/api/gallery/stream/<id>`, `/api/upload`,
`/api/download/<id>` (DELETE), `/api/tags`, `/api/stats`, `/api/users/list`,
`/api/jobs`, `/api/apps`, `/api/apps/ui`, `/api/config`, `/api/trash`, `/api/branding`.

Все `/api/*`, кроме входа и статуса БД, требуют сессии — проверка
централизованная, декоратор писать не нужно.

---

## 9. Полный пример: приложение «Заметки»

**manifest.json**
```json
{
  "id": "notes",
  "name": "Заметки",
  "version": "1.0.0",
  "description": "Заметки к архиву",
  "author": "Felix",
  "icon": "📝",
  "core_api_version": 1,
  "min_core_version": "1.0.0",
  "dependencies": {},
  "permissions": ["database"],
  "backend": "backend",
  "table_prefix": "notes_",
  "menu": [{"id": "notes", "label": {"uk": "Нотатки", "ru": "Заметки"},
            "icon": "📝", "order": 50, "page": "notes"}],
  "pages": [{"id": "notes", "js": "static/notes.js", "init": "notesInit"}]
}
```

**migrations/001_init.sql**
```sql
CREATE TABLE IF NOT EXISTS notes_items (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id    INT,
    text       TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**backend/__init__.py**
```python
from flask import Blueprint, jsonify, request

bp = Blueprint("notes", __name__)
core = None


@bp.route("/items", methods=["GET", "POST"])
def items():
    db = core.db
    try:
        if request.method == "POST":
            text = (request.json or {}).get("text", "").strip()
            if not text:
                return jsonify({"error": "пустая заметка"}), 400
            with db.cursor() as cur:
                cur.execute("INSERT INTO notes_items (user_id, text) VALUES (%s,%s)",
                            (core.current_user_id(), text[:core.settings.get("max_len", 1000)]))
            db.commit()
        with db.cursor() as cur:
            cur.execute("SELECT id, text FROM notes_items WHERE user_id=%s ORDER BY id DESC",
                        (core.current_user_id(),))
            return jsonify(cur.fetchall())
    finally:
        db.close()


def on_file_created(payload):
    print(f"[notes] новый файл {payload.get('file_id')}")


def register(core_api):
    global core
    core = core_api
    core.api.add_routes(bp)
    core.events.on("file.created", on_file_created)
```

**static/notes.js**
```javascript
async function notesInit(root) {
  root.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input id="note-input" class="input" placeholder="новая заметка" style="flex:1">
        <button class="btn btn-sm" onclick="notesAdd()">Добавить</button>
      </div>
      <div id="note-list"></div>
    </div>`;
  notesReload();
}

async function notesReload() {
  const items = await fetch('/api/apps/notes/items', {credentials: 'include'}).then(r => r.json());
  document.getElementById('note-list').innerHTML =
    items.length ? items.map(i => `<div style="padding:6px 0;border-bottom:1px solid var(--border)">${i.text}</div>`).join('')
                 : '<div class="muted">Пока пусто</div>';
}

async function notesAdd() {
  const el = document.getElementById('note-input');
  if (!el.value.trim()) return;
  await fetch('/api/apps/notes/items', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: el.value})
  });
  el.value = '';
  notesReload();
}
```

Положить папку в `apps/installed/notes/`, перезапустить архив. Приложение
появится в разделе «Застосунки» и в меню.

---

## 10. Установка, включение, удаление

- **Установка** — положить папку в `apps/installed/` и перезапустить сервер.
  Миграции выполнятся сами, новое приложение включается сразу.
- **Выключение** — в разделе «Застосунки». **Вступает в силу после
  перезапуска**: убрать из работающего Flask уже зарегистрированные маршруты
  нельзя. Ядро честно пишет об этом, а не делает вид, что всё применилось.
- **Приложение, от которого зависят другие**, выключить нельзя — ядро назовёт,
  кто мешает.
- **Упавшее приложение** получает статус `error` и пытается загрузиться снова
  при каждом старте: причину могли уже исправить. Выключенное вручную остаётся
  выключенным.
- **Удаление** — удалить папку. Таблицы и настройки останутся в базе.

---

## 11. Ограничения, о которых нужно знать заранее

1. **Настоящей изоляции нет.** Приложение получает обычное соединение с базой и
   выполняется в том же процессе, что и ядро. Технически оно может дотянуться
   куда угодно. Проверка префикса таблиц — защита от опечатки, а не от злого
   умысла. Чужой непроверенный код ставить не стоит.
2. **Маршруты нельзя снять на лету** — отсюда требование перезапуска.
3. **Два приложения не могут занять один адрес** — второй blueprint просто не
   зарегистрируется.
4. **Расширять можно не все ответы.** Точка расширения пока одна — фильтр
   `file.meta` для карточки файла. Для остальных ответов ядра фильтров нет;
   нужны свои данные — делайте свой эндпоинт.
5. **Фронтенд общий.** Ядро подгружает js приложения, но разметка страниц ядра
   и большой общий `dashboard.js` пока не разделены по приложениям.
6. **Один рабочий поток задач.** Тяжёлая задача задержит остальные.
7. **Точка с запятой внутри SQL-комментария** раньше ломала разбор миграции —
   исправлено, но комментарии лучше держать простыми.

---

## 12. Чек-лист перед сдачей приложения

- [ ] Имя папки совпадает с `id` в манифесте
- [ ] Есть `backend/__init__.py` с функцией `register(core)`
- [ ] Нет ни одного `from core.` — всё через объект `core`
- [ ] Все таблицы с префиксом из манифеста, все с `IF NOT EXISTS`
- [ ] Каждое соединение с базой закрывается, после записи есть `commit()`
- [ ] Длинные задачи проверяют `job.cancelled()`
- [ ] Фронтенд-запросы идут с `credentials: 'include'`
- [ ] Функция из `pages[].init` объявлена глобально
- [ ] Приложение проверено и во включённом, и в выключенном состоянии
- [ ] Ядро без этого приложения продолжает работать
- [ ] Включён English — `i18nMissing()` в консоли пуст (переводы в `locales/en.json`)

---

## 13. Как проверять

```bash
# приложение загрузилось и в каком состоянии
curl -b cookies.txt http://localhost:5000/api/apps

# меню, которое увидит фронтенд
curl -b cookies.txt http://localhost:5000/api/apps/ui

# свой эндпоинт
curl -b cookies.txt http://localhost:5000/api/apps/myapp/items

# история фоновых задач
curl -b cookies.txt http://localhost:5000/api/jobs
```

Если приложение не загрузилось, причина будет в поле `error` ответа
`/api/apps` и в консоли сервера при старте.
