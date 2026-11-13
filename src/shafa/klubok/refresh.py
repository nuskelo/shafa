"""KLB-010: черга Клубку завжди актуальна — без кнопки «Перерахувати чергу».

* `reclassify` (F10) — уже записані `klubok_waves` за поточними правилами `waves.kind` (тапочки —
  домашнє, хвиля 4; рукавиці, шапки, одяг — не для Клубку). Змінилась хвиля — рядок оновлено; тип
  більше не береться — рядок хвилі видалено, Картки «лише Клубок» цього поста (ще не на Клубку) —
  в Архів «не для Клубку». Повтор нічого не змінює, тож можна кликати на кожному старті; кожна
  зміна — рядок у журнал подій (`klubok.waves`).
* `refresh` — `reclassify` + `queue.sync` (лише коли Клубок увімкнено й налаштування цілі).
* `Background` — те саме в окремому потоці (≈ 2,5 с на копії 12.11 — екран не чекає): після
  старту програми, налаштувань Клубку, «Перечитати мої оголошення», кінця сесії робота. Кличе той,
  хто змінив дані; GET-сторінки нічого не пишуть.
"""

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shafa import db, events, review
from shafa.db import Status
from shafa.klubok import queue, waves

ARCHIVABLE = (Status.NEW, Status.REVIEW, Status.APPROVED, Status.PROBLEM)
ON_KLUBOK = ("publishing", "published", "sold_asked", "sold", "removed")


@dataclass
class Reclassified:
    moved: list[tuple[int, str, str]] = field(default_factory=list)  # пост, було, стало
    dropped: list[int] = field(default_factory=list)  # пости, що вже не для Клубку
    archived: list[int] = field(default_factory=list)  # Картки «лише Клубок» → Архів

    def text(self) -> str:
        parts = [f"{post}: {old} → {new}" for post, old, new in self.moved[:10]]
        if self.dropped:
            parts.append(f"не для Клубку: {', '.join(map(str, self.dropped[:10]))}")
        more = (
            len(self.moved)
            + len(self.dropped)
            - min(len(self.moved), 10)
            - min(len(self.dropped), 10)
        )
        return (
            "Хвилі Клубку перераховано новими правилами — "
            + "; ".join(parts)
            + (f" і ще {more}" if more > 0 else "")
        )


def reclassify(conn: sqlite3.Connection) -> Reclassified:
    """Рядки `klubok_waves` за поточними `waves.kind` / `wave_of` (див. модуль)."""
    done = Reclassified()
    rows = conn.execute(
        "SELECT w.post_id, w.wave, w.kind, p.text FROM klubok_waves w "
        "JOIN posts p ON p.post_id = w.post_id ORDER BY w.post_id"
    ).fetchall()
    stamp = db.now()
    with conn:
        for post_id, wave, kind, text in rows:
            new_kind = waves.kind(text)
            new_wave = waves.wave_of(new_kind)
            if new_kind == kind and new_wave == wave:
                continue
            if new_wave is None:
                conn.execute("DELETE FROM klubok_waves WHERE post_id = ?", (post_id,))
                done.dropped.append(post_id)
                for (item_id,) in conn.execute(
                    "SELECT i.id FROM items i LEFT JOIN klubok_ads k ON k.item_id = i.id "
                    "WHERE i.post_id = ? AND i.only_klubok = 1 AND i.purged_at IS NULL "
                    f"AND i.status IN ({', '.join('?' * len(ARCHIVABLE))}) "
                    f"AND COALESCE(k.state, '') NOT IN ({', '.join('?' * len(ON_KLUBOK))})",
                    (post_id, *ARCHIVABLE, *ON_KLUBOK),
                ).fetchall():
                    conn.execute(
                        "UPDATE items SET status = ?, archived_at = ?, problem_code = ?, "
                        "problem_text = ?, updated_at = ? WHERE id = ?",
                        (Status.ARCHIVED, stamp, review.NOT_FOR_KLUBOK,
                         f"Не для Клубку ({new_kind})", stamp, item_id),
                    )  # fmt: skip
                    conn.execute(
                        "DELETE FROM klubok_ads WHERE item_id = ? AND state IN (?, ?)",
                        (item_id, *queue.OPEN),
                    )
                    done.archived.append(item_id)
                continue
            conn.execute(
                "UPDATE klubok_waves SET wave = ?, kind = ? WHERE post_id = ?",
                (new_wave, new_kind, post_id),
            )
            done.moved.append((post_id, f"{kind}/{wave}", f"{new_kind}/{new_wave}"))
    if done.moved or done.dropped:
        events.emit(
            "klubok.waves",
            done.text(),
            data={"moved": len(done.moved), "dropped": done.dropped, "archived": done.archived},
        )
    return done


def refresh(conn: sqlite3.Connection, root: Path, today: datetime | None = None) -> int | None:
    """`reclassify` + `queue.sync`. Повертає кількість вердиктів або None (Клубок вимкнено чи
    налаштування з помилкою — черга не пишеться, як і раніше)."""
    reclassify(conn)
    if not queue.enabled():
        return None
    try:
        return len(queue.sync(conn, root, today))
    except queue.SetupError:
        return None


Dups = Callable[[sqlite3.Connection, Path], object]


class Background:
    """`refresh` в окремому потоці; поки йде — нові прохання зливаються в один повтор.
    KLB-011: `dups` — після черги, у тому самому потоці, перевірка дублів (`dupcheck.background`)."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        root: Callable[[], Path],
        log: Callable[[str], None],
        dups: Dups | None = None,
    ) -> None:
        self.connect, self.root, self.log, self.dups = connect, root, log, dups
        self._lock = threading.Lock()
        self._again = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._again = True
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name="klubok-sync")
            self._thread.start()

    def wait(self, timeout: float = 30) -> None:
        """Для тестів: дочекатись кінця."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            try:
                conn = self.connect()
                try:
                    root = self.root()
                    done = refresh(conn, root, datetime.now().astimezone())
                    if done is not None and self.dups is not None:
                        try:
                            self.dups(conn, root)
                        except Exception as error:  # noqa: BLE001 — черга вже записана
                            self.log(f"Дублі Клубку не перевірено: {type(error).__name__}: {error}")
                finally:
                    conn.close()
            except Exception as error:  # noqa: BLE001 — черга не важливіша за екран
                self.log(f"Черга Клубку не перерахована: {type(error).__name__}: {error}")
            with self._lock:
                if not self._again:
                    self._thread = None
                    return
                self._again = False
