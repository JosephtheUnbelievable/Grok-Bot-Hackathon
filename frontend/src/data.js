import healthFixture from "../../fixtures/health.json";
import iceObservedFixture from "../../fixtures/ice_observed.json";
import iceForecastFixture from "../../fixtures/ice_forecast.json";
import iceGapFixture from "../../fixtures/ice_gap.json";
import extentFixture from "../../fixtures/extent_north.json";
import impactFixture from "../../fixtures/impact.json";
import backtestFixture from "../../fixtures/backtest.json";
import { isAfter, isGap, parseThrough } from "./format.js";

export const USE_FIXTURES = import.meta.env.VITE_USE_FIXTURES !== "false";
export const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

const FIXTURE_NOTICE = healthFixture.fixture_notice;

function clone(value) {
  return structuredClone(value);
}

export function gibsUrl(through) {
  const year = through?.year || 2024;
  const month = String(through?.month || 9).padStart(2, "0");
  const time = `${year}-${month}-15`;
  return {
    time,
    url: `https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=MODIS_Terra_CorrectedReflectance_TrueColor&FORMAT=image/jpeg&WIDTH=1024&HEIGHT=512&CRS=EPSG:4326&BBOX=-90,-180,90,180&TIME=${time}`,
  };
}

export function decodeValues(b64) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return bytes;
}

