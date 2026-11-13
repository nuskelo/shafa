"""KLB-011: дублі Клубку вирішує програма — відбиток фото → ШІ-зір → лише тоді людина.

`check` бере Картки `problem klubok_duplicate`, яких програма ще не дивилась (немає маркера в
`problem_text`), і для кожної рахує всіх кандидатів (`dup.find_all` — щоразу заново: знімок міг
змінитись; `dup_klubok_id` — лише для екрана):

1. Немає кандидатів (оголошення продане) → «Інша пара — схожого на Клубку вже немає».
2. Фото: усі наші фото (+ запасне з Експорту) проти мініатюри її оголошення (чи фото Картки,
   виставленої програмою). Хоч один ≤ `photo_dup.T1` → «Дубль — фото збіглось…» (як «Так, дубль»).
3. Немає нашого фото, її фото немає/не картинка → людина «Фото не порівняно: …».
   Кандидатів > 3 чи оголошення програми ще публікується → людина «ШІ не порівняв: …».
4. ШІ (`dup.Judge(site="klubok")`), по кандидату: `yes` → «Дубль — ШІ: …»; усі `no` → «Інша пара —
   ШІ: …» (як «Інша пара — виставити»); інакше людина «ШІ не впевнений: …».

Тимчасове (мережа, бюджет ШІ, немає ключа, ліміт запитів за прохід) — без маркера: наступного
разу знову. 429/403 від Клубку — стоп усього проходу (подія). Кожне авторішення — тиха подія
`klubok.dup_auto`; «Ні, це інша пара» з Архіву — `queue.undo_auto_dup`.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shafa import dup as shafa_dup
from shafa import events
from shafa.klubok import dup, photo_dup, queue
from shafa.klubok.photo_dup import FetchFailed, FetchStop, Photos
from shafa.review import Card, load_card
from shafa.robot.listings import Listing

AUTO_DUP = queue.AUTO_DUP  # «Дубль — фото збіглось…» / «Дубль — ШІ: …»
AUTO_OTHER = "Інша пара — "  # «Інша пара — ШІ: …» / «Інша пара — схожого на Клубку вже немає»
NOT_COMPARED = "Фото не порівняно: "
UNSURE = "ШІ не впевнений: "
NOT_ASKED = "ШІ не порівняв: "  # людина без ШІ: кандидатів багато, оголошення ще публікується
MARKERS = (NOT_COMPARED, UNSURE, NOT_ASKED)
MAX_CANDIDATES = 3
AI_LIMIT = 40  # запитів ШІ за один прохід (`refresh.Background`); решта — наступного разу
DUP, OTHER, HUMAN, RETRY, ASK = "dup", "other", "human", "retry", "ask"
OUTCOMES = {
    DUP: "дубль", OTHER: "інша пара", HUMAN: "людині", RETRY: "повтор", ASK: "піде на ШІ",
}  # fmt: skip
AI_REQUEST_USD = 0.0068  # ≈ 0,68 ¢ за запит (проба Фази 0: 10 пар — 6,8 ¢)
REPORT_LINES = 60


def note(problem_text: str | None) -> str | None:
    """Що програма сказала про дубль, який лишила людині (для рядка «Проблем»); немає — None."""
    text = problem_text or ""
    return text if text.startswith(MARKERS) else None


def pending(conn: sqlite3.Connection) -> list[int]:
    """Картки з дублем, яких програма ще не дивилась (немає маркера), у порядку `item_id`."""
    rows = conn.execute(
        "SELECT item_id, problem_text FROM klubok_ads WHERE state = ? AND problem_code = ? "
        "ORDER BY item_id",
        (queue.PROBLEM, queue.DUPLICATE),
    )
    return [r[0] for r in rows if note(r[1]) is None]


@dataclass
class Decision:
    item_id: int
    post_id: int | None
    outcome: str
    text: str = ""  # для `klubok_ads.problem_text` (у «повтор» — чому)
    klubok_id: int | None = None
    name: str | None = None
    way: str = ""  # «фото, відстань 0» / «ШІ» — для події


@dataclass
class Report:
    decisions: list[Decision] = field(default_factory=list)
    ai_requests: int = 0
    ai_would: int = 0  # `--dry-run`: скільки запитів ШІ знадобилось би (ШІ не питаємо)
    photo_requests: int = 0
    stopped: str | None = None  # 429/403 — чому прохід зупинився
    left: int = 0  # Картки, до яких цього разу не дійшли

    def count(self, outcome: str) -> int:
        return sum(d.outcome == outcome for d in self.decisions)

    def lines(self, limit: int = REPORT_LINES) -> list[str]:
        out = [
            f"Перевірено Карток: {len(self.decisions)} · "
            + " · ".join(
                f"{OUTCOMES[k]}: {self.count(k)}" for k in OUTCOMES if k != ASK or self.count(k)
            )
            + (f" · не дійшли: {self.left}" if self.left else ""),
            f"Запитів ШІ: {self.ai_requests} · завантажено фото: {self.photo_requests}",
        ]
        if self.count(ASK):
            out.append(
                f"Піде на ШІ: {self.count(ASK)} Карток, {self.ai_would} запитів "
                f"≈ {self.ai_would * AI_REQUEST_USD:.2f} $ (за прохід — не більше {AI_LIMIT})"
            )
        if self.stopped:
            out.append(f"Зупинено: {self.stopped}")
        for d in self.decisions:
            if len(out) >= limit - 1:
                out.append(f"… ще {len(self.decisions) - (len(out) - 2)} Карток")
                break
            post = f"пост {d.post_id}" if d.post_id is not None else f"Картка {d.item_id}"
            said = (
                d.text
                if d.text.startswith((AUTO_DUP, AUTO_OTHER))
                else (f"{OUTCOMES[d.outcome]} — {d.text}")
            )
            out.append(f"{post}: {said}"[:200])
        return out


class _Retry(Exception):
    """Тимчасово — Картку перевіримо наступного разу."""


Judge = Callable[[Card, Listing, Path, bytes], shafa_dup.Verdict]


@dataclass
class _Candidate:
    found: dup.Dup
    theirs: list[int] = field(default_factory=list)  # відбитки її фото
    dist: int | None = None
    our_photo: Path | None = None
    other: Card | None = None  # Картка, виставлена програмою
    missing: str = ""  # чому фото оголошення немає (постійно)


def _listing(conn: sqlite3.Connection, found: dup.Dup) -> Listing:
    """Факти оголошення для ШІ: її — зі знімка, виставлене програмою — з тієї Картки."""
    row = conn.execute(
        "SELECT name, brand, price, sizes FROM klubok_listings WHERE id = ?", (found.klubok_id,)
    ).fetchone()
    if row is not None:
        sizes = tuple(json.loads(row["sizes"] or "[]")) or tuple(dup.title_sizes(row["name"]))
        return Listing(
            found.klubok_id or 0, row["name"], row["brand"], "", sizes, found.url or "",
            price=row["price"],
        )  # fmt: skip
    return Listing(found.klubok_id or 0, found.name, None, "", (), found.url or "")


def _ours_listing(card: Card) -> Listing:
    facts = dup.card_facts(card)
    return Listing(0, facts.name, facts.brand, "", facts.sizes, "", price=facts.price)


class Checker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        photos: Photos,
        judge: Judge | None,
        ai_limit: int = AI_LIMIT,
        estimate: bool = False,
    ) -> None:
        self.conn, self.root, self.photos, self.judge = conn, root, photos, judge
        self.estimate = estimate  # `--dry-run`: ШІ не питаємо, лише рахуємо, скільки піде
        self.ai_would = 0
        self.ai_left = ai_limit
        self.ai_requests = 0
        self.ai_off: str | None = None  # бюджет/ключ — до кінця проходу ШІ не кличемо

    def decide(self, card: Card) -> Decision:
        base = {"item_id": card.id, "post_id": card.post_id}
        found = dup.find_all(self.conn, card)
        if not found:
            return Decision(**base, outcome=OTHER, text=AUTO_OTHER + "схожого на Клубку вже немає")
        ours = photo_dup.hashed(photo_dup.our_photos(card, self.root))
        if not ours:
            first = found[0]
            return Decision(
                **base, outcome=HUMAN, text=NOT_COMPARED + "у Картки немає фото, що читається",
                klubok_id=first.klubok_id, name=first.name,
            )  # fmt: skip
        try:
            candidates = [self._photo(c, ours) for c in found]
        except FetchFailed as error:  # тимчасово (мережа, 5xx)
            return Decision(**base, outcome=RETRY, text=str(error))
        for c in candidates:
            if c.dist is not None and c.dist <= photo_dup.T1:
                return Decision(
                    **base, outcome=DUP, klubok_id=c.found.klubok_id, name=c.found.name,
                    text=f"{AUTO_DUP}фото збіглось з оголошенням {self._who(c.found)}",
                    way=f"фото, відстань {c.dist}",
                )  # fmt: skip
        for c in candidates:
            if c.missing:
                return self._human(base, c, NOT_COMPARED + c.missing)
        if len(candidates) > MAX_CANDIDATES:
            text = f"схожих оголошень {len(candidates)} — порівняйте самі"
            return self._human(base, candidates[0], NOT_ASKED + text)
        for c in candidates:
            if c.found.whose == dup.OURS and c.found.klubok_id is None:
                return self._human(base, c, NOT_ASKED + "оголошення програми ще виставляється")
        return self._ask(card, base, candidates)

    def _who(self, found: dup.Dup) -> str:
        return str(found.klubok_id) if found.klubok_id is not None else f"«{found.name}»"

    def _human(self, base: dict, c: _Candidate, text: str) -> Decision:
        return Decision(**base, outcome=HUMAN, text=text, klubok_id=c.found.klubok_id,
                        name=c.found.name)  # fmt: skip

    def _photo(self, found: dup.Dup, ours: list[photo_dup.Hashed]) -> _Candidate:
        c = _Candidate(found)
        if found.whose == dup.OURS:
            c.other = load_card(self.conn, found.item_id) if found.item_id else None
            files = photo_dup.our_photos(c.other, self.root) if c.other else []
            c.theirs = [h.hash for h in photo_dup.hashed(files)]
            if not c.theirs:
                c.missing = f"у виставленої програмою Картки «{found.name}» немає фото"
        else:
            try:
                c.theirs = [photo_dup.theirs_hash(self.photos.small(found.klubok_id, found.photo))]
            except FetchFailed as error:
                if not error.permanent:
                    raise
                c.missing = f"фото оголошення {found.klubok_id}: {error}"
            except (OSError, ValueError):
                c.missing = f"фото оголошення {found.klubok_id} не читається"
        best = photo_dup.closest(ours, c.theirs)
        if best is not None:
            c.dist, c.our_photo = best
        return c

    def _their_bytes(self, c: _Candidate) -> bytes:
        if c.found.whose == dup.OURS:
            files = photo_dup.our_photos(c.other, self.root) if c.other else []
            return files[0].read_bytes()
        return self.photos.large(c.found.klubok_id, c.found.photo)

    def _ask(self, card: Card, base: dict, candidates: list[_Candidate]) -> Decision:
        if self.judge is None:
            if self.estimate:
                self.ai_would += len(candidates)
                return Decision(**base, outcome=ASK, text=f"запитів ШІ: {len(candidates)}")
            return Decision(**base, outcome=RETRY, text="ШІ вимкнено (--no-ai)")
        if self.ai_off:
            return Decision(**base, outcome=RETRY, text=self.ai_off)
        unsure: tuple[_Candidate, str] | None = None
        whys = []
        for c in candidates:
            if self.ai_left <= 0:
                return Decision(**base, outcome=RETRY, text="ліміт запитів ШІ за прохід")
            try:
                theirs = self._their_bytes(c)
            except FetchFailed as error:
                if error.permanent:
                    return self._human(base, c, NOT_COMPARED + str(error))
                return Decision(**base, outcome=RETRY, text=str(error))
            except OSError as error:
                return self._human(base, c, NOT_COMPARED + f"фото не читається ({error})")
            found = _ours_listing(c.other) if c.other else _listing(self.conn, c.found)
            verdict = self.judge(card, found, c.our_photo, theirs)
            if verdict.asked:
                self.ai_left -= 1
                self.ai_requests += 1
            if verdict.retry:
                self.ai_off = f"ШІ: {verdict.why}"
                return Decision(**base, outcome=RETRY, text=self.ai_off)
            if verdict.same == shafa_dup.YES:
                return Decision(
                    **base, outcome=DUP, klubok_id=c.found.klubok_id, name=c.found.name,
                    text=f"{AUTO_DUP}ШІ: {verdict.why}", way="ШІ",
                )  # fmt: skip
            if verdict.same == shafa_dup.NO:
                whys.append(verdict.why)
            elif unsure is None:
                prefix = UNSURE if verdict.asked else NOT_ASKED
                unsure = (c, prefix + verdict.why)
        if unsure is not None:
            return self._human(base, *unsure)
        return Decision(**base, outcome=OTHER, text=f"{AUTO_OTHER}ШІ: {whys[0]}", way="ШІ")


def apply(conn: sqlite3.Connection, d: Decision) -> bool:
    """Записати рішення (лише якщо Картка й досі «Проблема» з дублем); подія — для авторішень."""
    if d.outcome == DUP:
        done = queue.dup_yes(conn, d.item_id, d.klubok_id, d.name, d.text)
    elif d.outcome == OTHER:
        done = queue.dup_other(conn, d.item_id, d.text)
    elif d.outcome == HUMAN:
        done = queue.dup_mark(conn, d.item_id, d.klubok_id, d.name, d.text)
    else:
        return False
    if done and d.outcome in (DUP, OTHER):
        post = f"пост {d.post_id}" if d.post_id is not None else f"Картка {d.item_id}"
        events.emit(
            "klubok.dup_auto",
            f"Клубок: {post} — {d.text}",
            data={"item": d.item_id, "listing": d.klubok_id, "outcome": d.outcome, "way": d.way},
        )
    return done


def check(
    conn: sqlite3.Connection,
    root: Path,
    photos: Photos,
    judge: Judge | None,
    limit: int = AI_LIMIT,
    *,
    write: bool = True,
    estimate: bool = False,
) -> Report:
    """Один прохід по Картках з непереглянутим дублем. `limit` — запитів ШІ за прохід;
    `write=False` — лише звіт (нічого не пише в базу); `estimate` — без `judge`: Картки, які
    фото не вирішило, рахуються як «піде на ШІ» (кількість запитів і ціна)."""
    report = Report()
    checker = Checker(conn, root, photos, judge, limit, estimate)
    todo = pending(conn)
    for n, item_id in enumerate(todo):
        card = load_card(conn, item_id)
        if card is None:
            continue
        try:
            decision = checker.decide(card)
        except FetchStop as error:
            report.stopped = str(error)
            report.left = len(todo) - n
            if write:
                events.emit("klubok.dup_stop", f"Клубок: перевірку дублів зупинено — {error}")
            break
        report.decisions.append(decision)
        if write:
            apply(conn, decision)
    report.ai_requests = checker.ai_requests
    report.ai_would = checker.ai_would
    report.photo_requests = photos.requests
    return report


def make_judge(conn: sqlite3.Connection) -> shafa_dup.Judge:
    """Суддя Клубку: та сама модель, ключ і денний бюджет ШІ, що й бланк (ключ — у момент запиту)."""
    from datetime import datetime

    from shafa import ai, batch, config

    def budget_left() -> int:
        return batch.budget_left(conn, datetime.now().astimezone(), batch.limits())

    return shafa_dup.Judge(
        conn, lambda: ai.make_client(config.api_key()), budget_left, site=shafa_dup.KLUBOK
    )


def background(conn: sqlite3.Connection, root: Path) -> Report:
    """Прохід для `refresh.Background`: кеш фото в теці даних, ≤ `AI_LIMIT` запитів ШІ."""
    if not pending(conn):
        return Report()
    return check(conn, root, Photos(photo_dup.cache_dir()), make_judge(conn), AI_LIMIT)
