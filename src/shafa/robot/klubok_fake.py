"""KLB-001: підставний Клубок — лише для тестів і ручних прогонів (`--klubok-dry-fill --fake-klubok`).

Локальний сервер на 127.0.0.1 зі сторінкою, схожою на форму додавання `klubok.com/item/add` (ті самі
`id`/`name`, глобальний `formData` з `userItem`, `images`, `categoryId()`, але без Vue). Браузер у
цьому режимі пускає лише 127.0.0.1; справжній `klubok_guard` застосовується до тих самих шляхів
(адреса підставного сервера підміняється на `https://klubok.com`), тому пастки «Обновить все»
(`/renew/all`) і «Отметить проданным» блокуються так само, як на живому сайті. Профіль Chrome —
тимчасовий, не профіль робота. Дерево категорій і фільтри — зі знімка `klubok.catalog`.

KLB-003: `GET /api/get-items` посторінково зі штучних оголошень (`listing`), помилки сторінок і
«зсув» списку посеред читання; KLB-006: `renew_all` — однакова `renew_date` і випадковий порядок
при рівності (`unstable_ties`), `items_limit_max` — сайт віддає не більше N за запит; `/item/add` без входу → 301 `/signup`; `POST /api/add-edit-used` —
режими `SUBMITS` (успіх, «низька оцінка», тайм-аут, HTTP 500, не JSON, інший статус, кнопка не
надсилає); підставна reCAPTCHA на «чужому» хості: шлях `/google/…` підставного сервера guard бачить
як `https://www.google.com/…` (так перевіряється дозвіл reCAPTCHA), `/evil/…` — як чужий сайт; 429.

Фото: кожне вантажиться `DELAY` секунд (випадково), у `formData.images` елемент з'являється в
порядку **відповіді сервера** — так відтворюється розсип порядку (1, 2, 3, 4, 5, 7, 8, 6), якщо
вибрати кілька файлів одразу.
"""

import html
import itertools
import json
import random
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from shafa.klubok import catalog
from shafa.robot import klubok_form as form

SCENARIOS = {
    "ok": "усе гаразд",
    "login": "сторінка просить увійти",
    "signup": "без входу: /item/add → 301 /signup (як живий Клубок, HYP-4)",
    "evil": "сторінка ще й надсилає щось на чужий сайт (guard має заблокувати)",
    "error429": "відповідь на відправку — «низька оцінка reCAPTCHA» (/error429)",
    "brand_none": "довідник брендів порожній",
}
DELAY = (0.1, 1.5)  # скільки секунд сервер «приймає» одне фото
BRANDS = [
    "Ecco Golf", "Ecco (Кирилиця)", "Nike (Кирилиця)", "Zara (Кирилиця)", "Salomon (Кирилиця)",
    "Marks & Spencer (Кирилиця)", "Ugg (Кирилиця)",
]  # fmt: skip
COLORS = {
    1: "Синий", 2: "Голубой", 3: "Зелёный", 4: "Хаки", 5: "Коричневый", 6: "Серый", 7: "Чёрный",
    8: "Белый", 9: "Серебряный", 10: "Бежевый", 11: "Жёлтый", 12: "Золотой", 13: "Оранжевый",
    14: "Красный", 15: "Розовый", 16: "Фиолетовый", 17: "Бордо",
}  # fmt: skip
TRAP_WORDS = ("renew", "marksold", "delete", "payment", "error429")
SUBMITS = {
    "success": "успіх: status success, used_id",
    "error": "status Error (низька оцінка reCAPTCHA)",
    "timeout": "відповідь не приходить довше, ніж чекає робот",
    "http500": "HTTP 500",
    "notjson": "відповідь не JSON",
    "other": "status — щось інше",
    "nosend": "кнопка нічого не надсилає",
    "429": "HTTP 429",
}
USER = "198135"
NAMES = ("Черевики", "Чоботи", "Кросівки", "Туфлі", "Кеди", "Сумка")


