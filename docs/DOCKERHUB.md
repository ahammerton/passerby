# Passerby

**Live planes, trains and buses passing your home.** A self-hosted web app that
shows aircraft (anywhere in the world), and trains, buses and road closures (UK),
around your home on a live map. It alerts you when something flies overhead or a
train is about to pass, and keeps a history of what it has seen.

![Passerby around Heathrow](https://raw.githubusercontent.com/ahammerton/passerby/main/docs/screenshots/map-light.png)

**Full documentation, source and issues: [github.com/ahammerton/passerby](https://github.com/ahammerton/passerby)**

## Features

- **Live map** of aircraft from [adsb.lol](https://adsb.lol), the [OpenSky Network](https://opensky-network.org) and/or your own ADS-B receiver (readsb, tar1090, dump1090). Icons by type, airliners in their airline's colours with logos.
- **Overhead alerts**, and **interesting aircraft**: emergency squawks, notable types (Beluga, B-52, Spitfire…), types rarely seen near you, and military aircraft.
- **UK trains** from Network Rail's open data: every train nearby, departure boards, and alerts for trains about to pass your house.
- **Buses** (England) from the Bus Open Data Service, **road closures** from National Highways, **rain radar** from RainViewer.
- **History** of every aircraft seen, a **settings page** with backup and restore, and an optional admin password.

## Quick start

```sh
docker run -d --name passerby --restart unless-stopped \
  -p 8000:8000 -v passerby-data:/data \
  -e HOME_LAT=51.4700 -e HOME_LON=-0.4543 \
  -e ADMIN_PASSWORD=choose-one \
  cyrune/passerby:latest
```

Open `http://<server-ip>:8000`. Set your own location with `HOME_LAT` and `HOME_LON`,
or on the settings page (⚙). Logins for the optional feeds (Network Rail, Bus Open Data,
National Highways, OpenSky) can be entered there too.

With Docker Compose, use the repository's
[`docker-compose.yml`](https://github.com/ahammerton/passerby/blob/main/docker-compose.yml)
and [`.env.example`](https://github.com/ahammerton/passerby/blob/main/.env.example).

## Tags

| Tag | What it is |
|---|---|
| `latest` | The main branch |
| `1.2.3`, `1.2`, `1` | Releases (see the [changelog](https://github.com/ahammerton/passerby/blob/main/CHANGELOG.md)) |
| `sha-<commit>` | One exact build |

Images are built for **amd64** and **arm64** (Raspberry Pi 4/5). The same images are
published as `ghcr.io/ahammerton/passerby`.

## Data

Everything the app keeps (history, caches, settings and logins) is in `/data`. Use a
named volume as above. To use a folder instead, create it owned by uid 1000 (the app's
user) first, e.g. `mkdir passerby-data && sudo chown 1000:1000 passerby-data`: a folder
Docker creates itself is owned by root, and the app can't write to it. Treat the data,
and its backups, as private.

Run Passerby on your home network or behind a VPN such as Tailscale. Don't expose it to
the internet: the map, including your home location, needs no login.

## Licence

[Apache License 2.0](https://github.com/ahammerton/passerby/blob/main/LICENSE). The app
shows data from third-party services, each with its own terms: see the
[README](https://github.com/ahammerton/passerby#third-party-data-and-services).
