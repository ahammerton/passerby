"use strict";

// Values are kept in the server's units (km, ft) and converted only for display.
const DISTANCE = { km: 1, mi: 0.621371, nm: 0.539957 };
const ALTITUDE = { ft: 1, m: 0.3048 };
const UNIT_LABEL = { km: "km", mi: "miles", nm: "nm", ft: "ft", m: "m" };

const form = document.getElementById("settings");
const field = (name) => form.elements[name];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

let loaded = null; // last response from /api/settings
let values = {}; // current form values, canonical units

function factor(kind) {
  return kind === "distance" ? DISTANCE[values.distance_unit] : ALTITUDE[values.altitude_unit];
}

// ---------- rendering ----------

function renderUnits() {
  for (const el of form.querySelectorAll("[data-unit]")) {
    el.textContent = `(${UNIT_LABEL[el.dataset.unit === "distance" ? values.distance_unit : values.altitude_unit]})`;
  }
  for (const input of form.querySelectorAll("input[data-kind]")) {
    const f = factor(input.dataset.kind);
    input.value = values[input.name] == null ? "" : +(values[input.name] * f).toFixed(2);
    input.min = +(input.dataset.min * f).toFixed(2);
    input.max = +(input.dataset.max * f).toFixed(2);
  }
}

function markOverridden() {
  for (const el of form.querySelectorAll("label")) el.classList.remove("changed");
  for (const name of loaded.overridden) {
    const el = field(name)?.closest?.("label");
    if (el) {
      el.classList.add("changed");
      el.title = `Changed from the .env default (${loaded.defaults[name] ?? "empty"})`;
    }
  }
}

async function loadStations() {
  const near = await (await fetch(`/api/stations?lat=${values.home_lat}&lon=${values.home_lon}&radius_km=25`)).json();
  const chosen = new Set((values.train_boards || "").split(",").filter(Boolean));
  const watch = (values.train_watch || "").split(",").filter(Boolean);
  // Keep already-chosen stations in the lists even if home moved away from them.
  const codes = new Set(near.map((s) => s.crs));
  const extra = [...chosen, ...watch].filter((c) => !codes.has(c)).map((crs) => ({ crs, name: crs, distance_km: null }));
  const all = [...near, ...extra];
  const label = (s) => `${s.name} (${s.crs})${s.distance_km != null ? ` · ${(s.distance_km * DISTANCE[values.distance_unit]).toFixed(1)} ${UNIT_LABEL[values.distance_unit]}` : ""}`;

  document.getElementById("boards").innerHTML = all.slice(0, 20).map((s) => `
    <label class="check"><input type="checkbox" value="${esc(s.crs)}" ${chosen.has(s.crs) ? "checked" : ""}> ${esc(label(s))}</label>`).join("")
    || '<p class="hint">No stations within 25 km.</p>';

  for (const [i, name] of [[0, "watch_a"], [1, "watch_b"]]) {
    field(name).innerHTML = '<option value="">— none —</option>' + all.map((s) =>
      `<option value="${esc(s.crs)}" ${watch[i] === s.crs ? "selected" : ""}>${esc(label(s))}</option>`).join("");
  }
}

function render() {
  for (const name of ["home_name", "home_lat", "home_lon", "distance_unit", "altitude_unit", "speed_unit", "aircraft_colours",
    "train_alert_seconds", "nrod_username", "opensky_client_id", "contact", "readsb_url", "online_feeds"]) {
    field(name).value = values[name] ?? "";
  }
  field("readsb_enabled").checked = !!values.readsb_enabled;
  field("passing_window").checked = !!values.passing_window;
  field("passing_minutes").value = values.passing_minutes ?? 5;
  field("buses_enabled").checked = !!values.buses_enabled;
  field("bus_min_zoom").value = values.bus_min_zoom ?? 12;
  field("bus_delays").checked = !!values.bus_delays;
  field("bus_disruptions").checked = !!values.bus_disruptions;
  field("bus_region").value = values.bus_region || "auto";
  field("roads_enabled").checked = !!values.roads_enabled;
  // Secrets: never filled in; just say whether one is saved.
  for (const el of form.querySelectorAll(".secret-state")) {
    el.textContent = loaded.secrets[el.dataset.secret] ? "(saved)" : "(not set)";
  }
  for (const input of form.querySelectorAll("input[data-secret]")) {
    input.value = "";
    input.placeholder = loaded.secrets[input.name] ? "•••••••• keep current" : "";
  }
  for (const box of form.querySelectorAll("input[data-clear]")) {
    box.checked = false;
    field(box.dataset.clear).disabled = false;
  }
  renderUnits();
  markOverridden();
  document.getElementById("trains-off").hidden = loaded.trains_available;
  document.getElementById("buses-off").hidden = loaded.secrets.bods_api_key;
  document.getElementById("roads-off").hidden = loaded.secrets.national_highways_api_key;
  renderBackup();
  placeMarker();
  loadStations().catch((e) => console.warn("stations", e));
}