def listing(n: int) -> dict[str, Any]:
    """Штучне оголошення `get-items` (та сама структура, що в живого, KLB-003 HYP-1)."""
    item_id = 800_000_000 + n
    name = f"{NAMES[n % len(NAMES)]} Fakebrand{n % 97} {35 + n % 12} розміру"
    return {
        "ban_reason": "", "count_favourites": 0, "count_msg": 0, "count_view": 0, "id": item_id,
        "img_link": f"https://img.klubok.com/img/used/fake/m/{item_id}_1.jpg", "is_fixed": False,
        "is_p": True, "link": f"https://klubok.com/item-fake-{item_id}", "moderated": 0,
        "name": name, "price": 500 + n % 40 * 50, "promoted": False,
        "renew_date": f"2026-11-17T10:{n // 60 % 60:02d}:{n % 60:02d}Z", "sold": False,
    }  # fmt: skip  # адреси-пастки: сервер їх записує


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Підставний Клубок — не справжній сайт</title></head><body>
<h1>Підставний Клубок — не справжній сайт</h1>
<p><a href="/renew/all">Обновить все</a> ·
<a href="/users/198135?pg=marksold&id=1">Отметить проданным</a> ·
<a href="/users/198135?pg=renew-used&id=1">Обновить</a></p>
<form id="addItemForm" autocomplete="off">
<input id="token" type="hidden" value="fake">
<input id="user_id" type="hidden" value="%(user)s">
<div><label for="name">Описание</label>
<input id="name" type="text" maxlength="100" minlength="3" name="item_name"></div>
<div><textarea name="text" rows="6" minlength="3"></textarea></div>
<div><label for="price">Стоимость</label><input id="price" min="1" name="price" type="number"></div>
<div><label>Загрузка фотографий</label>
<input class="custom-file-input" type="file" multiple accept="image/png, image/jpeg">
<div id="section">Выберите фотографии</div>
<div id="overload" hidden>Невозможно опубликовать более 15 изображений</div></div>
<div><select id="category"></select></div>
<div id="sub1" hidden><select name="subcategory"></select></div>
<div id="sub2" hidden><select name="targetcategory"></select></div>
<div id="filters" hidden>
<div id="filter1" hidden></div>
<div id="filter2" hidden><label for="brand">Бренд</label>
<input id="brand" placeholder="Бренд">
<div class="dropdown-menu" hidden></div></div>
<div id="filter3" hidden></div>
<div id="filter4" hidden></div>
<div id="filter15" hidden></div>
</div>
<div id="uploading" hidden></div>
<button id="btn-submit" type="submit">Опубликовать</button>
</form>
<script>
var TREE = %(tree)s, FILTERS = %(filters)s, SIZES = %(sizes)s, COLORS = %(colors)s;
var SCENARIO = %(scenario)s, SENDS = %(sends)s;
// Підставна reCAPTCHA: як справжня, ходить POST на google.com (тут — /google/…); не пустили — токена немає.
window.grecaptcha = {ready: function (f) { f(); },
  execute: function () {
    return fetch("/google/recaptcha/api2/reload?k=fake", {method: "POST", body: "x"})
      .then(function () { return "fake-token"; });
  }};
if (SCENARIO === "evil") fetch("/evil/collect", {method: "POST", body: "x"}).catch(function () {});
var formData = {
  userItem: {item_name: "", text: "", price: "", brand_name: "", sex: "", condition: "",
    size: [], color: "", last_category_id: "", captcha: ""},
  images: [], pending: 0, category: null, subcategory: null, targetcategory: null,
  categoryId: function () {
    var n = this.targetcategory || this.subcategory || this.category;
    return n ? n.id : undefined;
  },
  numberOfUploading: function () { return this.pending; },
};
var $ = function (s) { return document.querySelector(s); };
var selects = {category: $("#category"), sub: $("[name=subcategory]"),
  target: $("[name=targetcategory]")};

