"""KLB-005: робот позначає проданим на Клубку те, на що Вікторія сказала «Так, продано».

`robot --klubok-marksold` і початок сесії публікації (після «Обновить все» й знімка, до першої
пари). Для кожного `sold.targets` (≤ 20 за сесію): `MarkSoldTicket(id)` → **один** GET рівно
`https://klubok.com/users/198135?pg=marksold&id=<id>` (`page.goto`; `--human-click` — людина тисне
«Пометить как проданное» на сторінці керування, guard пропускає лише цей id) → пауза 3–6 с; між
оголошеннями 5–10 с. Наприкінці — **один** запит `get-items type=2` («проданные и скрытые»): id там
з `sold: true` → `removed`, тиха подія `klubok.sold`; немає — гучна `klubok.sold_failed` і питання
знову: «ще активне» чи «зникло з активних, але серед проданих немає — перевірте на Клубку» (тоді ще
два запити `type=0` ∪ `type=1`, знімок не пишеться). 429 / немає входу / `/error429` — стоп (`MarkStop`),
надіслане без перевірки — назад у питання. Повтору без людини немає. Видалення з «Проданого» — ні.
"""

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from shafa import events
from shafa.klubok import snapshot, sold
from shafa.robot import klubok_form as form

LOGIN, TOO_MANY, CAPTCHA = "klubok_login", snapshot.TOO_MANY, "klubok_captcha"
NOTHING, DONE, FAILED = "nothing", "done", "sold_failed"
PAUSE = (5.0, 10.0)  # с між оголошеннями
SETTLE = (3.0, 6.0)  # с після GET
HUMAN_WAIT = 600.0  # с: `--human-click` — людина тисне посилання сама
LOGIN_TEXT = "Увійдіть у Клубок: python -m shafa robot --klubok-login"
ACTIVE_TEXT = "Позначка «продано» не підтвердилась: оголошення досі активне"
HIDDEN_TEXT = "Оголошення зникло з активних, але серед проданих його немає — перевірте на Клубку"
UNCHECKED = "Не вдалося перевірити, чи позначено проданим, — перевірте на Клубку"


