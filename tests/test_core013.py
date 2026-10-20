"""CORE-013: назви систем розмірів у знімку («Европейский») не повинні губити розмір взуття."""

import json

import pytest

from shafa import ai, catalog, fill, review
from shafa.db import Status
from shafa.robot import lang, sync
from test_ai import NAMES, answer, by_name
from test_catalog import WOMEN_SNEAKERS
from test_core008 import item, put_edit
from test_core009 import blank
from test_review import add_card, edits
from test_robot_sync import catalog as catalog_response

RUSSIAN = {
    "Європейський": "Европейский",
    "Український": "Украинский",
    "Американський": "Американский",
}
BACKPACKS = "Аксесуари / Сумки / Рюкзаки"
POST = "Кросівки Asics 41 розміру. 950грн"


@pytest.fixture
def russian_snapshot(monkeypatch, tmp_path):
    """Знімок так, як його записав sync 19.10: назви систем розмірів російською."""
    data = json.loads(catalog.SNAPSHOT.read_text(encoding="utf-8"))
    for entry in data["sections"].values():
        entry["sizes"] = {RUSSIAN.get(g, g): names for g, names in entry["sizes"].items()}
    path = tmp_path / "shafa-catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog, "SNAPSHOT", path)
    catalog.snapshot.cache_clear()
    assert (
        "Европейский"
        in json.loads(path.read_text(encoding="utf-8"))["sections"][WOMEN_SNEAKERS]["sizes"]
    )
    yield
    catalog.snapshot.cache_clear()


def test_sync_writes_ukrainian_size_groups():
    response = catalog_response(
        1533,
        {"Европейский": ["38", "Другой"], "🇺🇦 Украинский": ["15"], "Американский": ["7"]},
        [],
        [],
    )

    entry, _ = sync.section_entry(response, "krosivky")

    assert list(entry["sizes"]) == ["Європейський", "Український", "Американський"]
    assert entry["sizes"]["Європейський"] == ["38", "Другой"]


def test_both_spellings_of_one_system_are_merged():
    assert lang.size_groups({"Европейский": ["38", "39"], "Європейський": ["39", "40"]}) == {
        "Європейський": ["38", "39", "40"]
    }


def test_catalog_reads_old_russian_snapshot(russian_snapshot):
    assert catalog.size_system(WOMEN_SNEAKERS) == "Європейський"
    assert catalog.sizes(WOMEN_SNEAKERS)[0] == "34" and "41" in catalog.sizes(WOMEN_SNEAKERS)
    assert catalog.size_system("Дитячий одяг / Для дівчаток / Взуття / Кросівки") == "Європейський"


def test_real_snapshot_has_no_russian_size_groups():
    data = json.loads(
        (catalog.SNAPSHOT.parent.parent.parent / "src/shafa/data/shafa-catalog.json").read_text(
            encoding="utf-8"
        )
    )
    groups = {g for entry in data["sections"].values() for g in entry["sizes"]}
    assert groups <= {"Європейський", "Український", "Американський"}


def test_ai_size_survives_russian_snapshot(russian_snapshot):
    fields = by_name(ai.check_answer(answer(section=WOMEN_SNEAKERS, sizes=["41"]), NAMES, POST))

    assert fields["sizes"].value == ["41"]


def test_shoes_without_size_cannot_be_approved(conn, root, russian_snapshot):
    blank_ = ai.check_answer(answer(section=WOMEN_SNEAKERS, sizes=["41"]), NAMES, POST)
    item_id = add_card(conn, root, text=POST, blank=blank_)
    with conn:
        conn.execute("DELETE FROM blanks WHERE item_id = ? AND field = 'sizes'", (item_id,))
        conn.execute(
            "INSERT INTO blanks (item_id, field, value, yellow, reason) VALUES (?, 'sizes', '[]', 0, NULL)",
            (item_id,),
        )

    assert review.load_card(conn, item_id).approve_block() == "no_sizes"
    assert review.act(conn, item_id, "approve") == "no_sizes"


# --- уражені Картки: бланк з уже збереженої відповіді ШІ ---


def damaged(conn, root, folder, post_id, status=Status.APPROVED, section=WOMEN_SNEAKERS, size="41"):
    """Картка, у якої ШІ відповів розмір, а бланк лишився порожнім (як у пілотній базі)."""
    stored = answer(section=section, sizes=[size] if size else [])
    broken = ai.check_answer(answer(section=section, sizes=[]), NAMES, POST)
    item_id = add_card(
        conn, root, folder=folder, post_id=post_id, text=POST, status=status, blank=broken
    )
    with conn:
        fill.save_answer(conn, item_id, stored)
    return item_id


def sizes_of(conn, item_id):
    return blank(conn, item_id)["sizes"][0]


def test_recheck_sizes_restores_size_without_ai_and_returns_approved_to_review(conn, root):
    approved = damaged(conn, root, "1", 1)
    in_review = damaged(conn, root, "2", 2, status=Status.REVIEW)
    robot_failed = damaged(conn, root, "3", 3, status=Status.PROBLEM)
    with conn:
        conn.execute(
            "UPDATE items SET problem_code = 'publish_error', problem_text = 'size' WHERE id = ?",
            (robot_failed,),
        )
    put_edit(conn, approved, "title", "Мої Asics")
    requests = conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0]

    result = fill.recheck_sizes(conn)

    assert result.fixed == [1, 2, 3] and result.still_empty == []
    for item_id in (approved, in_review, robot_failed):
        assert sizes_of(conn, item_id) == ["41"]
        row = item(conn, item_id)
        assert (row["status"], row["problem_code"]) == (Status.REVIEW, None)
    assert edits(conn, approved) == {"title": "Мої Asics"}
    assert conn.execute("SELECT count(*) FROM ai_requests").fetchone()[0] == requests
    assert fill.recheck_sizes(conn) == fill.SizesResult([], [])  # вдруге нічого


def test_recheck_sizes_dry_run_changes_nothing(conn, root):
    item_id = damaged(conn, root, "1", 1)

    result = fill.recheck_sizes(conn, dry_run=True)

    assert result.fixed == [1]
    assert sizes_of(conn, item_id) == [] and item(conn, item_id)["status"] == Status.APPROVED


def test_recheck_sizes_leaves_other_cards_alone(conn, root):
    published = damaged(conn, root, "1", 1, status=Status.PUBLISHED)
    uncertain = damaged(conn, root, "2", 2, status=Status.PROBLEM)
    with conn:
        conn.execute(
            "UPDATE items SET problem_code = 'publish_uncertain' WHERE id = ?", (uncertain,)
        )
    edited = damaged(conn, root, "3", 3)
    put_edit(conn, edited, "sizes", ["42"])
    no_sizes_section = damaged(conn, root, "4", 4, section=BACKPACKS)
    empty_answer = damaged(conn, root, "5", 5, size=None)

    result = fill.recheck_sizes(conn)

    assert result.fixed == [] and result.still_empty == [5]
    for item_id in (published, uncertain, edited, no_sizes_section, empty_answer):
        assert sizes_of(conn, item_id) == []
    assert item(conn, uncertain)["status"] == Status.PROBLEM


def test_recheck_sizes_cli(conn, root, tmp_path, capsys):
    from shafa import __main__ as cli

    damaged(conn, root, "1", 1)
    conn.close()

    assert (
        cli.main(["fill", "--db", str(tmp_path / "база.sqlite"), "--recheck-sizes", "--dry-run"])
        == 0
    )
    assert "1" in capsys.readouterr().out