function option(parent, text, value) {
  var o = document.createElement("option");
  o.textContent = text; o.value = value; parent.appendChild(o); return o;
}
function reset(select) {
  select.innerHTML = ""; option(select, "Выберите категорию", "");
}
function leafOf() {
  var n = formData.targetcategory || formData.subcategory || formData.category;
  return n && !(n.sub && n.sub.length) ? n : null;
}
function check(name, value, id, label, list, type) {
  var box = document.createElement("div"); box.className = "form-check";
  var i = document.createElement("input"); i.type = type; i.name = name; i.value = value;
  i.id = id; i.className = "form-check-input";
  var l = document.createElement("label"); l.htmlFor = id; l.textContent = label;
  l.className = "form-check-label";
  i.addEventListener("change", function () { list(i); });
  box.appendChild(i); box.appendChild(l); return box;
}
function show() {
  var leaf = leafOf(), f = leaf ? leaf.filters : [];
  formData.userItem.last_category_id = leaf ? leaf.id : "";
  $("#filters").hidden = !leaf;
  $("#filter1").hidden = f.indexOf("1") < 0;
  $("#filter2").hidden = f.indexOf("2") < 0;
  $("#filter3").hidden = f.indexOf("3") < 0;
  $("#filter4").hidden = f.indexOf("4") < 0;
  $("#filter15").hidden = f.indexOf("15") < 0;
}
for (var s of FILTERS["1"]) {
  $("#filter1").appendChild(check("sex", s.key, "sex" + s.key, s.value, function (i) {
    formData.userItem.sex = Number(i.value); }, "radio"));
}
for (var c of FILTERS["3"]) {
  $("#filter3").appendChild(check("condition", c.key, "condition" + c.key, c.value, function (i) {
    formData.userItem.condition = Number(i.value); }, "radio"));
}
for (var z of SIZES) {
  $("#filter4").appendChild(check("size[]", z, String(z), String(z), function () {
    formData.userItem.size = Array.from(document.querySelectorAll("[name='size[]']:checked"))
      .map(function (x) { return Number(x.value); }); }, "checkbox"));
}
for (var k in COLORS) {
  $("#filter15").appendChild(check("color", k, COLORS[k], COLORS[k], function (i) {
    formData.userItem.color = Number(i.value); }, "radio"));
}
$("#filter15").appendChild(check("color", 99, "Разноцветное", "🌈 Разноцветный", function (i) {
  formData.userItem.color = Number(i.value); }, "radio"));

reset(selects.category);
TREE.forEach(function (n, i) { option(selects.category, n.name, i); });
selects.category.addEventListener("change", function () {
  formData.category = selects.category.value === "" ? null : TREE[selects.category.value];
  formData.subcategory = formData.targetcategory = null;
  reset(selects.sub); reset(selects.target);
  $("#sub2").hidden = true;
  var kids = formData.category ? formData.category.sub : [];
  $("#sub1").hidden = !kids.length;
  kids.forEach(function (n, i) { option(selects.sub, n.name, i); });
  show();
});
selects.sub.addEventListener("change", function () {
  var kids = formData.category.sub;
  formData.subcategory = selects.sub.value === "" ? null : kids[selects.sub.value];
  formData.targetcategory = null;
  reset(selects.target);
  var list = formData.subcategory ? formData.subcategory.sub : [];
  $("#sub2").hidden = !list.length;
  list.forEach(function (n, i) {
    if (n.sub && n.sub.length) {
      var g = document.createElement("optgroup"); g.label = n.name;
      n.sub.forEach(function (m, j) { option(g, m.name, i + "." + j); });
      selects.target.appendChild(g);
    } else { option(selects.target, n.name, String(i)); }
  });
  show();
});
selects.target.addEventListener("change", function () {
  var v = selects.target.value, list = formData.subcategory.sub;
  if (v === "") formData.targetcategory = null;
  else if (v.indexOf(".") > 0) {
    var p = v.split("."); formData.targetcategory = list[p[0]].sub[p[1]];
  } else formData.targetcategory = list[v];
  show();
});

$("#name").addEventListener("input", function (e) { formData.userItem.item_name = e.target.value; });
$("[name=text]").addEventListener("input", function (e) { formData.userItem.text = e.target.value; });
$("#price").addEventListener("input", function (e) {
  formData.userItem.price = e.target.value === "" ? "" : Number(e.target.value); });

