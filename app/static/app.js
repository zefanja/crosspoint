// Small progressive enhancements. Everything works without JS too.
"use strict";

document.addEventListener("submit", (e) => {
  const msg = e.target.dataset && e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-print]")) window.print();
});

document.addEventListener("input", (e) => {
  if (e.target.id === "desired_retention") {
    const out = document.getElementById("retention_out");
    const lang = document.documentElement.lang || undefined;
    if (out) out.textContent = Number(e.target.value).toLocaleString(lang, { minimumFractionDigits: 2 });
    const { min, max, value } = e.target;
    e.target.style.setProperty("--pct", ((value - min) / (max - min)) * 100 + "%");
  }
});

// Stepper buttons (− / +) next to a number input.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-step]");
  if (!btn) return;
  const input = btn.closest(".stepper").querySelector("input[type=number]");
  if (btn.dataset.step === "up") input.stepUp(); else input.stepDown();
  input.dispatchEvent(new Event("input", { bubbles: true }));
});

// "Copy prompt": copy the text of the element named in data-copy.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn || !navigator.clipboard) return;
  const source = document.getElementById(btn.dataset.copy);
  navigator.clipboard.writeText(source.textContent.trim()).then(() => {
    btn.textContent = btn.dataset.copied || btn.textContent;
  }).catch(() => {});
});

// File drop zones: show the chosen file name.
document.addEventListener("change", (e) => {
  const zone = e.target.closest(".filepick");
  if (!zone || !e.target.files) return;
  const label = zone.querySelector("[data-filename]");
  if (label && e.target.files[0]) label.textContent = e.target.files[0].name;
});

// Selects that filter a list (tag filter): apply on change.
document.addEventListener("change", (e) => {
  if (e.target.matches("select[data-autosubmit]")) e.target.form.requestSubmit();
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
