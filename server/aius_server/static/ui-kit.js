"use strict";
// ui-kit 2.0.0 — shared by AI Ops Platform, SSH MCP Server and Log Analysis
// Platform; keep byte-identical in the three repositories (edit one, run
// scripts/sync_ui_kit.py). Loaded synchronously in <head> so the theme applies
// before the first paint. Provides the theme switch, the SVG icon set, the
// narrow-screen navigation drawer, and the interaction primitives every console
// uses the same way: escape, notify, confirm and the unsaved-changes guard
// (docs/UI-GUIDELINES.md).
const UIKit = (() => {
  const VERSION = "2.0.0";
  const THEME_KEY = "ui-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const paths = {
    overview:
      '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
    alerts:
      '<path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15L6 16Z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    ai: '<path d="M12 3.5 13.8 9a2 2 0 0 0 1.2 1.2l5.5 1.8-5.5 1.8a2 2 0 0 0-1.2 1.2L12 20.5 10.2 15a2 2 0 0 0-1.2-1.2L3.5 12 9 10.2A2 2 0 0 0 10.2 9L12 3.5Z"/><path d="M19 3v3m-1.5-1.5h3"/>',
    connections:
      '<path d="M9 7H7a5 5 0 0 0 0 10h2m6-10h2a5 5 0 0 1 0 10h-2M8 12h8"/>',
    skills:
      '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5v-15Z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19M9 7.5h6M9 11h4"/>',
    hosts:
      '<rect x="3.5" y="4" width="17" height="6.5" rx="2"/><rect x="3.5" y="13.5" width="17" height="6.5" rx="2"/><path d="M7 7.25h.01M7 16.75h.01M11 7.25h6M11 16.75h6"/>',
    zabbix: '<path d="M12 3.5v11m-4-4 4 4 4-4M4.5 15.5v4h15v-4"/>',
    groups:
      '<rect x="8.5" y="3.5" width="7" height="5.5" rx="1.5"/><rect x="3" y="15.5" width="7" height="5" rx="1.5"/><rect x="14" y="15.5" width="7" height="5" rx="1.5"/><path d="M12 9v3.5M6.5 15.5v-3h11v3"/>',
    credentials:
      '<circle cx="8" cy="8" r="4.5"/><path d="m11.2 11.2 9 9m-3-3 2.6-2.6m-5.6-.4 2.6-2.6"/>',
    proxies: '<path d="M4 7.5h16m-4-4 4 4-4 4M20 16.5H4m4-4-4 4 4 4"/>',
    keys: '<path d="m12 3 7.5 3.5v5c0 4.7-3.3 8-7.5 9.5-4.2-1.5-7.5-4.8-7.5-9.5v-5L12 3Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    diagnostics: '<path d="M3 12h4l3-7.5 4 15 3-7.5h4"/>',
    history:
      '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 4.5V9H8M12 7.5v5l3 2"/>',
    audit:
      '<path d="M8.5 3.5h7v3h-7z"/><path d="M8.5 5H6a1.5 1.5 0 0 0-1.5 1.5v13A1.5 1.5 0 0 0 6 21h12a1.5 1.5 0 0 0 1.5-1.5v-13A1.5 1.5 0 0 0 18 5h-2.5M8.5 11h7M8.5 15h7"/>',
    settings:
      '<path d="M4 6.5h9m4 0h3M4 12h3m4 0h9M4 17.5h11m4 0h1"/><circle cx="15" cy="6.5" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17.5" r="2"/>',
    terminal:
      '<rect x="3" y="4.5" width="18" height="15" rx="3"/><path d="m7.5 9.5 3 2.5-3 2.5m5 0h4"/>',
    pulse: '<path d="M3 12h4l3-7.5 4 15 3-7.5h4"/>',
    refresh: '<path d="M20 11.5A8 8 0 1 0 17.7 17"/><path d="M20.5 5v6.5H14"/>',
    moon: '<path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2m0 15v2M4.6 4.6 6 6m12 12 1.4 1.4M2.5 12h2m15 0h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    close: '<path d="m6.5 6.5 11 11m-11 0 11-11"/>',
  };

  function icon(name) {
    return (
      '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (paths[name] || paths.pulse) +
      "</svg>"
    );
  }

  function hydrate(root = document) {
    for (const el of root.querySelectorAll("[data-icon]"))
      el.innerHTML = icon(el.dataset.icon);
  }

  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch {
      return null; // storage blocked: follow the system setting
    }
  }

  function applyTheme() {
    const choice = storedTheme();
    const theme =
      choice === "light" || choice === "dark"
        ? choice
        : system.matches
          ? "dark"
          : "light";
    document.documentElement.dataset.theme = theme;
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.innerHTML = icon(theme === "dark" ? "sun" : "moon");
      button.title = theme === "dark" ? "切换到浅色" : "切换到深色";
    }
  }

  function toggleTheme() {
    const next =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {}
    applyTheme();
  }

  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    document
      .getElementById("menu-toggle")
      ?.setAttribute("aria-expanded", String(open));
  }

  const ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };

  // Text for innerHTML and attribute values; every interpolated value goes
  // through it unless it is markup the console built itself.
  function escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
  }

  // One toast at a time. Errors stay twice as long and are announced at once;
  // inside an open modal dialog the toast joins it, or it would sit behind.
  let toastTimer = 0;
  function notify(message, { type = "info", timeout } = {}) {
    let box = document.getElementById("ui-toast");
    if (!box) {
      box = document.createElement("div");
      box.id = "ui-toast";
      box.addEventListener("click", () => (box.hidden = true));
    }
    const host = document.querySelector("dialog:modal") || document.body;
    if (box.parentElement !== host) host.append(box);
    const error = type === "error";
    box.className = "toast" + (error || type === "success" ? " " + type : "");
    box.setAttribute("role", error ? "alert" : "status");
    box.textContent = String(message ?? "");
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(
      () => (box.hidden = true),
      timeout ?? (error ? 12000 : 6000),
    );
  }

  // A modal question that names its consequence. Resolves true only on the
  // confirm button; Esc and the cancel button resolve false. A destructive
  // question starts on cancel, so a stray Enter cannot confirm it.
  let confirms = 0;
  function confirm({
    title,
    message = "",
    confirmText = "确认",
    cancelText = "取消",
    danger = false,
  } = {}) {
    return new Promise((resolve) => {
      const id = "ui-confirm-" + ++confirms;
      const dialog = document.createElement("dialog");
      dialog.className = "ui-confirm";
      dialog.setAttribute("aria-labelledby", id);
      dialog.innerHTML =
        `<form method="dialog"><h2 id="${id}">${escape(title)}</h2>` +
        (message ? `<p>${escape(message)}</p>` : "") +
        `<div class="modal-actions"><button class="btn" value="cancel">${escape(cancelText)}</button>` +
        `<button class="btn ${danger ? "danger" : "primary"}" value="confirm">${escape(confirmText)}</button></div></form>`;
      dialog.addEventListener("close", () => {
        dialog.remove();
        resolve(dialog.returnValue === "confirm");
      });
      document.body.append(dialog);
      dialog.showModal();
      dialog
        .querySelector(`button[value="${danger ? "cancel" : "confirm"}"]`)
        .focus();
    });
  }

  // Asks before the page is left while isDirty() holds; returns the remover.
  function guardUnsaved(isDirty) {
    const handler = (event) => {
      if (!isDirty()) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }

  applyTheme();
  system.addEventListener("change", applyTheme);
  document.addEventListener("DOMContentLoaded", () => {
    hydrate();
    applyTheme();
    document
      .getElementById("theme-toggle")
      ?.addEventListener("click", toggleTheme);
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest('[data-action="toggle-nav"]'))
      setNav(!document.body.classList.contains("nav-open"));
    else if (event.target.closest(".nav-link")) setNav(false);
  });
  window.addEventListener("hashchange", () => setNav(false));

  return {
    version: VERSION,
    icon,
    hydrate,
    escape,
    notify,
    confirm,
    guardUnsaved,
  };
})();
