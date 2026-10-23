"""ROBOT-016: необов'язкове поле «Тип» (Челсі, Берці) не заповнювалось.

Причини в коді: підказка форми «Додайте заголовок(до +11%)» бралась за вибране значення (і робот
клікав її «×»), підпис «Тип» чіплявся за «Тип товару», порожній список при першому відкритті
вважався остаточним. Сторінка — у справжньому Chromium (без нього — пропуск); логіка — без браузера.
"""

import pytest

from shafa.robot import browser, form
from shafa.robot.form import FieldMissing, PageDriver, pick_values
from shafa.robot.plan import Step
from test_robot003_page import chromium  # noqa: F401 — фікстура браузера
from test_robot_form import bare_session

PAGE = """<!doctype html><meta charset="utf-8"><body><form>
<div class="quality"><div class="title">Тип товару</div>
  <button type="button">Покращити оголошення</button></div>
<div class="field"><div class="label">Тип</div>
  <div class="select" data-options="Берці|Дербі|Челсі" data-lazy="__LAZY__"></div></div>
<div class="field"><div class="label">Матеріал</div>
  <div class="select" data-options="Замша|Шкіра" data-lazy=""></div></div>
</form>
<script>
window.hintClicks = 0;
for (const box of document.querySelectorAll('.select')) {
  const all = box.dataset.options.split('|');
  let lazy = !!box.dataset.lazy, chips = [];
  box.innerHTML = '<div class="select__control"><div class="values"></div>'
    + '<input role="combobox"></div><div class="select__menu" hidden></div>';
  const values = box.querySelector('.values'), input = box.querySelector('input'),
        menu = box.querySelector('.select__menu');
  const draw = () => {
    values.innerHTML = '';
    for (const c of chips.length ? chips : ['Додайте заголовок(до +11%)']) {
      const chip = document.createElement('div');
      chip.className = 'select__multi-value';
      const hint = !chips.length;
      chip.innerHTML = `<div class="select__multi-value__label">${c}</div>`
        + `<div role="button" class="select__multi-value__remove"><svg width="8" height="8"></svg></div>`;
      chip.lastChild.onclick = () => {
        if (hint) window.hintClicks++; else { chips = chips.filter(x => x !== c); draw(); }
      };
      values.appendChild(chip);
    }
  };
  const open = () => {
    const text = input.value.toLowerCase();
    const shown = lazy ? [] : all.filter(o => !chips.includes(o) && o.toLowerCase().includes(text));
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
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { menu.hidden = true; lazy = false; }  // список «дозрів» після закриття
  });
  draw();
}
</script></body>"""


def make(chromium, monkeypatch, tmp_path, lazy=""):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(form, "LIST_WAIT", 1)
    page = chromium.new_page()
    page.set_content(PAGE.replace("__LAZY__", lazy))
    session = bare_session()
    session.page = page
    driver = PageDriver(session)
    driver.screens = tmp_path
    return driver, page


def real_chips(driver, label):
    return [c for c in driver.page.evaluate(form.CHIPS, driver._find(label)) if not form.is_hint(c)]


def test_chelsea_chosen_in_real_field_next_to_quality_block(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path)
    assert driver.pick("Тип", ["Челсі"]) == []
    assert real_chips(driver, "Тип") == ["Челсі"]
    assert page.evaluate("window.hintClicks") == 0  # «×» підказки не чіпали
    driver.verify()
    page.close()


def test_label_is_found_exactly_not_by_prefix(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path)
    mark = driver._find("Тип")
    block = page.locator(f"[data-robot-block='{mark}']")
    assert block.locator("input[role=combobox]").count() == 1
    assert "Тип товару" not in block.inner_text()
    page.close()


def test_hint_is_not_a_chip_of_widget(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path)
    widget = driver._widget("Тип")
    assert widget.chips() == []  # у сторінки CHIPS підказку бачить, віджет — ні
    assert page.evaluate(form.CHIPS, widget.mark) == ["Додайте заголовок(до +11%)"]
    assert widget.listed
    page.close()


def test_empty_list_on_first_open_is_reopened(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path, lazy="1")
    assert driver.pick("Тип", ["Берці"]) == []
    assert real_chips(driver, "Тип") == ["Берці"]
    page.close()


def test_material_next_to_hint_still_works(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path)
    assert driver.pick("Матеріал", ["Замша"]) == []
    assert real_chips(driver, "Матеріал") == ["Замша"]
    assert page.evaluate("window.hintClicks") == 0
    page.close()


def test_optional_failure_saves_markup_and_screenshot_once(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path)
    step = Step("choice", "Тип", ["Мокасини"], required=False)  # у списку такого немає
    first = form.optional(driver, step)
    assert "не знайдено Мокасини" in first[0] and "Розмітка поля: " in first[0]
    dumps = sorted(tmp_path.glob("*-поле-Тип.html"))
    assert len(dumps) == 1 and sorted(tmp_path.glob("*-поле-Тип.png"))
    assert first[0].endswith(dumps[0].name)
    text = dumps[0].read_text(encoding="utf-8")
    assert "select__menu" in text and "Додайте заголовок" in text and "Мокасини" in text
    again = form.optional(driver, step)  # те саме поле в тій самій сесії — файлів не додається
    assert "Розмітка поля" not in again[0]
    assert len(list(tmp_path.glob("*-поле-Тип.html"))) == 1
    page.close()


def test_other_field_gets_its_own_dump(chromium, monkeypatch, tmp_path):  # noqa: F811
    driver, page = make(chromium, monkeypatch, tmp_path)
    form.optional(driver, Step("choice", "Тип", ["Мокасини"], required=False))
    form.optional(driver, Step("choice", "Матеріал", ["Тканина"], required=False))
    assert len(list(tmp_path.glob("*-поле-Матеріал.html"))) == 1
    page.close()


# --- Без браузера ---


class Lazy:
    """Список не показує варіантів, поки його не закрили й не відкрили знову."""

    listed = True

    def __init__(self):
        self.closed = 0
        self.picked = []

    def chips(self):
        return self.picked

    def remove(self, chip):
        raise AssertionError("прибирати нічого")

    def press(self, value):
        return False

    def search(self, text):
        return ["Берці", "Челсі"] if self.closed else []

    def choose(self, option):
        self.picked.append(option)

    def close(self):
        self.closed += 1

    def describe(self):
        return ""


def test_pick_values_reopens_empty_list_once():
    widget = Lazy()
    assert pick_values(widget, "Тип", ["Челсі"]) == []
    assert widget.picked == ["Челсі"] and widget.closed == 2  # одне повторне відкриття + фінальне


def test_pick_values_does_not_reopen_when_list_has_options():
    class Plain(Lazy):
        def search(self, text):
            return ["Берці"]

    widget = Plain()
    with pytest.raises(FieldMissing):
        pick_values(widget, "Тип", ["Челсі"])
    assert widget.closed == 0  # варіанти були, потрібного немає — це не «не вантажиться»


def test_optional_without_diagnose_keeps_old_note():
    class Old:
        def pick(self, label, values):
            raise FieldMissing(f"{label}: {values[0]}", "у списку (0): порожньо")

    notes = form.optional(Old(), Step("choice", "Тип", ["Берці"], required=False))
    assert notes == [
        "Необов'язкове поле «Тип» не заповнено: на формі не знайдено Берці (у списку (0): порожньо)"
    ]
