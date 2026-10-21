"""CORE-016: «термо» — мембранне взуття, а не зима. Сезон за типом взуття, без ШІ."""

import json

import pytest

from shafa import ai, catalog, fill, review, rules
from shafa.__main__ import main
from shafa.db import Status
from test_ai import NAMES, answer, by_name
from test_catalog import WOMEN_SNEAKERS
from test_core008 import item, put_edit
from test_core009 import blank
from test_review import add_card

BOOTS = "Жіночий одяг / Взуття / Черевики"
DEMI, WINTER = "Демісезон", "Зима"
BOTH = [DEMI, WINTER]

# Справжні перші рядки постів (6566, 6939 і подібні) + ціна, щоб текст був повним.
SNEAKERS_KEEN = "Термо кросівки Keen в ідеальному стані. 38 розмір. 1500грн"
SNEAKERS_LOWA = "Термо кросівки Lowa з мембраною gore-tex. 38 розмір. 1700грн"
SNEAKERS_VADO = "Термо ккросівки VADO в ідеальному стані. 38 розмір. 900грн"
BOOTS_MEINDL = "Шкіряні термо черевики Meindl з мембраною gore-tex. 38 розмір. 2500грн"
BOOTS_MERRELL = "Зимові термо черевики Merrell. 38 розмір. 1800грн"
BOOTS_VIKING = "Зимові термочеревики Viking. 38 розмір. 1900грн"
BOOTS_CONVERSE = "Термо черевики Converse на хутрі. 38 розмір. 1400грн"
BOOTS_NO_FUR = "Термо черевики Converse без хутра. 38 розмір. 1400грн"
BOOTS_STORM = "Нові термо черевики Peter Storm. В наявності декілька розмірів. 800грн"


@pytest.mark.parametrize(
    ("text", "season"),
    [
        (SNEAKERS_KEEN, [DEMI]),
        (SNEAKERS_LOWA, [DEMI]),
        (SNEAKERS_VADO, [DEMI]),  # «ккросівки» — опечатка 6939
        ("Термо-кросівки Salomon. 900грн", [DEMI]),
        ("Термо кеди Converse. 900грн", [DEMI]),
        (BOOTS_MEINDL, BOTH),
        ("Термочеревики Columbia. 900грн", BOTH),  # злито
        ("Термо чоботи Sorel. 900грн", BOTH),
        ("Термо сапожки Ecco. 900грн", BOTH),
        ("Термо ботильйони Ecco. 900грн", BOTH),
        (BOOTS_STORM, BOTH),
        ("Термо Keen 38 розмір. 900грн", BOTH),  # тип невідомий
        (BOOTS_MERRELL, [WINTER]),
        (BOOTS_VIKING, [WINTER]),
        (BOOTS_CONVERSE, [WINTER]),
        (BOOTS_NO_FUR, BOTH),  # «без хутра» — не ознака зими
        ("Кросівки термо, всередині фліс. 900грн", [WINTER]),
        ("Термо кросівки утеплені. 900грн", [WINTER]),
        ("Демісезонні термо черевики. 900грн", [DEMI]),  # сезон названо прямо
        ("Літні термо кросівки. 900грн", ["Літо"]),
        ("Кросівки з мембраною. 900грн", [DEMI]),  # без «термо» — як було
        ("Кросівки Nike. 900грн", None),
    ],
)
def test_season_rule(text, season):
    assert rules.season(text) == (season, None)


def test_termo_type_falls_back_to_section():
    assert rules.season("Термо Keen 38 розмір. 900грн", WOMEN_SNEAKERS) == ([DEMI], None)
    assert rules.season("Термо Keen 38 розмір. 900грн", BOOTS) == (BOTH, None)
    # тип у тексті сильніший за розділ
    assert rules.season(BOOTS_MEINDL, WOMEN_SNEAKERS) == (BOTH, None)


def test_contradiction_stays_yellow():
    _, doubt = rules.season("Літні черевики на хутрі. 900грн")
    assert doubt and "літо" in doubt


