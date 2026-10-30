window.PM = window.PM || {};

/** Fair value, edge, order book and lead/lag readouts shared by Terminal and Grid. */
PM.fmtCents = (v, digits = 1) => (v == null ? "—" : `${Number(v).toFixed(digits)}¢`);

PM.fmtEdge = (edge) => {
  if (edge == null) return { text: "—", cls: "" };
  const sign = edge > 0 ? "+" : "";
  return { text: `${sign}${edge.toFixed(1)}¢`, cls: edge > 0 ? "up" : edge < 0 ? "down" : "" };
};

PM.fmtShares = (v) => {
  if (v == null) return "—";
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return `${Math.round(v)}`;
};

PM.fmtUsdCompact = (v) => {
  if (v == null) return "—";
  if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(1)}k`;
  return `$${Math.round(v)}`;
};

/** Best side to buy by model edge: { side, edge } or null. */
PM.bestEdge = (m) => {
  const yes = m?.edge_yes;
  const no = m?.edge_no;
  if (yes == null && no == null) return null;
  if (no == null || (yes != null && yes >= no)) return { side: "YES", edge: yes };
  return { side: "NO", edge: no };
};

PM.fmtLeadLag = (row) => {
  if (!row) return "—";
  const basis = row.basis == null ? "" : ` · basis ${row.basis >= 0 ? "+" : "−"}$${Math.abs(row.basis).toFixed(2)}`;
  return `${row.lag_sec}s · ρ ${Number(row.corr).toFixed(2)}${basis}`;
};

PM.renderEdgeStrip = (m, leadLag) => {
  const set = (id, text, cls) => {
    const node = document.getElementById(id);
    if (!node) return;
    node.textContent = text;
    if (cls !== undefined) node.className = `edge-value ${cls}`;
  };
  set("edge-fair", PM.fmtCents(m?.fair_yes));
  const yes = PM.fmtEdge(m?.edge_yes);
  const no = PM.fmtEdge(m?.edge_no);
  set("edge-yes", yes.text, yes.cls);
  set("edge-no", no.text, no.cls);
  set("edge-sigma", m?.sigma == null ? "—" : `$${Number(m.sigma).toFixed(3)}`);
  set("book-yes", `${PM.fmtShares(m?.yes_ask_size)} · ${PM.fmtShares(m?.yes_ask_depth)}`);
  set("book-no", `${PM.fmtShares(m?.no_ask_size)} · ${PM.fmtShares(m?.no_ask_depth)}`);
  set("book-volume", PM.fmtUsdCompact(m?.volume_usd));
  if (leadLag !== undefined) set("lead-lag", PM.fmtLeadLag(leadLag));
};

/** Toasts + optional browser notifications for server alerts (edge / arb / feed gaps). */
PM.Alerts = class {
  constructor(toggleEl, stackEl) {
    this.toggleEl = toggleEl;
    this.stackEl = stackEl;
    this.enabled = localStorage.getItem("pm.alerts") === "1";
    this._render();
    toggleEl?.addEventListener("click", () => void this.toggle());
  }

  async toggle() {
    if (!this.enabled && "Notification" in window && Notification.permission === "default") {
      await Notification.requestPermission();
    }
    this.enabled = !this.enabled;
    localStorage.setItem("pm.alerts", this.enabled ? "1" : "0");
    this._render();
  }

  _render() {
    if (!this.toggleEl) return;
    this.toggleEl.textContent = this.enabled ? "Alerts: on" : "Alerts: off";
    this.toggleEl.classList.toggle("is-on", this.enabled);
    this.toggleEl.setAttribute("aria-pressed", String(this.enabled));
  }

  handle(alert) {
    if (!this.enabled) return;
    this._toast(alert);
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
      try {
        new Notification(alert.title, { body: alert.message, tag: `${alert.kind}:${alert.slug || ""}` });
      } catch (_) {
        /* some browsers only allow notifications from a service worker */
      }
    }
  }

  _toast(alert) {
    if (!this.stackEl) return;
    const node = document.createElement("div");
    node.className = `alert-toast alert-${alert.kind}`;
    node.setAttribute("role", "status");
    const title = document.createElement("strong");
    title.textContent = alert.title;
    const body = document.createElement("span");
    body.textContent = alert.message;
    node.append(title, body);
    if (alert.asset && alert.interval) {
      node.addEventListener("click", () => {
        location.href = `/market/${alert.asset}/${alert.interval}`;
      });
    }
    this.stackEl.prepend(node);
    while (this.stackEl.children.length > 4) this.stackEl.lastElementChild.remove();
    setTimeout(() => node.remove(), 12000);
  }
};
