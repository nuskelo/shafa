"""ROBOT-013: обов'язковий блок «Виберіть розмір *» у шапок, якого немає у знімку (пост 6454, 22.10)."""

from dataclasses import replace

import pytest

from shafa import ai
from shafa.robot import browser, form, sync
from shafa.robot.form import FieldMissing, PageDriver, execute
from shafa.robot.plan import OTHER_SIZE, Step, plan
from test_core010 import HATS, approved_gloves
from test_robot003_page import chromium  # noqa: F401  (фікстура)
from test_robot005_page import clicked, page_html, sizes
from test_robot_form import FakeDriver, bare_session

# Кнопки блоку на скриншоті форми «Шапки» (6454-після-кліку.png)
BUTTONS = (
    "XXS/54 XS/55 S/56 M/57 L/58 XL/59 XXL/60 XXXL/61 One size Інший 58 57 56 55 40 35"
).replace("One size", "One_size")
HAT_BUTTONS = [b.replace("One_size", "One size") for b in BUTTONS.split()]
HINT = '<div class="hint">для вживаних — можливі дві опції; для нових — одна</div>'
HEADING = "Виберіть розмір <span>*</span> <span>(+5%)</span>"
HAT_BLOCK = sizes(HEADING, *HAT_BUTTONS).replace("</div><button", f"</div>{HINT}<button", 1)
COLORS = sizes("Виберіть до 2 відтінків *", "Білий", "Сірий", "Чорний")
HAT_TEXT = "Шапка michael kors в новому стані. Висота шапки 21см. 750грн"


@pytest.fixture
def make(chromium, monkeypatch):  # noqa: F811
    monkeypatch.setattr(browser, "pause", lambda *a: None)
    monkeypatch.setattr(sync, "SIZE_WAIT", 1)
    pages = []

    def build(*fields, script=""):
        page = chromium.new_page()
        page.set_content(page_html(*fields) + script)
        pages.append(page)
        session = bare_session()
        session.page = page
        return PageDriver(session)

    yield build
    for page in pages:
        page.close()


def hat_card(conn, root, text=HAT_TEXT, **fields):
    return approved_gloves(conn, root, text=text, section=HATS, sizes=[], **fields)


def live_step(steps):
    return next((s for s in steps if s.kind == "size_live"), None)


# --- sync / живе читання ---


def test_read_size_block_sees_the_hat_block_with_heading_decorations(make):
    block = sync.read_size_block(make(HAT_BLOCK, COLORS).page)
    assert block == {"title": "Виберіть розмір", "required": True, "choices": HAT_BUTTONS}


def test_read_size_block_waits_for_late_buttons(make):
    late = """<script>setTimeout(() => {
      const f = document.querySelector('.field');
      for (const n of ['S/56', 'Інший']) { const b = document.createElement('button');
        b.type = 'button'; b.textContent = n; f.appendChild(b); }
    }, 700);</script>"""
    driver = make(sizes(HEADING), script=late)
    assert sync.read_size_block(driver.page, wait=0) is None
    assert sync.read_size_block(driver.page, wait=3)["choices"] == ["S/56", "Інший"]


def test_read_size_block_without_block_is_none(make):
    assert sync.read_size_block(make(COLORS).page) is None


def test_size_block_entry_drops_the_percent_note():
    entry = sync.size_block_entry("Виберіть розмір * (+5%)", ["S/56"])
    assert entry["title"] == "Виберіть розмір" and entry["required"]


# --- план ---


def test_hat_plan_has_a_live_size_step(conn, root):
    steps, _ = plan(hat_card(conn, root), root)
    assert live_step(steps) is not None
    assert not {s.kind for s in steps} & {"size_block", "sizes", "size_system"}


def test_gloves_plan_keeps_the_snapshot_block_and_has_no_live_step(conn, root):
    gloves, _ = plan(approved_gloves(conn, root, sizes=[]), root)
    assert live_step(gloves) is None and any(s.kind == "size_block" for s in gloves)


def test_shoes_have_no_live_size_step(conn, root):
    from test_robot_plan import approved

    steps, _ = plan(approved(conn, root), root)
    assert live_step(steps) is None


def resolve(step, choices=HAT_BUTTONS):
    notes: list[str] = []
    block = {"title": "Виберіть розмір", "required": True, "choices": choices}
    return step.value(block, notes), notes


def test_live_step_without_size_in_text_picks_other(conn, root):
    values, notes = resolve(live_step(plan(hat_card(conn, root), root)[0]))
    assert values == [OTHER_SIZE]
    assert any("прочитано з форми" in n for n in notes)


def test_live_step_takes_the_letter_from_the_text(conn, root):
    card = hat_card(conn, root, text="Шапка Michael Kors розмір M. Стан ідеальний. 750грн")
    values, _ = resolve(live_step(plan(card, root)[0]))
    assert values == ["M/57"]


def test_seller_edit_wins_over_the_text(conn, root):
    card = hat_card(conn, root, text="Шапка Michael Kors розмір M. 750грн")
    card.fields["sizes"] = replace(card.fields["sizes"], value=["L"], edited=True)
    assert resolve(live_step(plan(card, root)[0]))[0] == ["L/58"]


def test_block_choice_matches_the_letter_part_only():
    assert ai.block_choice("M", ["S/56", "M/57"]) == "M/57"
    assert ai.block_choice("m", ["S", "M"]) == "M"
    assert ai.block_choice("XL", ["XXL/60", "XL/59"]) == "XL/59"
    assert ai.block_choice("Z", ["S/56"]) is None


# --- форма ---


def test_execute_fills_the_live_block_with_other(make, conn, root):
    driver = make(HAT_BLOCK, COLORS)
    execute([live_step(plan(hat_card(conn, root), root)[0])], driver)
    assert clicked(driver) == ["Інший"]


def test_execute_fills_the_letter_from_the_text(make, conn, root):
    card = hat_card(conn, root, text="Шапка Michael Kors розмір M. 750грн")
    driver = make(HAT_BLOCK, COLORS)
    execute([live_step(plan(card, root)[0])], driver)
    assert clicked(driver) == ["M/57"]


def test_no_block_on_the_form_is_not_an_error(make, conn, root):
    driver = make(COLORS)
    assert execute([live_step(plan(hat_card(conn, root), root)[0])], driver) == []
    assert clicked(driver) == []


def test_required_block_without_a_pressable_choice_stops_before_the_click(make, conn, root):
    no_other = sizes(HEADING, "S/56", "M/57")  # «Інший» немає, розміру в тексті теж
    driver = make(no_other)
    with pytest.raises(FieldMissing, match="Виберіть розмір") as error:
        execute([live_step(plan(hat_card(conn, root), root)[0])], driver)
    assert "Інший" in error.value.details
    assert clicked(driver) == []


def test_execute_passes_live_step_to_the_driver_as_the_snapshot_block():
    class Live(FakeDriver):
        def size_block(self):
            return {"title": "Виберіть розмір", "required": True, "choices": ["S", OTHER_SIZE]}

    driver = Live(None)
    step = Step("size_live", "Виберіть розмір", lambda block, notes: [OTHER_SIZE], required=False)
    form.execute([step], driver)
    assert driver.calls == [("pick", "Виберіть розмір", OTHER_SIZE)]