def fields_for(text, section, season=("Зима",)):
    return by_name(ai.check_answer(answer(section=section, season=list(season)), NAMES, text))


def test_ai_cannot_turn_termo_into_winter():
    for text, section in ((SNEAKERS_KEEN, WOMEN_SNEAKERS), (BOOTS_MEINDL, BOOTS)):
        wrong = fields_for(text, section, season=["Зима"])["season"]
        assert not wrong.yellow
    assert fields_for(SNEAKERS_KEEN, WOMEN_SNEAKERS)["season"].value == [DEMI]
    assert fields_for(BOOTS_MEINDL, BOOTS)["season"].value == BOTH
    assert fields_for(BOOTS_MERRELL, BOOTS, season=[DEMI])["season"].value == [WINTER]


def test_season_limited_to_section_values(monkeypatch, tmp_path):
    """У розділі немає «Демісезон» — ставимо лише те, що є (`check_values`)."""
    data = json.loads(catalog.SNAPSHOT.read_text(encoding="utf-8"))
    for entry in data["sections"].values():
        for c in entry["characteristics"]:
            if c["title"] == "Сезон":
                c["choices"] = [v for v in c["choices"] if v != DEMI]
    path = tmp_path / "shafa-catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "SNAPSHOT", path)
    catalog.snapshot.cache_clear()
    try:
        assert fields_for(BOOTS_MEINDL, BOOTS)["season"].value == [WINTER]
        assert fields_for(BOOTS_MEINDL, BOOTS)["season"].yellow is False
    finally:
        catalog.snapshot.cache_clear()


def test_prompt_no_longer_says_termo_is_winter():
    line = next(
        row for row in ai.PROMPT_FILE.read_text(encoding="utf-8").splitlines()
        if row.startswith("**season**")
    )  # fmt: skip
    assert "«термо», «хутро»" not in line
    assert "мембранне взуття, а не зима" in line


# --- Наявні Картки: перерахунок без ШІ ---


def old_blank(text, section, season=("Зима",)):
    """Бланк, як його зробив код до CORE-016: «термо» → Зима."""
    fields = ai.check_answer(answer(section=section, season=["Демісезон"]), NAMES, text)
    return [ai.Field("season", list(season)) if f.name == "season" else f for f in fields]


def add(conn, root, n, text, section, status=Status.REVIEW, season=("Зима",)):
    return add_card(
        conn, root, folder=str(n), post_id=n, status=status, text=text,
        blank=old_blank(text, section, season),
    )  # fmt: skip


def season_of(conn, item_id):
    return blank(conn, item_id)["season"][0]


@pytest.fixture
def cards(conn, root):
    return {
        "keen": add(conn, root, 6566, SNEAKERS_KEEN, WOMEN_SNEAKERS),
        "meindl": add(conn, root, 6567, BOOTS_MEINDL, BOOTS, status=Status.APPROVED),
        "merrell": add(conn, root, 6568, BOOTS_MERRELL, BOOTS, status=Status.APPROVED),
        "lowa": add(conn, root, 6569, SNEAKERS_LOWA, WOMEN_SNEAKERS, status=Status.APPROVED),
        "vado": add(conn, root, 6570, SNEAKERS_VADO, WOMEN_SNEAKERS, status=Status.PROBLEM),
        "published": add(conn, root, 6571, SNEAKERS_KEEN, WOMEN_SNEAKERS, status=Status.PUBLISHED),
    }


def test_recheck_season_dry_run_changes_nothing(conn, cards):
    before = conn.execute("SELECT * FROM blanks ORDER BY item_id, field").fetchall()

    result = fill.recheck_season(conn, dry_run=True)

    assert sorted(p for p, *_ in result.changed) == [6566, 6567, 6569, 6570]
    assert sorted(result.returned) == [6567, 6569]
    assert conn.execute("SELECT * FROM blanks ORDER BY item_id, field").fetchall() == before


