import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from shafa import ai
from shafa.photos import PreparedPhoto

FAKE_KEY = "test-key-vyhadanyi-123"
NAMES = ["IMG_1.JPG", "IMG_2.JPG", "IMG_3.JPG"]
POST = (
    "Шкіряні черевики Pavers 38 розміру в ідеальному стані. Устілка 25см. 1200грн\n"
    "#VS_38_розмір\n\nПишіть сюди 👉 @prodavchynia_test, тел. 067 123 45 67"
)

GOOD = {
    "title": "Шкіряні черевики pavers 38 розміру в ідеальному стані",
    "section": "Жіночий одяг / Взуття / Черевики",
    "sizes": ["38"],
    "colors": ["Чорний"],
    "brand": "Pavers",
    "material_upper": ["Шкіра"],
    "material_lining": [],
    "season": ["Демісезон"],
    "type": None,
    "style": None,
    "teen": None,
    "gender": None,
    "keywords": ["черевики", "черевики жіночі", "черевики pavers", "черевики 38"],
    "photo_order": NAMES,
    "doubts": [],
}


def answer(**changes):
    return {**GOOD, **changes}


def photos(names=NAMES):
    return [PreparedPhoto(n, b"\xff\xd8jpeg", 1092, 819) for n in names]


def message(text, stop_reason="end_turn", usage=(15000, 600), cache=(None, None)):
    """Відповідь API; `cache` — (записано в кеш, прочитано з кешу), None — поля немає."""
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=usage[0],
            output_tokens=usage[1],
            cache_creation_input_tokens=cache[0],
            cache_read_input_tokens=cache[1],
        ),
    )


class FakeClient:
    """Підставний клієнт API: повертає задану відповідь або кидає задану помилку."""

    def __init__(self, result):
        self.result = result
        self.requests = []
        self.messages = self

    def with_options(self, **options):
        return self

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def by_name(fields):
    return {f.name: f for f in fields}


def check(**changes):
    return by_name(ai.check_answer(answer(**changes), NAMES, POST))


# --- Запит ---


def test_request_shape():
    request = ai.build_request(POST, photos())

    assert request["model"] == "claude-sonnet-5-5"
    assert request["thinking"] == {"type": "between_tools"}
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": ai.SCHEMA}}
    content = request["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image"] * 3 + ["text"]
    assert content[0]["text"] == "Фото 1: IMG_1.JPG"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"] == "/9hqcGVn"


def test_plain_request_has_no_cache_mark():
    """Звичайний запит («ШІ, спробуй ще раз») — без кешу (D2): інструкція — простий рядок."""
    request = ai.build_request(POST, photos())
    assert request["system"] == ai.system_prompt()
    assert "cache_control" not in json.dumps(request)


def test_cached_request_marks_only_instruction_for_an_hour():
    request = ai.build_request(POST, photos(), cache=True)

    assert request["system"] == [
        {
            "type": "text",
            "text": ai.system_prompt(),
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }
    ]
    assert json.dumps(request).count("cache_control") == 1
    plain = ai.build_request(POST, photos())
    assert {**request, "system": plain["system"]} == plain


def test_request_text_has_code_fields_but_no_contacts():
    text = ai.build_request(POST, photos())["messages"][0]["content"][-1]["text"]

    assert "Шкіряні черевики Pavers 38 розміру" in text
    assert "Ціна на Шафі: 1380 грн" in text
    assert "Стан: Ідеальний" in text
    assert "Довжина устілки: 25" in text
    assert "@prodavchynia_test" not in text
    assert "Пишіть сюди" not in text
    assert "067" not in text


def test_system_prompt_has_catalog():
    prompt = ai.system_prompt()
    assert ai.CATALOG_MARK not in prompt
    assert "Різнокольоровий" in prompt
    assert "Жіночий одяг / Взуття / … : Кросівки, Черевики" in prompt
    assert "type («Тип», до 1): Берці, Челсі" in prompt


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_schema_uses_only_supported_features():
    nodes = list(_walk(ai.SCHEMA))
    unsupported = {"minLength", "maxLength", "maxItems", "minimum", "maximum", "multipleOf"}
    assert not any(unsupported & node.keys() for node in nodes)
    objects = [n for n in nodes if n.get("type") == "object"]
    assert all(n["additionalProperties"] is False for n in objects)
    assert all(set(n["required"]) == set(n["properties"]) for n in objects)
    assert sum(1 for n in nodes if "anyOf" in n) <= 16  # ліміт union-типів
    assert set(ai.SCHEMA["properties"]) == {*ai.FIELDS, "size_system", "doubts"}


