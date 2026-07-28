/* JobPilot dashboard — progressive enhancement only. Everything works without JS;
   this adds the theme toggle, clickable rows, the bell dropdown + polling, toasts,
   and fetch-submits for the small write actions. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  // ── Toast ───────────────────────────────────────────────────────────
  let toastTimer;
  function toast(msg, kind) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.className = "toast is-shown" + (kind ? " toast--" + kind : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = "toast"), 3200);
  }

  // ── Theme toggle ────────────────────────────────────────────────────
  const themeBtn = $(".js-theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme
        || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("jobpilot-theme", next); } catch (e) {}
    });
  }

  // ── Copy buttons ────────────────────────────────────────────────────
  $$(".js-copy").forEach((btn) => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.copy || "";
      navigator.clipboard?.writeText(text).then(
        () => toast("Copied"),
        () => toast("Copy failed", "bad")
      );
    });
  });

  // ── Clickable table rows ────────────────────────────────────────────
  $$(".row-link[data-href]").forEach((row) => {
    const go = () => (window.location.href = row.dataset.href);
    row.addEventListener("click", (e) => {
      if (e.target.closest("a, button, input, select, form")) return;
      go();
    });
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter") go();
    });
  });

  // ── Bell dropdown ───────────────────────────────────────────────────
  const bell = $("[data-bell]");
  if (bell) {
    const btn = $(".js-bell-toggle", bell);
    const menu = $("[data-bell-menu]", bell);
    const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => {
      if (!bell.contains(e.target)) close();
    });
    const clearBtn = $(".js-bell-clear", bell);
    if (clearBtn) {
      clearBtn.addEventListener("click", async () => {
        try {
          await fetch("/notifications/read", { method: "POST", headers: { "x-requested-with": "fetch" } });
          $("[data-bell-count]").setAttribute("hidden", "");
          $$(".bell__item.is-unread", bell).forEach((i) => i.classList.remove("is-unread"));
          toast("Notifications cleared");
        } catch (e) { toast("Could not clear", "bad"); }
      });
    }

    // Poll the bell every 30s so new replies surface without a refresh.
    async function pollBell() {
      try {
        const r = await fetch("/api/bell", { headers: { "x-requested-with": "fetch" } });
        if (!r.ok) return;
        const data = await r.json();
        const badge = $("[data-bell-count]");
        if (data.unread > 0) { badge.textContent = data.unread; badge.removeAttribute("hidden"); }
        else badge.setAttribute("hidden", "");
      } catch (e) {}
    }
    setInterval(pollBell, 30000);
  }

  // ── Fetch-submit small action forms ─────────────────────────────────
  $$("[data-action-form]").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const r = await fetch(form.action, {
          method: "POST",
          headers: { "x-requested-with": "fetch", "content-type": "application/json" },
          body: JSON.stringify(Object.fromEntries(new FormData(form))),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { toast(data.error || "Failed", "bad"); return; }
        toast(data.message || "Done");
        setTimeout(() => window.location.reload(), 500);
      } catch (err) { toast("Request failed", "bad"); }
    });
  });
})();