def test_recheck_season_updates_blank_and_statuses(conn, cards):
    fill.recheck_season(conn)

    assert season_of(conn, cards["keen"]) == [DEMI]
    assert season_of(conn, cards["lowa"]) == [DEMI]
    assert season_of(conn, cards["vado"]) == [DEMI]
    assert season_of(conn, cards["meindl"]) == BOTH
    assert item(conn, cards["keen"])["status"] == Status.REVIEW
    assert item(conn, cards["vado"])["status"] == Status.PROBLEM  # проблема лишилась проблемою
    # схвалені зі зміною → «На перевірку» з приміткою
    for name, now_ in (("meindl", "Демісезон, Зима"), ("lowa", "Демісезон")):
        row = item(conn, cards[name])
        assert row["status"] == Status.REVIEW
        assert row["problem_code"] == review.SEASON_CHANGED
        assert row["problem_text"] == f"Сезон змінено правилом: було Зима → стало {now_}"
    # без зміни (справді зимові) — лишилась схваленою
    assert item(conn, cards["merrell"])["status"] == Status.APPROVED
    assert item(conn, cards["merrell"])["problem_code"] is None


def test_recheck_season_never_touches_published(conn, cards):
    fill.recheck_season(conn)

    row = item(conn, cards["published"])
    assert row["status"] == Status.PUBLISHED
    assert season_of(conn, cards["published"]) == [WINTER]


def test_recheck_season_skips_card_on_shafa_and_publish_problems(conn, root):
    on_shafa = add(conn, root, 6580, SNEAKERS_KEEN, WOMEN_SNEAKERS, status=Status.PROBLEM)
    uncertain = add(conn, root, 6581, SNEAKERS_KEEN, WOMEN_SNEAKERS, status=Status.PROBLEM)
    with conn:
        conn.execute("UPDATE items SET shafa_id = '1' WHERE id = ?", (on_shafa,))
        conn.execute(
            "UPDATE items SET problem_code = 'publish_uncertain' WHERE id = ?", (uncertain,)
        )

    assert fill.recheck_season(conn).changed == []
    assert season_of(conn, on_shafa) == season_of(conn, uncertain) == [WINTER]


def test_recheck_season_keeps_her_edit_and_other_fields(conn, cards):
    put_edit(conn, cards["keen"], "season", ["Літо"])
    before = {k: v for k, v in blank(conn, cards["keen"]).items() if k != "season"}

    fill.recheck_season(conn)

    assert season_of(conn, cards["keen"]) == [WINTER]  # її правка сильніша
    assert {k: v for k, v in blank(conn, cards["keen"]).items() if k != "season"} == before


def test_recheck_season_is_idempotent_and_has_no_ai_or_version_bump(conn, cards):
    versions = conn.execute("SELECT id, rules_version FROM items ORDER BY id").fetchall()
    fill.recheck_season(conn)

    again = fill.recheck_season(conn)

    assert again.changed == [] and again.returned == []
    assert conn.execute("SELECT id, rules_version FROM items ORDER BY id").fetchall() == versions
    assert conn.execute("SELECT COUNT(*) FROM ai_requests").fetchone()[0] == 0


def test_approve_clears_season_note(conn, root, cards):
    fill.recheck_season(conn)
    assert review.act(conn, cards["lowa"], "approve") == "approved"
    row = item(conn, cards["lowa"])
    assert row["status"] == Status.APPROVED and row["problem_code"] is None


def test_recheck_season_cli(tmp_path, capsys, monkeypatch):
    from shafa import db

    path = tmp_path / "cli.sqlite"
    conn = db.connect(path)
    root = tmp_path / "Шафа"
    root.mkdir()
    add(conn, root, 6566, SNEAKERS_KEEN, WOMEN_SNEAKERS, status=Status.APPROVED)
    conn.close()

    assert main(["fill", "--db", str(path), "--recheck-season", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Пост 6566: Зима → Демісезон" in out and "Змінилось би: 1" in out
    assert main(["fill", "--db", str(path), "--recheck-season"]) == 0
    assert "Змінилось: 1; схвалених іде «На перевірку»: 1" in capsys.readouterr().out
