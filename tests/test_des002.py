"""DES-002: панель стану v2 (`.sp`) показує все, що показувала стара (`.status`, `fixtures/panel_v1.html`).

Для кожного стану панелі обидва шаблони рендеряться з одним контекстом, і множина видимих текстів,
кнопок (name/value/disabled/підпис) і посилань нового ⊇ старого. Заміни, які тікет дозволяє
(підписи прямокутників замість рядків «Сьогодні на Шафі: …», «Опублікує до N», лічильників ШІ),
перелічені в `replaced` і перевіряються окремо.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from shafa import health, web
from shafa.robot import control, publish
from test_web import PORT, TOKEN

OLD = (Path(__file__).parent / "fixtures" / "panel_v1.html").read_text(encoding="utf-8")


class Page(HTMLParser):
    """Видимі тексти, кнопки й посилання; `hidden`-форми й порожні вузли не рахуємо."""

    def __init__(self, source: str):
        super().__init__()
        self.texts: list[str] = []
        self.buttons: set[tuple] = set()
        self.links: set[str] = set()
        self._button: list | None = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button":
            self._button = [attrs.get("name", ""), attrs.get("value", ""), "disabled" in attrs, ""]
        elif tag == "a" and attrs.get("href"):
            self.links.add(attrs["href"])
            if "button" in (attrs.get("class") or ""):
                self._button = ["a", attrs["href"], False, ""]

    def handle_endtag(self, tag):
        if tag in ("button", "a") and self._button is not None:
            self.buttons.add((*self._button[:3], norm(self._button[3])))
            self._button = None

    def handle_data(self, data):
        text = norm(data)
        if text:
            self.texts.append(text)
            if self._button is not None:
                self._button[3] += " " + text

    @property
    def full(self) -> str:
        return norm(" ".join(self.texts))


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- стани ---


def robot(code="stopped", **kw) -> control.State:
    base = dict(  # noqa: C408
        code=code, text=control.TEXTS.get(code, "Робот"), published_today=0, day_limit=30,
        approved=3, can_start=False, buttons=[],
        started=publish.Groups(1, 2), limits=publish.Groups(20, 10),
    )  # fmt: skip
    base.update(kw)
    return control.State(**base)


def idle(**kw) -> control.State:
    return robot(
        can_start=True, buttons=["start", "rehearse"], queue_today=3, eta_cards="3 Картки",
        eta_span="9 хв", eta_text="Сьогодні опублікує до 3 Картки (≈ 9 хв)", **kw,
    )  # fmt: skip


def working(code="running", **kw) -> control.State:
    buttons = ["resume", "stop"] if code == "paused" else ["pause", "stop"]
    return robot(code, buttons=buttons, session_published=2, **kw)


ROBOTS = {
    "idle": idle(idle_note="Сьогодні робот ще не запускався — можна натискати «Старт»."),
    "idle-last": idle(
        last="Останній запуск: вчора о 12:05 — опубліковано 20",
        last_session=7,
        last_problems=1,
        rehearsal="Пробний прогін: добре 4 з 5",
        rehearsal_problems=1,
    ),
    "idle-details": idle(details=["Причина: щось", "рядок журналу"]),
    "no-approved": robot(
        approved=0, buttons=["start"], why_not="Немає Схвалених — схваліть Картки у «На перевірку»."
    ),
    "limit": robot(
        buttons=["start", "rehearse"], why_not="На сьогодні все", why_code="limit", approved=2
    ),
    "never-logged": robot(buttons=["start", "login"], never_logged=True, approved=0),
    "login-needed": robot(
        "login_needed", buttons=["start", "login"], why_not="Спершу увійдіть на Шафу"
    ),
    "login-open": robot("login_open", buttons=["login_done"]),
    "running": working(
        progress="Публікує: 4 з 19 на сьогодні · працює 18 хв · ще ≈ 1 год 20 хв",
        doing="Зараз: пост 6979 — заповнює форму",
        previous="Останній: пост 6393 — опубліковано ✓",
        plan=19,
        handled=4,
        left_text="1 год 20 хв",
    ),
    "running-no-plan": working(),
    "pausing": working("pausing", text="Пауза: дописую пост 5"),
    "paused": working(
        "paused", progress="Опубліковано 4 з 19 на сьогодні · на паузі", plan=19, handled=4
    ),
    "stopping": robot("stopping", text="Стоп: дописую пост 5"),
    "starting": robot("starting"),
    "rehearsing": robot("rehearsing"),
    "busy": robot("busy"),
    "crashed": robot(
        "crashed",
        buttons=["start", "rehearse"],
        can_start=True,
        details=["TargetClosed"],
        last="Останній запуск: сьогодні",
        last_session=3,
    ),
    "failed": robot("failed", buttons=["start"], can_start=True, details=["Traceback…"]),
}


def ai(**kw) -> web.AiState:
    state = web.AiState(wait_text="7 днів", young=2, **kw)
    state.counters = [  # як у `App.ai_state`
        (text, href)
        for text, href in (
            (f"Готові йти на ШІ: {state.ready}" if state.ready else None, None),
            (f"ШІ зараз заповнює: {state.pending}" if state.pending else None, None),
            (f"Готові до перевірки: {state.to_check}" if state.to_check else None, "/review"),
        )
        if text
    ]
    return state


AIS = {
    "quiet": ai(),
    "counts": ai(ready=5, to_check=7, pending=0),
    "busy": ai(ready=1, pending=2, busy="відправляю пакет…", note="12:00 · щось"),
    "filling": ai(
        pending=3,
        filling="ШІ заповнює 3 Картки (відправлено о 11:45).",
        checking="Перевіряю, чи готово, кожні 3 хв.",
        cost="≈ 1,36 $ (зазвичай менше)",
    ),
    "finished": ai(to_check=30, finished="Готово о 12:21: 28 Карток у «На перевірку»."),
    "held": ai(
        ready=705, held_count=675, held="675 чекають своєї черги: ШІ готує Картки на 3 дні наперед."
    ),
    "new-posts": ai(new_posts=13),
    "error": ai(error=True, note="ШІ: ключ не налаштований", detail="немає ключа"),
    "refill": ai(refillable=4, recheckable=6),
    "refill-busy": ai(refillable=4, recheckable=6, busy="ШІ працює"),
    "pending-note": ai(pending=1, recheckable=1),
}


def bot(code, **kw) -> web.BotState:
    return web.BotState(code, {"running": "працює", "stopped": "вимкнений"}.get(code, code), **kw)


BOTS = {
    "running": bot("running"),
    "running-error": web.BotState("running", "працює, але: збій", detail="Traceback"),
    "stopped": bot("stopped", can_start=True),
    "no_token": web.BotState("no_token", "не налаштований — скажіть Власнику"),
    "failed": web.BotState("failed", "зупинився", can_start=True, detail="причина"),
}

ROWS = [
    health.Check("Ключ ШІ", health.FAIL, "ключ не працює", "Скажіть Власнику"),
    health.Check("Диск", health.WARN, "мало місця", ""),
]
HEALTHS = {
    "none": web.HealthView("none"),
    "running": web.HealthView("running"),
    "ok": web.HealthView("ok", "2026-11-01T15:23:00+00:00", [], 10),
    "old-ok": web.HealthView("ok", "2026-10-31T15:23:00+00:00", [], 10, old=True),
    "warn": web.HealthView("problems", "2026-11-01T15:23:00+00:00", ROWS[1:], 9),
    "bad": web.HealthView("problems", "2026-11-01T15:23:00+00:00", ROWS, 8),
    "old-bad": web.HealthView("problems", "2026-10-31T15:23:00+00:00", ROWS, 8, old=True),
}
SITES = {"none": None, "ok": (68, 0, 49, 0), "fuse": (68, 0, 49, 4)}


def context(robot_, ai_, bot_, health_, site_, fake=False) -> dict:
    tones = {
        "robot": web.ROBOT_TONES.get(robot_.code, "warn"),
        "ai": "warn" if ai_.error and not ai_.busy else "ok",
        "bot": web.BOT_TONES.get(bot_.code, "warn"),
        "site": "warn" if site_ and site_[3] else "ok",
        "health": "bad" if any(r.status == health.FAIL for r in health_.rows)
        else "warn" if health_.rows else "idle",
    }  # fmt: skip
    return dict(  # noqa: C408
        robot=robot_, ai=ai_, bot=bot_, health=health_, site=site_, tones=tones, fake=fake,
        token=TOKEN, back="/review", counts={"review": 3}, when=lambda value: f"«{value[:10]}»",
    )  # fmt: skip


def scenarios():
    quiet = (AIS["quiet"], BOTS["running"], HEALTHS["ok"], SITES["ok"])
    for name, state in ROBOTS.items():
        yield f"robot-{name}", context(state, *quiet)
    for name, state in AIS.items():
        yield f"ai-{name}", context(ROBOTS["idle"], state, *quiet[1:])
    for name, state in BOTS.items():
        yield f"bot-{name}", context(ROBOTS["idle"], quiet[0], state, *quiet[2:])
    for name, state in HEALTHS.items():
        yield f"health-{name}", context(ROBOTS["idle"], quiet[0], quiet[1], state, quiet[3])
    for name, state in SITES.items():
        yield f"site-{name}", context(ROBOTS["idle"], *quiet[:3], state)
    yield "fake", context(ROBOTS["idle"], *quiet, fake=True)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("des002")
    app = web.App(tmp / "база.sqlite", tmp / "root", tmp / "thumbs", PORT, token=TOKEN)
    return app.env


def replaced(text: str, ctx: dict) -> bool:
    """Рядки старої панелі, яких тікет замінив прямокутниками (числа перевіряє `test_*_stats`)."""
    state, ai_ = ctx["robot"], ctx["ai"]
    groups = (state.groups_text, f"{state.groups_text} · Схвалених {state.approved}")
    counters = {
        f"Готові йти на ШІ: {ai_.ready}", f"ШІ зараз заповнює: {ai_.pending}",
        f"Готові до перевірки: {ai_.to_check}",
    }  # fmt: skip
    parts = [part.strip() for part in text.split("·") if part.strip()]
    return text in (*groups, state.eta_text) or (bool(parts) and all(p in counters for p in parts))


NAMES = [name for name, _ in scenarios()]


@pytest.mark.parametrize("name", NAMES)
def test_new_panel_shows_everything_the_old_one_did(env, name):
    ctx = dict(scenarios())[name]
    old = Page(env.from_string(OLD).render(**ctx))
    new = Page(env.get_template("panel.html").render(**ctx))
    missing = [t for t in old.texts if t not in new.full and not replaced(t, ctx)]
    assert not missing, f"зникли тексти: {missing}"
    assert old.buttons <= {(n, v, d, norm(t)) for n, v, d, t in new.buttons} | _same(old, new)
    assert old.links <= new.links


def _same(old: Page, new: Page) -> set:
    """Кнопки, що змінили лише підпис-обгортку: порівнюємо за name/value/disabled і текстом."""
    return {b for b in old.buttons if any(b[:3] == n[:3] and b[3] in n[3] for n in new.buttons)}


def test_all_robot_codes_are_covered():
    codes = {state.code for state in ROBOTS.values()}
    assert set(web.ROBOT_TONES) <= codes | {"stopped"}


# --- нове: цифри в прямокутниках ---


def stats(env, **kw) -> dict:
    page = env.get_template("panel.html").render(**ctx(**kw))
    return dict(
        re.findall(
            r'class="stat-label">([^<]+)</span><span class="stat-num">(.*?)</span>', page, re.DOTALL
        )
    )


def quiet(**kw):
    base = dict(  # noqa: C408
        robot=ROBOTS["idle"], ai=AIS["quiet"], bot=BOTS["running"], health=HEALTHS["ok"], site=None
    )
    return {**base, **kw}


def ctx(**kw) -> dict:
    return context(*(quiet(**kw)[k] for k in ("robot", "ai", "bot", "health", "site")))


def clean(value: str) -> str:
    return norm(re.sub(r"<[^>]+>", "", value))


def test_robot_stats_idle(env):
    got = {k: clean(v) for k, v in stats(env, **quiet()).items()}
    assert got["Сьогодні опублікує"] == "до 3 Картки · ≈ 9 хв"
    assert got["З минулих постів"] == "1 з 20" and got["З нових (бот)"] == "2 з 10"
    assert got["Схвалених"] == "3"


def test_robot_stats_by_state(env):
    key = lambda name: clean(stats(env, **quiet(robot=ROBOTS[name]))["Сьогодні опублікує"])
    assert key("running") == "4 з 19 · ще ≈ 1 год 20 хв"
    assert key("paused") == "4 з 19 · на паузі"
    assert key("running-no-plan") == "2 у цій сесії"
    assert key("no-approved") == "0" and key("starting") == "0"


def test_ai_stats_hide_only_what_was_hidden_before(env):
    got = lambda name: {k: clean(v) for k, v in stats(env, **quiet(ai=AIS[name])).items()}
    base = got("quiet")
    assert base["Готові до перевірки"] == "0" and base["Готові йти на ШІ"] == "0"
    for label in ("Чекають своєї черги", "ШІ зараз заповнює", "Нових постів з бота"):
        assert label not in base
    assert got("counts")["Готові до перевірки"] == "7" and got("counts")["Готові йти на ШІ"] == "5"
    assert got("filling")["ШІ зараз заповнює"] == "3"
    assert got("held")["Чекають своєї черги"] == "675"
    assert got("new-posts")["Нових постів з бота"] == "13"


def test_classes_of_robot_block(env):
    def block(name):
        page = env.get_template("panel.html").render(**ctx(robot=ROBOTS[name]))
        return re.search(r'<div class="sp-robot ([^"]*)"', page).group(1)

    assert "is-running" in block("running") and "is-warn" in block("login-needed")
    assert "is-error" in block("crashed") and "is-error" in block("failed")
    assert not re.search(r"is-(running|warn|error)", block("idle"))
    assert not re.search(r"is-(running|warn|error)", block("paused"))
    assert "sp-error" not in env.get_template("panel.html").render(**ctx(robot=ROBOTS["crashed"]))


def test_fake_is_first_in_panel(env):
    page = env.get_template("panel.html").render(**dict(scenarios())["fake"])
    assert re.search(r'<section class="sp"[^>]*>\s*<p class="fake">ПІДСТАВНА ШАФА', page)
    assert 'class="fake"' not in env.get_template("panel.html").render(
        **dict(scenarios())["robot-idle"]
    )


def test_counts_marker_is_kept(env):
    page = env.get_template("panel.html").render(**dict(scenarios())["robot-idle"])
    assert 'data-counts="review=3;"' in page


def test_start_stays_disabled_when_it_cannot_start(env):
    page = env.get_template("panel.html").render(**dict(scenarios())["robot-no-approved"])
    assert re.search(r'<button name="action" value="start"[^>]*\bdisabled', page)
    assert re.search(r'value="start" class="approve sp-start"', page)


@pytest.mark.parametrize(
    "n, text",
    [(1, "1 Картки"), (2, "2 Карток"), (5, "5 Карток"), (11, "11 Карток"), (21, "21 Картки")],
)
def test_eta_cards_genitive_after_do(n, text):
    """«Сьогодні опублікує до 1 Картки» — після «до» родовий відмінок (DES-003 п. 7)."""
    from shafa.robot import control

    assert control.plural_cards(n) == text
