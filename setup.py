#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Airo setup — point it at where you live, and at instruments near you.

Run once:

    python3 setup.py

It asks where you are, finds monitoring sites near that point across every
supported network, lets you choose which to use, and writes your settings to
~/.airo/config.json.

Two deliberate choices worth knowing about:

  * Settings live OUTSIDE the repository. A config file holds your location and
    the sensor you have chosen -- personal data. Keeping it out of the working
    tree means it cannot be committed by accident and a contributor never
    receives it in a clone.

  * API keys are never written to the config. They go in ~/.airo/<provider>.key
    with mode 600, so the config stays safe to share when reporting a bug.

Standard library only.

How it fits the whole
---------------------
This is the terminal path to the same settings the browser page edits, and the
two are kept honest by sharing one validator. The prompts here refuse bad input
as it is typed, but `write_config()` then runs the whole result through
`poller.validate_settings()` before saving -- so the final check is the same
one the settings page applies. That last step is a check on *the wizard*, not
on the user: reaching it with errors means the prompts and the validator
disagree about what is valid, which is why it raises rather than warns.

A rule implemented here instead of there would let the wizard accept something
the page rejects, or the reverse -- and whichever a user hit would look like a
bug in the other.

It borrows more than the validator. Discovery, reporting probes, the
recommendation, key paths and the config path all live in poller.py; this file
is the interview, not the logic. Anything here that starts making a judgement
about air quality or about what a valid value is belongs over there.

Since the installer landed, most people will never see this. It remains for
people who prefer a terminal, for headless installs, and because `--keys`,
`--prefs` and `--profile` are genuinely faster than clicking.

Why the questions come in this order
------------------------------------
The sequence is not cosmetic; each step exists where it does because putting it
elsewhere made the flow worse:

  1. Location first, because every later step is filtered or ranked by it.
     Three ways to give it, and the wizard says plainly what each one
     discloses -- a place name goes to Nominatim, IP detection reveals an
     address to a geolocation service, and typed coordinates tell nobody
     anything.
  2. Networks before credentials. Asking for an account mid-search interrupts
     someone with paperwork for a network they may not even want, and makes the
     tool feel like it is demanding rather than offering. Choose first, then
     set up only what was chosen.
  3. Sites, with distance and instrument type shown, and the nearest reference
     monitor paired with the nearest consumer sensor rather than the two
     closest -- see `poller.recommend()` for why.
  4. Scale, suggested from the coordinates already given.
  5. Preferences last, because they are the only step with sensible defaults
     for everyone and so the only one safe to hurry through.

What it assumes
---------------
  * **A controlling terminal.** Checked before anything is asked, not partway
    through: piping into setup once produced a complete config of defaults and
    a cheerful "Done". `--keys` is the one exception, degrading to a read-only
    listing, because knowing which networks need an account is useful even
    where nothing can be typed.
  * **Every network endpoint is https.** The IP-lookup replies decide which
    monitors a user is offered, and over plain http an attacker who answers
    first chooses them.
  * **Nothing here is destructive without --force.** An existing config is
    never overwritten silently.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import poller
import units  # noqa: E402

def config_dir():
    """The directory the user's settings and keys live in, resolved when asked
    rather than when imported.

    These were module constants, which froze the *developer's* home at import:
    a test redirecting HOME afterwards still got the real one, and setup writes
    a config file. Nothing had driven that path yet, so nothing had broken —
    but `backup.py` had the identical shape and the suite duly wrote archives
    into the real `~/.airo/backups` and rotated the genuine ones away.

    Found by the contract added after that, which walks every shipped module
    for a home-relative path resolved at import. This is the one it caught.

    Derived from the config path rather than assumed to be `~/.airo`, because
    `$AIRO_CONFIG` may put it somewhere else and the wizard must write where
    the poller will read.
    """
    return config_path().parent


def config_path():
    """Where the wizard writes the settings — poller's own resolver, asked late.

    This hardcoded `~/.airo/config.json` and so ignored `$AIRO_CONFIG`, which
    `poller.config_path()` honours: with that variable set, setup wrote a file
    the poller never read and told the user it had worked. Late binding is
    still deliberate (see `config_dir()`); `poller.config_path()` resolves at
    call time too, so nothing is frozen by importing this module.
    """
    return poller.config_path()


BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m")


def say(msg=""):
    print(msg)


def head(msg):
    print(f"\n{BOLD}{msg}{RESET}")


#: poller's glyphs, not our own literals. Its `_console_safe()` degrades to
#: ASCII where the console cannot encode a tick; a tick written out here
#: raises UnicodeEncodeError on a cp1252 Windows console and takes the wizard
#: down at its first success message. CONVENTIONS "Console encoding is not
#: universal"; this file had its own copies without the fallback.
def ok(msg):
    print(f"  {GREEN}{poller.TICK}{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}{poller.WARN}{RESET} {msg}")


def bad(msg):
    print(f"  {RED}{poller.CROSS}{RESET} {msg}")


class NoTerminal(Exception):
    """Raised when input runs out mid-flow.

    Silently substituting defaults would write a plausible-looking config for
    a location nobody chose, and report success. A wrong config that claims to
    have worked is worse than a refusal.
    """


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        raise NoTerminal(
            "input ended unexpectedly — setup needs an interactive terminal")
    return got or (default or "")


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    got = ask(f"{prompt} ({d})", "").lower()
    if not got:
        return default
    return got.startswith("y")


# ------------------------------------------------------------------- profile

