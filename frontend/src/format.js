export const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

export const SCENARIOS = [
  { at: 0, name: "SSP1-1.9" },
  { at: 1, name: "SSP1-2.6" },
  { at: 2, name: "SSP2-4.5" },
  { at: 3, name: "SSP3-7.0" },
  { at: 4, name: "SSP5-8.5" },
];

export const FEED_LABELS = {
  nsidc_daily_extent: "NSIDC daily",
  nsidc_monthly_extent: "NSIDC monthly",
  nsidc_monthly_concentration: "NSIDC concentration",
  gibs: "GIBS",
  grace_icesheet: "GRACE",
  ssp_pathways: "SSP pathways",
  natural_earth: "Natural Earth",
};

const SOURCE_LABELS = {
  "nsidc-g02135": "NSIDC Sea Ice Index",
  "nsidc-g02135-cache": "NSIDC Sea Ice Index, cached",
  "model-forecast": "Forecast model",
  "model-forecast-cache": "Forecast model, cached",
  "model-not-ready": "Model not ready",
  "grace-gracefo": "GRACE / GRACE-FO",
  "nasa-science-summary-fallback": "NASA Science summary",
};

export function sourceLabel(source) {
  if (!source) return "";
  return SOURCE_LABELS[source] || source;
}

export function isGap(year, month) {
  return (year === 1987 && month === 12) || (year === 1988 && month === 1);
}

export function isAfter(year, month, through) {
  if (!through || through.year == null || through.month == null) return false;
  if (year > through.year) return true;
  if (year < through.year) return false;
  return month > through.month;
}

export function millionKm2(km2) {
  if (km2 === null || km2 === undefined) return null;
  const n = Number(km2);
  if (!Number.isFinite(n)) return null;
  return (n / 1e6).toFixed(1);
}

export function formatMm(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return `${Math.round(Number(value))} mm`;
}

export function formatOpening(iso) {
  if (!iso) return "does not open";
  const parts = String(iso).split("-").map(Number);
  if (parts.length < 3 || parts.some((n) => !Number.isFinite(n))) return "does not open";
  const [y, m, d] = parts;
  const short = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${short[m - 1]} ${y}`;
}

export function formatIndex(fraction) {
  if (fraction === null || fraction === undefined || !Number.isFinite(Number(fraction))) return null;
  const n = Number(fraction);
  return `${n.toFixed(2)} · ${Math.round(n * 100)}%`;
}

export function scenarioLabel(blend) {
  const b = Math.round(Number(blend) * 10) / 10;
  const i = Math.max(0, Math.min(3, Math.floor(b + 1e-6)));
  const f = b - Math.floor(b + 1e-6);
  if (b >= 4 - 1e-6) return SCENARIOS[4].name;
  if (f < 0.05) return SCENARIOS[Math.round(b)].name;
  if (f > 0.95) return SCENARIOS[Math.min(4, Math.ceil(b))].name;
  return `${SCENARIOS[i].name} – ${SCENARIOS[Math.min(4, i + 1)].name}`;
}

export function extentKey(band) {
  if (band === "low") return "low_km2";
  if (band === "high") return "high_km2";
  return "extent_km2";
}

export function isMissingExtent(point, key) {
  if (!point) return true;
  if (point.gap_masked === true) return true;
  const value = point[key];
  if (value === null || value === undefined) return true;
  return !Number.isFinite(Number(value));
}

export function lineSegments(series, key) {
  const segments = [];
  let run = [];
  const flush = () => {
    if (run.length) segments.push(run);
    run = [];
  };
  for (const pt of series || []) {
    if (isMissingExtent(pt, key)) {
      flush();
      continue;
    }
    const forecast = pt.is_forecast === true;
    if (run.length && run[run.length - 1].forecast !== forecast) {
      const prev = run[run.length - 1];
      flush();
      run.push({ year: prev.year, value: prev.value, forecast });
    }
    run.push({ year: pt.year, value: Number(pt[key]), forecast });
  }
  flush();
  return segments;
}

export function seaLevelCaption(source) {
  if (source === "nasa-science-summary-fallback") {
    return "GRACE needs an Earthdata login. Rates are the NASA Science summary, −264 Gt/yr Greenland and −135 Gt/yr Antarctica, not a live mass file.";
  }
  if (source === "grace-gracefo") {
    return "GRACE / GRACE-FO mass anomaly, converted at 362 Gt per mm.";
  }
  return "";
}

export function regressorCaption(regressor) {
  if (regressor === "ipcc_ar6_warming_c_fallback") {
    return "The CO2 pathway feed is down. The forecast uses IPCC warming anchors.";
  }
  return "";
}

export function parseThrough(value) {
  if (!value) return null;
  if (typeof value === "object" && value.year != null) {
    return { year: Number(value.year), month: Number(value.month) };
  }
  const match = String(value).match(/^(\d{4})-(\d{1,2})$/);
  if (!match) return null;
  return { year: Number(match[1]), month: Number(match[2]) };
}
