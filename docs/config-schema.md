# YAML Configuration Schema

The status page reads YAML from `STATUS_PAGE_CONFIG` or `config/status-page.yaml`.

String values in YAML support `${ENV_VAR}` interpolation (for example, `Authorization: Bearer ${API_KEY}`).

Before reading YAML, the app also loads environment variables from `.env` files (without overriding already-exported variables):

- `.env` in current working directory
- `.env` next to the config file
- `.env` in the parent directory of the config file

You can override this behavior with `STATUS_PAGE_DOTENV=/path/to/.env`.

## Top-level schema

```yaml
storage:
  path: data/status.db
  retention_hours: 24

default_sla: 100.0

ui:
  refresh_seconds: 30
  bucket_minutes: 15

services: []
```

## `storage`

- `path` (string, default: `data/status.db`): SQLite file path.
- `retention_hours` (int, default: `24`): historical retention window.

## `ui`

- `refresh_seconds` (int, default: `30`): browser API polling interval.
- `bucket_minutes` (int, default: `15`): timeline bar interval size.

Timeline contains `24 * 60 / bucket_minutes` bars per service.

## `default_sla`

- `default_sla` (number, default: `100.0`): default percentage of successful check samples required in the current SLA window before a check is considered unhealthy.

SLA windows use the same size as `ui.bucket_minutes` (for example, 30-minute windows when `bucket_minutes: 30`). Values are clamped to `0..100`. A check may override this with its own `sla` value.

## `services` (list)

Each service has:

- `id` (string, required): unique identifier.
- `name` (string, required): display name.
- `interval_seconds` (int, default: `60`): check cadence.
- `checks` (list, optional): zero or more check objects.

### Status mapping

- `green`: healthy / expected result
- `yellow`: degraded but partially available
- `red`: failed / unavailable
- `grey`: unknown or no data

Service-level status is the worst status among its checks.

## Check definitions

### 1) HTTP / HTTPS check

```yaml
- type: http   # or https
  url: https://example.com/health
  method: GET
  timeout_seconds: 8
  headers:
    Authorization: Bearer ${API_TOKEN}
    X-API-Key: your-key
  sla: 99.9
  expected_status: [200]
  degraded_statuses: [429, 503]
  body_contains: ok
  body_regex: '"ready":\s*true'
  verify_tls: true
```

Notes:
- `expected_status` may be an int or int list.
- `degraded_statuses` may be an int or int list.
- `headers` is optional and accepts any key/value map for request headers.
- `sla` is optional and overrides top-level `default_sla` for this check.
- If `body_contains`/`body_regex` is set and does not match, check fails (`red`).
- `json_fields` checks are evaluated after status / body checks (see below).

#### `json_fields` — asserting JSON response fields

Assert one or more fields in a JSON response body using dot-notation paths.
List elements can be addressed by numeric index (e.g. `items.0.name`).

```yaml
- type: https
  url: https://api.example.com/health
  json_fields:
    - path: status          # required – dot-notation path into the JSON body
      value: ok             # expected value (uses operator below)
    - path: database.connected
      value: true
    - path: response_time_ms
      operator: lte         # optional – defaults to "eq"
      value: 500
    - path: version
      operator: regex
      value: "^2\\.\\d+"
```

Supported `operator` values:

| Operator | Meaning |
|----------|---------|
| `eq` (default) | `actual == value` |
| `ne` | `actual != value` |
| `gt` | `actual > value` (numeric) |
| `gte` | `actual >= value` (numeric) |
| `lt` | `actual < value` (numeric) |
| `lte` | `actual <= value` (numeric) |
| `contains` | `str(value) in str(actual)` |
| `regex` | `re.search(value, str(actual))` |

If any assertion fails the check is marked `red`.

### 2) DNS check

```yaml
- type: dns
  host: example.com
  record_type: A
  timeout_seconds: 5
  nameserver: 8.8.8.8
  expected_rcode: NOERROR
  expected_values: [93.184.216.34]
```

Supported `expected_rcode` values:
- `NOERROR` (default)
- `NXDOMAIN`
- `NOANSWER`

If `expected_values` is provided, returned values must include them.

### 3) TCP check

```yaml
- type: tcp
  host: db.internal.local
  port: 5432
  timeout_seconds: 3
```

Passes when TCP connection can be established.

### 4) Shell command check

```yaml
- type: command
  command: ["sh", "-c", "./healthcheck.sh"]
  # or command: "./healthcheck.sh"
  shell: false
  timeout_seconds: 10
  expected_return_codes: [0]
```

Passes when process exit code is in `expected_return_codes`.

## Example with zero services

```yaml
services: []
```

The UI will render normally and show that no services are configured.