class MarkStop(Exception):
    """Стоп з кодом сесії (`klubok_login`, `klubok_429`, `klubok_captcha`)."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


@dataclass
class Outcome:
    code: str = NOTHING
    removed: list[sold.Target] = field(default_factory=list)
    failed: list[tuple[sold.Target, str]] = field(default_factory=list)
    skipped: list[sold.Target] = field(default_factory=list)  # людина не натиснула

    @property
    def ok(self) -> bool:
        return not self.failed


class MarkSold:
    def __init__(
        self,
        conn: sqlite3.Connection,
        session: form.Session,
        *,
        limit: int = sold.LIMIT,
        human: bool = False,
        human_wait: float = HUMAN_WAIT,
        sleep: Callable[[float], None] = lambda seconds: None,
        stopping: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = print,
        pause: tuple[float, float] = PAUSE,
        settle: tuple[float, float] = SETTLE,
        page_pause: tuple[float, float] = (1.0, 2.0),
    ) -> None:
        self.conn, self.session, self.limit, self.human = conn, session, limit, human
        self.human_wait, self.sleep, self.stopping, self.log = human_wait, sleep, stopping, log
        self.pause, self.settle, self.page_pause = pause, settle, page_pause

    def _check(self) -> None:
        if self.session.too_many:
            raise MarkStop(TOO_MANY, f"Клубок відповів 429 (забагато запитів): "
                                     f"{self.session.too_many[0]}")  # fmt: skip
        if any("/error429" in url for url, _why in self.session.blocked):
            raise MarkStop(CAPTCHA, "Клубок показав захист від роботів (/error429)")

    def run(self, logged_in: bool = False) -> Outcome:
        """`logged_in` — вхід уже перевірено (початок сесії публікації)."""
        out = Outcome()
        targets = sold.targets(self.conn, self.limit)
        if not targets:
            return out
        if not logged_in:
            opened = self.session.open_form()
            if not opened.ok or self.session.user_id() != form.KLUBOK_USER:
                raise MarkStop(LOGIN, LOGIN_TEXT)
        self.log(f"Клубок: позначити проданим — {len(targets)}")
        sent: list[sold.Target] = []
        try:
            for number, target in enumerate(targets):
                if self.stopping():
                    break
                if number:
                    self.sleep(random.uniform(*self.pause))
                if self._send(target):
                    sent.append(target)
                    self.log(f"  «продано» надіслано: {target.klubok_id} · {target.name[:60]}")
                else:
                    out.skipped.append(target)
                    self.log(f"  не натиснуто: {target.klubok_id} — лишається в черзі на зняття")
                self._check()
                self.sleep(random.uniform(*self.settle))
        except MarkStop as stop:
            for target in sent:  # перевірити не можна — назад у питання
                sold.failed(self.conn, target, f"{UNCHECKED} ({stop.text})")
            if sent:
                self._loud([(t, UNCHECKED) for t in sent])
            raise
        if sent:
            self._verify(sent, out)
        out.code = FAILED if out.failed else DONE
        return out

    def _send(self, target: sold.Target) -> bool:
        if not self.human:
            return self.session.marksold(target.klubok_id)
        self.session.open_manage()
        self.log(
            f"Натисніть «Пометить как проданное» біля оголошення {target.klubok_id} "
            f"«{target.name[:60]}» (робот чекає до {self.human_wait / 60:.0f} хв; інші посилання "
            "заблоковані)"
        )
        return self.session.wait_marksold_click(target.klubok_id, self.human_wait)

    def _verify(self, sent: list[sold.Target], out: Outcome) -> None:
        try:
            done = snapshot.sold_ids(self.session.fetch_text)
        except snapshot.SnapshotError as error:
            for target in sent:
                sold.failed(self.conn, target, f"{UNCHECKED} ({error.text})")
                out.failed.append((target, UNCHECKED))
            self._loud(out.failed)
            self._check()  # 429 — ще й стоп
            return
        active: frozenset[int] | None = None
        for target in sent:
            if target.klubok_id in done:
                sold.removed(self.conn, target)
                out.removed.append(target)
                text = f"Клубок: позначено проданим {target.klubok_id} (пост {target.post_id})"
                self.log(f"  ✅ {text}")
                events.emit("klubok.sold", text, data={"klubok_id": target.klubok_id})
                continue
            if active is None:
                active = self._active()
            why = HIDDEN_TEXT if active is not None and target.klubok_id not in active else (
                ACTIVE_TEXT if active is not None else UNCHECKED
            )  # fmt: skip
            sold.failed(self.conn, target, why)
            out.failed.append((target, why))
        if out.failed:
            self._loud(out.failed)

    def _active(self) -> frozenset[int] | None:
        """Її активні й «Обновить» (лише читання, знімок не пишеться); не вдалось — None."""
        try:
            snap = snapshot.read(
                self.session.fetch_text,
                pause=lambda: self.sleep(random.uniform(*self.page_pause)),
                wait=self.sleep, log=self.log,
            )  # fmt: skip
        except snapshot.SnapshotError:
            return None
        return frozenset(item["id"] for item in snap.items)

    def _loud(self, failed: list[tuple[sold.Target, str]]) -> None:
        lines = [f"{t.klubok_id} (пост {t.post_id}): {why}" for t, why in failed[:5]]
        more = f" і ще {len(failed) - 5}" if len(failed) > 5 else ""
        text = "Клубок: «продано» не підтвердилось — " + "; ".join(lines) + more
        self.log(f"  ⚠️ {text}")
        events.emit("klubok.sold_failed", text, loud=True,
                    data={"ids": [t.klubok_id for t, _ in failed]})  # fmt: skip


def mark_sold(conn: sqlite3.Connection, session: form.Session, **kwargs) -> Outcome:
    logged_in = kwargs.pop("logged_in", False)
    return MarkSold(conn, session, **kwargs).run(logged_in=logged_in)
