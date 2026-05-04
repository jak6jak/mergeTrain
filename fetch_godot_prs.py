#!/usr/bin/env python3
"""
fetch_godot_prs.py — Collect Godot Engine PR data from GitHub into DuckDB.

Phases:
  1  Bulk list ALL PRs (open + closed) — base fields only
  2  PR detail (open PRs + any PR updated since last run)
  3  Reactions for open PRs (requires /issues/{n} endpoint)
  4a Reviews for open PRs needing refresh
  4b Issue comments for open PRs needing refresh
  4c Inline review comments for open PRs needing refresh
  4d Changed files for open PRs needing refresh

All phases are idempotent and resumable — safe to kill and re-run.
On subsequent runs, only PRs updated since the last fetch are re-enriched.

Usage:
  python fetch_godot_prs.py                  # run all phases (incremental update)
  python fetch_godot_prs.py --phases 1       # bulk list only
  python fetch_godot_prs.py --phases 1,2,3   # list + detail + reactions
  python fetch_godot_prs.py --fresh          # drop DB and start over
  python fetch_godot_prs.py --workers 12     # more concurrent workers
"""

import argparse
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import duckdb
from tqdm import tqdm

REPO = "godotengine/godot"
DB_PATH = "godot_prs.duckdb"
RATE_LIMIT_TARGET = 4500   # requests/hour (leaves 500 buffer from 5000 max)
DEFAULT_WORKERS = 8
BATCH_SIZE = 500


# ── Rate Limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Thread-safe token bucket. Refills at RATE_LIMIT_TARGET tokens/hour."""

    def __init__(self, calls_per_hour: int = RATE_LIMIT_TARGET):
        self._lock = threading.Lock()
        self._tokens = min(calls_per_hour, 200)
        self._max_tokens = calls_per_hour
        self._refill_rate = calls_per_hour / 3600.0
        self._last_refill = time.monotonic()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._max_tokens, self._tokens + elapsed * self._refill_rate)
                self._last_refill = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
            time.sleep(0.05)

    def back_off(self, seconds: float):
        print(f"\n  [rate-limit] Sleeping {seconds:.0f}s ...")
        time.sleep(seconds)


# ── Database Layer ────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pull_requests (
    number               INTEGER PRIMARY KEY,
    node_id              TEXT,
    html_url             TEXT,
    state                TEXT,
    draft                BOOLEAN,
    locked               BOOLEAN,
    title                TEXT,
    body                 TEXT,
    author_login         TEXT,
    author_id            INTEGER,
    author_association   TEXT,
    created_at           TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ,
    closed_at            TIMESTAMPTZ,
    merged_at            TIMESTAMPTZ,
    merge_commit_sha     TEXT,
    head_ref             TEXT,
    head_sha             TEXT,
    base_ref             TEXT,
    base_sha             TEXT,
    merged               BOOLEAN,
    merged_by_login      TEXT,
    commits              INTEGER,
    additions            INTEGER,
    deletions            INTEGER,
    changed_files        INTEGER,
    comments             INTEGER,
    review_comments      INTEGER,
    mergeable_state      TEXT,
    milestone_number     INTEGER,
    milestone_title      TEXT,
    reactions_total      INTEGER,
    reactions_plus1      INTEGER,
    reactions_minus1     INTEGER,
    reactions_laugh      INTEGER,
    reactions_hooray     INTEGER,
    reactions_confused   INTEGER,
    reactions_heart      INTEGER,
    reactions_rocket     INTEGER,
    reactions_eyes       INTEGER,
    detail_fetched_at    TIMESTAMPTZ,
    reactions_fetched_at TIMESTAMPTZ,
    social_fetched_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pr_labels (
    pr_number  INTEGER,
    label_name TEXT,
    label_color TEXT,
    PRIMARY KEY (pr_number, label_name)
);

CREATE TABLE IF NOT EXISTS pr_assignees (
    pr_number  INTEGER,
    user_login TEXT,
    PRIMARY KEY (pr_number, user_login)
);

CREATE TABLE IF NOT EXISTS pr_requested_reviewers (
    pr_number  INTEGER,
    user_login TEXT,
    PRIMARY KEY (pr_number, user_login)
);