def test_estimate_counts_photos_exactly():
    request = ai.build_request(POST, photos())
    usage = ai.estimate(request, photos())
    assert usage.input_tokens > 3 * 1170
    assert usage.output_tokens == ai.OUTPUT_TOKENS_ESTIMATE


def test_estimate_of_cached_batch_request_is_worst_case():
    """Оцінка для пакета: інструкція — як запис у кеш на годину (кеш не спрацював), усе вдвічі."""
    plain = ai.estimate(ai.build_request(POST, photos()), photos())
    cached = ai.estimate(ai.build_request(POST, photos(), cache=True), photos(), batch=True)

    system = round(len(ai.system_prompt()) / ai.CHARS_PER_TOKEN)
    assert cached.cache_write_tokens == system
    assert cached.input_tokens == plain.input_tokens - system
    assert cached.batch
    assert cached.cost_micro_usd > plain.cost_micro_usd / 2


def test_cost_in_micro_dollars():
    assert ai.Usage(15000, 600).cost_micro_usd == 15000 * 2 + 600 * 10


@pytest.mark.parametrize(
    ("usage", "micro_usd"),
    [
        # звичайний запит: $2 і $10 за мільйон
        (ai.Usage(10_000, 400), 20_000 + 4_000),
        # пакет: усе вдвічі
        (ai.Usage(10_000, 400, batch=True), 12_000),
        # кеш: запис на годину $4, читання $0,10 за мільйон
        (ai.Usage(1_000, 400, cache_write_tokens=9_000), 2_000 + 4_000 + 36_000),
        (ai.Usage(1_000, 400, cache_read_tokens=9_000), 2_000 + 4_000 + 900),
        # пакет і кеш разом
        (ai.Usage(9_500, 370, cache_read_tokens=8_800, batch=True), 9_500 + 1_850 + 440),
        (ai.Usage(9_500, 370, cache_write_tokens=8_800, batch=True), 9_500 + 1_850 + 17_600),
        # дробові мікродолари округлюються
        (ai.Usage(0, 0, cache_read_tokens=5), 1),
        (ai.Usage(0, 0, cache_read_tokens=5, batch=True), 0),
    ],
)
def test_cost_for_batch_and_cache(usage, micro_usd):
    assert usage.cost_micro_usd == micro_usd


def test_parse_reads_cache_tokens():
    reply = message(GOOD, usage=(9500, 370), cache=(0, 8800))
    assert ai.parse(reply, batch=True)[1] == ai.Usage(9500, 370, 0, 8800, batch=True)
    assert ai.parse(message(GOOD, usage=(9500, 370)))[1] == ai.Usage(9500, 370)


# --- Відповідь ---


def test_call_returns_answer_and_usage():
    client = FakeClient(message(GOOD))

    result, usage = ai.call(client, {"model": ai.MODEL})

    assert result == GOOD
    assert usage == ai.Usage(15000, 600)
    assert client.requests == [{"model": ai.MODEL}]


@pytest.mark.parametrize(
    ("result", "text"),
    [
        (message(GOOD, stop_reason="refusal"), "ШІ відмовилось обробити цю Картку"),
        (message('{"title": "Шкіряні', stop_reason="max_tokens"), "Відповідь ШІ обрізана"),
        (message('{"title": "Шкіряні'), "Відповідь ШІ не читається"),
        (message("[1, 2]"), "Відповідь ШІ не читається"),
    ],
)
def test_bad_answers_are_errors_with_usage(result, text):
    with pytest.raises(ai.AiError) as error:
        ai.call(FakeClient(result), {})
    assert str(error.value) == text
    assert error.value.usage == ai.Usage(15000, 600)