// ---------- pages (General, Location, Aircraft, ...): one form, one Save ----------

const PAGES = [...document.querySelectorAll(".settings-page")].map((el) => el.dataset.page);

function showPage(name) {
  if (!PAGES.includes(name)) name = PAGES[0];
  for (const el of document.querySelectorAll(".settings-page")) el.hidden = el.dataset.page !== name;
  for (const b of document.querySelectorAll(".settings-tabs [data-page]")) {
    b.classList.toggle("active", b.dataset.page === name);
    b.setAttribute("aria-selected", String(b.dataset.page === name));
  }
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "location") setTimeout(() => { picker.invalidateSize(); placeMarker(); }, 0); // was hidden
}

document.querySelector(".settings-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-page]");
  if (b) showPage(b.dataset.page);
});
window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));

// The page holding a field, so a problem with it can be shown where it is.
function showFieldPage(name) {
  const page = field(name)?.closest?.(".settings-page") ?? document.querySelector(`[name="${CSS.escape(name)}"]`)?.closest(".settings-page");
  if (page) showPage(page.dataset.page);
}

// ---------- map picker ----------

const picker = L.map("picker", { scrollWheelZoom: false });
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(picker);
const marker = L.marker([0, 0], { draggable: true }).addTo(picker);

function placeMarker(pan = true) {
  const at = [values.home_lat, values.home_lon];
  marker.setLatLng(at);
  if (pan) picker.setView(at, Math.max(picker.getZoom() || 0, 11));
}

let stationTimer = null;
function moveHome(lat, lon) {
  values.home_lat = lat;
  values.home_lon = lon;
  placeMarker(false);
  // Station lists follow home, but wait until clicking or typing settles.
  clearTimeout(stationTimer);
  stationTimer = setTimeout(() => loadStations().catch(console.warn), 400);
}

function setHome(lat, lon) {
  moveHome(+lat.toFixed(6), +lon.toFixed(6));
  field("home_lat").value = values.home_lat;
  field("home_lon").value = values.home_lon;
}

picker.on("click", (e) => setHome(e.latlng.lat, e.latlng.lng));
marker.on("dragend", () => setHome(marker.getLatLng().lat, marker.getLatLng().lng));

document.getElementById("locate").addEventListener("click", () => {
  const hint = document.getElementById("locate-hint");
  // Browsers only share location with HTTPS pages (or localhost).
  if (!window.isSecureContext || !navigator.geolocation) {
    hint.textContent = "Your browser only allows this over HTTPS: pick on the map instead.";
    return;
  }
  hint.textContent = "Locating…";
  navigator.geolocation.getCurrentPosition(
    (pos) => { setHome(pos.coords.latitude, pos.coords.longitude); placeMarker(); hint.textContent = ""; },
    (err) => { hint.textContent = `Couldn't get your location: ${err.message}`; },
    { enableHighAccuracy: true, timeout: 15000 },
  );
});

// ---------- ADS-B receiver ----------

document.getElementById("receiver-find").addEventListener("click", async () => {
  const out = document.getElementById("receiver-result");
  const address = field("readsb_url").value.trim();
  if (!address) {
    out.textContent = "Enter the receiver's IP address first.";
    return;
  }
  out.textContent = "Looking…";
  try {
    const resp = await fetch("/api/receiver", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ address }),
    });
    if (resp.status === 401) return signedOut();
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail);
    field("readsb_url").value = body.url;
    field("readsb_enabled").checked = true;
    out.textContent = `Found it: ${body.aircraft} aircraft, ${body.with_position} with a position. Press Save to use it.`;
  } catch (err) {
    out.textContent = err.message;
  }
});

// ---------- OpenSky credentials.json import ----------

// OpenSky's download looks like {"clientId": "...", "clientSecret": "..."}.
function parseOpenSkyCredentials(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error("That file isn't valid JSON.");
  }
  const id = data?.clientId ?? data?.client_id;
  const secret = data?.clientSecret ?? data?.client_secret;
  if (typeof id !== "string" || typeof secret !== "string" || !id.trim() || !secret.trim()) {
    throw new Error("That file doesn't contain an OpenSky clientId and clientSecret.");
  }
  return { id: id.trim(), secret: secret.trim() };
}

