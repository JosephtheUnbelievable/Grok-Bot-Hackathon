import { renderChart } from "./chart.js";
import {
  API_BASE,
  USE_FIXTURES,
  gibsUrl,
  loadBacktest,
  loadExtent,
  loadHealth,
  loadIce,
  loadImpact,
  noticeFrom,
} from "./data.js";
import {
  FEED_LABELS,
  MONTHS,
  formatIndex,
  formatMm,
  formatOpening,
  isAfter,
  isGap,
  millionKm2,
  regressorCaption,
  scenarioLabel,
  seaLevelCaption,
  sourceLabel,
} from "./format.js";
import { createGlobe } from "./globe.js";

const state = {
  pole: "north",
  year: 2024,
  month: 9,
  blend: 2,
  band: "median",
  observedThrough: USE_FIXTURES ? { year: 2024, month: 9 } : null,
  yearTouched: false,
  monthTouched: false,
  health: null,
  iceMeta: null,
  extent: null,
  impact: null,
  backtest: null,
  feedDown: false,
  queryError: "",
  basemapNote: "Photograph loading.",
  basemapTime: "2024-09-15",
};

const $ = (id) => document.getElementById(id);
const yearInput = $("year");
const monthInput = $("month");
const blendInput = $("blend");

let globe = null;
try {
  globe = createGlobe($("globe"));
} catch (err) {
  $("globe").textContent = "The globe could not start.";
  console.error(err);
}

$("controls").addEventListener("submit", (event) => event.preventDefault());

function setDrawer(open) {
  $("drawer").hidden = !open;
  $("drawer-btn").setAttribute("aria-expanded", open ? "true" : "false");
  $("app").classList.toggle("drawer-open", open);
}

function bindToggle(button, panel, { focus = false } = {}) {
  const close = () => {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
  };
  const open = () => {
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    if (focus) panel.querySelector("input, button")?.focus();
  };
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (panel.hidden) open();
    else close();
  });
  return { open, close };
}

const optionsUi = bindToggle($("options-btn"), $("options-pop"), { focus: true });
const healthUi = bindToggle($("health-btn"), $("health-pop"));
$("drawer-btn").addEventListener("click", () => setDrawer($("drawer").hidden));
$("drawer-close").addEventListener("click", () => {
  setDrawer(false);
  $("drawer-btn").focus();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("options-pop").hidden) {
    optionsUi.close();
    $("options-btn").focus();
    return;
  }
  if (!$("health-pop").hidden) {
    healthUi.close();
    $("health-btn").focus();
    return;
  }
  if (!$("drawer").hidden) {
    setDrawer(false);
    $("drawer-btn").focus();
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".menu")) optionsUi.close();
  if (!event.target.closest(".health-wrap")) healthUi.close();
});
if (location.hash === "#impacts") setDrawer(true);

yearInput.addEventListener("input", () => {
  state.year = Number(yearInput.value);
  state.yearTouched = true;
  paintInstant();
  schedule();
});
monthInput.addEventListener("input", () => {
  state.month = Number(monthInput.value);
  state.monthTouched = true;
  paintInstant();
  schedule();
});
blendInput.addEventListener("input", () => {
  state.blend = Number(blendInput.value);
  paintInstant();
  schedule();
});
for (const input of document.querySelectorAll('input[name="pole"]')) {
  input.addEventListener("change", () => {
    if (!input.checked) return;
    state.pole = input.value;
    state.month = state.pole === "south" ? 2 : 9;
    state.monthTouched = false;
    monthInput.value = String(state.month);
    paintInstant();
    schedule();
    if (state.pole === "south") globe?.lookSouth();
    else globe?.lookNorth();
  });
}
for (const input of document.querySelectorAll('input[name="band"]')) {
  input.addEventListener("change", () => {
    if (!input.checked) return;
    state.band = input.value;
    paintInstant();
    schedule();
  });
}

let timer = 0;
let token = 0;
let inflight = null;

