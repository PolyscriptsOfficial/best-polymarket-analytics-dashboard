const PAGE_SIZE = 200;
let replayCharts = null;
let currentTab = "windows";
let pageOffset = 0;
let pageTotal = 0;

function fmtTs(tsMs) {
  if (!tsMs) return "—";
  return new Date(tsMs).toLocaleString();
}

function fmtNum(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString();
}

function setFiltersForTab(tab) {
  const interval = document.getElementById("filter-interval");
  const side = document.getElementById("filter-side");
  const source = document.getElementById("filter-source");
  const replay = document.getElementById("replay-panel");

  interval.classList.toggle("hidden", tab !== "windows");
  side.classList.toggle("hidden", tab !== "clob");
  source.classList.toggle("hidden", tab !== "oracle");
  replay.classList.toggle("hidden", tab !== "windows");
}

function syncRawTabs(stats) {
  const hasRaw = Boolean(
    stats?.feed_record ||
      stats?.binance_trade_count ||
      stats?.oracle_tick_count ||
      stats?.clob_quote_count
  );
  document.querySelectorAll(".history-tab[data-raw]").forEach((btn) => {
    btn.classList.toggle("hidden", !hasRaw);
  });
  if (!hasRaw && currentTab !== "windows") {
    currentTab = "windows";
    document.querySelectorAll(".history-tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === "windows");
    });
    pageOffset = 0;
  }
  return hasRaw;
}

async function loadStats() {
  const el = document.getElementById("history-stats");
  try {
    const res = await fetch("/api/feeds/stats");
    const s = await res.json();
    syncRawTabs(s);
    el.innerHTML = [
      ["Windows", s.window_count],
      ["Ticks (1s)", s.tick_count],
      ["Binance trades", s.binance_trade_count],
      ["Oracle ticks", s.oracle_tick_count],
      ["CLOB quotes", s.clob_quote_count],
    ]
      .map(
        ([label, n]) =>
          `<div class="stat-chip"><span class="stat-label">${label}</span><span class="stat-value">${fmtNum(n)}</span></div>`
      )
      .join("");
  } catch (err) {
    el.textContent = `Failed to load counts: ${err.message}`;
  }
}

function winRateChip(row, highlight) {
  const rate = row.win_rate_up_pct;
  const rateText = rate == null ? "—" : `${rate.toFixed(1)}%`;
  const tone = rate == null ? "" : rate >= 50 ? "win-up" : "win-down";
  const active = highlight ? " is-active" : "";
  return `<div class="stat-chip win-chip ${tone}${active}">
    <span class="stat-label">${row.asset} · ${row.interval}</span>
    <span class="stat-value">${rateText}</span>
    <span class="stat-sub">${fmtNum(row.up)} UP / ${fmtNum(row.down)} DOWN · ${fmtNum(row.resolved)} resolved</span>
  </div>`;
}

async function loadWinRates() {
  const el = document.getElementById("win-rate-stats");
  if (!el) return;
  try {
    const res = await fetch("/api/stats");
    const data = await res.json();
    const asset = document.getElementById("filter-asset").value;
    const interval = document.getElementById("filter-interval").value;
    const items = data.items || [];
    const filtered = items.filter((row) => {
      if (asset && row.asset !== asset) return false;
      if (interval && row.interval !== interval) return false;
      return true;
    });
    if (!filtered.length) {
      el.textContent = "No resolved windows yet.";
      return;
    }
    const highlightOne = Boolean(asset && interval);
    el.innerHTML = filtered.map((row) => winRateChip(row, highlightOne)).join("");
  } catch (err) {
    el.textContent = `Failed to load win rates: ${err.message}`;
  }
}

function updatePager() {
  const label = document.getElementById("page-label");
  const prev = document.getElementById("page-prev");
  const next = document.getElementById("page-next");
  const from = pageTotal === 0 ? 0 : pageOffset + 1;
  const to = Math.min(pageOffset + PAGE_SIZE, pageTotal);
  label.textContent = `${fmtNum(from)}–${fmtNum(to)} of ${fmtNum(pageTotal)}`;
  prev.disabled = pageOffset <= 0;
  next.disabled = pageOffset + PAGE_SIZE >= pageTotal;
}

function setTable(headers, rowsHtml) {
  const thead = document.querySelector("#history-table thead");
  const tbody = document.querySelector("#history-table tbody");
  thead.innerHTML = `<tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr>`;
  tbody.innerHTML = rowsHtml || `<tr><td colspan="${headers.length}">No rows</td></tr>`;
}

