"""ROBOT-006 у справжньому Chromium: пошук бренду на фальшивій сторінці (список react-select).

Без браузера Playwright (CI) — пропуск; логіку без браузера покриває `test_robot006.py`.
"""

from shafa.robot import browser, form
from shafa.robot.form import PageDriver
from test_robot003_page import chromium  # noqa: F401 — фікстура браузера
from test_robot_form import bare_session

PAGE = """<!doctype html><meta charset="utf-8"><body><form>
<div class="field"><div class="label">Бренд</div>
  <div class="select" data-options="{options}" data-delay="{delay}"></div></div>
<div class="field"><div class="label">Тип</div>
  <div class="select__control"><div class="chip">Додайте заголовок(до +11%)
    <div role="button" class="select__multi-value__remove"><svg width="8" height="8"></svg></div>
  </div><input role="combobox"></div></div>
<div class="field"><div class="label">Сезон</div>
  <div class="select__control"><div class="chip">Зима
    <div role="button" class="select__multi-value__remove"><svg width="8" height="8"></svg></div>
  </div><input role="combobox"></div></div>
</form>
<script>
window.picked = []; window.added = 0;
for (const box of document.querySelectorAll('.select')) {{
  const all = box.dataset.options.split('|'), delay = Number(box.dataset.delay);
  box.innerHTML = '<div class="select__control"><div class="values"></div>'
    + '<input role="combobox"></div><div class="select__menu" hidden></div>';
  const input = box.querySelector('input'), menu = box.querySelector('.select__menu');
  const show = () => {{
    const flat = t => t.toLowerCase().replace(/\\s+/g, ' ')
      .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');  // пошук Шафи — без діакритики
    const text = flat(input.value);
    const shown = all.filter(o => flat(o).includes(text));
    menu.innerHTML = '';
    if (!shown.length) {{
      const add = document.createElement('div');  // «Додати бренд» — клікати не можна ніколи
      add.className = 'select__option'; add.setAttribute('role', 'option');
      add.textContent = 'Додати бренд «' + input.value + '»';
      add.onclick = () => {{ window.added++; }};
      menu.appendChild(add);
    }}
    for (const o of shown) {{
      const el = document.createElement('div');
      el.className = 'select__option'; el.setAttribute('role', 'option');
      el.innerHTML = o.replace(/ /g, '&nbsp;').replace('|', ' ');  // нерозривний пробіл
      el.onclick = () => {{ window.picked.push(o); menu.hidden = true; }};
      menu.appendChild(el);
    }}
    menu.hidden = false;
  }};
  input.addEventListener('input', () => setTimeout(show, delay));
  input.addEventListener('click', () => {{}});
  input.addEventListener('keydown', e => {{ if (e.key === 'Escape') menu.hidden = true; }});
}}
</script></body>"""


def make(chromium, monkeypatch, options, delay=0):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    page = chromium.new_page()
    page.set_content(PAGE.format(options="|".join(options), delay=delay))
    session = bare_session()
    session.page = page
    return PageDriver(session), page


def test_brand_with_no_break_space_is_found_and_clicked(chromium, monkeypatch):  # noqa: F811
    driver, page = make(chromium, monkeypatch, ["Peter Storm", "Peter Storm Kids", "Ecco"])
    assert driver.brand("Peter Storm") is True
    assert page.evaluate("window.picked") == ["Peter Storm"]
    assert driver.brand_miss is None
    page.close()


def test_brand_that_appears_after_three_seconds_is_found(chromium, monkeypatch):  # noqa: F811
    driver, page = make(chromium, monkeypatch, ["Peter Storm"], delay=3000)
    assert driver.brand("peter storm") is True
    assert page.evaluate("window.picked") == ["Peter Storm"]
    page.close()


def test_missing_brand_waits_then_reports_what_it_saw_and_never_adds(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(form, "BRAND_WAIT", 2)
    driver, page = make(chromium, monkeypatch, ["Peter Storm Kids", "Snowdon"])
    assert driver.brand("Snowdonia") is False
    miss = driver.brand_miss
    assert miss.name == "Snowdonia"
    assert miss.options == ["Додати бренд «Snowdonia»"]
    assert "select__menu" in miss.markup and "Бренд" in miss.markup
    assert page.evaluate("window.added") == 0 and page.evaluate("window.picked") == []
    assert page.locator("input[role=combobox]").first.input_value() == ""
    page.close()


def test_similar_brand_is_not_clicked(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(form, "BRAND_WAIT", 2)
    driver, page = make(chromium, monkeypatch, ["Peter Storm Kids", "Peter Stormer"])
    assert driver.brand("Peter Storm") is False
    assert any("\xa0" in o for o in driver.brand_miss.options)
    assert [o.replace("\xa0", " ") for o in driver.brand_miss.options] == [
        "Peter Storm Kids",
        "Peter Stormer",
    ]  # у журналі — «як є», з \xa0
    assert page.evaluate("window.picked") == []
    page.close()


def test_hint_is_not_written_as_value_of_untouched_field(chromium, monkeypatch):  # noqa: F811
    driver, page = make(chromium, monkeypatch, ["Ecco"])
    chips = driver.page.evaluate(form.CHIPS, driver._find("Тип"))
    assert chips == ["Додайте заголовок(до +11%)"]  # форма показує підказку як «чип»
    assert driver.untouched(["Тип"]) == []
    assert driver.untouched(["Тип", "Сезон"]) == ["на формі Шафа поставила сама: Сезон: Зима"]
    page.close()