def load_existing():
    """Read the current config if there is one, so partial edits keep the rest."""
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def key_state(provider):
    """(has_key, human_status) for one provider, without revealing the key."""
    if not provider.needs_key:
        return True, "no account needed"
    if poller.get_api_key({"provider": provider.slug}):
        return True, "key set"
    return False, "needs a free account"


def save_key(slug, key):
    """Store a network key through poller's writer, and say what happened.

    This module had its own copy, which differed in three ways that mattered:
    it did not lowercase the slug (so a key saved as "PurpleAir" was invisible
    to `poller.key_path`, which lowercases when reading), it wrote an empty
    file instead of removing the key when handed a blank, and it reported
    success from `secure_path`'s return value rather than reading the mode
    back — which on Windows is the difference between a key file that looks
    protected and one that is. `poller.save_key()` does all three correctly.
    """
    path, restricted = poller.save_key(slug, key)
    if restricted is False:
        warn(f"could not restrict {path} to your account — check its permissions")
    return path


def choose_profile(existing=None):
    head("Your profile")
    say(f"  {DIM}Only used to greet you and to label exports. Stored locally.{RESET}")
    say()
    current = ((existing or {}).get("profile") or {}).get("name") or ""
    name = ask("Your name (optional)", current)
    return {"name": name}


def manage_keys(interactive=True):
    """Review every network's account status and offer to set up the missing ones.

    Separate from the setup wizard on purpose. Accounts are an ongoing concern,
    not a first-run question: networks get added, keys get rotated, and someone
    who skipped a signup during setup needs an obvious way back. Nothing here
    ever prints a key.
    """
    head("Networks and accounts")
    say(f"  {DIM}Airo reads these networks. Keyless government feeds work straight")
    say(f"  away; the rest need a free, read-only account.{RESET}")
    say()

    rows = []
    for slug, prov in sorted(poller.PROVIDERS.items()):
        has, status = key_state(prov)
        mark = f"{GREEN}ready{RESET}" if has else f"{YELLOW}not set up{RESET}"
        say(f"  {BOLD}{prov.label}{RESET}  [{mark}]")
        say(f"    {DIM}{prov.tier} · {prov.resolution_minutes} min · {prov.accuracy_note}{RESET}")
        say(f"    {DIM}licence: {prov.licence}{RESET}")
        if prov.needs_key:
            say(f"    {DIM}status: {status} · sign up: {prov.key_url}{RESET}")
            if has:
                say(f"    {DIM}key file: {poller.key_path(slug)} (mode 600){RESET}")
        else:
            say(f"    {DIM}status: {status}{RESET}")
        say()
        rows.append((slug, prov, has))

    missing = [(slug, prov) for slug, prov, has in rows if not has]
    if not missing:
        ok("Every network is set up.")
        return []

    if not interactive:
        return [slug for slug, _ in missing]

    say(f"  {len(missing)} network(s) need an account before Airo can read them.")
    say(f"  {DIM}Each is free and read-only. Airo never writes anything back.{RESET}")
    say()

    added = []
    for slug, prov in missing:
        if not ask_yes(f"Set up {prov.label} now?", True):
            continue
        say(f"    Sign up: {prov.key_url}")
        if ask_yes("Open that page in your browser?", True):
            try:
                poller.launch_browser(prov.key_url)
                ok("opened — create the account, then copy the read key")
            except Exception:
                warn("couldn't open a browser; use the link above")
        key = ask(f"Paste your {slug} key (blank to skip)")
        if key:
            path = save_key(slug, key)
            ok(f"saved to {path} (mode 600, outside the project)")
            added.append(slug)
        else:
            warn(f"skipped {slug}")
    return added


# ------------------------------------------------------------------ location

# Keyless IP-geolocation services, tried in order. Every entry MUST be https:
# the request reveals that this address is running Airo, and the reply is the
# user's approximate home location. Over plain http both are readable by
# anyone on the path, and the reply is trivially forgeable -- an attacker who
# can answer first chooses which monitors the user is offered.
#
# Each entry is (label, url, extractor). The extractor returns
# (lat, lon, city, region, country) or None.
IP_LOOKUP_SERVICES = [
    ("ipwho.is", "https://ipwho.is/?fields=success,city,region,country,latitude,longitude",
     lambda d: (d["latitude"], d["longitude"], d.get("city"),
                d.get("region"), d.get("country")) if d.get("success") else None),
    ("ipapi.co", "https://ipapi.co/json/",
     lambda d: (d["latitude"], d["longitude"], d.get("city"),
                d.get("region"), d.get("country_name")) if d.get("latitude") else None),
    ("freeipapi.com", "https://freeipapi.com/api/json",
     lambda d: (d["latitude"], d["longitude"], d.get("cityName"),
                d.get("regionName"), d.get("countryName")) if d.get("latitude") else None),
]


def locate_by_ip():
    """Best-effort location from the public IP address.

    Accurate to the city at best -- often the ISP's exchange rather than you --
    so it is offered as a starting point to confirm, never used silently.
    Returns None if no service can say.

    Several services are tried because a free keyless endpoint disappearing or
    rate-limiting is routine, and one that is down should not cost the user
    their automatic location.
    """
    for name, url, extract in IP_LOOKUP_SERVICES:
        if not url.startswith("https://"):        # belt and braces; see above
            continue
        # Derived from poller.VERSION, which is the canonical one. Written out
        # as "0.5" here, it went on identifying this as 0.5 for the whole of
        # 0.5.0 — a version string that lies is worse than none, since it is
        # what a rate-limit policy asks for.
        req = urllib.request.Request(
            url, headers={"User-Agent": f"airo-setup/{poller.VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8", errors="replace"))
            got = extract(d)
        except Exception:
            continue
        if not got:
            continue
        lat, lon, city, region, country = got
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        label = ", ".join(x for x in (city, region, country) if x)
        return {"name": city or "Home", "latitude": lat, "longitude": lon,
                "label": label, "service": name}
    return None


