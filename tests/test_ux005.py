"""UX-005: Налаштування, «Вийти», помилки й самоперевірка простими словами.

Дизайн не міняли: перевіряємо тексти й поведінку. Усе — у tmp_path, без мережі й справжніх даних.
"""

import html as htmllib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shafa import batch, config, health, options, web
from shafa.bot import run as bot_run
from shafa.robot import publish
from test_web import HOST, PORT, TOKEN, get, html, location, plain, post

SRC = Path(web.__file__).parent


@pytest.fixture
def quit_calls():
    return []


@pytest.fixture
def app(conn, tmp_path, root, quit_calls):
    return web.App(
        tmp_path / "база.sqlite", root, tmp_path / "thumbs", PORT, token=TOKEN,
        on_quit=lambda: quit_calls.append(1) or ["бота зупинено"],
    )  # fmt: skip


def form(root, **changes):
    values = {
        "SHAFA_DAILY_LIMIT": "25", "SHAFA_AI_DAILY_USD": "2,5", "SHAFA_AI_WAIT_DAYS": "5",
        "SHAFA_AI_BATCH_LIMIT": "40", "SHAFA_ROOT_DIR": str(root), "SHAFA_DELETE_PUBLISHED": "1",
        "SHAFA_DAILY_OLD": "10", "SHAFA_DAILY_NEW": "5", "SHAFA_LISTINGS_DAYS": "3",
        "SHAFA_SITE_WAIT_DAYS": "7", "SHAFA_SOLD_FUSE": "20",
    }  # fmt: skip
    values.update(changes)
    return {k: v for k, v in values.items() if v is not None}


# --- «Вийти» питає ---


def test_quit_button_only_opens_confirmation(app, quit_calls):
    page = html(get(app, "/review"))
    assert '<form method="get" action="/quit"' in page  # кнопка в шапці нічого не закриває
    asked = html(get(app, "/quit"))
    assert "Закрити програму? Бот у Telegram перестане приймати пости" in asked
    assert "доки програма закрита" in asked and "просто згорніть вікно" in asked
    assert ">Закрити</button>" in asked and ">Не закривати</a>" in asked
    assert quit_calls == []  # програма працює


def test_post_quit_without_confirmation_does_nothing(app, quit_calls):
    page = html(post(app, "/quit"))
    assert "Закрити програму?" in page
    assert quit_calls == []


def test_quit_with_confirmation_closes(app, quit_calls):
    page = html(post(app, "/quit", sure="1"))
    assert quit_calls == [1]
    assert "Програму закрито. Бот у Telegram не працює, доки ви знову не відкриєте" in page
    assert "«Шафа-помічник» ярликом." in page


def test_quit_confirmation_goes_back_to_where_we_came_from(app):
    request = web.Request(
        "GET", "/quit", {}, {}, {"host": HOST, "referer": f"http://{HOST}/problem"}
    )
    assert 'href="/problem"' in html(app.handle(request))
    elsewhere = {"host": HOST, "referer": "https://чужий.сайт/x"}
    page = html(app.handle(web.Request("GET", "/quit", {}, {}, elsewhere)))
    assert 'href="/review"' in page


def test_quit_confirmation_needs_the_ui_from_shortcut(conn, tmp_path, root):
    plain = web.App(tmp_path / "база.sqlite", root, tmp_path / "t", PORT, token=TOKEN)
    assert get(plain, "/quit").status == 404


# --- Налаштування ---


def test_flag_hints_are_shown(app):
    page = html(get(app, "/settings"))
    assert "Фото опублікованих пар і старих постів стираються назавжди через 3 дні." in page
    assert "Не знімайте: інакше продані пари лишаться на сайті." in page


