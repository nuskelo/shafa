"""Браузер робота: видимий встановлений Chrome з окремим профілем у теці даних програми.

Кожен запит проходить через `guard.request_verdict`, кожен клік — через `guard.click_allowed`,
кожна клавіша — через `guard.key_allowed`. Пароль робот не бачить: вхід робить Власник руками.
"""

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from shafa import config
from shafa.robot import guard

NEW_URL = "https://shafa.ua/uk/new"
LOGIN_PATH = "/login"
PUBLISH = re.compile(r"\b(CreateProduct|EditProduct|\w*Publish\w*)\b")


def profile_dir() -> Path:
    return config.data_dir() / "robot-profile"


def pause(low: float = 1.0, high: float = 2.5) -> None:
    """Темп людини."""
    time.sleep(random.uniform(low, high))


def operation(request: Any) -> str | None:
    """Назва GraphQL-операції запиту (для JSON-запитів), інакше None."""
    try:
        data = json.loads(request.post_data or "")
    except (ValueError, TypeError):
        return None
    item = data[0] if isinstance(data, list) and data else data
    return item.get("operationName") if isinstance(item, dict) else None


def launch(playwright: Any) -> Any:
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        profile, channel="chrome", headless=False, viewport={"width": 1400, "height": 1000}
    )


class Session:
    """Відкритий профіль робота із захистом. `allow_upload` — лише для `--dry-fill` (D3)."""

    def __init__(self, playwright: Any, allow_upload: bool = False) -> None:
        self.allow_upload = allow_upload
        self.blocked: list[tuple[str, str]] = []
        self.uploads = 0
        self.context = launch(playwright)
        self.context.route("**/*", self._route)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("dialog", self._dialog)

    @staticmethod
    def _dialog(dialog: Any) -> None:
        """«Покинути сторінку?» між Картками — так (форма не надсилається); інше — відмова."""
        if dialog.type == "beforeunload":
            dialog.accept()
        else:
            dialog.dismiss()

    def _route(self, route: Any, request: Any) -> None:
        allowed, reason = guard.request_verdict(
            request.method,
            request.url,
            request.headers.get("content-type", ""),
            request.post_data_buffer,
            self.allow_upload,
        )
        if allowed:
            self.uploads += reason == "завантаження фото"
            route.continue_()
        else:
            self.blocked.append((request.url.split("?")[0], reason))
            route.abort()

    def publish_attempts(self) -> list[str]:
        """Заблоковані спроби створити/змінити оголошення (має бути порожньо)."""
        return [r for _, r in self.blocked if PUBLISH.search(r)]

    def click(self, locator: Any, why: str = "") -> None:
        info = locator.evaluate(
            "e => ({text: [e.innerText, e.value, e.getAttribute('aria-label')].join(' '),"
            " type: e.getAttribute('type')})"
        )
        if not guard.click_allowed(info["text"], info["type"]):
            raise guard.Forbidden(f"Заборонений клік: {info['text'].strip()[:60]!r} ({why})")
        locator.click()
        pause()

    def press(self, key: str, in_keywords: bool = False) -> None:
        if not guard.key_allowed(key, in_keywords):
            raise guard.Forbidden(f"Клавіша {key} поза полем ключових слів")
        self.page.keyboard.press(key)

    def open_form(self) -> bool:
        """Відкрити форму «Додати товар». False — входу немає (Шафа веде на сторінку входу)."""
        self.page.goto(NEW_URL, wait_until="load")
        pause(2.0, 3.0)
        return LOGIN_PATH not in self.page.url and self.page.locator("input[name=titleUk]").count()

    def close(self) -> None:
        self.context.close()


def wait_for_manual_login(playwright: Any) -> None:
    """Відкрити профіль для ручного входу Власника; чекати, поки він закриє вікно.

    Без захисту мережі: вхід — це надсилання логіна, але його робить людина, не робот.
    """
    context = launch(playwright)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(NEW_URL)
    while context.pages:  # на Mac закрите вікно не закриває Chrome — чекаємо на сторінки
        time.sleep(0.5)
    context.close()  # Chrome закривається штатно: cookies входу записуються на диск
