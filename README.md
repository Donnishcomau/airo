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

*Both images use **synthetic demo data**, not a real location — build the same thing yourself
with `python3 tools/demo.py --into /tmp/airo-demo --serve`. The evening pattern above is the
effect Airo exists to find: air that is unremarkable as a daily average and several times worse
for the hours you are actually home.*

---

## What you need

A Mac with an Apple Silicon chip — that is any Mac sold since about 2020, the ones with an M1,
M2, M3 or M4 processor. To check: click the Apple menu at the top-left of your screen, choose
**About This Mac**, and look at the **Chip** line.

Nothing else. No accounts, and nothing to install first.

> **On Windows or Linux?** There are downloads for you on the releases page — a `.msi`
> for Windows, a `.deb` and an `.AppImage` for Linux. They are **built automatically but
> not yet tested by anyone on real hardware**, so if one does not work, the command-line
> install described at the end does, and telling us about it genuinely helps.
>
> **On an older Intel Mac?** Airo runs fine, but there is no installer for it — use
> [If you are comfortable with a terminal](#if-you-are-comfortable-with-a-terminal).

---

## Installing it

### 1. Download it

Go to the [releases page](https://github.com/Donnishcomau/airo/releases) and download the
file whose name starts with **`Airo_`** and ends with **`.dmg`** — the rest of the name is
the version, so it changes with every release. It is around 40 MB.

### 2. Open the file you downloaded

Double-click that `.dmg` file in your Downloads folder. A small window opens showing the Airo icon
next to a shortcut to your Applications folder.

### 3. Drag Airo onto Applications

That is the whole install. You can then close the window and eject `Airo` from the sidebar of
any Finder window.

### 4. Open it — right-click the first time, not double-click

This step is fiddly, and it is not something you did wrong.

Airo has not yet been registered with Apple, so macOS assumes the worst about it and shows a
warning that sounds like a virus alert. You only have to get past it once.

- Open your **Applications** folder
- **Right-click** Airo (or hold Control and click it), and choose **Open** from the menu
- A box says macOS "cannot verify the developer". Click **Open**

> **If you double-clicked instead**, you get a message saying Airo cannot be opened because it
> is from an unidentified developer, and the only button is *Cancel*. Nothing is broken. Close
> it and use right-click → **Open**.

**What you should see:** a small coloured dot appears in the menu bar along the top of your
screen, and Airo's settings window opens. That window is where everything is configured —
there is no terminal at any point.

---

## Setting it up

The settings window opens by itself the first time. You can always get back to it: click the
dot in the menu bar, then **Setup → Settings**.

It has six panels, and you can stop after the first two.

### Location and display

Type your address in **Your address** — a street address, your suburb, or a postcode, whatever
you know — and click **Find**. Airo looks it up and shows the matches; click **Use this** on
the right one and your coordinates fill themselves in. Then click **Save**.

Check the full address on each match before picking. A bare postcode can match places in
several countries, which is why the whole address including the country is shown.

Airo uses this to work out which monitors are near you and to rank them by distance. It is
stored on your computer. The only thing that leaves your machine is the text you typed, which
goes to OpenStreetMap to be turned into coordinates — if you would rather it did not, type the
latitude and longitude in directly and skip the lookup.

This panel also chooses which scale the number is shown on — Australian AQI, US EPA AQI, or the
raw measurement. Australian is the sensible default in Australia.

### Sources

This is the important one: *which monitors to read.*

Under **Find monitors near you**, click **Search**. Airo looks for air quality monitors around
your location and shows the **nearest five**, with how far away each is, whether it is
reporting right now, and which ones Airo suggests. Click **Add** on the ones you want, then
**Save changes**. There is a **Show all** button if you want the full list — near a city there
can be forty or more.

**Add at least one. Two is much better.** When two independent instruments agree, the number
means something. When they disagree, that is worth knowing too — and it is often the most
interesting thing on the screen.

Some networks need a free account and some need nothing at all:

| Network | Account needed? | Covers |
|---|---|---|
| **Queensland Government** | **No — nothing to do** | Queensland |
| **NSW Government** | **No — nothing to do** | NSW and the ACT |
| OpenAQ | Yes, free | Worldwide, wherever a national network publishes |
| PurpleAir | Yes, free | Worldwide, wherever someone has installed a sensor |

**If you live in Queensland or New South Wales, start with the government feed.** It needs no
account, the instruments are professionally calibrated, and you can add more later. Airo hides
networks that cannot reach your location, so you will not be offered the Queensland feed in
Tasmania.

### Account keys

Only needed for OpenAQ or PurpleAir. Skip this panel entirely if you are using a government
feed.

A "key" is a long string of letters and numbers the network gives you when you sign up. It is
how they know the requests are coming from you.

1. Open the sign-up page and create the free account:
   - **PurpleAir** — [develop.purpleair.com](https://develop.purpleair.com)
   - **OpenAQ** — [explore.openaq.org/register](https://explore.openaq.org/register)
2. The site shows you your key. Copy it.
3. Back in Airo's settings, paste it into the box next to that network in the **Account keys**
   panel, and click **Save**.

Airo writes it to a file only your account can open, and never shows it again — the box just
says "set". If you ever need to change it, type the new one over the top.

> **A note on PurpleAir.** Those sensors are bought and maintained by ordinary people, not by a
> government agency. Their terms do not allow you to republish the readings, so keep anything
> you export to yourself.

### Alerts

Airo can notify you when the air gets worse. Out of the box it tells you when the index passes
**67** — the bottom of the "Fair" band, roughly 17 µg/m³ (see [what the number
means](#what-the-number-means) below).

You can raise or lower that, turn alerts off entirely, and set quiet hours so it does not wake
you at 3am.

### Data

Where your readings are kept, how much space they use, and how long to keep them. The default
is to keep everything forever — it is about a megabyte a year per monitor, and a record of what
you breathed cannot be recreated once it is deleted.

### Backup and restore

Covered under [Your readings](#your-readings) below.

---

## Using it day to day

**The menu bar** shows the current reading and a coloured dot. Click it and the menu tells you:

- the number, the band it falls in, and which way it is heading
- every monitor you are reading, how far away it is and how old its last reading is
- whether the other monitors agree with the highest one

The menu also has **Fetch a new reading** if you do not want to wait for the next poll, and
**Open dashboard in browser** for the full picture.

**The dashboard** shows the last day, week or year as a chart, each source compared against the
others, an evening-by-evening heatmap, and a table of your worst nights — the two images at the
top of this page.

**The settings window** covers everything: where you are, which monitors to read, account keys,
alerts, storage and backup.

![The settings window: an address box that looks up your coordinates, the sources you read, the
scale, and which reading wins when sources disagree](docs/screenshots/settings.jpg)

**The menu bar** carries the current reading, so you can see it without opening anything.
Click it for the detail:

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

Printed rather than photographed, because a menu-bar item is a few lines of text and a
screenshot of one is less useful than the text: this can be searched, read aloud by a screen
reader, and checked against what the program actually prints. It is the real output of
`airo-tray --print-menu` against the same synthetic demo data as the images above — every
name and number invented. Reproduce it with:

```bash
python3 tools/demo.py --into /tmp/airo-demo --days 30
AIRO_DATA=/tmp/airo-demo/data ./tray/target/release/airo-tray --print-menu
```

Notice it names *both* sources with their distances. Two instruments a kilometre apart
routinely disagree, and the tray shows you that rather than picking one and hiding the
argument.

### What the number means

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

The underlying measurement is **µg/m³** — micrograms of particles per cubic metre of air. On
the Australian scale the index is simply that number multiplied by four, so 25 µg/m³ (the
national standard) is an index of 100.

### When to distrust a reading

Airo is deliberately honest about this rather than showing you a confident number:

- **One monitor reading high while its neighbours are calm** is usually a fire next door or a
  blocked air inlet, not the air across the suburb. Airo flags this and says so plainly — it
  never hides the reading, because if there *is* a fire next door that is genuinely the air you
  are breathing.
- **Cheap sensors over-read in humid weather**, typically by 20–40%. Airo tells you which kind
  of instrument each reading came from.
- **A reading several hours old is not current air.** Airo shows the age of every source and
  marks the stale ones.
- **With only one source there is nothing to cross-check against.** Adding a second is the
  single most useful thing you can do.

### Why the headline is a 10-minute average

The instantaneous number from a particle counter jumps around a great deal. A passing car, a
neighbour lighting a barbecue, or someone shaking out a tea towel near the inlet can move it
sharply for a few seconds. None of those describe the air you will be breathing in an hour.

So the headline is the **10-minute average**, and the raw instantaneous value is shown beside
it rather than instead of it. Ten minutes is long enough to average out a passing event and
short enough to notice smoke arriving. Where a source publishes no 10-minute figure — most
government monitors report hourly — Airo says which one it is using rather than quietly
substituting a different kind of number.

This is also why the rolling averages are shown together. A 10-minute reading well above the
hour is air that is getting worse; well below it is air that is clearing. The direction is
often more useful than the value.

### Whose air is it, though?

Most people will read somebody else's sensor, because most people do not own one. That is a
reasonable thing to do and it comes with a caveat worth stating plainly: **a monitor a few
kilometres away may not describe your air at all.**

Three things matter more than distance:

- **Elevation and terrain.** Cold air drains downhill after sunset and pools in the low ground,
  carrying particulates with it. A sensor at the top of a valley and one at the bottom can
  differ by a factor of several on exactly the nights that matter most. If you are in a gully,
  a hilltop reading is optimistic about your air.
- **What is between you and it.** A monitor on the far side of a main road, an industrial
  block, or a bushfire front is measuring something different from what is outside your window.
- **Height and siting of the instrument.** A sensor on a roof reads differently from one at
  head height in a courtyard. Consumer sensors near a wall, under an eave, or beside a dryer
  vent read their own microclimate.

What to do about it: prefer the nearest monitor that is on similar ground to you, add a second
one from a different network, and watch how they behave overnight. If two nearby sources
consistently disagree by a lot, that difference is information about your local terrain rather
than a fault in either instrument — and it is the reason Airo shows every source with its
distance rather than picking one and hiding the argument.

The most local reading available is one at your own house, which is what a consumer sensor is
genuinely good for even though it is less accurate than a regulatory monitor. Accuracy and
relevance are different things.

---

## Your readings

Everything stays on your computer, in a hidden folder called `.airo` in your home directory.
Airo never uploads anything. **Data → Reveal data folder** in the menu opens it in Finder if
you want to see it.

Your settings live there too, separately from the app — so removing and reinstalling Airo does
not lose anything.

### Backing up, or moving to a new Mac

In settings, open the **Backup and restore** panel:

1. Click **Browse…** and choose where to put it. An external drive or a cloud folder is fine.
2. Click **Export now**.

You get a single file containing your settings and every reading you have ever taken.

On the new machine, install Airo, open the same panel, point **Restore** at that file, and
click **Examine it**. Airo shows you what is inside — how many readings, from when, which
monitors — before it changes anything. Only then does it offer to replace your setup.

Your account keys are **left out of the backup unless you tick the box**, because backups end
up on memory sticks and in cloud folders in a way a settings file does not. Airo always tells
you whether a given backup contains keys.

---

## When something looks wrong

**"No reading yet" in the menu bar.** Airo has not managed to read anything. Open settings and
check that at least one source is listed under **Sources**. If you added one in the last few
minutes, wait until the next poll — it runs every fifteen minutes.

**One source says "stale".** That network has stopped publishing new readings. Usually it comes
back by itself within an hour or two. If it does not, the monitor may have been taken out of
service — search again in settings and add a different one.

**The number seems wrong.** Check how many sources you have. With one, there is nothing to
cross-check against, and a single blocked sensor can read four times the real value. Adding a
second source is the fix.

**Nothing has updated for hours.** Your Mac was probably asleep. Airo notices the gap on the
next poll and fills it in from each network's own history, so nothing is lost — give it a few
minutes after waking.

**A network needs a key and you no longer have it.** Sign in to that network's site and
generate a new one; pasting it into the **Account keys** panel replaces the old one.

**Something else.** The menu has **Background agent → Health check…**, which tests every part
of the chain and reports what it found in plain words.

---

## Removing Airo

Click the dot in the menu bar and choose **Quit Airo**, then drag Airo from your Applications
folder to the Bin.

**Your readings are not deleted by that.** They stay in the `.airo` folder in your home
directory, so if you reinstall later you pick up exactly where you left off.

If you want everything gone, including the readings and the background schedule, use
**Setup → Settings** first and take a backup you can keep — then delete the `.airo` folder from
your home directory.

---

## If you are comfortable with a terminal

Everything above is also available from the command line, and on **Intel Macs** it is the
only way — there is no installer for them.

**Windows and Linux** do have installers on the releases page, built by CI on every tag,
but **nobody has verified them on real hardware** — no one working on Airo has a Windows
or Linux machine to install them on. The command-line route below is the tested one.
[ROADMAP §3f](ROADMAP.md) tracks what it would take to call them supported.

Airo is Python with **no dependencies at all** — standard library only, enforced in CI. There
is no `pip install` and no build step. The menu-bar tray is a separate, optional Rust program.

```bash
git clone https://github.com/Donnishcomau/airo.git
cd airo
python3 setup.py                     # asks where you are, finds monitors near you
python3 poller.py --once             # take the first reading
python3 scheduler.py install         # poll every 15 minutes in the background
```

Python 3.9 or newer. It ships with macOS, is standard on Linux, and comes from
[python.org](https://python.org) on Windows.

Common commands:

```bash
python3 poller.py --status           # health and per-source row counts
python3 poller.py --list-sources     # providers available, and sources configured
python3 poller.py --open settings    # the same settings page, in a browser
python3 poller.py --serve            # dashboard on 127.0.0.1
python3 poller.py --backfill 365     # pull a year of history for every source
python3 poller.py --export           # one plain CSV per source
python3 poller.py --where            # where config and data live, size, retention
python3 poller.py --doctor           # test every source end to end
python3 poller.py --verify           # check the database for corruption
python3 poller.py --uninstall        # remove the schedule; readings are kept

python3 backup.py create             # one archive: config + all readings
python3 backup.py inspect FILE       # what is inside, and whether it holds keys
python3 backup.py restore FILE       # put it back

python3 setup.py --keys              # review networks, add accounts and keys
python3 setup.py --prefs             # change preferences only
python3 scheduler.py install|uninstall|status|start|stop|restart

python3 analyse.py evening --nights 30   # evening-premium analysis
python3 analyse.py agreement --by-hour   # how your sources compare, for tuning
```

The settings page and the wizard share one validator, so the two cannot disagree about what a
valid setting is.

### Where things are kept

```
~/.airo/config.json        your location, sources and preferences
~/.airo/data/airo.db       every reading, forever
~/.airo/<provider>.key     account keys, mode 600
```

Deliberately outside the checkout, so a re-clone or a moved folder cannot take years of
readings with it, and so a file holding your location can never be committed by accident.
`--where` prints the resolved paths.

The database is SQLite — one of five storage formats the US Library of Congress
[recommends for datasets](https://www.sqlite.org/locrsf.html) — and `--export` writes plain
CSV, round-trip tested in CI, so your history is never locked in. Why SQLite rather than the
CSV this project started with, including the benchmarks that decided it:
[ARCHITECTURE §2.5a](ARCHITECTURE.md).

### Why several sources

A single sensor tells you what one instrument thinks. Two tell you something much more useful.
Two sources at one location, one still evening in the demo install above — the same two
instruments the screenshots show, at the moment pictured:

| Source | Distance | PM2.5 | Australian AQI | Band |
|---|---|---|---|---|
| Consumer sensor | 1.1 km | **24.5 µg/m³** | 98 | **Fair** |
| Government monitor | 9.3 km | 5.7 µg/m³ | 23 | Very good |

Same city, same minute, **four times the particulate**. That gap *is* the valley effect, and it
is why the default rule picks the nearest instrument. Had it picked the government monitor, the
dashboard would have cheerfully said "Very good" to someone who should have been closing their
windows.

When sources disagree, `fusion.rule` in the config decides the headline: `nearest` (the
default), `freshest`, `all`, or `blend`. All of them skip sources that are stale or flagged
faulty, judged against each source's own cadence. `blend` reports a value no instrument
measured, and is labelled as computed wherever it appears.

### Catching false positives

One sensor screaming while every neighbour is calm needs explaining. Airo checks the instrument
against itself (a PurpleAir has two laser counters; when they disagree by more than 2× the
sensor is faulty, not the air), against its neighbours (more than 3× the median), and against
its own history at the same hour over the last 90 days — because a valley sensor that *always*
reads 3× after sunset is measuring something real.

Flagged readings are **shown, never hidden**. But every surface says plainly that the
neighbours do not see it.

### Adding a country

One class in `poller.py` and nothing else. `QldProvider` is the reference implementation for a
direct government feed — copy it.

### Building the installer

Needs Rust and the Tauri CLI:

```bash
python3 tools/fetch_runtime.py       # the Python interpreter that ships inside the app
python3 tools/stage_bundle.py        # assemble exactly what ships
cd tray && cargo tauri build
```

Run the tests before changing anything:

```bash
python3 -m unittest discover -s tests -v
```

### Known limitations

- **Low-cost optical sensors over-read at high humidity**, typically 20–40%. Treat readings as
  trends, not calibrated truth.
- **Some readings are not real.** Anything above 350 µg/m³ is flagged `suspect` and excluded
  from aggregates and from fusion — but stored, never dropped.
- **Date logic assumes no daylight saving.** Correct in Queensland, off by an hour twice a year
  elsewhere. [ROADMAP #5](ROADMAP.md).
- **The download is not yet signed by Apple**, which is why the first launch needs a
  right-click. [ROADMAP §3f](ROADMAP.md).
- **There is one widget, not three.** The tray is the only supported menu-bar surface; the
  macOS-only plugins were removed in 0.5.0.

---

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — how it works, the decisions, and the traps that have already bitten
- [CONVENTIONS.md](CONVENTIONS.md) — the rules this project holds itself to, and why each one exists
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, testing, PR process
- [ROADMAP.md](ROADMAP.md) — what needs doing, and a review of PurpleAir's terms
- [RESEARCH.md](RESEARCH.md) — the evidence base, with citations
- [SECURITY.md](SECURITY.md) — key handling and vulnerability disclosure
- [LICENSING.md](LICENSING.md) — the AGPL, the commercial option, and the third-party data terms

Contributions welcome — this is deliberately small and hackable. Issues tagged
`good first issue` are a good place to start, once the tracker has some.

---

## Data attribution and disclaimer

**Powered by [PurpleAir](https://www.purpleair.com)**

> Underlying air quality data utilized by Airo is sourced from PurpleAir, a network of consumer
> sensors. Sensors are owned and managed by consumers, therefore PurpleAir does not guarantee
> data accuracy. All insights and claims are solely made by Airo.

This wording is required by PurpleAir's Terms of Service §7.3 and their
[Attribution Guide](https://www.purpleair.com/attribution). If you fork this project,
substitute your own product name — do not remove it.

Queensland and NSW government data: **CC BY 4.0.** OpenAQ's licence varies per station, which
is why Airo shows it per source. Airo displays the attribution each install actually owes,
based on the sources in use.

### Do not publish retrieved data

PurpleAir's ToS §4.3 prohibits redistributing their data. **Do not commit `data/` to a public
repository** — it is gitignored, and CI fails if it appears. This also protects you: a
location-tagged air quality history reveals where you live and when you are home.

Full detail, including the per-source OpenAQ position: [LICENSING.md](LICENSING.md).

### Not medical advice

Airo reports readings from instruments of varying accuracy, some of which over-read in humid
weather and can fail in ways that look like real data. It is **not medical advice** and not a
medical device, and it cannot tell you whether the air is safe for you. Do not use it for
health or safety-critical decisions. If air quality is affecting your health, speak to a
doctor.

---

## Licence

**AGPL-3.0-or-later** — see [LICENSE](LICENSE). Copyright © 2026 Donnish Pty Ltd.

Self-hosting is free and unrestricted. If you need different terms — embedding Airo in a
proprietary product, or running a modified version as a service without publishing your
changes — a commercial licence is available. See [LICENSING.md](LICENSING.md).
