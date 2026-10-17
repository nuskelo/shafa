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
    Option(MONEY_SETTING, "Витрати на ШІ за добу, $", "від 0,01 до 100", str(DAILY_USD), "money"),
    Option(
        WAIT_SETTING, "ШІ — через скільки днів після посту", "від 0 до 60", str(WAIT_DAYS), "int"
    ),
    Option(BATCH_SETTING, "Товарів в одному пакеті ШІ", "від 1 до 500", "50", "int"),
    Option(
        listings.DAYS_SETTING,
        "Перечитувати «Мої речі» кожні N днів",
        "від 1 до 30; опубліковане роботом дописується одразу",
        str(listings.DAYS_DEFAULT),
        "int",
    ),
    Option(ROOT_SETTING, "Головна папка з фото", "папка має існувати", "", "path"),
    Option(DELETE_SETTING, "Видаляти папки з «Опубліковано» через 3 дні", "", "", "flag"),
    Option(
        site.SEND_SETTING,
        "Відправляти на сайт нові пари з бота",
        "потрібен ключ сайту в майстрі налаштувань",
        "",
        "flag",
    ),
)
_LIMITS = {
    OLD_SETTING: (0, GROUPS_MAX),
    NEW_SETTING: (0, GROUPS_MAX),
    DAILY_LIMIT_SETTING: (1, 100),
    WAIT_SETTING: (0, 60),
    BATCH_SETTING: (1, 500),
    listings.DAYS_SETTING: listings.DAYS_RANGE,
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
            if not raw.isdigit() or not low <= int(raw) <= high:
                errors[option.name] = f"ціле число {option.hint}"
            else:
                values[option.name] = str(int(raw))
        elif option.kind == "money":
            try:
                money = Decimal(raw.replace(",", "."))
            except InvalidOperation:
                money = Decimal(-1)
            if not money.is_finite() or not Decimal(0) < money <= 100:
                errors[option.name] = f"сума {option.hint}"
            else:
                values[option.name] = format(money.normalize(), "f")
        else:
            path = Path(raw.strip('"')) if raw else None
            if path is None or not path.is_dir():
                errors[option.name] = "такої папки немає"
            else:
                values[option.name] = str(path)
                if places.in_onedrive(path):
                    warnings.append(
                        "Головна папка в OneDrive: фото поїдуть у хмару, програма читатиме їх "
                        f"повільно. Краще {places.default_root()}"
                    )
    if OLD_SETTING in values and NEW_SETTING in values:
        total = int(values[OLD_SETTING]) + int(values[NEW_SETTING])
        if total > GROUPS_MAX:
            errors[OLD_SETTING] = errors[NEW_SETTING] = (
                f"старих і нових разом {total}, а можна не більше {GROUPS_MAX} на день — "
                "зменш одне з чисел"
            )
    return values, errors, warnings


def save(form: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Перевірити й записати. Повертає (помилки, попередження); з помилками нічого не пише."""
    values, errors, warnings = check(form)
    if not errors:
        config.save_settings(values)
    return errors, warnings
