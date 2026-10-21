// Екран перевірки: перегляд фото і попередження про незбережені правки. Без бібліотек.
"use strict";

const viewer = document.getElementById("viewer");
const photoLinks = Array.from(document.querySelectorAll("a.photo"));
let shown = -1;

function show(index) {
  shown = (index + photoLinks.length) % photoLinks.length;
  viewer.querySelector("img").src = photoLinks[shown].href;
  if (!viewer.open) viewer.showModal();
}

photoLinks.forEach((link, index) => {
  link.addEventListener("click", (event) => {
    if (!viewer) return; // без діалогу — просто відкриється фото
    event.preventDefault();
    show(index);
  });
});

if (viewer) {
  viewer.addEventListener("click", () => viewer.close());
  document.addEventListener("keydown", (event) => {
    if (!viewer.open) return;
    if (event.key === "ArrowRight") show(shown + 1);
    if (event.key === "ArrowLeft") show(shown - 1);
  });
}

// Змінили поле і йдете зі сторінки без «Зберегти» — браузер перепитає.
const form = document.querySelector("form.fields");
let dirty = false;
if (form) {
  form.addEventListener("input", () => { dirty = true; });
  form.addEventListener("submit", () => { dirty = false; });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) event.preventDefault();
  });
}

// Смужка стану оновлюється сама кожні 5 с і щойно вкладку знову видно (лише читання, лише свій
// сервер — CSP connect-src 'self'). Зв'язку немає — смужка жовта з приміткою, а не «як було».
const robot = document.getElementById("robot");
if (robot) {
  let last = null;
  let loading = false;
  const opened = new Map(); // «Детальніше»: що людина відкрила чи закрила — переживає оновлення

  robot.addEventListener("toggle", (event) => {
    const key = event.target.dataset && event.target.dataset.key;
    if (key) opened.set(key, event.target.open);
  }, true);

  function markStale(stale) {
    const bar = robot.querySelector(".status");
    if (!bar) return;
    bar.classList.toggle("stale", stale);
    let note = bar.querySelector(".stale-note");
    if (stale && !note) {
      note = document.createElement("p");
      note.className = "stale-note tile-sub";
      note.textContent = "Немає зв'язку з програмою — стан може бути застарілим. Якщо так триває, перезапустіть її.";
      note.style.flexBasis = "100%";
      note.style.margin = "0";
      bar.prepend(note);
    } else if (!stale && note) {
      note.remove();
    }
  }

  function swap(html) {
    const focused = document.activeElement;
    const wanted = focused && robot.contains(focused) && focused.tagName === "BUTTON"
      ? { name: focused.name, value: focused.value, text: focused.textContent } : null;
    robot.innerHTML = html;
    robot.querySelectorAll("details[data-key]").forEach((details) => {
      if (opened.has(details.dataset.key)) details.open = opened.get(details.dataset.key);
    });
    if (wanted) {
      const again = Array.from(robot.querySelectorAll("button")).find(
        (b) => b.name === wanted.name && b.value === wanted.value && b.textContent === wanted.text);
      if (again) again.focus();
    }
  }

  async function refresh() {
    if (loading || document.hidden) return;
    if (robot.querySelector("button:active")) return; // кнопку саме тиснуть
    loading = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(robot.dataset.panel, { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("panel " + response.status);
      const html = await response.text();
      if (html !== last) swap(html);
      last = html;
      markStale(false);
    } catch (error) {
      markStale(true); // сервер зупинили чи не відповідає
    } finally {
      clearTimeout(timer);
      loading = false;
    }
  }

  setInterval(refresh, 5000);
  document.addEventListener("visibilitychange", refresh);
  window.addEventListener("focus", refresh);
}

// Меню «Ще»: закривається кліком поза ним і клавішею Esc.
const more = document.querySelector("details.more");
if (more) {
  document.addEventListener("click", (event) => {
    if (!more.contains(event.target)) more.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") more.open = false;
  });
}
