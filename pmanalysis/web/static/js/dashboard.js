(() => {
  const { assets, intervals, preselect } = window.PM_CONFIG;
  let selected = { asset: preselect.asset, interval: preselect.interval };
  let marketByKey = {};
  let charts = null;

  const el = (id) => document.getElementById(id);

  function setWsStatus(state) {
    const badge = el("ws-status");
    if (state === "connected") {
      badge.textContent = "Live";
      badge.className = "ws-badge ws-on";
    } else {
      badge.textContent = state === "disconnected" ? "Reconnecting…" : "Connecting…";
      badge.className = "ws-badge ws-off";
    }
  }

  function renderTicker(oracle) {
    el("oracle-ticker").innerHTML = Object.entries(oracle || {})
      .map(([a, p]) => `<div class="ticker-item"><strong>${a}</strong>${PM.fmtUsd(p)}</div>`)
      .join("");
  }

  function renderNav() {
    const nav = el("market-nav");
    nav.innerHTML = assets
      .map((asset) => {
        const buttons = intervals
          .map((interval) => {
            const key = `${asset}:${interval}`;
            const m = marketByKey[key];
            const cls = PM.positionClass(m);
            const active = selected.asset === asset && selected.interval === interval ? "active" : "";
            const badge = m?.slug ? PM.fmtProb(m.yes_mid) : "—";
            return `<button class="nav-btn ${cls} ${active}" data-asset="${asset}" data-interval="${interval}">
              <span>${interval}</span><span class="badge">${badge}</span>
            </button>`;
          })
          .join("");
        return `<div class="asset-group"><div class="asset-label">${asset}</div>${buttons}</div>`;
      })
      .join("");

    nav.querySelectorAll(".nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        selected = { asset: btn.dataset.asset, interval: btn.dataset.interval };
        socket.select(selected.asset, selected.interval);
        renderNav();
        updateSelectedHeader();
      });
    });
  }

  function updateMetrics(m) {
    if (!m) return;
    const pos = m.dist_from_strike;
    el("position-val").textContent =
      pos != null ? `${pos >= 0 ? "UP ▲" : "DOWN ▼"} ${Math.abs(pos).toFixed(2)}` : "—";
    el("position-card").style.background =
      pos == null ? "" : pos >= 0 ? "var(--win)" : "var(--lose)";
    el("m-yes").textContent = PM.fmtProb(m.yes_mid);
    el("m-no").textContent = PM.fmtProb(m.no_mid);
    el("m-combined").textContent = m.combined_ask ? m.combined_ask.toFixed(1) + "¢" : "—";
    el("m-arb").textContent = m.arb_edge != null ? m.arb_edge.toFixed(1) + "¢" : "—";
    el("m-lean").textContent = m.lean != null ? m.lean.toFixed(3) : "—";
    el("m-strike").textContent = PM.fmtUsd(m.strike_price);
    el("m-question").textContent = m.question || "—";
    el("oracle-last").textContent = PM.fmtUsd(m.oracle_price);
    el("prob-last").textContent = `${PM.fmtProb(m.yes_mid)} / ${PM.fmtProb(m.no_mid)}`;
  }

  function updateSelectedHeader() {
    const m = marketByKey[`${selected.asset}:${selected.interval}`];
    el("sel-title").textContent = `${selected.asset} · ${selected.interval}`;
    if (m?.slug) {
      el("sel-slug").textContent = m.slug;
      el("countdown").textContent = PM.fmtSecs(m.secs_remaining);
    } else {
      el("sel-slug").textContent = "Waiting for active market…";
      el("countdown").textContent = "—";
    }
    updateMetrics(m);
  }

  charts = new PM.DualCharts(el("chart-oracle"), el("chart-prob"));

  const socket = new PM.LiveSocket("/ws/live", {
    onStatus: setWsStatus,
    onSnapshot: (msg) => {
      marketByKey = PM.marketMap(msg.markets);
      renderTicker(msg.oracle_prices);
      renderNav();
      updateSelectedHeader();
    },
    onHistory: (msg) => {
      if (msg.asset !== selected.asset || msg.interval !== selected.interval) return;
      charts.setHistory(msg.points, msg.strike_price);
      updateSelectedHeader();
    },
    onTick: (msg) => {
      const key = `${msg.asset}:${msg.interval}`;
      if (msg.market) marketByKey[key] = msg.market;
      if (msg.asset === selected.asset && msg.interval === selected.interval) {
        charts.appendPoint(msg.point);
        updateMetrics(msg.market);
        el("countdown").textContent = PM.fmtSecs(msg.market?.secs_remaining);
        el("oracle-last").textContent = PM.fmtUsd(msg.point.oracle_price);
        el("prob-last").textContent = `${PM.fmtProb(msg.point.yes_mid)} / ${PM.fmtProb(msg.point.no_mid)}`;
      }
      renderNav();
    },
  });

  socket._selected = selected;
  socket.connect();
})();
