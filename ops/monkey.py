"""QA-004: «мавпа» по всій програмі й роботові на підставній Шафі. Не входить у `make ci`.

    make monkey                          # типові параметри
    make monkey SEED=7 ACTIONS=500 CYCLES=100

Усе відбувається в тимчасовій теці (`/tmp/qa-004-*`): вигадані дані (`monkey_seed.py`), порожній
`.env`, порт 8799, `--fake-shafa`. Справжніх запитів немає: дочірні процеси бачать лише чорну
діру замість проксі, браузер пускає тільки 127.0.0.1, `SHAFA_TESTING=1` ховає справжні теки й `.env`.
Підсумок — `report.md` і `report.json` (і скриншоти проблемних місць) у теці `--out`.
Код виходу: 0 — знахідок немає, 1 — є.
"""

import argparse
import contextlib
import http.client
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import monkey_seed

REPO = Path(__file__).resolve().parent.parent
WIDTHS = (1400, 390)
TABS = ("/review", "/approved", "/problem", "/published", "/sessions", "/archived", "/settings",
        "/refill")  # fmt: skip
VARIANTS = ("/review?f=warn", "/review?f=ok", "/problem?f=warn", "/approved?f=ok")
JUNK = (
    "", " ", "0", "-1", "999999999999", "а" * 400, "<script>window.__xss=3</script>",
    '"><img src=x onerror="window.__xss=4">', "'; DROP TABLE items; --", "Ёжик 😀 \u202e تست",
    "1200грн", "38,5", "https://example.com/x", "\n\n", "%s %d {0} {{x}}", "../../etc/passwd",
)  # fmt: skip
ROBOT_FORM = 'form[action="/robot"]'
PAGE_CHECK = """() => {
  const de = document.documentElement, W = window.innerWidth;
  const clipped = e => {
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') return true;
    }
    return false;
  };
  let el = null;
  if (de.scrollWidth - W > 1) {
    for (const e of document.body.querySelectorAll('*')) {
      const r = e.getBoundingClientRect();
      if (r.width > 0 && r.right > W + 1 && getComputedStyle(e).position !== 'fixed' && !clipped(e)) {
        el = e.tagName.toLowerCase() + (e.className ? '.' + String(e.className).split(' ')[0] : '')
          + ' «' + (e.innerText || '').trim().slice(0, 30) + '» +' + Math.round(r.right - W) + 'px';
        break;
      }
    }
  }
  const text = document.body ? document.body.innerText : '';
  return {
    len: text.trim().length, over: de.scrollWidth - W, el,
    broken: [...document.images].filter(i => i.hasAttribute('src') && i.complete && i.naturalWidth === 0).map(i => i.getAttribute('src') || i.outerHTML.slice(0, 140)),
    bad: [...text.matchAll(/\\{\\{|\\{%|undefined|\\bNone\\b|\\bNaN\\b|Traceback|Щось пішло не так/g)].map(m => text.slice(Math.max(0, m.index - 40), m.index + 30).replace(/\\s+/g, ' ')),
    xss: window.__xss || 0,
  };
}"""
TAG = """(skip) => {
  document.querySelectorAll('details').forEach(d => { d.open = true; });
  document.querySelectorAll('[data-mk]').forEach(e => e.removeAttribute('data-mk'));
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(
      'button, input[type=submit], a[href], summary, [role=button]')) {
    if (!shown(e) || e.disabled || e.closest(skip) || e.closest('[aria-hidden=true]')
        || e.getBoundingClientRect().right <= 0) continue;
    const href = e.getAttribute('href');
    if (href !== null && (!href.startsWith('/') || href.startsWith('/photo') || href.startsWith('/thumb')
        || href.startsWith('/duplicate') || e.target === '_blank')) continue;
    e.setAttribute('data-mk', String(n));
    out.push({n, label: ((e.innerText || e.value || e.getAttribute('aria-label') || '') + '').trim().slice(0, 40),
              href, tag: e.tagName.toLowerCase()});
    n++;
  }
  return out;
}"""
FIELDS = """() => [...document.querySelectorAll(
  'input:not([type=hidden]):not([type=file]):not([type=submit]):not([type=checkbox]):not([type=radio]),'
  + 'textarea')].filter(e => e.getBoundingClientRect().width > 0 && !e.disabled && !e.readOnly
  && !e.closest('form[action="/settings"]')).length"""


class Findings:
    """Знахідки без повторів: ключ — вид + шаблон адреси + початок опису."""

    def __init__(self) -> None:
        self.items: dict[tuple, dict] = {}
        self.shots: list[str] = []
        self.known: list[str] = []  # адреси, чия відсутність очікувана
        self.known_hits = 0

    @staticmethod
    def pattern(where: str) -> str:
        return re.sub(r"\d+", "N", where)

    def add(self, kind: str, where: str, detail: str, url: str | None = None) -> None:
        if kind in ("http_404", "broken_image") and any(k in where + detail for k in self.known):
            self.known_hits += 1  # відома пастка сіду (порожній файл фото): сервер каже 404, не 500
            return
        key = (kind, self.pattern(where), self.pattern(detail)[:70])
        if key in self.items:
            self.items[key]["count"] += 1
            return
        self.items[key] = {"kind": kind, "where": where, "detail": detail[:600], "url": url,
                           "count": 1}  # fmt: skip
        print(f"  ! {kind}: {where} — {detail[:140]}")


