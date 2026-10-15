"""Програма з ярлика (WIN-001): один екземпляр, екран у браузері, бот у фоні, «Вийти».

Ярлик запускає `pythonw.exe -m shafa app` — без вікна. Другий клік на ярлик нічого не запускає
вдруге: лише відкриває той самий екран (замок ОС `ui.lock` і адреса в `ui.url` у теці даних).
Закриття браузера програму не гасить; «Вийти» на екрані — гасить екран і бота, а робот, якщо
публікує, отримує «Стоп» (допише поточний товар — убивати його посеред кліку небезпечно).
База — `shafa.sqlite` у теці даних; Головна папка — `SHAFA_ROOT_DIR` (майстер налаштування).
"""

import os
import signal
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from shafa import config, db, photos, places, review, web
from shafa.robot import lock, publish

LOCK_NAME = "ui.lock"
URL_NAME = "ui.url"
DB_NAME = "shafa.sqlite"


def db_path() -> Path:
    return config.data_dir() / DB_NAME


def root_dir() -> Path:
    raw = config.setting("SHAFA_ROOT_DIR")
    return Path(raw) if raw else places.default_root()


def stop_process(lock_name: str, kill: Callable[[int, int], None] = os.kill) -> bool:
    """Загасити процес, що тримає замок (`kill` на Windows — TerminateProcess). True — був."""
    state = lock.busy(config.data_dir() / lock_name)
    if not state or not isinstance(state.get("pid"), int):
        return False
    try:
        kill(state["pid"], signal.SIGTERM)
    except OSError:
        return False
    return True


def quit_all(kill: Callable[[int, int], None] = os.kill) -> list[str]:
    """«Вийти»: бот — загасити; робот — «Стоп» (допише поточний товар). Повертає, що зроблено."""
    from shafa.bot import run as bot_run

    done = []
    if stop_process(bot_run.LOCK_NAME, kill):
        done.append("бота зупинено")
    if lock.busy() is not None and db_path().is_file():
        conn = db.connect(db_path())
        try:
            if publish.request(conn, "stop"):
                done.append("робот допише поточний товар і зупиниться")
        finally:
            conn.close()
    return done


def start(
    open_browser: Callable[[str], object] = webbrowser.open,
    port: int = web.DEFAULT_PORT,
    serve: Callable[[web.Server], None] = lambda server: server.serve_forever(),
    kill: Callable[[int, int], None] = os.kill,
) -> int:
    """Відкрити екран: уже працює — той самий у браузері; ні — сервер, бот і браузер."""
    data = config.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    url_file = data / URL_NAME
    try:
        held = lock.acquire("ui", data / LOCK_NAME, wait=0.5)
    except lock.Busy:
        url = url_file.read_text(encoding="utf-8").strip() if url_file.is_file() else ""
        open_browser(url or f"http://{web.HOST}:{port}/")
        return 0
    try:
        path = db_path()
        conn = db.connect(path)  # нова база створюється сама
        cache = path.parent / "thumbs"
        try:
            for item_id in review.purge_archive(conn):
                photos.drop_thumbnails(cache, item_id)
        finally:
            conn.close()
        holder: dict[str, web.Server] = {}

        def on_quit() -> list[str]:
            done = quit_all(kill)
            threading.Thread(target=holder["server"].shutdown, daemon=True).start()
            return done

        app = web.App(
            path, root_dir(), cache, channel_id=config.setting("TELEGRAM_CHANNEL_ID"),
            on_quit=on_quit,
        )  # fmt: skip
        server = holder["server"] = web.make_server(app, port)
        url = f"http://{web.HOST}:{app.port}/"
        url_file.write_text(url, encoding="utf-8")
        print(f"Екран перевірки: {url}")
        if app.start_bot() == "bot_started":
            print("Бот запускається окремим процесом (журнал — bot-run.log у теці даних)")
        open_browser(url)
        try:
            serve(server)
        finally:
            server.server_close()
    finally:
        url_file.unlink(missing_ok=True)
        held.release()
    return 0
