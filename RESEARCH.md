# Research findings

Evidence base behind this project. Compiled mid-2026 from government monitoring data,
peer-reviewed literature and the project's own sensor record.

Kept as a reference for product decisions — what we actually know, how confident we are, and
where the gaps sit. **Not medical advice.**

---

## 1. The originating question

A resident of a low-lying valley suburb experienced episodic chest
tightness on winter evenings, without any smell of smoke. Symptoms were absent when outdoors on
higher ground nearby during the same hours. PurpleAir sensors nearby read far higher
than sensors in a ridge-top suburb elsewhere.

Three observations turned out to be diagnostic:

| Observation | What it rules in |
|---|---|
| "Only in winter" | Radiative inversion — needs long, clear, cold nights |
| "Not every day" | Stagnation — needs wind below ~0.5 m/s |
| "Fine during evening sport on higher ground nearby" | Position within the drainage path, not time of day |
| "No smoke smell" | Weakens a neighbour-scale source; doesn't exclude diluted smoke from km away |

---

## 2. The mechanism: cold-air drainage in a valley

On clear, calm winter nights, radiative cooling chills the surface, cold air drains downhill
and pools in valley floors, and a nocturnal inversion caps it a few tens of metres up.
Pollutants emitted under that cap cannot disperse.

**Elevation is the controlling variable, not suburb.**

| Location | Elevation | Position in catchment |
|---|---|---|
| A ridge-top suburb elsewhere | ~130 m | Ridge crest — sits *above* the trapped layer |
| Upper catchment, same city | ~100 m average, tens to a couple of hundred metres | *Head* of the catchment — air arrives off high ground, before crossing suburbia |
| **Valley floor** | **tens of metres** | **Receiving end** of the catchment drainage, downstream of everything above it |

Same city, same hour, opposite ends of one physical process. This explains the evening-sport
observation completely — and it means **advice must be elevation-specific, not suburb-specific.**

### Corroborating meteorology

At the government reference station, on the winter evening the symptoms were worst: **wind speed
0.2 m/s** — effectively dead calm. [^1]

---

## 3. What the measurements show

### 3.1 Government reference network — 22 days hourly, mid-winter

Source: Queensland DETSI air monitoring network, about a thousand hourly records. [^1] [^2]

**Diurnal profile — evening peak is unambiguous:**

| Hour | PM2.5 µg/m³ | NO₂ ppb |
|---|---|---|
| Early afternoon | **~3.4** (min) | **~6** (min) |
| Early evening | ~5.9 | **~19** (peak) |
| Mid evening | **~6.1** (peak) | ~18 |

Evening PM2.5 is **1.8×** the early-afternoon minimum. **Every hour above 10 µg/m³ in the
record fell between 17:00 and 01:00.**

**Wind speed dominates:**

| Wind speed | Mean PM2.5 | Mean NO₂ |
|---|---|---|
| 0.0–0.5 m/s | **6.49** | **15.9 ppb** |
| 0.5–1.0 m/s | 4.05 | 7.1 |
| 1.0–1.5 m/s | 3.53 | 4.2 |

Correlation, overnight wind speed vs peak NO₂: **−0.85**. Minimum temperature vs peak PM2.5:
**−0.59**.

**Important correction to intuition:** humidity correlates *positively* with PM2.5 (+0.16);
bad nights ran 76–89% RH. The signature is **calm + cold**, not calm + dry.

Controlling for speed, the worst calm hours clustered in a single wind sector — the
downslope direction — with almost none from the opposing sectors. That is the signature of
local cold-air drainage, not a source carried in from across the city.

**Locality:** a busier station elsewhere in the city averages *higher* but has an almost flat
daily cycle, and correlates only weakly with the reference. The reference station's sharp
evening spike is therefore a local effect, not an airshed-wide one.

### 3.2 Community sensor — valley floor, ~120 nights at 10-minute resolution

Source: a public PurpleAir sensor on the valley floor. [^3]

**The valley amplifies the effect far beyond what the reference station suggested:**