export function paintIce(ice) {
  const canvas = document.createElement("canvas");
  canvas.width = 1024;
  canvas.height = 512;
  if (!ice || ice.gap_masked || ice.extent_km2 == null) return canvas;
  const width = ice.grid?.width || 0;
  const height = ice.grid?.height || 0;
  let bytes;
  try {
    bytes = decodeValues(ice.values_b64 || "");
  } catch {
    return canvas;
  }
  if (!width || !height || bytes.length < width * height) return canvas;

  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(1024, 512);
  const data = img.data;
  const cx = (width - 1) / 2;
  const cy = (height - 1) / 2;
  const south = ice.pole === "south";
  const reach = 34;

  for (let y = 0; y < 512; y++) {
    const lat = 90 - (y / 511) * 180;
    const colat = south ? 90 + lat : 90 - lat;
    if (colat < 0 || colat > reach) continue;
    const radius = (colat / reach) * 3.15;
    for (let x = 0; x < 1024; x++) {
      const lon = -180 + (x / 1023) * 360;
      const theta = (lon * Math.PI) / 180;
      const ix = Math.round(cx + Math.sin(theta) * radius);
      const iy = Math.round(cy - Math.cos(theta) * radius);
      if (ix < 0 || iy < 0 || ix >= width || iy >= height) continue;
      const v = bytes[iy * width + ix];
      if (v === 255 || v < 15) continue;
      const t = Math.min(1, (v - 15) / 85);
      const o = (y * 1024 + x) * 4;
      data[o] = Math.round(93 + (255 - 93) * t);
      data[o + 1] = Math.round(173 + (255 - 173) * t);
      data[o + 2] = Math.round(226 + (255 - 226) * t);
      data[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

function metaFromIce(ice) {
  return {
    pole: ice.pole,
    year: ice.year,
    month: ice.month,
    isForecast: ice.is_forecast === true,
    stale: ice.stale === true,
    gapMasked: ice.gap_masked === true,
    extentKm2: ice.extent_km2 == null ? null : Number(ice.extent_km2),
    source: ice.source || "",
    regressor: ice.regressor || "",
    fixture: ice.fixture === true,
    fixtureNotice: ice.fixture_notice || "",
    observedThrough: ice.observed_through || null,
    blend: ice.blend,
  };
}

export function fixtureIce(pole, year, month) {
  if (isGap(year, month)) {
    const ice = clone(iceGapFixture);
    ice.pole = pole;
    ice.year = year;
    ice.month = month;
    ice.gap_masked = true;
    ice.extent_km2 = null;
    ice.area_km2 = null;
    ice.is_forecast = false;
    return ice;
  }
  const forecast = isAfter(year, month, healthFixture.observed_through);
  const ice = clone(forecast ? iceForecastFixture : iceObservedFixture);
  ice.pole = pole;
  ice.year = year;
  ice.month = month;
  ice.is_forecast = forecast;
  if (!forecast) {
    ice.blend = null;
    ice.scenario = "ssp245";
  }
  return ice;
}

export function fixtureExtent(pole, month) {
  const data = clone(extentFixture);
  data.pole = pole;
  data.month = month;
  if (month === 12) {
    for (const pt of data.series) {
      if (pt.year === 1987) {
        pt.extent_km2 = null;
        pt.low_km2 = null;
        pt.high_km2 = null;
        pt.gap_masked = true;
      }
    }
    data.client_note = "December. 1987 is an instrument gap, left empty. Other points are the September fixture sample.";
  } else if (month === 1) {
    data.series.push({
      year: 1988,
      extent_km2: null,
      low_km2: null,
      high_km2: null,
      is_forecast: false,
      gap_masked: true,
      source: "nsidc-g02135",
      extent_method: "nsidc_monthly_csv",
    });
    data.series.sort((a, b) => a.year - b.year);
    data.client_note = "January. 1988 is an instrument gap, left empty. Other points are the September fixture sample.";
  } else if (month !== 9 || pole !== "north") {
    data.client_note = "Fixture series is the northern September sample. It does not change length with the slider.";
  }
  return data;
}

function fail(err) {
  if (err?.name === "AbortError") {
    const abort = new Error("aborted");
    abort.name = "AbortError";
    throw abort;
  }
  const error = new Error(err?.message || "feed down");
  error.feedDown = err?.feedDown !== false && (err?.status == null || err.status === 503 || err.status >= 500);
  error.status = err?.status;
  error.detail = err?.detail;
  if (err?.status === 400) error.feedDown = false;
  throw error;
}

async function getJson(url, signal) {
  let res;
  try {
    res = await fetch(url, { signal });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    const error = new Error("feed down");
    error.feedDown = true;
    throw error;
  }
  let body = null;
  const text = await res.text();
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const error = new Error(body?.error?.message || `HTTP ${res.status}`);
    error.status = res.status;
    error.feedDown = res.status === 503 || res.status >= 500;
    error.detail = body?.error || null;
    throw error;
  }
  return body;
}

function blendParam(blend) {
  return Number(blend).toFixed(1);
}

export async function loadHealth(signal) {
  if (USE_FIXTURES) return clone(healthFixture);
  return getJson(`${API_BASE}/health`, signal);
}

export async function loadIce(state, signal) {
  if (USE_FIXTURES) {
    const ice = fixtureIce(state.pole, state.year, state.month);
    return { meta: metaFromIce(ice), canvas: paintIce(ice) };
  }
  const forecast = isAfter(state.year, state.month, state.observedThrough);
  const params = new URLSearchParams();
  params.set("format", "png");
  params.set("band", state.band);
  if (forecast) params.set("blend", blendParam(state.blend));
  const url = `${API_BASE}/ice/${state.pole}/${state.year}/${state.month}?${params}`;
  let res;
  try {
    res = await fetch(url, { signal });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    const error = new Error("feed down");
    error.feedDown = true;
    throw error;
  }
  const type = res.headers.get("content-type") || "";
  if (!res.ok || type.includes("json")) {
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    const error = new Error(body?.error?.message || `HTTP ${res.status}`);
    error.status = res.status;
    error.feedDown = res.status === 503 || res.status >= 500;
    throw error;
  }
  const blob = await res.blob();
  const headers = res.headers;
  const rawExtent = headers.get("X-Ice-Extent-Km2");
  const extent = rawExtent == null || rawExtent.trim() === "" ? null : Number(rawExtent);
  return {
    meta: {
      pole: headers.get("X-Ice-Pole") || state.pole,
      year: Number(headers.get("X-Ice-Year") || state.year),
      month: Number(headers.get("X-Ice-Month") || state.month),
      isForecast: headers.get("X-Ice-Is-Forecast") === "true",
      stale: headers.get("X-Ice-Stale") === "true",
      gapMasked: headers.get("X-Ice-Gap-Masked") === "true",
      extentKm2: extent == null || Number.isNaN(extent) ? null : extent,
      source: headers.get("X-Ice-Source") || "",
      regressor: headers.get("X-Ice-Regressor") || "",
      fixture: headers.get("X-Ice-Fixture") === "true",
      fixtureNotice: headers.get("X-Ice-Fixture") === "true" ? FIXTURE_NOTICE : "",
      observedThrough: parseThrough(headers.get("X-Ice-Observed-Through")),
      blend: headers.get("X-Ice-Blend") || null,
    },
    imageUrl: URL.createObjectURL(blob),
  };
}

export async function loadExtent(state, signal) {
  if (USE_FIXTURES) return fixtureExtent(state.pole, state.month);
  const params = new URLSearchParams();
  params.set("month", String(state.month));
  params.set("blend", blendParam(state.blend));
  return getJson(`${API_BASE}/extent/${state.pole}?${params}`, signal);
}

export async function loadImpact(state, signal) {
  if (USE_FIXTURES) return clone(impactFixture);
  const params = new URLSearchParams();
  params.set("year", String(state.year));
  params.set("blend", blendParam(state.blend));
  return getJson(`${API_BASE}/impact?${params}`, signal);
}

export async function loadBacktest(signal) {
  if (USE_FIXTURES) return clone(backtestFixture);
  return getJson(`${API_BASE}/backtest`, signal);
}

export function noticeFrom(...docs) {
  for (const doc of docs) {
    if (doc?.fixture && doc.fixture_notice) return doc.fixture_notice;
    if (doc?.fixtureNotice) return doc.fixtureNotice;
  }
  if (USE_FIXTURES) return FIXTURE_NOTICE;
  return "";
}

void fail;
