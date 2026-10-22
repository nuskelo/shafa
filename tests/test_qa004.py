"""QA-004: сценарії підставної Шафи й помічники «мавпи» (`ops/monkey*.py`) — без браузера й мережі."""

import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from shafa import web
from shafa.db import Status
from shafa.robot import browser, fake, publish
from shafa.robot.form import FieldMissing, PhotosShort

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import monkey
import monkey_seed

from test_review import add_card
from test_web import PORT, TOKEN, get, html


def draws(scenarios, n):
    return [scenarios.next() for _ in range(n)]


def test_scenarios_are_reproducible_with_a_seed():
    weights = {"ok": 5, "nofield": 2, "balance": 1}
    assert draws(fake.Scenarios(weights, 7), 40) == draws(fake.Scenarios(weights, 7), 40)
    assert draws(fake.Scenarios(weights, 7), 40) != draws(fake.Scenarios(weights, 8), 40)


def test_slow_lasts_for_every_attempt_of_one_card():
    scenarios = fake.Scenarios({"slow": 1})
    assert draws(scenarios, 4) == ["slow", "slow", "slow", "slow"]  # 3 + новий вибір теж slow
    scenarios = fake.Scenarios(sequence=["slow", "ok"])
    assert draws(scenarios, 5) == ["slow", "slow", "slow", "ok", "ok"]
    assert fake.SLOW_OPENS == publish.ATTEMPTS  # інакше `robot_slow` ніколи не настане


def test_sequence_goes_first_then_weights():
    scenarios = fake.Scenarios({"ok": 1}, sequence=["login", "balance"])
    assert draws(scenarios, 4) == ["login", "balance", "ok", "ok"]


def test_unknown_scenario_is_an_error():
    with pytest.raises(ValueError, match="невідомий сценарій"):
        fake.Scenarios({"explode": 1})
    with pytest.raises(ValueError, match="невідомий сценарій"):
        fake.Scenarios(sequence=["explode"])


def test_scenarios_from_env(monkeypatch):
    assert draws(fake.Scenarios.from_env(), 3) == ["ok"] * 3  # без змінних — як раніше
    monkeypatch.setenv(fake.SCENARIOS_ENV, "ok:3, nofield:1")
    monkeypatch.setenv(fake.SEQUENCE_ENV, "login,photos")
    monkeypatch.setenv(fake.SEED_ENV, "5")
    scenarios = fake.Scenarios.from_env()
    assert scenarios.names == ["ok", "nofield"] and scenarios.weights == [3, 1]
    assert draws(scenarios, 2) == ["login", "photos"]


def test_balance_window_only_in_balance_scenarios():
    assert "від'ємний баланс" not in fake.page("ok")
    stuck, closable = fake.page("balance_stuck"), fake.page("balance")
    assert "від'ємний баланс" in stuck and "від'ємний баланс" in closable
    assert 'getElementById("bal").remove()' in closable
    assert 'getElementById("bal").remove()' not in stuck  # хрестик не працює
    assert fake.page("login") == fake.page("ok")


def test_log_is_written_only_when_asked(tmp_path, monkeypatch):
    fake.write_log(event="nothing")  # змінної немає — нічого не відбувається
    path = tmp_path / "fake.jsonl"
    monkeypatch.setenv(fake.LOG_ENV, str(path))
    fake.write_log(event="create", item=5)
    fake.write_log(event="open", scenario="Ёж")
    assert [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] == [
        {"event": "create", "item": 5},
        {"event": "open", "scenario": "Ёж"},
    ]


class _Page:
    def __init__(self):
        self.visited = []
        self.url = "http://127.0.0.1:1/new"

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url


def session(*names):
    s = fake.FakeSession.__new__(fake.FakeSession)
    s._setup(allow_upload=True, publish=True)
    s.base, s.page = "http://127.0.0.1:50123", _Page()
    s.scenarios = fake.Scenarios(sequence=list(names))
    return s


def test_login_scenario_does_not_open_the_form():
    s = session("login")
    opened = s.open_form()
    assert opened.state == browser.NO_LOGIN and not opened.ok
    assert s.page.visited == []  # сторінки форми Шафа не показала


def test_other_scenarios_open_the_form_with_their_name():
    s = session("balance", "ok")
    assert s.open_form().ok and s.page.visited == [s.base + "/new?s=balance"]
    assert s.open_form().ok and s.page.visited[-1] == s.base + "/new?s=ok"


@pytest.mark.parametrize(
    ("name", "error"),
    [("nofield", FieldMissing), ("slow", TimeoutError), ("photos", PhotosShort)],
)
def test_driver_fails_the_way_the_scenario_says(name, error, monkeypatch):
    monkeypatch.setattr(fake.FakeDriver, "_step", staticmethod(lambda: None))
    s = session(name)
    s.open_form()
    driver = fake.FakeDriver.__new__(fake.FakeDriver)
    driver.session, driver.page = s, s.page
    action = (
        (lambda: driver.set_photos(["a.jpg", "b.jpg"]))
        if name == "photos"
        else (lambda: driver.fill("titleUk", "x", "Назва"))
    )
    with pytest.raises(error) as caught:
        action()
    if name == "slow":
        assert publish.is_transient(caught.value)  # робот пробує форму ще раз, а не падає


