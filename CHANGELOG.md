# Changelog

All notable changes to Passerby are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