# Geocoding lives in poller.py so the wizard and the settings page cannot
# disagree about what a place is called or where it is. This wrapper keeps the
# wizard's call sites unchanged and returns the normalised shape.
geocode = poller.geocode


def choose_location():
    head("1. Where are you?")
    say(f"  {DIM}Used to rank nearby monitors and to label the UI. Stored on your")
    say(f"  machine, never sent with a reading.{RESET}")
    say()
    say(f"  {DIM}Three ways, and they disclose different things:{RESET}")
    say(f"  {DIM}  · a place name is sent to OpenStreetMap Nominatim to be")
    say(f"  {DIM}    turned into coordinates — they see what you typed;{RESET}")
    say(f"  {DIM}  · automatic detection reveals your IP to a geolocation")
    say(f"  {DIM}    service, and nothing else;{RESET}")
    say(f"  {DIM}  · typing coordinates directly sends nothing to anyone.{RESET}")
    say()

    # Offer an IP guess as a starting point. It is frequently the ISP's
    # exchange rather than the user, so it is always confirmed, never assumed.
    services = ", ".join(n for n, _, _ in IP_LOOKUP_SERVICES)
    say(f"  {DIM}Automatic detection asks a public IP-geolocation service "
        f"({services})")
    say(f"  over HTTPS. That reveals your IP address to them and nothing else — "
        f"no")
    say(f"  reading, no sensor, no name. Skip it and type a place or "
        f"coordinates instead.{RESET}")
    say()
    if ask_yes("Try to detect your location automatically?", True):
        guess = locate_by_ip()
        if guess:
            say(f"  {DIM}Looks like: {guess['label']} ({guess['latitude']:.3f}, "
                f"{guess['longitude']:.3f}){RESET}")
            say(f"  {DIM}IP lookups often land on your ISP rather than you, so check "
                f"it is roughly right.{RESET}")
            if ask_yes("Use this?", True):
                name = ask("A short name for this place", guess["name"])
                loc = {"name": name,
                       "latitude": guess["latitude"],
                       "longitude": guess["longitude"],
                       "_lookup": "ip"}
                ok(f"{name} — {loc['latitude']:.4f}, {loc['longitude']:.4f}")
                return loc
        else:
            warn("Automatic lookup didn't work. Enter it manually.")
        say()

    # Escalate rather than repeat. The same one-line error printed forever is
    # how this loop behaved: pressing Enter at the prompt reprinted "a location
    # is needed" indefinitely, with no hint that 'coords' or Ctrl-C existed.
    # Someone who does not know the answer needs a different question, not the
    # same one louder.
    attempts = 0
    while True:
        place = ask("Suburb, town or postcode — sent to OpenStreetMap "
                    "(or 'coords' to send nothing, or 'quit')")
        attempts += 1

        if place.lower() in ("quit", "q", "exit", "abort"):
            raise SystemExit("Setup stopped. Nothing was saved. "
                             "Run `python3 setup.py` when you're ready.")

        if not place:
            bad("A location is needed to find nearby monitors.")
            if attempts == 2:
                say(f"  {DIM}Type an address, a suburb or a postcode, or")
                say(f"  'coords' to enter latitude and longitude directly.{RESET}")
            elif attempts >= 3:
                say(f"  {DIM}Stuck? 'quit' leaves setup without saving anything.{RESET}")
                if ask_yes("Try automatic detection from your IP instead?", True):
                    guess = locate_by_ip()
                    if guess:
                        name = ask("A short name for this place", guess["name"])
                        ok(f"{name} — {guess['latitude']:.4f}, "
                           f"{guess['longitude']:.4f}")
                        return {"name": name, "latitude": guess["latitude"],
                                "longitude": guess["longitude"],
                                "_lookup": "ip"}
                    warn("That didn't work either.")
            continue

        if place.lower() in ("coords", "coordinates", "latlon"):
            try:
                lat = float(ask("Latitude"))
                lon = float(ask("Longitude"))
            except ValueError:
                bad("Those weren't numbers.")
                continue
            name = ask("A name for this place", "Home")
            # Tagged so nothing downstream quietly makes a network call on
            # behalf of somebody who chose this path precisely because it
            # makes none. The promise five lines into this screen is that
            # typing coordinates sends nothing to anyone, and a timezone
            # lookup would be a lookup.
            return {"name": name, "latitude": lat, "longitude": lon,
                    "_lookup": "manual"}

        try:
            results = geocode(place)
        except Exception as e:
            warn(f"Lookup failed ({type(e).__name__}). Enter coordinates instead.")
            continue

        if not results:
            bad("No match. Try being more specific, or type 'coords'.")
            continue

        # `geocode()` hands back its own normalised shape -- name, label,
        # latitude, longitude -- precisely so the wizard and the settings page
        # cannot disagree about what a place is called. Reading Nominatim's
        # raw keys here (`display_name`, `lat`, `lon`) went behind that: every
        # row rendered as `?` and the pick below raised `KeyError`. Read the
        # normalised keys, and nothing else.
        say()
        for i, r in enumerate(results, 1):
            say(f"  {i}. {r.get('label', '?')}")
        say()
        pick = ask(f"Which one? (1-{len(results)}, or blank to search again)")
        if not pick:
            continue
        if not pick.isdigit() or not (1 <= int(pick) <= len(results)):
            bad(f"Pick a number from 1 to {len(results)}, or press Enter to "
                f"search again.")
            continue

        chosen = results[int(pick) - 1]
        lat, lon = float(chosen["latitude"]), float(chosen["longitude"])
        short = chosen.get("name") or chosen.get("label", place).split(",")[0]
        name = ask("A short name for this place", short)
        ok(f"{name} — {lat:.4f}, {lon:.4f}")
        return {"name": name, "latitude": lat, "longitude": lon,
                "_lookup": "search"}


