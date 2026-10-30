"use strict";

const map = L.map("map", { zoomControl: true });
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

// Map layers the viewer can toggle; all on by default.
const aircraftLayer = L.layerGroup().addTo(map);
const airportLayer = L.layerGroup().addTo(map);
const railLayer = L.layerGroup().addTo(map);

const markers = new Map(); // icao24 -> L.Marker
const latest = new Map(); // icao24 -> most recent aircraft data
const photos = new Map(); // icao24 -> photo, null (none) or "loading"
let photosEnabled = false;

// ---------- helpers ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const fmt = (v, digits = 0) => (v == null ? "—" : Number(v).toFixed(digits));

// Display units, chosen on the settings page. The server always sends km, ft and knots.
let units = { distance: "km", altitude: "ft", speed: "kt" };
const DISTANCE = { km: [1, "km"], mi: [0.621371, "mi"], nm: [0.539957, "nm"] };
const SPEED = { kt: [1, "kt"], mph: [1.150779, "mph"], kmh: [1.852, "km/h"] };
const fmtDist = (km, digits = 1) => {
  const [factor, unit] = DISTANCE[units.distance];
  return km == null ? "—" : `${(km * factor).toFixed(digits)} ${unit}`;
};
const fmtAlt = (ft) => (ft == null ? "—" : units.altitude === "m" ? `${Math.round(ft * 0.3048)} m` : `${Math.round(ft)} ft`);
const fmtSpeed = (kt) => {
  const [factor, unit] = SPEED[units.speed];
  return kt == null ? "—" : `${Math.round(kt * factor)} ${unit}`;
};
const fmtVrate = (fpm) => (fpm == null ? "—" : units.altitude === "m" ? `${(fpm * 0.00508).toFixed(1)} m/s` : `${Math.round(fpm)} ft/min`);
const label = (a) => a.callsign || a.registration || a.icao24.toUpperCase();
const airportCode = (end) => end.iata || end.icao || "?";
const routeText = (a) => a.route ? `${airportCode(a.route.origin)} → ${airportCode(a.route.destination)}` : null;
// Badges after the flight number: military, and why an aircraft is interesting.
const INTEREST_BADGE = { emergency: (a) => `🚨 ${a.squawk}`, notable: () => "✨ NOTABLE", rare: () => "✨ RARE" };
const milBadge = (a) => (a.military ? ' <span class="badge-mil">MIL</span>' : "") +
  (INTEREST_BADGE[a.interest?.level] ? ` <span class="badge-int ${a.interest.level}" title="${esc(a.interest.reason)}">${esc(INTEREST_BADGE[a.interest.level](a))}</span>` : "");
const interestLevel = (a) => a.interest?.level;

function altitudeColor(a) {
  if (a.on_ground) return "#9ca3af";
  const ft = a.altitude_ft ?? 0;
  if (ft < 5000) return "#f97316";
  if (ft < 15000) return "#eab308";
  if (ft < 30000) return "#22c55e";
  return "#3b82f6";
}

// Top-down silhouettes, nose up, on a 24×24 grid. `size` is the map icon size in pixels;
// `tail` is where the tail section starts, painted in the airline's tail colour.
const PLANE_SHAPES = {
  widebody: { size: 34, tail: 17.5, d: "M12 1c.9 0 1.5 1 1.5 2.3v5.4l9.5 5.3v2.2l-9.5-2.7v5l2.7 2.1v1.6L12 21.5l-4.2.7v-1.6l2.7-2.1v-5L1 16.2V14l9.5-5.3V3.3C10.5 2 11.1 1 12 1z" },
  narrowbody: { size: 27, tail: 16.8, d: "M12 2c.8 0 1.3.9 1.3 2v5.2l7.7 4.5v2l-7.7-2.3v4.4l2.2 1.7v1.6L12 20.2l-3.5.9v-1.6l2.2-1.7v-4.4L3 15.7v-2l7.7-4.5V4c0-1.1.5-2 1.3-2z" },
  turboprop: { size: 25, tail: 16.6, d: "M12 2.5c.6 0 1 .6 1 1.4v4.3h8.5v2.1L13 11.4v6l2.8.9v1.6H8.2v-1.6l2.8-.9v-6l-8.5-1.1V8.2H11V3.9c0-.8.4-1.4 1-1.4zM5.8 6.4h1.8v4H5.8zM16.4 6.4h1.8v4h-1.8z" },
  bizjet: { size: 23, tail: 16.8, d: "M12 2.5c.6 0 1 .7 1 1.7v5.5l6.5 4.3v1.6L13 13.9v3.7l2.6 1.9v1.4L12 20.1l-3.6.8v-1.4l2.6-1.9v-3.7l-6.5 1.7v-1.6l6.5-4.3V4.2c0-1 .4-1.7 1-1.7zM9.3 14.4h1.4v2.6H9.3zM13.3 14.4h1.4v2.6h-1.4z" },
  light: { size: 22, d: "M12 3c.7 0 1.2.6 1.2 1.4v3.4h8.3v2.6l-8.3.8v5.5l3 .9v1.9H7.8v-1.9l3-.9v-5.5l-8.3-.8V7.8h8.3V4.4c0-.8.5-1.4 1.2-1.4zM9.6 1.6h4.8v1.1H9.6z" },
  military: { size: 27, d: "M12 1.5l1.3 4.2.4 4.6 7.8 6.6v2.1l-7.7-2.6-.4 2.4 2.7 2.1v1.4L12 21.5l-4.1.8v-1.4l2.7-2.1-.4-2.4-7.7 2.6v-2.1l7.8-6.6.4-4.6z" },
  glider: { size: 27, d: "M12 5.5c.4 0 .7.4.7 1v3.2l10.8.9v1.3l-10.8.6v6.3l2.4.7v1.1H8.9v-1.1l2.4-.7v-6.3L.5 11.9v-1.3l10.8-.9V6.5c0-.6.3-1 .7-1z" },
  helicopter: { size: 27, d: "M12 5.5c1.7 0 2.8 1.5 2.8 3.6 0 2-.9 3.4-1.9 3.9V20h2v1.3H9.1V20h2v-7c-1-.5-1.9-1.9-1.9-3.9 0-2.1 1.1-3.6 2.8-3.6z",
    extra: (c) => `<circle cx="12" cy="9.5" r="9" fill="none" stroke="${c}" stroke-opacity=".6" stroke-width="1.2"/>
      <path d="M4 4l16 11M20 4L4 15" stroke="#000" stroke-opacity=".45" stroke-width=".9"/>` },
  balloon: { size: 20, d: "M12 2a7 7 0 0 1 7 7c0 4-3.5 7-5.6 8.5h-2.8C8.5 16 5 13 5 9a7 7 0 0 1 7-7zM10.3 19h3.4v3h-3.4z" },
  drone: { size: 19, d: "M10 10h4v4h-4zM4.5 4.5l6 6M19.5 4.5l-6 6M4.5 19.5l6-6M19.5 19.5l-6-6",
    extra: (c) => ["5,5", "19,5", "5,19", "19,19"].map((p) => `<circle cx="${p.split(",")[0]}" cy="${p.split(",")[1]}" r="3" fill="${c}" stroke="#000" stroke-width=".6"/>`).join("") },
  vehicle: { size: 13, d: "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z" },
};
// Not identified yet: the generic airliner shape, see-through so it doesn't pass for a known type.
PLANE_SHAPES.unknown = { ...PLANE_SHAPES.narrowbody, size: 23, faded: true };

const MILITARY_COLOR = "#65a30d";
const OVERHEAD_COLOR = "#dc2626";
let aircraftColours = "airline"; // or "altitude", from the settings page

// How a plane is painted: { body, tail, glow }. Airliners in their airline's colours glow red on
// the map when overhead; everything else turns red.
function planeLook(a) {
  if (aircraftColours === "airline" && a.livery && !a.military) {
    return { body: a.livery.body, tail: a.livery.tail, glow: a.overhead };
  }
  const color = a.overhead ? OVERHEAD_COLOR : a.military ? MILITARY_COLOR : altitudeColor(a);
  return { body: color, tail: color };
}

// The tail clip shapes live in one hidden SVG: a clip-path defined inside each icon would stop
// working whenever the first icon using it sat in a hidden panel.
const tailClips = document.createElementNS("http://www.w3.org/2000/svg", "svg");
tailClips.setAttribute("style", "position: absolute; width: 0; height: 0");
tailClips.innerHTML = `<defs>${[...new Set(Object.values(PLANE_SHAPES).map((s) => s.tail).filter(Boolean))].map((y) =>
  `<clipPath id="tail-${String(y).replace(".", "_")}"><rect y="${y}" width="24" height="${24 - y}"/></clipPath>`).join("")}</defs>`;
document.body.append(tailClips);

function shapeSvg(shape, look, size, rotate = 0) {
  const s = PLANE_SHAPES[shape] || PLANE_SHAPES.unknown;
  const { body, tail, glow } = look;
  const svg = (inner) => `<svg${glow ? ' class="overhead"' : ""} width="${size}" height="${size}" viewBox="0 0 24 24" style="transform: rotate(${rotate}deg)">
    ${s.extra ? s.extra(body) : ""}${inner}</svg>`;
  if (shape === "drone") return svg(`<path stroke="${body}" stroke-width="1.6" fill="${body}" d="${s.d}"/>`);
  const faded = s.faded ? ' fill-opacity=".45"' : "";
  const tailPart = s.tail && tail !== body
    ? `<path fill="${tail}"${faded} clip-path="url(#tail-${String(s.tail).replace(".", "_")})" d="${s.d}"/>` : "";
  return svg(`<path fill="${body}"${faded} d="${s.d}"/>${tailPart}<path fill="none" stroke="#000" stroke-width=".6" d="${s.d}"/>`);
}

// Airline logo, if there is one (missing ones are remembered and not asked for again).
const noLogo = new Set();
function logoImg(a, size = 16) {
  const code = a.airline_icao;
  if (!code || noLogo.has(code)) return "";
  return `<img class="logo" src="/api/logo/${esc(code)}" data-code="${esc(code)}" width="${size}" height="${size}"
    alt="" title="${esc(a.airline_name || code)}">`;
}
document.addEventListener("error", (e) => {
  if (e.target.matches?.("img.logo")) {
    noLogo.add(e.target.dataset.code);
    e.target.remove();
  }
}, true);

function planeIcon(a) {
  const { size } = PLANE_SHAPES[a.shape] || PLANE_SHAPES.unknown;
  // Balloons and ground vehicles don't point anywhere.
  const rotate = ["balloon", "vehicle"].includes(a.shape) ? 0 : a.track_deg ?? 0;
  // Emergencies get a pulsing red ring, notable and rare types a gold one.
  const ring = { emergency: " ring-emergency", notable: " ring-interesting", rare: " ring-interesting" }[interestLevel(a)] || "";
  return L.divIcon({
    className: `plane${ring}`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: shapeSvg(a.shape, planeLook(a), size, rotate),
  });
}

function photoHtml(a) {
  const p = photos.get(a.icao24);
  if (!p || p === "loading") return "";
  // planespotters.net terms: hotlink the image, credit the photographer, link back.
  return `<a class="photo" href="${esc(p.link || "https://www.planespotters.net/")}" target="_blank" rel="noopener">
      <img src="${esc(p.thumbnail)}" alt="Photo of ${esc(label(a))}"></a>
    <div class="credit">© ${esc(p.photographer)} · planespotters.net</div>`;
}