| Hour | Mean AQI (AU) | µg/m³ |
|---|---|---|
| 13:00 | ~10 | ~3 |
| 19:00 | ~46 | ~11 |
| **21:00** | **~69** | **~17** |
| 06:00 | ~50 | ~12 (secondary morning peak) |

**Peak hour is roughly 6–7× the afternoon minimum** — versus 1.8× at the reference station, which
is far better ventilated, so it should be read as a **floor, not a
ceiling**, for valley conditions.

**Peak hour is 21:00**, an hour later than the reference station implied — which shifts
practical advice (filtration should run later, not just earlier).

**Night-by-night variation is enormous:**

| Night | Evening mean | Daytime mean | Ratio | Peak |
|---|---|---|---|---|
| **The worst night** | ~95 | ~7 | **~14×** | **~250 (Hazardous)** |
| Next worst | ~61 | ~18 | 3.4× | ~83 |
| Third | ~63 | ~43 | 1.5× | ~110 |
| The other four | — | — | 0.34–0.94× | ventilated |

**2 of 7 nights were trapping nights.** On the worst, five consecutive readings sat in the
Hazardous band across a single evening peak, topping **60 µg/m³** — 2.5× the Australian
24-hour standard, as an instantaneous value.

> **Product implication:** an average is useless here. The distribution is bimodal — most
> nights are fine, a minority are severe. Any product must surface *episodes*, not means.

---

## 4. Sensor accuracy and data quality

### 4.1 Low-cost optical sensors over-read

PurpleAir units use optical particle counters that overestimate PM2.5, worsening with
humidity, because hygroscopic particles grow and scatter more light. The US EPA developed a
national correction for exactly this reason. [^4] [^5]

Practical magnitude: roughly **20–40%**, worse on damp evenings. Treat readings as **trends
and relative comparisons, not calibrated truth.**

### 4.2 But the valley effect is larger than the sensor error

Initial instinct was to attribute most of the PurpleAir/reference gap to sensor error. The
120-night record contradicts that. The reference station peaked around 15 µg/m³ on its worst night while being
the best-ventilated comparison site available; a confined valley recorded roughly four
times that. **The terrain effect exceeds the calibration error by a wide margin.**

There is **no government monitor in this valley** — the nearest sit on higher, better-ventilated
ground and cannot see the trapped layer. [^2] This is the gap community
sensors fill, and the reason a regulatory-data-only product would not have detected this at all.

### 4.3 Some readings are not real

The 365-day record contains a scatter of readings orders of magnitude above the rest. Values
that high are not plausible suburban ambient air; they exceed Australian Black Summer peaks.
Likely causes: blocked inlet, sensor fault, or combustion directly beside the unit.

Left in, they dragged the "average evening" midnight figure well above 100 AQI. **Design
consequences adopted:**

- Flag implausible values (>~350 µg/m³) and name the affected dates
- Use **medians, not means**, for summary statistics
- Surface, don't silently drop — the user decides

Other known failure modes worth detecting: flat-lining, A/B channel disagreement (PurpleAir
exposes both), and `last_seen` staleness distinguishing "sensor offline" from "our poller
stopped".

---

## 5. AQI scales are not interchangeable

**Australian AQI:** `AQI = concentration ÷ standard × 100`, where the PM2.5 standard is
25 µg/m³ (NEPM 24-hour). So **AQI = µg/m³ × 4**. Bands: 33 / 66 / 99 / 149 / 200. [^6]

**US EPA AQI:** piecewise-linear with different breakpoints; 2024 revision moved the
Good/Moderate boundary to 9.0 µg/m³. [^7]

The same air gives very different numbers. PurpleAir's map defaults to US EPA. **Comparing a
figure from one scale to the other is meaningless** — a mistake made early in this project and
worth designing against explicitly.

---

## 6. Indoor mitigation — what the evidence supports

### 6.1 Closing windows works; combining it with filtration works much better

Median indoor/outdoor PM2.5 ratio, from a review of 20 studies: [^8]

