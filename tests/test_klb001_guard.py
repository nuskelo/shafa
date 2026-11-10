"""KLB-001: захист робота Клубку — кожен рядок правил (чисті функції, без браузера)."""

import pytest

from shafa.robot import klubok_guard as g

K = "https://klubok.com"
MULTIPART = "multipart/form-data; boundary=x"


# --- клік ---


@pytest.mark.parametrize(
    "text",
    [
        "Опубликовать", "опубликовать", "Редактировать", "Обновить все", "Отметить проданным",
        "Пометить как проданное", "Удалить", "Сделать горячим", "Реклама", "Оплатить", "VIP",
        "Эконом", "Быстро продать", "Выйти",
    ],
)  # fmt: skip
def test_forbidden_words_block_click(text):
    assert not g.click_allowed(text)


def test_submit_is_never_clickable_even_with_harmless_text():
    assert not g.click_allowed("Далі", "submit")
    assert not g.click_allowed("", "SUBMIT")


def test_ordinary_controls_are_clickable():
    assert g.click_allowed("Чёрный")
    assert g.click_allowed("38", "checkbox")
    assert g.click_allowed("Выберите фотографии")


def test_links_are_never_clickable():
    assert not g.click_allowed("Мои объявления", anchor_href="/users/198135/manage")
    assert not g.click_allowed("Обновить все", anchor_href="/renew/all")
    assert not g.click_allowed("x", anchor_href="https://klubok.com/")
    assert not g.click_allowed("x", anchor_href="")  # порожній href — теж посилання


def test_brand_hint_is_clickable_only_in_exact_shape():  # D1
    ok = {"anchor_href": "#", "anchor_classes": "dropdown-item", "anchor_in_brand_list": True}
    assert g.click_allowed("Ecco (Кирилиця)", **ok)
    assert not g.click_allowed("Ecco", **(ok | {"anchor_href": "/renew/all"}))  # не `#`
    assert not g.click_allowed("Ecco", **(ok | {"anchor_in_brand_list": False}))  # поза #filter2
    assert not g.click_allowed("Ecco", **(ok | {"anchor_classes": "nav-link"}))  # інший клас
    assert not g.click_allowed("Ecco", **(ok | {"anchor_href": "#top"}))  # не рівно `#`
    assert not g.click_allowed("Обновить", **ok)  # заборонене слово сильніше
    assert not g.click_allowed("Ecco", "submit", **ok)


def test_enter_is_forbidden_everywhere():
    assert not g.key_allowed("Enter")
    assert not g.key_allowed("NumpadEnter")
    assert g.key_allowed("Escape")
    assert g.key_allowed("Tab")


# --- мережа: небезпечні GET ---


@pytest.mark.parametrize(
    "url",
    [
        f"{K}/renew/all",
        f"{K}/renew/37953247",
        f"{K}/users/198135?pg=renew-used&id=37953247",
        f"{K}/users/198135?pg=marksold&id=37953247",
        f"{K}/users/198135/manage?pg=marksold",
        f"{K}/users/198135?pg=delete-used&id=1",
        f"{K}/item/remove?id=1",
        f"{K}/promotion/hot",
        f"{K}/payment/123?action=used-submit-ok-logged",
        f"{K}/logout",
        f"{K}/users/exit",
        f"{K}/hot/1",
        f"{K}/item/edit-ad?used_id=1&action=hot-ad",
        f"{K}/%72enew/all",  # закодоване «r»
        f"{K}/api/%252Frenew",
        f"{K}/Renew/All",
        "https://static.klubok.com/renew/all",
        "http://klubok.com/renew/all",
    ],
)
def test_dangerous_get_is_blocked(url):
    ok, why = g.request_verdict("GET", url)
    assert not ok, url
    assert "небезпечна" in why


def test_get_renew_all_is_blocked_head_and_options_too():
    for method in ("GET", "HEAD", "OPTIONS"):
        assert not g.request_verdict(method, f"{K}/renew/all")[0]


