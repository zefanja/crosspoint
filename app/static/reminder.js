// Daily reminder on the "Me" page: subscribe this device to Web Push.
"use strict";

(() => {
  const box = document.getElementById("reminder");
  if (!box) return;
  const timeInput = box.querySelector("#reminder_time");
  const msg = box.querySelector("[data-msg]");
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

  function show(state) {
    box.hidden = false;
    box.querySelectorAll("[data-show]").forEach((el) => {
      el.hidden = !el.dataset.show.split(" ").includes(state);
    });
  }

  function say(key) { msg.textContent = key ? msg.dataset[key] : ""; }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": box.dataset.csrf },
      body: JSON.stringify(body),
    }).then((res) => { if (!res.ok) throw new Error(res.status); return res; });
  }

  function keyBytes(b64) {
    const raw = atob(b64.replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  const registration = () => navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready);

  function save(sub) {
    return post("/reminder", { ...sub.toJSON(), time: timeInput.value });
  }

  async function turnOn() {
    say();
    if (await Notification.requestPermission() !== "granted") return show(Notification.permission === "denied" ? "denied" : "off");
    const reg = await registration();
    const sub = (await reg.pushManager.getSubscription())
      || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(box.dataset.key) });
    await save(sub);
    show("on");
    say("saved");
  }

  async function turnOff() {
    say();
    const sub = await (await registration()).pushManager.getSubscription();
    if (sub) {
      await post("/reminder/off", { endpoint: sub.endpoint });
      await sub.unsubscribe();
    }
    show("off");
  }

  async function changeTime() {
    if (!timeInput.value) return;
    const sub = await (await registration()).pushManager.getSubscription();
    if (sub) { await save(sub); say("saved"); }
  }

  async function test() {
    say();
    await post("/reminder/test", {});
    say("sent");
  }

  const actions = { on: turnOn, off: turnOff, test };
  box.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    btn.disabled = true;
    actions[btn.dataset.action]().catch(() => say("failed")).finally(() => { btn.disabled = false; });
  });
  timeInput.addEventListener("change", () => { changeTime().catch(() => say("failed")); });

  if (!supported) return show("unsupported");
  if (Notification.permission === "denied") return show("denied");
  registration()
    .then((reg) => reg.pushManager.getSubscription())
    .then((sub) => {
      if (!sub || Notification.permission !== "granted") return show("off");
      show("on");
      return save(sub);  // keep the server in sync (e.g. after a key change by the browser)
    })
    .catch(() => show("off"));
})();
