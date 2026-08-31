# Security

## Reporting a vulnerability

**Please do not open a public issue.**

Email **security@donnish.com.au** with:

- what the issue is and roughly how severe you think it is
- steps to reproduce
- affected version or commit

If you'd rather not use email, GitHub's private vulnerability reporting is enabled on this
repository — open the **Security** tab and choose *Report a vulnerability*.

Expect an acknowledgement within a few working days. This is a small side project, not a
funded product — there's no formal SLA, but reports are taken seriously and you'll be
credited unless you'd rather not be.

---

## Threat model

This is a local-only desktop tool. It holds **one read-only API key per configured provider
that needs one**, and every host it can reach is listed below.

This table used to name three endpoints and close with "no other network activity occurs",
which was not true — the NSW network, both Open-Meteo hosts, three IP-geolocation services
and Nominatim were all reachable and none was mentioned. A threat model that under-states
its own network surface is worse than none, because the reader stops looking. It is now
generated from the source and **held there by a test**: `test_contracts.py` extracts every
hostname in the shipped modules and fails if this document does not name it, so a provider
or a lookup added later cannot arrive unlisted.

### Hosts the code contacts

| Host | What for | When | What is sent |
|---|---|---|---|
| `api.purpleair.com` | PurpleAir sensor readings | Every poll, per configured `purpleair` source. Also on discovery and when a source is probed | Sensor index and the `X-API-Key` header. Discovery sends a bounding box derived from your coordinates. A privately-registered sensor's `read_key` goes in the query string, so it lands in their access log |
| `airquality.des.qld.gov.au` | Queensland regulatory readings | Every poll, per configured `qld` source; the station list once per process and on discovery | Station id, PM2.5's parameter id, a date window. **No key and no coordinates** — the station list is fetched whole and filtered on your machine |
| `data.airquality.nsw.gov.au` | NSW and ACT regulatory readings | Every poll, per configured `nsw` source; the site list on discovery | A POST body naming the site id, PM2.5 and a date window. **No key and no coordinates**, filtered locally as above |
| `api.openaq.org` | OpenAQ aggregated readings | Every poll, per configured `openaq` source; discovery during setup | Sensor id and the `X-API-Key` header. **Discovery sends your exact coordinates** plus a radius — the most precise disclosure any provider receives |
| `api.open-meteo.com` | The hour's wind, temperature, humidity and pressure — the cause the readings are the effect of | Every poll, once a location is set and while `capture_weather` is on. Also `--forecast`, and the timezone lookup if you use it | Coordinates and nothing else. No key. The timezone lookup deliberately rounds to two decimals (~1 km); the rest use four |
| `archive-api.open-meteo.com` | The same hours, for dates older than the forecast API carries | Weather backfill only, for anything more than six days back | Coordinates and a date range |
| `nominatim.openstreetmap.org` | Turning a place you typed into coordinates | Only when you type an address in the wizard or the settings page. The "enter coordinates instead" path never calls it | The text you typed |
| `ipwho.is`, `ipapi.co`, `freeipapi.com` | Approximate location from your IP, offered as a shortcut | Setup only, and only if you choose auto-detect. Tried in that order, first success wins, so the later two fire only when the one before fails | Nothing in the request — but the request itself discloses your public IP and that this address runs Airo. Plain HTTP is refused in code: a network attacker who could forge the reply would be choosing which monitors you are offered |

Every one of those is HTTPS. The only plaintext HTTP anywhere in the codebase is the
dashboard server talking to `127.0.0.1`, which never leaves the machine. There is **no
telemetry, no analytics, no update check and no crash reporting**, and no provider's base
URL is configurable — they are class constants, so a tampered config cannot redirect
readings to a host of someone else's choosing.

### Hosts the code names but never contacts

Listed because "we only talk to these" is checkable only if the near-misses are named too.

