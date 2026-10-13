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
