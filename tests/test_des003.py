"""DES-003: дрібниці після звірки з еталоном — крапка «Бот», «Вийти», без анімацій."""

import re
from pathlib import Path

from shafa import web
from test_des002 import AIS, BOTS, HEALTHS, ROBOTS, SITES, context, env  # noqa: F401

SRC = Path(web.__file__).parent


def test_bot_dot_is_green_when_running(env):  # noqa: F811
    assert web.BOT_TONES["running"] == "ok"
    ctx = context(ROBOTS["idle"], AIS["quiet"], BOTS["running"], HEALTHS["ok"], SITES["ok"])
    page = env.get_template("panel_mini.html").render(mini=True, **ctx)
    assert re.search(r'bot-running"[^>]*><span class="dot dot-ok"', page)


def test_quit_confirm_has_no_save_bar():
    """«Зберегти» (`.actionbar`) на сторінці «Вийти» не рендериться."""
    assert "actionbar" not in (SRC / "templates" / "quit_confirm.html").read_text(encoding="utf-8")


def test_no_animations_in_css():
    css = (SRC / "static" / "style.css").read_text(encoding="utf-8").splitlines()
    hits = [line for line in css if re.search(r"transition|animation", line)]
    assert len(hits) == 1 and "animation: none" in hits[0] and "transition: none" in hits[0]
