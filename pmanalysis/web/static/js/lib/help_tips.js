window.PM = window.PM || {};

/**
 * Trader-facing help for each readout: what it is, and how it helps a decision.
 * Markup: <button class="tip-btn" data-help="fair_yes" aria-label="About Fair YES">…</button>
 */
PM.HELP = {
  initial_twap: {
    title: "Initial TWAP (target)",
    what: "The 60-second Chainlink TWAP when this round opened. This is the price to beat.",
    use: "The round settles UP if the closing 60s TWAP is at or above this number, DOWN otherwise.",
  },
  chainlink: {
    title: "Chainlink spot",
    what: "The latest Chainlink price, the oracle Polymarket settles these markets on.",
    use: "Spot leads the TWAP: when spot sits above the target late in the round, the TWAP tends to follow.",
  },
  twap: {
    title: "Live TWAP",
    what: "The rolling Chainlink TWAP, published by Polymarket. At the bell, this value decides the round.",
    use: "In the last 60 seconds the TWAP moves slowly, because most of its average is already fixed.",
  },
  difference: {
    title: "Difference",
    what: "Live TWAP minus the target, in dollars.",
    use: "Positive means UP is currently winning, negative means DOWN. Weigh it against σ and the time left.",
  },
  fair_yes: {
    title: "Fair YES",
    what: "Model probability (in ¢) that the round settles UP. It uses spot, the target, time left and recent Chainlink volatility.",
    use: "An independent price to compare with the YES ask. It is a model, so check Research → Calibration to see how well it has matched past results.",
  },
  yes_edge: {
    title: "YES edge",
    what: "Fair YES minus the YES ask, in cents.",
    use: "Positive means YES costs less than the model thinks it is worth. Edges of a few cents are within model error, and fees and slippage reduce them further.",
  },
  no_edge: {
    title: "NO edge",
    what: "(100 − Fair YES) minus the NO ask, in cents.",
    use: "Positive means NO costs less than the model thinks it is worth. YES and NO edges are rarely both positive.",
  },
  sigma: {
    title: "σ /√s (volatility)",
    what: "Typical Chainlink move in dollars per √second over the last 15 minutes.",
    use: "Expected move over t seconds ≈ σ × √t (about σ × 17 for 5 minutes). Compare that with the Difference: a gap much smaller than the expected move can still flip.",
  },
  yes_book: {
    title: "YES ask size · depth",
    what: "Shares offered at the best YES ask · total shares within 2¢ above it.",
    use: "Shows how much you can buy near the quoted price. A thin book means a larger order moves the price against you.",
  },
  no_book: {
    title: "NO ask size · depth",
    what: "Shares offered at the best NO ask · total shares within 2¢ above it.",
    use: "Shows how much you can buy near the quoted price. A thin book means a larger order moves the price against you.",
  },
  volume: {
    title: "Volume",
    what: "USDC traded on YES and NO in this round, since the collector subscribed.",
    use: "More volume means a more active market and more trustworthy quotes. Quiet books can show stale prices.",
  },
  lead_lag: {
    title: "Binance lead",
    what: "Seconds Chainlink trails Binance · ρ (how closely their 1-second moves agree, up to 1) · basis (Binance − Chainlink).",
    use: "With high ρ, a sharp Binance move usually reaches Chainlink after this delay, which gives an early read on where settlement is heading. A low ρ means the signal is unreliable right now.",
  },
};

PM.HelpTips = class {
  constructor(root = document) {
    this.tip = document.createElement("div");
    this.tip.className = "help-tip";
    this.tip.id = "help-tip";
    this.tip.setAttribute("role", "tooltip");
    this.tip.hidden = true;
    document.body.append(this.tip);
    this.active = null;

    root.querySelectorAll(".tip-btn[data-help]").forEach((btn) => {
      const help = PM.HELP[btn.dataset.help];
      if (!help) return;
      if (!btn.hasAttribute("aria-label")) btn.setAttribute("aria-label", `About ${help.title}`);
      btn.addEventListener("mouseenter", () => this.show(btn));
      btn.addEventListener("mouseleave", () => this.hide());
      btn.addEventListener("focus", () => this.show(btn));
      btn.addEventListener("blur", () => this.hide());
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (this.active === btn) this.hide();
        else this.show(btn); // touch screens have no hover
      });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.hide();
    });
    document.addEventListener("click", () => this.hide());
    window.addEventListener("scroll", () => this.hide(), { passive: true, capture: true });
    window.addEventListener("resize", () => this.hide());
  }

  show(btn) {
    const help = PM.HELP[btn.dataset.help];
    if (!help) return;
    const title = document.createElement("strong");
    title.textContent = help.title;
    const what = document.createElement("p");
    what.textContent = help.what;
    const use = document.createElement("p");
    use.className = "help-tip-use";
    const label = document.createElement("span");
    label.textContent = "How it helps: ";
    use.append(label, help.use);
    this.tip.replaceChildren(title, what, use);
    this.tip.hidden = false;
    this.active?.removeAttribute("aria-describedby");
    btn.setAttribute("aria-describedby", this.tip.id);
    this.active = btn;
    this._place(btn);
  }

  hide() {
    if (!this.active) return;
    this.active.removeAttribute("aria-describedby");
    this.active = null;
    this.tip.hidden = true;
  }

  _place(btn) {
    const margin = 8;
    const anchor = btn.getBoundingClientRect();
    const box = this.tip.getBoundingClientRect();
    let left = anchor.left + anchor.width / 2 - box.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - box.width - margin));
    let top = anchor.bottom + 6;
    if (top + box.height > window.innerHeight - margin) top = anchor.top - box.height - 6; // flip up
    this.tip.style.left = `${Math.round(left)}px`;
    this.tip.style.top = `${Math.round(Math.max(margin, top))}px`;
  }
};

document.addEventListener("DOMContentLoaded", () => {
  if (document.querySelector(".tip-btn[data-help]")) PM.helpTips = new PM.HelpTips();
});
