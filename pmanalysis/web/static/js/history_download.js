/** History → "Download historical data": builds a /v1 dataset zip and downloads it. */
(() => {
  const cfg = window.PM_DOWNLOAD;
  const form = document.getElementById("download-form");
  if (!cfg || !form) return;

  const status = document.getElementById("download-status");
  const button = document.getElementById("download-btn");
  const startInput = form.elements.start;
  const endInput = form.elements.end;
  const DAY_MS = 86_400_000;
  const earliest = cfg.earliest_start.slice(0, 10);
  const today = new Date().toISOString().slice(0, 10);
  const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);

  for (const input of [startInput, endInput]) input.max = today;
  startInput.value = isoDay(Math.max(Date.parse(`${earliest}T00:00:00Z`), Date.now() - 6 * DAY_MS));
  endInput.value = today;

  function say(text, kind = "") {
    status.textContent = text;
    status.className = `download-status ${kind}`;
  }

  function fmtBytes(n) {
    if (!n) return "";
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(n / 1e3))} kB`;
  }

  function checked(name) {
    return [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
  }

  function validate() {
    const assets = checked("asset");
    const intervals = checked("interval");
    const start = startInput.value;
    const end = endInput.value;
    if (!assets.length || !intervals.length) return { error: "Pick at least one asset and one interval." };
    if (!start || !end) return { error: "Pick a From and To date." };
    if (start < earliest) {
      return { error: `Downloads cover the last ${cfg.lookback_days} days (from ${earliest}). For older data, contact the owner.` };
    }
    if (end < start) return { error: "To must be on or after From." };
    // Inclusive To date: the request ends at the next UTC midnight.
    const startIso = `${start}T00:00:00Z`;
    const endIso = `${isoDay(Date.parse(`${end}T00:00:00Z`) + DAY_MS)}T00:00:00Z`;
    if ((Date.parse(endIso) - Date.parse(startIso)) / DAY_MS > cfg.max_range_days) {
      return { error: `One download can span up to ${cfg.max_range_days} days.` };
    }
    return { body: { start: startIso, end: endIso, assets, intervals } };
  }

  async function poll(id) {
    for (;;) {
      const res = await fetch(`/v1/datasets/${encodeURIComponent(id)}`);
      const rec = await res.json();
      if (!res.ok) throw new Error(rec.detail || `HTTP ${res.status}`);
      if (rec.status === "ready") return rec;
      if (rec.status === "failed") throw new Error(rec.error || "The build failed. Try again.");
      const done = rec.windows_done ?? 0;
      const total = rec.windows_total ?? 0;
      say(total ? `Building… ${done.toLocaleString()} / ${total.toLocaleString()} rounds` : "Building…");
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const { error, body } = validate();
    if (error) return say(error, "is-error");
    button.disabled = true;
    say("Preparing…");
    try {
      const res = await fetch("/v1/datasets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      let rec = await res.json();
      if (!res.ok) throw new Error(rec.detail || `HTTP ${res.status}`);
      if (rec.status !== "ready") rec = await poll(rec.dataset_id);
      const size = fmtBytes(rec.bytes);
      say(
        `Ready: ${(rec.window_count ?? 0).toLocaleString()} rounds, ${(rec.tick_rows ?? 0).toLocaleString()} rows` +
          (size ? ` (${size})` : "") + ". Download started.",
        "is-ok",
      );
      window.location.href = rec.download_path;
    } catch (err) {
      say(err.message, "is-error");
    } finally {
      button.disabled = false;
    }
  });
})();