var timer = null, menu = $("#filter2 .dropdown-menu");
$("#brand").addEventListener("input", function (e) {
  formData.userItem.brand_name = e.target.value;
  clearTimeout(timer);
  timer = setTimeout(function () {
    var q = "name=" + encodeURIComponent(e.target.value) + "&category=" + formData.categoryId()
      + "&limit=10&s=" + SCENARIO;
    fetch("/api/get-brands?" + q).then(function (r) { return r.json(); }).then(function (list) {
      menu.innerHTML = "";
      list.forEach(function (b) {
        var a = document.createElement("a"); a.className = "dropdown-item"; a.href = "#";
        a.textContent = b.name;
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          formData.userItem.brand_name = b.name; $("#brand").value = b.name; menu.hidden = true;
        });
        menu.appendChild(a);
      });
      menu.hidden = !list.length;
    });
  }, 500);
});

$("input[type=file]").addEventListener("change", function (e) {
  Array.from(e.target.files).forEach(function (file) {
    if (formData.images.length + formData.pending >= 15) { $("#overload").hidden = false; return; }
    formData.pending++;
    var fd = new FormData(); fd.append("username", "photo"); fd.append("files[]", file);
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload-used-photo");
    xhr.onload = function () {
      formData.pending--;
      formData.images.push({title: file.name, tmp_name: JSON.parse(xhr.responseText).tmp_name,
        uploading: false, angel: 0, id: "f" + Math.random().toString(16).slice(2)});
      $("#section").textContent = formData.images.length + " из 15";
      $("#btn-submit").disabled = formData.pending > 0;
    };
    $("#btn-submit").disabled = true;
    xhr.send(fd);
  });
});

$("#addItemForm").addEventListener("submit", function (e) {
  e.preventDefault();
  if (!SENDS) return;
  grecaptcha.ready(function () {
    grecaptcha.execute("key", {action: "add"}).then(function (token) {
      formData.userItem.captcha = token;
      fetch("/api/add-edit-used?s=" + SCENARIO, {method: "POST",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify(formData.userItem)})
        .then(function (r) { return r.json(); }).then(function (data) {
          if (data.status === "success" && data.used_id > 0)
            location.replace("/payment/" + data.used_id + "?action=used-submit-ok-logged");
          else if (data.status === "Error")
            location.replace("/error429?action=Add_new_item&score=" + data.score);
        }).catch(function () {});
    });
  });
});
</script></body></html>"""

SIGNUP_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Регистрация</title>
</head><body><h1>Підставний Клубок — вхід / реєстрація</h1></body></html>"""

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Вход</title></head>
<body><h1>Підставний Клубок — вхід</h1><form id="login"><input name="login">
<button type="button">Войти</button></form></body></html>"""


def tree(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Дерево категорій зі знімка: лише гілки до листків таблиці (`ids` + `path` + `filters`).

    `id` — рядок, як `cat_id` у відповіді сайту."""
    roots: list[dict[str, Any]] = []
    for leaf in snapshot["leaves"].values():
        level = roots
        for cat_id, name in zip(leaf["ids"], leaf["path"], strict=True):
            node = next((n for n in level if n["id"] == str(cat_id)), None)
            if node is None:
                node = {"id": str(cat_id), "name": name, "filters": leaf["filters"], "sub": []}
                level.append(node)
            level = node["sub"]
    return roots


def page(
    snapshot: dict[str, Any], scenario: str = "ok", sends: bool = True, user: str = USER
) -> str:
    if scenario == "login":
        return LOGIN_PAGE
    filters = snapshot["filters"]
    values = {
        "tree": tree(snapshot),
        "filters": {k: v["values"] for k, v in filters.items()},
        "sizes": [v["key"] for v in filters["4"]["values"]],
        "colors": COLORS,
        "scenario": scenario,
        "sends": sends,
    }
    return PAGE % (
        {k: json.dumps(v, ensure_ascii=False).replace("</", "<\\/") for k, v in values.items()}
        | {"user": html.escape(user)}
    )


