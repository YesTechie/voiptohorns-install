/* YesTechie Labs Style 0.4.0 — chapter 14 · theme controller (THM-01..04)
   No dependencies.

   <html data-theme-default="system">  default when nothing is stored (decided T1: system)
   localStorage "yt-theme"             "light" | "dark" | "system"
   <html data-theme-pref="…">          the user's choice
   <html data-theme="light|dark">      what CSS reads (system resolved)

   Put the THM-04 boot snippet inline in <head> before any stylesheet so the
   first paint is already in the right theme. This file only wires controls:

     <div class="theme-switch" role="radiogroup" data-theme-switch>
       <button role="radio" data-theme-set="light">…</button> …
     <button class="theme-cycle" data-theme-cycle></button>
     <input type="radio" name="theme" value="dark" data-theme-radio>
     <span class="theme-resolved" data-theme-resolved></span>

   Fires `yt-themechange` on document with { pref, theme }. */
(function () {
  "use strict";

  const KEY = "yt-theme";
  const PREFS = ["light", "dark", "system"];
  const root = document.documentElement;
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const label = { light: "Light", dark: "Dark", system: "System" };
  const short = { light: "Light", dark: "Dark", system: "Sys" };

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function pref() {
    const p = stored() || root.dataset.themeDefault || "system";
    return PREFS.includes(p) ? p : "system";
  }
  function resolve(p) {
    return p === "system" ? (mq && mq.matches ? "dark" : "light") : p;
  }

  function syncControls(p, t) {
    document.querySelectorAll("[data-theme-switch] [data-theme-set]").forEach((b) => {
      const on = b.dataset.themeSet === p;
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll("[data-theme-radio]").forEach((r) => { r.checked = r.value === p; });
    document.querySelectorAll("[data-theme-cycle]").forEach((b) => {
      b.innerHTML = `<span class="theme-glyph ${p}"></span>${short[p]}${p === "system" ? ` · ${t === "dark" ? "D" : "L"}` : ""}`;
      b.setAttribute("aria-label", `Theme: ${label[p]}${p === "system" ? ` (${t})` : ""}. Click to change.`);
      b.title = "Theme: Light → Dark → System";
    });
    document.querySelectorAll("[data-theme-resolved]").forEach((el) => {
      el.innerHTML = p === "system" ? `System → <b>${label[t]}</b>` : `<b>${label[t]}</b>`;
    });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      const c = getComputedStyle(root).getPropertyValue("--theme-meta").trim();
      if (c) meta.setAttribute("content", c);
    }
  }

  function apply(p) {
    const t = resolve(p);
    root.classList.add("theme-switching");
    root.dataset.themePref = p;
    root.dataset.theme = t;
    syncControls(p, t);
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theme-switching")));
    document.dispatchEvent(new CustomEvent("yt-themechange", { detail: { pref: p, theme: t } }));
  }

  function set(p) {
    if (!PREFS.includes(p)) return;
    try { localStorage.setItem(KEY, p); } catch (e) { /* private mode: session only */ }
    apply(p);
  }

  window.ytTheme = { get: pref, set, resolved: () => root.dataset.theme || resolve(pref()) };

  if (mq) {
    const onChange = () => { if (pref() === "system") apply("system"); };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
  window.addEventListener("storage", (e) => { if (e.key === KEY) apply(pref()); });

  function bind() {
    document.querySelectorAll("[data-theme-switch]").forEach((group) => {
      if (!group.getAttribute("role")) group.setAttribute("role", "radiogroup");
      if (!group.getAttribute("aria-label")) group.setAttribute("aria-label", "Theme");
      const btns = Array.from(group.querySelectorAll("[data-theme-set]"));
      btns.forEach((b, i) => {
        b.setAttribute("role", "radio");
        b.type = "button";
        if (!b.innerHTML.trim()) b.innerHTML = `<span class="theme-glyph ${b.dataset.themeSet}"></span>${label[b.dataset.themeSet]}`;
        b.addEventListener("click", () => set(b.dataset.themeSet));
        b.addEventListener("keydown", (e) => {
          const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
          if (!d) return;
          e.preventDefault();
          const n = btns[(i + d + btns.length) % btns.length];
          set(n.dataset.themeSet);
          n.focus();
        });
      });
    });
    document.querySelectorAll("[data-theme-cycle]").forEach((b) => {
      b.type = "button";
      b.addEventListener("click", () => set(PREFS[(PREFS.indexOf(pref()) + 1) % PREFS.length]));
    });
    document.querySelectorAll("[data-theme-radio]").forEach((r) => r.addEventListener("change", () => { if (r.checked) set(r.value); }));
    apply(pref());
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
