(() => {
  const { assets, intervals, preselect } = window.PM_CONFIG;
  let selected = { asset: preselect.asset, interval: preselect.interval };
  let marketByKey = {};
  let charts = null;
  let candleChart = null;
  let liveCandle = null;
  let candleLoadToken = 0;
  let liveSlug = "";
  let selectedSlug = "";
  let slugWindows = [];
  let slugCacheKey = "";
  let slugRefreshAt = 0;
  let viewingLive = true;
  let activeBounds = null;
  let countdownEndTs = 0;
  let countdownTimer = null;
  let openingPollTimer = null;
  let chartOpeningPrice = null;
  let lastOraclePrices = {};
  let wsStatusTimer = null;
  let wsIsLive = false;
  let tickerRaf = 0;
  let navRaf = 0;
  let metricsRaf = 0;
  let pendingMetricsMarket = null;
  let noticeTimer = 0;

  let previewCharts = [];
  let previewLoadToken = 0;
  let previewKey = "";
  let previewPage = 0;
  let previewHasOlder = false;
  let previewHasNewer = false;
  let rollSeq = 0;
  const HISTORY_RESYNC_MS = [6000, 20000];
  const RESYNC_MIN_GAP_MS = 5000;
  let resyncTimer = 0;
  let lastResyncAt = 0;
  let previewTimer = 0;
  let leadLag = {};

  const el = (id) => document.getElementById(id);
  const SLUG_CACHE_MS = 60000;
  const PREVIEW_COUNT = 7;
  const PREVIEW_REFRESH_MS = 10000;
  const SIDEBAR_KEY = "pm.sidebarCollapsed";
  const NARROW_MQ = window.matchMedia("(max-width: 780px)");
  const alerts = new PM.Alerts(el("alerts-toggle"), el("alert-stack"));

  function isNarrow() {
    return NARROW_MQ.matches;
  }

  function updateTwapLabels() {
    const label = PM.twapLabel(selected.interval);
    const heroLabel = el("hero-twap-label");
    if (heroLabel) heroLabel.textContent = label;
    const legend = el("legend-twap");
    if (legend) legend.textContent = label;
    if (typeof charts?.setTwapLabel === "function") charts.setTwapLabel(label);
  }

  function applySidebarCollapsed(collapsed, persist) {
    const layout = el("app-layout");
    const btn = el("sidebar-toggle");
    const backdrop = el("sidebar-backdrop");
    if (!layout) return;
    layout.classList.toggle("sidebar-collapsed", !!collapsed);
    if (btn) {
      const icon = btn.querySelector(".sidebar-toggle-icon") || btn;
      icon.textContent = collapsed ? "›" : "‹";
      const title = isNarrow()
        ? collapsed
          ? "Show markets"
          : "Close markets"
        : collapsed
          ? "Show markets panel"
          : "Hide markets panel";
      btn.title = title;
      btn.setAttribute("aria-label", title);
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
    if (backdrop) backdrop.hidden = !isNarrow() || !!collapsed;
    if (persist !== false && !isNarrow()) {
      try {
        localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0");
      } catch (_) {
        /* ignore */
      }
    }
    requestAnimationFrame(() => {
      try {
        charts?.resize?.();
        candleChart?.resize?.();
      } catch (_) {
        /* ignore */
      }
    });
  }

  function setCountdownDigits(mins, secs) {
    const timer = el("countdown-timer");
    const outcome = el("countdown-outcome");
    if (timer) timer.hidden = false;
    if (outcome) {
      outcome.hidden = true;
      outcome.textContent = "";
    }
    el("countdown").className = "countdown poly-countdown";
    if (el("countdown-mins")) el("countdown-mins").textContent = mins;
    if (el("countdown-secs")) el("countdown-secs").textContent = secs;
  }

  function setCountdownResolved(text) {
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = null;
    el("countdown-timer").hidden = true;
    const outcome = el("countdown-outcome");
    outcome.hidden = false;
    outcome.textContent = text;
    el("countdown").className = "countdown poly-countdown resolved";
  }

  function clearCountdown() {
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = null;
    setCountdownDigits("—", "—");
  }

  function setWsStatus(state) {
    const badge = el("ws-status");
    if (!badge) return;

    if (state === "connected") {
      wsIsLive = true;
      clearTimeout(wsStatusTimer);
      badge.textContent = "Live";
      badge.className = "ws-badge ws-on";
      return;
    }

    wsIsLive = false;
    clearTimeout(wsStatusTimer);
    wsStatusTimer = setTimeout(() => {
      if (wsIsLive) return;
      badge.textContent = state === "disconnected" ? "Reconnecting…" : "Connecting…";
      badge.className = "ws-badge ws-off";
    }, 1500);
  }

  function scheduleRenderTicker(oracle) {
    if (oracle) lastOraclePrices = { ...lastOraclePrices, ...oracle };
    if (tickerRaf) return;
    tickerRaf = requestAnimationFrame(() => {
      tickerRaf = 0;
      renderTicker();
    });
  }

  function scheduleNavBadges() {
    if (navRaf) return;
    navRaf = requestAnimationFrame(() => {
      navRaf = 0;
      updateNavBadges();
    });
  }
  function scheduleUpdateMetrics(m) {
    if (!m) return;
    pendingMetricsMarket = m;
    if (metricsRaf) return;
    metricsRaf = requestAnimationFrame(() => {
      metricsRaf = 0;
      const pending = pendingMetricsMarket;
      pendingMetricsMarket = null;
      if (pending) updateMetrics(pending);
    });
  }

  function isStaleClockMarket(m, interval = selected.interval) {
    if (!m?.start_ts) return false;
    const clock = PM.clockBounds(interval || m.interval);
    return !!(clock && m.start_ts < clock.start);
  }

  function liveAssetPrices(prev) {
    if (!prev) return {};
    const out = {};
    if (prev.oracle_price != null) out.oracle_price = prev.oracle_price;
    if (prev.twap_oracle != null) out.twap_oracle = prev.twap_oracle;
    return out;
  }

  function freshClockMarket(asset, interval, bounds, prev) {
    const clock = bounds || PM.clockBounds(interval);
    return {
      asset,
      interval,
      slug: PM.clockSlug(asset, interval),
      question: prev?.question || "",
      start_ts: clock.start,
      end_ts: clock.end,
      yes_mid: 0,
      no_mid: 0,
      yes_ask: 0,
      no_ask: 0,
      combined_ask: 0,
      strike_price: null,
      opening_oracle_price: null,
      twap_initial: null,
      dist_from_opening: null,
      dist_from_strike: null,
      outcome: null,
      secs_remaining: Math.max(0, clock.end - Math.floor(Date.now() / 1000)),
      ...liveAssetPrices(prev),
    };
  }

  function applyMarketCard(incoming) {
    if (!incoming?.asset || !incoming?.interval) return null;
    const key = `${incoming.asset}:${incoming.interval}`;
    const clock = PM.clockBounds(incoming.interval);
    const prev = marketByKey[key];
    if (clock && incoming.start_ts && incoming.start_ts < clock.start) {
      if (!prev?.start_ts || prev.start_ts < clock.start) {
        marketByKey[key] = freshClockMarket(incoming.asset, incoming.interval, clock, incoming);
      }
      return marketByKey[key];
    }
    if (prev?.start_ts && incoming.start_ts && incoming.start_ts < prev.start_ts) {
      return prev;
    }
    if (prev && incoming.start_ts === prev.start_ts) {
      incoming = {
        ...incoming,
        twap_initial: incoming.twap_initial ?? prev.twap_initial ?? null,
        opening_oracle_price: incoming.opening_oracle_price ?? prev.opening_oracle_price ?? null,
      };
    }
    marketByKey[key] = incoming;
    return incoming;
  }

  function hasLiveQuote(m) {
    return !!(m && (m.yes_ask > 0 || m.no_ask > 0 || m.yes_mid > 0 || m.no_mid > 0));
  }

  function marketBounds(m, interval = selected.interval) {
    if (m?.start_ts && m?.end_ts) return { start: m.start_ts, end: m.end_ts };
    return PM.windowBounds(m, interval);
  }

  function pointInLiveBounds(point, bounds) {
    if (!bounds || point?.ts_ms == null) return false;
    const t = point.ts_ms / 1000;
    return t >= bounds.start && t < bounds.end + 1.5;
  }

  function resetLiveChart(bounds, market) {
    const twapInitial = market?.twap_initial ?? null;
    const openings = {
      opening: spotOpeningForMarket(market, null),
      openingBinance: null,
      openingProb: null,
      twapInitial,
    };
    applyBounds(bounds, true, openings);
    if (charts) {
      charts.setHistory(
        [],
        twapInitial,
        bounds?.start,
        bounds?.end,
        openings.opening,
        openings.openingBinance,
        openings.openingProb,
        twapInitial
      );
    }
    if (bounds?.end) startCountdown(bounds.end);
  }

  function decorateLiveRoll(seq) {
    pendingMetricsMarket = null;
    slugRefreshAt = 0;
    const select = el("slug-select");
    if (select && selectedSlug) select.value = selectedSlug;
    socket.select(selected.asset, selected.interval);
    updatePolymarketLinks(liveSlug);
    const m = marketByKey[`${selected.asset}:${selected.interval}`];
    if (m) {
      updateMetrics(m);
      applyLiveCandle(m);
      if (m.twap_initial == null || m.opening_oracle_price == null) scheduleOpeningPoll();
      else stopOpeningPoll();
    }
    updateNavBadges();
    renderTicker();
    void refreshSlugDropdown(true);
    void loadSlugHistory(liveSlug).catch(() => {});
    // The new window's stored ticks are still empty at the open (and the
    // collector is busiest at 15m rolls); re-sync once they exist so the
    // opening seconds are on the chart even if this tab missed live pushes.
    for (const delay of HISTORY_RESYNC_MS) {
      setTimeout(() => {
        if (seq !== rollSeq || !viewingLive || charts?.hasWindowHistory()) return;
        void loadSlugHistory(liveSlug).catch(() => {});
      }, delay);
    }
    void loadCandles();
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      previewTimer = 0;
      if (seq !== rollSeq) return;
      void loadRecentPreviews(true);
    }, 300);
  }

  /**
   * Replace the live lines with the window's stored 1s ticks. Used when live
   * painting missed seconds it cannot fill honestly (tab hidden or throttled,
   * slow roll); without it the gap showed as a flat line until a refresh.
   */
  function resyncLiveHistory() {
    if (!viewingLive || resyncTimer) return;
    const wait = Math.max(300, lastResyncAt + RESYNC_MIN_GAP_MS - Date.now());
    resyncTimer = setTimeout(() => {
      resyncTimer = 0;
      if (!viewingLive) return;
      lastResyncAt = Date.now();
      snapToClockWindow();
      if (liveSlug) void loadSlugHistory(liveSlug).catch(() => {});
    }, wait);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    resyncLiveHistory();
    if (previewPage === 0) void loadRecentPreviews(true, 0);
  });

  function snapToClockWindow(forceChartReset = false) {
    if (!viewingLive) return false;
    const bounds = PM.clockBounds(selected.interval);
    if (!bounds) return false;

    let marketsRolled = 0;
    for (const asset of assets) {
      const key = `${asset}:${selected.interval}`;
      const prev = marketByKey[key];
      if (prev?.start_ts === bounds.start && prev?.end_ts === bounds.end) continue;
      marketByKey[key] = freshClockMarket(asset, selected.interval, bounds, prev);
      marketsRolled += 1;
    }

    const chartNeedsReset =
      forceChartReset ||
      !(
        activeBounds &&
        activeBounds.start === bounds.start &&
        activeBounds.end === bounds.end
      );
    if (!chartNeedsReset && !marketsRolled) return false;

    const key = `${selected.asset}:${selected.interval}`;
    const slug = marketByKey[key]?.slug || PM.clockSlug(selected.asset, selected.interval);
    liveSlug = slug;
    selectedSlug = slug;
    pendingMetricsMarket = null;

    if (chartNeedsReset) {
      rollSeq += 1;
      chartOpeningPrice = null;
      resetLiveChart(bounds, marketByKey[key]);
      decorateLiveRoll(rollSeq);
    } else {
      const m = marketByKey[key];
      if (m) {
        updateMetrics(m);
        updateOracleHero(m);
        applyLiveCandle(m);
        if (m.twap_initial == null || m.opening_oracle_price == null) scheduleOpeningPoll();
        else stopOpeningPoll();
      }
      updateNavBadges();
      renderTicker();
      const select = el("slug-select");
      if (select && selectedSlug) select.value = selectedSlug;
      updatePolymarketLinks(liveSlug);
    }
    return true;
  }

  function ensureTicker() {
    const host = el("oracle-ticker");
    if (!host || host.dataset.built === "1") return;
    host.replaceChildren(
      ...assets.map((asset) => {
        const row = document.createElement("div");
        row.className = "ticker-item";
        row.dataset.asset = asset;
        const icon = document.createElement("img");
        icon.className = "ticker-icon";
        icon.src = `/static/img/coins/${asset.toLowerCase()}.svg?v=term37`;
        icon.alt = asset;
        icon.width = 16;
        icon.height = 16;
        const price = document.createElement("span");
        price.className = "ticker-price";
        price.textContent = "—";
        const diff = document.createElement("span");
        diff.className = "ticker-diff";
        row.append(icon, price, diff);
        return row;
      })
    );
    host.dataset.built = "1";
  }

  function renderTicker() {
    const host = el("oracle-ticker");
    if (!host) return;
    ensureTicker();
    for (const asset of assets) {
      const row = host.querySelector(`[data-asset="${asset}"]`);
      if (!row) continue;
      const price = lastOraclePrices[asset];
      const m = marketByKey[`${asset}:5m`];
      const opening = m?.twap_initial ?? null;
      const currentTwap = m?.twap_oracle ?? null;
      const current = price ?? m?.oracle_price ?? null;
      const diff = PM.distInitialMinusTwap(opening, currentTwap);
      const delta = PM.fmtPriceDelta(diff);
      const priceEl = row.querySelector(".ticker-price");
      const diffEl = row.querySelector(".ticker-diff");
      if (priceEl) priceEl.textContent = current != null ? PM.fmtUsd(current) : "—";
      if (diffEl) {
        diffEl.textContent = diff != null ? delta.text : "";
        diffEl.className = `ticker-diff${delta.cls ? ` ${delta.cls}` : ""}`;
      }
    }
  }

  function showNotice(text) {
    const node = el("form-notice");
    if (!node) return;
    clearTimeout(noticeTimer);
    if (!text) {
      node.hidden = true;
      node.textContent = "";
      return;
    }
    node.hidden = false;
    node.textContent = text;
    noticeTimer = setTimeout(() => {
      node.hidden = true;
    }, 4200);
  }

  function fmtQuotePair(m) {
    if (!hasLiveQuote(m)) return "—";
    return `${PM.fmtAsk(m.yes_ask, m.yes_mid)} / ${PM.fmtAsk(m.no_ask, m.no_mid)}`;
  }

  function currentViewSlug() {
    if (selectedSlug) return selectedSlug;
    if (viewingLive && liveSlug) return liveSlug;
    const m = marketByKey[`${selected.asset}:${selected.interval}`];
    return m?.slug || "";
  }

  function updatePolymarketLinks(slug = currentViewSlug()) {
    const resolved = String(slug || currentViewSlug() || "").trim();
    const url = PM.polymarketEventUrl(resolved);
    ["poly-link-oracle", "poly-link-prob"].forEach((id) => {
      const link = el(id);
      if (!link) return;
      if (url) {
        link.href = url;
        link.dataset.slug = resolved;
        link.title = `Open ${resolved} on Polymarket`;
        link.setAttribute("aria-disabled", "false");
        link.classList.remove("is-disabled");
        link.hidden = false;
      } else {
        link.href = "#";
        link.dataset.slug = "";
        link.title = "No market selected";
        link.setAttribute("aria-disabled", "true");
        link.classList.add("is-disabled");
        link.hidden = false;
      }
    });
  }

  function openDisplayedPolymarket(ev) {
    if (ev) ev.preventDefault();
    const slug = currentViewSlug();
    const url = PM.polymarketEventUrl(slug);
    if (!url) {
      showNotice("No market slug to open yet");
      return false;
    }
    updatePolymarketLinks(slug);
    window.open(url, "_blank", "noopener,noreferrer");
    return false;
  }

  function updateChartTitle() {
    const title = el("oracle-chart-title");
    if (title) {
      title.textContent = `${selected.asset} Price Chart`;
    }
    const candles = el("candle-chart-title");
    if (candles) candles.textContent = `${selected.interval} candles`;
  }

  function updateNavBadges() {
    document.querySelectorAll(".nav-btn").forEach((btn) => {
      const key = `${btn.dataset.asset}:${btn.dataset.interval}`;
      const m = marketByKey[key];
      const badge = btn.querySelector(".badge");
      if (badge) badge.textContent = hasLiveQuote(m) ? PM.fmtProb(m.yes_mid) : "—";
      btn.classList.remove("win", "lose", "idle");
      btn.classList.add(PM.positionClass(m));
      btn.classList.toggle(
        "active",
        selected.asset === btn.dataset.asset && selected.interval === btn.dataset.interval
      );
    });
  }

  function renderNav() {
    const nav = el("market-nav");
    const expectedCount = assets.length * intervals.length;
    const needsBuild =
      !nav.querySelector(".nav-btn") ||
      nav.querySelectorAll(".nav-btn").length !== expectedCount;
    if (needsBuild) {
      nav.innerHTML = assets
        .map((asset) => {
          const buttons = intervals
            .map((interval) => {
              const key = `${asset}:${interval}`;
              const m = marketByKey[key];
              const cls = PM.positionClass(m);
              const active =
                selected.asset === asset && selected.interval === interval ? "active" : "";
              const badge = hasLiveQuote(m) ? PM.fmtProb(m.yes_mid) : "—";
              return `<button class="nav-btn ${cls} ${active}" data-asset="${asset}" data-interval="${interval}">
                <span>${interval}</span><span class="badge">${badge}</span>
              </button>`;
            })
            .join("");
          return `<div class="asset-group"><div class="asset-label"><img class="asset-label-icon" src="/static/img/coins/${asset.toLowerCase()}.svg?v=term37" alt="" width="14" height="14">${asset}</div>${buttons}</div>`;
        })
        .join("");

      nav.querySelectorAll(".nav-btn").forEach((btn) => {
        btn.addEventListener("click", () => switchMarket(btn.dataset.asset, btn.dataset.interval));
      });
    } else {
      updateNavBadges();
    }
  }

  async function switchMarket(asset, interval) {
    const switched = selected.asset !== asset || selected.interval !== interval;
    selected = { asset, interval };
    viewingLive = true;
    slugRefreshAt = 0;
    if (switched) {
      activeBounds = null;
      chartOpeningPrice = null;
    }
    updateLiveButton();
    updateTwapLabels();

    el("sel-title").textContent = `${asset} Up or Down · ${interval}`;
    updateChartTitle();
    if (isNarrow()) applySidebarCollapsed(true, false);
    snapToClockWindow(switched);
    socket.select(asset, interval);
    const m = marketByKey[`${asset}:${interval}`];
    if (!activeBounds) {
      if (m?.slug) {
        liveSlug = m.slug;
        selectedSlug = liveSlug;
        const bounds = PM.windowBounds(m, interval);
        const openings = chartOpenings([], bounds?.start, m);
        applyBounds(bounds, true, openings);
        if (charts) {
          charts.setHistory(
            [],
            openings.twapInitial ?? m.twap_initial ?? null,
            bounds?.start,
            bounds?.end,
            openings.opening,
            openings.openingBinance,
            openings.openingProb,
            openings.twapInitial ?? m.twap_initial ?? null
          );
        }
        updateMetrics(m);
        startCountdown(m.end_ts);
      } else {
        liveSlug = "";
        clearCountdown();
        applyBounds(null, true);
        if (charts) charts.setHistory([], null, 0, 0, null);
      }
    }
    updateNavBadges();
    const history = viewingLive && liveSlug
      ? loadSlugHistory(liveSlug).catch(() => {})
      : Promise.resolve();
    await Promise.all([refreshSlugDropdown(false), history]);
    updatePolymarketLinks();
    void loadRecentPreviews(true);
    void loadCandles();
  }

  function boundsFromWindow(w) {
    if (w?.start_ts && w?.end_ts) return { start: w.start_ts, end: w.end_ts };
    return PM.windowBounds({ slug: w?.slug }, selected.interval);
  }

  function spotOpeningForMarket(m, windowMeta) {
    return (
      m?.opening_oracle_price ??
      windowMeta?.opening_oracle_price ??
      m?.strike_price ??
      windowMeta?.strike_price ??
      null
    );
  }

  function chartOpenings(points, startTs, m, windowMeta) {
    const fromPoints =
      points?.length && startTs
        ? {
            oracle: PM.openingFromPoints(points, startTs),
            binance: PM.openingBinanceFromPoints(points, startTs),
            prob: PM.openingProbFromPoints(points, startTs),
            twap: PM.twapInitialFromPoints(points, startTs),
          }
        : {};
    return {
      // Spot open seeds Chainlink line / Target.
      opening: spotOpeningForMarket(m, windowMeta) ?? fromPoints.oracle ?? null,
      openingBinance: fromPoints.binance ?? null,
      openingProb: fromPoints.prob ?? null,
      // Chainlink TWAP at open seeds TWAP line + Initial Price.
      twapInitial:
        twapInitialPrice(m, windowMeta) ?? fromPoints.twap ?? chartOpeningPrice ?? null,
    };
  }

  function applyBounds(bounds, force = false, openings = null) {
    if (
      !force &&
      bounds &&
      activeBounds &&
      activeBounds.start === bounds.start &&
      activeBounds.end === bounds.end
    ) {
      return;
    }
    activeBounds = bounds;
    if (bounds && charts) {
      charts.setWindowBounds(
        bounds.start,
        bounds.end,
        openings?.opening ?? null,
        openings?.openingBinance ?? null,
        openings?.openingProb ?? null,
        openings?.twapInitial ?? null
      );
    }
    el("window-range").textContent = bounds
      ? PM.fmtWindowRange(bounds.start, bounds.end)
      : "—";
  }

  function setChartHistory(points, strikePrice, bounds, m, windowMeta) {
    const openings = chartOpenings(points, bounds?.start, m, windowMeta);
    chartOpeningPrice = openings.twapInitial ?? null;
    // Target = TWAP initial (fall back to explicit strikePrice only if TWAP missing).
    const target = openings.twapInitial ?? strikePrice ?? null;
    applyBounds(bounds, false, openings);
    if (charts) {
      charts.setHistory(
        points,
        target,
        bounds?.start,
        bounds?.end,
        openings.opening,
        openings.openingBinance,
        openings.openingProb,
        openings.twapInitial
      );
    }
    return openings;
  }

  function stopOpeningPoll() {
    if (openingPollTimer) {
      clearInterval(openingPollTimer);
      openingPollTimer = null;
    }
  }

  function scheduleOpeningPoll() {
    stopOpeningPoll();
    openingPollTimer = setInterval(async () => {
      if (!viewingLive) {
        stopOpeningPoll();
        return;
      }
      const key = `${selected.asset}:${selected.interval}`;
      const m = marketByKey[key];
      if (!m?.slug) return;
      if (m.twap_initial != null && m.opening_oracle_price != null) {
        stopOpeningPoll();
        return;
      }
      try {
        const res = await fetch("/api/markets");
        if (!res.ok) return;
        const markets = await res.json();
        const fresh = (markets || []).find(
          (row) => row.asset === selected.asset && row.interval === selected.interval
        );
        if (!fresh) return;
        if (isStaleClockMarket(fresh, selected.interval)) return;
        const adopted = applyMarketCard(fresh) || marketByKey[key];
        updateMetrics(adopted);
        renderTicker();
        if (adopted?.twap_initial != null && adopted?.opening_oracle_price != null) stopOpeningPoll();
      } catch {
        /* retry */
      }
    }, 3000);
  }

  function handleWindowRoll(market) {
    if (!viewingLive || !market?.slug) return;
    const clock = PM.clockBounds(selected.interval);
    if (clock && market.start_ts && market.start_ts < clock.start) return;
    const bounds = marketBounds(market, selected.interval) || clock;
    if (activeBounds && bounds && activeBounds.start === bounds.start) {
      applyMarketCard(market);
      liveSlug = market.slug;
      selectedSlug = market.slug;
      updateMetrics(market);
      applyLiveCandle(market);
      updateNavBadges();
      renderTicker();
      updatePolymarketLinks(market.slug);
      if (market.end_ts) startCountdown(market.end_ts);
      if (market.twap_initial == null || market.opening_oracle_price == null) scheduleOpeningPoll();
      else stopOpeningPoll();
      return;
    }
    rollSeq += 1;
    liveSlug = market.slug;
    selectedSlug = market.slug;
    applyMarketCard(market);
    chartOpeningPrice = null;
    resetLiveChart(bounds, market);
    decorateLiveRoll(rollSeq);
  }

  async function refreshSlugDropdown(force = false) {
    const key = `${selected.asset}:${selected.interval}`;
    const now = Date.now();
    if (!force && key === slugCacheKey && now - slugRefreshAt < SLUG_CACHE_MS) {
      return;
    }

    const m = marketByKey[key];
    liveSlug = m?.slug || "";

    try {
      const res = await fetch(
        `/api/windows?asset=${selected.asset}&interval=${selected.interval}&limit=40`
      );
      const data = await res.json();
      slugWindows = Array.isArray(data) ? data : data.items || [];
      slugCacheKey = key;
      slugRefreshAt = now;
    } catch {
      if (!slugWindows.length) slugWindows = [];
    }

    const select = el("slug-select");
    const seen = new Set();
    const options = [];

    if (liveSlug) {
      options.push({ slug: liveSlug, label: `${liveSlug} (live)`, live: true });
      seen.add(liveSlug);
    }
    for (const w of slugWindows) {
      if (!w.slug || seen.has(w.slug)) continue;
      seen.add(w.slug);
      const end = new Date(w.end_ts * 1000).toLocaleString();
      const tag = w.outcome ? ` [${w.outcome}]` : "";
      options.push({ slug: w.slug, label: `${w.slug}${tag} · ${end}`, live: false });
    }

    select.innerHTML = options
      .map((o) => `<option value="${o.slug}">${o.label}</option>`)
      .join("");

    if (!options.length) {
      select.innerHTML = `<option value="">No windows</option>`;
      selectedSlug = "";
      return;
    }

    if (viewingLive && liveSlug) {
      selectedSlug = liveSlug;
    } else if (selectedSlug && seen.has(selectedSlug)) {
      // keep selection
    } else {
      selectedSlug = liveSlug || options[0].slug;
      viewingLive = selectedSlug === liveSlug;
    }
    select.value = selectedSlug;
    const input = el("slug-input");
    if (input && selectedSlug) input.value = selectedSlug;
    updateLiveButton();
  }

  function setChartOpening(points, startTs) {
    // Initial Price = TWAP initial only (never Chainlink spot).
    chartOpeningPrice = PM.twapInitialFromPoints(points, startTs);
  }

  function twapInitialPrice(m, windowMeta) {
    return m?.twap_initial ?? windowMeta?.twap_initial ?? null;
  }

  function syncChartTwapTarget(opening) {
    if (opening == null || !(opening > 0)) return;
    if (typeof charts?.setTwapInitial === "function") charts.setTwapInitial(opening);
  }

  function updateOracleHero(m, windowMeta) {
    if (!m && !windowMeta) return;
    if (viewingLive && isStaleClockMarket(m, m?.interval || selected.interval)) return;
    const opening = twapInitialPrice(m, windowMeta);
    const oracle = m?.oracle_price;
    const twap =
      m?.twap_oracle ??
      (typeof charts?.getTwap === "function" ? charts.getTwap() : null) ??
      null;
    // Difference = current TWAP − initial TWAP.
    const diff = PM.distInitialMinusTwap(opening, twap);

    el("hero-initial").textContent = opening != null ? PM.fmtUsdPrecise(opening) : "—";
    el("hero-current").textContent = oracle != null ? PM.fmtUsd(oracle) : "—";
    el("hero-twap").textContent = twap != null ? PM.fmtUsdPrecise(twap) : "—";
    syncChartTwapTarget(opening);

    const delta = PM.fmtPriceDelta(diff);
    const diffEl = el("hero-diff");
    diffEl.textContent = diff != null ? delta.text : "—";
    diffEl.className = `price-delta ${diff != null ? delta.cls : ""}`;
  }

  function setCandleLast(bar) {
    const node = el("candle-last");
    if (!node || !bar) return;
    const up = bar.close >= bar.open;
    node.textContent = up ? "UP" : "DOWN";
    node.classList.toggle("is-up", up);
    node.classList.toggle("is-down", !up);
  }

  function applyLiveCandle(market) {
    if (!candleChart || !market) return;
    const start = market.start_ts;
    if (start == null) return;
    if (liveCandle && liveCandle.time !== start) liveCandle = null;
    const open = market.twap_initial ?? market.opening_oracle_price ?? market.strike_price;
    const close = market.oracle_price ?? market.twap_oracle;
    if (open == null || close == null) return;
    if (!liveCandle || liveCandle.time !== start) {
      liveCandle = {
        time: start,
        open,
        high: Math.max(open, close),
        low: Math.min(open, close),
        close,
        slug: market.slug,
      };
    } else {
      liveCandle.open = open;
      liveCandle.close = close;
      liveCandle.high = Math.max(liveCandle.high, close, open);
      liveCandle.low = Math.min(liveCandle.low, close, open);
      if (market.slug) liveCandle.slug = market.slug;
    }
    candleChart.updateLive(liveCandle);
    setCandleLast(liveCandle);
  }

  async function loadCandles() {
    const token = ++candleLoadToken;
    const title = el("candle-chart-title");
    if (title) title.textContent = `${selected.interval} candles`;
    try {
      const res = await fetch(
        `/api/candles/${encodeURIComponent(selected.asset)}/${encodeURIComponent(selected.interval)}?limit=180`
      );
      if (!res.ok || token !== candleLoadToken) return;
      const data = await res.json();
      if (token !== candleLoadToken) return;
      const last = candleChart?.setCandles(data.items || []);
      const live = (data.items || []).find((row) => row.live) || last;
      if (live) {
        liveCandle = {
          time: live.time,
          open: live.open,
          high: live.high,
          low: live.low,
          close: live.close,
          slug: live.slug,
        };
        setCandleLast(live);
      }
    } catch (err) {
      console.error("candles", err);
    }
  }

  async function openCandleWindow(meta) {
    if (!meta?.slug) return;
    viewingLive = meta.slug === liveSlug;
    selectedSlug = meta.slug;
    updateLiveButton();
    ensureSlugOption(meta.slug, viewingLive ? `${meta.slug} (live)` : meta.slug);
    const select = el("slug-select");
    if (select) select.value = meta.slug;
    const input = el("slug-input");
    if (input) input.value = meta.slug;
    try {
      await loadSlugHistory(meta.slug);
    } catch (err) {
      console.error("candle select", err);
    }
  }

  function startCountdown(endTs) {
    countdownEndTs = endTs || 0;
    if (countdownTimer) clearInterval(countdownTimer);
    const tick = () => {
      if (viewingLive) {
        snapToClockWindow();
        const clock = PM.clockBounds(selected.interval);
        if (clock?.end) countdownEndTs = clock.end;
      }
      const endTsNow = countdownEndTs || activeBounds?.end || 0;
      if (!endTsNow) {
        setCountdownDigits("—", "—");
        return;
      }
      const secs = Math.max(0, endTsNow - Math.floor(Date.now() / 1000));
      const parts = PM.fmtCountdown(secs);
      setCountdownDigits(parts.mins, parts.secs);
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  }

  async function loadSlugHistory(slug) {
    if (!slug) return;
    const res = await fetch(`/api/windows/${encodeURIComponent(slug)}/ticks`);
    if (!res.ok) {
      throw new Error(res.status === 404 ? "Market not found" : `Failed to load (${res.status})`);
    }
    const data = await res.json();
    const bounds = boundsFromWindow(data.window);
    if (viewingLive) {
      const clock = PM.clockBounds(selected.interval);
      if (clock && bounds?.start && bounds.start !== clock.start) return data;
    }
    applyBounds(bounds);
    const points = PM.filterPointsForWindow(data.ticks, bounds?.start, bounds?.end);
    if (!points.length && viewingLive && charts?.hasWindowHistory()) return data;
    setChartOpening(points, bounds?.start);
    const last = data.ticks.at(-1);
    const liveM = marketByKey[`${selected.asset}:${selected.interval}`];
    // The live card's target only applies to the live slug; a past window
    // uses its own stored open TWAP.
    const liveTwapInitial = liveM?.slug === slug ? liveM.twap_initial : null;
    const twapInitial =
      liveTwapInitial ??
      data.window.twap_initial ??
      PM.twapInitialFromPoints(points, bounds?.start);
    const marketMeta = { twap_initial: twapInitial };
    if (points.length || !charts?.hasWindowHistory()) {
      setChartHistory(points, twapInitial, bounds, marketMeta, data.window);
    }
    updateOracleHero(
      {
        opening_oracle_price: data.window.opening_oracle_price,
        oracle_price: last?.oracle_price,
        strike_price: data.window.strike_price,
        twap_oracle: last?.twap_oracle,
        twap_initial: twapInitial,
      },
      data.window
    );
    if (!viewingLive && data.window.outcome) {
      setCountdownResolved(data.window.outcome);
    } else if (viewingLive) {
      startCountdown(data.window.end_ts);
    }
    updatePolymarketLinks(slug);
    return data;
  }

  function parseMarketSlug(raw) {
    const slug = String(raw || "").trim().toLowerCase();
    const match = slug.match(/^(btc|eth|sol)-updown-(5m|15m)-(\d+)$/);
    if (!match) return null;
    return {
      slug,
      asset: match[1].toUpperCase(),
      interval: match[2],
      startTs: Number(match[3]),
    };
  }

  function ensureSlugOption(slug, label = slug) {
    const select = el("slug-select");
    if (!select || !slug) return;
    const exists = [...select.options].some((o) => o.value === slug);
    if (!exists) {
      const opt = document.createElement("option");
      opt.value = slug;
      opt.textContent = label;
      select.appendChild(opt);
    }
    select.value = slug;
  }

  function updateLiveButton() {
    const btn = el("slug-live-btn");
    if (!btn) return;
    btn.classList.toggle("is-live", !!viewingLive);
    btn.title = viewingLive ? "Viewing live market" : "Jump back to live market";
  }

  async function goLive() {
    const btn = el("slug-live-btn");
    const m = marketByKey[`${selected.asset}:${selected.interval}`];
    viewingLive = true;
    snapToClockWindow();
    const slug = liveSlug || PM.clockSlug(selected.asset, selected.interval) || m?.slug;
    if (!slug) {
      showNotice("No live market available yet");
      return;
    }

    if (btn) btn.disabled = true;
    try {
      liveSlug = slug;
      selectedSlug = slug;
      ensureSlugOption(slug, `${slug} (live)`);
      const input = el("slug-input");
      if (input) input.value = slug;
      updateLiveButton();
      socket.select(selected.asset, selected.interval);
      await loadSlugHistory(slug);
      updateMetrics(m);
      const clock = PM.clockBounds(selected.interval);
      startCountdown(m?.end_ts && m.start_ts === clock?.start ? m.end_ts : clock?.end);
      document.querySelectorAll(".preview-card").forEach((c) => {
        c.classList.toggle("active", c.dataset.slug === slug);
      });
    } catch (err) {
      console.error("go live", err);
      showNotice(err?.message || "Failed to load live market");
    } finally {
      if (btn) btn.disabled = false;
      updateLiveButton();
    }
  }

  async function loadSlugFromInput() {
    const input = el("slug-input");
    const btn = el("slug-load-btn");
    const parsed = parseMarketSlug(input?.value);
    if (!parsed) {
      showNotice("Enter a slug like btc-updown-5m-1784264400");
      input?.focus();
      return;
    }
    showNotice("");

    if (btn) btn.disabled = true;
    try {
      if (parsed.asset !== selected.asset || parsed.interval !== selected.interval) {
        selected = { asset: parsed.asset, interval: parsed.interval };
        el("sel-title").textContent = `${parsed.asset} Up or Down · ${parsed.interval}`;
        updateChartTitle();
        renderNav();
        void loadRecentPreviews(true);
        socket.select(parsed.asset, parsed.interval);
      }

      const m = marketByKey[`${parsed.asset}:${parsed.interval}`];
      liveSlug = m?.slug || liveSlug;
      selectedSlug = parsed.slug;
      viewingLive = selectedSlug === liveSlug && !!liveSlug;
      ensureSlugOption(parsed.slug, viewingLive ? `${parsed.slug} (live)` : parsed.slug);
      if (input) input.value = parsed.slug;
      updateLiveButton();

      await loadSlugHistory(parsed.slug);
      if (viewingLive) updateMetrics(marketByKey[`${selected.asset}:${selected.interval}`]);
      document.querySelectorAll(".preview-card").forEach((c) => {
        c.classList.toggle("active", c.dataset.slug === parsed.slug);
      });
    } catch (err) {
      console.error("slug load", err);
      showNotice(err?.message || "Failed to load market");
    } finally {
      if (btn) btn.disabled = false;
      updateLiveButton();
    }
  }

  function destroyPreviews() {
    previewCharts.forEach((c) => {
      try {
        c.destroy();
      } catch {
        /* ignore */
      }
    });
    previewCharts = [];
  }

  function fmtPreviewTime(endTs) {
    if (!endTs) return "—";
    return new Date(endTs * 1000).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function previewOutcomeMeta(outcome) {
    if (!outcome) return { text: "OPEN", cls: "open" };
    const up = String(outcome).toUpperCase() === "UP" || String(outcome).toUpperCase() === "YES";
    const down = String(outcome).toUpperCase() === "DOWN" || String(outcome).toUpperCase() === "NO";
    if (up) return { text: "UP", cls: "up" };
    if (down) return { text: "DOWN", cls: "down" };
    return { text: String(outcome).toUpperCase(), cls: "open" };
  }

  function updatePreviewPager() {
    const prevBtn = el("preview-prev");
    const nextBtn = el("preview-next");
    const label = el("preview-page-label");
    // Prev = older markets, Next = newer (toward live).
    if (prevBtn) prevBtn.disabled = !previewHasOlder;
    if (nextBtn) nextBtn.disabled = !previewHasNewer;
    if (label) label.textContent = String(previewPage + 1);
  }

  /**
   * Same cards as on screen (periodic refresh inside one window): update ticks,
   * badge and count in place instead of rebuilding every chart. Returns false
   * when the card list changed and needs a full rebuild.
   */
  function refreshPreviewsInPlace(payloads) {
    const list = el("recent-previews");
    const cardEls = list ? [...list.querySelectorAll(".preview-card")] : [];
    if (!payloads.length || cardEls.length !== payloads.length) return false;
    if (previewCharts.length !== payloads.length) return false;
    const same = payloads.every(
      (p, i) => (p.window?.slug || "") === cardEls[i].dataset.slug && previewCharts[i].slug === cardEls[i].dataset.slug
    );
    if (!same) return false;
    payloads.forEach((payload, i) => {
      const w = payload.window || {};
      const card = cardEls[i];
      const isLiveWindow = !!(w.slug && liveSlug && w.slug === liveSlug);
      const outcome = isLiveWindow ? { text: "LIVE", cls: "live" } : previewOutcomeMeta(w.outcome);
      card.classList.toggle("win", outcome.cls === "up");
      card.classList.toggle("lose", outcome.cls === "down");
      card.classList.toggle("is-live-window", isLiveWindow);
      card.classList.toggle("active", !!(w.slug && w.slug === selectedSlug));
      const meta = card.querySelector(".preview-card-meta");
      if (meta) {
        meta.className = `preview-card-meta ${outcome.cls}`;
        meta.textContent = outcome.text;
      }
      const count = card.querySelector(".preview-card-foot span");
      if (count) {
        count.textContent = `${payload.tick_count != null ? payload.tick_count : (payload.ticks || []).length} ticks`;
      }
      previewCharts[i].setData(payload.ticks || []);
    });
    return true;
  }

  // The live card is fetched at the roll, when its window has no ticks yet, and
  // the previous window is still unresolved: refresh the newest page while the
  // tab is visible so the live preview grows and the last outcome appears.
  setInterval(() => {
    if (document.hidden || previewPage !== 0 || !previewCharts.length) return;
    void loadRecentPreviews(true, 0);
  }, PREVIEW_REFRESH_MS);

  async function loadRecentPreviews(force = false, page = null) {
    const key = `${selected.asset}:${selected.interval}`;
    const sub = el("preview-subtitle");
    if (sub) sub.textContent = `${selected.asset} · ${selected.interval}`;

    if (page != null) {
      previewPage = Math.max(0, page);
    } else if (previewKey !== key || force) {
      // Asset change / window roll / refresh → back to newest previous page.
      previewPage = 0;
    }

    if (!force && previewKey === key && previewCharts.length && page == null) {
      updatePreviewPager();
      return;
    }

    const token = ++previewLoadToken;
    previewKey = key;
    const list = el("recent-previews");
    if (!list) return;
    const hadCards = !!list.querySelector(".preview-card");
    if (!hadCards) list.innerHTML = `<div class="preview-empty">Loading previews…</div>`;
    updatePreviewPager();

    try {
      const qs = new URLSearchParams({
        limit: String(PREVIEW_COUNT),
        page: String(previewPage),
        skip_current: "false",
      });
      const res = await fetch(
        `/api/previews/${encodeURIComponent(selected.asset)}/${encodeURIComponent(selected.interval)}?${qs}`
      );
      if (!res.ok) throw new Error(`preview ${res.status}`);
      const data = await res.json();
      if (token !== previewLoadToken) return;

      // Support both new `{items}` payload and legacy array responses.
      const payloads = Array.isArray(data) ? data : data.items || [];
      previewHasOlder = Array.isArray(data) ? false : !!data.has_older;
      previewHasNewer = Array.isArray(data) ? previewPage > 0 : !!data.has_newer;
      if (typeof data.page === "number") previewPage = data.page;
      updatePreviewPager();

      if (!payloads.length) {
        destroyPreviews();
        list.innerHTML = `<div class="preview-empty">${
          previewPage > 0 ? "No older markets" : "No previous markets"
        }</div>`;
        return;
      }

      const nowSec = Math.floor(Date.now() / 1000);
      // Prefetched next window has no ticks yet — keep it off the sidebar.
      const visible = payloads.filter((p) => !(p.window?.start_ts > nowSec));
      if (refreshPreviewsInPlace(visible)) return;

      const frag = document.createDocumentFragment();
      const cards = [];
      visible.forEach((payload) => {
        const w = payload.window || {};
        const isLiveWindow = !!(w.slug && liveSlug && w.slug === liveSlug);
        const outcome = isLiveWindow
          ? { text: "LIVE", cls: "live" }
          : previewOutcomeMeta(w.outcome);
        const isActive = w.slug && w.slug === selectedSlug;
        const card = document.createElement("button");
        card.type = "button";
        card.className = `preview-card ${outcome.cls === "up" ? "win" : outcome.cls === "down" ? "lose" : ""} ${isLiveWindow ? "is-live-window" : ""} ${isActive ? "active" : ""}`;
        card.dataset.slug = w.slug || "";
        card.innerHTML = `
          <div class="preview-card-head">
            <span class="preview-card-time">${fmtPreviewTime(w.end_ts)}</span>
            <span class="preview-card-meta ${outcome.cls}">${outcome.text}</span>
          </div>
          <div class="preview-chart"></div>
          <div class="preview-card-foot">
            <span>${payload.tick_count != null ? payload.tick_count : (payload.ticks || []).length} ticks</span>
            <span class="preview-slug">${(w.slug || "").replace(/^.*?-updown-/, "")}</span>
          </div>
        `;
        card.addEventListener("click", async () => {
          if (!w.slug) return;
          const select = el("slug-select");
          selectedSlug = w.slug;
          viewingLive = selectedSlug === liveSlug && !!liveSlug;
          updateLiveButton();
          if (select) {
            const exists = [...select.options].some((o) => o.value === w.slug);
            if (!exists) {
              const opt = document.createElement("option");
              opt.value = w.slug;
              opt.textContent = w.slug;
              select.appendChild(opt);
            }
            select.value = w.slug;
          }
          const input = el("slug-input");
          if (input) input.value = w.slug;
          document.querySelectorAll(".preview-card").forEach((c) => c.classList.remove("active"));
          card.classList.add("active");
          await loadSlugHistory(w.slug);
          if (viewingLive) updateMetrics(marketByKey[`${selected.asset}:${selected.interval}`]);
        });
        frag.appendChild(card);
        cards.push({ card, ticks: payload.ticks || [], slug: w.slug || "" });
      });
      if (!cards.length) {
        destroyPreviews();
        list.innerHTML = `<div class="preview-empty">${
          previewPage > 0 ? "No older markets" : "No previous markets"
        }</div>`;
        return;
      }
      destroyPreviews();
      list.replaceChildren(frag);
      for (const item of cards) {
        const chartEl = item.card.querySelector(".preview-chart");
        const mini = new PM.MiniPreviewChart(chartEl);
        mini.setData(item.ticks);
        mini.slug = item.slug;
        previewCharts.push(mini);
      }
    } catch (err) {
      if (token !== previewLoadToken) return;
      destroyPreviews();
      list.innerHTML = `<div class="preview-empty">Failed to load previews</div>`;
      previewHasOlder = false;
      previewHasNewer = previewPage > 0;
      updatePreviewPager();
      console.error("preview load", err);
    }
  }

  el("preview-prev")?.addEventListener("click", () => {
    if (!previewHasOlder) return;
    void loadRecentPreviews(true, previewPage + 1);
  });
  el("preview-next")?.addEventListener("click", () => {
    if (!previewHasNewer) return;
    void loadRecentPreviews(true, previewPage - 1);
  });

  el("sidebar-toggle")?.addEventListener("click", () => {
    const layout = el("app-layout");
    const collapsed = !layout?.classList.contains("sidebar-collapsed");
    applySidebarCollapsed(collapsed);
  });
  el("sidebar-backdrop")?.addEventListener("click", () => applySidebarCollapsed(true, false));
  function syncSidebarForViewport() {
    if (isNarrow()) {
      applySidebarCollapsed(true, false);
    } else {
      let stored = false;
      try {
        stored = localStorage.getItem(SIDEBAR_KEY) === "1";
      } catch (_) {
        stored = false;
      }
      applySidebarCollapsed(stored);
    }
  }
  syncSidebarForViewport();
  if (typeof NARROW_MQ.addEventListener === "function") {
    NARROW_MQ.addEventListener("change", syncSidebarForViewport);
  } else if (typeof NARROW_MQ.addListener === "function") {
    NARROW_MQ.addListener(syncSidebarForViewport);
  }
  updateTwapLabels();

  async function bootstrapFromRest() {
    try {
      const res = await fetch("/api/markets");
      if (!res.ok) return;
      const markets = await res.json();
      if (!markets?.length) return;
      marketByKey = {};
      for (const row of markets) applyMarketCard(row);
      renderNav();
      updateSelectedHeader();
      if (viewingLive) {
        snapToClockWindow();
        const clock = PM.clockBounds(selected.interval);
        const m = marketByKey[`${selected.asset}:${selected.interval}`];
        if (m?.slug && m.start_ts && clock && m.start_ts === clock.start) {
          liveSlug = m.slug;
          selectedSlug = liveSlug;
          updateMetrics(m);
          startCountdown(m.end_ts);
          updatePolymarketLinks(liveSlug);
          void loadSlugHistory(liveSlug);
        } else if (clock) {
          startCountdown(clock.end);
        }
      }
      void loadRecentPreviews(true);
    } catch (err) {
      console.error("bootstrap", err);
    }
  }

  function updateMetrics(m) {
    if (!m) return;
    if (viewingLive && isStaleClockMarket(m, m.interval || selected.interval)) return;
    const prob = el("prob-last");
    if (prob) prob.textContent = fmtQuotePair(m);
    updateOracleHero(m);
    PM.renderEdgeStrip(m, leadLag[selected.asset]);
  }

  function applyLeadLag(msg) {
    if (!msg?.lead_lag) return;
    leadLag = msg.lead_lag;
    const node = el("lead-lag");
    if (node) node.textContent = PM.fmtLeadLag(leadLag[selected.asset]);
  }

  function updateSelectedHeader() {
    el("sel-title").textContent = `${selected.asset} Up or Down · ${selected.interval}`;
    if (viewingLive) {
      snapToClockWindow();
      const clock = PM.clockBounds(selected.interval);
      const m = marketByKey[`${selected.asset}:${selected.interval}`];
      if (m?.slug && m.start_ts && clock && m.start_ts === clock.start) {
        liveSlug = m.slug;
        selectedSlug = liveSlug;
        updateMetrics(m);
        startCountdown(m.end_ts || clock.end);
      } else if (clock) {
        startCountdown(clock.end);
      }
    }
    refreshSlugDropdown(false);
    updateChartTitle();
    updatePolymarketLinks();
    void loadRecentPreviews(false);
  }

  el("slug-select").addEventListener("change", async (e) => {
    selectedSlug = e.target.value;
    viewingLive = selectedSlug === liveSlug && !!liveSlug;
    updateLiveButton();
    const input = el("slug-input");
    if (input) input.value = selectedSlug || "";
    await loadSlugHistory(selectedSlug);
    if (viewingLive) {
      const m = marketByKey[`${selected.asset}:${selected.interval}`];
      updateMetrics(m);
    }
  });

  el("slug-load-btn")?.addEventListener("click", () => {
    void loadSlugFromInput();
  });
  el("slug-live-btn")?.addEventListener("click", () => {
    void goLive();
  });
  el("slug-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void loadSlugFromInput();
    }
  });
  ["poly-link-oracle", "poly-link-prob"].forEach((id) => {
    el(id)?.addEventListener("click", openDisplayedPolymarket);
  });
  updateLiveButton();
  updatePolymarketLinks();

  const socket = new PM.LiveSocket("/ws/live", {
    onStatus: setWsStatus,
    onAlert: (msg) => alerts.handle(msg),
    onOracle: (msg) => {
      applyLeadLag(msg);
      scheduleRenderTicker(msg.oracle_prices);
      if (!viewingLive || selectedSlug !== liveSlug || !charts) return;
      snapToClockWindow();
      const liveTwap = PM.pickTwapPrice(msg, selected.asset, selected.interval);
      charts.updateFeedPrices({
        ts_ms: msg.ts_ms,
        oracle_price: msg.oracle_prices?.[selected.asset],
        binance_price: msg.binance_prices?.[selected.asset],
        twap_oracle: liveTwap,
      });
      const m = marketByKey[`${selected.asset}:${selected.interval}`];
      if (m) {
        if (msg.oracle_prices?.[selected.asset] != null) {
          m.oracle_price = msg.oracle_prices[selected.asset];
        }
        if (liveTwap != null) {
          m.twap_oracle = liveTwap;
        }
        // Never seed Initial Price from live TWAP — only true window-open TWAP.
        if (
          msg.twap_initial != null &&
          m.twap_initial == null &&
          !isStaleClockMarket(m, selected.interval)
        ) {
          m.twap_initial = msg.twap_initial;
        }
      }
      updateOracleHero(m);
    },
    onSnapshot: (msg) => {
      applyLeadLag(msg);
      for (const row of msg.markets || []) applyMarketCard(row);
      scheduleRenderTicker(msg.oracle_prices);
      renderNav();
      updateSelectedHeader();
    },
    onHistory: async (msg) => {
      if (msg.asset !== selected.asset || msg.interval !== selected.interval) return;
      if (!viewingLive) return;
      try {
        const m = marketByKey[`${selected.asset}:${selected.interval}`];
        const bounds =
          msg.start_ts && msg.end_ts
            ? { start: msg.start_ts, end: msg.end_ts }
            : marketBounds(m, selected.interval);
        const clock = PM.clockBounds(selected.interval);
        if (clock && bounds?.start && bounds.start !== clock.start) return;

        let points = PM.filterPointsForWindow(msg.points, bounds?.start, bounds?.end);
        if (!points.length && msg.slug) {
          if (!charts?.hasWindowHistory()) await loadSlugHistory(msg.slug);
          return;
        }

        if (charts?.hasWindowHistory()) {
          if (bounds) {
            applyBounds(bounds, false, chartOpenings(points, bounds?.start, m, {
              strike_price: msg.strike_price,
              opening_oracle_price: msg.opening_oracle_price,
            }));
          }
          updateOracleHero(m, {
            strike_price: msg.strike_price,
            opening_oracle_price: msg.opening_oracle_price,
          });
          if (!msg.opening_oracle_price && m?.opening_oracle_price == null) scheduleOpeningPoll();
          else stopOpeningPoll();
          return;
        }

        setChartOpening(points, bounds?.start);
        setChartHistory(points, m?.twap_initial ?? null, bounds, m, {
          strike_price: msg.strike_price,
          opening_oracle_price: msg.opening_oracle_price,
          twap_initial: m?.twap_initial,
        });
        updateOracleHero(m, {
          strike_price: msg.strike_price,
          opening_oracle_price: msg.opening_oracle_price,
          twap_initial: m?.twap_initial,
        });
        if (!msg.opening_oracle_price && m?.opening_oracle_price == null) scheduleOpeningPoll();
        else stopOpeningPoll();
      } catch (err) {
        console.error("history handler", err);
      }
    },
    onTick: (msg) => {
      const key = `${msg.asset}:${msg.interval}`;
      if (msg.asset === selected.asset && msg.interval === selected.interval) {
        snapToClockWindow();
        const clock = PM.clockBounds(selected.interval);
        if (msg.market?.start_ts && clock && msg.market.start_ts < clock.start) {
          scheduleRenderTicker();
          scheduleNavBadges();
          return;
        }
        const adopted = msg.market ? applyMarketCard(msg.market) : marketByKey[key];
        if (adopted) applyLiveCandle(adopted);
        if (adopted?.slug) {
          const slugChanged = liveSlug && liveSlug !== adopted.slug;
          liveSlug = adopted.slug;
          if (viewingLive) selectedSlug = liveSlug;
          if (slugChanged) handleWindowRoll(adopted);
          else updatePolymarketLinks(liveSlug);
        }
        if (viewingLive && selectedSlug === liveSlug) {
          const bounds = activeBounds || marketBounds(adopted, selected.interval);
          if (msg.point && charts && pointInLiveBounds(msg.point, bounds)) {
            charts.appendProbPoint(msg.point);
            if (
              msg.point.oracle_price != null ||
              msg.point.binance_price != null ||
              msg.point.twap_oracle != null
            ) {
              charts.updateFeedPrices({
                ts_ms: msg.point.ts_ms,
                oracle_price: msg.point.oracle_price,
                binance_price: msg.point.binance_price,
                twap_oracle: msg.point.twap_oracle,
              });
            }
          }
          scheduleUpdateMetrics(adopted);
          if (adopted?.end_ts && (!clock || adopted.end_ts >= clock.end)) {
            countdownEndTs = adopted.end_ts;
          }
          if (msg.point) {
            const prob = el("prob-last");
            if (prob) prob.textContent = fmtQuotePair(adopted);
          }
        }
      } else if (msg.market) {
        applyMarketCard(msg.market);
      }
      scheduleRenderTicker();
      scheduleNavBadges();
    },
  });

  socket._selected = selected;
  // Connect + REST first so the UI works even if chart init fails.
  void bootstrapFromRest();
  socket.connect();
  setInterval(() => {
    if (!wsIsLive) void bootstrapFromRest();
  }, 3000);

  try {
    if (typeof LightweightCharts === "undefined") {
      throw new Error("LightweightCharts failed to load");
    }
    charts = new PM.DualCharts(el("chart-oracle"), el("chart-prob"));
    charts.onLiveGap = resyncLiveHistory;
    const candleEl = el("chart-candles");
    if (candleEl) {
      candleChart = new PM.CandleChart(candleEl);
      candleChart.onSelect = (meta) => {
        void openCandleWindow(meta);
      };
    }
    updateTwapLabels();
    void loadCandles();
  } catch (err) {
    console.error("chart init failed", err);
    charts = null;
    const badge = el("ws-status");
    if (badge && !wsIsLive) {
      badge.textContent = "Charts offline";
      badge.className = "ws-badge ws-off";
    }
  }
})();
