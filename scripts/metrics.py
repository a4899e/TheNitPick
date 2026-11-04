#!/usr/bin/env python3
"""Metrics and findings ledger for the pre-PR review loop.

One SQLite database in the user's home directory accumulates every review run,
so the loop's cost and yield can be studied later. The same ``findings`` rows
are the run's ledger: Claude reads them to spot repeats and contradictions and
to cite earlier decisions. Codex never sees them.

Database: ``$THENITPICK_DB`` or ``~/.thenitpick/metrics.db``.

Finding text comes from a reviewer reading repository content, so it is
untrusted: it is only ever passed in through a JSON file (``--file``), never on
the command line, and it is stored and printed as data.

Usage (each command prints JSON):
  metrics.py run-start --repo R --branch B [--base origin/main]
  metrics.py cycle-start --run ID --reviewer codex|code-review --model M --effort E --sha SHA
  metrics.py cycle-end --cycle ID [--tokens N]
  metrics.py findings --cycle ID --file findings.json
  metrics.py finding-update --id N [--disposition D] [--verified 0|1] [--severity S]
                            [--fix-commit SHA] [--repeat-of N] [--settled-by H]
  metrics.py ledger --run ID
  metrics.py run-end --run ID --outcome clean|escalated|cap|abandoned [--pr-url URL]
  metrics.py escape --run ID --found-by ci|human|production --severity S --file note.json
  metrics.py report [--repo R] [--since YYYY-MM-DD]
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SEVERITIES = ("P0", "P1", "P2", "P3")
DISPOSITIONS = ("open", "fixed", "declined", "disputed", "deferred", "duplicate")
SETTLED_BY = ("reproduction", "codex-recheck", "subagent", "user")
REVIEWERS = ("codex", "code-review")
OUTCOMES = ("clean", "escalated", "cap", "abandoned")
FOUND_BY = ("ci", "human", "production")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    base TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    pr_url TEXT
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    n INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    model TEXT,
    effort TEXT,
    head_sha TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    tokens INTEGER
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    reviewer_severity TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    disposition TEXT NOT NULL DEFAULT 'open',
    settled_by TEXT,
    fix_commit TEXT,
    repeat_of INTEGER REFERENCES findings(id),
    reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escapes (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    found_by TEXT NOT NULL,
    severity TEXT NOT NULL,
    note TEXT,
    recorded_at TEXT NOT NULL
);
"""


