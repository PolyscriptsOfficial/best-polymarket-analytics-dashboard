(() => {
  const { checkpoints } = window.PM_CONFIG;
  const el = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  // Validated on the #121a24 panel (dataviz validator, dark mode).
  const SERIES = {
    market: { label: "Market mid", color: "#4f8ff0" },
    fair: { label: "Fair value", color: "#c27c1e" },
  };
  const MIN_BUCKET_N = 5;
  let calData = null;
  let btData = null;

  const svg = (tag, attrs = {}) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };

  const params = () => ({
    asset: el("rs-asset").value,
    interval: el("rs-interval").value,
    days: Number(el("rs-days").value),
    t_minus: Number(el("rs-tminus").value),
  });

  function fillCheckpoints() {
    const select = el("rs-tminus");
    const prev = Number(select.value) || 30;
    const options = checkpoints[el("rs-interval").value] || [];
    select.replaceChildren(
      ...options.map((t) => {
        const o = document.createElement("option");
        o.value = t;
        o.textContent = `T−${t >= 60 ? `${t / 60}m` : `${t}s`}`;
        return o;
      })
    );
    select.value = options.includes(prev) ? prev : options.includes(30) ? 30 : options[0];
  }

  // --- tooltip --------------------------------------------------------------
  const tip = document.createElement("div");
  tip.className = "research-tip";
  tip.hidden = true;
  document.body.append(tip);
  function showTip(ev, lines) {
    tip.replaceChildren(
      ...lines.map((text, i) => {
        const row = document.createElement(i === 0 ? "strong" : "span");
        row.textContent = text;
        return row;
      })
    );
    tip.hidden = false;
    const x = Math.min(window.innerWidth - tip.offsetWidth - 12, ev.clientX + 14);
    tip.style.left = `${x}px`;
    tip.style.top = `${ev.clientY + 14}px`;
  }
  const hideTip = () => {
    tip.hidden = true;
  };

  // --- shared axes ------------------------------------------------------------
  function frame(container, height) {
    // An empty chart box is display:none until filled, so fall back to its
    // parent's width (minus the box padding) instead of a fixed default.
    const avail = container.clientWidth || (container.parentElement?.clientWidth ?? 640) - 26;
    const width = Math.max(320, avail);
    const m = { top: 14, right: 18, bottom: 34, left: 52 };
    const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "research-svg" });
    return { root, width, height, m, w: width - m.left - m.right, h: height - m.top - m.bottom };
  }

  function axis(f, { xTicks, yTicks, x, y, xFmt, yFmt, xLabel, yLabel }) {
    const g = svg("g", { class: "axis" });
    for (const t of yTicks) {
      g.append(svg("line", { x1: f.m.left, x2: f.m.left + f.w, y1: y(t), y2: y(t), class: "grid-line" }));
      const label = svg("text", { x: f.m.left - 8, y: y(t) + 4, "text-anchor": "end" });
      label.textContent = yFmt(t);
      g.append(label);
    }
    xTicks.forEach((t, i) => {
      // Edge labels hug the plot edges so they are never clipped.
      const anchor = i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle";
      const label = svg("text", { x: x(t), y: f.m.top + f.h + 18, "text-anchor": anchor });
      label.textContent = xFmt(t);
      g.append(label);
    });
    if (xLabel) {
      const label = svg("text", { x: f.m.left + f.w / 2, y: f.height - 2, "text-anchor": "middle", class: "axis-title" });
      label.textContent = xLabel;
      g.append(label);
    }
    if (yLabel) {
      const label = svg("text", {
        x: -(f.m.top + f.h / 2), y: 12, transform: "rotate(-90)", "text-anchor": "middle", class: "axis-title",
      });
      label.textContent = yLabel;
      g.append(label);
    }
    f.root.append(g);
  }

  function legend(container, keys) {
    const row = document.createElement("div");
    row.className = "research-legend";
    for (const key of keys) {
      const s = SERIES[key];
      const item = document.createElement("span");
      item.className = "research-legend-item";
      item.style.setProperty("--swatch", s.color);
      item.classList.add(key === "fair" ? "is-square" : "is-dot");
      item.textContent = s.label;
      row.append(item);
    }
    container.append(row);
  }

  // --- calibration -----------------------------------------------------------
  function renderCalibration() {
    const box = el("cal-chart");
    box.replaceChildren();
    if (!calData) return;
    const series = ["market", "fair"].filter((k) => calData[k].bins.length);
    if (!series.length) {
      box.textContent = "No resolved windows with this checkpoint yet.";
      return;
    }
    legend(box, series);
    const f = frame(box, 320);
    const x = (v) => f.m.left + (v / 100) * f.w;
    const y = (v) => f.m.top + f.h - (v / 100) * f.h;
    const ticks = [0, 25, 50, 75, 100];
    axis(f, {
      xTicks: ticks, yTicks: ticks, x, y,
      xFmt: (t) => `${t}¢`, yFmt: (t) => `${t}%`,
      xLabel: "Implied YES at checkpoint", yLabel: "Realized UP rate",
    });
    f.root.append(svg("line", { x1: x(0), y1: y(0), x2: x(100), y2: y(100), class: "ref-line" }));

    // Buckets are not a continuous series: dots sized by window count with
    // 95% whiskers, no connecting line. Thin buckets are faded.
    const maxN = Math.max(...series.flatMap((k) => calData[k].bins.map((b) => b.n)));
    for (const key of series) {
      const s = SERIES[key];
      for (const b of calData[key].bins) {
        const opacity = b.n < MIN_BUCKET_N ? 0.35 : 1;
        if (b.ci) {
          f.root.append(svg("line", {
            x1: x(b.avg_price), x2: x(b.avg_price), y1: y(b.ci[0]), y2: y(b.ci[1]),
            stroke: s.color, "stroke-width": 1.5, opacity: opacity * 0.6,
          }));
        }
        const dot = svg(key === "fair" ? "rect" : "circle", {
          fill: s.color, class: "dot", opacity,
        });
        const r = 4 + 5 * Math.sqrt(b.n / maxN);
        if (key === "fair") {
          dot.setAttribute("x", x(b.avg_price) - r);
          dot.setAttribute("y", y(b.up_rate) - r);
          dot.setAttribute("width", 2 * r);
          dot.setAttribute("height", 2 * r);
          dot.setAttribute("rx", 2);
        } else {
          dot.setAttribute("cx", x(b.avg_price));
          dot.setAttribute("cy", y(b.up_rate));
          dot.setAttribute("r", r);
        }
        const hit = svg("circle", { cx: x(b.avg_price), cy: y(b.up_rate), r: 12, class: "hit" });
        const lines = [
          `${s.label} · ${b.label}`,
          `Avg implied ${b.avg_price.toFixed(1)}¢`,
          `UP ${b.up_rate.toFixed(1)}% (${b.up}/${b.n})`,
          b.ci ? `95% CI ${b.ci[0]}–${b.ci[1]}%` : "",
        ].filter(Boolean);
        hit.addEventListener("mousemove", (ev) => showTip(ev, lines));
        hit.addEventListener("mouseleave", hideTip);
        f.root.append(dot, hit);
      }
    }
    box.append(f.root);
  }

  function renderCalibrationTable() {
    const wrap = el("cal-table");
    if (!calData) return wrap.replaceChildren();
    const rows = [];
    for (const key of ["market", "fair"]) {
      for (const b of calData[key].bins) {
        rows.push([SERIES[key].label, b.label, b.n, `${b.avg_price.toFixed(1)}¢`, `${b.up_rate.toFixed(1)}%`,
          b.ci ? `${b.ci[0]}–${b.ci[1]}%` : "—"]);
      }
    }
    wrap.replaceChildren(table(["Series", "Bucket", "Windows", "Avg implied", "UP rate", "95% CI"], rows));
  }

  function table(headers, rows) {
    const t = document.createElement("table");
    t.className = "data-table compact-table";
    const thead = t.createTHead().insertRow();
    for (const h of headers) {
      const th = document.createElement("th");
      th.textContent = h;
      thead.append(th);
    }
    const body = t.createTBody();
    for (const row of rows) {
      const tr = body.insertRow();
      for (const cell of row) {
        const td = tr.insertCell();
        if (cell instanceof Node) td.append(cell);
        else td.textContent = cell;
      }
    }
    return t;
  }

  async function loadCalibration() {
    const p = params();
    const q = new URLSearchParams({ asset: p.asset, interval: p.interval, days: p.days, t_minus: p.t_minus });
    const res = await fetch(`/api/research/calibration?${q}`);
    calData = res.ok ? await res.json() : null;
    const fmt = (s) => (s.brier == null ? "—" : s.brier.toFixed(4));
    el("cal-summary").textContent = calData
      ? `${calData.market.n} windows at T−${p.t_minus}s · Brier score (lower is better): market ${fmt(calData.market)}, fair value ${fmt(calData.fair)}. Marks above the diagonal mean UP was underpriced; size = windows, faded = fewer than ${MIN_BUCKET_N}.`
      : "Could not load calibration.";
    renderCalibration();
    renderCalibrationTable();
  }

  // --- regimes ---------------------------------------------------------------
  function rateCell(row) {
    const wrap = document.createElement("span");
    wrap.className = "rate-cell";
    const bar = document.createElement("span");
    bar.className = "rate-bar";
    bar.style.setProperty("--rate", `${row.up_rate ?? 0}%`);
    const text = document.createElement("span");
    text.textContent = row.up_rate == null ? "—" : `${row.up_rate.toFixed(1)}%`;
    wrap.append(bar, text);
    return wrap;
  }

  function regimeTable(id, rows, extra) {
    const node = el(id);
    if (!rows?.length) {
      node.textContent = "Not enough data.";
      return;
    }
    const headers = ["", "Windows", "UP rate", "95% CI (%)", ...(extra ? [extra.title] : [])];
    node.replaceChildren(
      table(headers, rows.map((r) => [
        r.label, r.n, rateCell(r), r.ci ? `${r.ci[0]}–${r.ci[1]}` : "—", ...(extra ? [extra.get(r)] : []),
      ]))
    );
  }

  async function loadRegimes() {
    const p = params();
    const q = new URLSearchParams({ asset: p.asset, interval: p.interval, days: p.days });
    const res = await fetch(`/api/research/regimes?${q}`);
    const data = res.ok ? await res.json() : null;
    if (!data) {
      el("reg-summary").textContent = "Could not load regimes.";
      return;
    }
    const o = data.overall;
    el("reg-summary").textContent = o.n
      ? `${o.n} resolved windows · UP ${o.up_rate.toFixed(1)}% (95% CI ${o.ci[0]}–${o.ci[1]}%). Rows whose interval excludes 50% are worth a closer look.`
      : "No resolved windows in this period.";
    regimeTable("reg-hour", data.by_hour_et);
    regimeTable("reg-streak", data.after_streak);
    regimeTable("reg-vol", data.by_volatility, { title: "Range", get: (r) => r.range });
    regimeTable("reg-move", data.by_open_move, { title: "Range ($)", get: (r) => r.range });
  }

  // --- backtest --------------------------------------------------------------
  function tile(label, value, cls = "") {
    const node = document.createElement("div");
    node.className = "bt-tile";
    const l = document.createElement("span");
    l.className = "edge-label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = `bt-tile-value ${cls}`;
    v.textContent = value;
    node.append(l, v);
    return node;
  }

  const fmtUsdSigned = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}`);
  const fmtTime = (ts) =>
    new Date(ts * 1000).toLocaleString("en-US", {
      timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });

  function renderEquity() {
    const box = el("bt-chart");
    box.replaceChildren();
    const curve = btData?.equity || [];
    if (curve.length < 2) return;
    const f = frame(box, 220);
    const values = curve.map((p) => p.equity);
    const lo = Math.min(0, ...values);
    const hi = Math.max(0, ...values);
    const pad = (hi - lo) * 0.08 || 1;
    const yMin = lo - pad;
    const yMax = hi + pad;
    const x = (i) => f.m.left + (i / (curve.length - 1)) * f.w;
    const y = (v) => f.m.top + f.h - ((v - yMin) / (yMax - yMin)) * f.h;
    const step = (yMax - yMin) / 4;
    axis(f, {
      xTicks: [0, Math.floor((curve.length - 1) / 2), curve.length - 1],
      yTicks: [0, 1, 2, 3, 4].map((k) => yMin + k * step),
      x, y,
      xFmt: (i) => fmtTime(curve[i].ts),
      yFmt: (v) => fmtUsdSigned(v),
    });
    f.root.append(svg("line", { x1: f.m.left, x2: f.m.left + f.w, y1: y(0), y2: y(0), class: "zero-line" }));
    f.root.append(svg("polyline", {
      points: curve.map((p, i) => `${x(i)},${y(p.equity)}`).join(" "),
      fill: "none", stroke: SERIES.market.color, "stroke-width": 2, "stroke-linejoin": "round",
    }));
    const cross = svg("line", { y1: f.m.top, y2: f.m.top + f.h, class: "crosshair", visibility: "hidden" });
    const marker = svg("circle", { r: 4.5, fill: SERIES.market.color, class: "dot", visibility: "hidden" });
    const overlay = svg("rect", { x: f.m.left, y: f.m.top, width: f.w, height: f.h, class: "hit" });
    overlay.addEventListener("mousemove", (ev) => {
      const rect = overlay.getBoundingClientRect();
      const i = Math.round(((ev.clientX - rect.left) / rect.width) * (curve.length - 1));
      const p = curve[Math.max(0, Math.min(curve.length - 1, i))];
      const idx = curve.indexOf(p);
      cross.setAttribute("x1", x(idx));
      cross.setAttribute("x2", x(idx));
      marker.setAttribute("cx", x(idx));
      marker.setAttribute("cy", y(p.equity));
      cross.setAttribute("visibility", "visible");
      marker.setAttribute("visibility", "visible");
      showTip(ev, [fmtTime(p.ts) + " ET", `Cumulative P&L ${fmtUsdSigned(p.equity)}`]);
    });
    overlay.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      marker.setAttribute("visibility", "hidden");
      hideTip();
    });
    f.root.append(cross, marker, overlay);
    box.append(f.root);
  }

  function renderBacktest() {
    const tiles = el("bt-tiles");
    const s = btData?.summary;
    if (!s) {
      tiles.replaceChildren();
      el("bt-trades").replaceChildren();
      return;
    }
    const pnlCls = s.pnl_usd > 0 ? "up" : s.pnl_usd < 0 ? "down" : "";
    const sk = s.skipped;
    tiles.replaceChildren(
      tile("Trades", String(s.trades)),
      tile("Win rate", s.win_rate == null ? "—" : `${s.win_rate}%`),
      tile("Break-even rate", s.breakeven_rate == null ? "—" : `${s.breakeven_rate}%`),
      tile("Avg fill", PM.fmtCents(s.avg_fill, 2)),
      tile("P&L", fmtUsdSigned(s.pnl_usd), pnlCls),
      tile("ROI", s.roi_pct == null ? "—" : `${s.roi_pct}%`, pnlCls),
      tile("Max drawdown", fmtUsdSigned(s.max_drawdown_usd)),
      tile("Skipped", `${Object.values(sk).reduce((a, b) => a + b, 0)}`)
    );
    const skipText = Object.entries(sk).filter(([, n]) => n).map(([k, n]) => `${k.replace("_", " ")} ${n}`).join(" · ");
    tiles.lastChild.title = skipText || "none";
    renderEquity();
    el("bt-trades").replaceChildren(
      table(
        ["Window (ET)", "Side", "Ask", "Fill", "TWAP diff", "Fair YES", "Edge", "Outcome", "P&L"],
        btData.trades.map((t) => {
          const pnl = document.createElement("span");
          pnl.className = t.pnl_usd >= 0 ? "up" : "down";
          pnl.textContent = fmtUsdSigned(t.pnl_usd);
          return [
            fmtTime(t.start_ts), t.side, PM.fmtCents(t.ask), PM.fmtCents(t.fill, 2),
            t.twap_diff == null ? "—" : t.twap_diff.toFixed(2), PM.fmtCents(t.fair_yes),
            t.edge == null ? "—" : PM.fmtEdge(t.edge).text, t.outcome, pnl,
          ];
        })
      )
    );
  }

  async function runBacktest(ev) {
    ev?.preventDefault();
    const form = new FormData(el("bt-form"));
    const num = (name) => {
      const raw = form.get(name);
      return raw === null || raw === "" ? null : Number(raw);
    };
    const body = {
      ...params(),
      side: form.get("side"),
      min_ask: num("min_ask"),
      max_ask: num("max_ask"),
      min_diff: num("min_diff"),
      min_edge: num("min_edge"),
      shares: num("shares"),
      slippage_cents: num("slippage_cents"),
      fee_pct: num("fee_pct"),
      require_size: form.get("require_size") === "on",
    };
    const res = await fetch("/api/research/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    btData = res.ok ? await res.json() : null;
    if (!res.ok) {
      el("bt-tiles").textContent = `Backtest failed: ${(await res.json().catch(() => ({}))).detail || res.status}`;
      el("bt-chart").replaceChildren();
      el("bt-trades").replaceChildren();
      return;
    }
    renderBacktest();
  }

  // --- wiring ----------------------------------------------------------------
  function reloadAll() {
    void loadCalibration();
    void loadRegimes();
    void runBacktest();
  }

  el("rs-interval").addEventListener("change", () => {
    fillCheckpoints();
    reloadAll();
  });
  for (const id of ["rs-asset", "rs-days", "rs-tminus"]) el(id).addEventListener("change", reloadAll);
  el("bt-form").addEventListener("submit", runBacktest);
  el("cal-table-toggle").addEventListener("click", (ev) => {
    const wrap = el("cal-table");
    const show = wrap.classList.toggle("hidden") === false;
    ev.currentTarget.textContent = show ? "Hide table" : "Show table";
    ev.currentTarget.setAttribute("aria-pressed", String(show));
  });
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      renderCalibration();
      renderEquity();
    }, 120);
  });

  fillCheckpoints();
  reloadAll();
})();