# ------------------------------------------------------------------- sources

def ensure_key(provider):
    """Prompt for a provider's key if it needs one and hasn't got one."""
    if not provider.needs_key:
        return True
    if poller.get_api_key({"provider": provider.slug}):
        return True

    say()
    say(f"  {BOLD}{provider.label}{RESET} needs a free account and API key.")
    say(f"  {DIM}Free, read-only, and used only to fetch readings for the sites")
    say(f"  you choose. Airo never writes anything back.{RESET}")
    say(f"  Sign up: {provider.key_url}")

    if ask_yes("Open that page in your browser now?", True):
        try:
            poller.launch_browser(provider.key_url)
            ok("opened — create the account, then copy the read key")
        except Exception:
            warn("couldn't open a browser; visit the link above")

    key = ask(f"Paste your {provider.slug} key (blank to skip this network)")
    if not key:
        return False

    path = save_key(provider.slug, key)
    ok(f"saved to {path} (mode 600, outside the project)")
    return True


def resolve_timezone(location):
    """Fill in the timezone for a location, where that costs nothing new.

    Quiet hours and the evening window are read in this zone. Left unset they
    fall back to whatever zone the *machine* is in, which on a NAS or a Pi is
    usually UTC -- and a 22:00-07:00 quiet window kept against UTC in Brisbane
    silences the middle of the day and notifies at 3am.

    Nobody types an IANA name, so it is derived: Open-Meteo answers for
    coordinates without a key. But only where a lookup has already happened.
    The screen above promises that typing coordinates directly sends nothing to
    anyone, and a timezone lookup is a lookup -- somebody who chose that path
    chose it for a reason, and quietly spending their privacy to save them a
    question would be exactly the kind of small betrayal that makes a tool not
    worth trusting. They are told what to do instead, and --doctor repeats it.
    """
    lookup = location.pop("_lookup", None)
    if not location.get("latitude") or not location.get("longitude"):
        return location

    if lookup == "manual":
        say()
        say(f"  {DIM}Timezone not looked up: you entered coordinates, and that")
        say(f"  path sends nothing to anyone. Airo will use this computer's")
        say(f"  timezone for quiet hours and the evening window.{RESET}")
        say(f"  {DIM}If this machine is not set to your own timezone, set")
        say(f"  location.timezone in the config. `poller.py --doctor` shows")
        say(f"  which zone is in force.{RESET}")
        return location

    try:
        import weather
        name = weather.timezone_at(location["latitude"], location["longitude"])
    except Exception:
        name = None

    if name:
        location["timezone"] = name
        ok(f"Timezone: {name}")
    else:
        warn("Couldn't determine your timezone; using this computer's. "
             "See `poller.py --doctor`.")
    return location


def choose_networks(location):
    """Pick which networks to search, before asking for any credentials.

    Deliberately a separate step. Prompting for an account mid-search
    interrupts the user with paperwork for a network they may not want, and
    makes the flow feel like it is demanding things rather than offering them.
    Choose first, then set up only what was chosen.
    """
    head("2. Which networks?")
    say(f"  {DIM}Airo can read several networks. Keyless government feeds work")
    say(f"  immediately; the others need a free account, which it will help you")
    say(f"  set up. Pick everything plausible for where you are -- the next step")
    say(f"  shows what each actually has nearby.{RESET}")
    say()

    lat = location.get("latitude")
    lon = location.get("longitude")

    # Order by whether the network can plausibly cover this location. A state
    # feed offered to someone in another state is a dead end that reads as the
    # tool being broken -- which is exactly what happened: a user in Tasmania
    # was defaulted onto the Queensland and NSW feeds and hunted outward to
    # 200 km finding nothing.
    covering, elsewhere = [], []
    for slug, prov in sorted(poller.PROVIDERS.items()):
        (covering if prov.covers(lat, lon) else elsewhere).append((slug, prov))
    options = covering + elsewhere

    recommended = []
    for i, (slug, prov) in enumerate(options, 1):
        covers = prov.covers(lat, lon)
        if prov.needs_key:
            has = poller.get_api_key({"provider": slug})
            cost = f"{GREEN}key already set{RESET}" if has else "free account needed"
        else:
            cost = f"{GREEN}no account needed{RESET}"

        if covers:
            where = f"{DIM}covers {prov.coverage_note}{RESET}"
            # Recommend anything that covers them, whether or not it needs an
            # account -- an account is a minor cost, no coverage is fatal.
            recommended.append(str(i))
        else:
            where = f"{YELLOW}covers {prov.coverage_note} — not your area{RESET}"

        say(f"  {i}. {prov.label}")
        say(f"     {DIM}{prov.tier} · {prov.resolution_minutes} min · "
            f"{prov.accuracy_note}{RESET}")
        say(f"     {cost} · {where}")
        say(f"     {DIM}{prov.licence}{RESET}")
    say()

    if not covering:
        warn("None of the supported networks list your area.")
        say(f"  {DIM}Search anyway if you like -- coverage is a rough guide, not")
        say(f"  a guarantee.{RESET}")
        say()

    keyless_covering = [s_ for s_, pv in covering if not pv.needs_key]
    if covering and not keyless_covering:
        say(f"  {DIM}Note: every network covering your area needs a free account.")
        say(f"  Setup will help you create one in a moment.{RESET}")
        say()

    default = ",".join(recommended) if recommended else "1"
    while True:
        pick = ask("Networks to search, comma separated", default)
        try:
            idx = [int(x) for x in pick.replace(" ", "").split(",") if x]
        except ValueError:
            bad("Numbers only, please.")
            continue
        if not idx or any(not (1 <= i <= len(options)) for i in idx):
            bad(f"Pick between 1 and {len(options)}.")
            continue
        break

    chosen = []
    for i in idx:
        slug, prov = options[i - 1]
        if ensure_key(prov):
            chosen.append(slug)
        else:
            warn(f"skipping {slug} — no key provided")
    return chosen