def test_ok_scenario_changes_nothing(monkeypatch):
    monkeypatch.setattr(fake.FakeDriver, "_step", staticmethod(lambda: None))
    s = session("ok")
    s.open_form()
    driver = fake.FakeDriver.__new__(fake.FakeDriver)
    driver.session, driver.page = s, s.page
    assert driver.set_photos(["a.jpg", "b.jpg"]) == 2
    driver.fill("titleUk", "x", "Назва")


# --- помічники «мавпи» ---


def test_findings_are_deduplicated_by_pattern():
    found = monkey.Findings()
    found.add("http_500", "/item/12", "дія A")
    found.add("http_500", "/item/13", "дія A")  # той самий шаблон — лише лічильник
    found.add("http_500", "/item/13", "дія B")
    assert len(found.items) == 2
    assert next(iter(found.items.values()))["count"] == 2


def test_pick_cards_one_per_status_and_code():
    rows = [(1, "problem", "a"), (2, "problem", "a"), (3, "problem", "b"), (4, "review", None)]
    assert monkey.pick_cards(rows, None) == [1, 2, 3, 4]
    assert monkey.pick_cards(rows, 1) == [1, 3, 4]


def test_seed_has_every_status_and_every_problem_code(tmp_path):
    data, root, info = monkey_seed.prepare(tmp_path / "m", seed=3, approved=5)
    assert set(info["items"]) == {
        "review", "new", "problem", "approved", "published", "archived", "publishing"
    }  # fmt: skip
    assert (data / ".env").read_text(encoding="utf-8").count("KEY") == 0  # без ключів
    with contextlib.closing(monkey.ro(data)) as conn:
        codes = {r[0] for r in conn.execute("SELECT problem_code FROM items")}
        photos = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    assert set(monkey_seed.PROBLEM_CODES) <= codes and photos > 0
    assert (root / "Фото Ольги 5").is_dir()  # кирилиця в назві папки
    assert all(not p.is_relative_to(Path.home() / "shafa") for p in (data, root))


def test_seed_is_reproducible(tmp_path):
    a = monkey_seed.prepare(tmp_path / "a", seed=1, approved=3)[2]
    b = monkey_seed.prepare(tmp_path / "b", seed=1, approved=3)[2]
    assert a == b


def test_invariants_catch_a_published_card_without_shafa_id(tmp_path):
    data, _, _ = monkey_seed.prepare(tmp_path / "m", seed=1, approved=3)
    server = SimpleNamespace(base=tmp_path)  # журналу підставної Шафи немає — подій немає
    clean, bad = monkey.Findings(), monkey.Findings()
    monkey.invariants(data, server, clean, monkey.Stage())
    assert {k[0] for k in clean.items} == {"invariant_publishing"}  # лише сіяна «publishing»
    with contextlib.closing(sqlite3.connect(data / "shafa.sqlite")) as conn, conn:
        conn.execute(
            "UPDATE items SET shafa_id = NULL WHERE id = (SELECT MIN(id) FROM items "
            "WHERE status = 'published')"
        )
    monkey.invariants(data, server, bad, monkey.Stage())
    assert "invariant_published" in {k[0] for k in bad.items}


# --- Знайдене «мавпою» ---


@pytest.mark.parametrize("code", [None, "some_future_code", "many_items", "publish_error"])
def test_problem_card_without_text_does_not_print_none(conn, root, tmp_path, code):
    """Проблема без тексту (поламаний запис, майбутній код) — порожньо, а не слово «None»."""
    item_id = add_card(conn, root, status=Status.PROBLEM)
    with conn:
        conn.execute(
            "UPDATE items SET problem_code = ?, problem_text = NULL WHERE id = ?", (code, item_id)
        )
    app = web.App(tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN)
    page = html(get(app, f"/item/{item_id}"))
    assert "None" not in page.split("<main", 1)[1]


def test_more_menu_stays_on_screen_and_above_status_panel():
    """390 px: меню «Ще» не виходило вправо за екран і ховалось під панеллю стану (Сесії, Архів,
    Налаштування не натискались)."""
    css = (Path(web.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    menu = css.split(".menu {", 1)[1].split("}", 1)[0]
    assert "z-index" in menu
    narrow = css.split("@media (max-width: 640px)", 1)[1]
    assert ".menu { left: auto; right: 0; }" in narrow


def test_every_robot_launch_gets_its_own_scenario_order(tmp_path, monkeypatch):
    """Інакше кожна сесія робота починалась би з одного й того ж сценарію (у «мавпі» — з login)."""
    monkeypatch.setenv(fake.LOG_ENV, str(tmp_path / "fake.jsonl"))
    monkeypatch.setenv(fake.SCENARIOS_ENV, "ok:5,login:1,nofield:1,photos:1")
    monkeypatch.setenv(fake.SEED_ENV, "2")
    firsts = [draws(fake.Scenarios.from_env(), 6) for _ in range(6)]
    assert len({tuple(x) for x in firsts}) > 1
    monkeypatch.setenv(fake.LOG_ENV, str(tmp_path / "other.jsonl"))
    assert [draws(fake.Scenarios.from_env(), 6) for _ in range(6)] == firsts  # той самий сід
