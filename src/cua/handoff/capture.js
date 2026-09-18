// Injected into every document of the session: shows who holds control and reports what a
// human does. Values are masked here, in the page, so raw input never leaves the browser.
(() => {
  if (window.__cuaCapture) return;
  window.__cuaCapture = true;

  const norm = (t) => (t || "").replace(/\s+/g, " ").trim().slice(0, 80);

  const describe = (el) => {
    if (!el || el.nodeType !== 1) return "unknown";
    const tag = el.tagName.toLowerCase();
    if (tag === "input" && ["submit", "button", "image"].includes(el.type)) return `${tag}[${norm(el.value)}]`;
    let label = "";
    const cell = el.closest && el.closest("td,th");
    if (cell) {
      for (let c = cell.previousElementSibling; c && !label; c = c.previousElementSibling) {
        if (!c.querySelector("input,select,textarea,button")) label = norm(c.innerText).replace(/:$/, "");
      }
    }
    const own = norm(el.getAttribute?.("aria-label") || (tag === "a" || tag === "button" ? el.innerText : ""));
    return `${tag}${el.type ? "[" + el.type + "]" : ""}${own ? " " + own : ""}${label ? " (" + label + ")" : ""}`;
  };

  const maskedValue = (el, value) => {
    if (!el) return null;
    if (el.type === "password") return "[secret]";
    return value == null ? null : norm(String(value));
  };

  const send = (kind, el, value) => {
    try {
      window.__cuaHumanEvent({
        kind,
        control: describe(el),
        value: maskedValue(el, value),
        path: location.pathname,
        frame: window.name || "top",
      });
    } catch (_) {
      /* binding not installed (replay without handoff) */
    }
  };

  document.addEventListener("click", (e) => send("click", e.target), true);
  document.addEventListener("change", (e) => send("fill", e.target, e.target.value), true);
  document.addEventListener("submit", (e) => send("submit", e.target), true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter") send("press_key", e.target, "Enter");
  }, true);

  const paint = () => {
    const state = window.__cuaControl || "automation";
    let bar = document.getElementById("cua-control-bar");
    if (!document.body) return;
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "cua-control-bar";
      bar.style.cssText =
        "position:fixed;top:0;left:0;right:0;z-index:2147483647;pointer-events:none;" +
        "font:bold 11px Verdana,sans-serif;padding:3px 6px;text-align:center;color:#fff";
      document.body.appendChild(bar);
    }
    const human = state === "human";
    bar.style.background = human ? "#137333" : state === "paused" ? "#8a6d00" : "#7a0000";
    bar.textContent = human
      ? "YOU HAVE CONTROL — make the manual changes, then press Hand back in the operator console"
      : state === "paused"
        ? "PAUSED — waiting for an operator"
        : "AUTOMATION IN CONTROL — please do not interact with this window";
  };

  window.__cuaSetControl = (state) => {
    window.__cuaControl = state;
    paint();
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", paint);
  else paint();
})();
