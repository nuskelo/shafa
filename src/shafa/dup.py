"""«Схоже оголошення»: ШІ сам порівнює фото й текст (DUP-001), людина бачить лише справжні сумніви.

Коли `robot.listings.find_duplicate` знайшов її товар, схожий на схвалену Картку, до «Проблеми» ще
один крок: запит до Claude (та сама модель і ключ, що для бланка) з двома фото — наше перше й фото
знайденого — і текстом обох. Відповідь `{"same": yes|no|unsure, "why": …}`:

- `no` — інша пара, публікуємо; `yes` — та сама, Картка в Архів; `unsure` — як до DUP-001
  (`possible_duplicate`, вирішує людина). Те саме, коли ШІ не запитано: немає фото, вичерпаний денний
  бюджет ШІ, помилка мережі чи ШІ, відповідь не за схемою.
- Обережне правило першої версії: ШІ каже `no`, а в даних явні збіги (кольори й довжина устілки
  однакові) — це `unsure`. Дані знайденого — лише зі знімка «Мої речі»: кольорів у ньому майже немає
  (TD-034), опису немає зовсім, тож правило діє, лише коли обидва збіги відомі.

Модуль не знає ні браузера, ні бази Карток: запит, відповідь і гроші. Куди йде рішення — справа
`robot.publish`.
"""

import base64
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import UnidentifiedImageError

from shafa import ai, config, fill, photos, review, rules
from shafa.photos import PreparedPhoto
from shafa.robot import lang
from shafa.robot.listings import Listing

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "duplicate.md"
MAX_TOKENS = 400
# Оцінка виходу для бюджету: JSON з двома полями, `why` — 1–2 речення.
OUTPUT_TOKENS_ESTIMATE = 200

YES, NO, UNSURE = "yes", "no", "unsure"
CODE = "duplicate"  # `problem_code` Картки в Архіві: ШІ впевнено сказав «та сама річ»
NOTE_CODE = "duplicate_checked"  # `problem_code` опублікованої Картки: ШІ сказав «інша пара»

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same": {"type": "string", "enum": [YES, NO, UNSURE]},
        "why": {"type": "string"},
    },
    "required": ["same", "why"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Verdict:
    """Рішення про схоже оголошення. `asked` — чи був запит до ШІ (інакше `why` — чому не було)."""

    same: str
    why: str
    asked: bool = False
    downgraded: bool = False  # ШІ казав «no», обережне правило зробило «unsure»


_AI_SAID = re.compile(r"ШІ не (?:впевнений|порівняв): .*", re.DOTALL)


def verdict_text(problem_text: str | None) -> str | None:
    """Слова ШІ з тексту «Проблеми» `possible_duplicate` (для екрана); немає — None."""
    match = _AI_SAID.search(problem_text or "")
    return match.group(0) if match else None


def unasked(why: str) -> Verdict:
    return Verdict(UNSURE, why)


def system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def _number(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _list(values: Any) -> str:
    return ", ".join(values) if values else "невідомо"


def our_facts(card: review.Card) -> str:
    f = {name: view.value for name, view in card.fields.items()}
    insole = rules.insole_lengths(card.post_text or "")
    lines = [
        f"Назва: {f['title'] or '—'}",
        f"Ціна на Шафі: {f['price']} грн" if f["price"] else "Ціна: немає",
        f"Розміри: {_list(f['sizes'])}",
        f"Кольори: {_list(f['colors'])}",
        "Довжина устілки: " + (", ".join(_number(x) for x in insole) if insole else "—"),
        f"Текст поста:\n{ai.post_for_ai(card.post_text or '')}",
    ]
    return "Новий товар (ще не опублікований):\n" + "\n".join(lines)


def their_facts(found: Listing) -> str:
    lines = [
        f"Назва: {found.name}",
        f"Бренд: {found.brand or 'невідомо'}",
        f"Ціна: {found.price} грн" if found.price else "Ціна: невідомо",
        f"Розміри: {_list(found.sizes)}",
        f"Кольори: {_list(found.colors)}",
        "Опису в знімку немає.",
    ]
    return "Уже опубліковане оголошення на Шафі:\n" + "\n".join(lines)


def other_post(conn: sqlite3.Connection, card: review.Card, found: Listing) -> int | None:
    """DUP-002: номер поста, з якого опубліковано знайдене оголошення, якщо це наша Картка з
    ІНШОГО поста. Чуже оголошення, невідомий пост і той самий пост — None."""
    row = conn.execute(
        "SELECT post_id FROM items WHERE shafa_id = ? AND id != ?", (found.id, card.id)
    ).fetchone()
    post = row[0] if row is not None else None
    return post if post is not None and post != card.post_id else None


def post_fact(card: review.Card, post: int) -> str:
    ours = f" (новий товар — з поста {card.post_id})" if card.post_id is not None else ""
    return (
        f"Факт: знайдене оголошення опубліковане нашою програмою з іншого поста {post}{ours}. "
        "Продавчиня зазвичай робить один пост на одну пару."
    )


def _image(photo: PreparedPhoto) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(photo.data).decode("ascii"),
        },
    }