document.getElementById("opensky-import").addEventListener("click", () => document.getElementById("opensky-file").click());
document.getElementById("opensky-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // allow re-importing the same file
  if (!file) return;
  try {
    const { id, secret } = parseOpenSkyCredentials(await file.text());
    field("opensky_client_id").value = id;
    const secretInput = field("opensky_client_secret");
    form.querySelector('input[data-clear="opensky_client_secret"]').checked = false;
    secretInput.disabled = false;
    secretInput.value = secret;
    show(`Imported OpenSky client <b>${esc(id)}</b> from ${esc(file.name)}. Press <b>Save</b> to check it with OpenSky and apply it.`, "ok");
  } catch (err) {
    show(esc(err.message), "bad");
  }
});

// ---------- editing ----------

form.addEventListener("input", (e) => {
  const input = e.target;
  if (input.dataset.kind) {
    values[input.name] = input.value === "" ? null : input.valueAsNumber / factor(input.dataset.kind);
  } else if (input.name === "home_lat" || input.name === "home_lon") {
    const lat = field("home_lat").valueAsNumber, lon = field("home_lon").valueAsNumber;
    // Typing: move the marker without rewriting the box being typed in.
    if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) moveHome(lat, lon);
  }
});

form.addEventListener("change", (e) => {
  const name = e.target.name;
  if (e.target.dataset.clear) {
    // Ticking "Clear" empties and disables that secret's box.
    const input = field(e.target.dataset.clear);
    input.disabled = e.target.checked;
    if (e.target.checked) input.value = "";
  }
  if (name === "distance_unit" || name === "altitude_unit") {
    values[name] = e.target.value;
    renderUnits();
    loadStations().catch(console.warn);
  }
});

function collect() {
  const boards = [...document.querySelectorAll("#boards input:checked")].map((c) => c.value);
  const watch = [field("watch_a").value, field("watch_b").value].filter(Boolean);
  const round = (v, d) => (v == null ? null : +v.toFixed(d));
  return {
    home_name: field("home_name").value.trim(),
    home_lat: +field("home_lat").value,
    home_lon: +field("home_lon").value,
    distance_unit: field("distance_unit").value,
    altitude_unit: field("altitude_unit").value,
    speed_unit: field("speed_unit").value,
    aircraft_colours: field("aircraft_colours").value,
    radius_km: round(values.radius_km, 2),
    alert_radius_km: round(values.alert_radius_km, 2),
    alert_min_altitude_ft: round(values.alert_min_altitude_ft, 0),
    train_radius_km: round(values.train_radius_km, 2),
    train_alert_seconds: field("train_alert_seconds").valueAsNumber,
    train_boards: boards.join(","),
    train_watch: watch.join(","),
    passing_window: field("passing_window").checked,
    passing_minutes: field("passing_minutes").valueAsNumber,
    buses_enabled: field("buses_enabled").checked,
    bus_radius_km: round(values.bus_radius_km, 2),
    bus_min_zoom: +field("bus_min_zoom").value,
    bus_delays: field("bus_delays").checked,
    bus_disruptions: field("bus_disruptions").checked,
    bus_region: field("bus_region").value,
    roads_enabled: field("roads_enabled").checked,
    road_radius_km: round(values.road_radius_km, 2),
    readsb_enabled: field("readsb_enabled").checked,
    readsb_url: field("readsb_url").value.trim(),
    online_feeds: field("online_feeds").value,
    nrod_username: field("nrod_username").value.trim(),
    opensky_client_id: field("opensky_client_id").value.trim(),
    contact: field("contact").value.trim(),
    ...secretChanges(),
  };
}

// Only send a secret when it's being replaced or cleared; otherwise the server keeps it.
function secretChanges() {
  const out = {};
  for (const input of form.querySelectorAll("input[data-secret]")) {
    const clear = form.querySelector(`input[data-clear="${input.name}"]`).checked;
    if (clear) out[input.name] = "";
    else if (input.value) out[input.name] = input.value;
  }
  return out;
}

function show(text, kind = "") {
  const el = document.getElementById("message");
  el.className = kind;
  el.innerHTML = text;
}

function errorText(body) {
  // FastAPI validation errors come back as a list of {loc, msg}: open the page of the first one.
  if (Array.isArray(body.detail)) {
    if (body.detail[0]?.loc) showFieldPage(String(body.detail[0].loc.at(-1)));
    return body.detail.map((d) => `${esc(d.loc.at(-1))}: ${esc(d.msg)}`).join("<br>");
  }
  return esc(body.detail || "Something went wrong");
}

async function send(method, body) {
  const resp = await fetch("/api/settings", {
    method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body),
  });
  if (resp.status === 401) {
    signedOut();
    throw new Error("Signed out: sign in again, then save.");
  }
  const data = await resp.json();
  if (!resp.ok) throw new Error(errorText(data));
  return data;
}