function schedule() {
  window.clearTimeout(timer);
  timer = window.setTimeout(refresh, 40);
}

function modeOf() {
  if (isGap(state.year, state.month)) return { text: "Instrument gap", kind: "gap" };
  if (state.observedThrough) {
    return isAfter(state.year, state.month, state.observedThrough)
      ? { text: "Forecast", kind: "forecast" }
      : { text: "Observed", kind: "observed" };
  }
  if (state.feedDown) return { text: "Feed down", kind: "down" };
  return { text: "—", kind: "" };
}

function fill(input) {
  const min = Number(input.min);
  const max = Number(input.max);
  const pct = ((Number(input.value) - min) / (max - min)) * 100;
  input.style.setProperty("--pct", `${pct}%`);
}

function paintInstant() {
  const mode = modeOf();
  const blend = Number(state.blend).toFixed(1);
  const monthName = MONTHS[state.month - 1];
  yearInput.value = String(state.year);
  monthInput.value = String(state.month);
  blendInput.value = String(state.blend);
  yearInput.setAttribute("aria-valuenow", String(state.year));
  yearInput.setAttribute("aria-valuetext", `${state.year}, ${mode.text}`);
  monthInput.setAttribute("aria-valuenow", String(state.month));
  monthInput.setAttribute("aria-valuetext", monthName);
  blendInput.setAttribute("aria-valuenow", blend);
  blendInput.setAttribute("aria-valuetext", `${scenarioLabel(state.blend)}, blend ${blend}`);
  $("year-out").textContent = String(state.year);
  $("month-out").textContent = monthName;
  $("blend-out").textContent = `blend ${blend}`;
  const shortMonth = monthName.slice(0, 3);
  const poleName = state.pole === "north" ? "North" : "South";
  const bandName = state.band === "low" ? "Low" : state.band === "high" ? "High" : "Median";
  $("options-btn").textContent = `${shortMonth} · ${poleName} · ${bandName}`;
  fill(yearInput);
  fill(monthInput);
  fill(blendInput);

  const through = state.observedThrough;
  const record = $("record-label");
  const tick = $("today-tick");
  if (through) {
    record.textContent = `record ${MONTHS[through.month - 1].slice(0, 3)} ${through.year}`;
    tick.hidden = false;
    tick.style.left = `${((through.year - 1979) / (2100 - 1979)) * 100}%`;
    tick.title = `Observed through ${MONTHS[through.month - 1]} ${through.year}`;
  } else {
    record.textContent = "";
    tick.hidden = true;
  }

  const pole = state.pole === "north" ? "North" : "South";
  $("when").textContent = `${monthName} ${state.year} · ${pole}`;
  $("mode-pill").textContent = mode.text;
  $("mode-pill").dataset.kind = mode.kind;

  if (through && !isAfter(state.year, state.month, through)) {
    $("scenario-help").textContent = isGap(state.year, state.month)
      ? `blend ${blend}. The instrument gap has no ice to blend.`
      : `blend ${blend}. Applies after ${MONTHS[through.month - 1]} ${through.year}. This month stays on the satellite record.`;
  } else if (through) {
    $("scenario-help").textContent = `Sending blend ${blend}.`;
  } else {
    $("scenario-help").textContent = `blend ${blend}. Sent with forecast years.`;
  }

  const modelNote = $("model-note");
  modelNote.hidden = state.health?.model_ready !== false;

  if (isGap(state.year, state.month)) globe?.clearIce();

  const extent = extentReadout();
  $("extent-value").textContent = extent.value;
  $("extent-unit").textContent = extent.unit;
  $("globe").setAttribute(
    "aria-label",
    `Globe for ${monthName} ${state.year}, ${pole}, ${mode.text}. Drag to rotate. Both poles are reachable.`,
  );
}

