"""ROBOT-014: вікно Шафи «У вас від'ємний баланс» поверх форми «Додати товар».

Вікно закриває форму, і робот хибно скаржиться на «не знайдено поле». Тут: розпізнати вікно, закрити
його лише хрестиком (чи Escape) і піти далі; не закрилось чи з'явилось знову на тій самій формі —
`BalanceStop`, зупинка сесії (причина загальна, Картка не винна).

Грошей не рухаємо **ніколи**: «Підтвердити» (списання з картки Продавчині) і «Поповнити» робот не
натискає — `safe_close` відсікає їх за словами, поки `guard.FORBIDDEN_CLICK` цих слів не знає (сам
`guard.py` не міняємо). Номер картки з вікна не читаємо: сторінка віддає лише суми. Червона смуга
внизу («Поповнити») формі не заважає — її не чіпаємо.
"""

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shafa import config, events

# Що не можна натискати, щоб закрити вікно: гроші й будь-які платні дії.
NEVER_CLICK = re.compile(
    r"підтверд|поповн|списан|оплат|сплат|погасит|картк|купит|підключ|confirm|pay|top.?up",
    re.IGNORECASE,
)
CLOSE_LABEL = re.compile(r"^\s*(?:×|✕|✖|x|закрити|close)?\s*$", re.IGNORECASE)

NOTE = "Шафа показала вікно від'ємного балансу — закрито, гроші не списувались"
STOP_TEXT = (
    "Шафа просить поповнити баланс{amount}. Робот нічого не платить. Відкрийте shafa.ua → "
    "Мій баланс, поповніть, потім «Старт». Не знаєте як — скажіть Власнику."
)
ALERT_NAME = "balance-alert"  # дата, коли вже писали Власнику (файл у теці даних)
SETTLE = 800  # мс: дати вікну зникнути після кліку

# Знаходить вікно (не червону смугу: у ній немає «списання»/«деактивації»), ставить мітку на кнопку
# закриття і повертає лише суми. Хрестик — контрольний елемент без тексту (або «×»/«Закрити») у
# правому верхньому куті вікна.
FIND = r"""() => {
  const head = /від['’ʼ`]?ємн\S*\s+баланс/i, tail = /списан|деактивац/i;
  const shown = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = e => e.innerText || '';
  const match = e => shown(e) && head.test(text(e)) && tail.test(text(e));
  let cands = [...document.querySelectorAll(
    "[role=dialog],[aria-modal=true],[class*=odal],[class*=opup],[class*=ialog],[class*=verlay]")]
    .filter(match);
  if (!cands.length) {
    cands = [...document.querySelectorAll('div,section')].filter(
      e => match(e) && getComputedStyle(e).position === 'fixed');
  }
  cands = cands.filter(e => !cands.some(o => o !== e && e.contains(o)));
  if (!cands.length) return null;
  const root = cands[0], box = root.getBoundingClientRect();
  document.querySelectorAll('[data-robot-balance-close]').forEach(
    e => e.removeAttribute('data-robot-balance-close'));
  let close = null, near = 1e9;
  for (const e of root.querySelectorAll("button,[role=button],a,svg,[class*=lose],[aria-label]")) {
    if (!shown(e)) continue;
    const r = e.getBoundingClientRect();
    const label = [e.innerText, e.value, e.getAttribute('aria-label'), e.getAttribute('title')]
      .filter(Boolean).join(' ');
    const corner = (box.right - r.right) / box.width + (r.top - box.top) / box.height;
    const inCorner = r.left > box.left + box.width / 2 && r.top < box.top + box.height / 3;
    if (inCorner && corner < near) { near = corner; close = [e, label]; }
  }
  if (close) close[0].setAttribute('data-robot-balance-close', '1');
  const page = document.body.innerText;
  const num = '(\\d[\\d\\s.,]*\\d|\\d)';
  const bal = page.match(new RegExp('від[\'’ʼ`]?ємн\\S*\\s+баланс\\s*[−–-]\\s*' + num + '\\s*грн', 'i'));
  const chg = text(root).match(new RegExp('списан\\S*\\s+' + num + '\\s*грн', 'i'));
  return {
    balance: bal ? bal[1].replace(/\s/g, '') : null,
    charge: chg ? chg[1].replace(/\s/g, '') : null,
    close: close ? close[1] : null,
  };
}"""