CREATE TABLE IF NOT EXISTS pr_reviews (
    id                 BIGINT PRIMARY KEY,
    pr_number          INTEGER,
    reviewer_login     TEXT,
    reviewer_id        INTEGER,
    state              TEXT,
    submitted_at       TIMESTAMPTZ,
    commit_id          TEXT,
    body               TEXT,
    author_association TEXT,
    html_url           TEXT
);

CREATE TABLE IF NOT EXISTS pr_issue_comments (
    id                 BIGINT PRIMARY KEY,
    pr_number          INTEGER,
    author_login       TEXT,
    author_id          INTEGER,
    created_at         TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ,
    body               TEXT,
    author_association TEXT,
    reactions_total    INTEGER,
    reactions_plus1    INTEGER,
    reactions_minus1   INTEGER
);

CREATE TABLE IF NOT EXISTS pr_review_comments (
    id                     BIGINT PRIMARY KEY,
    pr_number              INTEGER,
    pull_request_review_id BIGINT,
    author_login           TEXT,
    path                   TEXT,
    diff_hunk              TEXT,
    line                   INTEGER,
    created_at             TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ,
    body                   TEXT,
    reactions_total        INTEGER,
    reactions_plus1        INTEGER
);

CREATE TABLE IF NOT EXISTS pr_files (
    pr_number  INTEGER,
    filename   TEXT,
    status     TEXT,
    additions  INTEGER,
    deletions  INTEGER,
    changes    INTEGER,
    PRIMARY KEY (pr_number, filename)
);

CREATE TABLE IF NOT EXISTS fetch_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db(path: str, fresh: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path)
    if fresh:
        for tbl in [
            "pr_files", "pr_review_comments", "pr_issue_comments",
            "pr_reviews", "pr_requested_reviewers", "pr_assignees",
            "pr_labels", "pull_requests", "fetch_state",
        ]:
            con.execute(f"DROP TABLE IF EXISTS {tbl}")
        print("Dropped all tables.")
    con.execute(SCHEMA_SQL)

    # Migrations for existing databases
    try:
        con.execute("ALTER TABLE pull_requests ADD COLUMN social_fetched_at TIMESTAMPTZ")
        # Treat PRs that already have file data as fully enriched so they
        # aren't re-fetched on the first update run.
        con.execute("""
            UPDATE pull_requests
            SET social_fetched_at = CURRENT_TIMESTAMP
            WHERE social_fetched_at IS NULL
              AND number IN (SELECT DISTINCT pr_number FROM pr_files)
        """)
        con.commit()
        print("  Migrated: added social_fetched_at column.")
    except Exception:
        pass  # column already exists

    return con


