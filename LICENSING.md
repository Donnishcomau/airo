# Licensing

Airo is **dual-licensed**: AGPL-3.0-or-later for everyone, plus a commercial licence for
anyone who needs different terms.

---

## The open source licence — AGPL-3.0-or-later

The code in this repository is licensed under the
[GNU Affero General Public License v3.0](LICENSE).

In practice, for almost everyone:

- **Run it on your own machine, for yourself or your organisation** — no
  obligations beyond keeping the copyright notice.
- **Modify it, fork it, publish your fork** — yes, under the same licence.
- **Contribute changes back** — very welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).

The one clause that distinguishes AGPL from GPL is §13. If you **run a modified
version as a network service** that other people use, you must offer those users
the source of your modified version. Running the unmodified project for yourself
triggers nothing.

### Why AGPL and not MIT

Airo started under MIT. It moved to AGPL so that anyone who runs Airo as a
network service for others must publish their changes — this project included,
should it ever run one. The reasoning, stated plainly so contributors can
judge it:

- A permissive licence would let a third party take the project, run it as a
  closed hosted service, and contribute nothing back — while the maintainers
  carry the cost of the open version.
- AGPL keeps self-hosting completely free and unrestricted, which is the case
  almost every user is in, while requiring that anyone offering it *as a
  service* shares their improvements.

This is a deliberate trade. AGPL is incompatible with inclusion in some
proprietary codebases, and some organisations forbid it outright. That is what
the commercial licence is for.

## The commercial licence

If AGPL does not suit you — you want to embed Airo in a proprietary product,
run a modified version as a service without publishing your changes, or your
organisation prohibits AGPL dependencies — a commercial licence is available.

Contact: **quinn@donnish.com.au**

## Contributing and copyright

Contributions are accepted under the AGPL, and contributors also grant a
**sublicensable** licence — the thing that allows the same code to be offered
commercially. The terms are in **[CLA.md](CLA.md)**, agreed by signing off each
commit; contributors keep their copyright, and [CONTRIBUTING.md](CONTRIBUTING.md)
is where the process lives.

---

## Third-party data licensing — read this before redistributing anything

**The licence above covers Airo's code. It does not cover the data Airo
retrieves.** Each source carries its own terms, and they differ materially.
Airo records the licence of every configured source in `--list-sources` and in
`latest.json`, so you can always check what applies to the data you hold.

| Source | Data licence | What it means for you |
|---|---|---|
| **PurpleAir** | PurpleAir Terms of Service | **Do not redistribute.** ToS §4.3 prohibits making PurpleAir data available to third parties. Storing and analysing it locally is fine and explicitly encouraged. |
| **Queensland Government** | CC BY 4.0 | Free to redistribute with attribution. |
| **NSW Government** | CC BY 4.0 | Free to redistribute with attribution. Keyless, so it is one of the two networks a new user can read immediately — the obligation applies from the first poll. |
| **OpenAQ** | **Per source** | OpenAQ aggregates networks with differing terms and publishes a Licenses resource for this reason. There is no blanket licence — check the licence of the specific station you use. |
| **Open-Meteo** (weather) | CC BY 4.0 | Free to redistribute with attribution. Not an air-quality source: Airo reads wind, temperature, humidity and pressure from it to record the *conditions* a reading was taken in — ROADMAP #9. Credited in `latest.json` only once an install has actually stored weather, so nobody is credited for data an install never used. |

### A note on what the weather is for

Weather is captured so a forecast can eventually be built on it (ROADMAP #9
Phase C). That makes **PurpleAir ToS §4.4** relevant in advance: it grants
PurpleAir a perpetual, sublicensable licence over models *derived from their
data*, so a forecast trained on a PurpleAir feed would not be exclusively the
author's — and neither would anything built on it later.

`forecast.training_sources()` already excludes those rows by construction, so
the constraint is enforced rather than remembered. Capturing weather does not
change that: weather is CC BY and carries no such term, but the PM2.5 side of
any training pair still has to come from government data.

### Specific cautions

**Do not commit `data/` to a public repository.** It is gitignored for this
reason. Beyond the licensing question, a location-tagged air quality history
reveals where you live and when you are home.

**PurpleAir ToS §4.5 ("No Open Source Materials")** names open source licences
and restricts using such materials to create data derivatives. Read literally,
an AGPL air-quality logger sits inside that restriction — moving from MIT to
AGPL makes the project more emphatically open source, not less. PurpleAir appear
not to enforce this, and ship their own tooling under an open licence, but it is
an unresolved tension rather than a settled question. The practical consequence
is the architecture described below: government open data as the licensed
backbone, PurpleAir as an optional bring-your-own-key enhancement.

**PurpleAir ToS §4.4** grants PurpleAir a perpetual, sublicensable licence over
models derived from their data. Any forecasting model trained on PurpleAir
readings may therefore not be exclusively yours. Training on government open
data avoids this entirely.

**Do not build a subscription service on a single shared PurpleAir key.** Their
terms prohibit "service bureau" use, which is precisely what one key serving
many paying users describes. Charging for an app is not prohibited; embedding a
shared key in it is. The supportable architecture is government open data as the
licensed backbone, with PurpleAir as an optional bring-your-own-key enhancement
each user enables with their own credentials — which is exactly how Airo's
provider system is built.

## Not medical advice

Airo reports readings from instruments that can over-read at high humidity and
can fail in ways that look like real data. It is not a medical device and gives
no medical advice. Do not use it for health or safety-critical decisions.