def _response(status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx2.Response(status, request=request, json={"error": {"message": FAKE_KEY}})


def _status_error(cls, status):
    return cls(f"секрет у тексті {FAKE_KEY}", response=_response(status), body=None)


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "Ключ Claude API не прийнято (401)"),
        (
            _status_error(anthropic.RateLimitError, 429),
            "Забагато запитів до Claude API (429), спробуйте пізніше",
        ),
        (_status_error(anthropic.InternalServerError, 500), "Помилка Claude API (500)"),
        (_status_error(anthropic.BadRequestError, 400), "Помилка Claude API (400)"),
        (anthropic.APIConnectionError(request=_REQUEST), "Немає з'єднання з Claude API"),
        (anthropic.APITimeoutError(request=_REQUEST), "Немає з'єднання з Claude API"),
    ],
)
def test_api_errors_have_plain_text_without_details(error, text):
    with pytest.raises(ai.AiError) as caught:
        ai.call(FakeClient(error), {})
    assert str(caught.value) == text
    assert FAKE_KEY not in str(caught.value)
    assert caught.value.usage is None


def test_make_client_uses_explicit_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "інший-ключ")
    client = ai.make_client(FAKE_KEY)
    assert client.api_key == FAKE_KEY


# --- Перевірка кодом ---


def test_good_answer_has_no_yellow():
    fields = ai.check_answer(GOOD, NAMES, POST)
    assert [f.name for f in fields] == list(ai.FIELDS)
    assert not [f for f in fields if f.yellow]
    assert by_name(fields)["sizes"].value == ["38"]


def test_unknown_section_is_yellow():
    field = check(section="Жіночий одяг / Взуття / Ласти")["section"]
    assert (field.value, field.yellow, field.reason) == (
        "Жіночий одяг / Взуття / Ласти",
        True,
        "розділу немає в довіднику Шафи",
    )


def test_section_from_snapshot_is_not_yellow():
    field = check(section="чоловічий одяг/взуття/кросівки", sizes=["44"], material_upper=[])
    assert (field["section"].value, field["section"].yellow) == (
        "Чоловічий одяг / Взуття / Кросівки",
        False,
    )


def test_adult_size_44_moves_to_men_without_yellow():
    fields = check(sizes=["44"])  # ШІ поставило жіноче; у тексті стать не сказано
    assert (fields["section"].value, fields["section"].yellow) == (
        "Чоловічий одяг / Взуття / Черевики",
        False,
    )
    assert not fields["sizes"].yellow


def test_size_41_fits_women_and_men():
    assert not check(sizes=["41"])["section"].yellow
    assert not check(sizes=["36,5"])["section"].yellow


def test_kids_section_with_kids_size():
    girls = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"
    fields = check(section=girls, sizes=["31"], teen=False)
    assert (fields["section"].yellow, fields["section"].reason) == (True, ai.NO_KID_GENDER)
    assert fields["teen"].value is False
    text = "Кросівки для дівчинки 31 розміру. 500грн"
    fields = by_name(ai.check_answer(answer(section=girls, sizes=["31"], teen=False), NAMES, text))
    assert not fields["section"].yellow


@pytest.mark.parametrize(
    ("sizes", "value", "reason"),
    [
        ([], [], "немає розміру"),
        (["38", "38,5", "39"], ["38", "38,5", "39"], "більше двох розмірів"),
        (["38.5"], ["38,5"], None),
        (["тридцять"], ["тридцять"], "розмір не число"),
        (["38,3"], ["38,3"], "розміру немає у списку Шафи для розділу"),
        (["30"], ["30"], "розміру немає у списку Шафи для розділу"),
        (["41", "42"], ["41", "42"], None),  # вживане: ярлик і на який іде
        ("38", [], "немає у відповіді ШІ"),
    ],
)
def test_sizes(sizes, value, reason):
    field = check(sizes=sizes)["sizes"]
    assert (field.value, field.reason) == (value, reason)
    assert field.yellow is (reason is not None)