function applied(data) {
  loaded = data;
  values = { ...data.values };
  render();
  const notes = (data.notes || []).map((n) => `<br>${esc(n)}`).join("");
  show(data.restarted ? `Saved and applied. <a href="/">Back to the map</a>${notes}` : "No changes to save.", "ok");
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const watch = [field("watch_a").value, field("watch_b").value].filter(Boolean);
  if (watch.length === 1 || (watch.length === 2 && watch[0] === watch[1])) {
    showPage("trains");
    show("Choose two different stations for “Trains past home”, or none.", "bad");
    return;
  }
  if (document.querySelectorAll("#boards input:checked").length > 4) {
    showPage("trains");
    show("Choose at most 4 departure boards.", "bad");
    return;
  }
  const button = document.getElementById("save");
  button.disabled = true;
  show("Checking and saving…");
  try {
    applied(await send("PUT", collect()));
  } catch (err) {
    show(err.message, "bad");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("reset").addEventListener("click", async () => {
  if (!confirm("Forget everything changed here and go back to the values in .env?")) return;
  show("Resetting…");
  try {
    applied(await send("DELETE"));
  } catch (err) {
    show(err.message, "bad");
  }
});

// ---------- backup and restore ----------

const backupSecrets = document.getElementById("backup-secrets");

function renderBackup() {
  // Logins can only go in a backup when an admin password protects downloading it.
  backupSecrets.disabled = !loaded.admin_password_set;
  if (backupSecrets.disabled) backupSecrets.checked = false;
  document.getElementById("backup-secrets-hint").textContent = loaded.admin_password_set
    ? "The file will hold your passwords and keys in plain text: keep it somewhere safe."
    : "Needs an admin password (ADMIN_PASSWORD in .env), so nobody else on the network can download them. "
      + "Without logins, a restore keeps the current ones.";
}

document.getElementById("backup").addEventListener("click", async () => {
  try {
    const resp = await fetch(`/api/settings/backup?secrets=${backupSecrets.checked}`);
    if (resp.status === 401) return signedOut();
    if (!resp.ok) throw new Error(errorText(await resp.json()));
    const name = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") || "")?.[1] || "settings.json";
    const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await resp.blob()), download: name });
    link.click();
    URL.revokeObjectURL(link.href);
    show(`Backup saved as ${esc(name)}${backupSecrets.checked ? ", including logins: keep it safe" : ""}.`, "ok");
  } catch (err) {
    show(err.message, "bad");
  }
});

document.getElementById("restore").addEventListener("click", () => document.getElementById("restore-file").click());

document.getElementById("restore-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // choosing the same file again should work too
  if (!file) return;
  let backup;
  try {
    backup = JSON.parse(await file.text());
  } catch {
    show(`${esc(file.name)} isn't a settings backup (not JSON).`, "bad");
    return;
  }
  const when = backup.exported ? new Date(backup.exported).toLocaleString() : "an unknown date";
  if (!confirm(`Replace the current settings with the backup from ${when}?`)) return;
  show("Restoring…");
  try {
    const resp = await fetch("/api/settings/restore", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(backup),
    });
    if (resp.status === 401) return signedOut();
    const data = await resp.json();
    if (!resp.ok) throw new Error(errorText(data));
    applied(data);
  } catch (err) {
    show(err.message, "bad");
  }
});

// ---------- signing in (when ADMIN_PASSWORD is set) ----------

const signin = document.getElementById("signin");
const signout = document.getElementById("signout");

function signedOut() {
  form.hidden = true;
  signout.hidden = true;
  signin.hidden = false;
  signin.elements.password.focus();
}

signin.addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = document.getElementById("signin-message");
  out.textContent = "Checking…";
  try {
    const resp = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: signin.elements.password.value }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || "Sign-in failed");
    signin.elements.password.value = "";
    out.textContent = "";
    await load();
  } catch (err) {
    out.textContent = err.message;
  }
});

signout.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  signedOut();
});

async function load() {
  try {
    const session = await (await fetch("/api/session")).json();
    if (session.required && !session.signed_in) return signedOut();
    const resp = await fetch("/api/settings");
    if (resp.status === 401) return signedOut();
    loaded = await resp.json();
    values = { ...loaded.values };
    signin.hidden = true;
    form.hidden = false;
    signout.hidden = !session.required;
    render();
    showPage(location.hash.slice(1));
  } catch (err) {
    form.hidden = false;
    show(`Couldn't load settings: ${esc(err.message)}`, "bad");
  }
}

load();
