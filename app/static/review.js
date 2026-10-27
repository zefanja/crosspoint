// Keyboard shortcuts for the review screen (desktop):
// Space = show answer / Good, 1–4 = rating, Z = undo.
"use strict";

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
  if (e.target.closest("input:not([type=checkbox]), textarea, select")) return;

  const toggle = document.getElementById("show-answer");
  const rate = (n) => {
    const btn = document.querySelector(`.ratings [data-rating="${n}"]`);
    if (btn && !btn.disabled) btn.click();
  };

  if (e.key === " " || e.key === "Enter") {
    if (!toggle) return;
    e.preventDefault();
    if (!toggle.checked) toggle.checked = true;
    else rate(3);
  } else if (["1", "2", "3", "4"].includes(e.key)) {
    if (toggle && toggle.checked) rate(e.key);
  } else if (e.key === "z" || e.key === "Z") {
    const undo = document.getElementById("undo-btn");
    if (undo) undo.click();
  }
});
