// Тема: світла за замовчуванням, темна — з вибору в localStorage. Підключається в <head> без defer, щоб не було білого спалаху.
try { if (localStorage.getItem("shafa-theme") === "dark") document.documentElement.setAttribute("data-theme", "dark"); } catch (e) {}