def discover_all(location, radius_km, slugs):
    """Search the chosen providers, nearest first, narrating as it goes.

    The searching itself is poller.discover_sites(); this is the terminal's
    voice over it. Two front ends ask this question now, and only one of them
    has a terminal to talk to.
    """
    found, failures = poller.discover_sites(location, radius_km, slugs)
    for slug, why in sorted(failures.items()):
        warn(f"{slug}: search failed ({why})")
    for slug in sorted(slugs):
        n = sum(1 for s in found if s.get("provider") == slug)
        if n:
            ok(f"{slug}: {n} site(s) within {radius_km} km")
        elif slug not in failures:
            say(f"  {DIM}{slug}: nothing within {radius_km} km{RESET}")
    return found


# Re-exported so the terminal wizard and its tests keep one name for these,
# while the logic itself lives beside the providers it reasons about.
TIER_LABEL = poller.TIER_LABEL
PROBE_LIMIT = poller.PROBE_LIMIT
probe_reporting = poller.probe_reporting
recommend = poller.recommend


def annotate_reporting(found):
    """Mark the nearest sites with whether they are actually reporting."""
    if not found:
        return found
    say()
    say(f"  {DIM}Checking which of the nearest stations are actually "
        f"reporting...{RESET}")
    ordered, probed, dead = poller.annotate_reporting(found)
    if dead:
        say(f"  {DIM}{dead} of the {probed} nearest publish no PM2.5 right "
            f"now -- shown below, not suggested.{RESET}")
    return ordered


def choose_sources(location, networks):
    head("3. Which monitors?")
    say(f"  {DIM}Closest and most accurate are different questions, and usually")
    say(f"  different instruments. A community sensor down the road describes your")
    say(f"  street but over-reads in humidity; a government monitor is calibrated")
    say(f"  but may be kilometres away on the wrong side of the terrain.")
    say(f"  Running one of each is what lets Airo tell a fire next door from a")
    say(f"  smoky city -- and flag a reading its neighbours don't support.{RESET}")

    radius = 25
    while True:
        say()
        found = discover_all(location, radius, networks)
        if found:
            break

        warn(f"Nothing found within {radius} km.")

        # Say something useful rather than widening forever. If the chosen
        # networks do not cover this location, no radius will help.
        lat, lon = location.get("latitude"), location.get("longitude")
        chosen_cover = [n for n in networks
                        if poller.PROVIDERS[n].covers(lat, lon)]
        if not chosen_cover:
            say()
            bad("None of the networks you chose cover this area, so a wider")
            say("  search will not help.")
            others = [(s_, p_) for s_, p_ in sorted(poller.PROVIDERS.items())
                      if s_ not in networks and p_.covers(lat, lon)]
            if others:
                say()
                say("  These do cover it:")
                for s_, p_ in others:
                    need = ("needs a free account: " + p_.key_url) if p_.needs_key \
                        else "no account needed"
                    say(f"    {p_.label}  {DIM}({need}){RESET}")
                say()
                if ask_yes("Add them and search again?", True):
                    added = []
                    for s_, p_ in others:
                        if ensure_key(p_):
                            added.append(s_)
                    if added:
                        networks = list(networks) + added
                        radius = 25
                        continue
            else:
                say()
                say("  No supported network lists this area. If you know of a public")
                say("  feed that covers it, adding one is a single class in poller.py")
                say("  — see CONTRIBUTING.md.")
            return []

        if radius >= 200:
            say()
            bad("Nothing within 200 km. The networks you chose cover your region,")
            say("  but appear to have no station near you.")
            say(f"  {DIM}Try `python3 poller.py --list-sources`, or widen your")
            say(f"  location and run setup again.{RESET}")
            return []

        if not ask_yes("Search a wider area?", True):
            return []
        radius = min(200, radius * 2)

    found = annotate_reporting(found)
    say()
    shown = found[:25]
    suggested = recommend(found)
    suggested_ids = {(s["provider"], str(s["site_id"])) for s in suggested}

    say(f"  {'':4}{'DISTANCE':>9}  {'NETWORK':<10} {'TYPE':<11} SITE")
    default_picks = []
    for i, s in enumerate(shown, 1):
        # Shown in the reader's own distance unit. The value is stored and
        # reasoned about in kilometres either way -- this is the label on the
        # way out, not a second source of truth about how far away a station
        # is. Somebody choosing between two sensors should not have to convert
        # to know which is nearer.
        d = s.get("distance_km")
        dist = (f"{units.show('distance', d):>8}" if d is not None
                else f"{'? ' + units.label('distance'):>8}")
        tier = TIER_LABEL.get(poller.PROVIDERS[s["provider"]].tier, "consumer")
        star = ""
        if s.get("reporting") is False:
            star = f"  {YELLOW}not reporting now{RESET}"
        elif (s["provider"], str(s["site_id"])) in suggested_ids:
            star = f"  {GREEN}<- suggested{RESET}"
            default_picks.append(str(i))
        say(f"  {i:>2}. {dist}  {s['provider']:<10} {tier:<11} "
            f"{str(s['site_name'])[:38]}{star}")
    if len(found) > len(shown):
        say(f"  {DIM}...and {len(found) - len(shown)} more{RESET}")

    say()
    for tier in ("reference", "indicative", "consumer"):
        provs = {s["provider"] for s in found
                 if TIER_LABEL.get(poller.PROVIDERS[s["provider"]].tier) == tier}
        for slug in sorted(provs):
            say(f"  {DIM}{tier:<11} {poller.PROVIDERS[slug].accuracy_note}{RESET}")
    say()
    say(f"  {DIM}Site names are free text set by their owners and are frequently")
    say(f"  stale -- one sensor 1 km from Melbourne's CBD is named after a suburb")
    say(f"  16 km away. Trust the distance; it is computed from coordinates.{RESET}")
    say()

    while True:
        pick = ask("Numbers to use, comma separated",
                   ",".join(default_picks) or "1")
        try:
            idx = [int(x) for x in pick.replace(" ", "").split(",") if x]
        except ValueError:
            bad("Numbers only, please.")
            continue
        if not idx or any(not (1 <= i <= len(shown)) for i in idx):
            bad(f"Pick between 1 and {len(shown)}.")
            continue
        break

    chosen = []
    for i in idx:
        s = shown[i - 1]
        chosen.append({
            "provider": s["provider"],
            "site_id": s["site_id"],
            "site_name": s["site_name"],
            "latitude": s.get("latitude"),
            "longitude": s.get("longitude"),
            "enabled": True,
        })
        ok(f"{s['provider']}/{s['site_id']} — {s['site_name']}")

    chosen.extend(choose_your_own_sensors())
    return chosen


