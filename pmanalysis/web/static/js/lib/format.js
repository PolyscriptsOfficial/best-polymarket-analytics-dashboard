window.PM = window.PM || {};

PM.fmtUsd = (v) =>
  v != null ? "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—";

PM.fmtProb = (v) =>
  v != null && !Number.isNaN(v) ? (Number(v) * 100).toFixed(1) + "¢" : "—";

PM.fmtSecs = (s) => {
  if (!s || s <= 0) return "0:00";
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
};

PM.marketMap = (markets) => {
  const map = {};
  (markets || []).forEach((m) => {
    map[`${m.asset}:${m.interval}`] = m;
  });
  return map;
};

PM.positionClass = (m) => {
  if (!m || m.dist_from_strike == null) return "idle";
  return m.dist_from_strike >= 0 ? "win" : "lose";
};
