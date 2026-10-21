"""ROBOT-012 у справжньому Chromium: поле-список, що після вибору максимуму прибирає input
(Шафа так робить, коли значень більше не можна), і бренд з діакритикою.

Без браузера Playwright (CI) — пропуск; `brand_key` без браузера покриває `test_robot012.py`.
"""

import time

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver
from test_robot003_page import chromium  # noqa: F401 — фікстура браузера
from test_robot006_page import make as make_brand
from test_robot_form import bare_session

PAGE = """<!doctype html><meta charset="utf-8"><body><form>
<div class="field"><div class="label">Матеріал підкладки</div>
  <div class="select" data-options="Фліс|Хутро|Текстиль" data-max="1"></div></div>
<div class="field"><div class="label">Довжина устілки</div>
  <div class="select" data-options="26,5 см|27 см|27,5 см|28 см" data-max="2" data-chips="26,5 см"></div></div>
<div class="field"><div class="label">Сезон</div>
  <div class="select" data-options="Зима|Літо" data-max="1" data-chips="Зима"></div></div>
<input name="price" value="150">
</form>
<script>
for (const box of document.querySelectorAll('.select')) {
  const all = box.dataset.options.split('|'), max = Number(box.dataset.max);
  let chips = box.dataset.chips ? box.dataset.chips.split('|') : [];
  box.innerHTML = '<div class="select__control"><div class="values"></div><span class="slot"></span>'
    + '</div><div class="select__menu" hidden></div>';
  const values = box.querySelector('.values'), slot = box.querySelector('.slot'),
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
    slot.innerHTML = '';
    if (chips.length < max) {  // максимум досягнуто — поля введення більше немає
      const input = document.createElement('input');
      input.setAttribute('role', 'combobox');
      input.addEventListener('input', open);
      input.addEventListener('click', open);
      slot.appendChild(input);
    }
  };
  const open = () => {
    const input = slot.querySelector('input');
    const text = input ? input.value.toLowerCase() : '';
    const shown = all.filter(o => !chips.includes(o) && o.toLowerCase().includes(text));
    menu.innerHTML = '';
    for (const o of shown) {
      const el = document.createElement('div');
      el.className = 'select__option'; el.setAttribute('role', 'option'); el.textContent = o;
      el.onclick = () => { chips.push(o); menu.hidden = true; draw(); };
      menu.appendChild(el);
    }
    menu.hidden = false;
  };
  draw();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.select__menu').forEach(m => m.hidden = true);
});
</script></body>"""


@pytest.fixture
def driver(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(form, "LIST_WAIT", 1)
    page = chromium.new_page()
    page.set_default_timeout(8000)  # без виправлення тест падає за секунди, не за 30 с
    page.set_content(PAGE)
    session = bare_session()
    session.page = page
    yield PageDriver(session)
    page.close()


def chips(driver, label):
    return driver.page.evaluate(form.CHIPS, driver._find(label))


def test_list_that_loses_its_input_after_choice_does_not_wait(driver):
    started = time.monotonic()
    assert driver.pick("Матеріал підкладки", ["Фліс"]) == []
    assert time.monotonic() - started < form.FIELD_WAIT / 1000 + 3
    assert chips(driver, "Матеріал підкладки") == ["Фліс"]
    driver.verify()


def test_two_values_with_max_two_both_kept(driver):
    driver.pick("Довжина устілки", ["26,5 см", "27 см"])
    assert chips(driver, "Довжина устілки") == ["26,5 см", "27 см"]
    driver.verify()


def test_second_attempt_with_value_already_chosen_and_no_input(driver):
    # «Перевірити ще раз»: Шафа вже поставила значення, input зник — нічого не набираємо
    started = time.monotonic()
    assert driver.pick("Сезон", ["Зима"]) == []
    assert time.monotonic() - started < 5
    assert chips(driver, "Сезон") == ["Зима"]


def test_foreign_chip_removed_then_own_chosen_when_input_was_gone(driver):
    notes = driver.pick("Сезон", ["Літо"])
    assert notes == ["Сезон: Шафа поставила сама «Зима» — прибрано"]
    assert chips(driver, "Сезон") == ["Літо"]


def test_extra_value_over_max_is_a_clear_stop_not_a_timeout(driver):
    with pytest.raises(FieldMissing) as error:
        driver.pick("Сезон", ["Зима", "Літо"])
    assert "вибрано: Зима" in error.value.details


def test_abandon_on_field_without_input_does_not_wait(driver):
    started = time.monotonic()
    driver.abandon("Сезон")
    assert time.monotonic() - started < 5


def test_unfinished_price_is_caught_before_submit(driver):
    driver.fill("price", "1500", "Ціна")
    driver.page.evaluate("document.querySelector('[name=price]').value = '150'")
    with pytest.raises(FieldMissing, match="Ціна: на формі 150, треба 1500"):
        driver.verify()


BRANDS = ["Waldläufer", "Waldläufer Kids", "Ecco"]


def test_brand_with_umlaut_is_chosen_without_diacritics(chromium, monkeypatch):  # noqa: F811
    driver, page = make_brand(chromium, monkeypatch, BRANDS)
    assert driver.brand("Waldlaufer") is True
    assert page.evaluate("window.picked") == ["Waldläufer"]
    assert driver.brand_miss is None
    page.close()


def test_brand_typed_with_umlaut_matches_plain_variant(chromium, monkeypatch):  # noqa: F811
    driver, page = make_brand(chromium, monkeypatch, ["Waldlaufer"])
    assert driver.brand("Waldläufer") is True
    assert page.evaluate("window.picked") == ["Waldlaufer"]
    page.close()


def test_parenti_is_not_picked_among_similar(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(form, "BRAND_WAIT", 2)
    driver, page = make_brand(chromium, monkeypatch, ["MiO Parenti", "Parenti's"])
    assert driver.brand("Parenti") is False
    assert page.evaluate("window.picked") == []
    assert page.evaluate("window.added") == 0
    page.close()