def test_accessory_fields():
    fields = check(
        section="Аксесуари / Сумки та рюкзаки / Рюкзаки",
        sizes=["38"],
        gender="для жінок",
        material_upper=["Шкіра", "Замша"],
    )
    assert not fields["section"].yellow
    assert (fields["sizes"].value, fields["sizes"].reason) == ([], "у цього розділу розміру немає")
    assert fields["gender"].value == "Для жінок"
    # CORE-005: «Замша» не названа в тексті — відкинуто, без жовтого
    assert (fields["material_upper"].value, fields["material_upper"].yellow) == (["Шкіра"], False)


def test_gender_and_teen_only_where_they_belong():
    fields = check(gender="Для жінок", teen=True)  # жіночі черевики: ні «Стать», ні «Категорія»
    # QA-001: поля в типу немає — порожнє і не жовте (Шафа його не покаже)
    assert (fields["gender"].value, fields["gender"].yellow) == (None, False)
    assert fields["teen"].value is None


def test_sizes_new_item_only_label_size():
    new_post = POST.replace("в ідеальному стані", "нові")
    fields = by_name(ai.check_answer(answer(sizes=["41", "42"]), NAMES, new_post))
    assert (fields["sizes"].yellow, fields["sizes"].reason) == (True, "нове — лише один розмір")


def test_babies_ukrainian_sizes_and_required_gender():
    babies = "Дитячий одяг / Для малюків / Взуття / Кросівки"
    fields = check(section=babies, sizes=["17,5"], material_upper=[], season=[], gender=None)
    assert not fields["sizes"].yellow
    assert (fields["gender"].yellow, fields["gender"].reason) == (True, "обов'язкове поле Шафи")
    fields = check(section=babies, sizes=["23"], material_upper=[], gender="унісекс")
    assert fields["sizes"].reason == "розміру немає у списку Шафи для розділу"
    assert (fields["gender"].value, fields["gender"].yellow) == ("Унісекс", False)


def test_type_style_and_limits_from_section():
    fields = check(type="челсі", style="Класичний", season=["Весна", "Осінь", "Зима", "Літо"])
    assert (fields["type"].value, fields["type"].yellow) == ("Челсі", False)
    assert (fields["style"].value, fields["style"].yellow) == ("Класичний", False)
    assert not fields["season"].yellow  # до 4
    fields = check(type="Кеди")
    assert fields["type"].reason == "немає у списку Шафи: Кеди"


@pytest.mark.parametrize(
    ("colors", "value", "reason"),
    [
        (["чорний", "Білий"], ["Чорний", "Білий"], None),
        (["Ультрамарин"], [], "кольору немає в довіднику: Ультрамарин"),
        (["Чорний", "Білий", "Сірий"], ["Чорний", "Білий"], None),  # головний перший, до двох
        ([], [], "немає кольору"),
    ],
)
def test_colors(colors, value, reason):
    field = check(colors=colors)["colors"]
    assert (field.value, field.reason) == (value, reason)
    assert field.yellow is (reason is not None)


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Черевики @prodavchynia_test 38 розміру", "у назві контакти або посилання"),
        ("Черевики t.me/kanal 38 розміру", "у назві контакти або посилання"),
        ("Черевики " + "дуже " * 30, f"назва довша за {ai.TITLE_MAX} символів"),
        ("  ", "немає назви"),
    ],
)
def test_title(title, reason):
    field = check(title=title)["title"]
    assert field.yellow and field.reason == reason


def test_title_up_to_150_is_fine():
    title = "Шкіряні черевики pavers " + "дуже гарні " * 8 + "38 розміру"
    assert 100 < len(title) <= ai.TITLE_MAX == 150
    assert not check(title=title)["title"].yellow


def test_season_outside_list():
    field = check(season=["Міжсезоння"])["season"]
    assert (field.value, field.yellow) == (["Міжсезоння"], True)
    assert field.reason == "немає у списку Шафи: Міжсезоння"
    assert check(season="Зима")["season"].value == ["Зима"]  # старий бланк: рядок


@pytest.mark.parametrize(
    ("keywords", "reason"),
    [
        (["черевики", "черевики жіночі", "черевики pavers"], "ключових слів не 4–6"),
        (["a", "b", "c", "d", "@nik"], "у ключових словах контакти"),
        ([], "немає ключових слів"),
    ],
)
def test_keywords(keywords, reason):
    assert check(keywords=keywords)["keywords"].reason == reason


