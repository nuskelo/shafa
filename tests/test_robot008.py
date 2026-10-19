# ruff: noqa: F401, F811
"""ROBOT-008: стан драйвера між Картками, «Длина стельки», старі правила — не до роботи."""

import pytest

from shafa import catalog, rules
from shafa.db import Status
from shafa.robot import lang
from shafa.robot.form import FieldMissing, PageDriver
from test_robot003 import FormPage, driver_for
from test_robot_publish import (
    PubDriver,
    add,
    env,
    new_cards_allowed,
    other_item,
    quiet,
    row,
)

# --- A. Стан між Картками ---


def test_second_card_with_empty_field_passes_verify_after_reset():
    """3660: у 1-ї підкладка «Текстиль», у 2-ї `[]` — `verify` не має чекати «Текстиль»."""
    page = FormPage(blocks={"Матеріал підкладки": ["Текстиль"]})
    driver = driver_for(page)
    driver.lists["Матеріал підкладки"] = ["Текстиль"]
    driver.verify()

    page.blocks["Матеріал підкладки"] = []  # нова форма: друга Картка без підкладки
    driver.reset()
    driver.verify()


def test_without_reset_the_old_card_breaks_verify():
    """Те, що було до фікса: драйвер пам'ятає попередню Картку."""
    page = FormPage(blocks={"Матеріал підкладки": []})
    driver = driver_for(page)
    driver.lists["Матеріал підкладки"] = ["Текстиль"]
    with pytest.raises(FieldMissing, match="Матеріал підкладки: на формі порожньо, треба Текстиль"):
        driver.verify()


def test_reset_clears_everything_the_card_left():
    driver = driver_for(FormPage())
    driver.typed.add("кеди")
    driver.texts["price"] = ("Ціна", "1200")
    driver.lists["Сезон"] = ["Літо"]
    driver.brand_miss = object()
    driver.reset()
    assert (driver.typed, driver.texts, driver.lists, driver.brand_miss) == (set(), {}, {}, None)


class Counting(PubDriver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = []

    def reset(self):
        self.log.append("reset")

    def fill(self, name, value, label):
        self.log.append("fill")


def test_robot_resets_the_driver_before_each_card(env):
    """Без `driver.reset()` у `_one` цей тест червоніє: драйвер живе всю сесію."""
    add(env, "1342", 1342, "2025-03-01T10:00:00")
    other_item(env, add(env, "1400", 1400, "2025-05-01T10:00:00"))

    def make_driver(session):
        env.driver = Counting(session, env.conn)
        return env.driver

    env.make_driver = make_driver
    report = env.run()
    assert len(report.published) == 2, env.lines

    log = env.driver.log
    assert log.count("reset") == 2
    assert log[0] == "reset" and log.index("fill") > 0
    assert [i for i, x in enumerate(log) if x == "reset"][1] > log.index("fill")


# --- B. «Длина стельки» і підписи характеристик ---


def test_russian_label_of_insole_is_known():
    assert "Длина стельки" in lang.variants("Довжина устілки")
    assert lang.equivalent("Длина стельки", "Довжина устілки")


def test_every_characteristic_title_of_the_snapshot_is_covered():
    """Підпис поля зі знімка без російської пари (чи запису в SAME) — червоний тест."""
    titles = {
        c["title"] for e in catalog.snapshot()["sections"].values() for c in e["characteristics"]
    }
    missing = sorted(t for t in titles if not lang.covered(t))
    assert missing == []
    assert len(titles) > 30  # охоронець дивиться на справжній знімок


def test_robot_titles_have_russian_variants():
    for names in catalog.FIELD_TITLES.values():
        for title in names:
            if title not in ("Сезон", "Стиль", "Тип", "Мембрана"):  # однакові обома мовами
                assert len(lang.variants(title)) > 1, title


def test_page_driver_finds_russian_insole_block():
    page = FormPage(blocks={"Длина стельки": []})
    driver = driver_for(page)
    assert driver._find("Довжина устілки") is not None


# --- C. Схвалена за старими правилами — не до роботи ---


@pytest.mark.parametrize("version", [None, rules.RULES_VERSION - 1])
def test_card_with_old_rules_is_not_published(env, version):
    item = add(env)
    with env.conn:
        env.conn.execute("UPDATE items SET rules_version = ? WHERE id = ?", (version, item))

    report = env.run()

    assert (row(env.conn, item)["status"], row(env.conn, item)["problem_code"]) == (
        Status.PROBLEM,
        "stale_rules",
    )
    assert report.published == [] and env.driver.submits == []


def test_current_rules_card_is_published(env):
    item = add(env)
    assert env.run().published and row(env.conn, item)["status"] == Status.PUBLISHED


def test_recheck_takes_stale_problem_back_to_review(env):
    from shafa import fill

    item = add(env)
    with env.conn:
        env.conn.execute("UPDATE items SET rules_version = NULL WHERE id = ?", (item,))
    env.run()
    assert row(env.conn, item)["problem_code"] == "stale_rules"

    assert fill.recheck(env.conn, [item]) == 1
    after = row(env.conn, item)
    assert after["status"] == Status.REVIEW and after["rules_version"] == rules.RULES_VERSION
