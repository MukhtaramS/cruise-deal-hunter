# cruise-deal-hunter — AGENTS.md

## What this project does

Personal tool that scrapes German cruise portals every 4 hours, tracks price
history per cruise/cabin in PostgreSQL, detects hot deals (big price drops or
very cheap per-night prices), and sends alerts via a Telegram bot.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Language | Python 3.12 |
| HTTP | `httpx` (async) |
| HTML parsing | `selectolax`; Playwright only as last resort (optional `browser` extra) |
| Database | PostgreSQL 16 via docker-compose |
| ORM / Migrations | SQLAlchemy 2.0 (sync, psycopg 3) + Alembic |
| Scheduling | APScheduler 3.x (`AsyncIOScheduler`, interval job every 4h) |
| Alerts | `python-telegram-bot` v21 |
| LLM (fallback only) | Groq API, `llama-3.3-70b-versatile` |
| Config | `pydantic-settings` reading `.env` — no secrets in code |

---

## Folder Structure

```
cruise-deal-hunter/
├── docker-compose.yml      # db (postgres:16) + scraper (scheduler) + bot services
├── Dockerfile              # one image, two commands (app.scheduler / app.bot)
├── Makefile                # up, down, migrate, revision, scrape, test, logs, psql
├── alembic/                # migrations; env.py pulls DATABASE_URL from app.config
│   └── versions/0001_initial_schema.py
├── app/
│   ├── config.py           # Settings (pydantic-settings), exported as `settings`
│   ├── db.py               # sync engine, SessionLocal, session_scope()
│   ├── models.py           # Cruise, PriceSnapshot, AlertSent, TelegramChat, RouteCountries
│   ├── profiles.py         # Profile definitions + PROFILE env var resolution
│   ├── visa.py             # visa-free country set (RU) + route-country cache logic
│   ├── deals.py            # hot-deal rules + median + per-profile dedup — NO LLM
│   ├── detector.py         # find_hot_deals (pure detection); HotDeal dataclass
│   ├── alerts.py           # OutgoingAlert, formatting + Telegram delivery
│   ├── llm.py              # Groq: fallback parser, cross-portal dedup, country inference
│   ├── jobs.py             # pipeline: scrape -> store -> detect -> evaluate per profile -> alert
│   ├── scheduler.py        # scraper service entrypoint (APScheduler, every 4h)
│   ├── bot.py              # bot entrypoint (/start, /status, /top, /visafree)
│   ├── seed.py             # fake price history + test alert (make seed)
│   ├── scrape.py           # CLI: run one scraper (--source, --dry-run, --file)
│   └── scrapers/
│       ├── __init__.py       # SCRAPERS registry — register new scrapers here
│       ├── base.py           # BaseScraper ABC + CruiseOffer; rate limit + retry
│       ├── seascanner.py     # seascanner.de JSON API — ACTIVE
│       ├── cruiseportal24.py # cruiseportal24.com JSON API (via cruisec.net widget) — ACTIVE
│       ├── kreuzfahrten.py   # kreuzfahrten.de server-rendered HTML — ACTIVE
│       ├── cruise24.py       # cruise24.de server-rendered HTML, per-cabin prices — ACTIVE
│       ├── kreuzfahrt_de.py  # kreuzfahrt.de via cruiseportal.de widget AJAX — ACTIVE
│       └── dreamlines.py     # dreamlines.de — UNREGISTERED (Cloudflare, see below)
├── samples/                # raw saved portal pages (gitignored) — source for fixtures below
└── tests/                  # deal rules, detector, dedup, alert format, scrapers
    └── fixtures/           # saved pages/responses the parser tests run against
                            # seascanner.json, cruiseportal24*.json, kreuzfahrten.de.html
                            # are all REAL captures, not synthetic
```

---

## Database Schema