def choose_your_own_sensors():
    """Sensors added by id, which is the only way to add one you own.

    `discover()` sends `location_type: 0` and returns outdoor sensors only,
    and a private sensor is absent from those results whatever it is set to.
    Somebody who has bought a sensor has its id and nothing else, and until
    this existed the only route was editing config.json by hand -- which is
    also the route that gets a read key written somewhere it should not be.

    Shares `poller.probe_source()` with the settings page, so the two front
    ends cannot disagree about what a valid sensor is. The same reasoning as
    the one validator behind `write_config`.
    """
    added = []
    say()
    if not ask_yes("Do you have a sensor of your own to add by id?", False):
        return added

    say()
    say(f"  {DIM}On PurpleAir the id is the number in the URL when you open your")
    say(f"  sensor. A private sensor also needs its read key, under")
    say(f"  My Sensors -> Edit. Airo reads the sensor once before adding it, so")
    say(f"  a mistyped id fails here rather than as silence tomorrow.{RESET}")

    while True:
        say()
        slug = ask("Network", "purpleair").strip().lower()
        if slug not in poller.PROVIDERS:
            bad(f"Unknown network. Choose from: "
                f"{', '.join(sorted(poller.PROVIDERS))}")
            continue
        site_id = ask("Sensor id", "").strip()
        if not site_id:
            break
        read_key = ask("Read key (blank if the sensor is public)", "").strip()

        say(f"  {DIM}Reading the sensor...{RESET}")
        found = poller.probe_source(slug, site_id, read_key=read_key or None)
        if not found.get("ok"):
            bad(found.get("error", "could not read that sensor"))
            if not ask_yes("Try another?", True):
                break
            continue

        ok(f"{found['site_name']} — {found['placement']}"
           + (f" — {found['pm25']:.1f} µg/m³" if found.get("pm25") is not None
              else ""))
        # Said before they commit, because it decides what the sensor is
        # allowed to mean and they are the only one who can correct it.
        say(f"  {DIM}{found['placement_note']}{RESET}")

        if ask_yes("Add this sensor?", True):
            entry = {
                "provider": found["provider"], "site_id": found["site_id"],
                "site_name": found["site_name"],
                "latitude": found.get("latitude"),
                "longitude": found.get("longitude"),
                "enabled": True, "placement": found["placement"],
            }
            if read_key:
                entry["read_key"] = read_key
            added.append(entry)

        if not ask_yes("Add another?", False):
            break
    return added


# --------------------------------------------------------------------- scale

def choose_scale(location):
    head("4. How should air quality be reported?")
    say(f"  {DIM}Raw micrograms are always what gets stored. This only changes how")
    say(f"  it is shown -- the same air gives very different index numbers on")
    say(f"  different national scales.{RESET}")
    say()
    options = [
        ("au", "Australian AQI", "100 = the NEPM standard of 25 µg/m³"),
        ("us_epa", "US EPA AQI", "2024 revision; 9.0 µg/m³ = AQI 50"),
        ("raw", "Raw µg/m³", "no index; WHO 2021 guideline is 15 µg/m³"),
    ]
    # Suggest by hemisphere/longitude rather than pretending to know the country.
    lon, lat = location.get("longitude") or 0, location.get("latitude") or 0
    default = "au" if (110 < lon < 155 and lat < 0) else "us_epa"
    for i, (slug, label, note) in enumerate(options, 1):
        mark = " (suggested)" if slug == default else ""
        say(f"  {i}. {label}{mark}")
        say(f"     {DIM}{note}{RESET}")
    say()
    pick = ask("Which?", str(next(i for i, o in enumerate(options, 1) if o[0] == default)))
    try:
        return options[int(pick) - 1][0]
    except (ValueError, IndexError):
        return default


