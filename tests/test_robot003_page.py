"""ROBOT-003 у справжньому Chromium на фальшивій сторінці (списки як react-select на Шафі).

Перевіряє скрипти сторінки (`CHIPS`, `OPTIONS`, `LIST_FIELDS`) і `PageDriver`. Без браузера
Playwright (CI) — пропуск: логіку покривають тести `test_robot003.py`.
"""

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver
from test_robot_form import bare_session

PAGE = """<!doctype html><meta charset="utf-8"><body><form>
<div class="field"><div class="label">Розмір</div>
  <button type="button">Європейський</button><button type="button">37</button></div>
<div class="field"><div class="label">Матеріал</div>
  <div class="select" data-options="Атлас|Гума|Замша|Текстиль|Шкіра|Штучна шкіра"
       data-chips="Шкіра"></div></div>
<div class="field"><div class="label">Довжина устілки</div>
  <div class="select" data-options="23|24|24,5|25" data-chips="24"></div></div>
<div class="field"><div class="label">Принт</div>
  <div class="select" data-options="Без принту|Зебра" data-broken="1"></div></div>
<div class="field"><div class="label">Введіть ключові слова</div>
  <div class="select" data-options="" data-chips="кросівки 37 розмір"></div></div>
<input name="price" value="150"><input name="titleUk" value="">
</form>
<script>
for (const box of document.querySelectorAll('.select')) {
  const all = box.dataset.options ? box.dataset.options.split('|') : [];
  let chips = box.dataset.chips ? box.dataset.chips.split('|') : [];
  box.innerHTML = '<div class="select__control"><div class="values"></div>'
    + '<input role="combobox"></div><div class="select__menu" hidden></div>';
  const values = box.querySelector('.values'), input = box.querySelector('input'),
        menu = box.querySelector('.select__menu');
  const draw = () => {
    values.innerHTML = '';
    for (const c of chips) {
      const chip = document.createElement('div');
      chip.className = 'select__multi-value';
      chip.innerHTML = `<div class="select__multi-value__label">${c}</div>`
        + `<div role="button" class="select__multi-value__remove" aria-label="Remove ${c}">`
        + '<svg width="8" height="8"></svg></div>';
      chip.lastChild.onclick = () => { chips = chips.filter(x => x !== c); draw(); };
      values.appendChild(chip);
    }
  };
  const open = () => {
    const text = input.value.toLowerCase();
    const shown = (text && box.dataset.broken) ? []
      : all.filter(o => !chips.includes(o) && o.toLowerCase().includes(text));
    menu.innerHTML = shown.length ? '' :
      '<div class="select__menu-notice--no-options">Доступних опцій немає</div>';
    for (const o of shown) {
      const el = document.createElement('div');
      el.className = 'select__option'; el.setAttribute('role', 'option'); el.textContent = o;
      el.onclick = () => { chips.push(o); input.value = ''; menu.hidden = true; draw(); };
      menu.appendChild(el);
    }
    menu.hidden = false;
  };
  input.addEventListener('input', open);
  input.addEventListener('click', open);
  input.addEventListener('keydown', e => { if (e.key === 'Escape') menu.hidden = true; });
  draw();
}
document.querySelector('[name=price]').addEventListener('input', e => {
  if (document.body.dataset.stubborn) e.target.value = '150';
});
</script></body>"""


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import Error, sync_playwright

    playwright = sync_playwright().start()
    try:
        browser_ = playwright.chromium.launch()
    except Error:
        playwright.stop()
        pytest.skip("немає браузера Playwright")
    yield browser_
    browser_.close()
    playwright.stop()


@pytest.fixture
def driver(chromium, monkeypatch):
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(form, "LIST_WAIT", 1)
    page = chromium.new_page()
    page.set_content(PAGE)
    session = bare_session()
    session.page = page
    yield PageDriver(session)
    page.close()


def chips(driver, label):
    mark = driver._find(label)
    return driver.page.evaluate(form.CHIPS, mark)


def test_selected_chip_is_read_and_kept(driver):
    assert chips(driver, "Матеріал") == ["Шкіра"]
    assert driver.pick("Матеріал", ["Шкіра"]) == []
    assert chips(driver, "Матеріал") == ["Шкіра"]


def test_foreign_chip_removed_own_chosen(driver):
    notes = driver.pick("Довжина устілки", ["24,5"])
    assert notes == ["Довжина устілки: Шафа поставила сама «24» — прибрано"]
    assert chips(driver, "Довжина устілки") == ["24,5"]
    driver.verify()


def test_more_values_added_to_existing_chip(driver):
    driver.pick("Матеріал", ["Шкіра", "Замша"])
    assert chips(driver, "Матеріал") == ["Шкіра", "Замша"]


def test_empty_list_after_typing_falls_back_to_full_list(driver):
    driver.pick("Принт", ["зебра"])
    assert chips(driver, "Принт") == ["Зебра"]


def test_value_not_in_list_lists_options(driver):
    with pytest.raises(FieldMissing) as error:
        driver.pick("Матеріал", ["Шкіра", "Мех"])
    assert error.value.details == (
        "поле «Матеріал»; вибрано: Шкіра; у списку (5): Атлас, Гума, Замша, Текстиль, Штучна шкіра"
    )


def test_buttons_still_clicked(driver):
    assert driver.pick("Розмір", ["37"]) == []


def test_untouched_logged(driver):
    notes = driver.untouched(["Довжина устілки", "Принт", "Сезон"])
    assert notes == ["на формі Шафа поставила сама: Довжина устілки: 24"]


def test_price_overwritten_and_stubborn_price_stops(driver):
    driver.fill("price", "1200", "Ціна")
    assert driver.page.input_value("[name=price]") == "1200"
    driver.page.evaluate("document.body.dataset.stubborn = '1'")
    with pytest.raises(FieldMissing, match="Ціна: на формі 150, треба 1300"):
        driver.fill("price", "1300", "Ціна")


def test_size_labels_skip_keyword_chips(driver):
    with pytest.raises(FieldMissing) as error:
        driver.pick("Розмір", ["39"])
    assert "кросівки 37 розмір" not in error.value.details
    assert "підписи розміру: Розмір" in error.value.details