def test_settings_texts_are_plain(app):
    page = html(get(app, "/settings"))
    assert "Ключ ШІ, бота Telegram і канал налаштовує Власник — тут їх змінити не можна." in page
    assert "<h2>Сайт Viktoria Shoes</h2>" in page
    for label in (
        "Скільки Карток відправляти на ШІ за раз",
        "Скільки днів чекати, перш ніж ШІ заповнить нову пару",
        "Скільки можна витратити на ШІ за день, доларів",
        "Захист від помилкового «Продано»: якщо за ніч зникло більше постів, ніж стільки",
        "Як часто перечитувати «Мої речі» на Шафі, днів",
    ):
        assert label in page
    assert "не змінювати без Власника" in page
    assert "Потрібно лише для старих постів (до бота)" in page
    assert "Список ваших товарів на Шафі ще не прочитано" in page


def test_snapshot_line(app, conn):
    with conn:
        conn.execute(
            "INSERT INTO my_listings_read (id, read_at) VALUES (1, '2026-10-30T10:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO my_listings (id, name, slug, sizes, url, colors, added_at) "
            "VALUES (1, 'a', 's', '[]', 'u', '[]', 'x')"
        )
    page = html(get(app, "/settings"))
    assert re.search(r"На Шафі у вас 1 товар \(перечитано 30\.10\)\.", page)


def test_daily_limit_below_old_plus_new_warns_and_keeps_value(app, root):
    response = post(app, "/settings", **form(root, SHAFA_DAILY_LIMIT="5", SHAFA_DAILY_OLD="20",
                                             SHAFA_DAILY_NEW="10"))  # fmt: skip
    page = html(get(app, location(response)))
    assert "Публікацій за добу (5) менше, ніж старих + нових (30): робот зупиниться на 5." in page
    assert publish.daily_limit() == 5  # значення само не міняється


def test_no_limit_warning_when_it_fits(app, root):
    response = post(app, "/settings", **form(root))
    assert "менше, ніж старих + нових" not in html(get(app, location(response)))


def test_root_change_tells_about_restart(app, root, tmp_path):
    other = tmp_path / "інша"
    other.mkdir()
    page = html(get(app, location(post(app, "/settings", **form(root, SHAFA_ROOT_DIR=str(other))))))
    assert "Нова папка діятиме для бота після перезапуску програми" in page
    assert "«Вийти» і відкрити ярликом знову." in page
    assert app.root == other


def test_other_change_says_next_start(app, root):
    page = html(get(app, location(post(app, "/settings", **form(root)))))
    assert "Збережено. Робот візьме нові ліміти при наступному «Старт»; решта діє одразу." in page
    assert "перезапуску програми" not in page


def test_missing_new_folder_blocks_with_help(app, root, tmp_path):
    gone = tmp_path / "немає такої"
    response = post(app, "/settings", **form(root, SHAFA_ROOT_DIR=str(gone)))
    page = htmllib.unescape(html(response))
    assert f"Папки «{gone}» немає на комп'ютері." in page
    assert "Провідник → адресний рядок → копіювати" in page
    assert config.setting("SHAFA_DAILY_LIMIT") is None


def test_unchanged_missing_folder_does_not_block_other_fields(app, root, tmp_path):
    gone = tmp_path / "зникла"
    gone.mkdir()
    post(app, "/settings", **form(root, SHAFA_ROOT_DIR=str(gone)))  # вона збережена
    gone.rmdir()
    response = post(
        app, "/settings", **form(root, SHAFA_ROOT_DIR=str(gone), SHAFA_DAILY_LIMIT="12")
    )
    assert response.status == 303  # збережено, не «Не збережено»
    assert publish.daily_limit() == 12
    assert config.setting("SHAFA_ROOT_DIR") == str(gone)  # папку не чіпали
    assert "зараз немає на комп'ютері" in htmllib.unescape(html(get(app, "/settings")))


def test_post_settings_redirects_and_msg_goes_away_on_reload(app, root):
    response = post(app, "/settings", **form(root))
    assert response.status == 303
    assert location(response).startswith("/settings?msg=")
    assert "msg" in (SRC / "static" / "app.js").read_text(encoding="utf-8")  # UX-002


