(() => {
  const { assets, intervals } = window.PM_CONFIG;
  const el = (id) => document.getElementById(id);
  const grid = el("market-grid");
  const alerts = new PM.Alerts(el("alerts-toggle"), el("alert-stack"));
  const markets = {};
  const cards = {};
  let leadLag = {};
  let renderRaf = 0;
  const dirty = new Set();

  function buildCard(asset, interval) {
    const key = `${asset}:${interval}`;
    const node = document.createElement("a");
    node.className = "grid-card";
    node.href = `/market/${asset}/${interval}`;
    node.innerHTML = `
      <div class="grid-card-head">
        <span class="grid-card-title">${asset} · ${interval}</span>
        <span class="grid-card-clock" data-f="clock">—</span>
      </div>
      <div class="grid-card-quotes">
        <div><span class="edge-label">YES ask</span><span class="grid-big yes" data-f="yes">—</span></div>
        <div><span class="edge-label">NO ask</span><span class="grid-big no" data-f="no">—</span></div>
        <div><span class="edge-label">Fair YES</span><span class="grid-big fair" data-f="fair">—</span></div>
      </div>
      <dl class="grid-card-stats">
        <div><dt>TWAP diff</dt><dd data-f="diff">—</dd></div>
        <div><dt>Best edge</dt><dd data-f="edge">—</dd></div>
        <div><dt>YES+NO</dt><dd data-f="combined">—</dd></div>
        <div><dt title="Shares within 2¢ of the best ask, YES · NO">Depth Y·N</dt><dd data-f="depth">—</dd></div>
        <div><dt>Volume</dt><dd data-f="volume">—</dd></div>
        <div><dt>Target</dt><dd data-f="target">—</dd></div>
      </dl>`;
    const fields = {};
    node.querySelectorAll("[data-f]").forEach((n) => {
      fields[n.dataset.f] = n;
    });
    cards[key] = { node, fields };
    return node;
  }

  for (const asset of assets) {
    for (const interval of intervals) grid.append(buildCard(asset, interval));
  }

  function fmtClock(endTs) {
    if (!endTs) return "—";
    const left = Math.max(0, Math.round(endTs - Date.now() / 1000));
    return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  }

  function renderCard(key) {
    const card = cards[key];
    const m = markets[key];
    if (!card || !m) return;
    const f = card.fields;
    f.yes.textContent = PM.fmtCents(m.yes_ask > 0 ? m.yes_ask : null);
    f.no.textContent = PM.fmtCents(m.no_ask > 0 ? m.no_ask : null);
    f.fair.textContent = PM.fmtCents(m.fair_yes);
    const diff = PM.fmtPriceDelta(m.dist_from_opening);
    f.diff.textContent = m.dist_from_opening != null ? diff.text : "—";
    f.diff.className = diff.cls;
    const best = PM.bestEdge(m);
    const edge = PM.fmtEdge(best?.edge ?? null);
    f.edge.textContent = best ? `${best.side} ${edge.text}` : "—";
    f.edge.className = edge.cls;
    const combined = m.yes_ask > 0 && m.no_ask > 0 ? m.yes_ask + m.no_ask : null;
    f.combined.textContent = PM.fmtCents(combined);
    f.combined.className = combined != null && combined < 100 ? "up" : "";
    f.depth.textContent = `${PM.fmtShares(m.yes_ask_depth)} · ${PM.fmtShares(m.no_ask_depth)}`;
    f.volume.textContent = PM.fmtUsdCompact(m.volume_usd);
    f.target.textContent = PM.fmtUsdPrecise(m.twap_initial);
    card.node.classList.toggle("is-up", (m.dist_from_opening ?? 0) > 0);
    card.node.classList.toggle("is-down", (m.dist_from_opening ?? 0) < 0);
  }

  function scheduleRender(key) {
    dirty.add(key);
    if (renderRaf) return;
    renderRaf = requestAnimationFrame(() => {
      renderRaf = 0;
      for (const k of dirty) renderCard(k);
      dirty.clear();
    });
  }

  function applyMarket(m) {
    if (!m?.asset || !m?.interval) return;
    const key = `${m.asset}:${m.interval}`;
    const clock = PM.clockBounds(m.interval);
    // Ignore cards from a bucket that already rolled.
    if (m.end_ts && clock && m.end_ts < clock.end) return;
    markets[key] = { ...markets[key], ...m };
    scheduleRender(key);
  }

  function renderLead(msg) {
    if (!msg?.lead_lag) return;
    leadLag = msg.lead_lag;
    el("grid-lead").textContent = assets
      .filter((a) => leadLag[a])
      .map((a) => `${a} ${PM.fmtLeadLag(leadLag[a])}`)
      .join("   ·   ") || "—";
  }

  setInterval(() => {
    for (const [key, card] of Object.entries(cards)) {
      const [, interval] = key.split(":");
      const m = markets[key];
      card.fields.clock.textContent = fmtClock(m?.end_ts || PM.clockBounds(interval).end);
    }
  }, 250);

  function setWsStatus(state) {
    const badge = el("ws-status");
    if (!badge) return;
    const live = state === "connected";
    badge.textContent = live ? "Live" : state === "disconnected" ? "Reconnecting…" : "Connecting…";
    badge.className = `ws-badge ${live ? "ws-on" : "ws-off"}`;
  }

  const socket = new PM.LiveSocket("/ws/live", {
    onStatus: setWsStatus,
    onSnapshot: (msg) => {
      renderLead(msg);
      for (const m of msg.markets || []) applyMarket(m);
    },
    onOracle: renderLead,
    onTick: (msg) => applyMarket(msg.market),
    onAlert: (msg) => alerts.handle(msg),
  });
  socket.selectAll();
  socket.connect();
})();