def test_photo_order_changed_is_yellow():
    order = ["IMG_2.JPG", "IMG_1.JPG", "IMG_3.JPG"]
    field = check(photo_order=order)["photo_order"]
    assert (field.value, field.yellow, field.reason) == (order, True, "ШІ переставило фото")


@pytest.mark.parametrize("order", [["IMG_1.JPG", "IMG_2.JPG"], NAMES + ["IMG_9.JPG"], None])
def test_photo_order_not_matching_folder_keeps_folder_order(order):
    field = check(photo_order=order)["photo_order"]
    assert field.value == NAMES
    assert field.reason == "порядок фото від ШІ не збігається з папкою"


def test_doubts_make_fields_yellow_and_keep_code_reason():
    fields = check(
        section="Чоловічий одяг / Взуття / Кросівки",
        sizes=["41"],
        material_upper=[],
        doubts=[
            {"field": "section", "reason": "не видно, жіночі чи чоловічі"},
            {"field": "brand", "reason": "бірка нечітка"},
            {"field": "невідоме", "reason": "x"},
        ],
    )
    assert fields["section"].reason == "ШІ не впевнене: не видно, жіночі чи чоловічі"
    assert (fields["brand"].value, fields["brand"].yellow) == ("Pavers", True)
    assert fields["brand"].reason == "ШІ не впевнене: бірка нечітка"


def test_missing_and_wrong_types_do_not_crash():
    fields = by_name(ai.check_answer({"title": 5, "colors": "Чорний", "doubts": "ні"}, NAMES, POST))
    assert [f.name for f in fields.values()] == list(ai.FIELDS)
    assert fields["title"].yellow and fields["section"].yellow and fields["colors"].yellow
    assert fields["brand"].value is None


def test_prompt_rules_from_text_only():
    prompt = ai.system_prompt()
    assert "лише якщо вони є в тексті поста" in prompt
    assert "«Бутси Adidas …» → «… / Бутси»" in prompt
    assert "лише до 22 розміру включно" in prompt


@pytest.mark.parametrize(
    ("section", "sizes", "reason"),
    [
        (
            "Дитячий одяг / Для малюків / Взуття / Кросівки",
            ["24"],
            "«Для малюків» лише до 22 розміру; " + ai.NO_KID_GENDER,
        ),
        (
            "Дитячий одяг / Для дівчаток / Взуття / Кросівки",
            ["22"],
            "до 22 розміру включно — «Для малюків»",
        ),
        ("Жіночий одяг / Взуття / Кросівки", ["30"], "розмір 30 — дитячий"),
    ],
)
def test_size_hint_reasons(section, sizes, reason):
    field = check(section=section, sizes=sizes, material_upper=[], season=[])["section"]
    assert (field.yellow, field.reason) == (True, reason)


@pytest.mark.parametrize(
    ("lining", "text", "kept"),
    [
        ("Текстиль", "Шкіряні кросівки. Верх - натуральна шкіра. 990грн", []),  # не з тексту
        ("Шкіра", "Кросівки. Верх і всередині - натуральна шкіра. 1500грн", ["Шкіра"]),
        ("Хутро", "Зимові черевики на хутрі. 1200грн", ["Хутро"]),
        ("Фліс", "Черевики з флісом всередині. 900грн", ["Фліс"]),
        (None, "Кросівки. 990грн", []),
    ],
)
def test_lining_must_be_in_text(lining, text, kept):
    field = by_name(ai.check_answer(answer(material_lining=lining), NAMES, text))["material_lining"]
    assert (field.value, field.yellow) == (kept, False)


@pytest.mark.parametrize(
    ("word", "text", "found"),
    [
        ("Шкіра", "шкіряні кросівки", True),
        ("Текстиль", "текстильний верх", True),
        ("Вовна", "вовняна підкладка", True),
        ("Шкіра", "штучна замша", False),
        ("Нубук", "бук", False),
        ("Шкіра", "верх — екошкіра", False),
    ],
)
def test_in_text(word, text, found):
    assert ai.in_text(word, text) is found


