"""ROBOT-012 без браузера: порівняння брендів без діакритики — лише точний збіг."""

import pytest

from shafa.robot.form import brand_key


@pytest.mark.parametrize(
    "ours, theirs",
    [
        ("Waldlaufer", "Waldläufer"),
        ("Waldläufer", "Waldlaufer"),
        ("Hogl", "Högl"),
        ("Gabor", "GABOR"),
        ("Peter\xa0Storm", "Peter Storm"),
        ("Cafe Noir", "Café Noir"),
    ],
)
def test_brand_key_ignores_diacritics_case_and_spaces(ours, theirs):
    assert brand_key(ours) == brand_key(theirs)


@pytest.mark.parametrize(
    "ours, theirs",
    [("Parenti", "MiO Parenti"), ("Parenti", "Parenti's"), ("Ecco", "Ecco Kids")],
)
def test_brand_key_does_not_merge_similar_brands(ours, theirs):
    assert brand_key(ours) != brand_key(theirs)