function hoverHtml(a) {
  // Flight number, where it's going, and what it is.
  const lines = [`${logoImg(a)}<b>${esc(label(a))}</b>${milBadge(a)}`];
  if (a.interest && a.interest.level !== "military") lines.push(`<span class="tip-interest">${esc(a.interest.reason)}</span>`);
  if (a.airline_name) lines.push(esc(a.airline_name));
  if (a.route) {
    const { origin: o, destination: d } = a.route;
    lines.push(`${esc(airportCode(o))} → ${esc(airportCode(d))}`);
    const cities = [o.city || o.name, d.city || d.name];
    if (cities[0] && cities[1]) lines.push(`<span class="tip-meta">${esc(cities[0])} → ${esc(cities[1])}</span>`);
  }
  lines.push(esc(a.aircraft_name || a.type_code || a.kind || "Aircraft"));
  return lines.join("<br>");
}

function routeHtml(a) {
  if (!a.route) return "";
  const end = (e) => `<b>${esc(airportCode(e))}</b> ${esc(e.city || e.name || "")}`;
  return `<div class="route">${end(a.route.origin)} → ${end(a.route.destination)}
    <div class="credit">${esc(a.route.airline || "")} · scheduled route, may be outdated</div></div>`;
}

function popupHtml(a) {
  const rows = [
    ["Interesting", a.interest && a.interest.level !== "military" ? a.interest.reason : null],
    ["Callsign", a.callsign],
    ["Airline", a.airline_name],
    ["Registration", a.registration],
    ["Aircraft", a.aircraft_name || a.type_code],
    ["Operator", a.operator],
    ["ICAO24", a.icao24.toUpperCase()],
    ["Country", a.origin_country],
    ["Altitude", a.on_ground ? "On ground" : fmtAlt(a.altitude_ft)],
    ["Speed", fmtSpeed(a.speed_kt)],
    ["Heading", `${fmt(a.track_deg)}°`],
    ["Vertical", fmtVrate(a.vertical_rate_fpm)],
    ["Squawk", a.squawk],
    ["From home", `${fmtDist(a.distance_km)} ${a.direction}`],
    ["Source", a.source],
  ].filter(([, v]) => v != null && v !== "");
  const links = `<a href="https://globe.adsbexchange.com/?icao=${esc(a.icao24)}" target="_blank" rel="noopener">ADS-B Exchange</a>`;
  return `${photoHtml(a)}<div class="title">${logoImg(a, 32)}<b>${esc(label(a))}</b>${milBadge(a)}</div>${routeHtml(a)}<table>${rows.map(([k, v]) =>
    `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("")}</table>${links}`;
}

async function loadPhoto(icao24) {
  if (!photosEnabled || photos.has(icao24)) return;
  photos.set(icao24, "loading");
  try {
    const resp = await fetch(`/api/photo/${icao24}`);
    photos.set(icao24, resp.ok ? await resp.json() : null);
  } catch {
    photos.delete(icao24); // network blip: try again next time the popup opens
    return;
  }
  if (isSelected("aircraft", icao24)) renderDetails();
}

// ---------- rendering ----------

// ---------- smooth movement ----------
// Between updates each plane carries on along its last known track at its last known speed,
// following its current turn (dead reckoning). When a new position arrives, it eases onto it.
const KT_TO_KM_PER_S = 1.852 / 3600;
const MAX_EXTRAPOLATE_S = 45; // stop guessing if a plane hasn't been heard from for this long
const MIN_PREDICT_KT = 30; // taxiing / hovering: too erratic to predict
const MAX_TURN_DEG_S = 3; // a standard-rate turn; anything faster is noise
const MAX_TURN_DEG = 90; // never predict more than a quarter turn
const BLEND_S = 2.5; // time to ease onto a corrected position
const MAX_BLEND_KM = 5; // bigger corrections (e.g. a source switch) just snap
const FRAME_MS = 33;
const CLOCK_SAMPLES = 60; // about two minutes of snapshots
const motion = new Map(); // icao24 -> { lat, lon, track, speed, rate, t0, offset, blendStart }
let zooming = false;

// Server clock minus browser clock. Every sample is late by however long the message took to
// arrive and be handled, so the largest recent sample is the most accurate one. Using it (rather
// than the latest) stops predictions stepping back and forth with each update's delay.
const clockSamples = [];
let clockOffset = 0;
function noteServerTime(serverTime) {
  clockSamples.push(serverTime - Date.now() / 1000);
  if (clockSamples.length > CLOCK_SAMPLES) clockSamples.shift();
  clockOffset = Math.max(...clockSamples);
}
const serverNow = () => Date.now() / 1000 + clockOffset;

const wrapDeg = (d) => ((d % 360) + 540) % 360 - 180;

function predicted(mo, t) {
  // [lat, lon, heading] of the plane at server time t.
  if (mo.ground || mo.track == null || !(mo.speed >= MIN_PREDICT_KT)) return [mo.lat, mo.lon, mo.track];
  const dt = Math.min(Math.max(t - mo.t0, 0), MAX_EXTRAPOLATE_S);
  const v = mo.speed * KT_TO_KM_PER_S; // km/s
  const turn = Math.max(-MAX_TURN_DEG, Math.min(MAX_TURN_DEG, (mo.rate || 0) * dt));
  const h0 = (mo.track * Math.PI) / 180;
  let east, north;
  if (Math.abs(turn) < 0.01) {
    east = v * dt * Math.sin(h0);
    north = v * dt * Math.cos(h0);
  } else {
    // Constant-rate turn: the plane follows a circular arc.
    const w = (turn * Math.PI) / 180 / dt;
    east = (v / w) * (Math.cos(h0) - Math.cos(h0 + w * dt));
    north = (v / w) * (Math.sin(h0 + w * dt) - Math.sin(h0));
  }
  const lat = mo.lat + north / 111.2;
  return [lat, mo.lon + east / (111.2 * Math.cos((mo.lat * Math.PI) / 180)), mo.track + turn];
}

const smoothstep = (k) => k * k * (3 - 2 * k);

function displayed(mo, t) {
  const p = predicted(mo, t);
  if (!mo.offset) return p;
  const k = (t - mo.blendStart) / BLEND_S;
  if (k >= 1) {
    mo.offset = null;
    return p;
  }
  const left = 1 - smoothstep(k);
  return [p[0] + mo.offset[0] * left, p[1] + mo.offset[1] * left, p[2]];
}

function updateMotion(a, t) {
  const prev = motion.get(a.icao24);
  const next = {
    lat: a.lat, lon: a.lon, track: a.track_deg, speed: a.speed_kt, ground: a.on_ground,
    t0: a.position_time ?? a.last_seen, rate: 0, offset: null, blendStart: t,
  };
  if (prev) {
    const gap = next.t0 - prev.t0;
    if (gap > 2 && gap < 60 && prev.track != null && next.track != null) {
      // A fresh position: the change in heading since the last one gives the turn rate.
      const rate = wrapDeg(next.track - prev.track) / gap;
      next.rate = Math.max(-MAX_TURN_DEG_S, Math.min(MAX_TURN_DEG_S, rate));
    } else if (gap <= 2) {
      next.rate = prev.rate; // same position resent: keep the turn we already had
    }
    // Start from where the plane is drawn now and ease towards the corrected track.
    const shown = displayed(prev, t), target = predicted(next, t);
    const offset = [shown[0] - target[0], shown[1] - target[1]];
    const km = Math.hypot(offset[0] * 111.2, offset[1] * 111.2 * Math.cos((target[0] * Math.PI) / 180));
    if (km > 0.001 && km < MAX_BLEND_KM) next.offset = offset;
  }
  motion.set(a.icao24, next);
  return displayed(next, t);
}

// Leaflet rounds marker positions to whole pixels, so slow movement steps a pixel at a time.
// Planes are positioned to a fraction of a pixel instead, so they glide.
const GlidingMarker = L.Marker.extend({
  update() {
    if (this._icon && this._map) {
      const pos = this._map.latLngToLayerPoint(this._latlng);
      L.DomUtil.setPosition(this._icon, pos);
      // CSS z-index must be a whole number.
      this._zIndex = Math.round(pos.y) + this.options.zIndexOffset;
      this._resetZIndex();
    }
    return this;
  },
});

function animate(now) {
  if (!zooming && (!animate.last || now - animate.last >= FRAME_MS)) {
    animate.last = now;
    const t = serverNow();
    for (const [icao, mo] of motion) {
      const m = markers.get(icao);
      if (!m) continue;
      const [lat, lon, heading] = displayed(mo, t);
      m.setLatLng([lat, lon]);
      // Turn the icon with the predicted heading while the plane is turning.
      if (mo.rate && heading != null) {
        const svg = m.getElement()?.firstElementChild;
        if (svg) svg.style.transform = `rotate(${heading}deg)`;
      }
    }
  }
  requestAnimationFrame(animate);
}
map.on("zoomstart", () => (zooming = true));
map.on("zoomend", () => (zooming = false));
requestAnimationFrame(animate);

function iconKey(a) {
  // Only rebuild a plane's icon when what it shows changes.
  return `${a.shape}|${JSON.stringify(planeLook(a))}|${interestLevel(a)}|${Math.round((a.track_deg ?? 0) / 3)}`;
}

function renderAircraft(list) {
  const seen = new Set();
  const t = serverNow();
  for (const a of list) {
    seen.add(a.icao24);
    latest.set(a.icao24, a);
    const at = updateMotion(a, t);
    let m = markers.get(a.icao24);
    if (!m) {
      m = new GlidingMarker(at.slice(0, 2), { icon: planeIcon(a) }).addTo(aircraftLayer);
      m.iconKey = iconKey(a);
      m.on("click", () => select("aircraft", a.icao24));
      markers.set(a.icao24, m);
    } else {
      m.setLatLng(at.slice(0, 2));
      if (m.iconKey !== iconKey(a)) {
        m.iconKey = iconKey(a);
        m.setIcon(planeIcon(a));
      }
    }
    // Update the hover label in place, so it doesn't flicker while hovered.
    if (m.getTooltip()) m.setTooltipContent(hoverHtml(a));
    else {
      const { size } = PLANE_SHAPES[a.shape] || PLANE_SHAPES.unknown;
      m.bindTooltip(hoverHtml(a), { direction: "top", offset: [0, -size / 2], className: "plane-tip" });
    }
  }
  for (const [icao, m] of markers) {
    if (!seen.has(icao)) {
      m.remove();
      markers.delete(icao);
      latest.delete(icao);
      motion.delete(icao);
    }
  }
  if (selection.kind === "aircraft") renderDetails();

  // Side panel: just what's overhead right now.
  const overhead = list.filter((a) => a.overhead);
  document.getElementById("overhead-count").textContent = overhead.length;
  document.getElementById("overhead").innerHTML = overhead.length
    ? overhead.map((a) => `<li data-icao="${esc(a.icao24)}">
        <div class="row"><span class="callsign">${shapeSvg(a.shape, planeLook(a), 16)} ${logoImg(a, 14)}${esc(label(a))}${milBadge(a)}</span>
          <span>${fmtAlt(a.altitude_ft)}</span></div>
        <div class="row meta"><span>${esc(a.kind || "")}${routeText(a) ? ` · ${esc(routeText(a))}` : ""}</span>
          <span>${fmtDist(a.distance_km)} ${esc(a.direction)}</span></div></li>`).join("")
    : '<li class="empty">Nothing overhead</li>';

  // Side panel: anything interesting in range, most urgent first.
  const ORDER = { emergency: 0, notable: 1, rare: 2, military: 3 };
  const interesting = list.filter((a) => a.interest).sort((x, y) =>
    ORDER[x.interest.level] - ORDER[y.interest.level] || x.distance_km - y.distance_km);
  document.getElementById("interesting-count").textContent = interesting.length;
  document.getElementById("interesting").innerHTML = interesting.length
    ? interesting.slice(0, 10).map((a) => `<li data-icao="${esc(a.icao24)}" class="interest-${a.interest.level}">
        <div class="row"><span class="callsign">${shapeSvg(a.shape, planeLook(a), 16)} ${logoImg(a, 14)}${esc(label(a))}${milBadge(a)}</span>
          <span>${a.on_ground ? "Ground" : fmtAlt(a.altitude_ft)}</span></div>
        <div class="row meta"><span>${esc(a.interest.level === "military" ? (a.aircraft_name || a.kind || "Military") : a.interest.reason)}</span>
          <span>${fmtDist(a.distance_km)} ${esc(a.direction)}</span></div></li>`).join("")
      + (interesting.length > 10 ? `<li class="empty">and ${interesting.length - 10} more (filter the table)</li>` : "")
    : '<li class="empty">Nothing unusual in range</li>';

  // Drawer: every aircraft in range, nearest first.
  document.getElementById("aircraft-count").textContent = list.length;
  document.getElementById("aircraft").innerHTML = list.length
    ? list.slice(0, 200).map((a) => `<tr data-icao="${esc(a.icao24)}" class="${a.overhead ? "overhead-item" : ""}">
        <td class="icon">${shapeSvg(a.shape, planeLook(a), 20)}</td>
        <td>${esc(label(a))}${milBadge(a)}<small>${esc(a.registration || a.icao24.toUpperCase())}</small></td>
        <td>${esc(a.kind || "Aircraft")}<small>${esc(a.aircraft_name || a.type_code || "")}</small></td>
        <td>${esc(routeText(a) || "—")}<small>${logoImg(a, 12)}${esc(a.airline_name || a.operator || a.route?.airline || a.origin_country || "")}</small></td>
        <td class="num">${a.on_ground ? "Ground" : fmtAlt(a.altitude_ft)}</td>
        <td class="num">${fmtSpeed(a.speed_kt)}</td>
        <td class="num">${fmtDist(a.distance_km)} ${esc(a.direction)}</td></tr>`).join("")
    : '<tr class="empty"><td colspan="7">No aircraft in range yet</td></tr>';
  applyFilter();
}

const SOURCE_NAMES = { receiver: "📡 My receiver", adsblol: "adsb.lol", opensky: "OpenSky", airplaneslive: "airplanes.live" };

function renderSources(sources) {
  // Your own receiver first, then the online feeds.
  const entries = Object.entries(sources).sort(([a], [b]) => (b === "receiver") - (a === "receiver"));
  document.getElementById("sources").innerHTML = entries.map(([name, s]) => {
    const label = esc(SOURCE_NAMES[name] || name);
    if (s.standby) {
      return `<span title="Standing by while your receiver is working"><span class="dot"></span>${label} (standby)</span>`;
    }
    const cls = s.ok === true ? "ok" : s.ok === false ? "err" : "";
    const title = s.error ? esc(s.error) : `${s.count} aircraft, every ${s.interval}s`;
    return `<span title="${title}"><span class="dot ${cls}"></span>${label} (${s.count})</span>`;
  }).join("");
}

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`.trim();
  el.textContent = text;
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), 6000);
}