def _db_path() -> Path:
    override = os.environ.get("THENITPICK_DB")
    return Path(override) if override else Path.home() / ".thenitpick" / "metrics.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Migrations for databases created before a column existed.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(cycles)")}
    if "tokens" not in columns:
        conn.execute("ALTER TABLE cycles ADD COLUMN tokens INTEGER")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def _seconds(start: "str | None", end: "str | None") -> "float | None":
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _require(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    """The row with ``row_id``, or exit with a clear error (never a traceback)."""
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        sys.exit(f"no {table[:-1]} with id {row_id}")
    return row


def _load_json(path: str) -> object:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run_start(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    cur = conn.execute(
        "INSERT INTO runs (repo, branch, base, started_at) VALUES (?, ?, ?, ?)",
        (args.repo, args.branch, args.base, _now()),
    )
    _emit({"run": cur.lastrowid, "db": str(_db_path())})


def cycle_start(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _require(conn, "runs", args.run)
    n = conn.execute(
        "SELECT COUNT(*) FROM cycles WHERE run_id = ? AND reviewer = ?", (args.run, args.reviewer)
    ).fetchone()[0] + 1
    cur = conn.execute(
        "INSERT INTO cycles (run_id, n, reviewer, model, effort, head_sha, started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (args.run, n, args.reviewer, args.model, args.effort, args.sha, _now()),
    )
    _emit({"cycle": cur.lastrowid, "n": n})


def cycle_end(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _require(conn, "cycles", args.cycle)
    conn.execute("UPDATE cycles SET ended_at = COALESCE(ended_at, ?), tokens = COALESCE(?, tokens)"
                 " WHERE id = ?",
                 (_now(), args.tokens, args.cycle))
    row = conn.execute("SELECT * FROM cycles WHERE id = ?", (args.cycle,)).fetchone()
    _emit({"cycle": args.cycle, "seconds": _seconds(row["started_at"], row["ended_at"]),
           "tokens": row["tokens"]})


def findings(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Record a cycle's findings from a JSON list of objects.

    Each object: ``severity`` (reviewer's P0-P3, required), ``title`` (required),
    optional ``location``. Claude's own assessment starts equal to the
    reviewer's and is revised with ``finding-update --severity`` after
    reproduction.
    """
    cycle = conn.execute("SELECT run_id FROM cycles WHERE id = ?", (args.cycle,)).fetchone()
    if cycle is None:
        sys.exit(f"no cycle {args.cycle}")
    items = _load_json(args.file)
    if not isinstance(items, list):
        sys.exit("findings file must hold a JSON list")
    ids = []
    for item in items:
        severity = str(item.get("severity", "")).upper()
        if severity not in SEVERITIES:
            sys.exit(f"bad severity {severity!r}")
        cur = conn.execute(
            "INSERT INTO findings (run_id, cycle_id, reviewer_severity, severity, title,"
            " location, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cycle["run_id"],
                args.cycle,
                severity,
                severity,
                str(item["title"]),
                item.get("location"),
                _now(),
            ),
        )
        ids.append(cur.lastrowid)
    _emit({"findings": ids})


def finding_update(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _require(conn, "findings", args.id)
    if args.repeat_of is not None:
        _require(conn, "findings", args.repeat_of)
    sets, values = [], []
    for column, value in (
        ("disposition", args.disposition),
        ("severity", args.severity),
        ("fix_commit", args.fix_commit),
        ("repeat_of", args.repeat_of),
        ("settled_by", args.settled_by),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            values.append(value)
    if args.verified is not None:
        sets.append("verified = ?")
        values.append(int(args.verified))
    if args.reason_file:
        sets.append("reason = ?")
        values.append(str(_load_json(args.reason_file)))
    if not sets:
        sys.exit("nothing to update")
    sets.append("updated_at = ?")
    values.extend([_now(), args.id])
    conn.execute(f"UPDATE findings SET {', '.join(sets)} WHERE id = ?", values)
    _emit(dict(conn.execute("SELECT * FROM findings WHERE id = ?", (args.id,)).fetchone()))


def ledger(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _require(conn, "runs", args.run)
    rows = conn.execute(
        "SELECT f.id, c.reviewer, c.n AS cycle, f.reviewer_severity, f.severity, f.verified,"
        " f.disposition, f.settled_by, f.fix_commit, f.repeat_of, f.location, f.title, f.reason"
        " FROM findings f JOIN cycles c ON c.id = f.cycle_id WHERE f.run_id = ?"
        " ORDER BY f.id",
        (args.run,),
    ).fetchall()
    _emit([dict(row) for row in rows])


def run_end(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _require(conn, "runs", args.run)
    conn.execute(
        "UPDATE runs SET ended_at = ?, outcome = ?, pr_url = COALESCE(?, pr_url) WHERE id = ?",
        (_now(), args.outcome, args.pr_url, args.run),
    )
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (args.run,)).fetchone()
    _emit({"run": args.run, "outcome": args.outcome,
           "seconds": _seconds(run["started_at"], run["ended_at"])})


def escape(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    _require(conn, "runs", args.run)
    note = _load_json(args.file) if args.file else None
    cur = conn.execute(
        "INSERT INTO escapes (run_id, found_by, severity, note, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (args.run, args.found_by, args.severity, None if note is None else str(note), _now()),
    )
    _emit({"escape": cur.lastrowid})


def report(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    where, params = ["1 = 1"], []
    if args.repo:
        where.append("r.repo = ?")
        params.append(args.repo)
    if args.since:
        where.append("r.started_at >= ?")
        params.append(args.since)
    clause = " AND ".join(where)
    runs = conn.execute(f"SELECT * FROM runs r WHERE {clause}", params).fetchall()
    run_ids = [row["id"] for row in runs]
    if not run_ids:
        _emit({"runs": 0})
        return
    marks = ",".join("?" * len(run_ids))
    cycles = conn.execute(f"SELECT * FROM cycles WHERE run_id IN ({marks})", run_ids).fetchall()
    found = conn.execute(f"SELECT * FROM findings WHERE run_id IN ({marks})", run_ids).fetchall()
    escaped = conn.execute(f"SELECT * FROM escapes WHERE run_id IN ({marks})", run_ids).fetchall()

    def total(rows, start, end):
        return round(sum(_seconds(r[start], r[end]) or 0 for r in rows))

    # Time is reported over ended runs only. An open or unclosed run has review
    # time but no run duration, which would push fixing_and_other negative.
    ended = {r["id"] for r in runs if r["ended_at"]}
    ended_cycles = [c for c in cycles if c["run_id"] in ended]
    review_seconds = {
        reviewer: total([c for c in ended_cycles if c["reviewer"] == reviewer],
                        "started_at", "ended_at")
        for reviewer in REVIEWERS
    }
    run_seconds = total([r for r in runs if r["id"] in ended], "started_at", "ended_at")
    by = {}
    for f in found:
        key = (f["reviewer_severity"], f["disposition"], bool(f["verified"]))
        by[key] = by.get(key, 0) + 1
    _emit({
        "runs": len(runs),
        "outcomes": {o: sum(1 for r in runs if r["outcome"] == o) for o in OUTCOMES},
        "codex_cycles_per_run": round(
            sum(1 for c in cycles if c["reviewer"] == "codex") / len(runs), 2),
        "open_runs": len(runs) - len(ended),
        "tokens": {reviewer: sum(c["tokens"] or 0 for c in cycles if c["reviewer"] == reviewer)
                   for reviewer in REVIEWERS},
        "cycles_missing_tokens": sum(1 for c in cycles if c["tokens"] is None),
        "seconds": {"ended_runs_total": run_seconds, "review": review_seconds,
                    "fixing_and_other": run_seconds - sum(review_seconds.values())},
        "findings": {
            "total": len(found),
            "verified": sum(1 for f in found if f["verified"]),
            "by_reviewer_severity": {s: sum(1 for f in found if f["reviewer_severity"] == s)
                                     for s in SEVERITIES},
            "by_disposition": {d: sum(1 for f in found if f["disposition"] == d)
                               for d in DISPOSITIONS},
            "severity_relabelled": sum(1 for f in found
                                       if f["severity"] != f["reviewer_severity"]),
            "repeats": sum(1 for f in found if f["repeat_of"] is not None),
            "by_reviewer": {r: sum(1 for f in found for c in cycles
                                   if c["id"] == f["cycle_id"] and c["reviewer"] == r)
                            for r in REVIEWERS},
        },
        "escapes": {fb: sum(1 for e in escaped if e["found_by"] == fb) for fb in FOUND_BY},
        "detail": [{"reviewer_severity": k[0], "disposition": k[1], "verified": k[2], "count": v}
                   for k, v in sorted(by.items())],
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run-start")
    p.add_argument("--repo", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--base", default="origin/main")
    p.set_defaults(func=run_start)

    p = sub.add_parser("cycle-start")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--reviewer", choices=REVIEWERS, required=True)
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--sha")
    p.set_defaults(func=cycle_start)

    p = sub.add_parser("cycle-end")
    p.add_argument("--cycle", type=int, required=True)
    p.add_argument("--tokens", type=int,
                   help="tokens the reviewer reported (Codex 'tokens used'; /code-review subagent_tokens)")
    p.set_defaults(func=cycle_end)

    p = sub.add_parser("findings")
    p.add_argument("--cycle", type=int, required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=findings)

    p = sub.add_parser("finding-update")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--disposition", choices=DISPOSITIONS)
    p.add_argument("--severity", choices=SEVERITIES)
    p.add_argument("--verified", type=int, choices=(0, 1))
    p.add_argument("--fix-commit")
    p.add_argument("--repeat-of", type=int)
    p.add_argument("--settled-by", choices=SETTLED_BY)
    p.add_argument("--reason-file", help="JSON file holding a string reason")
    p.set_defaults(func=finding_update)

    p = sub.add_parser("ledger")
    p.add_argument("--run", type=int, required=True)
    p.set_defaults(func=ledger)

    p = sub.add_parser("run-end")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--outcome", choices=OUTCOMES, required=True)
    p.add_argument("--pr-url")
    p.set_defaults(func=run_end)

    p = sub.add_parser("escape")
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--found-by", choices=FOUND_BY, required=True)
    p.add_argument("--severity", choices=SEVERITIES, required=True)
    p.add_argument("--file", help="JSON file holding a string note")
    p.set_defaults(func=escape)

    p = sub.add_parser("report")
    p.add_argument("--repo")
    p.add_argument("--since")
    p.set_defaults(func=report)

    args = parser.parse_args()
    with _connect() as conn:
        args.func(conn, args)


if __name__ == "__main__":
    main()