# --- CORE-007: менше жовтих — рішення за текстом (формулювання перефразовані) ---

WOMEN_BOOTS = "Жіночий одяг / Взуття / Черевики"
MEN_BOOTS = "Чоловічий одяг / Взуття / Черевики"


def check_text(text, names=NAMES, **changes):
    return by_name(ai.check_answer(answer(**changes), names, text))


def doubt(field, reason):
    return {"field": field, "reason": reason}


@pytest.mark.parametrize(
    ("text", "season"),
    [
        ("Термо черевики Columbia 38 розміру. 1100грн", ["Зима"]),
        ("Черевики на натуральному хутрі, 38 розмір. 1400грн", ["Зима"]),
        ("Утеплені черевики Ecco 38 розмір. 900грн", ["Зима"]),
        ("Уги замшеві оригінал 38 розміру. 1800грн", ["Зима"]),
        ("Черевики Geox на флісі 38 розмір. 800грн", ["Зима"]),
        ("Черевики з мембраною, не промокають, 38 розмір. 1300грн", ["Демісезон"]),
        ("Черевики Lowa gore-tex 38 розміру. 2000грн", ["Демісезон"]),
        ("Демісезонні черевики на флісі 38 розмір. 700грн", ["Демісезон"]),
        ("Зимові черевики Salomon gore-tex 38 розміру. 2100грн", ["Зима"]),
    ],
)
def test_season_from_text_is_not_yellow(text, season):
    fields = check_text(text, season=["Літо"], doubts=[doubt("season", "не видно з фото")])
    assert (fields["season"].value, fields["season"].yellow) == (season, False)


def test_season_yellow_only_when_text_contradicts_itself():
    fields = check_text("Літні черевики на хутрі 38 розмір. 600грн", season=[])
    assert fields["season"].yellow
    assert fields["season"].reason == "у тексті і літо, і ознаки зими — перевір сезон"


def test_season_when_text_is_silent_is_ai_without_yellow():
    fields = check_text(POST, season=[], doubts=[doubt("season", "з тексту не зрозуміло")])
    assert (fields["season"].value, fields["season"].yellow) == ([], False)


@pytest.mark.parametrize(
    ("section", "size", "text", "wanted"),
    [
        (MEN_BOOTS, "38", POST, WOMEN_BOOTS),  # ≤ 39 — жіноче
        (MEN_BOOTS, "39,5", POST, WOMEN_BOOTS),
        (WOMEN_BOOTS, "43", POST, MEN_BOOTS),  # ≥ 43 — чоловіче
        (WOMEN_BOOTS, "39", "Чоловічі черевики Pavers 39 розміру. 900грн", MEN_BOOTS),  # текст
        (MEN_BOOTS, "44", "Жіночі черевики великого 44 розміру. 900грн", WOMEN_BOOTS),
        (MEN_BOOTS, "38", "Черевики унісекс 38 розміру. 900грн", WOMEN_BOOTS),
    ],
)
def test_adult_gender_by_text_then_size(section, size, text, wanted):
    fields = check_text(
        text,
        section=section,
        sizes=[size],
        doubts=[doubt("section", "не видно, жіночі чи чоловічі")],
    )
    assert (fields["section"].value, fields["section"].yellow) == (wanted, False)


def test_sizes_40_to_42_left_to_ai():
    fields = check_text(POST, section=MEN_BOOTS, sizes=["41"])
    assert (fields["section"].value, fields["section"].yellow) == (MEN_BOOTS, False)
    fields = check_text(
        POST, section=MEN_BOOTS, sizes=["41"], doubts=[doubt("section", "не видно, чиї")]
    )
    assert (fields["section"].value, fields["section"].yellow) == (MEN_BOOTS, True)


def test_code_decided_group_keeps_ai_doubt_about_type():
    fields = check_text(POST, sizes=["38"], doubts=[doubt("section", "може, це ботильйони")])
    assert fields["section"].yellow
    assert fields["section"].reason == "ШІ не впевнене: може, це ботильйони"


BABIES = "Дитячий одяг / Для малюків / Взуття / Кросівки"
GIRLS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"