function onAlert(a) {
  const route = routeText(a) ? ` (${routeText(a)})` : "";
  const text = `${label(a)}${route} overhead — ${fmtAlt(a.altitude_ft)}, ${fmtDist(a.distance_km)} ${a.direction}`;
  toast(text);
  // Browser notifications only work over HTTPS or on localhost.
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("Aircraft overhead", { body: text });
  }
}

function onInterestAlert(a) {
  const where = `${fmtDist(a.distance_km)} ${a.direction}${a.on_ground ? ", on the ground" : `, ${fmtAlt(a.altitude_ft)}`}`;
  const what = a.aircraft_name || a.type_code || a.kind || "aircraft";
  const text = `${a.interest.level === "emergency" ? "🚨" : "✨"} ${label(a)}: ${a.interest.reason} (${what}), ${where}`;
  toast(text, a.interest.level === "emergency" ? "emergency" : "interesting");
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(a.interest.level === "emergency" ? "Aircraft emergency" : "Interesting aircraft", { body: text });
  }
}

// Clicking a list entry selects that train or aircraft.
function focusTrain(e) {
  const row = e.target.closest("[data-train]");
  if (row && trainData.has(row.dataset.train)) select("train", row.dataset.train);
}
document.getElementById("train-nearby").addEventListener("click", focusTrain);

for (const id of ["overhead", "interesting", "aircraft"]) {
  document.getElementById(id).addEventListener("click", (e) => {
    const li = e.target.closest("[data-icao]");
    if (li && latest.has(li.dataset.icao)) select("aircraft", li.dataset.icao);
  });
}

// ---------- tabs, drawer and filter ----------

const remember = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
const recall = (key) => { try { return localStorage.getItem(key); } catch { return null; } };

function showTab(group, id) {
  const nav = document.querySelector(`[data-tabs="${group}"]`);
  for (const b of nav.querySelectorAll("button[data-tab]")) {
    b.classList.toggle("active", b.dataset.tab === id);
    document.getElementById(b.dataset.tab).hidden = b.dataset.tab !== id;
  }
  remember(`tab-${group}`, id);
  if (group === "drawer") applyFilter();
}

for (const nav of document.querySelectorAll("[data-tabs]")) {
  nav.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) showTab(nav.dataset.tabs, b.dataset.tab);
  });
}

function restoreTabs() {
  // Called once trains are known, so a remembered Trains/Departures tab can be shown.
  for (const group of ["drawer", "panel"]) {
    const id = recall(`tab-${group}`);
    const button = id && document.querySelector(`[data-tabs="${group}"] [data-tab="${id}"]`);
    if (button && !button.hidden) showTab(group, id);
  }
}

const drawer = document.getElementById("drawer");
let mapResizeTimer = null;
const refreshMapSize = () => {
  clearTimeout(mapResizeTimer);
  mapResizeTimer = setTimeout(() => map.invalidateSize(), 50);
};

function setDrawerHeight(px) {
  const h = Math.round(Math.min(Math.max(px, 90), window.innerHeight * 0.7));
  drawer.style.setProperty("--drawer-height", `${h}px`);
  remember("drawer-height", h);
  refreshMapSize();
}

function setDrawerCollapsed(collapsed) {
  drawer.classList.toggle("collapsed", collapsed);
  document.getElementById("drawer-toggle").textContent = collapsed ? "▴" : "▾";
  remember("drawer-collapsed", collapsed ? "1" : "");
  refreshMapSize();
}

document.getElementById("drawer-toggle").addEventListener("click", () => setDrawerCollapsed(!drawer.classList.contains("collapsed")));

// Side panel: hide it all, or fold away groups in the Now tab. Both are remembered.
function setPanelHidden(hidden) {
  document.getElementById("main").classList.toggle("panel-hidden", hidden);
  document.getElementById("panel-show").hidden = !hidden;
  remember("panel-hidden", hidden ? "1" : "");
  refreshMapSize();
}
document.getElementById("panel-hide").addEventListener("click", () => setPanelHidden(true));
document.getElementById("panel-show").addEventListener("click", () => setPanelHidden(false));
if (recall("panel-hidden")) setPanelHidden(true);

// ---------- selection: one aircraft, train or bus, its details at the top of the side panel ----------
// Picked on the map, in the Now tab or in the tables; the map centres on it.
const selection = { kind: null, id: null };
const isSelected = (kind, id) => selection.kind === kind && selection.id === id;
const SELECT_ZOOM = { aircraft: 10, train: 13 };

function selectedItem() {
  const { kind, id } = selection;
  if (kind === "aircraft") return latest.get(id);
  if (kind === "train") return trainData.get(id);
  if (kind === "bus") return latestBuses.find((b) => b.id === id);
  if (kind === "disruption") return latestDisruptions?.find((d) => d.id === id);
  if (kind === "road") return latestRoads.find((r) => r.id === id);
  return null;
}

function select(kind, id) {
  const again = isSelected(kind, id);
  if (!again && selection.kind === "bus") hideRoute(selection.id);
  if (!again) disruptionLayer.clearLayers();
  Object.assign(selection, { kind, id });
  const item = selectedItem();
  if (!item) return deselect();
  if (document.getElementById("main").classList.contains("panel-hidden")) setPanelHidden(false);
  if (kind === "aircraft") {
    map.setView(markers.get(id)?.getLatLng() || [item.lat, item.lon], Math.max(map.getZoom(), SELECT_ZOOM.aircraft));
    loadPhoto(id);
  } else if (kind === "disruption") {
    showDisruptionStops(item);
  } else if (kind === "road") {
    if (!map.hasLayer(roadLayer)) roadLayer.addTo(map);
    map.fitBounds(L.latLngBounds(item.lines.flat()).pad(0.4), { maxZoom: 14 });
    drawRoads();
  } else if (kind === "train") {
    map.setView(trainMarkers.get(id)?.getLatLng() || [item.lat, item.lon], Math.max(map.getZoom(), SELECT_ZOOM.train));
  } else {
    // Zoomed in far enough for buses to be drawn.
    if (!map.hasLayer(busLayer)) busLayer.addTo(map);
    map.setView([item.lat, item.lon], Math.max(map.getZoom(), busMinZoom + 2));
    if (!again || !shownRoute) showRoute(item);
    renderBuses();
  }
  if (kind !== "road" && latestRoads.length) drawRoads(); // un-highlight a closure selected before
  renderDetails();
}

function deselect() {
  if (selection.kind === "bus") hideRoute(selection.id);
  disruptionLayer.clearLayers();
  const wasRoad = selection.kind === "road";
  Object.assign(selection, { kind: null, id: null });
  renderDetails();
  if (wasRoad) drawRoads();
}

const DETAILS_TITLE = { aircraft: "✈ Selected aircraft", train: "🚆 Selected train", bus: "🚌 Selected bus",
  disruption: "⚠ Bus disruption", road: "🚧 Road closure" };

// Re-rendered on every update while something is selected.
function renderDetails() {
  const el = document.getElementById("details");
  markSelected();
  if (!selection.kind) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  const item = selectedItem();
  const body = !item ? '<p class="meta">Not in range any more.</p>'
    : selection.kind === "aircraft" ? popupHtml(item)
    : selection.kind === "train" ? trainPopup(item)
    : selection.kind === "disruption" ? disruptionHtml(item)
    : selection.kind === "road" ? roadHtml(item) : busPopup(item);
  el.hidden = false;
  el.innerHTML = `<button type="button" class="group-head details-head" title="Close">${DETAILS_TITLE[selection.kind]}</button>
    <div class="details-body">${body}</div>`;
}
document.getElementById("details").addEventListener("click", (e) => {
  if (e.target.closest(".details-head")) deselect();
  const d = e.target.closest("[data-disruption]");
  if (d) select("disruption", d.dataset.disruption);
});
// Clicking anywhere that isn't an item clears the selection: the map's background (Leaflet's
// click, which ignores drags; markers don't pass theirs on), or the page outside the map.
// Controls (tabs, buttons, links, the filter...) and the details card itself don't count.
map.on("click", () => selection.kind && deselect());
document.addEventListener("click", (e) => {
  if (!selection.kind || e.target.closest(`#map, #details, #station-view, [data-icao], [data-train], [data-bus], [data-disruption], [data-road],
      button, a, input, select, summary, label, #drawer-handle`)) return;
  deselect();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && document.getElementById("station-view").hidden && selection.kind) deselect();
});

