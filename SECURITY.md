# Security policy

## Supported versions

Security fixes are made for the latest release only. Please check that a problem still happens on the newest version before reporting it.

| Version | Supported |
|---|---|
| 0.1.x | ✅ |

## Reporting a vulnerability

**Please don't open a public issue for security problems.** Report them privately through GitHub:

1. Go to the [Security tab](https://github.com/ahammerton/passerby/security) of this repository.
2. Choose **Report a vulnerability**. You can also go straight to [the report form](https://github.com/ahammerton/passerby/security/advisories/new).

Please include:

- the Passerby version (shown in the startup log and at `/api/config`)
- how it's run: Docker or Python, and behind a reverse proxy or not
- the steps to reproduce the problem, and what an attacker could do with it

You'll get a reply as soon as possible, usually within a week. Once a fix is released, the advisory is published and you're credited, unless you'd rather not be.

## Scope

Passerby is meant to run on a **home network**, or reached over a VPN such as Tailscale. These are in scope:

- changing settings, or reading logins, without the admin password when `ADMIN_PASSWORD` is set
- getting around the sign-in lockout
- cross-site request forgery or DNS rebinding that gets past the built-in protections
- reading API keys or logins (Network Rail, OpenSky, Bus Open Data, National Highways) from the server
- cross-site scripting through feed content: callsigns, airline or airport names, bus stops, road works descriptions and so on
- making the server read or write files outside its data folder
- crashing the server, or making it unusable, with a single request

These are **by design**, and not vulnerabilities:

- **Anyone who can open the map can see it**, including your home location. Only the settings page and backups need the admin password.
- **Without `ADMIN_PASSWORD`, the settings page is open** to anyone on the network. Setting one is recommended.
- **Logins are stored unencrypted** in `data/settings.json`, with file permissions limited to the app's user.
- **Exposing Passerby directly to the internet isn't supported.** Problems that only arise that way are still welcome as reports, but may be treated as hardening rather than as vulnerabilities.
- **The receiver lookup makes requests** to the address you enter on the settings page. It needs the admin password when one is set.

## Security features

Passerby's built-in protections are described in the README's [Security section](README.md#security):

- the admin password and sign-in lockout
- the host check against DNS rebinding, and `ALLOWED_HOSTS`
- same-site checks for changes and the live feed
- the content security policy and other browser hardening
- how logins are stored