def get_state(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM fetch_state WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def set_state(con, key: str, value: str):
    con.execute(
        "INSERT OR REPLACE INTO fetch_state (key, value) VALUES (?, ?)",
        [key, value]
    )


# ── GitHub API ────────────────────────────────────────────────────────────────

def gh_api(endpoint: str, paginate: bool = False) -> list | dict:
    cmd = ["gh", "api", endpoint, "--header", "Accept: application/vnd.github+json"]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh api timed out: {endpoint}")
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "rate limit" in stderr.lower() or "API rate limit" in stderr:
            raise RateLimitError(stderr)
        raise RuntimeError(f"gh api error [{endpoint}]: {stderr}")
    return json.loads(result.stdout)


class RateLimitError(Exception):
    pass


# ── Upsert Helpers ────────────────────────────────────────────────────────────

def _ts(val):
    return val  # DuckDB parses ISO timestamps directly


def _upsert_pr_list_rows(con, prs: list[dict]):
    """Upsert list-level PR fields. Replaces labels/assignees/reviewers so
    removals (e.g. a label stripped from a PR) are reflected correctly."""
    pr_rows, label_rows, assignee_rows, reviewer_rows = [], [], [], []
    for pr in prs:
        user = pr.get("user") or {}
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        pr_rows.append((
            pr["number"],
            pr.get("node_id"),
            pr.get("html_url"),
            pr.get("state"),
            pr.get("draft", False),
            pr.get("locked", False),
            pr.get("title"),
            pr.get("body"),
            user.get("login"),
            user.get("id"),
            pr.get("author_association"),
            _ts(pr.get("created_at")),
            _ts(pr.get("updated_at")),
            _ts(pr.get("closed_at")),
            _ts(pr.get("merged_at")),
            pr.get("merge_commit_sha"),
            head.get("ref"),
            (head.get("sha") or "")[:40] or None,
            base.get("ref"),
            (base.get("sha") or "")[:40] or None,
        ))
        n = pr["number"]
        for lbl in pr.get("labels") or []:
            label_rows.append((n, lbl.get("name"), lbl.get("color")))
        for a in pr.get("assignees") or []:
            if a.get("login"):
                assignee_rows.append((n, a["login"]))
        for r in pr.get("requested_reviewers") or []:
            if r.get("login"):
                reviewer_rows.append((n, r["login"]))

    con.executemany("""
        INSERT INTO pull_requests
            (number, node_id, html_url, state, draft, locked, title, body,
             author_login, author_id, author_association,
             created_at, updated_at, closed_at, merged_at, merge_commit_sha,
             head_ref, head_sha, base_ref, base_sha)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (number) DO UPDATE SET
            state = excluded.state,
            title = excluded.title,
            updated_at = excluded.updated_at,
            closed_at = excluded.closed_at,
            merged_at = excluded.merged_at,
            locked = excluded.locked,
            draft = excluded.draft
    """, pr_rows)

    # Delete then re-insert so removed labels/assignees/reviewers are purged.
    pr_numbers = [pr["number"] for pr in prs]
    placeholders = ",".join(["?"] * len(pr_numbers))
    con.execute(f"DELETE FROM pr_labels WHERE pr_number IN ({placeholders})", pr_numbers)
    con.execute(f"DELETE FROM pr_assignees WHERE pr_number IN ({placeholders})", pr_numbers)
    con.execute(f"DELETE FROM pr_requested_reviewers WHERE pr_number IN ({placeholders})", pr_numbers)

    if label_rows:
        con.executemany("INSERT OR REPLACE INTO pr_labels VALUES (?,?,?)", label_rows)
    if assignee_rows:
        con.executemany("INSERT OR REPLACE INTO pr_assignees VALUES (?,?)", assignee_rows)
    if reviewer_rows:
        con.executemany("INSERT OR REPLACE INTO pr_requested_reviewers VALUES (?,?)", reviewer_rows)


def _update_pr_detail(con, rows: list[tuple]):
    con.executemany("""
        UPDATE pull_requests SET
            merged = ?, merged_by_login = ?,
            commits = ?, additions = ?, deletions = ?,
            changed_files = ?, comments = ?, review_comments = ?,
            mergeable_state = ?,
            milestone_number = ?, milestone_title = ?,
            detail_fetched_at = now()
        WHERE number = ?
    """, rows)


def _update_pr_reactions(con, rows: list[tuple]):
    con.executemany("""
        UPDATE pull_requests SET
            reactions_total = ?, reactions_plus1 = ?, reactions_minus1 = ?,
            reactions_laugh = ?, reactions_hooray = ?, reactions_confused = ?,
            reactions_heart = ?, reactions_rocket = ?, reactions_eyes = ?,
            reactions_fetched_at = now()
        WHERE number = ?
    """, rows)


def _insert_reviews(con, rows: list[tuple]):
    # INSERT OR REPLACE to pick up updated review bodies/states.
    con.executemany(
        "INSERT OR REPLACE INTO pr_reviews VALUES (?,?,?,?,?,?,?,?,?,?)", rows
    )


def _insert_issue_comments(con, rows: list[tuple]):
    # INSERT OR REPLACE to pick up edited comment bodies.
    con.executemany(
        "INSERT OR REPLACE INTO pr_issue_comments VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )


def _insert_review_comments(con, rows: list[tuple]):
    # INSERT OR REPLACE to pick up edited inline comment bodies.
    con.executemany(
        "INSERT OR REPLACE INTO pr_review_comments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )


# ── Phase 1: Bulk List ────────────────────────────────────────────────────────

def run_phase1_bulk_list(con, incremental: bool = True):
    print("\n=== Phase 1: Bulk PR List ===")
    last_fetch = get_state(con, "last_list_fetch")

    # Preserve the previous fetch timestamp so later phases can identify
    # which PRs were recently updated and need re-enrichment.
    if last_fetch:
        set_state(con, "prev_last_list_fetch", last_fetch)

    if incremental and last_fetch:
        print(f"  Incremental mode — stopping when updated_at <= {last_fetch}")

    page = 1
    total_inserted = 0
    done = False

    with tqdm(desc="Pages fetched", unit="page") as pbar:
        while not done:
            endpoint = (
                f"/repos/{REPO}/pulls"
                f"?state=all&per_page=100&sort=updated&direction=desc&page={page}"
            )
            try:
                prs = gh_api(endpoint)
            except RateLimitError:
                print("\nRate limit hit — sleeping 65s")
                time.sleep(65)
                continue

            if not prs:
                break

            if incremental and last_fetch:
                cutoff = prs[-1].get("updated_at", "")
                if cutoff and cutoff <= last_fetch:
                    prs = [p for p in prs if p.get("updated_at", "") > last_fetch]
                    done = True

            if prs:
                _upsert_pr_list_rows(con, prs)
                total_inserted += len(prs)

            page += 1
            pbar.update(1)
            pbar.set_postfix(total=total_inserted)

    set_state(con, "last_list_fetch", datetime.now(timezone.utc).isoformat())
    con.commit()
    print(f"  Done. Upserted {total_inserted} PRs total.")
    _print_stats(con)


def _print_stats(con):
    rows = con.execute("SELECT state, COUNT(*) FROM pull_requests GROUP BY state").fetchall()
    for state, cnt in rows:
        print(f"    {state}: {cnt}")


# ── Phase Worker Factory ──────────────────────────────────────────────────────

def _run_per_pr_phase(
    con,
    phase_name: str,
    queue_sql: str,
    fetch_fn,
    process_fn,
    workers: int,
    limiter: RateLimiter,
):
    """Generic per-PR enrichment phase with ThreadPoolExecutor + batch commits."""
    pending = [r[0] for r in con.execute(queue_sql).fetchall()]
    if not pending:
        print(f"  {phase_name}: nothing to do.")
        return

    print(f"\n=== {phase_name} ({len(pending)} PRs) ===")
    batch = []
    errors = 0

    def _worker(pr_number):
        limiter.acquire()
        return pr_number, fetch_fn(pr_number)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_worker, n): n for n in pending}
        with tqdm(total=len(pending), desc=phase_name) as pbar:
            for future in as_completed(futures):
                pr_num = futures[future]
                try:
                    _, data = future.result()
                    rows = process_fn(pr_num, data)
                    if rows:
                        batch.extend(rows)
                    if len(batch) >= BATCH_SIZE:
                        _flush(con, phase_name, batch)
                        batch.clear()
                except RateLimitError:
                    limiter.back_off(65)
                except Exception as e:
                    errors += 1
                    if errors <= 10:
                        tqdm.write(f"  Error PR#{pr_num}: {e}")
                pbar.update(1)

    if batch:
        _flush(con, phase_name, batch)
    con.commit()
    print(f"  {phase_name} complete. Errors: {errors}")


def _flush(con, phase_name: str, batch: list):
    if "detail" in phase_name.lower():
        _update_pr_detail(con, batch)
    elif "reaction" in phase_name.lower():
        _update_pr_reactions(con, batch)
    elif "review comment" in phase_name.lower():
        _insert_review_comments(con, batch)
    elif "review" in phase_name.lower():
        _insert_reviews(con, batch)
    elif "issue comment" in phase_name.lower():
        _insert_issue_comments(con, batch)
    con.commit()


# ── Phase 2: Detail ───────────────────────────────────────────────────────────

def _fetch_detail(pr_number: int) -> dict:
    return gh_api(f"/repos/{REPO}/pulls/{pr_number}")


def _process_detail(pr_number: int, data: dict) -> list[tuple]:
    mb = data.get("merged_by") or {}
    ms = data.get("milestone") or {}
    return [(
        data.get("merged", False),
        mb.get("login"),
        data.get("commits"),
        data.get("additions"),
        data.get("deletions"),
        data.get("changed_files"),
        data.get("comments"),
        data.get("review_comments"),
        data.get("mergeable_state"),
        ms.get("number"),
        ms.get("title"),
        pr_number,
    )]


def run_phase2_detail(con, limiter: RateLimiter, workers: int):
    prev = get_state(con, "prev_last_list_fetch")
    if prev:
        # Re-fetch detail for:
        #   - open PRs never fetched (new PRs)
        #   - any PR updated since last run (catches newly-closed PRs getting
        #     their final merged_by / commit count / etc.)
        queue_sql = f"""
            SELECT number FROM pull_requests
            WHERE (state = 'open' AND detail_fetched_at IS NULL)
               OR updated_at > '{prev}'
            ORDER BY number
        """
    else:
        queue_sql = """
            SELECT number FROM pull_requests
            WHERE state = 'open' AND detail_fetched_at IS NULL
            ORDER BY number
        """
    _run_per_pr_phase(
        con,
        phase_name="Phase 2: Detail",
        queue_sql=queue_sql,
        fetch_fn=_fetch_detail,
        process_fn=_process_detail,
        workers=workers,
        limiter=limiter,
    )


# ── Phase 3: Reactions ────────────────────────────────────────────────────────

def _fetch_reactions(pr_number: int) -> dict:
    return gh_api(f"/repos/{REPO}/issues/{pr_number}")


def _process_reactions(pr_number: int, data: dict) -> list[tuple]:
    r = data.get("reactions") or {}
    return [(
        r.get("total_count"),
        r.get("+1"),
        r.get("-1"),
        r.get("laugh"),
        r.get("hooray"),
        r.get("confused"),
        r.get("heart"),
        r.get("rocket"),
        r.get("eyes"),
        pr_number,
    )]


def run_phase3_reactions(con, limiter: RateLimiter, workers: int):
    prev = get_state(con, "prev_last_list_fetch")
    if prev:
        queue_sql = f"""
            SELECT number FROM pull_requests
            WHERE state = 'open'
              AND (reactions_fetched_at IS NULL OR updated_at > '{prev}')
            ORDER BY number
        """
    else:
        queue_sql = """
            SELECT number FROM pull_requests
            WHERE state = 'open' AND reactions_fetched_at IS NULL
            ORDER BY number
        """
    _run_per_pr_phase(
        con,
        phase_name="Phase 3: Reactions",
        queue_sql=queue_sql,
        fetch_fn=_fetch_reactions,
        process_fn=_process_reactions,
        workers=workers,
        limiter=limiter,
    )


# ── Phase 4a: Reviews ─────────────────────────────────────────────────────────

def _fetch_reviews(pr_number: int) -> list:
    return gh_api(f"/repos/{REPO}/pulls/{pr_number}/reviews")


def _process_reviews(pr_number: int, data: list) -> list[tuple]:
    rows = []
    for r in data or []:
        user = r.get("user") or {}
        rows.append((
            r["id"],
            pr_number,
            user.get("login"),
            user.get("id"),
            r.get("state"),
            _ts(r.get("submitted_at")),
            r.get("commit_id"),
            r.get("body"),
            r.get("author_association"),
            r.get("html_url"),
        ))
    return rows


def run_phase4a_reviews(con, limiter: RateLimiter, workers: int):
    # Queue: open PRs that have never had social data fetched, or were updated
    # since social data was last collected.
    _run_per_pr_phase(
        con,
        phase_name="Phase 4a: Reviews",
        queue_sql="""
            SELECT number FROM pull_requests
            WHERE state = 'open'
              AND (social_fetched_at IS NULL OR updated_at > social_fetched_at)
            ORDER BY number
        """,
        fetch_fn=_fetch_reviews,
        process_fn=_process_reviews,
        workers=workers,
        limiter=limiter,
    )


# ── Phase 4b: Issue Comments ──────────────────────────────────────────────────

def _fetch_issue_comments(pr_number: int) -> list:
    return gh_api(f"/repos/{REPO}/issues/{pr_number}/comments")


def _process_issue_comments(pr_number: int, data: list) -> list[tuple]:
    rows = []
    for c in data or []:
        user = c.get("user") or {}
        r = c.get("reactions") or {}
        rows.append((
            c["id"],
            pr_number,
            user.get("login"),
            user.get("id"),
            _ts(c.get("created_at")),
            _ts(c.get("updated_at")),
            c.get("body"),
            c.get("author_association"),
            r.get("total_count"),
            r.get("+1"),
            r.get("-1"),
        ))
    return rows


def run_phase4b_issue_comments(con, limiter: RateLimiter, workers: int):
    _run_per_pr_phase(
        con,
        phase_name="Phase 4b: Issue Comments",
        queue_sql="""
            SELECT number FROM pull_requests
            WHERE state = 'open'
              AND (social_fetched_at IS NULL OR updated_at > social_fetched_at)
            ORDER BY number
        """,
        fetch_fn=_fetch_issue_comments,
        process_fn=_process_issue_comments,
        workers=workers,
        limiter=limiter,
    )


# ── Phase 4c: Inline Review Comments ─────────────────────────────────────────

def _fetch_review_comments(pr_number: int) -> list:
    return gh_api(f"/repos/{REPO}/pulls/{pr_number}/comments")


def _process_review_comments(pr_number: int, data: list) -> list[tuple]:
    rows = []
    for c in data or []:
        user = c.get("user") or {}
        r = c.get("reactions") or {}
        rows.append((
            c["id"],
            pr_number,
            c.get("pull_request_review_id"),
            user.get("login"),
            c.get("path"),
            c.get("diff_hunk"),
            c.get("line"),
            _ts(c.get("created_at")),
            _ts(c.get("updated_at")),
            c.get("body"),
            r.get("total_count"),
            r.get("+1"),
        ))
    return rows


def run_phase4c_review_comments(con, limiter: RateLimiter, workers: int):
    _run_per_pr_phase(
        con,
        phase_name="Phase 4c: Review Comments",
        queue_sql="""
            SELECT number FROM pull_requests
            WHERE state = 'open'
              AND (social_fetched_at IS NULL OR updated_at > social_fetched_at)
            ORDER BY number
        """,
        fetch_fn=_fetch_review_comments,
        process_fn=_process_review_comments,
        workers=workers,
        limiter=limiter,
    )


# ── Phase 4d: Changed Files ───────────────────────────────────────────────────

def _fetch_files(pr_number: int) -> list:
    return gh_api(f"/repos/{REPO}/pulls/{pr_number}/files")


def _process_files(pr_number: int, data: list) -> list[tuple]:
    rows = []
    for f in data or []:
        rows.append((
            pr_number,
            f.get("filename"),
            f.get("status"),
            f.get("additions"),
            f.get("deletions"),
            f.get("changes"),
        ))
    return rows


def run_phase4d_files(con, limiter: RateLimiter, workers: int):
    open_prs = [
        r[0] for r in con.execute("""
            SELECT number FROM pull_requests
            WHERE state = 'open'
              AND (social_fetched_at IS NULL OR updated_at > social_fetched_at)
            ORDER BY number
        """).fetchall()
    ]
    if not open_prs:
        print("  Phase 4d: Files: nothing to do.")
        return

    print(f"\n=== Phase 4d: Files ({len(open_prs)} PRs) ===")
    processed = []
    errors = 0

    def _worker(n):
        limiter.acquire()
        return n, _fetch_files(n)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_worker, n): n for n in open_prs}
        with tqdm(total=len(open_prs), desc="Phase 4d: Files") as pbar:
            for future in as_completed(futures):
                pr_num = futures[future]
                try:
                    _, data = future.result()
                    rows = _process_files(pr_num, data)
                    # Delete then reinsert so renamed/removed files don't linger.
                    con.execute("DELETE FROM pr_files WHERE pr_number = ?", [pr_num])
                    if rows:
                        con.executemany("INSERT INTO pr_files VALUES (?,?,?,?,?,?)", rows)
                    processed.append(pr_num)

                    if len(processed) >= BATCH_SIZE:
                        _mark_social_fetched(con, processed)
                        processed.clear()
                except RateLimitError:
                    limiter.back_off(65)
                except Exception as e:
                    errors += 1
                    if errors <= 10:
                        tqdm.write(f"  Error PR#{pr_num}: {e}")
                pbar.update(1)

    if processed:
        _mark_social_fetched(con, processed)
    con.commit()
    print(f"  Phase 4d: Files complete. Errors: {errors}")


