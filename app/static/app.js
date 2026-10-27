// Small progressive enhancements. Everything works without JS too.
"use strict";

document.addEventListener("submit", (e) => {
  const msg = e.target.dataset && e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("input", (e) => {
  if (e.target.id === "desired_retention") {
    const out = document.getElementById("retention_out");
    const lang = document.documentElement.lang || undefined;
    if (out) out.textContent = Number(e.target.value).toLocaleString(lang, { minimumFractionDigits: 2 });
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
