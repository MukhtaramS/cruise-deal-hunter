.PHONY: up down migrate revision scrape seed test logs psql

up:            ## Build images and start db + scraper + bot
	docker compose up -d --build

down:          ## Stop everything (data volume is kept)
	docker compose down

migrate:       ## Apply alembic migrations
	docker compose run --rm scraper alembic upgrade head

revision:      ## Autogenerate a new migration: make revision m="add foo"
	docker compose run --rm scraper alembic revision --autogenerate -m "$(m)"

scrape:        ## Run one scrape cycle immediately
	docker compose run --rm scraper python -m app.jobs

seed:          ## Insert fake price history and trigger a test alert
	docker compose run --rm scraper python -m app.seed

test:          ## Run the test suite (no db needed)
	docker compose run --rm --no-deps scraper pytest -q

logs:          ## Tail logs of all services
	docker compose logs -f

psql:          ## Open a psql shell in the db container
	docker compose exec db psql -U $${POSTGRES_USER:-cruise} -d $${POSTGRES_DB:-cruise}
