// Екран перевірки: перегляд фото і попередження про незбережені правки. Без бібліотек.
"use strict";
// Прапорець для стилів: чипи показуються лише коли JS працює (без нього — звичайні поля).
document.documentElement.classList.add("js");

// Кнопка «Темна / Світла тема» (DES-001): вибір зберігається в localStorage, theme.js ставить його при завантаженні.
document.addEventListener("click", (event) => {
  if (!event.target.closest("[data-theme-toggle]")) return;
  const root = document.documentElement;
  const dark = root.getAttribute("data-theme") === "dark";
  if (dark) root.removeAttribute("data-theme"); else root.setAttribute("data-theme", "dark");
  try { localStorage.setItem("shafa-theme", dark ? "light" : "dark"); } catch (e) {}
});

// A23: повідомлення вже показане сервером; `?msg=` не лишаємо в адресі, інакше F5 покаже його знову.
try {
  const url = new URL(window.location.href);
  if (url.searchParams.has("msg")) {
    url.searchParams.delete("msg");
    url.searchParams.delete("n");  // UX-003: числа до повідомлення («Лишилось N», «Схвалено …»)
    url.searchParams.delete("ok");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
} catch (error) { /* старий браузер — лишаємо як є */ }

document.addEventListener("DOMContentLoaded", () => {

  const viewer = document.getElementById("viewer");
  const photoLinks = Array.from(document.querySelectorAll("a.photo"));
  const big = document.querySelector("a.big");
  let shown = 0; // яке фото зараз велике

  function select(index) {
    if (!photoLinks.length) return;
    shown = (index + photoLinks.length) % photoLinks.length;
    photoLinks.forEach((link, i) => {
      if (i === shown) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
    if (big) {
      big.href = photoLinks[shown].href;
      big.querySelector("img").src = photoLinks[shown].href;
    }
  }

  function openViewer() {
    if (!viewer || !photoLinks.length) return;
    viewer.querySelector("img").src = photoLinks[shown].href;
    if (!viewer.open) viewer.showModal();
  }

  // Мініатюра — на велике фото; клік по великому — на весь екран. Без діалогу відкриється саме фото.
  photoLinks.forEach((link, index) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      select(index);
    });
  });
  if (big) {
    big.addEventListener("click", (event) => {
      if (!viewer) return;
      event.preventDefault();
      openViewer();
    });
  }

  if (viewer) viewer.addEventListener("click", () => viewer.close());
  document.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    const step = event.key === "ArrowRight" ? 1 : -1;
    if (viewer && viewer.open) {
      select(shown + step);
      openViewer();
      return;
    }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "");
    if (!typing && !event.altKey && !event.ctrlKey && !event.metaKey) select(shown + step);
  });

  // Чипи: кнопки пишуть вибране в те саме поле, яке й так бачить сервер (без JS видно звичайне поле).
  // toggle — усі варіанти кнопками; pick — вибрані чипами і «+ …» зі списком із пошуком;
  // single — одне з двох. Колір — два поля вибору (до двох кольорів), решта — одне поле «через кому».
  function initChips(box) {
    const name = box.dataset.target;
    const fields = Array.from(document.querySelectorAll(`#fields [name="${CSS.escape(name)}"]`));
    if (!fields.length) return;
    const kind = box.dataset.chips;
    const max = Number(box.dataset.max) || 0;
    const readonly = box.hasAttribute("data-readonly");
    const options = Array.from(box.querySelectorAll(".opt"));
    const chosen = box.querySelector(".chosen");
    const add = box.querySelector(".add");
    const picker = box.querySelector(".picker");
    const search = box.querySelector(".picker-search");

    function read() {
      if (fields[0].tagName === "INPUT") {
        return fields[0].value.split(",").map((v) => v.trim()).filter(Boolean);
      }
      return Array.from(new Set(fields.map((f) => f.value).filter(Boolean)));
    }

    function write(values) {
      if (fields[0].tagName === "INPUT") fields[0].value = values.join(", ");
      else fields.forEach((f, i) => { f.value = values[i] || ""; });
      fields[0].dispatchEvent(new Event("input", { bubbles: true }));
    }

    function render() {
      const selected = read();
      box.querySelectorAll(".extra").forEach((el) => el.remove());
      if (kind === "toggle") { // значення, якого немає в довіднику, теж можна зняти
        selected.filter((v) => !options.some((o) => o.dataset.value === v)).forEach((value) => {
          const extra = document.createElement("button");
          extra.type = "button";
          extra.className = "chip opt extra";
          extra.dataset.value = value;
          extra.textContent = value;
          box.append(extra);
        });
      }
      box.querySelectorAll(".opt").forEach((opt) => {
        const on = selected.includes(opt.dataset.value);
        opt.setAttribute("aria-pressed", String(on));
        const full = max && selected.length >= max && !on;
        if (full) opt.setAttribute("aria-disabled", "true"); else opt.removeAttribute("aria-disabled");
        if (readonly) opt.disabled = true;
      });
      if (chosen) {
        chosen.replaceChildren();
        selected.forEach((value) => {
          const chip = document.createElement("span");
          chip.className = "chip-on";
          const original = options.find((o) => o.dataset.value === value);
          const swatch = original && original.querySelector(".swatch");
          if (swatch) chip.append(swatch.cloneNode(true));
          chip.append(document.createTextNode(value));
          if (!readonly) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.dataset.remove = value;
            remove.setAttribute("aria-label", "Прибрати " + value);
            remove.textContent = "×";
            chip.append(remove);
          }
          chosen.append(chip);
        });
      }
      if (add) {
        add.hidden = readonly;
        const full = max && selected.length >= max;
        if (full) add.setAttribute("aria-disabled", "true"); else add.removeAttribute("aria-disabled");
      }
    }

    function toggle(value) {
      const selected = read();
      const at = selected.indexOf(value);
      if (kind === "single") {
        write([value]);
      } else if (at >= 0) {
        selected.splice(at, 1);
        write(selected);
      } else {
        if (max && selected.length >= max) return;
        selected.push(value);
        write(selected);
      }
      render();
    }

    box.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button || readonly) return;
      if (button.dataset.remove) toggle(button.dataset.remove);
      else if (button.classList.contains("opt")) {
        if (button.getAttribute("aria-disabled") === "true") return;
        toggle(button.dataset.value);
      } else if (button.classList.contains("add") && picker) {
        if (button.getAttribute("aria-disabled") === "true") return;
        picker.hidden = !picker.hidden;
        button.setAttribute("aria-expanded", String(!picker.hidden));
        if (!picker.hidden && search) search.focus();
      }
    });

    if (search) {
      search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase();
        options.forEach((opt) => { opt.hidden = q !== "" && !opt.dataset.value.toLowerCase().includes(q); });
      });
      search.addEventListener("keydown", (event) => {
        if (event.key === "Enter") { // не відправляти форму; єдиний знайдений варіант — вибрати
          event.preventDefault();
          const found = options.filter((o) => !o.hidden);
          if (found.length === 1 && found[0].getAttribute("aria-disabled") !== "true") {
            toggle(found[0].dataset.value);
          }
        } else if (event.key === "Escape") {
          picker.hidden = true;
          add.setAttribute("aria-expanded", "false");
          add.focus();
        }
      });
    }
    render();
  }
  document.querySelectorAll(".chipset").forEach(initChips);

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
      const bar = robot.querySelector(".sp, .status");
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

    // A25: лічильники вкладок оновлюються разом із панеллю (її відповідь несе `data-counts`).
    function updateCounts(html) {
      const found = html.match(/data-counts="([^"]*)"/);
      if (!found) return;
      found[1].split(";").forEach((pair) => {
        const [key, value] = pair.split("=");
        if (!key || value === undefined) return;
        document.querySelectorAll(`.tabs a[href="/${key}"] .count, .tabs .menu a[href="/${key}"] .count`).forEach((count) => {
          if (count.textContent !== value) count.textContent = value;
          if (key === "problem") count.classList.toggle("alert", value !== "0");
        });
      });
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
        updateCounts(html);
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
});
