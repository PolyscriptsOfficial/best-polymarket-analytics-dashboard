let replayCharts = null;

async function loadHistory() {
  const asset = document.getElementById("filter-asset").value;
  const interval = document.getElementById("filter-interval").value;
  const params = new URLSearchParams({ limit: "100" });
  if (asset) params.set("asset", asset);
  if (interval) params.set("interval", interval);
  const res = await fetch(`/api/windows?${params}`);
  const rows = await res.json();
  const tbody = document.querySelector("#history-table tbody");
  tbody.innerHTML = rows
    .map(
      (r) => `<tr>
      <td><a href="#" data-slug="${r.slug}">${r.slug}</a></td>
      <td>${r.asset}</td><td>${r.interval}</td>
      <td>${new Date(r.end_ts * 1000).toLocaleString()}</td>
      <td>${r.strike_price ?? "—"}</td><td>${r.outcome ?? "—"}</td>
      <td>${r.tick_count}</td>
      <td><a href="/api/export/${r.id}?format=csv">CSV</a></td>
    </tr>`
    )
    .join("");

  tbody.querySelectorAll("a[data-slug]").forEach((el) => {
    el.addEventListener("click", async (e) => {
      e.preventDefault();
      const res = await fetch(`/api/windows/${encodeURIComponent(el.dataset.slug)}/ticks`);
      const data = await res.json();
      if (!replayCharts) {
        replayCharts = new PM.DualCharts(
          document.getElementById("replay-oracle"),
          document.getElementById("replay-prob")
        );
      }
      const points = data.ticks.map((t) => ({
        ts_ms: t.ts_ms,
        oracle_price: t.oracle_price,
        yes_mid: t.yes_mid,
        no_mid: t.no_mid,
      }));
      replayCharts.setHistory(points, data.window.strike_price);
    });
  });
}

document.getElementById("filter-asset").addEventListener("change", loadHistory);
document.getElementById("filter-interval").addEventListener("change", loadHistory);
loadHistory();
