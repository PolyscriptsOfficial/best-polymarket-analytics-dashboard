window.PM = window.PM || {};

PM.DualCharts = class {
  constructor(oracleEl, probEl) {
    const baseOpts = {
      layout: { background: { color: "#141c2b" }, textColor: "#8b97ad" },
      grid: { vertLines: { color: "#1e2a40" }, horzLines: { color: "#1e2a40" } },
      rightPriceScale: { borderColor: "#243049" },
      timeScale: { borderColor: "#243049", timeVisible: true, secondsVisible: true },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    };

    this.oracleChart = LightweightCharts.createChart(oracleEl, {
      ...baseOpts,
      height: oracleEl.clientHeight || 280,
    });
    this.probChart = LightweightCharts.createChart(probEl, {
      ...baseOpts,
      height: probEl.clientHeight || 220,
      rightPriceScale: { borderColor: "#243049", scaleMargins: { top: 0.1, bottom: 0.1 } },
    });

    this.oracleSeries = this.oracleChart.addLineSeries({
      color: "#f59e0b",
      lineWidth: 2,
      title: "Oracle",
    });
    this.yesSeries = this.probChart.addLineSeries({
      color: "#26a69a",
      lineWidth: 2,
      title: "YES",
    });
    this.noSeries = this.probChart.addLineSeries({
      color: "#ef5350",
      lineWidth: 2,
      title: "NO",
    });

    this._strikeLine = null;
    this._resizeObserver = new ResizeObserver(() => this.resize());
    this._resizeObserver.observe(oracleEl);
    this._resizeObserver.observe(probEl);
  }

  resize() {
    const o = document.getElementById("chart-oracle");
    const p = document.getElementById("chart-prob");
    if (o) this.oracleChart.applyOptions({ width: o.clientWidth, height: o.clientHeight });
    if (p) this.probChart.applyOptions({ width: p.clientWidth, height: p.clientHeight });
  }

  _toTime(tsMs) {
    return Math.floor(tsMs / 1000);
  }

  setHistory(points, strikePrice) {
    const oracle = [];
    const yes = [];
    const no = [];
    if (!points || !points.length) {
      this.oracleSeries.setData([]);
      this.yesSeries.setData([]);
      this.noSeries.setData([]);
      this._setStrike(strikePrice);
      return;
    }
    for (const p of points) {
      const t = this._toTime(p.ts_ms);
      if (p.oracle_price != null) oracle.push({ time: t, value: p.oracle_price });
      yes.push({ time: t, value: p.yes_mid * 100 });
      no.push({ time: t, value: p.no_mid * 100 });
    }
    this.oracleSeries.setData(oracle);
    this.yesSeries.setData(yes);
    this.noSeries.setData(no);
    this._setStrike(strikePrice);
    this.oracleChart.timeScale().fitContent();
    this.probChart.timeScale().fitContent();
  }

  appendPoint(point) {
    const t = this._toTime(point.ts_ms);
    if (point.oracle_price != null) {
      this.oracleSeries.update({ time: t, value: point.oracle_price });
    }
    this.yesSeries.update({ time: t, value: point.yes_mid * 100 });
    this.noSeries.update({ time: t, value: point.no_mid * 100 });
  }

  _setStrike(strike) {
    if (this._strikeLine) {
      this.oracleSeries.removePriceLine(this._strikeLine);
      this._strikeLine = null;
    }
    if (strike != null) {
      this._strikeLine = this.oracleSeries.createPriceLine({
        price: strike,
        color: "#ff9800",
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: "Strike",
      });
    }
  }

  destroy() {
    this._resizeObserver.disconnect();
    this.oracleChart.remove();
    this.probChart.remove();
  }
};
