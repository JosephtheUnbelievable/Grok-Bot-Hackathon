import { MONTHS, extentKey, isMissingExtent, lineSegments, millionKm2 } from "./format.js";

export function renderChart(container, extent, band, selectedYear) {
  container.replaceChildren();
  const key = extentKey(band);
  const bandName = band === "low" ? "Low" : band === "high" ? "High" : "Median";
  const monthName = MONTHS[(extent?.month || 1) - 1];
  const cap = document.createElement("p");
  cap.className = "chart-cap";
  cap.textContent = `${monthName} · ${bandName.toLowerCase()} band · million km²`;
  container.append(cap);

  if (extent?.client_note) {
    const note = document.createElement("p");
    note.className = "chart-note";
    note.textContent = extent.client_note;
    container.append(note);
  }

  const series = extent?.series || [];
  const segments = lineSegments(series, key);
  if (!extent || !segments.length) {
    const empty = document.createElement("p");
    empty.className = "chart-empty";
    empty.textContent = band === "median"
      ? "No extent for this month. Missing values are not drawn as zero."
      : `No ${band} band on these years. Null values are not drawn as zero.`;
    container.append(empty);
    return;
  }

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 640 120");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${bandName} ${monthName} sea-ice extent`);
  const years = series.map((pt) => pt.year);
  const minYear = Math.min(1979, ...years);
  const maxYear = Math.max(2100, ...years);
  const values = segments.flat().map((pt) => pt.value);
  const clim = extent.climatology_1981_2010_km2;
  const maxV = Math.max(...values, Number.isFinite(clim) ? clim : 0, 1) * 1.12;
  const pad = { l: 40, r: 8, t: 12, b: 20 };
  const width = 640;
  const height = 120;
  const xOf = (year) => pad.l + ((year - minYear) / (maxYear - minYear)) * (width - pad.l - pad.r);
  const yOf = (value) => pad.t + (1 - value / maxV) * (height - pad.t - pad.b);

  if (clim != null && Number.isFinite(Number(clim))) {
    const y = yOf(Number(clim));
    svg.append(line(pad.l, y, width - pad.r, y, "#5c5a53", "2 3"));
    svg.append(label(pad.l, y - 3, "1981–2010", "#8f8b80"));
  }

  for (const segment of segments) {
    const d = segment
      .map((pt, i) => `${i ? "L" : "M"}${xOf(pt.year).toFixed(1)},${yOf(pt.value).toFixed(1)}`)
      .join(" ");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", segment[0].forecast ? "#c6a56a" : "#9ec4d4");
    path.setAttribute("stroke-width", "1.5");
    svg.append(path);
    if (series.length < 40) {
      for (const pt of segment) {
        const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("cx", xOf(pt.year).toFixed(1));
        dot.setAttribute("cy", yOf(pt.value).toFixed(1));
        dot.setAttribute("r", "2.2");
        dot.setAttribute("fill", segment[0].forecast ? "#c6a56a" : "#9ec4d4");
        svg.append(dot);
      }
    }
  }

  const selected = series.find((pt) => pt.year === selectedYear);
  const x = xOf(selectedYear);
  svg.append(line(x, pad.t, x, height - pad.b, "#e7e2d6", null, 0.35));
  if (selected && !isMissingExtent(selected, key)) {
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", x.toFixed(1));
    dot.setAttribute("cy", yOf(Number(selected[key])).toFixed(1));
    dot.setAttribute("r", "3.2");
    dot.setAttribute("fill", "#e7e2d6");
    svg.append(dot);
  }

  svg.append(label(pad.l, height - 4, String(minYear), "#5c5a53"));
  svg.append(label(width - pad.r - 28, height - 4, String(maxYear), "#5c5a53"));
  svg.append(label(0, yOf(0) + 3, "0", "#5c5a53"));
  const top = millionKm2(maxV);
  if (top) svg.append(label(0, pad.t + 4, top, "#5c5a53"));
  container.append(svg);
}

function line(x1, y1, x2, y2, color, dash, opacity = 1) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "line");
  node.setAttribute("x1", x1);
  node.setAttribute("y1", y1);
  node.setAttribute("x2", x2);
  node.setAttribute("y2", y2);
  node.setAttribute("stroke", color);
  node.setAttribute("stroke-width", "1");
  node.setAttribute("stroke-opacity", String(opacity));
  if (dash) node.setAttribute("stroke-dasharray", dash);
  return node;
}

function label(x, y, text, color) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
  node.setAttribute("x", x);
  node.setAttribute("y", y);
  node.setAttribute("fill", color);
  node.setAttribute("font-size", "10");
  node.setAttribute("font-family", "IBM Plex Mono, ui-monospace, monospace");
  node.textContent = text;
  return node;
}