def test_photo_is_not_hot():
    assert not g.dangerous(f"{K}/api/upload-used-photo")
    assert not g.dangerous(f"{K}/images/photo/1.jpg")
    assert g.dangerous(f"{K}/hot")
    assert g.dangerous(f"{K}/?pg=hotlist")


# --- мережа: POST ---


def test_upload_post_only_when_allowed_and_multipart():
    url = f"{K}/api/upload-used-photo"
    assert g.request_verdict("POST", url, MULTIPART, True) == (True, "завантаження фото")
    assert not g.request_verdict("POST", url, MULTIPART, False)[0]
    assert not g.request_verdict("POST", url, "application/json", True)[0]


def test_add_edit_used_post_is_blocked():  # публікація — лише KLB-003
    for allow in (True, False):
        assert not g.request_verdict("POST", f"{K}/api/add-edit-used", "application/json", allow)[0]


@pytest.mark.parametrize(
    "url",
    [f"{K}/api/other", f"{K}/item/add", f"{K}/users/198135/manage", "https://example.com/x"],
)
def test_any_other_post_is_blocked(url):
    assert not g.request_verdict("POST", url, MULTIPART, True)[0]


def test_post_to_recaptcha_is_blocked_for_now():  # дозвіл — KLB-003
    assert not g.request_verdict("POST", "https://www.google.com/recaptcha/api2/reload", "", True)[
        0
    ]


def test_put_delete_patch_are_blocked():
    for method in ("PUT", "DELETE", "PATCH"):
        assert not g.request_verdict(method, f"{K}/api/get-items")[0]


# --- мережа: GET за списком ---


@pytest.mark.parametrize(
    "url",
    [
        f"{K}/item/add",
        f"{K}/item/add/",
        f"{K}/item/edit-ad?used_id=33864971&pro_edit=1",
        f"{K}/users/198135/manage",
        f"{K}/api/1/categors",
        f"{K}/api/1/filters",
        f"{K}/api/1/secondarycat",
        f"{K}/api/get-brands?name=ecco&category=1000637&limit=10",
        f"{K}/api/get-items?user_id=198135&type=0&sort=date&limit=60&offset=0",
    ],
)
def test_listed_navigation_is_allowed(url):
    assert g.request_verdict("GET", url)[0], url


@pytest.mark.parametrize(
    "url",
    [
        f"{K}/",
        f"{K}/users/198135",
        f"{K}/users/198135/shop",
        f"{K}/notifications",
        f"{K}/item-termo-chereviki-lowa-33-rozmru-37953246",
        f"{K}/api/add-edit-used",
        f"{K}/error429?action=Add_new_item",
        f"{K}/item/edit",
        f"{K}/item/added",
        "https://help.klubok.com/hc/ru",
    ],
)
def test_unlisted_navigation_is_blocked(url):
    ok, why = g.request_verdict("GET", url)
    assert not ok, url
    assert "не зі списку" in why


def test_assets_and_foreign_reads_are_allowed():
    assert g.request_verdict("GET", "https://static.klubok.com/static/css/minify.css")[0]
    assert g.request_verdict("GET", f"{K}/favicon.ico?v=78")[0]
    assert g.request_verdict("GET", "https://stackpath.bootstrapcdn.com/bootstrap.min.css")[0]
    assert g.request_verdict("GET", "https://www.google.com/recaptcha/api.js?render=x")[0]


def test_post_to_foreign_site_is_blocked():
    assert not g.request_verdict("POST", "https://evil.example/collect", "", True)[0]


def test_lookalike_host_is_not_klubok():
    # «notklubok.com» — чужий хост (читання можна, надсилання — ні), а не піддомен Клубку
    assert not g.is_klubok("notklubok.com")
    assert not g.is_klubok("klubok.com.evil.io")
    assert g.is_klubok("static.klubok.com") and g.is_klubok("klubok.com")
    assert not g.request_verdict(
        "POST", "https://notklubok.com/api/upload-used-photo", MULTIPART, True
    )[0]