// The selected aircraft or train gets a glow on the map (buses: see highlightBus).
let selectedEl = null;
function markSelected() {
  selectedEl?.classList.remove("selected");
  const m = selection.kind === "aircraft" ? markers.get(selection.id)
    : selection.kind === "train" ? trainMarkers.get(selection.id) : null;
  selectedEl = m?.getElement() || null;
  selectedEl?.classList.add("selected");
  m?.setZIndexOffset(2000);
  highlightBus();
}

const foldedGroups = new Set((recall("folded-groups") || "").split(",").filter(Boolean));
for (const group of document.querySelectorAll("#now-tab .group")) {
  group.classList.toggle("collapsed", foldedGroups.has(group.id));
  const head = group.querySelector(".group-head");
  head.setAttribute("aria-expanded", String(!foldedGroups.has(group.id)));
  head.addEventListener("click", () => {
    const folded = group.classList.toggle("collapsed");
    head.setAttribute("aria-expanded", String(!folded));
    if (folded) foldedGroups.add(group.id);
    else foldedGroups.delete(group.id);
    remember("folded-groups", [...foldedGroups].join(","));
  });
}
document.getElementById("drawer-handle").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  const startY = e.clientY, startH = drawer.getBoundingClientRect().height;
  const move = (ev) => setDrawerHeight(startH + (startY - ev.clientY));
  const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});
if (recall("drawer-height")) setDrawerHeight(+recall("drawer-height"));
if (recall("drawer-collapsed")) setDrawerCollapsed(true);

// Filter the visible table by any text in its rows (callsign, type, route, operator...).
const filterInput = document.getElementById("filter");
function applyFilter() {
  const q = filterInput.value.trim().toLowerCase();
  for (const body of ["aircraft", "train-nearby", "bus-nearby"]) {
    for (const row of document.getElementById(body).rows) {
      if (!row.classList.contains("empty")) row.hidden = q !== "" && !row.textContent.toLowerCase().includes(q);
    }
  }
}
filterInput.addEventListener("input", applyFilter);

// ---------- data ----------

async function getJSON(path) {
  let resp;
  try {
    resp = await fetch(path);
  } catch {
    // The browser couldn't reach the server at all (it says "Failed to fetch").
    throw new Error("can't reach Passerby. Is it running?");
  }
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`);
  return resp.json();
}

async function loadWeather() {
  try {
    const metars = await getJSON("/api/weather");
    document.getElementById("weather").innerHTML = metars.length
      ? metars.map((w) => `<li>
          <div class="row"><span class="callsign">${esc(w.icao)}
            ${w.flight_category ? `<span class="cat cat-${esc(w.flight_category)}">${esc(w.flight_category)}</span>` : ""}</span>
            <span class="meta">${fmtDist(w.distance_km, 0)}</span></div>
          <div class="metar">${esc(w.raw)}</div></li>`).join("")
      : '<li class="empty">No METAR stations nearby</li>';
  } catch (e) {
    console.warn(e);
  }
}

async function loadStats() {
  try {
    const s = (await getJSON("/api/stats")).last_24h;
    document.getElementById("stats").innerHTML =
      `<div><b>${s.aircraft}</b><span>aircraft</span></div>
       <div><b>${s.visits}</b><span>sightings</span></div>
       <div><b>${s.overhead}</b><span>overhead</span></div>`;
  } catch (e) {
    console.warn(e);
  }
}

async function loadAirports() {
  const airports = await getJSON("/api/airports?limit=30");
  for (const ap of airports) {
    L.circleMarker([ap.lat, ap.lon], { radius: 4, color: "#6b7280", weight: 1, fillOpacity: 0.6 })
      .bindTooltip(`${esc(ap.iata || ap.icao)} — ${esc(ap.name)}`)
      .addTo(airportLayer);
  }
}

// ---------- trains ----------

const trainLayer = L.layerGroup().addTo(map);
const trainMarkers = new Map(); // uid -> L.Marker
const trainData = new Map(); // uid -> latest position report, for the details panel
let trainStaticKey = null;

const clock = (t) => t == null ? "—" : new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function lateHtml(mins) {
  if (mins == null) return "";
  if (mins <= 0) return '<span class="late ok">On time</span>';
  return `<span class="late ${mins >= 5 ? "bad" : "warn"}">+${mins}m</span>`;
}

function countdown(t) {
  const s = Math.round(t - Date.now() / 1000);
  if (s < -30) return "passed";
  if (s < 60) return "now";
  return `in ${Math.round(s / 60)} min`;
}

const passengerIcon = L.divIcon({ className: "train", iconSize: [22, 22], iconAnchor: [11, 11], html: "🚆" });

let homeLatLng = null;

function drawTrainStatic(trains) {
  // Stations, radius and the watched line only change on restart; draw them once.
  // The watched track's length is in the key so it's redrawn once real track replaces the straight line.
  const key = JSON.stringify([trains.stations.map((s) => s.crs), trains.radius_km,
    trains.watch && [trains.watch.a.crs, trains.watch.b.crs, trains.watch.track.length]]);
  if (key === trainStaticKey) return;
  trainStaticKey = key;
  trainLayer.clearLayers();
  trainMarkers.clear();
  if (homeLatLng && trains.radius_km > 0) {
    L.circle(homeLatLng, { radius: trains.radius_km * 1000, color: "#7c3aed", weight: 1, fill: false, dashArray: "2 6" })
      .bindTooltip(`Train radius ${fmtDist(trains.radius_km, 0)}`).addTo(trainLayer);
  }
  if (trains.watch) {
    const { a, b, track } = trains.watch;
    L.polyline(track, { color: "#7c3aed", weight: 4, opacity: 0.6 })
      .bindTooltip(`Watched line ${esc(a.crs)}–${esc(b.crs)}`).addTo(trainLayer);
  }
  for (const s of trains.stations) {
    L.circleMarker([s.lat, s.lon], { radius: 6, color: "#7c3aed", weight: 2, fillColor: "#fff", fillOpacity: 1,
      bubblingMouseEvents: false }) // opening a station keeps the selection
      .bindTooltip(`${esc(s.name)} (${esc(s.crs)}) · click for the station view`)
      .on("click", () => openStation(s.crs))
      .addTo(trainLayer);
  }
}

const STATE_TEXT = { at: "at", approaching: "due at", departing: "leaving", terminated: "terminated at" };

function trainWhere(t) {
  return t.state === "between" ? `${esc(t.from)} → ${esc(t.to)}` : `${STATE_TEXT[t.state]} ${esc(t.at)}`;
}

// Destination from the timetable when the train is matched; otherwise its next reporting point.
const trainLabel = (t) => `${t.headcode} → ${t.destination || t.next || "?"}`;
const trainEmoji = (t) => (t.bus ? "🚌" : t.freight ? "🚂" : "🚆");

// Popups are re-rendered every update; remember which calling-point lists the viewer opened.
const openCalls = new Set();
document.addEventListener("toggle", (e) => {
  const uid = e.target.dataset?.uid;
  if (uid) e.target.open ? openCalls.add(uid) : openCalls.delete(uid);
}, true);

function callingPoints(t) {
  if (!t.calls?.length) return "";
  return `<details class="calls" data-uid="${esc(t.uid)}"${openCalls.has(t.uid) ? " open" : ""}><summary>Calling points (${t.calls.length})</summary><ol>${t.calls.map((c) => `
      <li class="${c.passed ? "passed" : ""}"><span>${esc(clock(c.time))}</span>
        ${c.expected ? `<span class="late warn">${esc(clock(c.expected))}</span>` : ""}
        ${esc(c.name)}${c.platform ? ` <span class="meta">plat ${esc(c.platform)}</span>` : ""}</li>`).join("")}</ol></details>`;
}

// "Class 165/166 · Diesel unit", or for freight "Diesel locomotive · 1,600 t".
function trainType(t) {
  const parts = [t.planned_class && `Class ${t.planned_class}`, t.power,
    t.trailing_load_t && `${t.trailing_load_t.toLocaleString()} t`];
  return parts.filter(Boolean).join(" · ") || t.kind || "";
}

function trainFacts(t) {
  const facts = [t.power, t.planned_class && `Class ${t.planned_class} (planned)`,
    t.trailing_load_t && `${t.trailing_load_t.toLocaleString()} t load`,
    t.max_speed_mph && `${t.max_speed_mph} mph`, t.first_class && "First class", t.catering, t.sleeper && "Sleeper"];
  return facts.filter(Boolean).map(esc).join(" · ");
}

function trainPopup(t) {
  const route = t.origin ? `<div class="route">${esc(t.origin)} → <b>${esc(t.destination)}</b></div>` : "";
  return `<b>${esc(t.headcode)}</b> ${esc(t.kind || "")}${route}
    ${t.operator ? `<div>${esc(t.operator)}</div>` : ""}
    ${trainFacts(t) ? `<div class="meta">${trainFacts(t)}</div>` : ""}
    <table>
      <tr><td>Now</td><td>${trainWhere(t)}</td></tr>
      <tr><td>Last report</td><td>${esc(t.last_report || "—")} ${esc(clock(t.last_report_time))}</td></tr>
      <tr><td>Next report</td><td>${esc(t.next || "—")}${t.next_time ? ` ${esc(clock(t.next_time))}` : ""}</td></tr>
      ${t.platform ? `<tr><td>Platform</td><td>${esc(t.platform)}</td></tr>` : ""}
      <tr><td>Running</td><td>${lateHtml(t.late_minutes) || "—"}</td></tr>
    </table>${callingPoints(t)}<div class="credit">Position estimated from Network Rail reports${t.state === "between" && !t.on_track ? " (straight line: no track data here)" : ""}</div>`;
}

const freightIcon = L.divIcon({ className: "train", iconSize: [22, 22], iconAnchor: [11, 11], html: "🚂" });

function renderTrainPositions(positions) {
  const seen = new Set();
  for (const t of positions) {
    seen.add(t.uid);
    let m = trainMarkers.get(t.uid);
    if (!m) {
      // Drawn above aircraft so nearby trains aren't hidden under passing planes.
      m = L.marker([t.lat, t.lon], { icon: t.freight ? freightIcon : passengerIcon, zIndexOffset: 1000 })
        .on("click", () => select("train", t.uid)).addTo(trainLayer);
      trainMarkers.set(t.uid, m);
    } else {
      m.setLatLng([t.lat, t.lon]);
    }
    trainData.set(t.uid, t);
    const type = t.planned_class ? ` · Class ${t.planned_class}` : "";
    m.bindTooltip(`${esc(trainLabel(t))}${esc(type)}`, { direction: "top" });
  }
  for (const [uid, m] of trainMarkers) {
    if (!seen.has(uid)) {
      m.remove();
      trainMarkers.delete(uid);
      trainData.delete(uid);
    }
  }
  if (selection.kind === "train") renderDetails();
  document.getElementById("trains-count").textContent = positions.length;
  document.getElementById("train-nearby").innerHTML = positions.length
    ? positions.map((t) => `<tr data-train="${esc(t.uid)}">
        <td class="icon">${trainEmoji(t)}</td>
        <td>${esc(trainLabel(t))}<small>${esc(t.origin ? `from ${t.origin}` : t.kind || "")}</small></td>
        <td>${esc(t.operator || "—")}<small>${esc(trainType(t))}</small></td>
        <td>${trainWhere(t)}</td>
        <td>${esc(t.next || "—")}${t.next_time ? `<small>due ${esc(clock(t.next_time))}</small>` : ""}</td>
        <td class="num">${lateHtml(t.late_minutes) || "—"}</td></tr>`).join("")
    : '<tr class="empty"><td colspan="6">No trains moving nearby right now</td></tr>';
  applyFilter();
}

function renderTrains(trains) {
  // Trains are on: show their tabs and sections.
  document.querySelector('[data-tab="trains-tab"]').hidden = false;
  document.querySelector('[data-tab="departures-tab"]').hidden = !(trains.boards || []).length;
  const st = trains.status;
  const title = st.error || (st.connected
    ? `Live · ${st.messages} reports received · tracking ${st.tracked} trains nationally` +
      (st.timetable ? ` · ${st.matched} matched to the timetable` : " · timetable loading…")
    : "Connecting to Network Rail…");
  document.getElementById("trains-status").innerHTML =
    `<span class="dot ${st.connected ? "ok" : st.ok === false ? "err" : ""}" title="${esc(title)}"></span>`;
  drawTrainStatic(trains);
  renderTrainPositions(trains.positions);

  renderPassing(trains.watch);

  document.getElementById("train-boards").innerHTML = (trains.boards || []).map((b) => `
    <h2><a href="#" class="station-link" data-crs="${esc(b.crs)}" title="Open the station view">${esc(b.name)}</a></h2>
    <ul class="list">${b.departures.length ? b.departures.map((d) => `<li>
      <div class="row"><span><b>${esc(clock(d.time))}</b> ${d.bus ? "🚌 " : ""}${esc(d.destination)}</span>
        <span class="meta">${d.platform ? `Plat ${esc(d.platform)}` : ""}</span></div>
      <div class="row meta"><span>${esc(d.operator || "")}</span>
        <span>${d.cancelled ? '<span class="late bad">Cancelled</span>'
          : d.expected ? `Exp ${esc(clock(d.expected))} ${lateHtml(d.late_minutes)}`
          : d.live ? lateHtml(d.late_minutes) : "Scheduled"}</span></div>
    </li>`).join("") : '<li class="empty">No departures in the next two hours</li>'}</ul>`).join("");
}

// ---------- "Passing home" (Now tab) ----------
// Only the trains actually about to pass home: tracked live and due within the next few
// minutes. It can be switched off in Settings.

let passing = null; // { minutes } when enabled

function renderPassing(watch) {
  if (!passing) return;
  const el = document.getElementById("passing");
  document.getElementById("passing-ahead").textContent = watch ? `next ${passing.minutes} min` : "";
  if (!watch) {
    el.innerHTML = '<div class="pw-empty">Choose the stations either side of home in <a href="settings.html">Settings</a>.</div>';
    return;
  }
  const now = Date.now() / 1000;
  const ahead = passing.minutes * 60;
  const live = (p) => p.live && !p.cancelled;
  // Due within the window (or passing this minute), and actually running.
  const soon = watch.passes.filter((p) => live(p) && p.at_home >= now - 60 && p.at_home <= now + ahead);
  const next = watch.passes.find((p) => !p.cancelled && !soon.includes(p) && p.at_home > now);
  const when = (p) => (Math.abs(p.at_home - now) < 60 ? "Passing now" : countdown(p.at_home));
  const rows = soon.map((p) => `<div class="pw-row${Math.abs(p.at_home - now) < 60 ? " now" : ""}" data-train="${esc(p.uid)}">
      <span class="pw-when">${esc(when(p))}</span>
      <span class="pw-train">${trainEmoji(p)} <b>${esc(p.headcode)}</b> → ${esc(p.destination || p.to)}</span>
      <span class="pw-late">${lateHtml(p.late_minutes)}</span></div>`).join("");
  el.innerHTML = `${watch.warning ? `<div class="pw-warning">⚠ ${esc(watch.warning)}</div>` : ""}
    ${rows || '<div class="pw-empty">Nothing passing right now</div>'}
    ${next ? `<div class="pw-next">Next: ${esc(clock(next.at_home))} ${esc(next.headcode)} → ${esc(next.destination || next.to)}</div>` : ""}`;
}
document.getElementById("passing").addEventListener("click", focusTrain);

// ---------- station view ----------

const stationView = { crs: null, map: null, layer: null, timer: null, fitted: null };

// A platform's two far ends (north one first), so labels and trains can sit along its length.
function platformEnds(p) {
  const pts = p.shapes.flat();
  let best = [pts[0], pts[0]], bestD = -1;
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) {
      const d = (pts[i][0] - pts[j][0]) ** 2 + ((pts[i][1] - pts[j][1]) * 0.63) ** 2;
      if (d > bestD) [best, bestD] = [[pts[i], pts[j]], d];
    }
  }
  return best[0][0] >= best[1][0] ? best : [best[1], best[0]];
}
const alongPlatform = (p, f) => {
  const [a, b] = platformEnds(p);
  return [a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])];
};

// Each platform's place in the side-by-side order on the ground (numbering doesn't follow it:
// at Guildford, 5 and 8 are neighbours). Used to stagger labels and trains between neighbours.
function sideBySideRanks(platforms) {
  if (!platforms.length) return new Map();
  const [a, b] = platformEnds(platforms[0]);
  const k = Math.cos((a[0] * Math.PI) / 180);
  const dx = (b[1] - a[1]) * k, dy = b[0] - a[0];
  const offset = (p) => {
    const [c0, c1] = alongPlatform(p, 0.5);
    return dx * (c0 - a[0]) - dy * (c1 - a[1]) * k;
  };
  return new Map([...platforms].sort((p, q) => offset(p) - offset(q)).map((p, i) => [p, i]));
}
const sameRef = (a, b) => String(a || "").toUpperCase() === String(b || "").toUpperCase();
const STATION_STATUS = { at: "at platform", departing: "departing", terminated: "terminated", approaching: "due now", arriving: "arriving" };

function openStation(crs) {
  const el = document.getElementById("station-view");
  el.hidden = false;
  if (!stationView.map) {
    stationView.map = L.map("sv-map", { zoomControl: true, attributionControl: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(stationView.map);
    stationView.layer = L.layerGroup().addTo(stationView.map);
  }
  if (stationView.crs !== crs) {
    stationView.fitted = null;
    stationView.layer.clearLayers();
    document.getElementById("sv-title").textContent = "Loading…";
    document.getElementById("sv-platforms").innerHTML = '<p class="meta">Loading the station layout…</p>';
    document.getElementById("sv-board").innerHTML = "";
  }
  stationView.crs = crs;
  setTimeout(() => stationView.map.invalidateSize(), 50);
  refreshStation();
  clearInterval(stationView.timer);
  stationView.timer = setInterval(refreshStation, 10000);
}

function closeStation() {
  document.getElementById("station-view").hidden = true;
  clearInterval(stationView.timer);
  stationView.crs = null;
}

async function refreshStation() {
  const crs = stationView.crs;
  // Still waiting for this station (a first visit downloads its layout, which can be slow): don't ask again.
  if (!crs || stationView.busy === crs) return;
  stationView.busy = crs;
  try {
    const data = await getJSON(`/api/station/${crs}`);
    if (stationView.crs === crs) renderStation(data);
  } catch (e) {
    if (stationView.crs === crs) {
      document.getElementById("sv-platforms").innerHTML = `<p class="meta">Couldn't load this station: ${esc(e.message)}</p>`;
    }
  } finally {
    if (stationView.busy === crs) stationView.busy = null;
  }
}