| Condition | I/O ratio |
|---|---|
| Windows open | 0.76 |
| Windows closed | 0.62 |
| **Windows closed + portable HEPA** | **0.25** |

Critically, air cleaners achieved only a **37% reduction with windows open** versus **71%
closed**. Closing up is not an alternative to filtration — it's a **multiplier** on it.

### 6.2 Australian housing is unusually leaky

*"Many Australian homes are very leaky (i.e., >15 ACH) compared to those in countries such as
the USA."* During Black Summer, airtight homes peaked **~30% lower** than leaky ones under
identical outdoor conditions. [^9] One review found a tighter house gave **68% passive
protection** versus **31%** for a leaky one. [^8]

Sealing and filtering are complements, not alternatives — filtration underperforms in a very
leaky envelope. [^9]

### 6.3 Indoor levels lag outdoor, and clear slowly

A Canberra study during the 2019–20 bushfires found **indoor PM peaks at night**, delayed
behind outdoors, because pollutants accumulate and settle slowly in a low-exchange
envelope. [^10] Particle loss-rate constants drop from a median **1.9 h⁻¹ on non-fire days to
1.2 h⁻¹ on fire days** across 1,274 buildings. [^11]

> **Design consequence:** sealing reduces what gets in *and* slows what gets out. Filtration
> should start **before** the outdoor peak and run **past** it.

### 6.4 Air-change rates: 2–3 is the efficient target

"5 ACH" has no health-authority provenance for smoke — it originates in COVID-era school
ventilation guidance. What EPA publishes is a CADR sizing table which reverse-engineers to
~4.9 ACH. [^12]

The threshold associated with a **48% reduction in modelled hospital admissions** was
CADR/V = 1 — just **1 ACH**. [^13] Going 0 → 2 ACH buys most of the available benefit;
2 → 5 ACH costs ~2.5× the hardware for a fraction more.

**Time to clear** (exponential decay, `t = −ln(1−X) ÷ ACH`):

| ACH | 50% | 80% | 90% |
|---|---|---|---|
| 1 | 42 min | 97 min | 138 min |
| **2** | **21** | **48** | **69** |
| 3 | 14 | 32 | 46 |
| 5 | 8 | 19 | 28 |

With continuous infiltration you never reach zero, but you approach the new equilibrium
faster — **90% of the way in 35–50 min at 2–3 ACH.**

⚠️ **CADR is a maximum-speed figure.** Sleep/quiet settings deliver roughly a third of rated
CADR, so real-world sizing should target ~3× the calculated requirement. Manufacturer
"covers X m²" claims typically assume 2.4 m ceilings and imply only 1.6–2 ACH.

**DIY box-fan filters** (Corsi-Rosenthal) are peer-reviewed at 1,020 m³/h on low speed — well
above most commercial units and about a tenth the cost per unit of air cleaned. Two caveats
matter here: they measure **58 dB on low**, unusable in a bedroom; and their MERV-13 media is
only ~55% efficient at 0.35 µm, precisely where wood-smoke accumulation-mode particles
sit. [^20] Australian sourcing is also poor — MERV 13 panel filters aren't sold through
consumer retail here.

⚠️ **Avoid ozone generators sold as purifiers.** They generate a lung irritant; CHOICE
recommends avoiding them outright. [^22]

### 6.5 Split-system air conditioners do not filter PM2.5

US EPA, on this product category by name: wall-mounted units have *"limited filtration
intended to keep the inside of the air conditioner clean rather than remove fine particles
from the indoor air."* [^14]

Split filters are G1–G4 class (≈MERV 1–4), which has **no rated efficiency at all** in the
0.3–3.0 µm bands where PM2.5 lives. [^15] An Australian review quantifies it: a G4 filter
captures *"around 10%"* of smoke particles; F6 (~MERV 10–11) about half. [^9]

Applying ASHRAE's conversion method to the best-documented ioniser claim yields an effective
clean-air delivery of **~32 m³/h** — which ASHRAE's own authors note suits *"a small
closet."* [^16]

