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
    "material_lining": None,
    "season": "Демісезон",
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


def message(text, stop_reason="end_turn", usage=(15000, 600)):
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]),
    )


class FakeClient:
    """Підставний клієнт API: повертає задану відповідь або кидає задану помилку."""

    def __init__(self, result):
        self.result = result
        self.requests = []
        self.messages = self

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
    assert "Жіночий одяг / Взуття / … : Черевики" in prompt


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
    assert set(ai.SCHEMA["properties"]) == {*ai.FIELDS, "doubts"}


def test_estimate_counts_photos_exactly():
    request = ai.build_request(POST, photos())
    usage = ai.estimate(request, photos())
    assert usage.input_tokens > 3 * 1170
    assert usage.output_tokens == ai.OUTPUT_TOKENS_ESTIMATE


def test_cost_in_micro_dollars():
    assert ai.Usage(15000, 600).cost_micro_usd == 15000 * 2 + 600 * 10


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


def test_unverified_section_is_yellow():
    field = check(section="чоловічий одяг/взуття/черевики", sizes=["44"])["section"]
    assert (field.value, field.yellow, field.reason) == (
        "Чоловічий одяг / Взуття / Черевики",
        True,
        "розділ не звірено з довідником",
    )


def test_size_hint_contradicts_section():
    fields = check(sizes=["44"])
    assert fields["section"].yellow
    assert fields["section"].reason == "розмір 44 зазвичай з іншого розділу"
    assert not fields["sizes"].yellow  # 44 є у жіночому списку форми (34–46)


def test_size_41_fits_women_and_men():
    assert not check(sizes=["41"])["section"].yellow
    assert not check(sizes=["36,5"])["section"].yellow


def test_kids_section_with_kids_size():
    fields = check(section="Дитячий одяг / Для дівчаток / Взуття / Кеди", sizes=["31"], teen=False)
    assert not fields["section"].yellow
    assert fields["teen"].value is False


@pytest.mark.parametrize(
    ("sizes", "value", "reason"),
    [
        ([], [], "немає розміру"),
        (["38", "38,5", "39"], ["38", "38,5", "39"], "більше двох розмірів"),
        (["38.5"], ["38,5"], None),
        (["тридцять"], ["тридцять"], "розмір не число"),
        (["38,3"], ["38,3"], "розміру немає у списку розділу"),
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
    assert (fields["sizes"].value, fields["sizes"].reason) == ([], "у аксесуарів розміру немає")
    assert fields["gender"].value == "Для жінок"
    assert fields["material_upper"].reason == "матеріалу немає в довіднику: Замша"


def test_gender_and_teen_only_where_they_belong():
    fields = check(gender="Для жінок", teen=True)
    assert fields["gender"].value is None
    assert fields["teen"].value is None


@pytest.mark.parametrize(
    ("colors", "value", "reason"),
    [
        (["чорний", "Білий"], ["Чорний", "Білий"], None),
        (["Ультрамарин"], [], "кольору немає в довіднику: Ультрамарин"),
        (["Чорний", "Білий", "Сірий"], ["Чорний", "Білий", "Сірий"], "більше двох кольорів"),
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


def test_season_outside_list():
    field = check(season="Міжсезоння")["season"]
    assert (field.value, field.yellow) == ("Міжсезоння", True)


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
        section="Чоловічий одяг / Взуття / Кеди",
        sizes=["41"],
        doubts=[
            {"field": "section", "reason": "не видно, жіночі чи чоловічі"},
            {"field": "brand", "reason": "бірка нечітка"},
            {"field": "невідоме", "reason": "x"},
        ],
    )
    assert fields["section"].reason == (
        "розділ не звірено з довідником; ШІ не впевнене: не видно, жіночі чи чоловічі"
    )
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
            "розділ не звірено з довідником; Для малюків лише до 22 розміру",
        ),
        (
            "Дитячий одяг / Для малюків / Взуття / Кросівки",
            ["22"],
            "розділ не звірено з довідником",
        ),
        (
            "Дитячий одяг / Для дівчаток / Взуття / Кеди",
            ["22"],
            "розмір 22 зазвичай з іншого розділу",
        ),
        (
            "Чоловічий одяг / Взуття / Кеди",
            ["38"],
            "розділ не звірено з довідником; розмір 38 зазвичай з іншого розділу",
        ),
    ],
)
def test_size_hint_reasons(section, sizes, reason):
    field = check(section=section, sizes=sizes)["section"]
    assert (field.yellow, field.reason) == (True, reason)


@pytest.mark.parametrize(
    ("lining", "text", "yellow"),
    [
        ("Текстиль", "Шкіряні кросівки. Верх - натуральна шкіра. 990грн", True),
        ("Шкіра", "Кросівки. Верх і всередині - натуральна шкіра. 1500грн", False),
        ("Хутро", "Зимові черевики на хутрі. 1200грн", False),
        ("Фліс", "Черевики з флісом всередині. 900грн", False),
        (None, "Кросівки. 990грн", False),
    ],
)
def test_lining_must_be_in_text(lining, text, yellow):
    field = by_name(ai.check_answer(answer(material_lining=lining), NAMES, text))["material_lining"]
    assert field.value == lining
    assert field.yellow is yellow
    assert field.reason == ("немає в тексті" if yellow else None)


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
