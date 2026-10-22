# Diagnostic history and read-only queries

Infra Sentinel records changes in existing observations: collection errors,
network alerts, host pressure, facility issues, and official upstream status.
It does not collect new request content or probe additional services. The
Python Agent owns normalization and persistence; the desktop and local query
entry points consume the same bounded contract.

## What a record means

- `raised`: the Agent first observed a condition, or observed it again after a
  confirmed recovery.
- `changed`: an unresolved condition changed its confirmed severity.
- `recovered`: a subsequent, usable observation explicitly cleared the condition.

Repeated samples do not append duplicate events. State and transitions are
committed together, so restarting the Agent preserves their identity. Records
start with the first observation after this feature is enabled; older logs are
not replayed into the journal.

Missing, disabled, stale, and unavailable sources cannot confirm recovery.
Unresolved conditions retain an `observation_status` of `unknown`, `disabled`,
or `unobserved` when appropriate. The unresolved count therefore does not mean
that every condition is still a confirmed live failure.
Pending confirmation after an Agent restart also cannot clear an existing
condition. Source collection, facility discovery, and connection checks retain
their actual attempt times; reading a cached snapshot does not make it fresh.
An error without a known attempt time has unknown coverage. A successful
facility connection also needs a current provider snapshot to confirm recovery.

Event times are observation times, not a claim about the exact instant the
underlying failure began. Concurrent events are troubleshooting context, not
proof of causality. Sleep, downtime, missing sources, and disabled collection
can leave observation gaps.

## Read from a local Agent or terminal

From a source checkout:

```sh
PYTHONPATH=src python3 -m infra_sentinel.cli.diagnostics --hours 24 --limit 50
PYTHONPATH=src python3 -m infra_sentinel.cli.diagnostics --category facility --hours 168
PYTHONPATH=src python3 -m infra_sentinel.cli.diagnostics --transition recovered --limit 20
```

Installing the Python package also provides `infra-sentinel-diagnostics`.
`--config` selects the configuration location; `--state-dir` selects an existing
Agent state directory. Both are local CLI options, never desktop query fields.
Without either option, the normal Agent configuration and state-directory
override apply. `--since` and `--until` accept ISO 8601 timestamps with an
explicit timezone. The default CLI window is 24 hours; `--hours` accepts a
positive duration up to 720 hours.
Diagnostic reads resolve only the state location and do not parse, migrate, or
rewrite configuration files.

The packaged Agent exposes the same journal without starting the sampler:

```sh
infra-agent --config /path/to/config.toml --diagnostics
infra-agent --config /path/to/config.toml --diagnostics --diagnostics-query '{"category":"network","limit":20}'
```

For the default macOS desktop installation, select its state explicitly when
querying from a source checkout:

```sh
PYTHONPATH=src python3 -m infra_sentinel.cli.diagnostics \
  --state-dir "$HOME/Library/Application Support/Infra Sentinel/state" --hours 24
```

The packaged binary is inside `Infra Sentinel.app/Contents/MacOS/infra-agent`.
Its `--config` should point to
`~/Library/Application Support/Infra Sentinel/config.toml`, or it can inherit
`INFRA_SENTINEL_STATE_DIR`. Running it without either uses the normal Agent
default location, which can differ from the desktop's state directory.

Both entry points write JSON to stdout. Exit code `0` means a query was served,
`1` means the journal is unavailable or has not been created, and `2` means the
arguments or query were invalid. A successful query may contain stale data;
always inspect `freshness`, `observed_at`, and `coverage`. Queries do not create
missing state, contact providers, start collection, or repair data.

## Desktop command and response

The existing allowlisted local command transport accepts `diagnostics.query`.
Its payload accepts only:

| Field | Meaning |
| --- | --- |
| `category` | `source`, `network`, `system`, `facility`, or `upstream` |
| `source_id` | A bounded source identity returned by a prior query |
| `severity` | `info`, `warning`, or `critical` |
| `transition` | `raised`, `changed`, or `recovered` |
| `since_epoch`, `until_epoch` | Inclusive Unix timestamp bounds for events |
| `limit` | Integer event limit from 1 to 200 |

Unknown fields are rejected. There is no raw SQL, filesystem path, arbitrary
command, or mutating diagnostic operation in this interface.

The response uses schema `20261022.1` and includes:

- `available`, with a fixed `error_code` when unavailable;
- `observed_at`, the latest persisted sampling time, and `generated_at`, the
  query time; reading never updates the observation time;
- `freshness`: `current`, `stale`, or `unknown`; the journal becomes stale after
  60 seconds without a new sample, while individual sources retain their own
  observation coverage;
- `active` and `active_count`, the current unresolved conditions;
- `events`, ordered newest first, and `truncated` when more matching events
  exist than the requested limit;
- `history_truncated` when the retention or storage cap has removed records,
  and `sample_truncated` when the latest normalized input exceeded its bound;
- `coverage`, describing the observation state of included sources;
- `history_started_at` and `retention_days`.

Category, source, and severity filters apply to conditions and events. Time and
transition filters apply only to events: unresolved conditions always describe
the latest known state, not a historical reconstruction. Coverage follows the
category and source filters.

## Storage and privacy

The Agent writes `diagnostics.sqlite3` inside its existing state directory.
The journal has independent tables and does not alter Token or network metric
accounting. History is bounded to 30 days and 10,000 events. Unresolved
conditions survive the age limit, with a separate cap of 512 conditions;
coverage is capped at 1,024 checks. It is not an audit log with unlimited
retention. Storage failure makes diagnostics explicitly
unavailable while the existing resource projection continues.
The journal uses short rollback-journal transactions. Queries open it in
read-only mode and do not create database sidecars; an unexpected WAL database
is reported unavailable until the sampling writer can restore the supported mode.

Persisted fields use bounded identities, fixed reason codes and display text,
severity, observation state, and timestamps. Source-specific private identities
are represented by stable hashes where needed. Prompts, responses, raw error
text, URLs, paths, tool arguments, and credentials are excluded. Existing
resource-specific panels and facility Consoles remain the places for deeper
diagnosis.
