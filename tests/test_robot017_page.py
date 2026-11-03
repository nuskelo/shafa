"""ROBOT-017 у справжньому Chromium: розмір малюків — вкладки «Американський» (вибрана) і
«🇺🇦Український» з прапорцем-емодзі. Без браузера Playwright (CI) — пропуск; чисту функцію
`plain` покриває тест нижче.
"""

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver, plain
from shafa.robot.plan import Step
from test_robot003_page import chromium  # noqa: F401 — фікстура браузера
from test_robot_form import bare_session

# Вкладки систем і кнопки розмірів вибраної вкладки — як у формі «Для малюків» на Шафі.
# `FLAG` — розмітка прапорця в кнопці: текст без пробілу, з пробілом, у <span>, з variation selector.
PAGE = """<!doctype html><meta charset="utf-8"><body><form>
<div class="field"><div class="label">Розмір *</div>
  <div class="tabs"></div><div class="sizes"></div></div>
<input name="price" value="150">
</form>
<script>
const SYSTEMS = JSON.parse(document.body.dataset.systems || 'null') || {
  'Американський': ['0,5', '1', '6,5', 'Інший'],
  [FLAG + 'Український']: ['19', '20', '20,5', 'Інший'],
};
let system = Object.keys(SYSTEMS)[0], size = '';
const tabs = document.querySelector('.tabs'), sizes = document.querySelector('.sizes');
const draw = () => {
  tabs.innerHTML = sizes.innerHTML = '';
  for (const name of Object.keys(SYSTEMS)) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = name === system ? 'tab active' : 'tab';
    b.innerHTML = name.replace(/^(\\p{Regional_Indicator}+\\ufe0f?\\s?)/u, '<span>$1</span>');
    b.onclick = () => { system = name; size = ''; draw(); };
    tabs.appendChild(b);
  }
  for (const s of SYSTEMS[system]) {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = s; b.className = s === size ? 'size active' : 'size';
    b.onclick = () => { size = s; draw(); };
    sizes.appendChild(b);
  }
  document.body.dataset.system = system; document.body.dataset.size = size;
};
draw();
</script></body>"""


def page_html(flag: str) -> str:
    return PAGE.replace("FLAG", repr(flag))


@pytest.fixture
def make(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(form, "LIST_WAIT", 1)
    pages = []

    def build(html: str) -> PageDriver:
        page = chromium.new_page()
        page.set_default_timeout(4000)  # без виправлення тест падає за секунди, не за 30 с
        page.set_content(html)
        pages.append(page)
        session = bare_session()
        session.page = page
        return PageDriver(session)

    yield build
    for page in pages:
        page.close()


def state(driver) -> tuple[str, str]:
    body = driver.page.locator("body")
    return body.get_attribute("data-system"), body.get_attribute("data-size")


def kids_steps() -> list[Step]:
    return [Step("size_system", "Розмір", "Український"), Step("sizes", "Розмір", ["20"])]


@pytest.mark.parametrize(
    "flag", ["\U0001f1fa\U0001f1e6", "\U0001f1fa\U0001f1e6 ", "\U0001f1fa\U0001f1e6️ "]
)
def test_kids_ukrainian_tab_with_flag_and_size_20(make, flag):
    driver = make(page_html(flag))
    assert state(driver) == ("Американський", "")
    form.execute(kids_steps(), driver)  # у кінці — verify
    system, size = state(driver)
    assert system.strip("\U0001f1fa\U0001f1e6️ ") == "Український"
    assert size == "20"


def test_value_missing_is_still_an_error(make):
    driver = make(page_html("\U0001f1fa\U0001f1e6"))
    with pytest.raises(FieldMissing, match="Розмір: Польський"):
        driver.pick("Розмір", ["Польський"])


def test_no_loose_match(make):
    # «Український» не збігається з кнопкою «Не Український», хоч і міститься в ній
    driver = make(PAGE.replace("FLAG + 'Український'", "'Не Український'"))
    with pytest.raises(FieldMissing):
        driver.pick("Розмір", ["Український"])


def test_adult_european_tab_unchanged(make):
    html = PAGE.replace("[FLAG + 'Український']: ['19', '20', '20,5', 'Інший']", "'Інший': ['x']")
    html = html.replace(
        "'Американський': ['0,5', '1', '6,5', 'Інший']", "'Європейський': ['37', '38']"
    )
    driver = make(html)
    form.execute(
        [Step("size_system", "Розмір", "Європейський"), Step("sizes", "Розмір", ["38"])], driver
    )
    assert state(driver) == ("Європейський", "38")


def test_plain_strips_flags_and_extra_spaces():
    assert plain("\U0001f1fa\U0001f1e6Український") == "Український"
    assert plain(" \U0001f1fa\U0001f1e6️  Український ") == "Український"
    assert plain("Американський") == "Американський"
    assert plain("37") == "37"
