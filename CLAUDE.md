# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A data pipeline and analytics dashboard for Godot Engine GitHub pull request data. It fetches PR metadata, reviews, comments, reactions, and file changes via the `gh` CLI, stores everything in DuckDB, and generates a self-contained HTML analytics dashboard.

The database is hosted on MotherDuck (cloud DuckDB). The local file `godot_prs.duckdb` is also used during development, but the canonical copy lives in MotherDuck and can be queried directly using `md:` connection strings or via the MotherDuck MCP tools available in Claude Code.

## Commands

```powershell
# Install dependencies
uv sync

# Fetch all data (incremental — only re-fetches updated PRs)
uv run python fetch_godot_prs.py

# Run specific phases only
uv run python fetch_godot_prs.py --phases 1,2,3

# Drop all data and start over
uv run python fetch_godot_prs.py --fresh

# More concurrent API workers (default: 8)
uv run python fetch_godot_prs.py --workers 12

# Backfill additions/deletions/file data for merged PRs
uv run python fetch_godot_prs.py --backfill-merged --backfill-years 2

# Preview backfill time estimate without fetching
uv run python fetch_godot_prs.py --estimate --backfill-merged

# Generate the HTML dashboard + social media PNGs (connects to MotherDuck by default)
uv run python analytics_dashboard.py

# Override the database (e.g. a local file)
uv run python analytics_dashboard.py --db godot_prs.duckdb
```

The `gh` CLI must be authenticated (`gh auth login`) before fetching.

## Architecture

### Data flow

`fetch_godot_prs.py` → `godot_prs.duckdb` (local) / MotherDuck (hosted) → `analytics_dashboard.py` → `godot_analytics_dashboard.html`

### Fetch phases

The fetcher is split into resumable, idempotent phases:

| Phase | What it fetches |
|-------|----------------|
| 1 | Bulk PR list — base fields, sorted by `updated_at` descending (incremental via cutoff) |
| 2 | PR detail — merge info, additions/deletions, commit count (open PRs + any updated since last run) |
| 3 | Reactions — via `/issues/{n}` endpoint (open PRs only) |
| 4a | Reviews per PR |
| 4b | Issue comments per PR |
| 4c | Inline review comments per PR |
| 4d | Changed files per PR; stamps `social_fetched_at` when complete |

Phases 4a–4d share the same queue: open PRs where `social_fetched_at IS NULL OR updated_at > social_fetched_at`.

### Rate limiting

`RateLimiter` is a thread-safe token bucket targeting 4,500 requests/hour (leaves 500 buffer from GitHub's 5,000/hour limit). It's shared across all `ThreadPoolExecutor` workers. On a `RateLimitError` from the API, the limiter backs off 65 seconds.

### Incremental updates

Phase 1 stores `last_list_fetch` in the `fetch_state` table. On subsequent runs it stops paginating once it reaches PRs older than that timestamp, then saves `prev_last_list_fetch` so phases 2–3 know which PRs need re-enrichment.

### DuckDB schema

Eight tables in `godot_prs.duckdb`:
- `pull_requests` — one row per PR, all scalar fields plus reaction counts and fetch timestamps
- `pr_labels`, `pr_assignees`, `pr_requested_reviewers` — child tables, delete-then-reinsert on each list update so removals are reflected
- `pr_reviews`, `pr_issue_comments`, `pr_review_comments`, `pr_files` — enrichment data, INSERT OR REPLACE
- `fetch_state` — key/value table for resumability state (`last_list_fetch`, `prev_last_list_fetch`)

### Dashboard

`analytics_dashboard.py` connects to `godot_prs.duckdb`, runs DuckDB SQL queries directly to pandas DataFrames, builds Plotly figures, and writes a single self-contained HTML file with inline JavaScript. Hard-coded KPI numbers at the bottom of the file must be updated manually when re-running after new data.