function renderStation(data) {
  const { station, layout, trains, board } = data;
  document.getElementById("sv-title").textContent = `${station.name} (${station.crs})`;
  document.getElementById("sv-updated").textContent = `updated ${clock(Date.now() / 1000)}`;
  const platforms = layout?.platforms || [];

  // --- the plan ---
  const layer = stationView.layer;
  layer.clearLayers();
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  for (const t of layout?.tracks || []) {
    L.polyline(t.line, { color: dark ? "#cbd5e1" : "#475569", weight: t.siding ? 1.5 : 2.5,
      opacity: t.siding ? 0.5 : 0.9, dashArray: t.siding ? "3 4" : null, interactive: false }).addTo(layer);
  }
  const rank = sideBySideRanks(platforms);
  platforms.forEach((p) => {
    const busy = trains.some((t) => p.refs.some((r) => sameRef(r, t.platform)) && t.status !== "arriving");
    for (const shape of p.shapes) {
      const closed = shape.length > 2 && shape[0][0] === shape.at(-1)[0] && shape[0][1] === shape.at(-1)[1];
      const style = { color: busy ? "#7c3aed" : "#64748b", weight: closed ? 1 : 6, opacity: 0.9,
        fillColor: busy ? "#7c3aed" : "#94a3b8", fillOpacity: 0.45, interactive: false };
      (closed ? L.polygon(shape, style) : L.polyline(shape, style)).addTo(layer);
    }
    if (p.refs.length) {
      // Side-by-side platforms: number them at alternate ends so the labels don't pile up.
      L.marker(alongPlatform(p, rank.get(p) % 2 ? 0.92 : 0.08), { interactive: false, icon: L.divIcon({
        className: "sv-plat-label", html: esc(p.refs.join("/")), iconSize: null }) }).addTo(layer);
    }
  });
  // Each train is a dot on its platform (platforms are only metres apart, so text labels would
  // collide); hover for details. Hollow = arriving. Several on one platform are spread along it.
  const perPlatform = new Map();
  for (const t of trains) {
    const p = platforms.find((pl) => pl.refs.some((r) => sameRef(r, t.platform)));
    if (!p) continue;
    const n = perPlatform.get(p) || 0;
    perPlatform.set(p, n + 1);
    const arriving = t.status === "arriving";
    // Neighbouring platforms' dots are staggered a little along their length so they don't touch.
    L.circleMarker(alongPlatform(p, Math.min(0.88, [0.4, 0.5, 0.6][rank.get(p) % 3] + n * 0.14)), {
      radius: 7, color: "#fff", weight: 2, fillColor: "#7c3aed", fillOpacity: arriving ? 0.25 : 1,
      dashArray: arriving ? "3 3" : null,
    }).bindTooltip(`${trainEmoji(t)} <b>${esc(t.headcode)}</b> → ${esc(t.destination || t.next || "?")}<br>
      Platform ${esc(t.platform)} · ${STATION_STATUS[t.status] || esc(t.status)} ${lateHtml(t.late_minutes)}`, { direction: "top" })
      .addTo(layer);
  }
  if (stationView.fitted !== station.crs) {
    stationView.fitted = station.crs;
    // The window has only just opened: measure the map before fitting the platforms into it.
    stationView.map.invalidateSize();
    const pts = platforms.flatMap((p) => p.shapes.flat());
    if (pts.length) stationView.map.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 18 });
    else stationView.map.setView([station.lat, station.lon], 16);
  }

  // --- platform by platform ---
  const refs = new Set(platforms.flatMap((p) => p.refs));
  for (const t of trains) if (t.platform) refs.add(String(t.platform).toUpperCase());
  for (const d of board?.departures || []) if (d.platform) refs.add(String(d.platform).toUpperCase());
  const ordered = [...refs].sort((a, b) => (parseInt(a) || 999) - (parseInt(b) || 999) || a.localeCompare(b));
  const trainLine = (t) => `<div class="sv-train-line${t.status === "arriving" ? " arriving" : ""}">${trainEmoji(t)}
      <b>${esc(t.headcode)}</b> → ${esc(t.destination || t.next || "?")}
      <span class="meta">${t.planned_class ? `Class ${esc(t.planned_class)} · ` : ""}${STATION_STATUS[t.status] || esc(t.status)}${t.status === "arriving" && t.next_time ? ` ${esc(clock(t.next_time))}` : ""}
      ${t.platform && !t.platform_confirmed ? " · planned" : ""}</span> ${lateHtml(t.late_minutes)}</div>`;
  const rows = ordered.map((ref) => {
    const here = trains.filter((t) => sameRef(t.platform, ref));
    const next = (board?.departures || []).find((d) => sameRef(d.platform, ref) && !d.cancelled
      && !here.some((t) => t.headcode === d.headcode));
    return `<div class="sv-platform"><div class="sv-plat-name">Platform ${esc(ref)}</div>
      ${here.map(trainLine).join("") || '<div class="meta">Empty</div>'}
      ${next ? `<div class="meta">Next: ${esc(clock(next.time))} ${esc(next.destination)}${next.expected ? ` (exp ${esc(clock(next.expected))})` : ""}</div>` : ""}</div>`;
  }).join("");
  const unplaced = trains.filter((t) => !t.platform);
  document.getElementById("sv-platforms").innerHTML =
    (layout ? "" : `<p class="meta">No platform layout in OpenStreetMap for this station${data.layout_error ? ` (${esc(data.layout_error)})` : ""}.</p>`) +
    (rows || '<p class="meta">No platform information.</p>') +
    (unplaced.length ? `<div class="sv-platform"><div class="sv-plat-name">Platform not known</div>${unplaced.map(trainLine).join("")}</div>` : "");

  // --- departures ---
  document.getElementById("sv-board").innerHTML = board?.departures?.length
    ? board.departures.map((d) => `<li>
        <div class="row"><span><b>${esc(clock(d.time))}</b> ${d.bus ? "🚌 " : ""}${esc(d.destination)}</span>
          <span class="meta">${d.platform ? `Plat ${esc(d.platform)}` : ""}</span></div>
        <div class="row meta"><span>${esc(d.operator || "")}</span>
          <span>${d.cancelled ? '<span class="late bad">Cancelled</span>' : d.expected ? `Exp ${esc(clock(d.expected))} ${lateHtml(d.late_minutes)}` : d.live ? lateHtml(d.late_minutes) : "Scheduled"}</span></div></li>`).join("")
    : '<li class="empty">No departures in the next two hours</li>';
}

