"""ROBOT-006: бренд, якого робот не знайшов, не зупиняє сесію; бренду немає ніде — поле порожнє."""

import pytest

from shafa import ai
from shafa.robot import control, form
from shafa.robot.form import BrandMiss, BrandMissing, brand_key, execute, is_hint
from shafa.robot.plan import Step, plan
from test_robot_form import FakeDriver
from test_robot_plan import BLANK, approved


def blank(brand):
    return [f for f in BLANK if f.name != "brand"] + [ai.Field("brand", brand)]


# --- План: чи є бренд у знімку ---


def test_plan_marks_brand_known_to_shafa_snapshot(conn, root):
    known = {s.kind: s for s in plan(approved(conn, root, blank=blank("peter storm")), root)[0]}
    assert (known["brand"].value, known["brand"].known) == ("Peter Storm", True)


def test_plan_marks_brand_outside_snapshot_unknown(conn, root):
    steps, _ = plan(approved(conn, root, blank=blank("Snowdonia")), root)
    step = next(s for s in steps if s.kind == "brand")
    assert (step.value, step.known) == ("Snowdonia", False)


# --- execute ---


def test_known_brand_not_found_live_is_a_robot_failure_when_strict():
    with pytest.raises(BrandMissing, match="Peter Storm"):
        execute([Step("brand", "Бренд", "Peter Storm", known=True)], missing_driver(), strict=True)


def test_known_brand_not_found_live_is_only_a_note_in_dry_fill():
    notes = execute([Step("brand", "Бренд", "Peter Storm", known=True)], missing_driver())
    assert notes == ["Бренд «Peter Storm» є у довіднику Шафи, але робот його не знайшов"]


def test_brand_nowhere_is_left_empty_even_when_strict():
    notes = execute([Step("brand", "Бренд", "Snowdonia")], missing_driver("Snowdonia"), strict=True)
    assert notes == ["бренд Snowdonia не в списку Шафи — лишено порожнім"]


def missing_driver(*names):
    driver = FakeDriver(None)
    driver.no_brands = set(names) or {"Peter Storm"}
    return driver


# --- Екран, нормалізація, підказки ---


def test_photos_stop_has_its_own_text_not_form_changed():
    assert "фото" in control.LAST["photos"]
    assert control.LAST["photos"] != control.LAST["site"]


def test_brand_key_ignores_spaces_zero_width_and_case():
    assert brand_key("Peter\xa0 Storm\n") == brand_key("peter storm")
    assert brand_key("Peter \u200bStorm") == brand_key("PETER STORM")
    assert brand_key("Peter Storm Kids") != brand_key("Peter Storm")


def test_hints_are_not_values():
    assert is_hint("Додайте заголовок(до +11%)") and is_hint("Виберіть тип")
    assert not is_hint("Челсі") and not is_hint("Peter Storm")


def test_brand_miss_details_use_repr():
    miss = BrandMiss("X", ["a\xa0b"])
    assert miss.details() == "бренд 'X'; варіанти (1): ['a\\xa0b']"
    assert form.BrandMiss is BrandMiss