| Table | Columns | Notes |
|---|---|---|
| `cruises` | id, source, cruise_line, ship, title, route_hash, departure_port, departure_date, nights, url | Unique on (source, url). `route_hash` indexed — same sailing across portals shares it |
| `price_snapshots` | id, cruise_id FK, cabin_type, price_eur, scraped_at | One row per cruise/cabin per scrape. Index on (cruise_id, scraped_at) |
| `alerts_sent` | cruise_id FK, price_eur, profile, sent_at | **Composite PK (cruise_id, price_eur, profile)**. Dedup rule *per profile*: a new alert fires only if the price is a fresh new low — anything already alerted at the **same or a lower** price within that profile suppresses it. Profiles never suppress each other |
| `telegram_chats` | chat_id (BigInteger PK), subscribed_at | Chats subscribed via /start; alerts go to all of them (+ TELEGRAM_CHAT_ID env fallback) |
| `route_countries` | route_hash PK, countries, inferred_at | LLM-inferred countries per route ("TR,GR", empty = LLM unsure, cached; no row = never asked / failed call, retried next run) |

Migrations live in `alembic/versions/`. Autogenerate new ones with
`make revision m="description"` (requires db up).

---

## Key Patterns

### Deal logic (app/deals.py) — pure Python, no LLM
Hot deal if **either**:
- current price **<** 40% of the median price for that cruise **and cabin type**
  over the last 30 days, or
- price per night **<** 60 EUR.

Thresholds are configurable (`HOT_DEAL_MEDIAN_RATIO`, `HOT_DEAL_MAX_PRICE_PER_NIGHT`).
Both comparisons are strict `<`. **Design decisions:** the median is computed per
(cruise, cabin_type), not per cruise overall — mixing suite and inside prices
into one median would produce false "drops". The median is `statistics.median`
in Python, not SQL — snapshot counts are tiny and it keeps deals/detector
testable on in-memory SQLite.

### Detection (app/detector.py)
`detect_hot_deals(session, since)` runs over **DB state**, not in-memory offers:
it takes the newest snapshot per (cruise, cabin) scraped since `since` (the run
start), applies the deal rules, and returns `HotDeal` dataclasses (plain values,
safe after session close). It records `AlertSent` rows in the same transaction
as the snapshots — a crash can at worst drop an alert, never duplicate one.

### Alert dedup — same or lower price
`deals.alerted_at_or_below()`: if any `alerts_sent` row exists for the cruise at
`price_eur <=` the current price, skip. So 1499→199 alerts, a bounce to 299
stays silent, and a further drop to 149 alerts again.

### Alert format (app/alerts.py)
```
🔥 -87% | AIDAnova, 7 nights, Hamburg, 12.09
199€ (was 1499€ median)
https://portal.example/offer
```
When the deal fired on the €/night rule with no real drop vs the median, the
header shows `🔥 57€/night | …` instead of a percentage.

### Scraper contract (app/scrapers/base.py)
Every portal gets a `BaseScraper` subclass with a `source: ClassVar[str]` slug
and `async fetch() -> list[CruiseOffer]`; server-rendered portals also
implement `parse(html)` and call `self.parse_with_fallback(html)` from
`fetch()`. Register the class in `app/scrapers/__init__.py:SCRAPERS` — that's
all the scheduler needs.

Built into `http_get` (all scrapers get this for free):
- rate limit 1 req/3s (`min_request_interval`), realistic browser headers
- retry with exponential backoff (5s/10s/20s, `max_retries=3`) on 429/5xx and
  transport errors, honoring `Retry-After`
- tests inject `httpx.MockTransport` via the constructor's `transport` arg

Parsing strategy, in order:
1. CSS selectors via selectolax (`parse`); a single broken card is skipped,
   it doesn't kill the page.
2. `parse_with_fallback`: on a parse crash or 0 offers from non-empty HTML
   (> 500 chars) → `app.llm.extract_offers(html, source)` (Groq fallback).
   Genuinely empty pages do NOT trigger the LLM.
3. Playwright only for JS-walled portals (`pip install -e ".[browser]"`).

### Scraper CLI (app/scrape.py)
```bash
python -m app.scrape --source dreamlines --dry-run                    # live fetch, print only
python -m app.scrape --source dreamlines --dry-run --file page.html   # parse a local save
python -m app.scrape --source dreamlines                              # store + detect + alert
```

