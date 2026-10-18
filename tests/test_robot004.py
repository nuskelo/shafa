"""ROBOT-004: розмітка поля-списку поруч зі скриншотом, коли робот зупинився (dry-fill і publish)."""

from pathlib import Path

from shafa.robot import form, publish
from shafa.robot.form import FieldMissing, save_markup
from test_robot_form import FakeDriver, FakeSession, approved, run
from test_robot_publish import add, env, new_cards_allowed, quiet  # noqa: F401 — фікстури

MARKUP = '<!-- блок поля -->\n<div class="field">Матеріал <span>Шкіра</span></div>'


def list_stop():
    return FieldMissing(
        "Матеріал: на формі порожньо, треба Шкіра",
        "поле «Матеріал»; вибрано: нічого; у списку (0): порожньо",
        field="Матеріал",
        markup=MARKUP,
    )


def test_markup_saved_next_to_screenshot(tmp_path):
    shot = tmp_path / "екрани" / "20261018-172200-878-нема-поля.png"
    path = save_markup(list_stop(), shot)
    assert path == tmp_path / "екрани" / "20261018-172200-878-Матеріал.html"
    assert path.read_text(encoding="utf-8") == MARKUP


def test_markup_cut_to_50_kb_and_field_name_safe_for_windows(tmp_path):
    error = FieldMissing("x", field='Розмір: US/UK "дитячий"', markup="ї" * 40_000)
    path = save_markup(error, tmp_path / "1-2-нема-поля.png")
    assert path.name == "1-2-Розмір-US-UK-дитячий.html"
    assert len(path.read_bytes()) == form.MARKUP_LIMIT == 50 * 1024
    assert path.read_text(encoding="utf-8") == "ї" * (25 * 1024)  # без половини літери


def test_no_markup_no_file(tmp_path):
    assert save_markup(FieldMissing("Сезон"), tmp_path / "1-2-нема-поля.png") is None
    assert list(tmp_path.iterdir()) == []


class ListDriver(FakeDriver):
    def pick(self, label, values):
        raise list_stop()


def test_dry_fill_writes_markup_and_logs_path(conn, root, tmp_path):
    approved(conn, root, folder="878", post_id=878)
    _, lines = run(conn, root, [878], FakeSession(), ListDriver, tmp_path)
    [dump] = tmp_path.glob("*.html")
    assert dump.name.endswith("-878-Матеріал.html")
    assert dump.read_text(encoding="utf-8") == MARKUP
    assert f"  · розмітка поля: {dump}" in lines


def test_publish_writes_markup_and_logs_path(env, monkeypatch):  # noqa: F811
    add(env, "878", 878)

    def fail(steps, driver, strict):
        raise list_stop()

    monkeypatch.setattr(publish, "execute", fail)
    env.run()
    [dump] = Path(env.tmp / "екрани").glob("*.html")
    assert dump.name.endswith("-878-Матеріал.html")
    assert f"Пост 878: розмітка поля «Матеріал» — {dump}" in env.lines
