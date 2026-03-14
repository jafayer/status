# Status Page (FastHTML + YAML)

A Docker-able status page that:

- reads monitor definitions from YAML
- executes checks on an interval
- stores up to 24 hours of historical data in SQLite
- renders a mobile-responsive page with a vertical-bar timeline
- auto-refreshes in the browser using `/api/status`

## Features

- **Config-driven**: add/change services without code changes
- **Check types**:
  - HTTP / HTTPS (status code, body substring, regex)
  - DNS (rcode + returned values)
  - TCP connectivity
  - shell command exit code
- **Durable history**: SQLite with retention pruning
- **Conventional status colors**:
  - green = available
  - yellow = degraded
  - red = offline/failing
  - grey = unknown/no data

## Project structure

- [main.py](main.py)
- [status_page/app.py](status_page/app.py)
- [status_page/config.py](status_page/config.py)
- [status_page/engine.py](status_page/engine.py)
- [status_page/checks.py](status_page/checks.py)
- [status_page/storage.py](status_page/storage.py)
- [config/status-page.yaml](config/status-page.yaml)
- [docs/config-schema.md](docs/config-schema.md)

## Configuration

Default config path: `config/status-page.yaml`

Override via env var:

- `STATUS_PAGE_CONFIG=/path/to/your.yaml`

Full schema and examples are in [docs/config-schema.md](docs/config-schema.md).

## Local run

1. Install deps
2. Start app

The app binds to `0.0.0.0:8080` by default.

Environment overrides:

- `STATUS_PAGE_HOST` (default `0.0.0.0`)
- `STATUS_PAGE_PORT` (default `8080`)
- `STATUS_PAGE_CONFIG` (default `config/status-page.yaml`)

## Docker

Build image:

- `docker build -t status-page:latest .`

Run with persistent data volume:

- `docker run --rm -p 8080:8080 -v $(pwd)/data:/app/data -v $(pwd)/config:/app/config status-page:latest`

Open:

- `http://localhost:8080`

## API

### `GET /api/status`

Returns current status and 24h timeline buckets:

- `generated_at`
- `refresh_seconds`
- `bucket_minutes`
- `services[]` with
  - `id`, `name`, `state`, `summary`, `checked_at`
  - `checks[]`
  - `buckets[]` (`green|yellow|red|grey`)