# -------------------------------------------------------------------- write

def choose_preferences(location, sources):
    """Everything else the user gets a say in."""
    head("5. Preferences")

    # --- fusion rule
    say(f"  {DIM}When your sources disagree, which one becomes the headline?{RESET}")
    rules = [
        ("nearest", "Nearest", "closest instrument to you — local air is the point"),
        ("freshest", "Freshest", "most recently updated, whatever the distance"),
        ("all", "Show all", "every source listed; headline is the nearest"),
        ("blend", "Blend", "distance and recency weighted — reports a value no "
                           "single instrument measured"),
    ]
    for i, (slug, label, note) in enumerate(rules, 1):
        mark = " (recommended)" if slug == "nearest" else ""
        say(f"  {i}. {label}{mark}")
        say(f"     {DIM}{note}{RESET}")
    pick = ask("Which?", "1")
    try:
        rule = rules[int(pick) - 1][0]
    except (ValueError, IndexError):
        rule = "nearest"

    # --- poll interval
    say()
    fastest = min((poller.PROVIDERS[s["provider"]].resolution_minutes
                   for s in sources), default=15)
    say(f"  {DIM}Your fastest source reports every {fastest} min. Polling more often")
    say(f"  than that spends API calls for no new data.{RESET}")
    while True:
        got = ask("Minutes between polls", str(max(15, fastest)))
        try:
            poll_minutes = max(2, int(got))
            break
        except ValueError:
            bad("A whole number of minutes, please.")

    # --- alerts
    say()
    alerts_on = ask_yes("Notify you when air quality worsens?", True)
    threshold = 16.75
    quiet = [1, 7]
    if alerts_on:
        say(f"  {DIM}A threshold in raw µg/m³, so it means the same thing whichever")
        say(f"  scale you display. 16.75 is the bottom of the Australian amber band;")
        say(f"  35.4 is the US EPA 'unhealthy for sensitive groups' line.{RESET}")
        while True:
            got = ask("Alert above (µg/m³)", "16.75")
            try:
                threshold = float(got)
                break
            except ValueError:
                bad("A number, please.")
        say()
        say(f"  {DIM}Quiet hours suppress notifications overnight. Data is still"
            f" collected.{RESET}")
        got = ask("Quiet from hour (0-23)", "1")
        got2 = ask("Quiet until hour (0-23)", "7")
        try:
            quiet = [int(got) % 24, int(got2) % 24]
        except ValueError:
            quiet = [1, 7]

    # --- dashboard
    say()
    serve = ask_yes("Enable the local dashboard (127.0.0.1 only, on demand)?", True)

    # --- history depth
    say()
    say(f"  {DIM}Airo can seed history on first run so the charts aren't empty.{RESET}")
    while True:
        got = ask("Days of history to pull now", "7")
        try:
            backfill_days = max(0, int(got))
            break
        except ValueError:
            bad("A whole number of days, please.")

    # --- retention
    say()
    say(f"  {DIM}How long should readings be kept? Roughly a megabyte a year per")
    say(f"  source, so keeping everything costs very little — and a record of")
    say(f"  what you breathed cannot be regenerated once discarded.{RESET}")
    say(f"  {DIM}Data lives in ~/.airo/data, outside the project folder, so it")
    say(f"  survives re-cloning or moving the code.{RESET}")
    say()
    say(f"  1. Keep everything (recommended)")
    say(f"  2. Keep the last 2 years")
    say(f"  3. Keep the last 1 year")
    say(f"  4. Keep the last 90 days")
    pick = ask("Which?", "1")
    retention = {"1": 0, "2": 730, "3": 365, "4": 90}.get(pick.strip(), 0)
    if retention:
        warn(f"older readings will be deleted once they pass {retention} days")
        say(f"  {DIM}Change it any time with `python3 setup.py --prefs`, or take a")
        say(f"  backup first with `python3 backup.py create`.{RESET}")

    # --- where it lives
    say()
    default_dir = str(Path.home() / ".airo" / "data")
    say(f"  {DIM}Readings default to {default_dir}, outside the project folder")
    say(f"  so they survive re-cloning or moving the code. Point it elsewhere")
    say(f"  for an external disk or a roomier volume.{RESET}")
    data_dir = ""
    while True:
        answer = ask("Where should readings be stored", default_dir).strip()
        if not answer or answer == default_dir:
            break
        candidate = Path(answer).expanduser()
        # Check it now, not on the first poll. A path that cannot be written
        # is how someone ends up logging into nowhere and not noticing for
        # weeks -- and a removable drive that is not mounted looks exactly
        # like a typo, so the distinction has to be made here, out loud.
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".airo-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            bad(f"cannot write there: {e}")
            say(f"  {DIM}If that is an external drive, mount it first. Airo would")
            say(f"  otherwise start an empty database somewhere you never look.{RESET}")
            if not ask_yes("Try a different location?", True):
                say(f"  {DIM}Keeping the default.{RESET}")
                break
            continue
        existing = candidate / "airo.db"
        if existing.exists():
            ok(f"found an existing database there — it will be used as is")
        data_dir = str(candidate)
        ok(f"readings will be stored in {data_dir}")
        break

    return {
        "data_dir": data_dir,
        "retention_days": retention,
        "fusion": {"rule": rule},
        "poll_minutes": poll_minutes,
        "serve": serve,
        "serve_port": 8787,
        "backfill_days_on_first_run": backfill_days,
        "alerts": {
            "enabled": alerts_on,
            "threshold_pm25": threshold,
            "rising_delta": 12,
            "cooldown_minutes": 60,
            "notify_when_clear": True,
            "quiet_hours": quiet,
            "sound": "Ping",
        },
    }


