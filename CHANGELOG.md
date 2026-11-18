# Changelog

All notable changes to Passerby are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-11-18

### Fixed

- **adsb.lol's daily limit.** adsb.lol now allows 500 requests a day for each internet connection,
  and polling every 30 seconds used that up within hours. It's now checked every 200 seconds by
  default, and anything under 180 is raised to 180. If the day's requests still run out, Passerby
  waits until adsb.lol says they reset instead of retrying, and the top bar shows
  "adsb.lol (paused until HH:MM)". OpenSky's daily credit limit is handled the same way.

### Added

- **Space stations and satellites.** The International Space Station and Tiangong move live on
  the map with their next orbit. The side panel lists the passes you can see from home, with where
  to look and whether each one fades into Earth's shadow, plus tonight's best bright-satellite
  passes. An alert comes 5 minutes before a station is visible. Orbits come from CelesTrak, with no
  key needed; it can be switched off under Settings → Space.
- adsb.lol can be switched on or off, and its interval changed, under Settings → Aircraft.

## [0.3.0] - 2026-11-18

### Added

- A **History** page (the clock button next to Settings): totals, aircraft per day, the busiest
  hours, top airlines, aircraft types and routes, interesting aircraft, an overhead log, trains past
  home and ship visits, for today, 7 days, 30 days, a year or everything.
- Sightings now record the aircraft's route, airline, model and why it was interesting, and trains
  passing home are logged. Existing databases are upgraded automatically.

### Changed

- **What to show** has moved from the box on the map to a **☰ menu** at the top left. Trains and
  railway lines are now one item (**Trains**), as are aircraft and airports (**Aircraft**).
- Switching something off now hides it everywhere: on the map, in the tables under it, in the side
  panel (including Departures and Rail disruptions for trains) and in the Today counts.
- The ✈ 🚆 icons before the Passerby name are gone.
- Alerts follow the menu too: nothing switched off pops up a notice or a browser notification.
- On narrow screens the table tabs are tighter, and the filter box moves to its own row instead
  of being cut off.
- New README screenshots.
- The Docker image now runs on **Python 3.15** (was 3.12). The tests also run on 3.15.

## [0.2.1] - 2026-11-17

### Added

- The **version** is shown after the Passerby name at the top of the page.

### Changed

- **Trains on the map** are square badges in the train company's colour (charcoal for freight), with the
  headcode in departure-board digits, an arrow for the way it's heading along the track, and a tab for
  how it's running: green on time, amber a few minutes late, red ten or more. Zoomed out beyond the
  starting view, the headcode is left off so the badges stay small.
- Dependencies: stomp.py 9.0.0 (the Network Rail feed; checked against the live feed), pytest 9.2.0.

## [0.2.0] - 2026-11-17

### Added

- **Ships**, from AIS via aisstream.io (optional; free API key). A 🚢 Ships map layer with icons by type
  that point the way each ship faces, a Ships tab with flag, status, destination and size, and details
  with the ship's recent track. Ships are moved on between reports, small boats are hidden unless turned
  on, and ship details are kept in `data/ships.db`.
- **Interesting ships**: cruise ships, warships, giant ships (300 m or longer), a few famous ships and
  emergency beacons get a ring on the map, a badge and a place in the side panel, with alerts (all,
  emergencies only, or none) when one is under way.
- **Port names** for ship destinations given as UN/LOCODEs: `BEANR>GBDAG` reads "Antwerpen → Dagenham".
- **Ship history**: visits are logged, so a ship says how often it has been seen here, and `/api/ships/history`
  lists recent visits. The Today panel counts vessels.
- **Rail disruptions** (optional; free Rail Data Marketplace key): incidents and engineering works at your
  stations with National Rail's advice, each train company's status, station notices on the departure
  boards, and an alert when a new disruption starts. Works with or without live trains.
- **Your own AIS receiver** (optional): standard NMEA from AIS-catcher or similar, over UDP
  (`AIS_RECEIVER=udp://0.0.0.0:10110`) or TCP (`tcp://host:port`), alongside aisstream.io or without it.
  Ships it hears are marked 📡.

### Changed

- The **Weather tab** is readable now: temperature, wind, visibility, cloud, rain or fog, pressure and when it was observed,
  in your chosen units, with the airport's name. A **Show raw METAR** switch shows the original reports too.

### Fixed

- Airport weather stopped loading: aviationweather.gov now rejects coordinates with long decimals.

## [0.1.2] - 2026-11-16

### Added

- A **home button** under the map's zoom buttons, which takes the map back to the starting view around your home.

## [0.1.1] - 2026-10-30

### Added

- **Interesting aircraft**, in a new "Interesting now" list and marked on the map:
  - **Emergencies** (squawk 7500, 7600 or 7700): a pulsing red ring and an immediate alert.
  - **Notable types** (Beluga, Dreamlifter, An-124, B-52, warbirds, airships and more): a gold ring and an alert.
  - **Types rarely seen here:** airliners, turboprops and military types seen on 3 or fewer airframes in your last 90 days of history.
  - **Military aircraft:** listed, but not alerted.

  Alerts can be set to all, emergencies only, or off, under Settings → Aircraft.
- **Docker Hub:** images are also published as `cyrune/passerby`.

### Changed

- **`docker-compose.yml` keeps the app's data in a named volume** (`passerby_data`) instead of `./data`.
  A `./data` folder that Docker created itself was owned by root, so the app couldn't write to it.
  To keep using a folder, create it owned by uid 1000 before the first start: see the README.
  **Upgrading with a `./data` folder that already works?** Keep `./data:/data` in your compose file,
  or your history and settings will seem to disappear (they're still in `./data`).

## [0.1.0] - 2026-10-30

The first public release.

### Added

- **Live aircraft map** (Leaflet and OpenStreetMap), updated over a WebSocket every 2 seconds, with smooth movement between updates.
  - **Aircraft sources:** adsb.lol, the OpenSky Network, and your own ADS-B receiver (readsb, tar1090 or dump1090).
  - **Icons by type:** jets, turboprops, private jets, light aircraft, helicopters, military jets, gliders, balloons, drones and ground vehicles.
  - **Airline colours and logos:** airliners are drawn in their airline's colours, with its logo. Everything else is coloured by altitude.
  - **Aircraft details:** type, registration and route from adsbdb, plus photos from planespotters.net.
- **Overhead alerts:** an in-page toast, plus browser notifications over HTTPS.
- **UK trains** from Network Rail's open data feeds.
  - Every train near home, freight included, following the railway track.
  - Departure boards and calling points.
  - **Passing home:** the trains about to pass your house, with alerts.
- **Buses** (England) from the Bus Open Data Service, with delays worked out from the timetable, and bus disruptions.
- **Road closures and incidents** (England) from National Highways.
- **Rain radar** from RainViewer, METAR weather, and nearby airports.
- **History** of every aircraft seen, kept in SQLite.
- **Settings page,** with backup and restore, and an optional admin password.
- **Security:**
  - a host check against DNS rebinding
  - same-site checks for changes and the live feed
  - a content security policy
- **Docker image** for amd64 and arm64, published to `ghcr.io/ahammerton/passerby`.
