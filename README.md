# cruise-deal-hunter

Personal tool that scrapes German cruise portals, tracks price history in
PostgreSQL, detects hot deals (price < 40% of 30-day median, or < 60 €/night),
and sends Telegram alerts.

## Quick start

```bash
cp .env.example .env   # fill in tokens
make up                # postgres + scraper + bot via docker compose
make migrate           # create the schema
make scrape            # run one scrape cycle manually
make test              # run the test suite
```

See [CLAUDE.md](CLAUDE.md) for architecture, schema, and conventions.