def _mark_social_fetched(con, pr_numbers: list[int]):
    """Stamp social_fetched_at so these PRs aren't re-queued on next update."""
    phs = ",".join(["?"] * len(pr_numbers))
    con.execute(
        f"UPDATE pull_requests SET social_fetched_at = now() WHERE number IN ({phs})",
        pr_numbers,
    )
    con.commit()


# ── Backfill: merged PR detail + files ───────────────────────────────────────

def _backfill_queue(con, years: int, need_detail: bool, need_files: bool) -> list[int]:
    """Return merged PR numbers from the last `years` years that are missing data."""
    if need_detail:
        col = "detail_fetched_at IS NULL"
    else:
        col = "NOT EXISTS (SELECT 1 FROM pr_files f WHERE f.pr_number = pull_requests.number)"
    return [
        r[0] for r in con.execute(f"""
            SELECT number FROM pull_requests
            WHERE merged_at IS NOT NULL
              AND merged_at >= NOW() - INTERVAL {years} YEAR
              AND {col}
            ORDER BY merged_at DESC
        """).fetchall()
    ]


def run_backfill_merged(con, limiter: RateLimiter, workers: int, years: int,
                        skip_files: bool = False):
    detail_q = _backfill_queue(con, years, need_detail=True,  need_files=False)
    files_q  = _backfill_queue(con, years, need_detail=False, need_files=True)

    detail_req = len(detail_q)
    files_req  = len(files_q) if not skip_files else 0
    total_req  = detail_req + files_req

    print(f"\n=== Merged-PR Backfill  (last {years} years) ===")
    print(f"  PRs needing detail (additions/deletions): {detail_req:,}")
    if not skip_files:
        print(f"  PRs needing file breakdown:               {len(files_q):,}")
    print(f"  Total API requests:                       {total_req:,}")
    est_min = total_req / RATE_LIMIT_TARGET * 60
    print(f"  Estimated time at {RATE_LIMIT_TARGET:,} req/hr:           "
          f"{est_min:.0f} min  (~{est_min/60:.1f}h)")
    print()

    # ── Phase B1: detail (additions / deletions / changed_files) ─────────────
    if detail_q:
        print(f"=== Backfill B1: Detail ({detail_req:,} PRs) ===")
        batch, errors = [], 0

        def _worker_detail(n):
            limiter.acquire()
            return n, _fetch_detail(n)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker_detail, n): n for n in detail_q}
            with tqdm(total=detail_req, desc="Backfill B1: Detail") as pbar:
                for future in as_completed(futures):
                    pr_num = futures[future]
                    try:
                        _, data = future.result()
                        rows = _process_detail(pr_num, data)
                        batch.extend(rows)
                        if len(batch) >= BATCH_SIZE:
                            _update_pr_detail(con, batch)
                            con.commit()
                            batch.clear()
                    except RateLimitError:
                        limiter.back_off(65)
                    except Exception as e:
                        errors += 1
                        if errors <= 10:
                            tqdm.write(f"  Error PR#{pr_num}: {e}")
                    pbar.update(1)
        if batch:
            _update_pr_detail(con, batch)
        con.commit()
        print(f"  Backfill B1 complete. Errors: {errors}")

    # ── Phase B2: per-file breakdown ──────────────────────────────────────────
    if skip_files:
        return

    if files_q:
        print(f"\n=== Backfill B2: Files ({len(files_q):,} PRs) ===")
        processed, errors = [], 0

        def _worker_files(n):
            limiter.acquire()
            return n, _fetch_files(n)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_worker_files, n): n for n in files_q}
            with tqdm(total=len(files_q), desc="Backfill B2: Files") as pbar:
                for future in as_completed(futures):
                    pr_num = futures[future]
                    try:
                        _, data = future.result()
                        rows = _process_files(pr_num, data)
                        con.execute("DELETE FROM pr_files WHERE pr_number = ?", [pr_num])
                        if rows:
                            con.executemany("INSERT INTO pr_files VALUES (?,?,?,?,?,?)", rows)
                        processed.append(pr_num)
                        if len(processed) >= BATCH_SIZE:
                            con.commit()
                            processed.clear()
                    except RateLimitError:
                        limiter.back_off(65)
                    except Exception as e:
                        errors += 1
                        if errors <= 10:
                            tqdm.write(f"  Error PR#{pr_num}: {e}")
                    pbar.update(1)
        con.commit()
        print(f"  Backfill B2 complete. Errors: {errors}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Godot PR data into DuckDB")
    parser.add_argument(
        "--phases",
        default="1,2,3,4a,4b,4c,4d",
        help="Comma-separated phases to run (e.g. 1,2,3 or 1,4a,4b). Default: all",
    )
    parser.add_argument("--fresh",          action="store_true", help="Drop all tables and start over")
    parser.add_argument("--workers",        type=int, default=DEFAULT_WORKERS, help="Concurrent workers for per-PR phases")
    parser.add_argument("--full-list",      action="store_true", help="Force full list re-fetch (non-incremental)")
    parser.add_argument("--db",             default=DB_PATH, help=f"DuckDB file path (default: {DB_PATH})")
    parser.add_argument("--backfill-merged",action="store_true",
                        help="Backfill additions/deletions + file breakdown for merged PRs")
    parser.add_argument("--backfill-years", type=int, default=2,
                        help="How many years back to backfill (default: 2, used with --backfill-merged)")
    parser.add_argument("--backfill-detail-only", action="store_true",
                        help="With --backfill-merged: skip per-file breakdown (faster, ~2.2h vs ~4.4h)")
    parser.add_argument("--estimate",       action="store_true",
                        help="Print backfill time estimate and exit without fetching")
    args = parser.parse_args()

    con = init_db(args.db, fresh=args.fresh)
    limiter = RateLimiter(RATE_LIMIT_TARGET)

    if args.estimate or args.backfill_merged:
        detail_n = len(_backfill_queue(con, args.backfill_years, need_detail=True,  need_files=False))
        files_n  = len(_backfill_queue(con, args.backfill_years, need_detail=False, need_files=True))
        skip_files = args.backfill_detail_only
        total_n  = detail_n + (files_n if not skip_files else 0)
        est_min  = total_n / RATE_LIMIT_TARGET * 60
        print(f"\nBackfill estimate  (last {args.backfill_years} years)")
        print(f"  PRs needing detail:       {detail_n:,}  (~{detail_n/RATE_LIMIT_TARGET*60:.0f} min)")
        if not skip_files:
            print(f"  PRs needing file data:    {files_n:,}  (~{files_n/RATE_LIMIT_TARGET*60:.0f} min)")
        print(f"  Total:                    {total_n:,}  (~{est_min:.0f} min / {est_min/60:.1f}h)")
        if args.estimate:
            con.close()
            return

    if args.backfill_merged:
        run_backfill_merged(con, limiter, args.workers,
                            years=args.backfill_years,
                            skip_files=args.backfill_detail_only)
        con.close()
        print("\nAll done.")
        return

    phases = {p.strip().lower() for p in args.phases.split(",")}
    print(f"DB: {args.db}  |  Workers: {args.workers}  |  Phases: {sorted(phases)}")

    if "1" in phases:
        run_phase1_bulk_list(con, incremental=not args.full_list)

    if "2" in phases:
        run_phase2_detail(con, limiter, args.workers)

    if "3" in phases:
        run_phase3_reactions(con, limiter, args.workers)

    if "4a" in phases:
        run_phase4a_reviews(con, limiter, args.workers)

    if "4b" in phases:
        run_phase4b_issue_comments(con, limiter, args.workers)

    if "4c" in phases:
        run_phase4c_review_comments(con, limiter, args.workers)

    if "4d" in phases:
        run_phase4d_files(con, limiter, args.workers)

    con.close()
    print("\nAll done.")


if __name__ == "__main__":
    main()
