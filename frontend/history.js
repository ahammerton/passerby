"use strict";

// History: what has passed by over a chosen period, from Passerby's own records.

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const num = (n) => (n ?? 0).toLocaleString();

let units = { distance: "km", altitude: "ft" };
const DISTANCE = { km: [1, "km"], mi: [0.621371, "mi"], nm: [0.539957, "nm"] };
const fmtDist = (km) => {
  if (km == null) return "—";
  const [f, unit] = DISTANCE[units.distance] || DISTANCE.km;
  return `${(km * f).toFixed(1)} ${unit}`;
};
const fmtAlt = (ft) => {
  if (ft == null || ft <= 0) return "—";
  return units.altitude === "m" ? `${Math.round(ft * 0.3048).toLocaleString()} m` : `${Math.round(ft).toLocaleString()} ft`;
};

const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function when(t) {
  const d = new Date(t * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return clock(t);
  const opts = { weekday: "short", day: "numeric", month: "short" };
  if (d.getFullYear() !== today.getFullYear()) opts.year = "numeric";
  return `${d.toLocaleDateString([], opts)} ${clock(t)}`;
}
const PERIOD_TEXT = { today: "today", "7d": "in the last 7 days", "30d": "in the last 30 days", "365d": "in the last year", all: "" };

async function getJSON(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return resp.json();
}

// ---------- bar charts (one series, so no legend: the caption names it) ----------

const tip = document.getElementById("chart-tip");

function showTip(e, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  const x = Math.min(e.clientX + 12, window.innerWidth - r.width - 8);
  const y = e.clientY - r.height - 12 < 8 ? e.clientY + 16 : e.clientY - r.height - 12;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

// bars: [{ label, value, tip }]. Every `labelEvery`th label is shown under the axis.
function barChart(figure, bars, labelEvery = 1) {
  const body = figure.querySelector(".chart-body");
  const W = 600, H = 160, top = 14, bottom = 22, left = 34;
  const max = Math.max(1, ...bars.map((b) => b.value));
  // Round the scale up to a tidy number, with gridlines at halves.
  const step = 10 ** Math.floor(Math.log10(max));
  const scaleMax = Math.ceil(max / step) * step;
  const slot = (W - left) / Math.max(bars.length, 1);
  const gap = Math.min(2, slot * 0.2);
  const plotH = H - top - bottom;
  const y = (v) => top + plotH - (v / scaleMax) * plotH;
  let svg = "";
  for (const v of [0, scaleMax / 2, scaleMax]) {
    svg += `<line class="grid-line" x1="${left}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>` +
      `<text class="axis" x="${left - 6}" y="${y(v) + 4}" text-anchor="end">${num(Math.round(v))}</text>`;
  }
  bars.forEach((b, i) => {
    const x = left + i * slot + gap / 2;
    const w = Math.max(slot - gap, 1);
    const h = Math.max((b.value / scaleMax) * plotH, b.value ? 1 : 0);
    const r = Math.min(4, w / 2, h);
    const y0 = top + plotH;
    // Rounded at the data end, square on the baseline.
    if (h > 0) {
      svg += `<path class="bar" d="M${x},${y0} V${y0 - h + r} Q${x},${y0 - h} ${x + r},${y0 - h} H${x + w - r}` +
        ` Q${x + w},${y0 - h} ${x + w},${y0 - h + r} V${y0} Z"/>`;
    }
    // A full-height hit area, easier to hover than a short bar.
    svg += `<rect class="hit" data-i="${i}" x="${left + i * slot}" y="${top}" width="${slot}" height="${plotH}"/>`;
    if (i % labelEvery === 0) {
      svg += `<text class="axis" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${esc(b.label)}</text>`;
    }
  });
  body.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(figure.querySelector("figcaption").textContent)}">${svg}</svg>`;
  for (const hit of body.querySelectorAll(".hit")) {
    hit.addEventListener("mousemove", (e) => showTip(e, bars[hit.dataset.i].tip));
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
  }
}

const hourLabel = (h) => `${String(h).padStart(2, "0")}:00`;

// ---------- the page ----------

function ranked(id, items, text, count, unit) {
  const el = document.getElementById(id);
  const max = Math.max(1, ...items.map(count));
  el.innerHTML = items.length
    ? items.map((it) => `<li><span class="ranked-name">${text(it)}</span><span class="ranked-n">${num(count(it))}</span>` +
        `<span class="ranked-bar" style="width:${(count(it) / max) * 100}%"></span></li>`).join("")
    : `<li class="empty">No ${unit} yet</li>`;
}

function tiles(data) {
  const a = data.aircraft;
  const out = [
    [a.aircraft, "aircraft"], [a.overhead, "overhead"], [a.sightings, "sightings"],
  ];
  if (data.trains.enabled) out.push([data.trains.passes, "trains past home"]);
  if (data.ships) out.push([data.ships.ships, "ships"]);
  document.getElementById("history-tiles").innerHTML =
    out.map(([n, label]) => `<div><b>${num(n)}</b><span>${label}</span></div>`).join("");
}

function overheadRows(list) {
  const q = document.getElementById("overhead-filter").value.trim().toLowerCase();
  const rows = list.filter((r) => !q || [r.callsign, r.registration, r.type_code, r.aircraft_name, r.airline_name,
    r.origin, r.destination].some((v) => (v || "").toLowerCase().includes(q)));
  document.getElementById("overhead-rows").innerHTML = rows.length
    ? rows.map((r) => `<tr>
        <td>${esc(when(r.first_seen))}</td>
        <td>${esc(r.callsign || r.icao24)}<small>${esc(r.registration || "")}</small></td>
        <td>${esc(r.aircraft_name || r.type_code || "—")}<small>${esc(r.airline_name || "")}</small></td>
        <td>${r.origin || r.destination ? `${esc(r.origin || "?")} → ${esc(r.destination || "?")}` : "—"}</td>
        <td class="num">${fmtDist(r.closest_km)}</td>
        <td class="num">${fmtAlt(r.max_altitude_ft)}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="6">${q ? "Nothing matches the filter" : "Nothing overhead in this period"}</td></tr>`;
}

let current = null;

function render(data) {
  current = data;
  const a = data.aircraft;
  const first = data.first_record;
  document.getElementById("history-note").textContent = first == null
    ? "Nothing recorded yet. History builds up as Passerby runs."
    : data.period === "all"
      ? `Everything since records began, ${when(first)}.`
      : first > data.since ? `Records start ${when(first)}, so this period is incomplete.` : "";
  tiles(data);

  // Per day: for periods longer than a day.
  const days = document.getElementById("chart-days");
  days.hidden = data.period === "today" || !a.by_day.length;
  if (!days.hidden) {
    // Every day in the period, including days with nothing recorded.
    const counts = new Map(a.by_day.map((d) => [d.date, d]));
    const iso = (dt) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
    const all = [];
    const day = new Date(Math.max(data.since, first || 0) * 1000);
    day.setHours(12, 0, 0, 0);
    for (const end = new Date(); day <= end || iso(day) === iso(end); day.setDate(day.getDate() + 1)) {
      all.push(counts.get(iso(day)) || { date: iso(day), aircraft: 0, overhead: 0 });
      if (all.length > 4000) break;
    }
    const bars = all.map((d) => {
      const date = new Date(`${d.date}T12:00:00`);
      const label = date.toLocaleDateString([], { day: "numeric", month: "short" });
      return { label, value: d.aircraft,
        tip: `<b>${esc(date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" }))}</b>` +
          `<br>${num(d.aircraft)} aircraft · ${num(d.overhead)} overhead` };
    });
    barChart(days, bars, Math.max(1, Math.ceil(bars.length / 8)));
  }
  barChart(document.getElementById("chart-hours"), a.by_hour.map((h) => ({
    label: String(h.hour).padStart(2, "0"), value: h.sightings,
    tip: `<b>${hourLabel(h.hour)}–${hourLabel((h.hour + 1) % 24)}</b><br>${num(h.sightings)} sightings · ${num(h.overhead)} overhead`,
  })), 3);

  ranked("top-airlines", a.airlines, (x) => `${esc(x.name || x.code)} <span class="meta">${esc(x.name ? x.code : "")}</span>`,
    (x) => x.aircraft, "airlines");
  ranked("top-types", a.types, (x) => `${esc(x.name || x.code)} <span class="meta">${esc(x.name ? x.code : "")}</span>`,
    (x) => x.aircraft, "aircraft types");
  ranked("top-routes", a.routes, (x) => `${esc(x.origin)} → ${esc(x.destination)}`, (x) => x.flights, "routes");

  document.getElementById("interesting-count").textContent = a.interesting.length;
  document.getElementById("interesting-list").innerHTML = a.interesting.length
    ? a.interesting.map((r) => `<li class="interest-${esc(r.interest_level)}"><div class="row">
        <span><b>${esc(r.callsign || r.registration || r.icao24)}</b> ${esc(r.aircraft_name || r.type_code || "")}</span>
        <span class="meta">${esc(when(r.first_seen))}</span></div>
        <div class="meta">${esc(r.interest_reason || r.interest_level)}${r.was_overhead ? " · overhead" : ""}</div></li>`).join("")
    : '<li class="empty">Nothing unusual recorded in this period</li>';

  document.getElementById("overhead-count").textContent = num(a.overhead);
  document.getElementById("overhead-more").textContent =
    a.overhead > a.overhead_list.length ? `the latest ${num(a.overhead_list.length)}` : "";
  overheadRows(a.overhead_list);

  const t = data.trains;
  document.getElementById("trains-block").hidden = !t.enabled;
  if (t.enabled) {
    barChart(document.getElementById("chart-trains"), t.by_hour.map((h) => ({
      label: String(h.hour).padStart(2, "0"), value: h.passes,
      tip: `<b>${hourLabel(h.hour)}–${hourLabel((h.hour + 1) % 24)}</b><br>${num(h.passes)} trains`,
    })), 3);
    ranked("top-operators", t.operators, (x) => esc(x.name), (x) => x.passes, "trains");
    document.getElementById("train-rows").innerHTML = t.recent.length
      ? t.recent.map((p) => `<tr>
          <td>${esc(when(p.passed_at))}</td>
          <td>${esc(p.headcode || "—")}</td>
          <td>${esc(p.operator || (p.freight ? "Freight" : "—"))}</td>
          <td>${p.origin || p.destination ? `${esc(p.origin || "?")} → ${esc(p.destination || "?")}` : "—"}</td>
          <td class="num">${p.late_minutes == null ? "—" : p.late_minutes > 0 ? `${p.late_minutes} min late` : "On time"}</td></tr>`).join("")
      : `<tr class="empty"><td colspan="5">No trains recorded passing home ${PERIOD_TEXT[data.period]}. They're logged from now on, when a train passes between your watched stations.</td></tr>`;
  }

  const s = data.ships;
  document.getElementById("ships-block").hidden = !s;
  if (s) {
    document.getElementById("ship-rows").innerHTML = s.recent.length
      ? s.recent.map((v) => `<tr>
          <td>${esc(when(v.last_seen))}</td>
          <td>${esc(v.name || `MMSI ${v.mmsi}`)}</td>
          <td>${esc(v.destination || "—")}</td>
          <td>${v.length_m ? `${Math.round(v.length_m)} m` : "—"}</td>
          <td class="num">${fmtDist(v.closest_km)}</td></tr>`).join("")
      : '<tr class="empty"><td colspan="5">No ships recorded in this period</td></tr>';
  }
}

async function load(period) {
  for (const b of document.querySelectorAll(".periods button")) {
    b.classList.toggle("active", b.dataset.period === period);
    b.setAttribute("aria-pressed", String(b.dataset.period === period));
  }
  try { localStorage.setItem("history-period", period); } catch {}
  document.getElementById("history-note").textContent = "Loading…";
  try {
    render(await getJSON(`/api/history/summary?period=${encodeURIComponent(period)}`));
  } catch (e) {
    document.getElementById("history-note").textContent = `Couldn't load the history: ${e.message}`;
  }
}

document.querySelector(".periods").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-period]");
  if (b) load(b.dataset.period);
});
document.getElementById("overhead-filter").addEventListener("input", () => {
  if (current) overheadRows(current.aircraft.overhead_list);
});

(async () => {
  try {
    const cfg = await getJSON("/api/config");
    units = cfg.units || units;
  } catch {}
  let period = "7d";
  try { period = localStorage.getItem("history-period") || period; } catch {}
  load(document.querySelector(`.periods [data-period="${period}"]`) ? period : "7d");
})();