def write_config(cfg):
    """Write the wizard's answers, through the same path the settings UI uses.

    Checked against the same rules first. The wizard and the settings page are
    two front ends onto one file, so anything the wizard can produce that the
    UI would refuse is a disagreement about what a valid setting is -- and it
    would surface as a config the user cannot edit in the UI they were told to
    use.
    """
    _, errors = poller.validate_settings(
        {k: v for k, v in cfg.items() if k in poller.SETTINGS_SCHEMA})
    if errors:
        # Loud, because this is the wizard contradicting the validator, not the
        # user typing something odd -- the prompts already refused that.
        for message in errors.values():
            bad(message)
        raise ValueError(f"setup produced settings the validator refuses: {errors}")

    return poller.save_config(cfg, config_path())


def main():
    ap = argparse.ArgumentParser(
        description="Set up Airo for your location.",
        epilog="With no options this runs the full wizard.")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing configuration")
    ap.add_argument("--keys", action="store_true",
                    help="review network accounts and add missing API keys")
    ap.add_argument("--prefs", action="store_true",
                    help="edit preferences without redoing location or sources")
    ap.add_argument("--profile", action="store_true",
                    help="edit your profile only")
    args = ap.parse_args()

    # Targeted edits, so a rotated key or a changed threshold does not mean
    # redoing the whole wizard.
    if args.keys:
        # Listing is useful without a terminal; prompting is not. Degrade to
        # read-only rather than crashing partway through the list.
        interactive = sys.stdin.isatty()
        missing = manage_keys(interactive=interactive)
        if missing and not interactive:
            say()
            warn("Not a terminal, so nothing was prompted for.")
            say(f"  Run this from a normal terminal to set them up:")
            say(f"    cd {HERE} && python3 setup.py --keys")
        return 0

    if args.profile or args.prefs:
        if not sys.stdin.isatty():
            bad("Editing preferences needs an interactive terminal.")
            say(f"  Run: cd {HERE} && python3 setup.py"
                f"{' --profile' if args.profile else ' --prefs'}")
            return 2
        existing = load_existing()
        if not existing:
            bad(f"No configuration at {config_path()}. Run: python3 setup.py")
            return 1
        if args.profile:
            existing["profile"] = choose_profile(existing)
        if args.prefs:
            existing["aqi_scale"] = choose_scale(existing.get("location") or {})
            existing.update(choose_preferences(
                existing.get("location") or {}, existing.get("sources") or []))
        path = write_config(existing)
        say()
        ok(f"updated {path}")
        return 0

    # Refuse before asking anything. Piping into setup, or running it through a
    # wrapper with no controlling terminal, previously produced a full config
    # of defaults and a cheerful "Done".
    if not sys.stdin.isatty():
        bad("Setup needs an interactive terminal.")
        say()
        say("  It looks like stdin isn't a terminal — that happens when setup is")
        say("  piped, backgrounded, or run through a wrapper.")
        say()
        say("  Open a normal terminal window and run:")
        say(f"    cd {HERE} && python3 setup.py")
        say()
        say("  To configure without prompts, copy config.example.json to")
        say(f"    {config_path()}")
        say("  and edit it directly.")
        return 2

    say()
    say(f"{BOLD}Airo setup{RESET}")
    say(f"{DIM}Local air quality monitoring. Nothing leaves your machine except")
    say(f"the API calls to the networks you choose.{RESET}")

    if config_path().exists() and not args.force:
        say()
        warn(f"You already have a configuration at {config_path()}")
        if not ask_yes("Replace it?", False):
            say("\nNothing changed.")
            return 0

    profile = choose_profile(load_existing())
    location = resolve_timezone(choose_location())
    networks = choose_networks(location)
    if not networks:
        say()
        bad("No networks selected — nothing to search.")
        return 1
    sources = choose_sources(location, networks)
    if not sources:
        say()
        bad("No sources chosen — nothing to poll. Run setup again when ready.")
        return 1
    scale = choose_scale(location)
    prefs = choose_preferences(location, sources)

    cfg = {
        "profile": profile,
        "location": location,
        "sources": sources,
        "aqi_scale": scale,
    }
    cfg.update(prefs)

    head("6. Saving")
    path = write_config(cfg)
    ok(f"settings written to {path} (mode 600)")
    say(f"  {DIM}Outside the project folder, so it can never be committed.{RESET}")

    head("Done")
    say("  Next:")
    say(f"    python3 poller.py --once        {DIM}# first reading, seeds history{RESET}")
    say(f"    python3 scheduler.py install    {DIM}# poll every 15 min in the background{RESET}")
    say(f"    python3 poller.py --status      {DIM}# confirm it is healthy{RESET}")
    say()
    say(f"  {DIM}Add or rotate a network account any time:{RESET}")
    say(f"    python3 setup.py --keys")
    say(f"  {DIM}Change preferences without redoing setup:{RESET}")
    say(f"    python3 setup.py --prefs")
    say()
    say(f"  {DIM}Optional menu-bar readout (needs Rust):{RESET}")
    say(f"    cd tray && cargo build --release && cd ..")
    say(f"    python3 scheduler.py install-tray")
    say()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nCancelled. Nothing was written.")
        sys.exit(130)
    except NoTerminal as e:
        print(f"\n\n{e}.\nNothing was written. Run setup from an interactive terminal.")
        sys.exit(2)