### Adding a scraper: fixture workflow
Save a real results page to `samples/<portal>.html` (gitignored), trim it to a
few representative cards (include one broken card) into
`tests/fixtures/<portal>.html` (committed), write `parse` against it, verify
with `python -m app.scrape --source <portal> --dry-run --file tests/fixtures/<portal>.html`.

### seascanner.py — the active scraper (JSON API, no HTML parsing)
seascanner.de is a React app on the Dreamlake platform; results come from:
```
GET https://www.seascanner.de/api/packages/search?pageSize=50&pageNumber=0
header: domain: www.seascanner.de     # mandatory, else 400 "Domain is required"
```
~7000 items total; the scraper fetches `max_pages`(4) × `page_size`(50) per
run, newest departures first, stopping early on an empty page. Lead price per
item = cheapest cabin, `price.superCategory` (INSIDE/OUTSIDE/BALCONY/SUITE)
maps to our cabin_type. Sold-out / non-bookable items are skipped. Detail
URLs: `/reisen/<item code>`. Endpoint discovered by grepping the site's JS
bundles (`searchPackages` in `_app-*.js`); verified live 2026-07-10.

### cruiseportal24.py — JSON API via a third-party widget, not the site itself
cruiseportal24.com is a small Jimdo-built site with no cruise data of its own
— every listing page just embeds an iframe from a whitelabel cruise-search
widget, **cpx.cruisec.net** (partner/agency id `aid=203142`), running an
AngularJS SPA. The widget's own endpoint holds the real data:
```
GET https://cpx.cruisec.net/api/Search/Results/json
    ?aid=203142&url=Meer/all/all/all/all/all/all&page=N
header: X-Requested-With: XMLHttpRequest
```
`url` is the widget's internal filter path (`<sea>/<area>/<line>/<ship>/
<duration>/<departure>/<sort>`); `Meer/all/all/all/all/all/all` is the
canonical "ocean cruises, no filters" search — confirmed by hitting the
facet endpoint (`/api/Search/count/json?aid=203142`), whose response reports
that exact string as its own current-filter `url` (78,332 total cruises with
zero filters, verified live 2026-07-17). Discovering the right `url=` value
took real reverse-engineering: the human-readable path segments visible in
the site's iframe src (`start/Kreuzfahrten/Meer/Mittelamerika_Karibik/AIDA_
Cruises+.../all/all/all/all`) are NOT what the Results endpoint accepts —
every guess along those lines returned `{"error":{"code":107,"msg":"EMPTY
RESULT"}}`. The facet endpoint's own `url` field was what revealed the real
format.

**PHP json_encode quirk (real bug, hit and fixed during dry-run testing):**
`cruises` is a JSON *array* when its keys start at 0 (page 1), but a JSON
*object* when they don't (page 2 = indices 10-19, etc.) — classic PHP
associative-array serialization. `parse()` normalizes both shapes to a list
before iterating; `tests/fixtures/cruiseportal24_page2_dict_shape.json` is a
real captured page-2 response pinning this down. Without the fix, every page
past the first silently fell through to the Groq fallback in production.

10 items/page, `pages` in the response gives the total; the scraper stops at
`max_pages`(10) or an empty/error page. No explicit cabin-type field (unlike
Seascanner's superCategory) — lead price is the cheapest cabin, so
`cabin_type` defaults to `"inside"`. `departure_port` is the first stop in
the `route` string (`"Fort Lauderdale - Seetag - Key West - ..."` → `"Fort
Lauderdale"`). Non-EUR items are skipped (all observed so far are EUR).

⚠️ Detail-page URL (`/product/<productID>?aid=203142`) is an educated guess,
**not verified**: the JSON has no `link` field, and cpx.cruisec.net's SPA
has a catch-all route (every path probed returned HTTP 200, including
obviously-wrong ones), so empirical testing couldn't disambiguate. Confirm
with real browser DevTools (click a result card, watch the address bar)
before trusting these URLs for outbound alert links.

### kreuzfahrten.py — server-rendered HTML, no API, no JS framework
kreuzfahrten.de has no JSON API or JS bundle worth grepping — it's a classic
PHP-style site, and results are baked directly into the HTML:
```
GET https://www.kreuzfahrten.de/termin/
    ?srcOrderBy=c_dateDepart_ASC&strOrderBy=c_dateDepart_ASC
    &srcPriceMin=0&srcPriceMax=0&page=N&per-page=10
```
`srcPriceMin=0&srcPriceMax=0` is the site's own sentinel for "no price
filter" — confirmed live 2026-07-18: the bare `/termin/` with zero params
reports the same "19.117 Routen, 46.223 Reisen" as pages carrying these
exact params in their own pagination links. 10 cards (`div.routeListItem`)
per page; detail URL is `/termin/<data-cruise-id>.html`.

Two fields needed workarounds:
- **cabin_type**: the compact card shows one lead ("ab") price with no
  breakdown, so it defaults to `"inside"` — same convention as the other
  two scrapers.
- **cruise_line**: no dedicated text field exists. The obvious source, the
  vendor logo `<img alt>`, is unreliable in practice — confirmed on real
  cards it's sometimes empty (Cunard, Norwegian) and sometimes a generic
  placeholder ("Impressionen" on a Royal Caribbean card) instead of the
  real name. `cruise_line_from_logo_url()` derives it from the logo
  **filename** instead (e.g. `Carnival-Cruise-Lines-13.png` → "Carnival
  Cruise Lines"), which held up across every real card sampled. Known
  artifact: a trailing abbreviation baked into a lowercase slug (e.g.
  `norwegian-cruise-line-ncl-...`) survives title-casing as an extra word
  ("Norwegian Cruise Line Ncl").

Sold-out / price-on-request cards (`class="price hidden"` with
`class="priceInquiry"` visible, CTA reads "Ausgebucht") are skipped — the
fixture keeps two real, naturally-occurring examples rather than a
synthetic one.

Reuses `parse_german_date` and `parse_nights` from `dreamlines.py` (format-
compatible); needed its own `parse_price` since this site puts `€` *before*
the number with a literal `,-` suffix (never real cents), unlike
Dreamlines' `<number>€` format.

### cruise24.py — server-rendered HTML with a real per-cabin price table
cruise24.de is a jQuery-era site (no framework, no API — main.min.js is
jQuery + owl-carousel, search-url-generator.js only builds filter URLs
client-side). Results are baked into the listing:
```
GET https://www.cruise24.de/kreuzfahrt/sort-price        # offset 0
GET https://www.cruise24.de/kreuzfahrt/sort-price/50     # offset 50, etc.
```
Pagination is a trailing **offset** segment, not a page number — 50 cards
per page, "Seite: 1 von 193" (~9.6k offers) at recon 2026-07-18. Unknown
path segments are silently ignored (all `page-2`-style guesses returned
page 1; the real grammar came from the rendered pagination nav).
`sort-price` = ascending, so the cheapest cruises come first.

**First scraper emitting multiple cabin types per cruise**: each card has a
real cabin table, so one card yields up to four CruiseOffers (same URL,
different cabin_type + price). The pipeline handles this natively —
`store_snapshots` upserts the cruise once by (source, url), one snapshot
per offer, detector medians per (cruise, cabin_type).

Quirks (all confirmed on real pages):
- Cabin labels map Innen/Außen/Balkon/Suite → our four types; **"Yacht
  Club" and "Kind" rows are deliberately dropped**. "Kind" is a child
  supplement (22.50€ for 3 nights on a real card) that would falsely
  trigger the <60€/night rule; "Yacht Club" would collide with the real
  Suite row as a second "suite" snapshot in the same scrape.
- Prices are DOT-decimal with no thousands separator even at 5 digits
  ("21995.00€") — un-German, has its own `parse_price`. "ausgebucht" and
  "ab 0.00€" rows (both seen live) are skipped.
- A card whose whole cabin table is sold out yields no offers (real
  example in the fixture); the headline `big_price` is ignored — on the
  sold-out card it even shows a stale price.
- Departure port = first stop of `div.rout`; port and country are
  TAB-separated ("Miami\t USA" → "Miami").
- Cruise line = 2nd path segment of the detail link
  (`/details/<id>/MSC-Cruises/...` → "MSC Cruises"); hyphens become
  spaces, so genuinely hyphenated brands (A-ROSA) would lose the hyphen.

### kreuzfahrt_de.py — AJAX HTML-fragment API via the cruiseportal.de widget
kreuzfahrt.de (**singular** — a different site from kreuzfahrten.de!) is a
Yii/PHP shell with no data of its own: /search embeds an iframe from
**cruiseportal.de** — the same JT-Touristik platform that powers
kreuzfahrten.de (identical image paths and special IDs). The iframe shell
loads its list client-side (InfiniteScrolling.js) from:
```
GET https://www.cruiseportal.de/modul/site3/ajax/routesCruise.ax.html
    ?ref=kreuzfahrt&la=de&srcShipType=1&srcBookedUp=0&srcPriceMin=0
    &srcPriceMax=0&srcOrderBy=pr_MinPriceInnen_ASC
    &action=loadInfiniteScrolling&intParentSiteID=27803&srcStartRoutes=<offset>
```
Response is JSON with an **HTML fragment** (`htmlRouten`): 5 `li#cruiseItemN`
cards per batch, `srcStartRoutes` = offset. `intParentSiteID=27803` comes
from the iframe shell's own SITE_ID constant. `srcBookedUp=0` filters
sold-out sailings server-side. Detail URLs point back to kreuzfahrt.de
(`/cruise/<id>`, verified 200). Source slug is `kreuzfahrt_de` (not
`kreuzfahrt`) to stay more than one typo away from `kreuzfahrten` in the CLI.

Quirks (all confirmed on real fragments, captured 2026-07-18):
- Cards list many alternative dates ("10 Termine verfügbar"); only the
  **"Gewählter Termin"** matches the displayed price — it's the first
  dd.mm.yyyy in the `.date` block, which is what `parse_german_date` grabs.
- Price format "p.P. ab € 51,-" is identical to kreuzfahrten.de (same
  platform) → reuses its `parse_price`.
- Departure port: "Genua - Marseille" harbor line is a DIRECT text node in
  `.route-list-item-bottom`, extracted with `text(deep=False)` so the
  sibling "Routeninfo" span doesn't bleed in.
- **Cruise line**: vendor logo filename is only a numeric id
  (`/vendor/16-20200103.png`). The widget's own filter endpoint
  (`vs3/ajax/routesFilter.ax.html?...action=loadFilterValues&uriName=
  v_VendorID`) returns the authoritative id→name map (49 vendors);
  `fetch()` refreshes it once per run (+1 request), and a real static
  snapshot (`DEFAULT_VENDORS`, pinned by test against the captured
  response) covers --file mode, tests, and failed refreshes. Unknown
  vendor ids skip the card — accurate data or nothing.
- Only 5 cards/batch → 10 batches = 50 cruises/run, cheapest-first. Lead
  price only, cabin_type "inside" (same convention as other lead-price
  sources).

### ⚠️ dreamlines.py status — kept but UNREGISTERED
dreamlines.de sits behind Cloudflare (403 for plain HTTP clients), so the
scraper is not in SCRAPERS. Its selectors were never verified against the
real site (the fixture is synthetic) — before re-registering: solve Cloudflare
(likely Playwright via the `browser` extra), save a real page, fix the
selectors in `_parse_card`, replace the fixture. The German price/date/nights
helpers in it are solid, unit-tested, and reusable for other SSR portals.

### CruiseOffer.route_hash
sha1 over normalized (cruise_line, ship, departure_port, departure_date,
nights). Primary cross-portal dedup mechanism. `app.llm.is_same_cruise(a, b)`
is the fuzzy LLM fallback for spelling variants the hash can't catch.

### Profiles (app/profiles.py) — PROFILE env var
Two independent deal-hunting configurations share one codebase, DB, and bot:
- `default` — all scrapers, no visa filter (the original behavior)
- `visa_ru` — RU-relevant scrapers only, alerts **only** when ALL inferred
  countries of a route are visa-free for Russian passports
- `PROFILE=all` (the default) runs both in one process.

Mechanics: scraping uses the **union** of the active profiles' scraper lists.
Detection (`find_hot_deals`) is profile-independent; `jobs.evaluate_deals`
then filters/dedups **per profile** and records `alerts_sent` rows per
profile. Because visa_ru's alert set is a subset of default's, a deal passing
both profiles under "all" is sent as ONE message with a `✈️ Visa-free`
badge line — never a duplicate. Running `PROFILE=visa_ru` alone
sends only visa-free alerts, with dedup fully independent of default's
history (migration 0003 seeded both profiles with pre-existing alerts so the
switch doesn't replay old deals).

### Visa filter (app/visa.py)
`VISA_FREE_RU` = ISO alpha-2 codes visa-free for RU passports (hand-
maintained). Ports/countries aren't in scraped data, so countries per route
are inferred by Groq from title/ship/departure port (`llm.infer_countries`,
batched 25/call) and cached forever in `route_countries` by route_hash.
Unknown-countries routes conservatively NEVER pass the filter. A failed Groq
call caches nothing (retried next run) and never crashes the pipeline. The
default profile alone triggers zero LLM calls.

### LLM boundaries
Groq is used **only** in `app/llm.py` for (1) fallback HTML parsing,
(2) fuzzy cross-portal dedup, (3) route-country inference for the visa
filter. Deal detection math and alerting never call the LLM.

### Sync DB, async scraping
Scrapers fetch async (httpx); DB writes are sync SQLAlchemy inside
`session_scope()`. `app/jobs.py:run_scrape()` bridges the two: gather offers
async, then process them in one transaction, then send Telegram alerts async.

### Two services, one image
`docker-compose.yml` runs the same image with different commands:
`python -m app.scheduler` (scraper service) and `python -m app.bot` (bot
service). Alerts are sent by the **scraper** service directly via the Bot API
to every chat in `telegram_chats`; the bot service only answers commands:
/start (subscribe, stores chat_id), /status (last scrape, cruises tracked,
deals this week), /top (5 cheapest per-night current offers), /visafree
(10 cheapest per-night among routes fully visa-free for RU passports — needs
the route_countries cache, i.e. a scrape under PROFILE=visa_ru or all).

### Testing alerts without waiting 30 days
`make seed` (`python -m app.seed`) inserts three fake cruises under
source='seed' with 30 days of history — one -87% drop, one cheap-per-night,
one control — then runs the detector and sends/prints the alerts.
Re-runnable; it wipes previous seed rows first.

---

## Environment

Copy `.env.example` to `.env` and fill in:

```
DATABASE_URL=postgresql+psycopg://cruise:cruise@db:5432/cruise   # "localhost" outside docker
TELEGRAM_BOT_TOKEN=   # from @BotFather
TELEGRAM_CHAT_ID=     # message /start to the running bot to get it
GROQ_API_KEY=
```

---

## Commands

```bash
make up        # build + start db, scraper, bot
make migrate   # alembic upgrade head
make scrape    # run one scrape cycle now
make seed      # fake price history + test alert
make test      # pytest (logic + detector tests, no Postgres needed)
make revision m="add column x"   # autogenerate migration
make logs / make psql / make down
```

First-time setup: `cp .env.example .env` → fill secrets → `make up` → `make migrate`.

Active scrapers: `seascanner`, `cruiseportal24` (JSON APIs), `kreuzfahrten`,
`cruise24` (server-rendered HTML), `kreuzfahrt_de` (widget AJAX fragment) —
see their sections above. `dreamlines` exists but is unregistered pending
Cloudflare.
