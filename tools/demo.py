#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Donnish Pty Ltd
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Build a self-contained Airo install with synthetic readings.

Why this exists
---------------
Nothing about this project is legible without seeing it. The dashboard, the
evening heatmap and the source comparison are the argument for it existing, and
a description of them is not the same as looking at one. ROADMAP #3b.

Screenshots of a real install cannot go in the repository: they show a home
location, chosen sensors and a year of when somebody was in. Rule 2b, and it
applies to a PNG exactly as it applies to a config file. So the demo comes
first, and the screenshots come from the demo.

It is also useful on its own. Somebody evaluating Airo can look at a populated
dashboard in one command, without an account, a key, or waiting a week for
history to accumulate.

What it is NOT
--------------
Not a shipped feature and not importable by the app. It lives in `tools/`
beside the build scripts because demo data and real readings must never be
able to meet: there is no flag on a reading that says "invented", and a
health-relevant number that might be fabricated is worse than no number.

It therefore refuses to write anywhere near `~/.airo`, and every site it
creates is named so a screenshot cannot be mistaken for a real one.

The numbers
-----------
Synthetic but not arbitrary — shaped to match what RESEARCH.md documents, so
the demo shows the effect the project was built to find rather than noise:

  * a diurnal curve with an afternoon minimum and an evening rise
  * an evening premium near 1.8x at the open reference site and higher at the
    valley sensor, which is the terrain effect (RESEARCH.md, "peak hour is
    roughly 6-7x the afternoon minimum versus 1.8x at the open site")
  * a handful of worse nights, because a heatmap of a uniform week shows
    nothing
  * one instrument fault, so the corroboration panel has something to say

Seeded, so the same command always produces the same picture and a screenshot
can be retaken years later without hunting for why it looks different.

    python3 tools/demo.py --into /tmp/airo-demo --serve
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# The shifted synthetic frame the tests use, so one invented place serves the
# whole project and nothing here is anywhere real. See tests/test_providers.py.
HOME_LAT, HOME_LON = -33.5000, 151.0000

# Named so a screenshot carries its own disclaimer. Somebody who finds the
# image without the caption still cannot read it as a real reading, which
# matters more here than it would for a to-do app.
#
# Real provider slugs, deliberately, with a cost worth stating. The
# reference-versus-consumer contrast IS the product -- the tier drives the
# recommendation, the corroboration wording and the accuracy note -- so a demo
# without it demonstrates the wrong thing. The cost is that the footer credits
# PurpleAir and Queensland beside numbers neither of them produced. Mitigated
# by naming every site "Demo ...", which puts the disclaimer inside the image,
# and by captioning the screenshots. If that ever feels too close to the line,
# the fix is two government slugs and a weaker demo, not an unlabelled one.
SITES = [
    {
        "provider": "qld", "site_id": "demo-ref", "site_name": "Demo Reference Monitor",
        "latitude": HOME_LAT + 0.070, "longitude": HOME_LON + 0.055,
        "enabled": True,
        # The open, calibrated site: further away, milder evening effect.
        "_premium": 1.8, "_base": 4.2, "_jitter": 0.9, "_resolution": 60,
    },
    {
        "provider": "purpleair", "site_id": "demo-valley", "site_name": "Demo Valley Sensor",
        "latitude": HOME_LAT + 0.008, "longitude": HOME_LON - 0.006,
        "enabled": True,
        # The near consumer sensor in the valley: the whole point of the tool.
        "_premium": 4.1, "_base": 5.6, "_jitter": 1.8, "_resolution": 10,
    },
]


def refuse_to_touch_real_data(target):
    """Never write demo readings where real ones live.

    A demo database that lands in ~/.airo is not a tidy-up problem: it is
    fabricated numbers in the store the tray, the dashboard and the alerting
    all read, with nothing marking them as invented.
    """
    target = Path(target).expanduser().resolve()
    real = (Path.home() / ".airo").resolve()
    if target == real or real in target.parents or target in real.parents:
        raise SystemExit(
            f"refusing to build a demo at {target}: that is inside, or "
            f"contains, the real data directory at {real}.\n"
            f"Pick somewhere else — /tmp/airo-demo is a good answer.")
    return target


# Hourly shape, as multipliers on a site's base concentration. Written out
# rather than computed from a cosine, which is what the first version did and
# got wrong twice: the peak has to sit in the evening *window* (15:00-01:00)
# and the minimum in the daytime one, or the evening premium the panels
# measure comes out near 1.0 however dramatic the curve looks.
#
# Shape follows RESEARCH.md: minimum mid-afternoon when the boundary layer is
# deepest, rising steeply after sunset as it collapses, decaying overnight.
BASELINE = {
    0: 1.05, 1: 0.98, 2: 0.94, 3: 0.92, 4: 0.90, 5: 0.92,
    6: 0.98, 7: 1.05, 8: 1.02, 9: 0.94, 10: 0.88, 11: 0.84,
    12: 0.80, 13: 0.78, 14: 0.77, 15: 0.80, 16: 0.88, 17: 0.95,
    18: 1.00, 19: 1.05, 20: 1.08, 21: 1.08, 22: 1.06, 23: 1.05,
}

# How much of the site's evening excess applies, hour by hour. Zero outside
# the window so the daytime average stays the baseline it is compared against.
EVENING = {
    15: 0.10, 16: 0.30, 17: 0.60, 18: 0.85, 19: 0.97, 20: 1.00,
    21: 0.96, 22: 0.82, 23: 0.60, 0: 0.35,
}


def pm25_at(when, site, rng, night_factor=1.0):
    """A plausible concentration for one moment at one site.

    `night_factor` scales the whole evening for that night, so some nights are
    genuinely worse than others — a heatmap of a uniform month shows nothing,
    which is the one thing the panel exists to show.
    """
    hour = when.hour
    excess = EVENING.get(hour, 0.0) * (site["_premium"] - 1) * night_factor

    value = site["_base"] * (BASELINE[hour] + excess)
    value += rng.gauss(0, site["_jitter"] * (1 + excess / 3))
    return max(0.3, round(value, 1))


def build(target, days, seed=7):
    """Write a complete install: config, database, latest.json."""
    target.mkdir(parents=True, exist_ok=True)
    data_dir = target / "data"
    data_dir.mkdir(exist_ok=True)
    config_path = target / "config.json"

    # Before importing poller, not after. It resolves CONFIG_PATH and DATA at
    # import time, so an import that happens first binds them to the real
    # ~/.airo — and build_latest() then writes that path into the demo's own
    # latest.json. The dashboard renders it verbatim, so a screenshot taken
    # from the demo showed a real home directory. Caught in a screenshot
    # review; it would have shipped otherwise.
    os.environ["AIRO_CONFIG"] = str(config_path)
    os.environ["AIRO_DATA"] = str(data_dir)

    import poller
    import store

    cfg = {
        "location": {"name": "Demo Valley", "latitude": HOME_LAT,
                     "longitude": HOME_LON},
        "sources": [{k: v for k, v in s.items() if not k.startswith("_")}
                    for s in SITES],
        "aqi_scale": "au",
        "fusion": {"rule": "nearest"},
        "poll_minutes": 15,
        "alerts": {"enabled": True, "threshold_aqi": 67},
        # Not 8787. A real server on the default port refuses a second one --
        # correctly -- and the demo would then quietly be looking at somebody's
        # actual readings. See serve() for the check that catches it anyway.
        "serve_port": DEMO_PORT,
    }
    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    rng = random.Random(seed)
    conn = store.connect(data_dir / "airo.db")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    try:
        for site in SITES:
            sid = store.upsert_source(
                conn, site["provider"], site["site_id"], site["site_name"],
                latitude=site["latitude"], longitude=site["longitude"],
                resolution_minutes=site["_resolution"],
                # The demo's sites are the outdoor monitors a real install
                # would have. Left unset they register as 'unknown', which is
                # excluded from describing outdoor air, and the screenshots
                # this generates would all read "no data from any configured
                # source" -- which is how the README's tray readout became
                # fiction the moment the exclusion landed.
                placement=site.get("_placement", "outdoor"))

            step = timedelta(minutes=site["_resolution"])
            when = now - timedelta(days=days)
            rows = []

            # One factor per night, drawn once and reused for every reading in
            # it, so a bad night is bad from dusk to dawn rather than being
            # noise that averages away. Keyed on the date the night *started*,
            # which is the same convention the heatmap and analyse.py use --
            # 00:30 belongs to the evening before.
            nights = {}

            def factor_for(local):
                key = (local - timedelta(hours=2)).date()
                if key not in nights:
                    # Mostly ordinary, occasionally still, rarely windy. Still
                    # nights are what the tool is for.
                    roll = rng.random()
                    if roll > 0.88:
                        nights[key] = rng.uniform(1.7, 2.4)   # a bad one
                    elif roll < 0.12:
                        nights[key] = rng.uniform(0.25, 0.5)  # breezy
                    else:
                        nights[key] = rng.uniform(0.8, 1.25)
                return nights[key]

            while when <= now:
                # Local time drives the diurnal shape; the store keeps UTC.
                local = when.astimezone()
                pm = pm25_at(local, site, rng, factor_for(local))
                rows.append({"observed_utc": when.isoformat(timespec="seconds"),
                             "pm25": pm, "pm25_now": pm})
                when += step

            # One instrument fault on the consumer sensor, a few days back, so
            # the corroboration panel has a real example to render rather than
            # a permanently clean board.
            if site["site_id"] == "demo-valley":
                fault_at = now - timedelta(days=3)
                for row in rows:
                    stamp = datetime.fromisoformat(row["observed_utc"])
                    if 0 <= (stamp - fault_at).total_seconds() < 3600:
                        row["pm25"] = round(row["pm25"] * 9, 1)
                        row["pm25_a"] = row["pm25"]
                        row["pm25_b"] = round(row["pm25"] / 7, 1)

            store.insert_readings(conn, sid, rows)
        conn.commit()

        latest = poller.build_latest(conn, cfg)
        poller.write_json_atomic(data_dir / "latest.json", latest)
    finally:
        conn.close()

    return {"config": config_path, "data": data_dir,
            "readings": sum(c["rows"] for c in _counts(data_dir))}


def _counts(data_dir):
    import store
    conn = store.connect(data_dir / "airo.db")
    try:
        return store.counts(conn)
    finally:
        conn.close()


DEMO_PORT = 8788


def serve(target, port):
    """Run the dashboard against the demo, and prove that is what it shows.

    The verification is the point. A real Airo already on the port makes
    serve_forever refuse the new one -- which is right, and leaves whatever was
    already there answering. Every URL this tool prints is aimed at a
    screenshot destined for the repository, so "probably the demo" is not good
    enough: a real install answers with a home location and chosen sensors, and
    that is rule 2b in a PNG.

    /api/ping reports the data directory it is serving, so the check is exact
    rather than a guess from what the page looks like.
    """
    import urllib.request

    env = dict(os.environ)
    env["AIRO_CONFIG"] = str(target / "config.json")
    env["AIRO_DATA"] = str(target / "data")

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "poller.py"), "--serve"],
        cwd=str(HERE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    expected = str((target / "data").resolve())
    deadline = time.time() + 20
    serving = None
    while time.time() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/ping", timeout=0.5) as r:
                serving = json.loads(r.read().decode())
            break
        except Exception:
            time.sleep(0.25)

    if serving is None:
        proc.terminate()
        raise SystemExit(
            f"the demo server did not come up on {port}.\n"
            f"{(proc.stdout.read() if proc.stdout else '')[-500:]}")

    actual = str(Path(serving.get("data_dir", "")).resolve())
    if actual != expected:
        proc.terminate()
        raise SystemExit(
            f"port {port} is answering, but for a DIFFERENT install:\n"
            f"  it is serving : {actual}\n"
            f"  the demo is at: {expected}\n"
            f"Something else — probably a real Airo — already holds this port. "
            f"Stop it, or pass --port. Do not screenshot what is there: it is "
            f"somebody's actual location and readings.")

    print(f"\n  serving the demo, verified: {actual}")
    print(f"  dashboard : http://127.0.0.1:{port}/dashboard.html")
    print(f"  settings  : http://127.0.0.1:{port}/settings")
    print("  ctrl-c to stop\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--into", default="/tmp/airo-demo",
                    help="where to build it (default /tmp/airo-demo)")
    ap.add_argument("--days", type=int, default=45,
                    help="how much history to invent (default 45)")
    ap.add_argument("--seed", type=int, default=7,
                    help="fixed, so the same command draws the same picture")
    ap.add_argument("--serve", action="store_true",
                    help="start the dashboard against it when done")
    ap.add_argument("--port", type=int, default=DEMO_PORT,
                    help="not 8787, so a demo never collides with a real "
                         "server somebody is already looking at")
    args = ap.parse_args()

    target = refuse_to_touch_real_data(args.into)
    built = build(target, args.days, args.seed)

    print(f"  built {built['readings']:,} synthetic readings")
    print(f"  config : {built['config']}")
    print(f"  data   : {built['data']}")
    print("\n  Everything here is invented. It is not a measurement of "
          "anywhere,\n  and it must never be copied into ~/.airo.")

    if args.serve:
        serve(target, args.port)
    else:
        print(f"\n  view it:  python3 tools/demo.py --into {target} --serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