| Host | Why it appears |
|---|---|
| `develop.purpleair.com`, `explore.openaq.org` | Where to get that provider's API key. Opened in **your** browser if you say yes to the offer, otherwise only printed |
| `open-meteo.com` | The CC BY attribution link served beside the weather data. A link, not a request |
| `github.com` | This project's own URL, carried in the User-Agent and in the `Documentation=` line of the generated systemd unit |
| `rustup.rs` | Named in the error message shown when the tray binary has not been built |
| `www.sqlite.org` | A citation in `store.py`'s header comment |

**The dashboard loads nothing from any third party** — it
was fetching Chart.js from a CDN, which handed that CDN the IP of a machine viewing its own
home air quality and gave a third party arbitrary code in a page rendering those coordinates.
It is now a local canvas renderer. The only remaining external reference in any surface is the
PurpleAir attribution link ToS §4.8 requires, which is a link and fires only if clicked.

**In scope:** API key disclosure, local privilege escalation via the installer or the
scheduled task, the local web server being reachable off-machine, code execution through
malformed API responses, and SQL injection into the local database.

**Out of scope:** anything requiring an attacker who already has your user account; the
providers' own infrastructure; the accuracy of sensor data.

---

## How secrets are handled

| Control | Implementation |
|---|---|
| **Storage** | `~/.airo/<provider>.key`, mode `600`, directory `700` |
| **Deliberately outside the repo** | So it can't be committed, synced or copied with the project |
| **Never in the config** | Keys are not read from config at all as of v0.5 — except a **private** PurpleAir sensor's `read_key`, which belongs to one source rather than to a network and so lives inside `config.json`, which is why the settings API reports keys as presence only |
| **Config outside the repo** | Settings live in `~/.airo/config.json` (mode 600), not in the working tree — a config holds a location and chosen sensors, which is personal data. CI fails if `config.json` is ever tracked |
| **Transmission** | `X-API-Key` header over HTTPS — never a query string, which would land in logs. The `read_key` above is the exception, because PurpleAir accepts it only as a query parameter: it lands in their access log, as the host table says. Omitted entirely for keyless providers |
| **Logging** | Never logged, printed or included in error output. `--status` and `poller.py --doctor` report only *whether* a key was found, and the file's permissions |
| **Backstop** | `.gitignore` excludes `apikey`, `*.key`, `.env`, `secrets.json`; CI fails the build if any is tracked or if a UUID-shaped key appears in source |

Resolution order, per provider: `$PURPLEAIR_API_KEY` / `$OPENAQ_API_KEY` env var →
`~/.airo/<provider>.key` → (PurpleAir only) the pre-v0.5 `~/.airo/apikey`.

Providers declaring `needs_key = False` are never asked for one and never sent a header.

### Windows: keys are restricted with ACLs, not file modes

`os.chmod(path, 0o600)` does almost nothing on Windows — it toggles the read-only
attribute, and access is governed by ACLs. Calling it and reporting success would
give an impression of protection the platform is not delivering.

So on Windows, `poller.secure_path()` calls **`icacls`** — which ships with the OS,
so this costs no dependency:

```
icacls <path> /inheritance:r /grant:r <you>:F
```

That drops inherited permissions and grants only the current account, which is the
closest equivalent to `0600` Windows offers. Directories get `(OI)(CI)F` so new
files inherit the restriction.

Every credential-bearing path goes through it: `~/.airo/` itself, each
`<provider>.key`, `config.json`, and any backup archive created with
`--include-keys`.

**If the restriction cannot be applied, you are told.** `secure_path()` returns
whether it succeeded, and callers warn rather than continuing silently — a key
file that merely looks protected is worse than one you know is not.
`poller.py --status` reports the real state by reading the ACLs back, not by
assuming the call worked.

---

## Network exposure

The dashboard server:

- binds **`127.0.0.1` only** — never `0.0.0.0`, so it is not reachable from your network
- runs **only on demand**, started by `poller.py --open` and stopped with
  `pkill -f "poller.py --serve"`