def test_babies_never_from_23():
    text = "Кросівки дитячі 23 розміру. 400грн"
    fields = check_text(text, section=BABIES, sizes=["23"], material_upper=[], gender="Унісекс")
    assert fields["section"].yellow
    assert fields["section"].reason.startswith("«Для малюків» лише до 22 розміру")
    text = "Кросівки для дівчинки 24 розміру. 400грн"
    fields = check_text(text, section=BABIES, sizes=["24"], material_upper=[], gender="Унісекс")
    assert (fields["section"].value, fields["section"].yellow) == (GIRLS, False)


def test_babies_up_to_22():
    text = "Кросівки 21 розміру. 300грн"
    fields = check_text(text, section=BABIES, sizes=["21"], material_upper=[], gender="Унісекс")
    assert not fields["section"].yellow


def test_kids_35_36_with_adult_signs_are_women():
    text = "Кросівки на невеликому каблуку 36 розміру. 600грн"
    fields = check_text(text, section=GIRLS, sizes=["36"], material_upper=[], season=[])
    assert fields["section"].value == "Жіночий одяг / Взуття / Кросівки"
    assert not fields["section"].yellow
    text = "Дитячі кросівки 36 розміру. 600грн"  # дитяче слово — як у v1.4
    fields = check_text(text, section=GIRLS, sizes=["36"], material_upper=[], season=[])
    assert (fields["section"].value, fields["section"].reason) == (GIRLS, ai.NO_KID_GENDER)


GIRLS_SNEAKERS = "Дитячий одяг / Для дівчаток / Взуття / Кросівки"


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("Кросівки Nike 36 розміру, стан хороший. 700грн", "36"),
        ("Кеди Converse, 37 розмір, устілка 23,5см. 550грн", "37"),
        ("Шкіряні черевики 35 розміру, носила один сезон. 900грн", "35"),
        ("Мокасини Geox, розмір 36,5. Майже нові. 800грн", "36,5"),
    ],
)
def test_35_and_up_without_kid_words_is_women_not_yellow(text, size):
    """SITE-003 6: пілот — ШІ ставило 36–37 у «Для дівчаток» і жовте teen; у неї 35+ — доросле."""
    fields = check_text(
        text, section=GIRLS_SNEAKERS, sizes=[size], material_upper=[], season=[], teen=True,
        doubts=[doubt("teen", "не видно, підліткове чи дитяче"),
                doubt("section", "35-37 — дитяче чи доросле")],
    )  # fmt: skip
    assert fields["section"].value == "Жіночий одяг / Взуття / Кросівки"
    assert not fields["section"].yellow
    assert (fields["teen"].value, fields["teen"].yellow) == (None, False)


@pytest.mark.parametrize(
    "text",
    [
        "Дитячі кеди 36 розміру. 400грн",
        "Підліткові кросівки 35 розміру. 500грн",
        "Шкільні туфлі 36 розміру. 450грн",
        "Кросівки для дівчинки чи хлопчика, 35 розмір. 500грн",
    ],
)
def test_35_36_with_kid_words_stays_kids_as_before(text):
    fields = check_text(text, section=GIRLS_SNEAKERS, sizes=["35"], material_upper=[], season=[])
    assert fields["section"].value.startswith("Дитячий одяг")
    assert fields["section"].yellow


def test_23_34_without_gender_as_before():
    text = "Кросівки 34 розміру. 400грн"
    fields = check_text(text, section=GIRLS_SNEAKERS, sizes=["34"], material_upper=[], season=[])
    assert (fields["section"].value, fields["section"].reason) == (GIRLS_SNEAKERS, ai.NO_KID_GENDER)


@pytest.mark.parametrize(
    ("text", "ai_sizes", "sizes"),
    [
        ("Черевики. Вказаний 39 розмір, ідуть на 38. 900грн", ["39", "38"], ["38"]),
        ("Черевики. На ярлику 40, ідуть на 39,5. 900грн", ["39,5"], ["39,5"]),
        ("Нові черевики. Вказаний 39 розмір, ідуть на 38. 900грн", ["39"], ["38"]),
    ],
)
def test_label_rule_applied_by_code(text, ai_sizes, sizes):
    """CORE-009 A2: на Шафу — «ідуть на» і для вживаного, і для нового."""
    fields = check_text(text, sizes=ai_sizes, doubts=[doubt("sizes", "ярлик і реальний різні")])
    assert (fields["sizes"].value, fields["sizes"].yellow) == (sizes, False)


