const fs = require("fs");
const path = require("path");

// Linux VPS: project venv. Windows: venv if present, else system Python
// (scripts/_env.ps1 runs without a venv).
function pythonPath() {
  if (process.platform !== "win32") return ".venv/bin/python";
  const venv = path.join(__dirname, ".venv", "Scripts", "python.exe");
  return fs.existsSync(venv) ? venv : "python";
}

module.exports = {
  apps: [
    {
      name: "pmanalysis",
      cwd: __dirname,
      script: pythonPath(),
      args: "-m pmanalysis start",
      interpreter: "none",
      env: {
        PM_HOST: "0.0.0.0",
        PM_PORT: "8420",
        // Small-VPS throttles: the 62 Hz quote-push / 2 Hz REST book-poll
        // defaults pinned the CPU and starved the CLOB WebSocket reader
        // ("slow consumer"), and unthrottled httpx INFO logging grew the PM2
        // error log into the multi-GB range. See pmanalysis/config.py for
        // what each knob does.
        PM_QUOTE_PUSH_SEC: "0.25",
        PM_BOOK_POLL_SEC: "3",
        PM_CLOB_STALE_SEC: "8",
        PM_LIVE_PUSH_MS: "150",
        PM_TICK_RETENTION_HOURS: "24",
        PM_FEED_RECORD: "0",
      },
      autorestart: true,
      max_restarts: 20,
      min_uptime: "5s",
      time: true,
      // Restart cleanly before the kernel OOM-kills the process (1.9 GiB
      // host, no headroom for an unbounded RSS climb).
      max_memory_restart: "700M",
    },
  ],
};