- serves static files from the project directory with `Cache-Control: no-store`
- exposes a read-only JSON API over `GET` (`/api/latest`, `/api/sources`, `/api/series`,
  `/api/settings`, `/api/indoor`) and a **writing** API over `POST`. This document claimed
  until August 2026 that there were no write endpoints, which stopped being true when the
  settings page replaced the terminal wizard: `POST` now reaches `/api/settings`,
  `/api/keys`, `/api/backup/export`, `/api/backup/restore`, `/api/choose-folder`,
  `/api/sources/probe`, `/api/sources/discover`, `/api/geocode` and `/api/timezone`. A
  reader deciding whether it is safe to expose this port was being given the wrong picture
- authenticates every `POST` with a **per-process token**, because "not reachable
  off-machine" was never the same as "not reachable by any page in your browser" — a
  loopback server is reachable by every site you have open. `do_POST` runs the whole chain
  *before* it routes, so a refused request tells an attacker nothing about which paths
  exist: loopback `Host` → `Origin` (an absent or loopback one; `null` is refused) →
  `Content-Type: application/json`, which denies a cross-origin caller the CORS
  simple-request path and forces a preflight this server never answers → `X-Airo-Token`,
  compared with `hmac.compare_digest`. The token is `secrets.token_urlsafe(32)`, minted per
  process, never written to disk, and embedded only in the page this server itself served
- as of v0.5, **refuses to start when the port is already in use** rather than leaving two
  servers running. A stale server from another directory serves old data and stale HTML,
  which is indistinguishable from a dead agent

`python3 poller.py --doctor` reports whether anything is listening. The expected resting state is nothing.

> If you change `serve_port` binding to expose it deliberately, you are on your own — the
> `GET` API has no authentication at all, and it serves your location history.

---

## Privileges

- Installs a **user**-scoped scheduled task on every platform — a `LaunchAgent` on macOS, a
  `systemd --user` timer on Linux, a per-user Task Scheduler entry on Windows. Never a system
  daemon
- No `sudo` or elevation anywhere in the install path
- Nothing in the scheduled task resolves a program through `PATH`. This document used to
  say the plist "sets a minimal explicit `PATH`"; it does not set one at all, which reads
  as a missing control and is not — launchd does not inherit your shell environment, and
  every `ProgramArguments` entry is an absolute path, so there is no lookup to hijack
- No code in this repository signs anything. This document previously said the generated
  `.app` bundle was ad-hoc signed with `codesign -s -`; that was true of a shell installer
  deleted in August 2026, and no call to `codesign` has existed since. Assume **no
  signature** unless you verify one yourself — and note that an ad-hoc signature would not
  have been a trust signature anyway, since it confers no verification of origin

## Data

Everything stays on your machine: `~/.airo/data/airo.db`, `~/.airo/data/latest.json`, `~/.airo/data/*.log`.

The database is **not encrypted**. A location-tagged air quality history is **personal data**
— it reveals where you live and, by inference, when you're home. Think before attaching an
export to a bug report.

- `data/` and `export/` are gitignored, and CI fails the build if either is ever tracked.
- PurpleAir's Terms of Service §4.3 independently prohibit redistributing their data. See
  [LICENSING.md](LICENSING.md).

All database access uses parameterised queries. The one value interpolated into SQL is the
bucket size in `store.series()`, which is cast to `int` first.

Logs are checked not to contain a key, but review them before sharing regardless.

## Supply chain

The Python side has **no runtime dependencies** — standard library only. This is a deliberate
security property as much as a maintenance one, and CI enforces it with an AST check over
every import plus a guard against dependency manifests appearing.

The optional Rust tray in `tray/` is the one exception. It is a separate binary that a user
must build deliberately, it makes no network calls at all, and it only reads
`~/.airo/data/latest.json`. Its dependencies (Tauri, serde, tokio) are conventional and lockfile-pinned.

There is no external asset in the Python path. The dashboard's charting is a local canvas
renderer implementing only what its two charts use; the previous CDN dependency was removed
rather than pinned with Subresource Integrity, because SRI would have fixed the tampering
risk while leaving the IP disclosure and the offline failure untouched.

`tests/test_contracts.py` enumerates every user-facing surface from disk and fails if any of
them loads a third-party subresource, so a UI added later is covered without anyone
remembering to add it.

## Supported versions

Pre-1.0: only `main` is supported. Please confirm an issue against latest `main` before
reporting.