**But run them anyway.** Both Victorian and NSW health guidance recommend split systems during
smoke events — purely because they let you keep windows shut comfortably, never because they
filter. Evaporative coolers must be **off**: they can replace a house's entire air volume every
2–3 minutes. [^17] [^18]

---

## 7. Health context

**Chest tightness is a lower-airway symptom** — distinct from upper-airway allergic symptoms.
Recurring, with a clear environmental pattern, it warrants medical assessment including
spirometry rather than environmental management alone.

**Pollen is not captured by PM2.5.** Grains are 10–100 µm; they don't register meaningfully in
a PM2.5 figure, so an air quality reading cannot confirm or exclude a pollen trigger. Brisbane
pollen monitoring runs seasonally and is off between roughly May and November. [^19]

**NO₂ is a plausible co-contributor** — odourless at ambient levels, peaks at evening rush,
accumulates under a stable nocturnal layer, and causes airway constriction in reactive
airways. The reference station recorded well below the 1-hour guideline. Community PM2.5 sensors do not measure it, and **no consumer device measures NO₂
reliably** at the 20–40 ppb levels that matter.

---

## 8. Product implications

| Finding | Consequence for product |
|---|---|
| Bimodal distribution — most nights fine, a few severe | Surface episodes and thresholds, never averages |
| Peak at 21:00, indoor lags outdoor | Alert *early*, advise running filtration *late* |
| Wind < 0.5 m/s is the dominant predictor | A forecast needs wind data; PM2.5 history alone can't predict |
| Signature is calm + **cold** + downslope, not dry | Don't use humidity as a risk proxy — it's positively correlated |
| Elevation controls exposure more than suburb | Location-based advice must consider terrain, not postcode |
| No regulatory monitor in this valley | Community sensors are the only viable data source here |
| Sensors produce implausible values | Quality flagging is mandatory, not optional |
| AU and US AQI scales differ substantially | Scale must be explicit and configurable |
| Consumer devices can't measure NO₂ | Don't claim to measure what you can't |

**A transparent rules-based forecast is the right first step** — calm + cold + downslope +
after sunset = elevated risk. Explainable, debuggable, needs no training data, and users can
see the reasoning. Only move to a learned model once there's enough labelled history to beat
the rules, and measure honestly against them.

---

## 9. Open questions

1. **Is the pattern genuinely winter-only?** Rests on subject report plus 22 days of reference
   data. The 365-day sensor record now on disk can settle it.
2. **How steep is the elevation gradient?** Prediction: on a calm cold night, a point 60–80 m
   higher and <2 km away should differ more than Brisbane differs from Sydney. Testable with a
   portable monitor or multi-sensor logging.
3. **Longitudinal profile along the catchment.** The drainage hypothesis predicts monotonic
   increase downstream: the upper catchment → the valley floor → the lower catchment.
4. **Source attribution.** The downslope sector implicates inland sources — domestic solid-fuel
   heating, acreage burn-offs, hazard reduction — but this is inference, not measurement.
   Planned-burn schedules are published and could be cross-referenced against episodes
   automatically. [^21] Note that smoke travelling 30–80 km loses most of its odour-carrying
   volatiles while retaining fine particles, which is consistent with the subject reporting
   symptoms without smelling smoke.
5. **Aircraft ultrafine particles.** Where aircraft routing passes over a catchment, UFPs are
   a candidate: odourless, sub-100 nm and barely registering in PM2.5 mass. Not excluded, not
   evidenced.
6. **How often does an event on that scale occur?** Two trapping nights in seven is not a base
   rate.

---

## Citations

