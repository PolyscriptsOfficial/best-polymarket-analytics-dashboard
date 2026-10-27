const REFRESH_MS = 5000;

function fmtLag(sec) {
  if (sec == null || Number.isNaN(Number(sec))) return "—";
  const n = Number(sec);
  if (n < 1) return `${n.toFixed(2)}s`;
  if (n < 60) return `${n.toFixed(1)}s`;
  const m = Math.floor(n / 60);
  const s = Math.round(n % 60);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function fmtNum(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString();
}

function feedCard(feed) {
  const status = feed.status || "waiting";
  return `<article class="status-card feed-card feed-${status}">
    <div class="feed-card-head">
      <h3>${feed.label || feed.source}</h3>
      <span class="status-pill status-${status}">${status}</span>
    </div>
    <p>Lag: <strong>${fmtLag(feed.lag_sec)}</strong></p>
    <p>Threshold: ${fmtLag(feed.threshold_sec)}</p>
    <p>Socket: ${feed.connected == null ? "unknown (DB)" : feed.connected ? "connected" : "down"}</p>
    <p>Source: ${feed.origin === "db" ? "latest DB row" : "live collector"}</p>
  </article>`;
}

function gapRows(gaps, { withEnded }) {
  if (!gaps.length) {
    const cols = withEnded ? 5 : 4;
    return `<tr><td colspan="${cols}">None</td></tr>`;
  }
  return gaps
    .map((g) => {
      const ended = withEnded
        ? `<td>${g.open ? "open" : fmtTs(g.ended_at)}</td>`
        : "";
      return `<tr>
        <td>${g.source}</td>
        <td>${fmtTs(g.started_at)}</td>
        ${ended}
        <td>${fmtLag(g.duration_sec)}</td>
        <td>${g.message || "—"}</td>
      </tr>`;
    })
    .join("");
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

async function refreshStatus() {
  const res = await fetch("/api/status");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();

  document.getElementById("feed-grid").innerHTML = (data.feeds || [])
    .map(feedCard)
    .join("");

  document.querySelector("#gaps-open-table tbody").innerHTML = gapRows(
    data.gaps_open || [],
    { withEnded: false }
  );
  document.querySelector("#gaps-recent-table tbody").innerHTML = gapRows(
    data.gaps_recent || [],
    { withEnded: true }
  );

  setText("st-embed", data.embed_collector ? "yes" : "no (DB sync)");
  setText("st-running", data.collector_running ? "yes" : "no");
  setText("st-record", data.feed_record ? "on" : "off");
  setText("st-last-tick", fmtTs(data.last_tick_at));
  setText("st-last-discovery", fmtTs(data.last_discovery_at));
  setText("st-db-path", data.path || "—");
  setText("st-db-size", `${data.size_mb ?? "—"} MB`);
  setText("st-windows", fmtNum(data.window_count));
  setText("st-ticks", fmtNum(data.tick_count));
  setText("st-binance", fmtNum(data.binance_trade_count));
  setText("st-oracle", fmtNum(data.oracle_tick_count));
  setText("st-clob", fmtNum(data.clob_quote_count));

  const updated = document.getElementById("status-updated");
  if (updated) updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function loop() {
  try {
    await refreshStatus();
  } catch (err) {
    const hint = document.getElementById("status-updated");
    if (hint) hint.textContent = `Status refresh failed: ${err.message}`;
  }
  setTimeout(loop, REFRESH_MS);
}

loop();