async function loadWindows() {
  const asset = document.getElementById("filter-asset").value;
  const interval = document.getElementById("filter-interval").value;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(pageOffset),
  });
  if (asset) params.set("asset", asset);
  if (interval) params.set("interval", interval);

  const res = await fetch(`/api/windows?${params}`);
  const data = await res.json();
  const rows = data.items || [];
  pageTotal = data.total || 0;
  updatePager();

  setTable(
    ["Slug", "Asset", "Interval", "End", "Strike", "Outcome", "Ticks", "Export"],
    rows
      .map(
        (r) => `<tr>
      <td><a href="#" data-slug="${r.slug}">${r.slug}</a></td>
      <td>${r.asset}</td><td>${r.interval}</td>
      <td>${new Date(r.end_ts * 1000).toLocaleString()}</td>
      <td>${r.strike_price ?? "—"}</td><td>${r.outcome ?? "—"}</td>
      <td>${fmtNum(r.tick_count)}</td>
      <td><a href="/api/export/${r.id}?format=csv">CSV</a></td>
    </tr>`
      )
      .join("")
  );

  document.querySelectorAll("a[data-slug]").forEach((el) => {
    el.addEventListener("click", async (e) => {
      e.preventDefault();
      document.querySelectorAll("#history-table tbody tr").forEach((row) => {
        row.classList.remove("is-selected");
      });
      el.closest("tr")?.classList.add("is-selected");
      const title = document.getElementById("replay-title");
      if (title) title.textContent = el.dataset.slug;
      const tickRes = await fetch(
        `/api/windows/${encodeURIComponent(el.dataset.slug)}/ticks`
      );
      const tickData = await tickRes.json();
      if (!tickRes.ok || !tickData.window) {
        if (title) title.textContent = "Window not found";
        return;
      }
      if (!replayCharts) {
        replayCharts = new PM.DualCharts(
          document.getElementById("replay-oracle"),
          document.getElementById("replay-prob")
        );
      }
      const points = (tickData.ticks || []).map((t) => ({
        ts_ms: t.ts_ms,
        oracle_price: t.oracle_price,
        binance_price: t.binance_price,
        twap_oracle: t.twap_oracle,
        yes_ask: t.yes_ask,
        no_ask: t.no_ask,
        yes_mid: t.yes_mid,
        no_mid: t.no_mid,
      }));
      const startTs = tickData.window.start_ts;
      const twapInitial = PM.twapInitialFromPoints(points, startTs);
      replayCharts.setHistory(
        points,
        twapInitial ?? tickData.window.strike_price,
        startTs,
        tickData.window.end_ts,
        tickData.window.opening_oracle_price ?? null,
        PM.openingBinanceFromPoints(points, startTs),
        PM.openingProbFromPoints(points, startTs),
        twapInitial
      );
      document.getElementById("replay-panel")?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    });
  });
}

async function loadBinance() {
  const asset = document.getElementById("filter-asset").value;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(pageOffset),
  });
  if (asset) params.set("asset", asset);
  const res = await fetch(`/api/feeds/binance?${params}`);
  const data = await res.json();
  pageTotal = data.total || 0;
  updatePager();
  setTable(
    ["Time", "Asset", "Price", "Qty", "Trade ID"],
    (data.items || [])
      .map(
        (r) => `<tr>
      <td>${fmtTs(r.ts_ms)}</td>
      <td>${r.asset}</td>
      <td>${r.price}</td>
      <td>${r.qty ?? "—"}</td>
      <td>${r.trade_id ?? "—"}</td>
    </tr>`
      )
      .join("")
  );
}

async function loadOracle() {
  const asset = document.getElementById("filter-asset").value;
  const source = document.getElementById("filter-source").value;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(pageOffset),
  });
  if (asset) params.set("asset", asset);
  if (source) params.set("source", source);
  const res = await fetch(`/api/feeds/oracle?${params}`);
  const data = await res.json();
  pageTotal = data.total || 0;
  updatePager();
  setTable(
    ["Time", "Asset", "Price", "Source"],
    (data.items || [])
      .map(
        (r) => `<tr>
      <td>${fmtTs(r.ts_ms)}</td>
      <td>${r.asset}</td>
      <td>${r.price}</td>
      <td>${r.source}</td>
    </tr>`
      )
      .join("")
  );
}

async function loadClob() {
  const asset = document.getElementById("filter-asset").value;
  const side = document.getElementById("filter-side").value;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(pageOffset),
  });
  if (asset) params.set("asset", asset);
  if (side) params.set("side", side);
  const res = await fetch(`/api/feeds/clob?${params}`);
  const data = await res.json();
  pageTotal = data.total || 0;
  updatePager();
  setTable(
    ["Time", "Asset", "Interval", "Side", "Ask", "Bid", "Event", "Token"],
    (data.items || [])
      .map(
        (r) => `<tr>
      <td>${fmtTs(r.ts_ms)}</td>
      <td>${r.asset ?? "—"}</td>
      <td>${r.interval ?? "—"}</td>
      <td>${r.side ?? "—"}</td>
      <td>${r.best_ask}</td>
      <td>${r.best_bid}</td>
      <td>${r.event_type ?? "—"}</td>
      <td class="mono-cell" title="${r.token_id}">${(r.token_id || "").slice(0, 12)}…</td>
    </tr>`
      )
      .join("")
  );
}

async function loadHistory() {
  setFiltersForTab(currentTab);
  if (currentTab === "windows") await loadWindows();
  else if (currentTab === "binance") await loadBinance();
  else if (currentTab === "oracle") await loadOracle();
  else if (currentTab === "clob") await loadClob();
}

document.getElementById("filter-asset").addEventListener("change", () => {
  pageOffset = 0;
  loadWinRates();
  loadHistory();
});
document.getElementById("filter-interval").addEventListener("change", () => {
  pageOffset = 0;
  loadWinRates();
  loadHistory();
});
document.getElementById("filter-side").addEventListener("change", () => {
  pageOffset = 0;
  loadHistory();
});
document.getElementById("filter-source").addEventListener("change", () => {
  pageOffset = 0;
  loadHistory();
});
document.getElementById("page-prev").addEventListener("click", () => {
  pageOffset = Math.max(0, pageOffset - PAGE_SIZE);
  loadHistory();
});
document.getElementById("page-next").addEventListener("click", () => {
  pageOffset += PAGE_SIZE;
  loadHistory();
});

document.querySelectorAll(".history-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".history-tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentTab = btn.dataset.tab;
    pageOffset = 0;
    loadHistory();
  });
});

loadStats();
loadWinRates();
loadHistory();