@pytest.mark.parametrize("sum_", ["2,5 $", "$2.5", " 2,5$ ", "2,5"])
def test_money_accepts_dollar_sign_and_spaces(sum_):
    values, errors, _ = options.check(
        {"SHAFA_AI_DAILY_USD": sum_, "SHAFA_ROOT_DIR": str(Path.cwd())}
    )
    assert values["SHAFA_AI_DAILY_USD"] == "2.5", errors


def test_money_error_is_plain(app, root):
    page = html(post(app, "/settings", **form(root, SHAFA_AI_DAILY_USD="багато")))
    assert "лише число, напр. 3 або 2,5" in page


def test_export_reread_and_release_redirect(app, root):
    bad = app.handle(
        web.Request("POST", "/export", {}, {"token": [TOKEN]}, {"host": HOST}, {"export": b"x"})
    )
    assert location(bad) == "/settings?msg=export_bad"
    assert "Це не експорт каналу Telegram" in html(get(app, "/settings?msg=export_bad"))
    reread = post(app, "/listings/reread")
    assert location(reread) == "/settings?msg=reread_started"
    assert "При наступному «Старт» робот спершу перечитає «Мої речі» — це додасть 5–10 хв." in html(
        get(app, location(reread))
    )
    release = post(app, "/site/release-sold")
    assert location(release).startswith("/settings?msg=sold_none")
    assert "Відкладених «Продано» немає." in html(get(app, location(release)))


def test_export_done_message_has_numbers(app):
    page = html(get(app, "/settings?msg=export_done&posts=3&added=2&folders=1&problems=0"))
    assert "постів 3, нових 2. Папок 1, у «Проблемах» 0." in page


def test_release_sold_text_uses_vy(app):
    page = html(get(app, "/settings"))
    assert "перевірте канал" in page and "натисніть кнопку" in page
    assert "перевір канал" not in page and "натисни кнопку" not in page


# --- Помилки сторінкою ---


@pytest.mark.parametrize(
    ("path", "status", "reason"),
    [
        ("/item/9999", 404, "Такої Картки немає"),
        ("/session/9999", 404, "Такої сесії немає"),
        ("/взагалі/нічого", 404, "Такої сторінки немає"),
    ],
)
def test_not_found_is_a_page_with_reason(app, path, status, reason):
    response = get(app, path)
    page = response.body.decode("utf-8")
    assert response.status == status and response.content_type.startswith("text/html")
    assert reason in page and 'href="/">На головну' in page


def test_unknown_action_is_a_page(app):
    response = post(app, "/bot", action="щось")
    assert response.status == 400 and 'href="/">На головну' in response.body.decode("utf-8")
    assert "не зрозуміла цю дію" in response.body.decode("utf-8")


def test_stale_page_and_crash_texts(app, monkeypatch):
    stale = post(app, "/settings", token="чужий")
    assert stale.status == 403
    assert (
        "Програму перезапускали — зміни не збережено. Натисніть «Назад» у браузері, оновіть сторінку (F5) і внесіть зміни ще раз."
        in (stale.body.decode("utf-8"))
    )
    monkeypatch.setattr(app, "route", lambda request: 1 / 0)
    crash = get(app, "/review")
    page = crash.body.decode("utf-8")
    assert crash.status == 500
    assert "Щось пішло не так — дію не виконано. Поверніться на головну й спробуйте ще раз" in page
    assert "якщо повториться — скажіть Власнику." in page


# --- Самоперевірка ---