def build_request(
    card: review.Card,
    found: Listing,
    ours: PreparedPhoto,
    theirs: PreparedPhoto,
    post: int | None = None,
) -> dict[str, Any]:
    """Параметри `client.messages.create`: Фото 1 — наше, Фото 2 — знайдене, далі тексти
    (і факт про інший пост, якщо `post` задано)."""
    facts = f"{our_facts(card)}\n\n{their_facts(found)}"
    if post is not None:
        facts += f"\n\n{post_fact(card, post)}"
    content = [
        {"type": "text", "text": "Фото 1 — новий товар:"},
        _image(ours),
        {"type": "text", "text": "Фото 2 — уже опубліковане оголошення:"},
        _image(theirs),
        {"type": "text", "text": facts},
    ]
    return {
        "model": ai.MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(),
        "thinking": {"type": "between_tools"},  # мислення вимкнене, як і в бланка
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": content}],
    }


def estimate(request: dict[str, Any], ours: PreparedPhoto, theirs: PreparedPhoto) -> ai.Usage:
    """Орієнтовні токени до запиту — щоб не вийти за денний бюджет: фото точно, текст грубо."""
    texts = [request["system"]]
    texts += [b["text"] for b in request["messages"][0]["content"] if b["type"] == "text"]
    return ai.Usage(
        sum(ai._tokens(t) for t in texts) + ours.tokens + theirs.tokens, OUTPUT_TOKENS_ESTIMATE
    )


def _known(values: Any) -> set[str]:
    return set(lang.keys([v for v in values or [] if v]))


def shared_signs(card: review.Card, found: Listing) -> list[str]:
    """Явні збіги за текстом, якщо обидва відомі: кольори й довжина устілки (обережне правило).

    Лише обидва разом — один збіг нічого не доводить (чорні черевики одного розміру бувають у всіх).
    Устілку знайденого беремо з його назви: опису в знімку немає.
    """
    colors = _known(card.fields["colors"].value)
    theirs_colors = _known(found.colors)
    ours_insole = set(rules.insole_lengths(card.post_text or ""))
    theirs_insole = set(rules.insole_lengths(found.name))
    if colors and colors == theirs_colors and ours_insole and ours_insole == theirs_insole:
        return ["однакові кольори", "однакова довжина устілки"]
    return []


def conservative(card: review.Card, found: Listing, verdict: Verdict) -> Verdict:
    """ШІ каже «no», але явні збіги є — «unsure» (перша версія обережна)."""
    if verdict.same != NO or not (signs := shared_signs(card, found)):
        return verdict
    why = f"{verdict.why} Але в даних збігаються: {', '.join(signs)} — перевірте самі."
    return Verdict(UNSURE, why, asked=True, downgraded=True)


def _answer(answer: dict[str, Any]) -> Verdict | None:
    same, why = answer.get("same"), answer.get("why")
    if same not in (YES, NO, UNSURE) or not isinstance(why, str) or not why.strip():
        return None
    return Verdict(same, why.strip(), asked=True)


class Judge:
    """Викликається роботом перед «Проблемою». Залежності ззовні — тести без мережі й ключів.

    `client` — фабрика клієнта Claude (ключ береться лише в момент потреби), `budget_left` —
    скільки мікродоларів лишилось у денному бюджеті ШІ.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Callable[[], Any],
        budget_left: Callable[[], int],
    ) -> None:
        self.conn, self.client, self.budget_left = conn, client, budget_left

    def __call__(
        self, card: review.Card, found: Listing, our_photo: Path | None, their_photo: bytes | None
    ) -> Verdict:
        if our_photo is None or their_photo is None:
            return unasked("немає фото одного з оголошень")
        try:
            ours = photos.prepare(our_photo)
            theirs = photos.prepare_bytes(their_photo, "shafa.jpg")
        except (OSError, UnidentifiedImageError):
            return unasked("фото не читається")
        request = build_request(card, found, ours, theirs, other_post(self.conn, card, found))
        try:
            left, cost = self.budget_left(), estimate(request, ours, theirs).cost_micro_usd
        except Exception as error:  # noqa: BLE001 — не вміємо порахувати гроші: не питаємо
            return unasked(f"бюджет ШІ не прочитано ({error})")
        if left < cost:
            return unasked("вичерпано денний бюджет ШІ")
        try:
            answer, usage = ai.call(self.client(), request)
        except ai.AiError as error:
            self._log(card.id, error.usage, str(error))
            return unasked(str(error))
        except config.ConfigError as error:
            return unasked(str(error))
        self._log(card.id, usage, None)
        if (verdict := _answer(answer)) is None:
            return unasked("відповідь ШІ не за схемою")
        return conservative(card, found, verdict)

    def _log(self, item_id: int, usage: ai.Usage | None, error: str | None) -> None:
        with self.conn:
            fill.log_request(self.conn, item_id, usage, error)
