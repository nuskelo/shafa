"""ROBOT-004 у справжньому Chromium: чипи у двох інших розмітках і розмітка поля при зупинці.

(а) «×» текстом, кнопка очищення поля «×» поруч із полем введення; (б) SVG у `div` без тексту,
кнопка очищення поля — SVG поруч із чипами, меню варіантів — поза блоком поля (як портал).
Без браузера Playwright (CI) — пропуск.
"""

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver
from test_robot003_page import chromium  # noqa: F401 — фікстура браузера
from test_robot_form import bare_session

PAGE = """<!doctype html><meta charset="utf-8"><body data-variant="{variant}">
<nav><ul class="menu"><li>Головна</li><li>Мої речі</li></ul></nav>
<form>
<div class="field"><div class="label">Матеріал <span class="hint"><svg width="8" height="8">
  </svg></span></div>
  <div class="select" data-options="Гума|Замша|Шкіра" data-chips="Шкіра"></div>
  <input type="hidden" name="csrf" value="СЕКРЕТ-123"></div>
<div class="field"><div class="label">Довжина устілки</div>
  <div class="select" data-options="23|24|24,5" data-chips="24"></div></div>
<div class="field"><div class="label">Сезон</div>
  <div class="ctl"><div class="single">Демісезон</div><div class="indicators">
    <div class="select__indicator select__clear-indicator"><svg width="8" height="8"></svg></div>
  </div></div><input role="combobox"></div>
</form>
<script>
const a = document.body.dataset.variant === 'a';
for (const box of document.querySelectorAll('.select')) {{
  const all = box.dataset.options.split('|');
  let chips = box.dataset.chips ? box.dataset.chips.split('|') : [];
  box.innerHTML = a
    ? '<div class="ctl"><div class="vals"></div><input role="combobox">'
      + '<span class="clear-all">×</span></div>'
    : '<div class="ctl"><div class="vals"></div><div class="actions">'
      + '<div class="clear" role="button"><svg width="8" height="8"></svg></div>'
      + '<svg width="8" height="8"></svg></div></div><input role="combobox">';
  const menu = document.createElement('div');
  menu.className = 'dropdown-menu'; menu.setAttribute('role', 'listbox'); menu.hidden = true;
  (a ? box : document.body).appendChild(menu);
  const vals = box.querySelector('.vals'), input = box.querySelector('input');
  const draw = () => {{
    vals.innerHTML = '';
    for (const c of chips) {{
      const chip = document.createElement(a ? 'span' : 'div');
      chip.innerHTML = a ? `${{c}}<span class="tag-x">×</span>`
        : `<span>${{c}}</span><div class="pill-btn"><svg width="8" height="8"></svg></div>`;
      chip.lastChild.onclick = () => {{ chips = chips.filter(x => x !== c); draw(); }};
      vals.appendChild(chip);
    }}
  }};
  box.querySelector(a ? '.clear-all' : '.clear').onclick = () => {{ chips = []; draw(); }};
  const open = () => {{
    const text = input.value.toLowerCase();
    const shown = all.filter(o => !chips.includes(o) && o.toLowerCase().includes(text));
    menu.innerHTML = shown.length ? '' : '<div class="no-options">Доступних опцій немає</div>';
    for (const o of shown) {{
      const el = document.createElement('div');
      el.setAttribute('role', 'option'); el.textContent = o;
      el.onclick = () => {{ chips.push(o); input.value = ''; menu.hidden = true; draw(); }};
      menu.appendChild(el);
    }}
    menu.hidden = false;
  }};
  input.addEventListener('input', open);
  input.addEventListener('click', open);
  input.addEventListener('keydown', e => {{ if (e.key === 'Escape') menu.hidden = true; }});
  draw();
}}
</script></body>"""


@pytest.fixture(params=["a", "b"])
def driver(request, chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(form, "LIST_WAIT", 1)
    page = chromium.new_page()
    page.set_content(PAGE.format(variant=request.param))
    session = bare_session()
    session.page = page
    yield PageDriver(session)
    page.close()


def chips(driver, label):
    return driver.page.evaluate(form.CHIPS, driver._find(label))


def test_chips_read_without_clear_button_and_label_icon(driver):
    assert chips(driver, "Матеріал") == ["Шкіра"]
    assert chips(driver, "Довжина устілки") == ["24"]
    assert chips(driver, "Сезон") == []  # одне значення без чипа: «очистити поле» — не чип


def test_selected_chip_kept_and_more_added(driver):
    assert driver.pick("Матеріал", ["Шкіра"]) == []
    driver.pick("Матеріал", ["Шкіра", "Замша"])
    assert chips(driver, "Матеріал") == ["Шкіра", "Замша"]
    driver.verify()


def test_foreign_chip_removed_by_its_own_button(driver):
    notes = driver.pick("Довжина устілки", ["24,5"])
    assert notes == ["Довжина устілки: Шафа поставила сама «24» — прибрано"]
    assert chips(driver, "Довжина устілки") == ["24,5"]
    assert chips(driver, "Матеріал") == ["Шкіра"]  # кнопку очищення поля не натиснуто ніде


def test_list_stop_carries_field_markup_without_hidden_values(driver):
    with pytest.raises(FieldMissing) as error:
        driver.pick("Матеріал", ["Мех"])
    assert error.value.field == "Матеріал"
    markup = error.value.markup
    assert markup.startswith("<!-- блок поля -->\n")
    assert "Шкіра" in markup and 'name="csrf"' in markup
    assert "СЕКРЕТ-123" not in markup
    assert "Гума" in markup and "Замша" in markup  # відкрите меню варіантів
    assert "Мої речі" not in markup  # меню сайту — не меню варіантів
    if driver.page.evaluate("document.body.dataset.variant") == "b":
        assert "\n<!-- меню варіантів -->\n" in markup  # меню поза блоком поля


def test_verify_stop_carries_field_markup(driver):
    driver.pick("Матеріал", ["Шкіра"])
    driver.page.evaluate("document.querySelector('.ctl').closest('.field')"
                         ".querySelector('.vals').innerHTML = ''")  # fmt: skip
    with pytest.raises(FieldMissing, match="Матеріал: на формі порожньо") as error:
        driver.verify()
    assert error.value.field == "Матеріал"
    assert "<!-- блок поля -->" in error.value.markup