class _Handler(BaseHTTPRequestHandler):
    server: "Server"

    def _scenario(self) -> str:
        return (parse_qs(urlsplit(self.path).query).get("s") or ["ok"])[0]

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        self.server.hits.append(("GET", self.path))
        if parts.path in self.server.too_many:
            self._send(429, b"{}", "application/json")
        elif any(word in self.path.lower() for word in TRAP_WORDS):
            self.server.traps.append(self.path)
            self._send(200, b"trap", "text/plain")
        elif parts.path == "/item/add" and self._scenario() == "signup":
            self.send_response(301)
            self.send_header("Location", "/signup")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif parts.path == "/signup":
            self._send(200, SIGNUP_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif parts.path == "/item/add":
            body = page(
                self.server.snapshot, self._scenario(), self.server.submit != "nosend",
                self.server.user,
            ).encode("utf-8")  # fmt: skip
            self._send(200, body, "text/html; charset=utf-8")
        elif parts.path == "/api/get-items":
            self._items(parse_qs(parts.query))
        elif parts.path == "/api/get-brands":
            self._brands(parts.query)
        else:
            self._send(404, b"{}", "application/json")

    def _items(self, params: dict[str, list[str]]) -> None:
        srv = self.server
        offset = int((params.get("offset") or ["0"])[0])
        limit = int((params.get("limit") or ["60"])[0])
        srv.item_reads.append(offset)
        fault = srv.items_fault.get(offset)
        if isinstance(fault, int):
            self._send(fault, b"{}", "application/json")
            return
        if fault == "notjson":
            self._send(200, b"<html>not json</html>", "text/html")
            return
        if (params.get("user_id") or [""])[0] != USER:
            self._send(
                200, b'{"status": "success", "items": [], "items_count": 0}', "application/json"
            )
            return
        if srv.items_limit_max is not None:
            limit = min(limit, srv.items_limit_max)
        srv.item_limits.append(limit)
        start = offset
        if srv.shift_reads and offset > 0:  # «Обновить все» посеред читання: список зсунувся на 1
            start = offset - 1
            if offset + limit >= len(srv.listings):
                srv.shift_reads -= 1
        ordered = srv.listings
        if srv.unstable_ties:  # як живий: `ORDER BY renew_date` без другого ключа (KLB-006)
            ordered = sorted(ordered, key=lambda item: (item["renew_date"], srv.random.random()))
        page_items = ordered[start : start + limit]
        count = len(srv.listings) if srv.items_count is None else srv.items_count
        body = {"items": page_items, "items_count": count, "tabs_count": [count, 0, 0, 0],
                "status": "success"}  # fmt: skip
        self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json")

    def _brands(self, query: str) -> None:
        params = parse_qs(query)
        name = (params.get("name") or [""])[0].casefold()
        found = [] if self._scenario() == "brand_none" else [
            {"name": b} for b in BRANDS if name and b.casefold().startswith(name)
        ]  # fmt: skip
        self._send(200, json.dumps(found, ensure_ascii=False).encode("utf-8"), "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        path = urlsplit(self.path).path
        self.server.hits.append(("POST", self.path))
        if path == "/api/upload-used-photo":
            time.sleep(self.server.random.uniform(*self.server.delay))
            self.server.uploaded += 1
            body = {"tmp_name": f"tmp{next(self.server.counter)}"}
            self._send(200, json.dumps(body).encode(), "application/json")
        elif path == "/api/add-edit-used":
            self._submit()
        elif path.startswith("/google/recaptcha/"):
            self.server.captcha_posts += 1
            self._send(200, b")]}'\n[]", "application/json")
        else:
            self._send(404, b"{}", "application/json")

    def _submit(self) -> None:
        srv = self.server
        srv.submits += 1  # у KLB-001 сюди не має дійти нічого
        mode = "error" if self._scenario() == "error429" else srv.submit
        if mode == "timeout":
            time.sleep(srv.submit_delay)
            mode = "success"
        if mode == "http500":
            self._send(500, b"{}", "application/json")
        elif mode == "429":
            self._send(429, b"{}", "application/json")
        elif mode == "notjson":
            self._send(200, b"<html>oops</html>", "text/html")
        elif mode == "other":
            self._send(200, b'{"status": "pending"}', "application/json")
        elif mode == "error":
            body = {"status": "Error", "score": 0.1, "message": "low score"}
            self._send(200, json.dumps(body).encode(), "application/json")
        else:
            used_id = next(srv.counter)
            srv.published.append(used_id)
            if not srv.hide_published:
                srv.listings.append(listing(len(srv.listings)) | {"id": used_id,
                    "link": f"https://klubok.com/item-new-{used_id}"})  # fmt: skip
            body = {"status": "success", "used_id": used_id}
            self._send(200, json.dumps(body).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class Server(ThreadingHTTPServer):
    """Підставний Клубок у фоновому потоці."""

    def __init__(
        self,
        snapshot: dict[str, Any] | None = None,
        delay: tuple[float, float] = DELAY,
        seed: int | None = None,
        listings: int = 0,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.snapshot = snapshot or catalog.snapshot()
        self.delay = delay
        self.random = random.Random(seed)
        self.counter = itertools.count(900_000_001)
        self.hits: list[tuple[str, str]] = []
        self.traps: list[str] = []  # запити на адреси «Обновить все» тощо, що дійшли до сервера
        self.uploaded = 0
        self.submits = 0
        # KLB-003
        self.listings = [listing(n) for n in range(listings)]
        self.items_fault: dict[int, int | str] = {}  # offset → HTTP-статус чи "notjson"
        self.items_count: int | None = None  # None — справжня довжина списку
        self.shift_reads = 0  # скільки повних читань «зсунуться»
        self.item_reads: list[int] = []
        # KLB-006
        self.item_limits: list[int] = []
        self.items_limit_max: int | None = None  # сайт віддає не більше стількох за запит
        self.unstable_ties = False  # однакова `renew_date` — порядок між запитами випадковий

        self.too_many: set[str] = set()  # шляхи, що відповідають 429
        self.submit = "success"  # режим `SUBMITS`
        self.submit_delay = 5.0  # с, режим "timeout"
        self.hide_published = False  # виставлене не з'являється в get-items
        self.published: list[int] = []
        self.captcha_posts = 0
        self.user = USER
        self.base = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()

    def renew_all(self, groups: int = 29) -> None:
        """«Обновить все»: `renew_date` однакова до секунди в групах (11.11: 29 значень на 1787)."""
        total = len(self.listings)
        for n, item in enumerate(self.listings):
            item["renew_date"] = f"2026-11-17T17:06:{n * groups // total:02d}Z"
        self.unstable_ties = True


FOREIGN = {"google": "https://www.google.com", "evil": "https://evil.example"}


def klubok_url(url: str, base: str) -> str:
    """Адреса підставного сервера → та сама на `https://klubok.com` (для справжнього guard)."""
    return "https://klubok.com" + url[len(base) :]


class FakeSession(form.Session):
    """Сесія робота проти підставного Клубку: той самий guard, інша мережа, тимчасовий профіль."""

    def __init__(
        self,
        playwright: Any,
        base: str,
        scenario: str = "ok",
        headless: bool = True,
        allow_upload: bool = True,
        context: Any = None,
        publishing: bool = False,
        human: bool = False,
    ) -> None:
        """`context` — готовий контекст браузера (тести); без нього — тимчасовий профіль Chrome."""
        if scenario not in SCENARIOS:
            raise ValueError(f"невідомий сценарій підставного Клубку: {scenario}")
        self._setup(allow_upload, publishing=publishing, human=human)
        self.base = base
        self.scenario = scenario
        self.profile = None if context else tempfile.TemporaryDirectory(prefix="fake-klubok-")
        self.context = context or playwright.chromium.launch_persistent_context(
            self.profile.name, channel="chrome", headless=headless
        )
        self._attach()

    def verdict_url(self, url: str) -> str | None:
        """Адреса для guard; None — запит не на підставний Клубок (блокується тут же).
        `/google/…` і `/evil/…` підставного сервера — «чужі» хости для перевірки guard."""
        for prefix, host in FOREIGN.items():
            if url.startswith(f"{self.base}/{prefix}/"):
                return host + url[len(self.base) + len(prefix) + 1 :]
        if url == self.base or url.startswith((self.base + "/", self.base + "?")):
            return klubok_url(url, self.base)
        return None

    def form_url(self) -> str:
        return f"{self.base}/item/add?s={self.scenario}"

    def outside(self) -> list[str]:
        """Запити, що намагались піти не на підставний Клубок (заблоковані сесією)."""
        return [url for url, why in self.blocked if why.startswith("не підставний")]

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            if self.profile:
                self.profile.cleanup()