@dataclass(frozen=True)
class Seen:
    """Вікно було на формі. `closed` — після нашої дії його вже немає."""

    amount: str  # « (−193.15 грн)» чи порожньо, коли суму не прочитано
    closed: bool


class BalanceStop(Exception):
    """Вікно не закрилось чи повернулось на тій самій формі: сесію зупиняємо. `text` — для людини."""

    def __init__(self, amount: str = "") -> None:
        self.amount = amount
        self.text = STOP_TEXT.format(amount=amount)
        super().__init__(self.text)


def safe_close(label: str | None) -> bool:
    """Чи можна клікнути елемент-«хрестик» з таким підписом (innerText + aria-label…)."""
    return label is not None and not NEVER_CLICK.search(label) and bool(CLOSE_LABEL.match(label))


def amount_text(found: dict) -> str:
    if found.get("balance"):
        return f" (−{found['balance']} грн)"
    if found.get("charge"):
        return f" (до погашення ≈ {found['charge']} грн)"
    return ""


def _look(page: Any) -> dict | None:
    try:
        return page.evaluate(FIND)
    except Exception:  # noqa: BLE001 — немає сторінки / закрилась / навігація: «вікна не видно»
        return None


def dismiss(session: Any) -> Seen | None:
    """Вікно є → закрити хрестиком (потім Escape) і сказати, чи вдалось; немає → None."""
    page = getattr(session, "page", None)
    if page is None or (found := _look(page)) is None:
        return None
    amount = amount_text(found)
    if safe_close(found.get("close")):
        session.click(page.locator("[data-robot-balance-close]").first, "вікно балансу: закрити")
        _settle(page)
    if _look(page) is not None:
        session.press("Escape")  # крім хрестика — лише Escape; клавіш «Enter» тут немає
        _settle(page)
    return Seen(amount, closed=_look(page) is None)


def _settle(page: Any) -> None:
    with contextlib.suppress(Exception):  # лише пауза
        page.wait_for_timeout(SETTLE)


def guarded(
    session: Any, driver: Any, steps: list, run: Callable[..., list[str]]
) -> tuple[list[str], str | None]:
    """`run(steps, driver, strict=True)` з вікном балансу. Вікно до заповнення чи після «поле не
    знайдено» закривається один раз; друга поява на цій формі, чи не закрилось — `BalanceStop`.
    Повертає (нотатки, сума): сума не `None`, якщо вікно було й закрите (нотатка `NOTE` вже в
    списку). Не вікно — `FieldMissing` летить як і раніше."""
    from shafa.robot.form import FieldMissing

    notes: list[str] = []
    amount: str | None = None
    if (seen := dismiss(session)) is not None:
        if not seen.closed:
            raise BalanceStop(seen.amount)
        notes.append(NOTE)
        amount = seen.amount
    try:
        return notes + run(steps, driver, strict=True), amount
    except FieldMissing:
        if (seen := dismiss(session)) is None:
            raise
        if amount is not None or not seen.closed:  # вікно повернулось на тій самій формі
            raise BalanceStop(seen.amount) from None
    notes.append(NOTE)
    driver.reset()  # форма вже закрита від вікна, але заповнена частково — з чистого стану Картки
    try:
        return notes + run(steps, driver, strict=True), seen.amount
    except FieldMissing:
        if (again := dismiss(session)) is not None:
            raise BalanceStop(again.amount) from None
        raise


def announce(amount: str, today: date | None = None, folder: Path | None = None) -> bool:
    """Гучна тривога Власнику — раз на добу. Без номера картки: лише сума. Повертає, чи писали."""
    day = (today or datetime.now().astimezone().date()).isoformat()
    marker = (folder or config.data_dir()) / ALERT_NAME
    try:
        if marker.read_text(encoding="utf-8").strip() == day:
            return False
    except OSError:
        pass
    events.emit(
        "shafa.balance",
        f"Шафа показала вікно від'ємного балансу{amount}. Робот його закрив хрестиком, гроші не "
        "списував. Поповніть баланс у Шафі вручну, інакше вона може деактивувати акаунт",
        loud=True,
    )
    with contextlib.suppress(OSError):
        marker.write_text(day, encoding="utf-8")
    return True