document.getElementById("sv-close").addEventListener("click", closeStation);
document.getElementById("station-view").addEventListener("click", (e) => {
  if (e.target.id === "station-view") closeStation(); // click outside the card
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && stationView.crs) closeStation();
});
document.getElementById("train-boards").addEventListener("click", (e) => {
  const link = e.target.closest(".station-link");
  if (link) {
    e.preventDefault();
    openStation(link.dataset.crs);
  }
});

function onTrainAlert(t) {
  const text = `${trainEmoji(t)} ${trainLabel(t)} (${t.operator || t.kind || "train"}) passing in about a minute`;
  toast(text);
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("Train passing", { body: text });
  }
}

// ---------- buses (Bus Open Data Service) ----------
// Hundreds of buses would bury the map at county scale, so they're only drawn when zoomed in
// to at least `busMinZoom` (Settings), and only those inside the view.

const busLayer = L.layerGroup();
const busMarkers = new Map(); // id -> marker, with .glide = { from, to, start }
let busMinZoom = 12;
let latestBuses = [];
const BUS_GLIDE_MS = 2500;
const BUS_LAYER_NAME = '<span class="bus-layer-name">🚌 Buses</span><span class="bus-zoom-hint"> · zoom in</span>';

function busesVisible() {
  return map.hasLayer(busLayer) && map.getZoom() >= busMinZoom;
}

