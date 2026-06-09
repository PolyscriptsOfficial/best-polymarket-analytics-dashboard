window.PM = window.PM || {};

PM.LiveSocket = class {
  constructor(url, handlers = {}) {
    this.url = url;
    this.handlers = handlers;
    this.ws = null;
    this._retry = 1000;
    this._shouldRun = true;
    this._selected = { asset: "BTC", interval: "5m" };
  }

  select(asset, interval) {
    this._selected = { asset, interval };
    this._send({ type: "select", asset, interval });
  }

  _send(payload) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload));
    }
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${location.host}${this.url}`);
    this.ws.onopen = () => {
      this._retry = 1000;
      this.handlers.onStatus?.("connected");
      this.select(this._selected.asset, this._selected.interval);
    };
    this.ws.onclose = () => {
      this.handlers.onStatus?.("disconnected");
      if (this._shouldRun) setTimeout(() => this.connect(), this._retry);
      this._retry = Math.min(this._retry * 2, 10000);
    };
    this.ws.onerror = () => this.handlers.onStatus?.("error");
    this.ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "snapshot") this.handlers.onSnapshot?.(msg);
        else if (msg.type === "history") this.handlers.onHistory?.(msg);
        else if (msg.type === "tick") this.handlers.onTick?.(msg);
      } catch (e) {
        console.error("ws parse", e);
      }
    };
  }

  close() {
    this._shouldRun = false;
    this.ws?.close();
  }
};