[^1]: Queensland DETSI air monitoring — station list. https://apps.des.qld.gov.au/air-quality/stations/
[^2]: Queensland DETSI air monitoring network. https://apps.des.qld.gov.au/air-quality/ · Bulk data: https://www.data.qld.gov.au/dataset/qld-air-quality-api (CC BY 4.0)
[^3]: A public PurpleAir sensor on the valley floor.
[^4]: Barkjohn, Gantt & Clements (2021), "Development and application of a United States-wide correction for PM2.5 data collected with the PurpleAir sensor", *Atmospheric Measurement Techniques* 14, 4617–4637. https://amt.copernicus.org/articles/14/4617/2021/
[^5]: PurpleAir calibration under high humidity, *AMT* 17, 6735 (2024). https://amt.copernicus.org/articles/17/6735/2024/
[^6]: National Environment Protection (Ambient Air Quality) Measure — PM2.5 standard 25 µg/m³ (24 h). https://www.legislation.gov.au/Series/F2007B01142
[^7]: US EPA, Technical Assistance Document for the Reporting of Daily Air Quality (AQI). https://www.airnow.gov/aqi/aqi-basics/
[^8]: NCCEH, "Wildfire smoke: what is the relationship between outdoor and indoor air?" — review of 20 PM2.5 studies. https://ncceh.ca/resources/evidence-reviews/wildfire-smoke-what-relationship-between-outdoor-and-indoor-air
[^9]: Rajagopalan & Goodman (2021), "Improving the Indoor Air Quality of Residential Buildings during Bushfire Smoke Events", *Climate* 9(2), 32. https://www.mdpi.com/2225-1154/9/2/32
[^10]: "Indoor air quality in Canberra during the 2019–20 bushfires", *Buildings & Cities*. https://journal-buildingscities.org/articles/10.5334/bc.87
[^11]: Liang et al. (2021), "Wildfire smoke impacts on indoor air quality assessed using crowdsourced data in California", *PNAS* 118(36). https://apte.berkeley.edu/wp-content/uploads/2021/08/Liang-et-al-PNAS-2021.pdf
[^12]: AHAM, "Portable Air Cleaners and Air Changes per Hour". https://ahamverifide.org/wp-content/uploads/2021/11/White-Paper-Portable-Air-Cleaners-and-AIr-Changes-per-Hour-FINAL-00106301.pdf
[^13]: Fisk & Chan (2017), "Health benefits and costs of filtration interventions that reduce indoor exposure to PM2.5 during wildfires", *Indoor Air* 27(1). https://onlinelibrary.wiley.com/doi/10.1111/ina.12285
[^14]: US EPA, *Guide to Air Cleaners in the Home*. https://www.epa.gov/indoor-air-quality-iaq/guide-air-cleaners-home
[^15]: US EPA, "What is a MERV rating?" https://www.epa.gov/indoor-air-quality-iaq/what-merv-rating
[^16]: Stephens et al. (2022), "Evaluating Air Cleaners", *ASHRAE Journal*, April 2022. https://www.ashrae.org/file%20library/technical%20resources/covid-19/20-31_stephens.pdf
[^17]: Better Health Channel (Victoria), "Using air conditioners when it's smoky outside". https://www.betterhealth.vic.gov.au/using-air-conditioners-when-its-smoky-outside
[^18]: Air Quality NSW, bushfire health advice. https://www.airquality.nsw.gov.au/health-advice/bushfire-health-advice
[^19]: AusPollen Brisbane (QUT Allergy Research Group). https://auspollen.edu.au/brisbane/
[^20]: Dal Porto et al. (2022), "Characterizing the performance of a do-it-yourself (DIY) box fan air filter", *Aerosol Science & Technology* 56(6). https://www.tandfonline.com/doi/full/10.1080/02786826.2022.2054674
[^21]: Seqwater planned burns. https://www.seqwater.com.au/project/planned-burns
[^22]: CHOICE Australia, air purifier buying guide. https://www.choice.com.au/home-and-living/cooling/air-purifiers/buying-guides/air-purifiers

---

*Analysis of the reference-station and community-sensor datasets was performed for this project;
the derived statistics in §3 are original to it. Underlying air quality data is sourced from the Queensland
DETSI monitoring network (CC BY 4.0) and from PurpleAir. Sensors on the PurpleAir network are
owned and managed by consumers; PurpleAir does not guarantee data accuracy.*
