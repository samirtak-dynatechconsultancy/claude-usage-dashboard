# AGENTS.md

Guidance for any coding agent (Codex, Claude Code, etc.) working on this repository.

> **Naming note.** This project *analyzes* Claude Code's local usage logs, so "Claude Code" below always refers to that product (the source of the JSONL data) — not to the agent reading this file. The agent working on the codebase is referred to as "the coding agent" or just "you".

## Project shape

This repo has **two flavours** of the same idea. They share concepts (JSONL parsing, the `turns`/`sessions` schema, pricing tiers) but ship and deploy independently — touch one, you usually don't need to touch the other.

### Local single-user tool (the original)

Three Python files at the repo root, stdlib only, no `pip install` step. Python 3.8+.

- [scanner.py](scanner.py) — parses Claude Code JSONL transcripts into a SQLite DB at `~/.claude/usage.db`.
- [cli.py](cli.py) — terminal commands (`scan` / `today` / `week` / `stats` / `dashboard`).
- [dashboard.py](dashboard.py) — single-file `http.server` serving an embedded HTML/JS SPA on `localhost:8080`.

Use `python` on Windows, `python3` on macOS/Linux. Both work the same.

### Hosted team dashboard (server)

[server/](server/) — Vercel project. Python serverless functions under `server/api/` + static HTML in `server/public/`. Uses Supabase (Postgres + Storage + Auth). Schema migrations in `server/supabase/migrations/`. See [SETUP.md](SETUP.md) for end-to-end deployment.

