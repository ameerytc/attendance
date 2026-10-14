// Mobile sidebar drawer toggle
document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("drawerToggle");
  const sidebar = document.getElementById("sidebar");

  if (toggle && sidebar) {
    toggle.addEventListener("click", () => {
      sidebar.classList.toggle("open");
    });

    document.addEventListener("click", (e) => {
      if (
        sidebar.classList.contains("open") &&
        !sidebar.contains(e.target) &&
        !toggle.contains(e.target)
      ) {
        sidebar.classList.remove("open");
      }
    });
  }

  // Auto-dismiss toasts after 4s
  document.querySelectorAll(".toast").forEach((toast, i) => {
    setTimeout(() => {
      toast.style.transition = "opacity 200ms ease";
      toast.style.opacity = "0";
      setTimeout(() => toast.remove(), 200);
    }, 4000 + i * 300);
  });
});

// ---- v2: confirm dialogs, loading indicators, auto-submit pickers, logo preview ----
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });
  document.querySelectorAll("form:not([method=get]):not([data-no-busy])").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (e.defaultPrevented) return;
      const btn = form.querySelector("button[type=submit]");
      const bar = document.getElementById("busy-bar");
      if (bar) bar.hidden = false;
      if (btn) setTimeout(() => btn.classList.add("is-loading"), 0);
    });
  });
  document.querySelectorAll("form[data-autosubmit] select").forEach((sel) => {
    sel.addEventListener("change", () => {
      // switching session resets the term (terms belong to a session)
      if (sel.name === "session_id") {
        const term = sel.form.querySelector("select[name=term_id]");
        if (term) term.value = "";
      }
      const bar = document.getElementById("busy-bar");
      if (bar) bar.hidden = false;
      sel.form.submit();
    });
  });
  const logoInput = document.getElementById("logo-input");
  const preview = document.getElementById("logo-preview");
  if (logoInput && preview) {
    logoInput.addEventListener("change", () => {
      const file = logoInput.files[0];
      if (!file) return;
      const ok = ["image/png", "image/jpeg", "image/webp"].includes(file.type);
      const msg = document.getElementById("logo-msg");
      if (!ok || file.size > 2 * 1024 * 1024) {
        if (msg) msg.textContent = "Choose a PNG, JPG or WebP image under 2 MB.";
        logoInput.value = "";
        return;
      }
      if (msg) msg.textContent = "Preview only — click “Save logo” to apply it.";
      preview.src = URL.createObjectURL(file);
      preview.hidden = false;
      const saveBtn = document.getElementById("logo-save");
      if (saveBtn) saveBtn.disabled = false;
    });
  }
  document.querySelectorAll("[data-edit-toggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const fs = document.getElementById(btn.dataset.editToggle);
      fs.disabled = !fs.disabled;
      btn.textContent = fs.disabled ? "Edit" : "Cancel";
      const save = document.getElementById(btn.dataset.editToggle + "-save");
      if (save) save.hidden = fs.disabled;
    });
  });
});

// ---- iOS shell: large-title collapse on scroll, haptic-style tap feedback ----
document.addEventListener("DOMContentLoaded", () => {
  const nav = document.getElementById("iosNav");
  if (nav) {
    const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 24);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }
  document.querySelectorAll(".ios-tab, .ios-row, .tile").forEach((el) => {
    el.addEventListener("pointerdown", () => { if (navigator.vibrate) navigator.vibrate(4); }, { passive: true });
  });
});

// ---- generic upload preview + client-side table search + repeatable rows ----
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("input[type=file][data-preview]").forEach((input) => {
    const img = document.getElementById(input.dataset.preview);
    const msg = input.parentElement.querySelector("[data-preview-msg]");
    input.addEventListener("change", () => {
      const file = input.files[0];
      if (!file) return;
      if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 2 * 1024 * 1024) {
        if (msg) msg.textContent = "Choose a PNG, JPG or WebP photo under 2 MB.";
        input.value = "";
        return;
      }
      if (msg) msg.textContent = "Preview — the photo is saved when you submit the form.";
      img.src = URL.createObjectURL(file);
      img.hidden = false;
      const placeholder = document.getElementById(input.dataset.preview + "-ph");
      if (placeholder) placeholder.hidden = true;
    });
  });
  document.querySelectorAll("input[data-filter-table]").forEach((box) => {
    const table = document.getElementById(box.dataset.filterTable);
    box.addEventListener("input", () => {
      const q = box.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach((tr) => { tr.hidden = q && !tr.textContent.toLowerCase().includes(q); });
    });
  });
  const addBtn = document.getElementById("add-teacher-row");
  if (addBtn) {
    const body = document.getElementById("teacher-rows");
    addBtn.addEventListener("click", () => {
      const clone = body.querySelector("tr").cloneNode(true);
      clone.querySelectorAll("input, select").forEach((el) => { el.value = ""; });
      body.appendChild(clone);
      clone.querySelector("input").focus();
    });
    body.addEventListener("click", (e) => {
      if (e.target.matches("[data-remove-row]") && body.children.length > 1) e.target.closest("tr").remove();
    });
  }
  document.querySelectorAll("[data-tabs] [data-tab]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const root = btn.closest("[data-tabs]");
      root.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b === btn));
      document.querySelectorAll("[data-tab-panel]").forEach((p) => { p.hidden = p.dataset.tabPanel !== btn.dataset.tab; });
    });
  });
});
