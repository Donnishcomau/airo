# Airo

**Airo tells you what the air is like where you live, and keeps a record of it.**

It checks air quality monitors near you every fifteen minutes, keeps every reading on your own
computer, and shows you one number in your menu bar. Nothing is uploaded anywhere — the only
connections it makes are to the monitoring networks you choose.

It was built to answer one question: *why the tight chest on winter evenings?* The
answer was visible in the data, and this is the tool that made it visible.

![The Airo dashboard: a headline reading, rolling averages, every source with its distance
and age, and the worst nights on record](docs/screenshots/dashboard-overview.jpg)

![A day of history with the 3pm–1am window shaded, and an evening heatmap showing each hour of
each night](docs/screenshots/evening-pattern.jpg)

*Both images use **synthetic demo data**, not a real location. The evening pattern above is the
effect Airo exists to find: air that is unremarkable as a daily average and several times worse
for the hours you are actually home.*

---

## Download and install

Everything is on the [releases page](https://github.com/Donnishcomau/airo/releases). Take the
file for your machine — the version is part of the name, so it changes with every release. There
are no accounts and nothing to install first, and every download has a `.sha256` beside it if you
want to check what you got.

**None of the builds are signed yet**, so every operating system warns you the first time you
open one. That is the fiddly part, it is not something you did wrong, and you only do it once.

### macOS — `Airo_*.dmg`, about 38 MB

Apple Silicon only: any Mac with an M1, M2, M3 or M4 chip, which is most Macs sold since about
2020. To check, click the Apple menu at the top-left of your screen, choose **About This Mac**,
and look at the **Chip** line. Intel Macs have no installer — use
[install from source](#install-from-source).

1. Double-click the `.dmg` in your Downloads folder. A window opens showing the Airo icon next to
   a shortcut to your Applications folder.
2. Drag Airo onto Applications. That is the whole install — you can close the window and eject
   `Airo` from the sidebar of any Finder window.
3. Open your **Applications** folder, **right-click** Airo (or hold Control and click it), and
   choose **Open**. A box says macOS "cannot verify the developer". Click **Open**.

Right-click matters here. If you double-click instead, you get a message saying Airo cannot be
opened because it is from an unidentified developer, and the only button is *Cancel*. Nothing is
broken: close it and use right-click → **Open**.

### Windows — `Airo_*.msi`, about 44 MB

1. Download the `.msi` and double-click it.
2. Windows says "Windows protected your PC" and offers only **Don't run**. Click **More info**,
   then **Run anyway**.
3. Follow the installer through.

### Linux — `Airo_*.deb`, about 249 MB, or `Airo_*.AppImage`, about 164 MB

On Debian, Ubuntu and the distributions built on them, download the `.deb` and open it with your
software installer, or from a terminal in the folder you downloaded it to run
`sudo apt install ./Airo_*.deb`.

Anywhere else, download the `.AppImage`, make it executable with `chmod +x Airo_*.AppImage`, and
run it. Nothing is installed system-wide; the file *is* the application.

> **The Windows and Linux builds are produced by CI and have not been installed by anyone on real
> hardware.** They are published so you have something to try and so build breakage is caught
> early. If one does not work, [install from source](#install-from-source) is the tested route,
> and telling us about it genuinely helps.
> [ROADMAP 3f](ROADMAP.md#3f-signing-and-installers-nobody-has-opened--blocked-on-the-maintainer)
> tracks what it would take to call them supported.

**What you should see:** a small coloured dot appears in the menu bar or system tray, and Airo's
settings window opens. That window is where everything is configured — there is no terminal at
any point.

---

## Setting it up

The settings window opens by itself the first time. You can always get back to it: click the dot
in the menu bar, then **Setup → Settings**. It has six panels, and you can stop after the first
two.

**Location and display.** Type a street address, suburb or postcode into **Your address** and
click **Find**. Check the full address on each match before picking one — a bare postcode can
match places in several countries — then click **Use this** and **Save**. Airo uses this to find
monitors near you and to rank them by distance, and keeps it on your computer. The only thing
that leaves your machine is the text you typed, which goes to OpenStreetMap to be turned into
coordinates; if you would rather it did not, type the latitude and longitude in directly. This
panel also chooses the scale: Australian AQI, US EPA AQI, or the raw measurement.

**Sources.** The important one: *which monitors to read.* Under **Find monitors near you**, click
**Search**. Airo shows the nearest five, with how far away each is, whether it is reporting right
now, and which ones it suggests. Click **Add** on the ones you want, then **Save changes**.
**Show all** gives you the full list — near a city there can be forty or more.

**Add at least one. Two is much better.** When two independent instruments agree, the number
means something. When they disagree, that is worth knowing too.

| Network | Account needed? | Covers |
|---|---|---|
| **Queensland Government** | **No — nothing to do** | Queensland |
| **NSW Government** | **No — nothing to do** | NSW and the ACT |
| OpenAQ | Yes, free | Worldwide, wherever a national network publishes |
| PurpleAir | Yes, free | Worldwide, wherever someone has installed a sensor |

In Queensland or New South Wales, start with the government feed: no account, professionally
calibrated instruments, and you can add more later. Airo hides networks that cannot reach your
location, so you will not be offered the Queensland feed in Tasmania. **Outside Australia**, use
OpenAQ or PurpleAir and set the scale to US EPA AQI.

**Account keys.** Only for OpenAQ or PurpleAir — skip this panel on a government feed. Create the
free account at [develop.purpleair.com](https://develop.purpleair.com) or
[explore.openaq.org/register](https://explore.openaq.org/register), copy the key the site gives
you, paste it into the box next to that network, and click **Save**. Airo writes it to a file
only your account can open and never shows it again — the box just says "set".

> **A note on PurpleAir.** Those sensors are bought and maintained by ordinary people, not by a
> government agency. Their terms do not allow you to republish the readings, so keep anything you
> export to yourself.

**Alerts.** Airo can notify you when the air gets worse. Out of the box it tells you when the
index passes **67** — the bottom of the "Fair" band, roughly 17 µg/m³. You can raise or lower
that, turn alerts off, and set quiet hours so it does not wake you at 3am.

**Data**, and **Backup and restore**, are covered under [your readings](#your-readings) below.

---

## Using it day to day

**The menu bar** carries the current reading and a coloured dot, so you can see it without
opening anything. Click it and the menu names the number and its band, every monitor you are
reading with its distance and the age of its last reading, and whether the others agree with the
highest one. It also has **Fetch a new reading**, if you do not want to wait for the next poll,
and **Open dashboard in browser**.

```
  🟢  31 · Very good   (Australian AQI)
  Demo Valley
  7.7 µg/m³ · via Demo Valley Sensor · 1.0 km · just now
  ---
  Sources
       7.7 µg ·  1.1 km ·   1 min   Demo Valley Sensor
       5.0 µg ·  9.3 km ·   1 min   Demo Reference Monitor
  ---
  Rolling averages
       31    7.7 µg   Now
       31    7.7 µg   10 min
  Risk window active — keep filtering
```

That is the real output of `airo-tray --print-menu` against synthetic demo data — every name and
number invented. Notice it names *both* sources with their distances: two instruments a kilometre
apart routinely disagree, and the tray shows you that rather than picking one and hiding the
argument.

**The dashboard** shows the last day, week or year as a chart, each source compared against the
others, an evening-by-evening heatmap, and a table of your worst nights — the two images at the
top of this page.

**The settings window** covers everything: where you are, which monitors to read, account keys,
alerts, storage and backup.

![The settings window: an address box that looks up your coordinates, the sources you read, the
scale, and which reading wins when sources disagree](docs/screenshots/settings.jpg)

The tray is the only menu-bar surface Airo has; the macOS-only plugins that preceded it were
removed in 0.6.0.

---

## What the number means

Airo shows an air quality index — a single number that stands in for a measurement of fine
particles in the air. On the Australian scale:

| Index | Band | What it means |
|---|---|---|
| 0–33 | Very good | Nothing to think about |
| 34–66 | Good | Nothing to think about |
| 67–99 | Fair | People with asthma or heart conditions may want to take it easy outdoors |
| 100–149 | Poor | Reduce prolonged outdoor exertion |
| 150–200 | Very poor | Avoid outdoor exertion; close windows |
| Over 200 | Hazardous | Stay indoors |

The underlying measurement is **µg/m³** — micrograms of particles per cubic metre of air. On the
Australian scale the index is simply that number multiplied by four, so 25 µg/m³, the national
standard, is an index of 100.

A number is only as good as the instrument and the place it came from.
**[docs/measuring.md](docs/measuring.md)** covers the rest: whether a monitor a few kilometres
away describes your air at all, why the headline is a 10-minute average, why a second source is
the most useful thing you can add, how Airo tells a real event from a faulty sensor, and when to
distrust a reading.

---

## Your readings

Everything stays on your computer, in a hidden folder called `.airo` in your home directory. Airo
never uploads anything. **Data → Reveal data folder** in the menu opens it.

```text
~/.airo/config.json        your location, sources and preferences
~/.airo/data/airo.db       every reading, forever
~/.airo/<provider>.key     account keys, mode 600
```

Your settings live there too, separately from the app, so removing and reinstalling Airo does not
lose anything. The **Data** panel shows how much space the readings use and how long to keep
them; the default is forever, at about a megabyte a year per monitor. The database is SQLite —
one of five storage formats the US Library of Congress
[recommends for datasets](https://www.sqlite.org/locrsf.html) — and the export writes plain CSV,
round-trip tested in CI, so your history is never locked in.

**To back up, or move to a new machine**, open the **Backup and restore** panel, click
**Browse…** to choose where to put the file, and click **Export now**. You get a single file
holding your settings and every reading you have ever taken. On the new machine, install Airo,
point **Restore** at that file and click **Examine it** — Airo shows you how many readings, from
when and from which monitors before it changes anything.

Your account keys are **left out of the backup unless you tick the box**, because backups end up
on memory sticks and in cloud folders in a way a settings file does not. Airo always tells you
whether a given backup contains keys.

**Removing Airo** does not delete your readings. Quit from the menu and remove the application;
the `.airo` folder stays, so reinstalling later picks up where you left off. If you want
everything gone, take a backup you can keep and then delete that folder.

---

## When something looks wrong

**"No reading yet" in the menu bar.** Airo has not managed to read anything. Open settings and
check that at least one source is listed under **Sources**. If you added one in the last few
minutes, wait for the next poll — it runs every fifteen minutes.

**One source says "stale".** That network has stopped publishing. Usually it comes back by itself
within an hour or two. If it does not, the monitor may have been taken out of service — search
again in settings and add a different one.

**The number seems wrong.** Check how many sources you have. With one there is nothing to
cross-check against, and a single blocked sensor can read four times the real value. Adding a
second source is the fix.

**Nothing has updated for hours.** Your machine was probably asleep. Airo notices the gap on the
next poll and fills it in from each network's own history, so nothing is lost.

**A network needs a key and you no longer have it.** Sign in to that network's site and generate
a new one; pasting it into **Account keys** replaces the old one.

**Something else.** The menu has **Background agent → Health check…**, which tests every part of
the chain and reports what it found in plain words.

---

## Install from source

The command line is the tested route on Windows and Linux, and the only route on Intel Macs.
Airo is Python with **no dependencies at all** — standard library only, enforced in CI. There is
no `pip install` and no build step. The menu-bar tray is a separate, optional Rust program.

```bash
git clone https://github.com/Donnishcomau/airo.git
cd airo
python3 setup.py                     # asks where you are, finds monitors near you
python3 poller.py --once             # take the first reading
python3 scheduler.py install         # poll every 15 minutes in the background
```

Without git, use the green **Code** button on
[the repository page](https://github.com/Donnishcomau/airo) and choose **Download ZIP**, then
unpack it and carry on from `cd airo`.

Python 3.9 or newer. It ships with macOS, is standard on Linux, and comes from
[python.org](https://python.org) on Windows.

| Command | What it does |
|---|---|
| `python3 poller.py --status` | health and per-source row counts |
| `python3 poller.py --serve` | dashboard on 127.0.0.1 |
| `python3 poller.py --open settings` | the same settings page, in a browser |
| `python3 poller.py --backfill 365` | pull a year of history for every source |
| `python3 poller.py --export` | one plain CSV per source |
| `python3 poller.py --where` | where config and data live, size, retention |
| `python3 poller.py --doctor` | test every source end to end |
| `python3 backup.py create` | one archive: config and all readings |
| `python3 analyse.py evening --nights 30` | the evening-premium analysis |

`--help` on any of them lists the rest. Building the installers, running the test suite and
adding a data source are covered in [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Documentation

- [docs/measuring.md](docs/measuring.md) — what a reading means, and when not to trust one
- [ARCHITECTURE.md](ARCHITECTURE.md) — how it works, the decisions, and the traps that have already bitten
- [CONVENTIONS.md](CONVENTIONS.md) — the rules this project holds itself to, and why each one exists
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, testing, PR process
- [ROADMAP.md](ROADMAP.md) — what needs doing, and a review of PurpleAir's terms
- [RESEARCH.md](RESEARCH.md) — the evidence base, with citations
- [SECURITY.md](SECURITY.md) — key handling and vulnerability disclosure
- [LICENSING.md](LICENSING.md) — the AGPL, the commercial option, and the third-party data terms

Contributions welcome — this is deliberately small and hackable.

---

## Data attribution and disclaimer

**Powered by [PurpleAir](https://www.purpleair.com)**

> Underlying air quality data utilized by Airo is sourced from PurpleAir, a network of consumer
> sensors. Sensors are owned and managed by consumers, therefore PurpleAir does not guarantee
> data accuracy. All insights and claims are solely made by Airo.

This wording is required by PurpleAir's Terms of Service §7.3 and their
[Attribution Guide](https://www.purpleair.com/attribution). If you fork this project,
substitute your own product name — do not remove it.

Queensland and NSW government data: **CC BY 4.0.** OpenAQ's licence varies per station, which is
why Airo shows it per source. Airo displays the attribution each install actually owes, based on
the sources in use.

**Do not publish retrieved data.** PurpleAir's ToS §4.3 prohibits redistributing it, so do not
commit `data/` to a public repository — it is gitignored, and CI fails if it appears. This also
protects you: a location-tagged air quality history reveals where you live and when you are home.
Full detail, including the per-source OpenAQ position: [LICENSING.md](LICENSING.md).

**Not medical advice.** Airo reports readings from instruments of varying accuracy, some of which
over-read in humid weather and can fail in ways that look like real data. It is **not medical
advice** and not a medical device, and it cannot tell you whether the air is safe for you. Do not
use it for health or safety-critical decisions. If air quality is affecting your health, speak to
a doctor.

---

## Licence

**AGPL-3.0-or-later** — see [LICENSE](LICENSE). Copyright © 2026 Donnish Pty Ltd.

Self-hosting is free and unrestricted. If you need different terms — embedding Airo in a
proprietary product, or running a modified version as a service without publishing your changes —
a commercial licence is available. See [LICENSING.md](LICENSING.md).
