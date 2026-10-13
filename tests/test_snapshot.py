"""Справжній знімок довідників Шафи в пакеті (не фікстура)."""

import json
from pathlib import Path

from shafa import catalog

REAL = Path(catalog.__file__).resolve().parent / "data" / "shafa-catalog.json"


def test_real_snapshot_is_complete():
    real = json.loads(REAL.read_text(encoding="utf-8"))
    sections = real["sections"]

    assert real["checked"] and real["tree_version"]
    assert tuple(real["colors"]) == catalog.COLORS
    assert real["conditions"][:2] == ["Новий", "Ідеальний"]
    assert len(sections) >= 100
    groups = {path.rsplit(" / ", 1)[0] for path in sections}
    assert groups == {
        "Жіночий одяг / Взуття",
        "Чоловічий одяг / Взуття",
        "Дитячий одяг / Для дівчаток / Взуття",
        "Дитячий одяг / Для хлопчиків / Взуття",
        "Дитячий одяг / Для малюків / Взуття",
        "Аксесуари / Сумки та рюкзаки",
        "Аксесуари / Гаманці та портмоне",
        "Аксесуари / Ремені та пояси",
    }
    assert "Дитячий одяг / Для хлопчиків / Взуття / Бутси" in sections
    assert len(real["brands"]) > 5000 and "Skechers" in real["brands"]
    for entry in sections.values():
        assert all(c["choices"] for c in entry["characteristics"])
