# What a reading means

Airo shows one number, and one number hides a great deal. This page is the rest of it: whose air
a monitor is actually describing, why the headline is averaged, why a second source is the most
useful thing you can add, and how Airo tells a real event from a broken instrument.

The bands themselves, and the µg/m³ behind them, are in the [README](../README.md).

---

## Whose air is it, though?

Most people will read somebody else's sensor, because most people do not own one. That is a
reasonable thing to do and it comes with a caveat worth stating plainly: **a monitor a few
kilometres away may not describe your air at all.**

Three things matter more than distance:

- **Elevation and terrain.** Cold air drains downhill after sunset and pools in the low ground,
  carrying particulates with it. A sensor at the top of a valley and one at the bottom can differ
  by a factor of several on exactly the nights that matter most. If you are in a gully, a hilltop
  reading is optimistic about your air.
- **What is between you and it.** A monitor on the far side of a main road, an industrial block,
  or a bushfire front is measuring something different from what is outside your window.
- **Height and siting of the instrument.** A sensor on a roof reads differently from one at head
  height in a courtyard. Consumer sensors near a wall, under an eave, or beside a dryer vent read
  their own microclimate.

What to do about it: prefer the nearest monitor that is on similar ground to you, add a second
one from a different network, and watch how they behave overnight. If two nearby sources
consistently disagree by a lot, that difference is information about your local terrain rather
than a fault in either instrument — and it is the reason Airo shows every source with its
distance rather than picking one and hiding the argument.

The most local reading available is one at your own house, which is what a consumer sensor is
genuinely good for even though it is less accurate than a regulatory monitor. Accuracy and
relevance are different things.

---

## Why the headline is a 10-minute average

The instantaneous number from a particle counter jumps around a great deal. A passing car, a
neighbour lighting a barbecue, or someone shaking out a tea towel near the inlet can move it
sharply for a few seconds. None of those describe the air you will be breathing in an hour.

So the headline is the **10-minute average**, and the raw instantaneous value is shown beside it
rather than instead of it. Ten minutes is long enough to average out a passing event and short
enough to notice smoke arriving. Where a source publishes no 10-minute figure — most government
monitors report hourly — Airo says which one it is using rather than quietly substituting a
different kind of number.

This is also why the rolling averages are shown together. A 10-minute reading well above the hour
is air that is getting worse; well below it is air that is clearing. The direction is often more
useful than the value.

---

## Why several sources

A single sensor tells you what one instrument thinks. Two tell you something much more useful.
Two sources at one location, one still evening in the synthetic demo data the screenshots use:

| Source | Distance | PM2.5 | Australian AQI | Band |
|---|---|---|---|---|
| Consumer sensor | 1.1 km | **24.5 µg/m³** | 98 | **Fair** |
| Government monitor | 9.3 km | 5.7 µg/m³ | 23 | Very good |

Same city, same minute, **four times the particulate**. That gap *is* the valley effect, and it
is why the default rule picks the nearest instrument. Had it picked the government monitor, the
dashboard would have cheerfully said "Very good" to someone who should have been closing their
windows.

When sources disagree, `fusion.rule` in the config decides the headline: `nearest` (the default),
`freshest`, `all`, or `blend`. All of them skip sources that are stale or flagged faulty, judged
against each source's own cadence. `blend` reports a value no instrument measured, and is
labelled as computed wherever it appears. The reasoning is in
[ARCHITECTURE §2.5b](../ARCHITECTURE.md#25b-fusion-is-a-decision-not-a-calculation).

---

## Catching false positives

One sensor screaming while every neighbour is calm needs explaining. Airo checks the instrument
against itself (a PurpleAir has two laser counters; when they disagree by more than 2× the sensor
is faulty, not the air), against its neighbours (more than 3× the median), and against its own
history at the same hour over the last 90 days — because a valley sensor that *always* reads 3×
after sunset is measuring something real.

Flagged readings are **shown, never hidden**. But every surface says plainly that the neighbours
do not see it. The full rule set is in
[ARCHITECTURE §2.5c](../ARCHITECTURE.md#25c-corroboration--telling-a-real-event-from-a-false-positive).

---

## When to distrust a reading

Airo is deliberately honest about this rather than showing you a confident number:

- **One monitor reading high while its neighbours are calm** is usually a fire next door or a
  blocked air inlet, not the air across the suburb. Airo flags this and says so plainly — it
  never hides the reading, because if there *is* a fire next door that is genuinely the air you
  are breathing.
- **Low-cost optical sensors over-read in humid weather**, typically by 20–40%. Airo tells you
  which kind of instrument each reading came from. Treat those readings as trends, not as
  calibrated truth.
- **A reading several hours old is not current air.** Airo shows the age of every source and
  marks the stale ones.
- **Some readings are not real.** Anything above 350 µg/m³ is flagged `suspect` and left out of
  aggregates and fusion — but stored, never dropped. See
  [ARCHITECTURE §3.5](../ARCHITECTURE.md#35-data-quality--some-readings-are-not-real).
- **With only one source there is nothing to cross-check against.** Adding a second is the single
  most useful thing you can do.

---

## Where the evidence comes from

The evening-premium effect, the humidity correction and the siting advice above are drawn from
published work, cited in [RESEARCH.md](../RESEARCH.md). Indoor placement is its own problem with
its own failure modes:
[ARCHITECTURE §2.5e](../ARCHITECTURE.md#25e-placement--an-indoor-sensor-never-speaks-for-the-air-outside).

Airo is **not medical advice** and not a medical device. If air quality is affecting your health,
speak to a doctor.
