"""Налаштування, які Продавчиня чи Власник міняють на екрані (WIN-001, OQ-006). Без секретів.

Значення перевіряються тими самими правилами, що їх потім читають робот і ШІ (`publish.daily_limit`,
`batch.limits`), і пишуться в `.env` у теці даних. Секрети (ключ ШІ, токен бота) — лише майстер.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from shafa import config, places, site
from shafa.batch import BATCH_SETTING, DAILY_USD, MONEY_SETTING, WAIT_DAYS, WAIT_SETTING
from shafa.robot import listings
from shafa.robot.publish import (
    DAILY_LIMIT,
    DAILY_LIMIT_SETTING,
    DELETE_SETTING,
    GROUPS_MAX,
    NEW_DEFAULT,
    NEW_SETTING,
    OLD_DEFAULT,
    OLD_SETTING,
)

ROOT_SETTING = "SHAFA_ROOT_DIR"


@dataclass(frozen=True)
class Option:
    name: str
    label: str
    hint: str
    default: str
    kind: str  # int / money / path / flag


OPTIONS = (
    Option(
        OLD_SETTING,
        "Старих на день (старі пости, одне фото)",
        f"від 0 до {GROUPS_MAX}; старих і нових разом — до {GROUPS_MAX}",
        str(OLD_DEFAULT),
        "int",
    ),
    Option(
        NEW_SETTING,
        "Нових на день (бот, папки з фото)",
        f"від 0 до {GROUPS_MAX}; старих і нових разом — до {GROUPS_MAX}",
        str(NEW_DEFAULT),
        "int",
    ),
    Option(
        DAILY_LIMIT_SETTING, "Публікацій на Шафі за добу", "від 1 до 100", str(DAILY_LIMIT), "int"
    ),
    Option(
        MONEY_SETTING,
        "Скільки можна витратити на ШІ за день, доларів",
        "від 0,01 до 100; на 3 $ ШІ заповнює ≈ 70 Карток",
        str(DAILY_USD),
        "money",
    ),
    Option(
        WAIT_SETTING,
        "Скільки днів чекати, перш ніж ШІ заповнить нову пару",
        "від 0 до 60; за цей час частина пар продається — і не коштує грошей",
        str(WAIT_DAYS),
        "int",
    ),
    Option(
        BATCH_SETTING,
        "Скільки Карток відправляти на ШІ за раз",
        "від 1 до 500; не змінювати без Власника",
        "50",
        "int",
    ),
    Option(
        listings.DAYS_SETTING,
        "Як часто перечитувати «Мої речі» на Шафі, днів",
        "від 1 до 30; опубліковане роботом дописується одразу",
        str(listings.DAYS_DEFAULT),
        "int",
    ),
    Option(ROOT_SETTING, "Головна папка з фото", "папка має існувати", "", "path"),
    Option(
        DELETE_SETTING,
        "Видаляти папки з «Опубліковано» через 3 дні",
        "Фото опублікованих пар і старих постів стираються назавжди через 3 дні.",
        "",
        "flag",
    ),
    Option(
        site.SEND_SETTING,
        "Відправляти на сайт нові пари з бота",
        "ключ сайту налаштовує Власник",
        "",
        "flag",
    ),
    Option(
        site.WAIT_SETTING,
        "Чекати днів перед сайтом",
        "від 0 до 60; 0 — одразу; видалене з каналу за цей час на сайт не йде",
        str(site.WAIT_DEFAULT),
        "int",
    ),
    Option(
        site.SOLD_SETTING,
        "Перевіряти продане щоночі",
        "Зник пост з каналу — на сайті «Продано». Не знімайте: інакше продані пари лишаться на сайті.",
        "1",
        "flag",
    ),
    Option(
        site.FUSE_SETTING,
        "Захист від помилкового «Продано»: якщо за ніч зникло більше постів, ніж стільки — "
        "«Продано» не надсилати, а спитати",
        "від 1 до 1000",
        str(site.FUSE_DEFAULT),
        "int",
    ),
)
_LIMITS = {
    OLD_SETTING: (0, GROUPS_MAX),
    NEW_SETTING: (0, GROUPS_MAX),
    DAILY_LIMIT_SETTING: (1, 100),
    WAIT_SETTING: (0, 60),
    BATCH_SETTING: (1, 500),
    listings.DAYS_SETTING: listings.DAYS_RANGE,
    site.WAIT_SETTING: site.WAIT_RANGE,
    site.FUSE_SETTING: site.FUSE_RANGE,
}


def current() -> dict[str, str]:
    """Що зараз діє (налаштоване або типове)."""
    values = {o.name: config.setting(o.name) or o.default for o in OPTIONS}
    if not values[ROOT_SETTING]:
        values[ROOT_SETTING] = str(places.default_root())
    for option in OPTIONS:
        if option.kind == "flag":
            flag = (values[option.name] or "").lower() in ("1", "так", "yes", "true")
            values[option.name] = "1" if flag else ""
    return values


def _unchanged_root(raw: str) -> bool:
    """Папку в полі не міняли: там те саме, що діє зараз (навіть якщо її вже нема на диску)."""
    return raw.strip('"') == current()[ROOT_SETTING]


def folder_warnings(path: Path) -> list[str]:
    """Про Головну папку: її немає на диску чи вона в OneDrive (збереженню не заважає)."""
    if not path.is_dir():
        return [f"Папки «{path}» зараз немає на комп'ютері — поверніть її або вкажіть іншу."]
    if places.in_onedrive(path):
        text = (
            "Головна папка в OneDrive: фото поїдуть у хмару, програма читатиме їх "
            f"повільно. Краще {places.default_root()}"
        )
        return [text]
    return []


def check(form: dict[str, str]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Форма → (значення для `.env`, помилки за назвою поля, попередження)."""
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    warnings: list[str] = []
    for option in OPTIONS:
        raw = (form.get(option.name) or "").strip()
        if option.kind == "flag":
            values[option.name] = "1" if raw else "0"
        elif option.kind == "int":
            low, high = _LIMITS[option.name]
            if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
                errors[option.name] = f"ціле число від {low} до {high}"
            else:
                values[option.name] = str(int(raw))
        elif option.kind == "money":
            # «$», пробіли й кома — як пише людина: «2,5 $»
            cleaned = raw.replace("$", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
            try:
                money = Decimal(cleaned)
            except InvalidOperation:
                errors[option.name] = "лише число, напр. 3 або 2,5"
                continue
            if not money.is_finite() or not Decimal(0) < money <= 100:
                errors[option.name] = "від 0,01 до 100"
            else:
                values[option.name] = format(money.normalize(), "f")
        else:
            path = Path(raw.strip('"')) if raw else None
            if path is not None and path.is_dir():
                values[option.name] = str(path)
                warnings += folder_warnings(path)
            elif path is not None and _unchanged_root(raw):
                # не змінювали — не блокуємо решту полів; папку лишаємо як є
                warnings += folder_warnings(path)
            else:
                where = f"Папки «{raw}» немає на комп'ютері." if raw else "Папку не вказано."
                errors[option.name] = (
                    f"{where} Вставте шлях до папки з фото "
                    "(Провідник → адресний рядок → копіювати)."
                )
    if OLD_SETTING in values and NEW_SETTING in values:
        total = int(values[OLD_SETTING]) + int(values[NEW_SETTING])
        if total > GROUPS_MAX:
            errors[OLD_SETTING] = errors[NEW_SETTING] = (
                f"старих і нових разом {total}, а можна не більше {GROUPS_MAX} на день — "
                "зменште одне з чисел"
            )
    return values, errors, warnings


def notes(values: dict[str, str]) -> dict[str, str]:
    """Попередження під полем (значення не міняються): добовий ліміт менший за старих + нових."""
    try:
        limit = int(values[DAILY_LIMIT_SETTING])
        total = int(values[OLD_SETTING]) + int(values[NEW_SETTING])
    except (KeyError, ValueError):
        return {}
    if limit >= total:
        return {}
    return {
        DAILY_LIMIT_SETTING: (
            f"Публікацій за добу ({limit}) менше, ніж старих + нових ({total}): "
            f"робот зупиниться на {limit}."
        )
    }


def save(form: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Перевірити й записати. Повертає (помилки, попередження); з помилками нічого не пише."""
    values, errors, warnings = check(form)
    if not errors:
        config.save_settings(values)
    return errors, warnings