> **The end-user collector + Windows installer live in a separate repo:**
> [claude-usage-exe](https://github.com/samirtak-dynatechconsultancy/claude-usage-exe).
> That repo holds `collector.py` (stdlib-only agent that uploads to this server) and the Inno Setup wrapper that produces the distributable `.exe`. Pre-built installers are on its [Releases page](https://github.com/samirtak-dynatechconsultancy/claude-usage-exe/releases).

Pricing math, model-priority rule, and dedup-by-`message_id` invariant are duplicated between the local tool, the hosted server, and the collector in the exe repo intentionally — they're shipped independently. Keep all three in sync when you change any one.

## Common commands

```
python cli.py scan                  # incremental scan (fast on re-run)
python cli.py today                 # today's usage by model
python cli.py week                  # last 7 days, per-day + by-model
python cli.py stats                 # all-time stats
python cli.py dashboard             # scan + open http://localhost:8080
python cli.py scan --projects-dir PATH    # scan a custom transcripts dir
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

python -m unittest discover -s tests -v             # full test suite (CI runs this)
python -m unittest tests.test_scanner -v            # one file
python -m unittest tests.test_scanner.TestProjectNameFromCwd.test_windows_path  # one test
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the suite on Python 3.9 / 3.11 / 3.12 against `main` and PRs.

## Architecture

### Data flow

```
~/.claude/projects/**/*.jsonl   →   scanner.parse_jsonl_file()
~/Library/.../Xcode/...                  ↓
                              aggregate_sessions() → upsert_sessions() + insert_turns()
                                         ↓
                              ~/.claude/usage.db (SQLite)
                                         ↓
                  cli.py queries   ←──────────→   dashboard.py /api/data
```

By default the scanner walks both `~/.claude/projects/` and the Xcode coding-assistant directory; missing dirs are silently skipped. Override with `--projects-dir`.

### SQLite schema (created/migrated in [scanner.py](scanner.py) `init_db`)

- **`turns`** — one row per assistant API response. The source of truth for tokens and per-model attribution.
- **`sessions`** — aggregated per session (denormalized totals + chosen primary model).
- **`processed_files`** — incremental-scan tracking: `(path, mtime, lines)`. A file is skipped if its mtime matches; if it grew, only lines past the stored `lines` count are processed.

A conditional unique index on `turns.message_id` (where non-empty) lets `INSERT OR IGNORE` cheaply dedupe replays across rescans.

### Non-obvious invariants

These three things will bite you if you don't know them:

1. **Streaming dedupe by `message.id`.** Claude Code writes multiple JSONL records per API response — only the *last* one for a given `message.id` has the final usage tallies. `parse_jsonl_file` keeps the last record per `message_id` in a dict; earlier records are discarded. Don't sum across records of the same `message_id`.

2. **Session totals are recomputed from `turns` at the end of `scan()`.** During an incremental scan `upsert_sessions` adds tokens additively, but `insert_turns` uses `INSERT OR IGNORE` against the `message_id` unique index — so if a turn is a duplicate, session totals would drift. The final `UPDATE sessions ... (SELECT SUM ... FROM turns)` block reconciles this. Preserve it if you refactor scan logic.

3. **Session primary model priority is opus > sonnet > haiku** (`_model_priority` in [scanner.py](scanner.py)). This prevents a subagent's haiku turn from overwriting the session's opus model when an existing session is updated. Per-turn model is always honored in the `turns` table; only the session-level summary uses the priority.

### Cost calculation

Costs are computed **per turn** (each turn knows its own model), then summed. This is true in both the CLI ([cli.py](cli.py) `calc_cost`) and the dashboard JS ([dashboard.py](dashboard.py) `calcCost` inside the embedded HTML). Aggregating tokens first and applying a single price is wrong for sessions that span multiple models.

Pricing is duplicated in **three** places in this repo that must stay in sync (plus the collector in the exe repo, where it's currently unused — token data leaves the collector before cost calc):
- [cli.py](cli.py) `PRICING` dict (Python, local tool)
- [dashboard.py](dashboard.py) `PRICING` const inside `HTML_TEMPLATE` (JavaScript, local tool)
- [server/lib/pricing.py](server/lib/pricing.py) `PRICING` dict (Python, hosted server)
- [server/public/index.html](server/public/index.html) `PRICING` const (JavaScript, hosted dashboard)

`get_pricing` / `getPricing` resolve in three tiers: exact match → `startswith` (handles date-suffixed model IDs like `claude-opus-4-7-20260215`) → substring fallback on `opus` / `sonnet` / `haiku`. Models that don't match any tier return `None` and are billed at $0 (shown as `n/a`) — this is intentional so local/3rd-party models (gemma, glm, etc.) aren't charged at Sonnet rates.

### Dashboard server (local tool)

`http.server.BaseHTTPRequestHandler`-based, two endpoints:
- `GET /api/data` → JSON snapshot from `get_dashboard_data()`. Returns *all* history; client-side filters by date range and model.
- `POST /api/rescan` → deletes the DB and runs a full rescan. Passes `db_path` and `projects_dirs` explicitly so tests that monkey-patch the module globals work — scan's default arg values are frozen at def time, so don't switch to bare defaults.

The entire UI lives in `HTML_TEMPLATE` as a raw string. Chart.js is loaded from CDN.

### Hosted server architecture

[server/api/](server/api/) — one `BaseHTTPRequestHandler` subclass per `.py` file, picked up automatically by Vercel as `/api/<filename>`. Hyphens in filenames become hyphens in URLs (`upload-url.py` → `/api/upload-url`). Shared code lives in [server/lib/](server/lib/) and is imported via a `sys.path.insert(0, ..)` shim at the top of each handler (Vercel doesn't auto-add the project root to `sys.path`).

Endpoints:

| Route | Auth | Used by |
|---|---|---|
| `GET /api/config` | none | Browser bootstrap (returns Supabase URL + anon key) |
| `POST /api/upload-url` | `X-Ingest-Token` | Collector — gets signed Supabase Storage upload URL |
| `POST /api/ingest` | `X-Ingest-Token` | Collector — posts parsed metadata batch |
| `GET /api/auth-check` | Supabase JWT (Bearer) | Dashboard login flow |
| `GET /api/data` | Supabase JWT (Bearer) | Dashboard — main data fetch (supports `?user_id=`) |
| `GET /api/content` | Supabase JWT (Bearer) | Dashboard — drill-down, fetches raw JSONL from Storage |

Two-tier auth model: **collectors** use a shared `INGEST_TOKEN` env-var secret; **dashboard viewers** use Supabase Auth + an allowlist table (`dashboard_users`). Service-role Supabase client bypasses RLS and is only ever used server-side; the anon key is safe to expose to the browser.

The `recompute_session_totals(uuid)` Postgres function (migration [0002](server/supabase/migrations/0002_recompute_function.sql)) is called by `/api/ingest` after each batch. It mirrors the local scanner's "recompute from `turns`" reconciliation pass — needed because `ON CONFLICT DO NOTHING` on duplicate `message_id`s would otherwise let session totals drift on retried uploads.

### Collector → server contract

The collector (in the [claude-usage-exe](https://github.com/samirtak-dynatechconsultancy/claude-usage-exe) repo) uploads raw JSONL files **first** (direct PUT to Supabase Storage via signed URL from `/api/upload-url`), **then** posts parsed metadata to `/api/ingest`. The metadata payload references uploaded files by `content_path` so the dashboard can re-fetch raw content on demand without re-shipping it on every batch.

Batch size is capped at 100 turns/request in [`collector.py`](https://github.com/samirtak-dynatechconsultancy/claude-usage-exe/blob/main/collector/collector.py) `INGEST_BATCH_SIZE`. Vercel's body limit is 4.5 MB on Hobby/Pro — turns are small (~1 KB metadata each), but if you re-introduce inline content, drop this aggressively or you'll hit `HTTP 413`.

Per-machine state lives in `%LOCALAPPDATA%\ClaudeUsageCollector\state.json`. Deleting it forces a full re-upload; the server dedupes on `message_id` so it's safe but expensive in Storage egress.

## Testing notes

- `tests/test_scanner.py` and `tests/test_dashboard.py` use `tempfile.NamedTemporaryFile` for an isolated DB; never touch the user's real `~/.claude/usage.db`.
- The `/api/rescan` test patches `dashboard.DB_PATH` and `scanner.DEFAULT_PROJECTS_DIRS` — keep that contract intact (see commit 8ae2664).
- On Windows, `~/.claude/` may not exist on a fresh checkout. `get_db` creates the parent dir (`mkdir(parents=True, exist_ok=True)`) — don't remove that or `sqlite3.connect` will fail in CI / fresh installs (commit b5d1e15).

## Respecting contributors

When merging community PRs, **preserve the original author's commit so they get GitHub contributor credit**. In practice:

- `git fetch origin pull/<N>/head:pr-<N>` → `git merge --no-ff pr-<N>` keeps the author commit verbatim inside the merge bubble (don't squash, don't rebase-flatten).
- For a partial merge — when only one hunk of a PR is wanted — use `git cherry-pick <commit-sha>` against the specific upstream commit so authorship is preserved. If the diff isn't a clean single commit, fall back to applying the hunk manually + adding a `Co-Authored-By: Name <email>` trailer.
- Improvements that the bot/maintainer makes _on top_ of a contributor's work go in **separate follow-up commits**, not amendments to the contributor's commit.
- When closing duplicate PRs (multiple authors fixed the same bug independently), thank each one and explain that landing the earliest version isn't a quality judgment.

This applies to all agents working on this repo, not just Claude Code.