class Stage:
    """Лічильники для звіту."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.notes: list[str] = []


class Server:
    """`shafa ui --fake-shafa` у дочірньому процесі з очищеним середовищем."""

    def __init__(self, base: Path, data: Path, root: Path, port: int, seed: int, weights: str):
        self.base, self.data, self.root, self.port = base, data, root, port
        self.log = base / "server.log"
        self.fake_log = base / "fake.jsonl"
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("ANTHROPIC", "TELEGRAM", "SITE_", "SHAFA_", "CLAUDE"))
        }  # fmt: skip
        dead = "http://127.0.0.1:9"  # будь-який вихід назовні — одразу помилка, а не запит
        self.env.update(
            SHAFA_TESTING="1", SHAFA_DATA_DIR=str(data), SHAFA_LEGACY_ENV=str(base / "no-legacy.env"),
            SHAFA_FAKE_SCENARIOS=weights, SHAFA_FAKE_SEED=str(seed), SHAFA_FAKE_LOG=str(self.fake_log),
            HTTP_PROXY=dead, HTTPS_PROXY=dead, ALL_PROXY=dead, NO_PROXY="127.0.0.1,localhost",
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",  # інакше рядок «Екран перевірки» не дійде до файла вчасно
        )  # fmt: skip
        self.process: subprocess.Popen | None = None
        self.token = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        out = open(self.log, "ab")  # noqa: SIM115 — живе, поки живе сервер
        self.process = subprocess.Popen(
            [sys.executable, "-m", "shafa", "ui", "--db", str(self.data / "shafa.sqlite"),
             "--root", str(self.root), "--port", str(self.port), "--no-browser", "--fake-shafa"],
            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=self.env,
            cwd=self.base,
        )  # fmt: skip
        deadline = time.time() + 40
        while time.time() < deadline:
            text = self.log.read_text(encoding="utf-8", errors="replace")
            if f"Екран перевірки: {self.url}/" in text:
                break
            if "зайнятий" in text or self.process.poll() is not None:
                raise SystemExit(f"Сервер не стартував (порт {self.port}?):\n{text[-600:]}")
            time.sleep(0.3)
        else:
            raise SystemExit("Сервер не відповів за 40 с")
        self.token = self.fetch_token()

    def request(self, method: str, path: str, form: dict | None = None) -> tuple[int, str, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            body = urlencode(form).encode() if form is not None else None
            headers = {"Origin": self.url}
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            return (
                response.status,
                response.getheader("Location") or "",
                response.read().decode("utf-8", "replace"),
            )
        finally:
            conn.close()

    def fetch_token(self) -> str:
        _, _, html = self.request("GET", "/review")
        match = re.search(r'name="token" value="([^"]+)"', html)
        if not match:
            raise SystemExit("Токен на сторінці не знайдено")
        return match[1]

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def tracebacks(self) -> list[str]:
        text = self.log.read_text(encoding="utf-8", errors="replace")
        return [t.strip().splitlines()[-1] for t in text.split("Traceback")[1:]]

    def robots(self) -> list[str]:
        """Живі процеси робота цієї «мавпи» (шукаємо за тимчасовою текою в аргументах)."""
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=False
        ).stdout
        return [
            line.strip() for line in out.splitlines()
            if " -m shafa robot " in line and str(self.data / "shafa.sqlite") in line
        ]  # fmt: skip

    def stop(self) -> None:
        for line in self.robots():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(line.split()[0]), 15)
        if self.process and self.alive():
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(10)
            if self.alive():
                self.process.kill()


def ro(data: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{data / 'shafa.sqlite'}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


class Browser:
    """Playwright Chromium: спостерігач за відповідями, консоллю, діалогами; лише 127.0.0.1:порт."""

    def __init__(self, playwright, server: Server, F: Findings, stage: Stage, out: Path) -> None:
        self.server, self.F, self.stage, self.out = server, F, stage, out
        self.browser = playwright.chromium.launch(headless=True)
        self.where = "—"
        self.outside: list[str] = []

    def page(self, width: int):
        height = 844 if width < 600 else 900
        context = self.browser.new_context(
            viewport={"width": width, "height": height}, is_mobile=width < 600,
            has_touch=width < 600, locale="uk-UA",
        )  # fmt: skip
        context.route("**/*", self._route)
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on("response", lambda r, w=width: self._response(r, w))
        page.on("console", lambda m, w=width: self._console(m, w))
        page.on("pageerror", lambda e, w=width: self.F.add("js_error", self.where, f"{w}px: {e}"))
        page.on("dialog", self._dialog)
        page.on("requestfailed", lambda r, w=width: self._failed(r, w))
        return page

    def _route(self, route, request) -> None:
        if request.url.startswith(self.server.url + "/") or request.url.startswith("data:"):
            route.continue_()
            return
        self.outside.append(request.url.split("?")[0])
        route.abort()

    def _response(self, response, width: int) -> None:
        if not response.url.startswith(self.server.url):
            return
        self.stage.counts["responses"] += 1
        if response.status >= 400:
            path = urlsplit(response.url).path
            self.F.add(
                f"http_{response.status}", path, f"{width}px, дія: {self.where}", response.url
            )

    def _console(self, message, width: int) -> None:
        if message.type == "error":
            text = message.text
            if "Failed to load resource" in text:  # той самий запит уже записаний як http_4xx/5xx
                return
            self.F.add("console_error", self.where, f"{width}px: {text[:300]}")

    def _failed(self, request, width: int) -> None:
        if request.url.startswith(self.server.url) and "net::ERR_ABORTED" not in (
            request.failure or ""
        ):
            self.F.add(
                "request_failed", urlsplit(request.url).path, f"{width}px: {request.failure}"
            )

    def _dialog(self, dialog) -> None:
        if dialog.type in ("confirm", "beforeunload"):  # «Залишити сторінку?» — звичайна поведінка
            dialog.accept()
            return
        self.F.add("dialog", self.where, f"{dialog.type}: {dialog.message[:100]}")
        dialog.dismiss()

    def check(self, page, where: str, width: int) -> None:
        """Порожня сторінка, зламані картинки, горизонтальна прокрутка, шматки шаблону."""
        self.stage.counts["checks"] += 1
        if page.url == "about:blank":  # «Назад» на початок історії
            return
        with contextlib.suppress(Exception):  # без цього перевіряли б сторінку, що ще вантажиться
            page.wait_for_load_state("load", timeout=4000)
        try:
            try:
                info = page.evaluate(PAGE_CHECK)
            except Exception:  # noqa: BLE001 — сторінка пішла посеред перевірки (редірект): ще раз
                page.wait_for_load_state("load", timeout=5000)
                info = page.evaluate(PAGE_CHECK)
        except Exception as error:  # noqa: BLE001
            self.F.add(
                "check_failed", where, f"{width}px: {type(error).__name__}: {str(error)[:80]}"
            )
            return
        if info["len"] < 15:  # може бути перехід посеред перевірки — дивимось ще раз
            page.wait_for_timeout(600)
            with contextlib.suppress(Exception):
                page.wait_for_load_state("load", timeout=4000)
                info = page.evaluate(PAGE_CHECK)
        url = page.url
        if info["len"] < 15:
            self.F.add("empty_page", where, f"{width}px: порожня сторінка", url)
        if width < 600 and info["over"] > 1:
            self.F.add("overflow_x", where, f"{width}px: +{info['over']}px — {info['el']}", url)
        for src in info["broken"]:
            self.F.add("broken_image", where, f"{width}px: {src}", url)
        for word in sorted(set(info["bad"])):
            self.F.add("bad_text", where, f"{width}px: у тексті «…{word}…»", url)
        if info["xss"]:
            self.F.add("xss", where, f"виконався скрипт з тексту ({info['xss']})", url)

    def shot(self, url: str, name: str) -> None:
        """Скриншот проблемного місця на 1400 і 390 (лише GET-адреси)."""
        for width in WIDTHS:
            page = self.page(width)
            try:
                with contextlib.suppress(Exception):  # скриншот не важливіший за сам звіт
                    page.goto(self.server.url + url, wait_until="load")
                    page.wait_for_timeout(400)
                    path = self.out / "shots" / f"{name}-{width}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(path), full_page=True)
                    self.F.shots.append(str(path.relative_to(self.out)))
            finally:
                page.context.close()

    def close(self) -> None:
        self.browser.close()


def goto(b: Browser, page, url: str, width: int) -> None:
    b.where = f"GET {url}"
    try:
        page.goto(b.server.url + url, wait_until="load")
    except Exception as error:  # noqa: BLE001
        b.F.add("goto_failed", url, f"{width}px: {type(error).__name__}: {str(error)[:120]}")
        return
    b.check(page, url, width)


def crawl(b: Browser, stage: Stage, width: int) -> list[str]:
    """Обійти всі сторінки: вкладки, фільтри й усі внутрішні посилання, що знайдуться."""
    page = b.page(width)
    seen: set[str] = set()
    queue = [*TABS, *VARIANTS]
    while queue and len(seen) < 700:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        goto(b, page, url, width)
        stage.counts["pages"] += 1
        with contextlib.suppress(Exception):
            hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
            for href in hrefs:
                skipped = ("//", "/quit", "/photo", "/thumb", "/duplicate")
                if href and href.startswith("/") and not href.startswith(skipped):
                    queue.append(href)
    page.context.close()
    return sorted(seen)


def press_all(b: Browser, page, url: str, width: int, stage: Stage, limit: int = 40) -> None:
    """Натиснути кожну кнопку й посилання на сторінці (крім кнопок робота — для них свій етап)."""
    goto(b, page, url, width)
    total = len(page.evaluate(TAG, ROBOT_FORM))
    for number in range(min(total, limit)):
        if (
            urlsplit(page.url).path + (("?" + urlsplit(page.url).query) if "?" in url else "")
            != url
        ):
            goto(b, page, url, width)
        tagged = page.evaluate(TAG, ROBOT_FORM)
        target = next((x for x in tagged if x["n"] == number), None)
        if target is None:
            continue
        b.where = f"{url} → «{target['label']}» ({width}px)"
        try:
            try:
                page.locator(f'[data-mk="{number}"]').first.click(timeout=4000)
            except Exception:  # noqa: BLE001 — JS Картки міг перемалювати елемент: мітки ще раз
                page.evaluate(TAG, ROBOT_FORM)
                page.locator(f'[data-mk="{number}"]').first.click(timeout=4000)
            page.wait_for_load_state("load", timeout=8000)
            page.wait_for_timeout(120)
        except Exception as error:  # noqa: BLE001
            stage.counts["click_failed"] += 1
            b.F.add("click_failed", url, f"{width}px «{target['label']}»: {str(error)[:100]}")
            continue
        stage.counts["clicks"] += 1
        b.check(page, f"{url} → «{target['label']}»", width)


def cards(data: Path) -> list[tuple[int, str, str | None]]:
    with contextlib.closing(ro(data)) as conn:
        return [
            (r["id"], r["status"], r["problem_code"])
            for r in conn.execute("SELECT id, status, problem_code FROM items ORDER BY id")
        ]


def pick_cards(rows: list, per_kind: int | None) -> list[int]:
    """Усі Картки; з `per_kind` — по кілька на кожну пару «статус + код» (вузькі екрани)."""
    if per_kind is None:
        return [r[0] for r in rows]
    taken: Counter = Counter()
    ids = []
    for item_id, status, code in rows:
        if taken[(status, code)] < per_kind:
            taken[(status, code)] += 1
            ids.append(item_id)
    return ids


def buttons_phase(b: Browser, data: Path, stage: Stage, pages: list[str], quick: bool) -> None:
    """Кожна кнопка кожної сторінки й кожної Картки: 1400 — усі, 390 — по Картці на кожен код."""
    rows = cards(data)
    for width in WIDTHS:
        page = b.page(width)
        for url in [*TABS, *VARIANTS]:
            press_all(b, page, url, width, stage)
        ids = pick_cards(rows, 1 if width < 600 or quick else None)
        for item_id in ids:
            press_all(b, page, f"/item/{item_id}", width, stage)
        sessions = [p for p in pages if p.startswith("/session/")]
        for url in sessions:
            press_all(b, page, url, width, stage)
        page.context.close()


def random_phase(
    b: Browser, rnd: random.Random, actions: int, stage: Stage, data: Path
) -> list[str]:
    """Випадкові дії: клік, введення сміття, перехід, назад/вперед, перезавантаження, зміна ширини."""
    log: list[str] = []
    width = rnd.choice(WIDTHS)
    page = b.page(width)
    ids = [r[0] for r in cards(data)]
    urls = [*TABS, *VARIANTS, *(f"/item/{i}" for i in ids), "/session/1", "/session/2"]
    goto(b, page, rnd.choice(urls), width)
    for number in range(actions):
        kind = rnd.choices(
            ["click", "type", "goto", "back", "reload", "resize", "dbl", "key"],
            [50, 14, 12, 6, 4, 3, 5, 6],
        )[0]
        b.where = f"#{number} {kind} на {page.url.replace(b.server.url, '')} ({width}px)"
        try:
            tagged = page.evaluate(TAG, ROBOT_FORM)
            if kind in ("click", "dbl") and tagged:
                target = rnd.choice(tagged)
                loc = page.locator(f'[data-mk="{target["n"]}"]').first
                if kind == "dbl":
                    loc.dblclick(timeout=3000)
                else:
                    loc.click(timeout=3000)
                log.append(f"{kind} «{target['label']}»")
            elif kind == "type" and page.evaluate(FIELDS):
                fields = page.locator(
                    "input:not([type=hidden]):not([type=file]):not([type=submit])"
                    ":not([type=checkbox]):not([type=radio]), textarea"
                ).filter(has_not=page.locator('form[action="/settings"] *'))
                fields.nth(rnd.randrange(fields.count())).fill(rnd.choice(JUNK), timeout=2000)
                log.append("type")
            elif kind == "goto":
                page.goto(b.server.url + rnd.choice(urls), wait_until="load")
                log.append(f"goto {page.url}")
            elif kind == "back":
                page.go_back(wait_until="load")
                log.append("back")
            elif kind == "reload":
                page.reload(wait_until="load")
                log.append("reload")
            elif kind == "resize":
                width = rnd.choice(WIDTHS)
                page.set_viewport_size({"width": width, "height": 844})
                log.append(f"resize {width}")
            elif kind == "key":
                page.keyboard.press(rnd.choice(["Escape", "Tab", "Enter", "Space", "Shift+Tab"]))
                log.append("key")
            page.wait_for_timeout(60)
        except Exception as error:  # noqa: BLE001 — елемент зник чи сторінка пішла: це нормально
            stage.counts["action_skipped"] += 1
            log.append(f"skip {kind}: {type(error).__name__}")
            if not b.server.alive():
                b.F.add("server_died", b.where, "сервер екрана помер")
                break
        stage.counts["actions"] += 1
        b.check(page, page.url.replace(b.server.url, "") or "/", width)
    page.context.close()
    return log


# --- Робот ---


def count(data: Path, sql: str, *args) -> int:
    with contextlib.closing(ro(data)) as conn:
        return conn.execute(sql, args).fetchone()[0]


def fake_events(base: Path) -> list[dict]:
    path = base / "fake.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def wait_idle(server: Server, data: Path, seconds: float = 90) -> bool:
    from shafa.robot import lock

    deadline = time.time() + seconds
    while time.time() < deadline:
        if not server.robots() and lock.busy(data / "robot.lock") is None:
            return True
        time.sleep(1)
    return False


def top_up(server: Server, data: Path, root: Path, seed: int, n: int) -> list[int]:
    """Свіжі Схвалені для робота (як у `monkey_seed.pool`, але на працюючій базі)."""
    s = monkey_seed.Seed(data, root, seed)
    try:
        s.post = s.conn.execute("SELECT MAX(post_id) FROM posts").fetchone()[0]
        ids = [s.add(monkey_seed.Status.APPROVED) for _ in range(n)]
        with s.conn:
            for item_id in ids:
                s.conn.execute(
                    "UPDATE items SET duplicate_ok_at = ? WHERE id = ?",
                    (monkey_seed.db.now(), item_id),
                )
    finally:
        s.close()
    return ids


def new_day(data: Path) -> None:
    """Доба закінчилась: усе, що почато «сьогодні», стає вчорашнім (ліміт 30 на добу знову вільний)."""
    with contextlib.closing(sqlite3.connect(data / "shafa.sqlite", timeout=30)) as conn, conn:
        conn.execute(
            "UPDATE items SET publish_started_at = datetime(publish_started_at, '-1 day') "
            "WHERE publish_started_at IS NOT NULL"
        )


def robot_cycles(
    server: Server,
    data: Path,
    root: Path,
    rnd: random.Random,
    cycles: int,
    F: Findings,
    stage: Stage,
    seed: int,
) -> None:
    """Старт/Пауза/Продовжити/Стоп у випадковому порядку — так, як натискала б Продавчиня."""
    actions = rnd.choices(["start", "pause", "resume", "stop"], [5, 1, 2, 1.5], k=cycles)
    made = sum(e["event"] == "create" for e in fake_events(server.base))
    for number, action in enumerate(actions):
        status, _, _ = server.request(
            "POST", "/robot", {"token": server.token, "action": action, "back": "/review"}
        )
        stage.counts[f"robot_{action}"] += 1
        if status >= 500 or status == 403 or status == 400:
            F.add(f"robot_http_{status}", f"POST /robot {action}", f"відповідь {status}")
        if action == "start" and rnd.random() < 0.3:  # подвійний клік «Старт» (HYP-3)
            server.request("POST", "/robot", {"token": server.token, "action": "start",
                                              "back": "/review"})  # fmt: skip
            stage.counts["robot_double_start"] += 1
            time.sleep(0.05)
            if len(server.robots()) > 1:
                F.add(
                    "two_robots", "POST /robot start ×2", f"живих роботів: {len(server.robots())}"
                )
        time.sleep(rnd.choice([0.1, 0.5, 2.0, 5.0, 9.0, 14.0]))  # публікація — кілька секунд
        if len(server.robots()) > 1:
            F.add("two_robots", f"цикл {number} ({action})", "одночасно кілька процесів робота")
        if number % 10 == 9:
            if count(data, "SELECT COUNT(*) FROM items WHERE status = 'approved'") < 12:
                top_up(server, data, root, seed + number, 40)
            if count(data, "SELECT COUNT(*) FROM sessions WHERE stop_code = 'limit' "
                           "AND started_at > datetime('now', '-3 minutes')"):  # fmt: skip
                new_day(data)
            if not server.alive():
                F.add("server_died", f"цикл {number}", "сервер екрана помер")
                return
    server.request("POST", "/robot", {"token": server.token, "action": "stop", "back": "/review"})
    if not wait_idle(server, data):
        F.add("lock_stuck", "кінець етапу робота", "після «Стоп» робот чи замок живі понад 90 с")
    stage.counts["cycle_created"] = (
        sum(e["event"] == "create" for e in fake_events(server.base)) - made
    )
    if cycles >= 20 and not stage.counts["cycle_created"]:
        F.add(
            "weak_cycles",
            "етап робота",
            "за всі цикли жодної публікації — етап нічого не перевірив",
        )


def run_robot(server: Server, data: Path, root: Path, sequence: str, limit: int, weights: str = ""):
    """Один запуск `robot --publish --fake-shafa` з відомою послідовністю сценаріїв (проба)."""
    env = {**server.env, "SHAFA_FAKE_SEQUENCE": sequence, "SHAFA_FAKE_SCENARIOS": weights}
    cmd = [sys.executable, "-m", "shafa", "robot", "--publish", "--db", str(data / "shafa.sqlite"),
           "--root", str(root), "--fake-shafa", "--limit", str(limit)]  # fmt: skip
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=300, cwd=server.base, check=False
    )


def last_session(data: Path) -> sqlite3.Row:
    with contextlib.closing(ro(data)) as conn:
        return conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()


def drills(server, data, root, F: Findings, stage: Stage, seed: int) -> list[dict]:
    """Кожен сценарій підставної Шафи окремо, з відомим очікуванням."""
    results = []

    def one(name, sequence, want_item, want_stop, want_next=None, limit=3):
        new_day(data)  # ліміт 30 на добу не заважає пробі
        ids = top_up(server, data, root, seed + len(results), 3)
        before = len(fake_events(server.base))
        with parked(data, ids):
            done = run_robot(server, data, root, sequence, limit)
        session = last_session(data)
        with contextlib.closing(ro(data)) as conn:
            state = [
                tuple(conn.execute("SELECT status, problem_code FROM items WHERE id = ?", (i,)).fetchone())
                for i in ids
            ]  # fmt: skip
        events = fake_events(server.base)[before:]
        creates = [e for e in events if e["event"] == "create"]
        got = {"scenario": name, "first": state[0], "second": state[1], "stop": session["stop_code"],
               "creates": len(creates), "exit": done.returncode}  # fmt: skip
        problems = []
        if want_item and state[0] != want_item:
            problems.append(f"перша Картка {state[0]}, очікували {want_item}")
        if want_next and state[1] != want_next:
            problems.append(f"друга Картка {state[1]}, очікували {want_next}")
        if want_stop and session["stop_code"] != want_stop:
            problems.append(f"зупинка {session['stop_code']}, очікували {want_stop}")
        if any(e["event"] == "paid" for e in events):
            problems.append("робот натиснув «Підтвердити» у вікні балансу!")
        if done.returncode not in (0, 130) and name != "uncertain":
            problems.append(
                f"код виходу {done.returncode}: {done.stdout[-200:]} {done.stderr[-200:]}"
            )
        if problems:
            F.add("drill", name, "; ".join(problems))
        got["ok"] = not problems
        results.append(got)
        stage.counts["drills"] += 1

    A, P = ("approved", None), ("published", None)
    one("ok", "ok", P, None, P)
    one("nofield", "ok,nofield,ok", ("problem", "robot_field_missing"), None, P)
    one("slow", "ok,slow,ok", ("problem", "robot_slow"), None, P)
    one("photos", "ok,photos,ok", ("problem", "robot_photos"), None, P)
    one("balance", "ok,balance,ok", P, None, P)
    one("balance_stuck", "ok,balance_stuck", A, "shafa_balance", A)
    one("login_start", "login", A, "login", A)
    one("login_card", "ok,login", ("problem", "robot_logged_out"), "login", A)
    one("uncertain", "ok,uncertain,ok", ("problem", "publish_uncertain"), "error", A)
    return results


@contextlib.contextmanager
def parked(data: Path, keep: list[int]):
    """Схвалені, крім `keep`, чекають: робот бере старі першими, а проба — про конкретні Картки."""
    with contextlib.closing(sqlite3.connect(data / "shafa.sqlite", timeout=30)) as conn, conn:
        others = [
            r[0] for r in conn.execute("SELECT id FROM items WHERE status = 'approved'")
            if r[0] not in keep
        ]  # fmt: skip
        conn.executemany("UPDATE items SET status = 'review' WHERE id = ?", [(i,) for i in others])
    try:
        yield
    finally:
        with contextlib.closing(sqlite3.connect(data / "shafa.sqlite", timeout=30)) as conn, conn:
            conn.executemany(
                "UPDATE items SET status = 'approved' WHERE id = ? AND status = 'review'",
                [(i,) for i in others],
            )


def similar_drill(server, data, root, F: Findings, stage: Stage, picked: list[int]) -> None:
    """«Схожий» товар: Картка з дублем у знімку «Мої речі» → Проблема, робот іде далі.
    Кнопки екрана могли чіпати ці Картки й знімок («Перечитати зараз») — повертаємо як у сіді."""
    if not picked:
        return
    with contextlib.closing(monkey_seed.db.connect(data / "shafa.sqlite")) as conn, conn:
        conn.executemany(
            "UPDATE items SET status = 'approved', problem_code = NULL, problem_text = NULL, "
            "duplicate_ok_at = NULL, shafa_id = NULL, shafa_url = NULL WHERE id = ?",
            [(i,) for i in picked],
        )
        monkey_seed.snapshot(conn, picked)
    with parked(data, picked):
        run_robot(server, data, root, "ok", len(picked) + 2)
    with contextlib.closing(ro(data)) as conn:
        for item_id in picked:
            row = conn.execute(
                "SELECT status, problem_code FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if tuple(row) != ("problem", "possible_duplicate"):
                F.add(
                    "drill",
                    "similar",
                    f"Картка {item_id}: {tuple(row)}, очікували possible_duplicate",
                )
    stage.counts["drills"] += 1


def invariants(data: Path, server: Server, F: Findings, stage: Stage) -> None:
    from shafa.db import Status

    allowed = {str(s) for s in Status}
    with contextlib.closing(ro(data)) as conn:

        def check(kind: str, sql: str, what: str) -> None:
            rows = conn.execute(sql).fetchall()
            if rows:
                F.add(f"invariant_{kind}", what, f"{len(rows)} шт., напр. Картка {rows[0][0]}")

        bad = [
            r[0] for r in conn.execute("SELECT DISTINCT status FROM items") if r[0] not in allowed
        ]
        if bad:
            F.add("invariant_status", "items.status", f"недопустимі статуси: {bad}")
        check(
            "published",
            "SELECT id FROM items WHERE status = 'published' AND shafa_id IS NULL",
            "published без shafa_id",
        )
        check(
            "shafa_id",
            "SELECT id FROM items WHERE shafa_id IS NOT NULL AND status NOT IN "
            "('published', 'archived', 'problem', 'review', 'approved')",
            "shafa_id у дивному статусі",
        )
        check(
            "double",
            "SELECT shafa_id FROM items WHERE shafa_id IS NOT NULL GROUP BY shafa_id "
            "HAVING COUNT(*) > 1",
            "один shafa_id у двох Карток",
        )
        check(
            "post_twice",
            "SELECT post_id FROM items WHERE status = 'published' AND post_id IS NOT NULL "
            "GROUP BY post_id HAVING COUNT(*) > 1",
            "пост опубліковано двічі",
        )
        check(
            "restored",
            "SELECT id FROM items WHERE status IN ('approved') AND problem_code IN "
            "('many_items', 'no_section')",
            "many_items/no_section стали Схваленими (CORE-017)",
        )
        check(
            "counters",
            "SELECT s.id FROM sessions s WHERE s.ended_at IS NOT NULL AND s.published != "
            "(SELECT COUNT(*) FROM items i WHERE i.session_id = s.id AND i.published_at IS NOT NULL "
            "AND i.shafa_id IS NOT NULL)",
            "sessions.published ≠ числу опублікованих Карток",
        )
        check("open_session", "SELECT id FROM sessions WHERE ended_at IS NULL", "сесія без кінця")
        check(
            "publishing",
            "SELECT id FROM items WHERE status = 'publishing'",
            "Картка лишилась у «publishing»",
        )
        # Лише робот створює опубліковані зі свіжими shafa_id (900 млн+ з підставної Шафи).
        fake = {e["item"] for e in fake_events(server.base) if e["event"] == "create"}
        made = {r[0] for r in conn.execute("SELECT id FROM items WHERE shafa_id >= 900000000")}
        if made - fake:
            F.add(
                "invariant_no_robot",
                "підставні shafa_id",
                f"Картки без створення на Шафі: {sorted(made - fake)[:5]}",
            )
        creates = Counter(e["item"] for e in fake_events(server.base) if e["event"] == "create")
        twice = [i for i, n in creates.items() if n > 1]
        if twice:
            F.add("double_publish", "підставна Шафа", f"Картки створені двічі: {twice}")
        if any(e["event"] == "paid" for e in fake_events(server.base)):
            F.add("paid", "підставна Шафа", "робот натиснув «Підтвердити» у вікні балансу")
        stage.counts["created"] = sum(creates.values())
        stage.counts["created_cards"] = len(creates)
        stage.notes.append(f"сесій у базі: {conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]}; "
                           f"причини: {dict(conn.execute('SELECT COALESCE(stop_code, \"—\"), COUNT(*) FROM sessions GROUP BY 1').fetchall())}")  # fmt: skip


def double_click(b: Browser, server: Server, data: Path, F: Findings) -> None:
    """HYP-3: подвійний клік «Схвалити і далі» не повинен схвалити дві Картки."""
    with contextlib.closing(ro(data)) as conn:
        ids = [
            r[0] for r in conn.execute("SELECT id FROM items WHERE status = 'review' ORDER BY id")
        ]
    if len(ids) < 3:
        return
    page = b.page(1400)
    before = count(data, "SELECT COUNT(*) FROM items WHERE status = 'approved'")
    b.where = "dblclick «Схвалити і далі»"
    page.goto(f"{server.url}/item/{ids[0]}?from=/review&next=/item/{ids[1]}")
    button = page.locator("button", has_text=re.compile("Схвалити"))
    if button.count():
        with contextlib.suppress(Exception):
            button.first.dblclick(timeout=3000)
        page.wait_for_timeout(800)
        after = count(data, "SELECT COUNT(*) FROM items WHERE status = 'approved'")
        if after - before > 1:
            F.add(
                "double_action",
                "Схвалити і далі ×2",
                f"схвалено {after - before} Карток замість однієї",
            )
    page.context.close()


def write_report(
    out: Path, args, F: Findings, stage: Stage, info: dict, drills_: list, log
) -> None:
    rows = sorted(F.items.values(), key=lambda x: (x["kind"], x["where"]))
    (out / "report.json").write_text(
        json.dumps({"seed": args.seed, "actions": args.actions, "cycles": args.cycles,
                    "counts": dict(stage.counts), "notes": stage.notes, "seeded": info,
                    "drills": drills_, "findings": rows, "shots": F.shots},
                   ensure_ascii=False, indent=1), encoding="utf-8")  # fmt: skip
    lines = [
        f"# «Мавпа» QA-004 · сід {args.seed} · дій {args.actions} · циклів робота {args.cycles}",
        "",
        "## Лічильники",
        *(f"- {k}: {v}" for k, v in sorted(stage.counts.items())),
        "",
        *(f"- {n}" for n in stage.notes),
        "",
        "## Проби підставної Шафи",
        "| сценарій | перша Картка | друга | зупинка | створень | ok |",
        "|---|---|---|---|---|---|",
        *(
            f"| {d['scenario']} | {d['first']} | {d['second']} | {d['stop']} | {d['creates']} | "
            f"{'так' if d['ok'] else 'НІ'} |"
            for d in drills_
        ),
        "",
        f"## Знахідки ({len(rows)})",
    ]
    lines += [f"- **{r['kind']}** `{r['where']}` ×{r['count']} — {r['detail']}" for r in rows] or [
        "- немає"
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "actions.log").write_text("\n".join(log), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--actions", type=int, default=500, help="випадкових дій у браузері")
    parser.add_argument("--cycles", type=int, default=100, help="Старт/Пауза/Стоп робота")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--out", type=Path, help="де звіт (типово — тека в /tmp)")
    parser.add_argument("--quick", action="store_true", help="лише по Картці на код (швидше)")
    parser.add_argument("--keep", action="store_true", help="не видаляти тимчасову теку")
    parser.add_argument(
        "--skip", default="", help="етапи, які пропустити: crawl,buttons,random,robot"
    )
    parser.add_argument("--scenarios", default="ok:60,balance:6,nofield:8,slow:5,photos:4,"
                        "uncertain:2,login:2,balance_stuck:2")  # fmt: skip
    args = parser.parse_args()
    if args.port == 8765:
        raise SystemExit("Порт 8765 — пілот. Беріть інший (типово 8799).")
    skip = set(args.skip.split(","))

    base = Path(tempfile.mkdtemp(prefix="qa-004-")).resolve()
    out = args.out or base / "report"
    out.mkdir(parents=True, exist_ok=True)
    os.environ.update(SHAFA_TESTING="1", SHAFA_DATA_DIR=str(base / "data"),
                      SHAFA_LEGACY_ENV=str(base / "no-legacy.env"))  # fmt: skip
    print(f"Тека: {base}\nЗвіт: {out}")
    data, root, info = monkey_seed.prepare(base, args.seed)
    print(f"Дані: {info['items']}")
    F, stage, rnd = Findings(), Stage(), random.Random(args.seed)
    F.known = [f"/thumb/{info['corrupt']}/2", f"/photo/{info['corrupt']}/2"]
    server = Server(base, data, root, args.port, args.seed, args.scenarios)
    log: list[str] = []
    drills_: list[dict] = []
    code = 1
    try:
        server.start()
        print(f"Сервер: {server.url}")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = Browser(p, server, F, stage, out)
            try:
                pages: list[str] = []
                if "crawl" not in skip:
                    print("Етап 1: обхід сторінок")
                    for width in WIDTHS:
                        pages = crawl(b, stage, width)
                if "buttons" not in skip:
                    print("Етап 2: кожна кнопка")
                    buttons_phase(b, data, stage, pages, args.quick)
                if "random" not in skip:
                    print(f"Етап 3: {args.actions} випадкових дій (сід {args.seed})")
                    log = random_phase(b, rnd, args.actions, stage, data)
                    double_click(b, server, data, F)
                for trace in server.tracebacks():
                    F.add("server_traceback", "журнал сервера", trace)
                for finding in list(F.items.values()):
                    if finding["url"] and finding["kind"] != "http_403" and len(F.shots) < 24:
                        b.shot(urlsplit(finding["url"]).path + (("?" + urlsplit(finding["url"]).query)
                                                             if urlsplit(finding["url"]).query else ""),
                               f"{finding['kind']}-{len(F.shots)}")  # fmt: skip
                stage.notes.append(
                    f"запити назовні (заблоковані браузером): {sorted(set(b.outside))}"
                )
                if b.outside:
                    F.add("outside_request", "браузер", f"{sorted(set(b.outside))[:5]}")
            finally:
                b.close()
        if "robot" not in skip:
            print("Етап 4: проби сценаріїв підставної Шафи")
            drills_ = drills(server, data, root, F, stage, args.seed)
            similar_drill(server, data, root, F, stage, info["similar"])
            print(f"Етап 5: {args.cycles} циклів Старт/Пауза/Стоп")
            robot_cycles(server, data, root, rnd, args.cycles, F, stage, args.seed)
        for trace in server.tracebacks():
            F.add("server_traceback", "журнал сервера", trace)
        robot_log = data / "robot-run.log"
        if robot_log.exists() and "Traceback" in robot_log.read_text(
            encoding="utf-8", errors="replace"
        ):
            F.add("robot_traceback", "robot-run.log", "у журналі робота є Traceback")
        invariants(data, server, F, stage)
        stage.notes.append(
            f"очікувані 404 для порожнього файла фото (Картка {info['corrupt']}): {F.known_hits}"
        )
        code = 1 if F.items else 0
    finally:
        server.stop()
        write_report(out, args, F, stage, info, drills_, log)
        print(f"\nЗнахідок: {len(F.items)}. Звіт: {out / 'report.md'}")
        if not args.keep and not args.out:
            shutil.copytree(out, Path(tempfile.gettempdir()) / f"qa-004-report-{args.seed}",
                            dirs_exist_ok=True)  # fmt: skip
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