function extentReadout() {
  if (isGap(state.year, state.month) || state.iceMeta?.gapMasked) {
    return { value: "—", unit: "instrument gap" };
  }
  if (state.iceMeta && (state.iceMeta.extentKm2 === null || Number.isNaN(state.iceMeta.extentKm2))) {
    return { value: "—", unit: "no extent" };
  }
  if (state.iceMeta) {
    return { value: millionKm2(state.iceMeta.extentKm2), unit: "million km²" };
  }
  if (state.feedDown) return { value: "—", unit: "feed down" };
  return { value: "—", unit: "million km²" };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function stat(label, value, extraClass) {
  const row = el("div", extraClass ? `stat ${extraClass}` : "stat");
  row.append(el("span", null, label), el("b", null, value));
  return row;
}

function renderPanel() {
  const body = $("panel-body");
  const down = $("panel-down");
  if (state.feedDown) {
    body.hidden = true;
    down.hidden = false;
    return;
  }
  body.hidden = false;
  down.hidden = true;
  body.replaceChildren();

  body.append(el("h2", null, "Impacts"));
  const blend = Number(state.blend).toFixed(1);
  body.append(el("p", "sub", `Year ${state.year} · blend ${blend} · ${scenarioLabel(state.blend)}`));
  if (USE_FIXTURES || state.impact?.fixture) {
    const sampleYear = state.impact?.year ?? "the fixture";
    body.append(el(
      "p",
      "sub",
      `Fixture sample for ${sampleYear}. Requested year ${state.year}, blend ${blend}.`,
    ));
  }
  if (state.queryError) body.append(el("p", "warn", state.queryError));

  const impact = state.impact;
  if (!impact) {
    body.append(el("p", "sub", "No impact response."));
  } else {
    const ship = el("div", "block");
    ship.append(el("h3", null, "Shipping season"));
    const nsr = impact.shipping?.northern_sea_route || {};
    const nwp = impact.shipping?.northwest_passage || {};
    ship.append(stat("Northern Sea Route", days(nsr.open_season_days)));
    ship.append(stat("Opening", formatOpening(nsr.opening_date)));
    ship.append(stat("Northwest Passage", days(nwp.open_season_days)));
    ship.append(stat("Opening", formatOpening(nwp.opening_date)));
    ship.append(el("p", "proxy", "Proxy, not a route solver."));
    body.append(ship);

    const communities = impact.communities || {};
    const winter = el("div", "block");
    winter.append(el("h3", null, "Winter ice"));
    const iceFree = communities.ice_free_september_year;
    winter.append(stat(
      "First ice-free September",
      iceFree == null ? "None" : String(iceFree),
    ));
    winter.append(stat(
      "Winter sea-ice loss",
      formatIndex(communities.winter_ice_loss_index) || "—",
    ));
    body.append(winter);

    const sea = impact.sea_level || {};
    const level = el("div", "block");
    level.append(el("h3", null, "Sea level"));
    level.append(stat("Greenland", formatMm(sea.greenland_mm)));
    level.append(stat("Antarctica", formatMm(sea.antarctica_mm)));
    level.append(stat("Total", formatMm(sea.total_mm), "total"));
    const low = sea.low_mm;
    const high = sea.high_mm;
    const band = low == null || high == null
      ? "—"
      : `${Math.round(Number(low))}–${Math.round(Number(high))} mm`;
    level.append(stat("Low–high total", band));
    const seaCaption = seaLevelCaption(sea.source);
    if (seaCaption) level.append(el("p", "caption", seaCaption));
    body.append(level);

    const regressor = regressorCaption(impact.regressor || state.health?.regressor || state.iceMeta?.regressor);
    if (regressor) body.append(el("p", "caption", regressor));

    const details = document.createElement("details");
    details.className = "def";
    const summary = document.createElement("summary");
    summary.textContent = "Definitions";
    details.append(summary);
    addDef(details, "Northern Sea Route", nsr.definition);
    addDef(details, "Northwest Passage", nwp.definition);
    addDef(details, "First ice-free September", communities.definitions?.ice_free_september_year);
    addDef(details, "Winter sea-ice loss", communities.definitions?.winter_ice_loss_index);
    if (sea.baseline) addDef(details, "Sea level", sea.baseline);
    body.append(details);
  }

  if (state.backtest?.trained) {
    const north = state.backtest.by_pole?.north;
    const south = state.backtest.by_pole?.south;
    const line = el("p", "backtest");
    line.textContent = `Trained through ${state.backtest.train_end_year}. September RMSE ${fmtRmse(north?.rmse_km2)} million km² north, ${fmtRmse(south?.rmse_km2)} south. Skill ${fmtSkill(north?.skill_vs_climatology)} and ${fmtSkill(south?.skill_vs_climatology)}.`;
    body.append(line);
  }

  if (state.health) {
    const feeds = el("div", "block");
    feeds.append(el("h3", null, `Feeds · ${state.health.status || "—"}`));
    const grid = el("div", "feeds");
    for (const [id, feed] of Object.entries(state.health.feeds || {})) {
      const row = el("div", "feed");
      row.append(el("span", "feed-name", FEED_LABELS[id] || id));
      const status = el("span", `feed-status status-${feed.status || "down"}`, feed.status || "down");
      row.append(status);
      if (feed.detail) row.title = feed.detail;
      grid.append(row);
    }
    feeds.append(grid);
    body.append(feeds);
  }
}

function days(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return `${Number(value)} days`;
}

function fmtRmse(km2) {
  return millionKm2(km2) ?? "—";
}

function fmtSkill(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toFixed(2);
}

function addDef(details, title, text) {
  if (!text) return;
  const p = document.createElement("p");
  const strong = document.createElement("strong");
  strong.textContent = title;
  p.append(strong, document.createTextNode(text));
  details.append(p);
}

function renderHealthPop() {
  const pop = $("health-pop");
  if (!pop) return;
  pop.replaceChildren();
  if (!state.health) {
    pop.append(el("p", "sub", state.feedDown ? "Feed down" : "No health response."));
    return;
  }
  pop.append(el("p", "sub", `Status ${state.health.status}`));
  const grid = el("div", "feeds");
  for (const [id, feed] of Object.entries(state.health.feeds || {})) {
    const row = el("div", "feed");
    row.append(el("span", "feed-name", FEED_LABELS[id] || id));
    row.append(el("span", `feed-status status-${feed.status || "down"}`, feed.status || "down"));
    if (feed.detail) row.title = feed.detail;
    grid.append(row);
  }
  pop.append(grid);
}

function renderSources() {
  const meta = state.iceMeta;
  const bits = [];
  if (meta?.source) bits.push(sourceLabel(meta.source));
  if (USE_FIXTURES && meta) {
    if (meta.gapMasked) bits.push("No concentration grid");
    else if (meta.isForecast) bits.push("Fixture forecast grid, 2050 sample");
    else bits.push("Fixture observed grid, 2024-09 sample");
  }
  if (meta?.stale) bits.push("Stale feed");
  $("source-line").textContent = bits.join(" · ");
  $("basemap-line").textContent = state.basemapNote;
  const showLegend = Boolean(meta && !meta.gapMasked && meta.extentKm2 != null);
  $("legend").hidden = !showLegend;
}

function renderBanner() {
  const notice = noticeFrom(state.health, state.impact, state.extent, state.iceMeta);
  const banner = $("banner");
  if (notice) {
    banner.hidden = false;
    banner.textContent = notice;
  } else {
    banner.hidden = true;
    banner.textContent = "";
  }
}

function render() {
  paintInstant();
  const mode = modeOf();
  const health = $("health-pill");
  const dot = $("health-dot");
  if (state.feedDown) {
    health.textContent = "Feed down";
    health.dataset.kind = "down";
    if (dot) dot.dataset.status = "down";
    setDrawer(true);
  } else if (state.health?.status) {
    health.textContent = state.health.status;
    health.dataset.kind = state.health.status === "ok" ? "observed" : state.health.status === "down" ? "down" : "forecast";
    if (dot) dot.dataset.status = state.health.status;
  } else {
    health.textContent = "—";
    health.dataset.kind = "";
    if (dot) dot.dataset.status = "down";
  }
  renderHealthPop();
  $("app").dataset.mode = state.feedDown ? "down" : mode.kind;
  renderSources();
  renderBanner();
  globe?.setRoutes(
    state.impact?.shipping?.northern_sea_route?.open_season_days,
    state.impact?.shipping?.northwest_passage?.open_season_days,
  );
  renderPanel();
  renderChart($("chart"), state.extent, state.band, state.year);
}

function rejectedDown(result) {
  return result.status === "rejected" && result.reason?.name !== "AbortError" && result.reason?.feedDown !== false;
}

async function refresh() {
  const my = ++token;
  if (inflight) inflight.abort();
  const ac = new AbortController();
  inflight = ac;
  const settled = await Promise.allSettled([
    loadHealth(ac.signal),
    loadIce(state, ac.signal),
    loadExtent(state, ac.signal),
    loadImpact(state, ac.signal),
    loadBacktest(ac.signal),
  ]);
  if (my !== token) return;
  if (settled.every((item) => item.status === "rejected" && item.reason?.name === "AbortError")) return;

  const [health, ice, extent, impact, backtest] = settled;
  state.queryError = "";
  const iceFailed = ice.status === "rejected" && ice.reason?.name !== "AbortError";
  const impactFailed = impact.status === "rejected" && impact.reason?.name !== "AbortError";
  const healthFailed = health.status === "rejected" && health.reason?.name !== "AbortError";
  state.feedDown = rejectedDown(health) || rejectedDown(ice) || rejectedDown(impact);

  if (health.status === "fulfilled") {
    state.health = health.value;
    if (health.value?.observed_through) state.observedThrough = health.value.observed_through;
    if (!state.yearTouched && state.observedThrough?.year) {
      state.year = state.observedThrough.year;
    }
  } else if (healthFailed && !state.health) {
    state.health = null;
  }

  if (ice.status === "fulfilled") {
    const value = ice.value;
    state.iceMeta = value.meta;
    if (value.meta?.observedThrough) state.observedThrough = value.meta.observedThrough;
    const hide = value.meta.gapMasked || value.meta.extentKm2 == null || isGap(state.year, state.month);
    if (hide) globe?.clearIce();
    else if (value.canvas) globe?.setIceCanvas(value.canvas);
    else if (value.imageUrl) globe?.setIceUrl(value.imageUrl);
  } else if (iceFailed) {
    if (isGap(state.year, state.month)) {
      globe?.clearIce();
      state.iceMeta = { gapMasked: true, extentKm2: null, source: "" };
    }
    if (ice.reason?.status === 400) state.queryError = ice.reason.message;
  }

  if (extent.status === "fulfilled") state.extent = extent.value;
  else if (extent.reason?.name !== "AbortError") state.extent = null;

  if (impact.status === "fulfilled") state.impact = impact.value;
  else if (impactFailed) state.impact = null;

  if (backtest.status === "fulfilled") state.backtest = backtest.value;
  else if (backtest.reason?.name !== "AbortError") state.backtest = null;

  if (!state.feedDown && (ice.reason?.status === 400 || impact.reason?.status === 400)) {
    state.queryError = ice.reason?.message || impact.reason?.message || state.queryError;
  }

  render();
}

const photo = gibsUrl(state.observedThrough);
state.basemapTime = photo.time;
globe?.setBasemap(photo.url, (ok) => {
  state.basemapNote = ok
    ? "Basemap: Blue Marble shaded relief. Not the selected month."
    : "Basemap unavailable. Dark sphere.";
  $("basemap-line").textContent = state.basemapNote;
});

paintInstant();
refresh().catch((err) => {
  console.error(err);
  state.feedDown = true;
  render();
});

void API_BASE;