def write_last(folder, at, checks):
    payload = {"at": at.isoformat(timespec="seconds"), "checks": checks}
    (folder / health.LAST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def row(status="ok", name="База", text="гаразд", fix=""):
    return {"name": name, "status": status, "text": text, "fix": fix}


def test_health_before_first_run_without_file(app):
    page = html(get(app, "/review"))
    assert "Самоперевірка ще не проходила" in page
    assert re.search(r'class="sp-check">Перевірити</button>', page)
    assert "Перевірити ще раз" not in page


def test_health_shows_last_result_from_file(app):
    at = datetime.now().astimezone() - timedelta(days=1)
    write_last(config.data_dir(), at, [row(), row()])
    page = html(get(app, "/review"))
    assert f"Востаннє вчора о {at:%H:%M}: усе гаразд" in page
    assert (
        re.search(r'class="sp-check">Перевірити</button>', page) and "Перевірити ще раз" not in page
    )


def test_health_last_result_with_problems_counts(app):
    at = datetime.now().astimezone() - timedelta(days=3)
    write_last(config.data_dir(), at, [row(), row("fail", "ШІ", "x", "y")])
    page = html(get(app, "/review"))
    assert f"Востаннє {at:%d.%m} о {at:%H:%M}: є що виправити (1)" in page


def test_health_after_run_counts_add_up(app):
    checks = [health.Check("a", "fail", "x"), health.Check("b", "warn", "y")]
    checks += [health.Check(str(i), "ok", "ok") for i in range(6)]
    checks += [health.Check("s", "skip", "вимкнено")] * 2
    app.health_result = (datetime.now().astimezone().isoformat(), checks)
    page = html(get(app, "/review"))
    assert "Є що виправити (2)" in page
    assert re.search(r"інші 8 — гаразд · перевірено сьогодні о \d\d:\d\d", page)  # 2 + 8 = 10
    assert "Перевірити ще раз" in page
    assert "решта" not in page


def test_health_all_ok_after_run(app):
    app.health_result = (datetime.now().astimezone().isoformat(), [health.Check("a", "ok", "ok")])
    page = html(get(app, "/review"))
    assert re.search(r"Все гаразд</b>\s*<span class=\"sp-note\">перевірено сьогодні о", page)


def test_load_last_ignores_broken_file(tmp_path):
    assert health.load_last(tmp_path) is None
    (tmp_path / health.LAST_NAME).write_text("{не json", encoding="utf-8")
    assert health.load_last(tmp_path) is None
    (tmp_path / health.LAST_NAME).write_text('{"at": "x", "checks": []}', encoding="utf-8")
    assert health.load_last(tmp_path) is None


# --- Плитка «Бот» ---


def panel(app):
    return plain(html(get(app, "/review")))


def test_bot_tile_stopped(app):
    config.save_settings({bot_run.TOKEN: "123:токен"})
    page = panel(app)
    assert "Бот: вимкнений — пости в Telegram через нього зараз не публікуються" in page


def test_bot_tile_not_configured(app):
    assert "Бот: не налаштований — скажіть Власнику" in panel(app)


def test_bot_tile_failed_hides_raw_error_in_details(app, conn):
    config.save_settings({bot_run.TOKEN: "123:токен"})
    with conn:
        conn.execute(
            "INSERT INTO bot_status (id, error) VALUES (1, 'технічна помилка 409') "
            "ON CONFLICT(id) DO UPDATE SET error = excluded.error"
        )
    page = panel(app)
    assert "Бот: зупинився через помилку — якщо повториться, скажіть Власнику" in page
    assert "Для Власника: технічна помилка 409" in page
    assert page.index("Бот: зупинився") < page.index("технічна помилка 409")
    assert "зупинився: технічна" not in page


# --- Жаргон, гроші, рід ---


JARGON = ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", ".env", "getUpdates", "вебхук", "¢", "(401)")


def test_no_jargon_in_templates_and_panel_strings():
    for template in (SRC / "templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for word in JARGON:
            assert word not in text, (template.name, word)
    shown = {**web.MESSAGES, **web.PROBLEM_HINTS, **web.STICKY_TITLES}
    for key, text in shown.items():
        for word in JARGON:
            assert word not in text, (key, word)
    for text in (web.AI_SETTINGS_TEXT, bot_run.OFFLINE_TEXT, bot_run.BUSY_TEXT):
        assert not any(word in text for word in JARGON)


def test_ai_error_texts_are_plain():
    import anthropic
    import httpx2

    from shafa import ai

    request = httpx2.Request("POST", "https://x")
    auth = anthropic.AuthenticationError(
        "x", response=httpx2.Response(401, request=request), body=None
    )
    assert ai.error_text(auth) == "Ключ ШІ не приймається — скажіть Власнику"
    assert "Claude" not in ai.error_text(anthropic.APIConnectionError(request=request))


def test_bot_conflict_text_is_plain():
    source = (SRC / "bot" / "run.py").read_text(encoding="utf-8")
    assert "його вже запущено на іншому комп'ютері — скажіть Власнику" in source
    assert "вебхук" not in source


def test_health_texts_without_jargon():
    assert health._plain("Немає TELEGRAM_BOT_TOKEN у .env") == (
        "Бот не налаштований до кінця — скажіть Власнику"
    )
    assert health._plain("Бот не адміністратор каналу") == "Бот не адміністратор каналу"
    source = (SRC / "health.py").read_text(encoding="utf-8")
    assert "Списки розділів і брендів Шафи давно не оновлювались" in source
    assert "Скажіть Власнику: OneDrive" in source


def test_money_is_written_as_dollars_everywhere():
    assert batch.dollars(2_400_000) == "2,40 $"
    assert batch.approx_usd(1_360_000) == "≈ 1,36 $"
    assert "¢" not in (SRC / "templates" / "panel.html").read_text(encoding="utf-8")


def test_ai_is_masculine_on_screen():
    for text in (
        web.MESSAGES["ai_retry_done"],
        web.MESSAGES["ai_photo"],
        web.PROBLEM_HINTS["ai_error"],
    ):
        assert not re.search(r"ШІ (не )?\w+ло\b", text), text
    assert "ШІ заповнив бланк заново" in web.MESSAGES["ai_retry_done"]
    assert web.PROBLEM_KINDS["ai_error"][0] == "ШІ не заповнив"


def test_no_ty_forms_on_screen():
    shown = " ".join(
        [*web.MESSAGES.values(), *web.PROBLEM_HINTS.values(), *web.STICKY_TITLES.values()]
    )
    shown += (SRC / "templates" / "settings.html").read_text(encoding="utf-8")
    assert not re.search(r"\b(перевір|натисни|відкрий|зачекай|спробуй)\b", shown)


def test_plain_rehearse_messages():
    assert web.MESSAGES["rehearse_busy"] == (
        "Робот зараз зайнятий — пробну перевірку не запущено. "
        "Натисніть ще раз, коли робот зупиниться."
    )
    assert "налаштування зіпсоване — скажіть Власнику" in web.MESSAGES["rehearse_setup"]
    assert web.MESSAGES["ai_no_key"] == "ШІ не підключений — скажіть Власнику"
    assert web.MESSAGES["bot_no_token"] == "Бот не підключений — скажіть Власнику"


# --- Встановлення на Windows ---


def test_install_cmd_checks_that_archive_was_unpacked():
    ops = Path(__file__).resolve().parent.parent / "ops"
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_windows", ops / "build_windows.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    cmd = build.INSTALL_CMD
    check = cmd.index('if not exist "%~dp0app\\python\\python.exe"')
    assert check < cmd.index("-m shafa.install")  # перевірка — до запуску Python
    assert "Спершу розпакуйте архів: правою кнопкою → «Видобути все…»" in cmd
    assert "exit /b 1" in cmd


def test_install_messages_are_plain(tmp_path):
    from shafa import install

    with pytest.raises(install.InstallError, match="Запускайте «Встановити» з розпакованої папки"):
        install.install(tmp_path / "порожньо", tmp_path / "дані")
    assert "Готово. Відкрийте програму ярликом «Шафа-помічник». Це вікно можна закрити." == (
        install.DONE
    )


def test_install_main_prints_done(tmp_path, capsys, monkeypatch):
    from shafa import install

    source = tmp_path / "нова"
    (source / "python").mkdir(parents=True)
    (source / "lib" / "shafa").mkdir(parents=True)
    monkeypatch.setattr(install, "_stop_running", lambda data: None)
    assert install.main([str(source), "--no-setup"]) == 0
    assert install.DONE in capsys.readouterr().out
