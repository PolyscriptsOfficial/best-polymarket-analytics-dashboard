(() => {
  const dialog = document.getElementById("help-dialog");
  const backdrop = document.getElementById("help-backdrop");
  const openBtn = document.getElementById("help-open");
  const closeBtn = document.getElementById("help-close");
  if (!dialog || !openBtn) return;

  const origin = window.location.origin;
  const baseEl = document.getElementById("help-base-url");
  if (baseEl) baseEl.textContent = origin;
  const fetchCmd = document.getElementById("help-fetch-cmd");
  if (fetchCmd) {
    fetchCmd.textContent =
      `python scripts/fetch_dataset.py --url ${origin} ` +
      "--start 2026-10-01T00:00:00Z --end 2026-10-27T00:00:00Z --asset BTC --interval 5m";
  }

  const show = () => {
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    if (backdrop) backdrop.hidden = false;
    document.body.classList.add("help-open");
  };

  const hide = () => {
    if (typeof dialog.close === "function") {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
    if (backdrop) backdrop.hidden = true;
    document.body.classList.remove("help-open");
  };

  openBtn.addEventListener("click", show);
  closeBtn?.addEventListener("click", hide);
  backdrop?.addEventListener("click", hide);
  dialog.addEventListener("cancel", (e) => {
    e.preventDefault();
    hide();
  });
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) hide();
  });
})();