def test_single_size_in_text_ignores_ai_size_doubt():
    fields = check_text(POST, sizes=["38"], doubts=[doubt("sizes", "ярлика на фото не видно")])
    assert not fields["sizes"].yellow
    fields = check_text(POST, sizes=["38,5"], doubts=[doubt("sizes", "ярлик 38,5")])
    assert fields["sizes"].yellow  # ШІ дало інший, ніж у тексті, — справжній сумнів


def test_unpaired_stays_yellow():
    text = "Черевики 38 розмір, правий трохи більший. 600грн"
    fields = check_text(text, sizes=["38"], doubts=[doubt("sizes", "розпаровка")])
    assert fields["sizes"].yellow


@pytest.mark.parametrize(
    ("section", "lining", "text", "kept"),
    [
        (WOMEN_BOOTS, ["натуральне хутро"], "Черевики на натуральному хутрі. 900грн", ["Хутро"]),
        (WOMEN_BOOTS, ["хутро"], "Черевики, всередині хутро. 900грн", ["Хутро"]),
        (WOMEN_BOOTS, ["Фліс"], "Черевики на флісі. 900грн", ["Фліс"]),
        (WOMEN_BOOTS, ["Текстиль"], "Черевики, всередині текстиль. 900грн", ["Текстиль"]),
        (WOMEN_BOOTS, ["Шкіра"], "Черевики, всередині шкіра. 900грн", ["Шкіра"]),
        (WOMEN_BOOTS, ["Мембрана"], "Черевики з мембраною. 900грн", []),  # немає в довіднику
    ],
)
def test_lining_synonyms_to_catalog(section, lining, text, kept):
    fields = check_text(
        text, section=section, material_lining=lining, doubts=[doubt("material_lining", "?")]
    )
    assert (fields["material_lining"].value, fields["material_lining"].yellow) == (kept, False)


def test_lining_fur_names_differ_by_section():
    girls_uggs = "Дитячий одяг / Для дівчаток / Взуття / Уггі"
    spec = ai.catalog.characteristic(girls_uggs, "material_lining")
    assert "Натуральне хутро" in spec["choices"] and "Хутро" not in spec["choices"]
    text = "Уги для дівчинки 30 розміру, хутро. 900грн"
    fields = check_text(text, section=girls_uggs, material_lining=["Хутро"], material_upper=[])
    assert fields["material_lining"].value == ["Натуральне хутро"]


def test_colors_doubt_is_not_yellow():
    fields = check_text(POST, colors=["Сірий"], doubts=[doubt("colors", "графітовий чи сірий")])
    assert (fields["colors"].value, fields["colors"].yellow) == (["Сірий"], False)


def test_one_photo_is_never_yellow():
    fields = check_text(
        POST, names=["1.jpg"], photo_order=["2.jpg"], doubts=[doubt("photo_order", "збоку?")]
    )
    assert (fields["photo_order"].value, fields["photo_order"].yellow) == (["1.jpg"], False)


def test_sapozhky_are_boots():
    text = "Замшеві сапожки 38 розміру. 900грн"
    fields = check_text(text, material_upper=["Замша"])
    assert (fields["section"].value, fields["section"].yellow) == (
        "Жіночий одяг / Взуття / Чоботи",
        False,
    )


def test_dutyky_only_when_text_says():
    dutyky = "Жіночий одяг / Взуття / Дутики"
    fields = check_text("Зимові чоботи 38 розміру. 900грн", section=dutyky)
    assert fields["section"].yellow
    assert not check_text("Дутики 38 розміру. 900грн", section=dutyky)["section"].yellow


def test_prompt_has_core_007_rules():
    prompt = ai.system_prompt()
    for phrase in (
        "«на флісі»",
        "≤ 39 — жіноче",
        "23 і більше — ніколи",
        "сапожки",
        "Різнокольоровий",
    ):
        assert phrase in prompt, phrase