// Operator colour from the server, used in inline styles only if it's a plain hex colour.
const busColour = (b) => (/^#[0-9a-f]{6}$/i.test(b.colour || "") ? b.colour : "#059669");
const operatorLabel = (b) => b.operator_name || b.operator || "";
const operatorDot = (b) => `<span class="op-dot" style="background: ${busColour(b)}"></span>`;

function busIcon(b) {
  // 🚌, the route number, then an arrow pointing the way it's heading.
  const arrow = b.bearing == null ? ""
    : `<span class="bus-dir" style="transform: rotate(${Math.round(b.bearing)}deg)">↑</span>`;
  return L.divIcon({
    className: "bus", iconSize: null,
    html: `<div class="bus-badge" style="background: ${busColour(b)}"><span class="bus-emoji">🚌</span><span>${esc(b.line || "?")}</span>${arrow}${b.disruptions?.length ? '<span class="bus-warn" title="Disruption on this route">⚠</span>' : ""}</div>`,
  });
}

function reportedAgo(b) {
  const ago = Math.max(0, Math.round(Date.now() / 1000 - b.recorded));
  return ago < 60 ? `${ago}s ago` : `${Math.round(ago / 60)} min ago`;
}

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const heading = (deg) => (deg == null ? "—" : COMPASS[Math.round(deg / 45) % 8]);

// How a bus is running against its timetable (empty when it couldn't be matched).
function busRunning(b) {
  const t = b.trip;
  if (!t) return "";
  if (t.starts_at) return `<span class="late">Starts ${esc(clock(t.starts_at))}</span>`;
  if (t.finished) return '<span class="late">Finished</span>';
  if (t.delay_s == null) return "";
  const mins = Math.round(t.delay_s / 60);
  if (mins <= -2) return `<span class="late early">${-mins} min early</span>`;
  return lateHtml(mins <= 1 ? 0 : mins);
}

const nextStop = (b) => b.trip?.next_stops?.[0];

function busStopsHtml(b) {
  const t = b.trip;
  if (!t) return '<div class="meta">Not matched to a timetable.</div>';
  const head = t.starts_at ? `Starts ${esc(clock(t.starts_at))} from ${esc(t.first_stop)}`
    : t.finished ? "Journey finished" : t.at_stop ? `At ${esc(t.at_stop)}` : "";
  const rows = (t.next_stops || []).map((s) => {
    const late = s.expected - s.due >= 60;
    return `<tr><td>${esc(s.name)}</td><td class="num">${late ? `<s>${esc(clock(s.due))}</s> ${esc(clock(s.expected))}` : esc(clock(s.due))}</td></tr>`;
  }).join("");
  return `${head ? `<div class="bus-at">${head}</div>` : ""}${rows ? `<div class="bus-next-head">Next stops</div><table class="bus-stops">${rows}</table>` : ""}`;
}

// ---- the route of the bus whose popup is open: drawn on the map, stops marked ----
const routeLayer = L.layerGroup().addTo(map);
let shownRoute = null; // { id, trip, state: "loading" | "ready" | "none", data, stops: [circle markers] }

async function showRoute(b) {
  const trip = b.trip?.trip_id;
  routeLayer.clearLayers();
  // Asked even without a trip: the server matches the bus itself, and a bus whose delay
  // couldn't be worked out this time is often still matched.
  shownRoute = { id: b.id, trip, state: "loading", asked: Date.now() };
  highlightBus();
  const mine = shownRoute;
  let data = null;
  try {
    const resp = await fetch(`/api/buses/route?id=${encodeURIComponent(b.id)}`);
    if (resp.ok) data = await resp.json();
  } catch {}
  if (shownRoute !== mine) return; // closed, or another bus opened, meanwhile
  if (!data) {
    mine.state = "none";
    return;
  }
  mine.state = "ready";
  mine.data = data;
  mine.trip = data.trip_id;
  const colour = busColour(b);
  // A white outline under the line keeps dark operator colours visible on the dark map.
  L.polyline(data.line, { color: "#fff", weight: 10, opacity: 0.9, interactive: false }).addTo(routeLayer);
  L.polyline(data.line, { color: colour, weight: 6, opacity: 1, interactive: false,
    dashArray: data.follows_roads ? null : "10 8" }).addTo(routeLayer);
  mine.stops = data.stops.map((st) => L.circleMarker([st.lat, st.lon], { radius: 5, weight: 3, color: colour,
    fillColor: "#fff", fillOpacity: 1, bubblingMouseEvents: false }).bindTooltip(`${esc(st.name)} · ${esc(clock(st.due))}`).addTo(routeLayer));
  markPassedStops(b);
  if (isSelected("bus", b.id)) renderDetails();
}

// Stops the bus has already called at are greyed out.
function markPassedStops(b) {
  if (!shownRoute?.stops) return;
  const next = shownRoute.data.stops.findIndex((st) => st.name === nextStop(b)?.name && st.due === nextStop(b)?.due);
  const passed = b.trip?.finished ? shownRoute.stops.length : next;
  shownRoute.stops.forEach((m, i) => m.setStyle(i < passed
    ? { color: "#94a3b8", fillColor: "#e2e8f0" } : { color: busColour(b), fillColor: "#fff" }));
}

function hideRoute(id) {
  if (shownRoute?.id !== id) return;
  routeLayer.clearLayers();
  shownRoute = null;
  highlightBus();
}

// While a bus is selected, the other buses fade into the background.
function highlightBus() {
  map.getContainer().classList.toggle("bus-selected", selection.kind === "bus");
  for (const [id, m] of busMarkers) {
    const selected = isSelected("bus", id);
    m.getElement()?.classList.toggle("selected", selected);
    m.setZIndexOffset(selected ? 1000 : 500);
  }
}

function busRouteHtml(b) {
  const r = shownRoute?.id === b.id ? shownRoute : null;
  if (!r || r.state === "none") return "";
  if (r.state === "loading") return '<div class="meta bus-route">Loading route…</div>';
  return `<div class="meta bus-route">Route on the map: ${r.data.stops.length} stops,
    ${r.data.follows_roads ? "following the roads" : "stop to stop (the operator doesn't publish the road path)"}</div>`;
}

function busPopup(b) {
  const operator = b.operator_name ? `${b.operator_name} (${b.operator})` : b.operator;
  const rows = [["Operator", operator], ["From", b.origin], ["Vehicle", b.vehicle],
    ["From home", `${fmtDist(b.distance_km)} ${b.direction || ""}`], ["Reported", reportedAgo(b)]]
    .filter(([, v]) => v);
  return `<b>🚌 ${esc(b.line || "Bus")}</b>${b.destination ? ` → ${esc(b.destination)}` : ""} ${busRunning(b)}
    <table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("")}</table>
    ${busDisruptionsHtml(b)}
    ${busStopsHtml(b)}
    ${busRouteHtml(b)}
    <div class="credit">Bus Open Data Service (DfT) · position as reported by the operator</div>`;
}

// Drawer table: every bus within the bus radius, nearest first, whatever the zoom.
function renderBusTable(snapshot) {
  const st = snapshot.status || {};
  document.getElementById("buses-count").textContent = latestBuses.length;
  document.getElementById("buses-status").innerHTML =
    `<span class="dot ${st.ok ? "ok" : st.ok === false ? "err" : ""}" title="${esc(st.error || "Live")}"></span>`;
  document.getElementById("bus-nearby").innerHTML = latestBuses.length
    ? latestBuses.slice(0, 200).map((b) => `<tr data-bus="${esc(b.id)}">
        <td class="icon">🚌</td>
        <td><b>${esc(b.line || "?")}</b> → ${esc(b.destination || "?")}<small>${esc(b.origin ? `from ${b.origin}` : "")}</small></td>
        <td>${operatorDot(b)}${esc(operatorLabel(b) || "—")}<small>${esc([b.operator_name && b.operator, b.vehicle && `vehicle ${b.vehicle}`].filter(Boolean).join(" · "))}</small></td>
        <td>${nextStop(b) ? `${esc(nextStop(b).name)}<small>due ${esc(clock(nextStop(b).due))}</small>` : "—"}</td>
        <td>${busRunning(b) || "—"}</td>
        <td>${esc(reportedAgo(b))}<small>heading ${esc(heading(b.bearing))}</small></td>
        <td class="num">${fmtDist(b.distance_km)} ${esc(b.direction || "")}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="7">${st.ok === false ? `No bus data: ${esc(st.error || "error")}` : "No buses nearby right now"}</td></tr>`;
  applyFilter();
}

// Now tab: buses within a mile of home (1.6 km for metric users).
const MILE_KM = 1.609344;
const nearBusKm = () => (units.distance === "km" ? 1.6 : MILE_KM);

function renderNearBuses() {
  const range = nearBusKm();
  const near = latestBuses.filter((b) => b.distance_km <= range); // already nearest first
  document.getElementById("near-buses-count").textContent = near.length;
  document.getElementById("near-buses-range").textContent =
    `within ${units.distance === "mi" ? "1 mi" : fmtDist(range, 1)}`;
  document.getElementById("near-buses").innerHTML = near.length
    ? near.map((b) => `<li data-bus="${esc(b.id)}">
        <div class="row"><span class="callsign">🚌 ${esc(b.line || "?")} → ${esc(b.destination || "?")}</span>
          <span>${fmtDist(b.distance_km)} ${esc(b.direction || "")}</span></div>
        <div class="row meta"><span>${operatorDot(b)}${esc(operatorLabel(b))}${nextStop(b) ? ` · next ${esc(nextStop(b).name)}` : ""}</span>
          <span>${busRunning(b) || esc(reportedAgo(b))}</span></div></li>`).join("")
    : '<li class="empty">No buses near home right now</li>';
}

// Clicking a bus in the table or the Now tab selects it.
for (const id of ["bus-nearby", "near-buses"]) {
  document.getElementById(id).addEventListener("click", (e) => {
    const row = e.target.closest("[data-bus]");
    if (row) select("bus", row.dataset.bus);
  });
}

function renderBuses(snapshot) {
  if (snapshot) {
    latestBuses = snapshot.buses || [];
    renderBusTable(snapshot);
    renderNearBuses();
    renderDisruptions(snapshot);
    const b = selection.kind === "bus" && selectedItem();
    if (b && shownRoute?.id === b.id) {
      if (b.trip?.trip_id && shownRoute.trip !== b.trip.trip_id) showRoute(b); // on to its next journey
      // Not found (the timetable may have been rebuilding): try again every 30 s.
      else if (shownRoute.state === "none" && b.trip && Date.now() - shownRoute.asked > 30000) showRoute(b);
      else markPassedStops(b);
    }
    if (selection.kind === "bus") renderDetails();
  }
  document.querySelectorAll(".bus-zoom-hint").forEach((el) => {
    el.hidden = map.getZoom() >= busMinZoom;
  });
  if (!busesVisible()) {
    // The selected bus's route stays drawn when zoomed out.
    busLayer.clearLayers();
    busMarkers.clear();
    highlightBus();
    return;
  }
  const view = map.getBounds().pad(0.2);
  const seen = new Set();
  const now = performance.now();
  for (const b of latestBuses) {
    if (!view.contains([b.lat, b.lon])) continue;
    seen.add(b.id);
    const key = `${b.line}|${busColour(b)}|${b.bearing == null ? "" : Math.round(b.bearing / 10)}|${b.disruptions?.length || 0}`;
    let m = busMarkers.get(b.id);
    if (!m) {
      m = L.marker([b.lat, b.lon], { icon: busIcon(b), zIndexOffset: 500 }).addTo(busLayer);
      m.on("click", () => select("bus", m.bus.id));
      m.iconKey = key;
      busMarkers.set(b.id, m);
    } else {
      if (m.iconKey !== key) {
        m.iconKey = key;
        m.setIcon(busIcon(b));
      }
      const to = L.latLng(b.lat, b.lon);
      if (!m.getLatLng().equals(to)) {
        m.glide = { from: m.getLatLng(), to, start: now };
        if (!renderBuses.frame) renderBuses.frame = requestAnimationFrame(glideBuses);
      }
    }
    m.bus = b;
  }
  highlightBus(); // new and re-iconed markers
  for (const [id, m] of busMarkers) {
    if (!seen.has(id)) {
      m.remove();
      busMarkers.delete(id);
    }
  }
}

// Ease each moved bus from where it was drawn to its new report.
function glideBuses(now) {
  renderBuses.frame = null;
  let moving = false;
  for (const m of busMarkers.values()) {
    if (!m.glide) continue;
    const t = Math.min(1, (now - m.glide.start) / BUS_GLIDE_MS);
    const e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    const { from, to } = m.glide;
    m.setLatLng([from.lat + (to.lat - from.lat) * e, from.lng + (to.lng - from.lng) * e]);
    if (t < 1) moving = true;
    else m.glide = null;
  }
  if (moving) renderBuses.frame = requestAnimationFrame(glideBuses);
}

map.on("zoomend moveend", () => renderBuses());
map.on("overlayadd overlayremove", (e) => {
  if (e.layer === busLayer) renderBuses();
});

// ---------- bus disruptions (BODS SIRI-SX): diversions, stop closures, road closures ----------

let latestDisruptions = null; // null: disruptions are off
const disruptionLayer = L.layerGroup().addTo(map);

// "Thu 30 Oct", with the time unless it's midnight.
function fmtDay(t) {
  const d = new Date(t * 1000);
  const day = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
  return d.getHours() || d.getMinutes() ? `${day} ${clock(t)}` : day;
}

function disruptionWhen(d) {
  if (!d.active) return `From ${fmtDay(d.starts)}${d.ends ? ` to ${fmtDay(d.ends)}` : ""}`;
  return d.ends ? `Until ${fmtDay(d.ends)}` : "Now";
}

const disruptionRoutes = (d) => [...new Set(d.lines.map((l) => l.line))].join(", ");

function renderDisruptions(snapshot) {
  const section = document.getElementById("disruptions-section");
  latestDisruptions = snapshot.disruptions ?? null;
  section.hidden = latestDisruptions == null;
  if (latestDisruptions == null) return;
  const st = snapshot.status?.disruptions || {};
  document.getElementById("disruptions-count").textContent = latestDisruptions.length;
  document.getElementById("disruptions").innerHTML = latestDisruptions.length
    ? latestDisruptions.map((d) => `<li data-disruption="${esc(d.id)}" class="${d.active ? "" : "upcoming"}">
        <div class="row"><span class="callsign">⚠ ${esc(d.summary)}</span></div>
        <div class="row meta"><span>${esc(disruptionWhen(d))}${d.lines.length ? ` · ${esc(disruptionRoutes(d))}` : ""}</span></div></li>`).join("")
    : `<li class="empty">${st.ok === false ? `Couldn't get disruptions: ${esc(st.error || "error")}` : st.ok ? "No disruptions near home" : "Loading…"}</li>`;
  if (selection.kind === "disruption") renderDetails();
}
document.getElementById("disruptions").addEventListener("click", (e) => {
  const li = e.target.closest("[data-disruption]");
  if (li) select("disruption", li.dataset.disruption);
});

function busDisruptionsHtml(b) {
  const ds = (b.disruptions || []).map((id) => latestDisruptions?.find((d) => d.id === id)).filter(Boolean);
  return ds.length ? `<div class="bus-disruptions">${ds.map((d) =>
    `<div class="bus-disruption" data-disruption="${esc(d.id)}">⚠ ${esc(d.summary)}</div>`).join("")}</div>` : "";
}

function disruptionHtml(d) {
  const operators = [...new Set(d.lines.map((l) => l.operator_name || l.operator).filter(Boolean))].join(", ");
  const rows = [["When", disruptionWhen(d)], ["Why", d.reason], ["Routes", disruptionRoutes(d)], ["Operator", operators],
    ["Severity", d.severity && d.severity !== "unknown" ? d.severity : null]].filter(([, v]) => v);
  // Both sides of a road often share a name: "Parkway (2 stops)".
  const named = new Map();
  for (const s of d.stops) named.set(s.name || "Stop", (named.get(s.name || "Stop") || 0) + 1);
  const stops = named.size ? `<div class="bus-next-head">Stops affected near home</div>
    <ul class="disruption-stops">${[...named].map(([name, n]) => `<li>${esc(name)}${n > 1 ? ` (${n} stops)` : ""}</li>`).join("")}</ul>` : "";
  return `<b>${esc(d.summary)}</b>
    <table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("")}</table>
    ${d.description ? `<p class="disruption-text">${esc(d.description)}</p>` : ""}
    ${stops}
    ${d.link ? `<a href="${esc(d.link)}" target="_blank" rel="noopener">More information</a>` : ""}
    <div class="credit">Bus Open Data Service (DfT) · as published by the operator or council</div>`;
}

// The selected disruption's stops, marked on the map, which fits them in view.
const disruptionIcon = L.divIcon({ className: "disruption-stop", iconSize: [22, 22], iconAnchor: [11, 11], html: "⚠" });
function showDisruptionStops(d) {
  disruptionLayer.clearLayers();
  for (const s of d.stops) {
    L.marker([s.lat, s.lon], { icon: disruptionIcon, zIndexOffset: 3000, bubblingMouseEvents: false })
      .bindTooltip(esc(s.name || "Stop")).addTo(disruptionLayer);
  }
  if (d.stops.length) {
    map.fitBounds(L.latLngBounds(d.stops.map((s) => [s.lat, s.lon])).pad(0.3), { maxZoom: 15 });
  }
}

// ---------- road closures and incidents (National Highways) ----------
// Motorways and major A-roads: incidents (closures set on the signs) in red, roadworks in
// orange, dashed until they're in force.

const roadLayer = L.layerGroup();
let latestRoads = [];
const roadColour = (r) => (r.kind === "unplanned" ? "#dc2626" : "#f97316");

// "Now · until 06:00", "Now · until Mon 21 Dec", "Fri 21:00 – Sat 06:00".
function roadWhen(r) {
  const short = (t) => (t - Date.now() / 1000 < 18 * 3600 ? clock(t) : fmtDay(t));
  if (r.active) return r.end ? `Now · until ${short(r.end)}` : "Now";
  return `${fmtDay(r.start)}${r.end ? ` – ${fmtDay(r.end)}` : ""}`;
}

function renderRoads(snapshot) {
  latestRoads = snapshot.items || [];
  const st = snapshot.status || {};
  document.getElementById("roads-count").textContent = latestRoads.length;
  document.getElementById("road-closures").innerHTML = latestRoads.length
    ? latestRoads.map((r) => `<li data-road="${esc(r.id)}" class="${r.active ? "" : "upcoming"}">
        <div class="row"><span class="callsign"><span class="road-dot" style="background: ${roadColour(r)}"></span>${esc(r.summary)}</span></div>
        <div class="row meta"><span>${esc(roadWhen(r))}${r.kind === "unplanned" ? " · incident" : r.cause ? ` · ${esc(r.cause)}` : ""}</span></div></li>`).join("")
    : `<li class="empty">${st.ok === false ? `Couldn't get closures: ${esc(st.error || "error")}` : st.ok ? "No closures near home" : "Loading…"}</li>`;
  drawRoads();
  if (selection.kind === "road") renderDetails();
}
document.getElementById("road-closures").addEventListener("click", (e) => {
  const li = e.target.closest("[data-road]");
  if (li) select("road", li.dataset.road);
});

function drawRoads() {
  roadLayer.clearLayers();
  for (const r of latestRoads) {
    const selected = isSelected("road", r.id);
    for (const line of r.lines) {
      // A point (a slip road junction, say) gets a short visible mark rather than nothing.
      if (line.length === 1) {
        L.circleMarker(line[0], { radius: selected ? 8 : 6, color: "#fff", weight: 2, fillColor: roadColour(r),
          fillOpacity: 1, bubblingMouseEvents: false }).on("click", () => select("road", r.id))
          .bindTooltip(esc(r.summary)).addTo(roadLayer);
        continue;
      }
      L.polyline(line, { color: "#fff", weight: selected ? 12 : 9, opacity: 0.85, interactive: false }).addTo(roadLayer);
      L.polyline(line, { color: roadColour(r), weight: selected ? 7 : 5, opacity: r.active ? 1 : 0.8,
        dashArray: r.active ? null : "8 6", bubblingMouseEvents: false })
        .on("click", () => select("road", r.id)).bindTooltip(esc(r.summary)).addTo(roadLayer);
    }
  }
}

function roadHtml(r) {
  const rows = [["When", roadWhen(r)], ["What", r.kind === "unplanned" ? "Incident (set on the signs)" : r.type],
    ["Why", r.kind === "unplanned" ? null : r.cause], ["Roads", r.roads.join(", ")]].filter(([, v]) => v);
  const more = r.comments.slice(1);
  return `<b>${esc(r.summary)}</b>
    <table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("")}</table>
    ${more.length ? `<ul class="road-notes">${more.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    ${r.places.length ? `<div class="bus-next-head">Where</div><ul class="road-notes">${r.places.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    <div class="credit">National Highways (motorways and major A-roads) · Open Government Licence</div>`;
}

// ---------- rain radar (RainViewer): the last two hours, playable ----------
// One tile layer per frame, all loaded, only the shown one visible, so playing doesn't flicker.

const RADAR_LAYER_NAME = "🌧 Rain radar";
const RADAR_OPACITY = 0.6;
const radarLayer = L.layerGroup();
map.createPane("radar").style.zIndex = 350; // above the map, below routes and markers
const radar = { frames: [], layers: [], shown: -1, timer: null, refresh: null, control: null };

async function loadRadar() {
  let data;
  try {
    data = await getJSON("/api/radar");
  } catch {
    radar.control?.update("Radar unavailable");
    return;
  }
  const atEnd = radar.shown === radar.frames.length - 1;
  radarLayer.clearLayers();
  radar.frames = data.frames;
  radar.layers = data.frames.map((f) => L.tileLayer(f.url, {
    pane: "radar", opacity: 0, maxNativeZoom: data.max_zoom, maxZoom: 19, tileSize: 256,
    attribution: '<a href="https://www.rainviewer.com/" target="_blank" rel="noopener">RainViewer</a>',
  }).addTo(radarLayer));
  showRadarFrame(atEnd || radar.shown < 0 ? radar.frames.length - 1 : Math.min(radar.shown, radar.frames.length - 1));
}

function showRadarFrame(i) {
  radar.layers[radar.shown]?.setOpacity(0);
  radar.shown = i;
  radar.layers[i]?.setOpacity(RADAR_OPACITY);
  const f = radar.frames[i];
  radar.control?.update(f ? `${clock(f.time)}${i === radar.frames.length - 1 ? " (latest)" : ""}` : "No radar");
}

function playRadar(on) {
  clearTimeout(radar.timer);
  radar.timer = null;
  radar.control?.playing(on);
  if (!on || !radar.frames.length) return;
  const step = () => {
    const last = radar.frames.length - 1;
    showRadarFrame(radar.shown >= last ? 0 : radar.shown + 1);
    radar.timer = setTimeout(step, radar.shown === last ? 2000 : 600); // pause on the latest
  };
  step();
}

const RadarControl = L.Control.extend({
  options: { position: "bottomleft" },
  onAdd() {
    const el = L.DomUtil.create("div", "leaflet-bar radar-control");
    el.innerHTML = '<button type="button" title="Play the last two hours">▶</button><span>🌧 Loading…</span>';
    L.DomEvent.disableClickPropagation(el);
    const button = el.querySelector("button");
    button.addEventListener("click", () => playRadar(!radar.timer));
    this.update = (text) => { el.querySelector("span").textContent = `🌧 ${text}`; };
    this.playing = (on) => { button.textContent = on ? "⏸" : "▶"; };
    return el;
  },
});

radarLayer.on("add", () => {
  radar.control = new RadarControl().addTo(map);
  loadRadar();
  radar.refresh = setInterval(() => { if (!radar.timer) loadRadar(); }, 5 * 60 * 1000);
});
radarLayer.on("remove", () => {
  playRadar(false);
  clearInterval(radar.refresh);
  radar.control?.remove();
  radar.control = null;
});

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "snapshot") {
      // Predictions run on the server's clock, so a browser clock that's off doesn't matter.
      if (msg.time) noteServerTime(msg.time);
      renderAircraft(msg.aircraft);
      renderSources(msg.sources);
      if (msg.buses) renderBuses(msg.buses);
      if (msg.roads) renderRoads(msg.roads);
      if (msg.trains) {
        renderTrains(msg.trains);
        if (!connect.tabsRestored) {
          connect.tabsRestored = true;
          restoreTabs();
        }
      }
    } else if (msg.type === "alert") {
      onAlert(msg.aircraft);
    } else if (msg.type === "interest_alert") {
      onInterestAlert(msg.aircraft);
    } else if (msg.type === "train_alert") {
      onTrainAlert(msg.train);
    }
  };
  // Reconnect if the server restarts.
  ws.onclose = () => setTimeout(connect, 3000);
}

async function loadRailways() {
  // Track geometry is static; fetch it once and draw it on a canvas (hundreds of lines).
  // The server may still be downloading it (or retrying a busy Overpass server), so keep trying.
  let lines;
  try {
    ({ lines } = await getJSON("/api/railways"));
  } catch {
    setTimeout(loadRailways, 30 * 1000);
    return;
  }
  const renderer = L.canvas({ padding: 0.5 });
  // The base map draws railways too, in much the same grey; a soft outline under bolder dashes keeps
  // ours visible in both light and dark mode.
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const casing = { renderer, color: dark ? "#0b0f14" : "#ffffff", weight: 6, opacity: 0.55, interactive: false };
  const dashes = { renderer, color: dark ? "#b4bcc8" : "#5b6472", weight: 2.5, opacity: 0.95, dashArray: "6 5", interactive: false };
  for (const line of lines) L.polyline(line, casing).addTo(railLayer);
  for (const line of lines) L.polyline(line, dashes).addTo(railLayer);
}

function addLayerControl(withTrains, withBuses, withRoads) {
  const overlays = { "✈ Aircraft": aircraftLayer };
  if (withTrains) {
    overlays["🚆 Trains"] = trainLayer;
    overlays["Railway lines"] = railLayer;
  }
  if (withBuses) {
    busLayer.addTo(map);
    overlays[BUS_LAYER_NAME] = busLayer;
  }
  if (withRoads) {
    roadLayer.addTo(map);
    overlays["🚧 Roads"] = roadLayer;
  }
  overlays["Airports"] = airportLayer;
  overlays[RADAR_LAYER_NAME] = radarLayer;
  radarLayer.addTo(map);
  L.control.layers(null, overlays, { collapsed: window.innerWidth < 760 }).addTo(map);

  // Remember which layers this viewer switched off (a per-browser convenience).
  let hidden = [];
  try {
    hidden = JSON.parse(localStorage.getItem("hiddenLayers") || "[]");
  } catch {}
  for (const name of hidden) overlays[name]?.remove();
  const save = () => {
    try {
      const off = Object.keys(overlays).filter((n) => !map.hasLayer(overlays[n]));
      localStorage.setItem("hiddenLayers", JSON.stringify(off));
    } catch {}
  };
  map.on("overlayadd overlayremove", save);
}

async function init() {
  const cfg = await getJSON("/api/config");
  units = cfg.units || units;
  aircraftColours = cfg.aircraft_colours || aircraftColours;
  photosEnabled = cfg.photos;
  const home = [cfg.home.lat, cfg.home.lon];
  homeLatLng = home;
  if (cfg.trains) {
    // Start zoomed to the train area so nearby trains are visible straight away.
    map.fitBounds(L.latLng(home).toBounds((cfg.train_radius_km || 5) * 2000));
  } else {
    map.setView(home, 9);
  }
  if (cfg.buses?.enabled) {
    busMinZoom = cfg.buses.min_zoom;
    document.querySelector('[data-tab="buses-tab"]').hidden = false;
    document.getElementById("buses-group").hidden = false;
  }
  if (cfg.roads?.enabled) document.getElementById("roads-group").hidden = false;
  addLayerControl(cfg.trains, cfg.buses?.enabled, cfg.roads?.enabled);
  renderBuses();
  if (cfg.trains && cfg.passing_window?.enabled) {
    passing = { minutes: cfg.passing_window.minutes };
    document.getElementById("trains-group").hidden = false;
  }
  L.marker(home, { title: cfg.home.name }).addTo(map).bindTooltip(esc(cfg.home.name));
  L.circle(home, { radius: cfg.alert_radius_km * 1000, color: "#dc2626", weight: 1, fillOpacity: 0.05 }).addTo(map);
  L.circle(home, { radius: cfg.radius_km * 1000, color: "#2563eb", weight: 1, fill: false, dashArray: "4 6" }).addTo(map);

  if ("Notification" in window && Notification.permission === "default" && window.isSecureContext) {
    Notification.requestPermission();
  }

  if (!cfg.trains) restoreTabs();
  loadAirports().catch(console.warn);
  if (cfg.trains) loadRailways();
  loadWeather();
  loadStats();
  setInterval(loadWeather, 10 * 60 * 1000);
  setInterval(loadStats, 60 * 1000);
  connect();
}

init();
