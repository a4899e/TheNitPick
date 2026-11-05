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

The database is shared by every session on the machine, so run, cycle and
finding IDs from different sessions interleave. A guessed ID can write into
another session's run. So instead of carrying IDs between commands, name the
run by ``--repo R --branch B`` (SEL below):

- It resolves to the one open run for that repo and branch. ``run-start``
  refuses to open a second one.
- ``cycle-end`` closes that run's open cycle, and ``findings`` goes to its
  latest cycle, which must have ended. ``cycle-start`` refuses while a cycle
  is still open.
- An explicit ``--run``, ``--cycle`` or ``--id`` still works. Given with SEL,
  it must belong to that run, or the command refuses.

Every command prints the run's ``run``, ``repo`` and ``branch``.

Usage (each command prints JSON; SEL is --repo R --branch B, or --run ID):
  metrics.py run-start --repo R --branch B [--base origin/main]
  metrics.py cycle-start SEL --reviewer codex|code-review --model M --effort E [--sha SHA]
  metrics.py cycle-end SEL [--cycle ID] [--tokens N [--replace-tokens]]
  metrics.py findings SEL [--cycle ID] [--reviewer R] --file findings.json
  metrics.py finding-update [SEL] --id N [--disposition D] [--verified 0|1] [--severity S]
                            [--fix-commit SHA] [--repeat-of N] [--settled-by H]
  metrics.py ledger SEL
  metrics.py run-end SEL --outcome clean|escalated|cap|abandoned [--pr-url URL]
  metrics.py escape SEL --found-by ci|human|production --severity S --file note.json
  metrics.py report [--repo R] [--since YYYY-MM-DD]

``ledger``, ``escape``, ``finding-update`` and ``run-end`` also accept a run
that has ended: with SEL and no open run, they take the most recent run for
that repo and branch. ``run-end`` on an ended run only adds a ``--pr-url``,
and only when ``--outcome`` repeats the recorded one.
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


def _where(run: sqlite3.Row) -> dict:
    """Which run a command acted on, echoed in every result so a mix-up shows."""
    return {"run": run["id"], "repo": run["repo"], "branch": run["branch"]}


def _open_runs(conn: sqlite3.Connection, repo: str, branch: str) -> list:
    return conn.execute(
        "SELECT * FROM runs WHERE repo = ? AND branch = ? AND ended_at IS NULL ORDER BY id",
        (repo, branch),
    ).fetchall()


def _resolve_run(conn: sqlite3.Connection, args: argparse.Namespace, *,
                 allow_ended: bool = False) -> sqlite3.Row:
    """The run a command acts on: ``--run``, or the open run for ``--repo``/``--branch``.

    Given both, they must agree. That catches a wrong ID before it writes into
    another session's run. With ``allow_ended`` and no open run, the most recent
    run for the repo and branch is used.
    """
    repo, branch = getattr(args, "repo", None), getattr(args, "branch", None)
    if (repo is None) != (branch is None):
        sys.exit("pass --repo and --branch together")
    if args.run is not None:
        run = _require(conn, "runs", args.run)
        if repo is not None and (run["repo"], run["branch"]) != (repo, branch):
            sys.exit(f"run {run['id']} is {run['repo']} {run['branch']}, not {repo} {branch}")
        if run["ended_at"] and not allow_ended:
            sys.exit(f"run {run['id']} ended at {run['ended_at']} ({run['outcome']})")
        return run
    if repo is None:
        sys.exit("name the run: --repo R --branch B (or --run ID)")
    rows = _open_runs(conn, repo, branch)
    if len(rows) > 1:
        ids = ", ".join(str(row["id"]) for row in rows)
        sys.exit(f"{len(rows)} open runs for {repo} {branch} (ids {ids}); pass --run")
    if rows:
        return rows[0]
    if allow_ended:
        row = conn.execute("SELECT * FROM runs WHERE repo = ? AND branch = ?"
                           " ORDER BY id DESC LIMIT 1", (repo, branch)).fetchone()
        if row is not None:
            return row
    sys.exit(f"no open run for {repo} {branch}; start one with run-start")


def _names_run(args: argparse.Namespace) -> bool:
    return any(getattr(args, name, None) is not None for name in ("run", "repo", "branch"))


def _owning_run(conn: sqlite3.Connection, args: argparse.Namespace, what: str,
                owner_id: int, *, allow_ended: bool = False) -> sqlite3.Row:
    """The run that owns a row named by ID. If the command names a run, it must be that one."""
    if not _names_run(args):
        return _require(conn, "runs", owner_id)
    run = _resolve_run(conn, args, allow_ended=allow_ended)
    if owner_id != run["id"]:
        sys.exit(f"{what} belongs to run {owner_id}, not run {run['id']}"
                 f" ({run['repo']} {run['branch']})")
    return run


def _resolve_cycle(conn: sqlite3.Connection, args: argparse.Namespace, *,
                   open_only: bool) -> "tuple[sqlite3.Row, sqlite3.Row]":
    """The run and cycle a command acts on.

    ``--cycle`` names one directly, and must belong to the named run if one is
    named. Its run may have ended, so a count or a late finding can still be
    corrected after ``run-end``. Otherwise it is the run's one open cycle (``open_only``), or its
    latest cycle, which must have ended: an open latest cycle means the
    findings being recorded probably belong to the one before it.
    """
    if args.cycle is not None:
        cycle = _require(conn, "cycles", args.cycle)
        run = _owning_run(conn, args, f"cycle {cycle['id']}", cycle["run_id"], allow_ended=True)
        return run, cycle
    run = _resolve_run(conn, args)
    if open_only:
        rows = conn.execute("SELECT * FROM cycles WHERE run_id = ? AND ended_at IS NULL"
                            " ORDER BY id", (run["id"],)).fetchall()
        if len(rows) != 1:
            sys.exit(f"run {run['id']} has {len(rows)} open cycles; pass --cycle")
        return run, rows[0]
    row = conn.execute("SELECT * FROM cycles WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                       (run["id"],)).fetchone()
    if row is None:
        sys.exit(f"run {run['id']} has no cycles; start one with cycle-start")
    if row["ended_at"] is None:
        sys.exit(f"cycle {row['id']} ({row['reviewer']} {row['n']}) is still open; end it"
                 f" first, or pass --cycle for an earlier one")
    return run, row


def run_start(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    # One open run per repo and branch, so --repo/--branch always names one run.
    existing = _open_runs(conn, args.repo, args.branch)
    if existing:
        row = existing[0]
        sys.exit(f"run {row['id']} for {args.repo} {args.branch} is still open (started"
                 f" {row['started_at']}). Continue it, or close it first with"
                 f" run-end --run {row['id']} --outcome abandoned")
    cur = conn.execute(
        "INSERT INTO runs (repo, branch, base, started_at) VALUES (?, ?, ?, ?)",
        (args.repo, args.branch, args.base, _now()),
    )
    _emit({"run": cur.lastrowid, "repo": args.repo, "branch": args.branch,
           "db": str(_db_path())})


def cycle_start(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    run = _resolve_run(conn, args)
    still_open = conn.execute("SELECT id FROM cycles WHERE run_id = ? AND ended_at IS NULL",
                              (run["id"],)).fetchone()
    if still_open is not None:
        sys.exit(f"cycle {still_open['id']} of run {run['id']} is still open; end it first")
    n = conn.execute(
        "SELECT COUNT(*) FROM cycles WHERE run_id = ? AND reviewer = ?", (run["id"], args.reviewer)
    ).fetchone()[0] + 1
    cur = conn.execute(
        "INSERT INTO cycles (run_id, n, reviewer, model, effort, head_sha, started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run["id"], n, args.reviewer, args.model, args.effort, args.sha, _now()),
    )
    _emit({**_where(run), "cycle": cur.lastrowid, "n": n, "reviewer": args.reviewer})


def cycle_end(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    run, cycle = _resolve_cycle(conn, args, open_only=True)
    # Never overwrite a recorded token count by accident: a wrong --cycle once
    # replaced another session's figure, and the original could not be
    # recovered. A deliberate correction names the cycle and its run.
    replace = args.replace_tokens
    if replace and (args.cycle is None or args.tokens is None or not _names_run(args)):
        sys.exit("--replace-tokens needs --tokens, --cycle, and --repo/--branch (or --run)")
    if (not replace and args.tokens is not None and cycle["tokens"] is not None
            and args.tokens != cycle["tokens"]):
        sys.exit(f"cycle {cycle['id']} already has tokens={cycle['tokens']}; pass"
                 f" --replace-tokens with --cycle and --repo/--branch to correct it")
    tokens = "?" if replace else "COALESCE(tokens, ?)"
    conn.execute(f"UPDATE cycles SET ended_at = COALESCE(ended_at, ?), tokens = {tokens}"
                 " WHERE id = ?",
                 (_now(), args.tokens, cycle["id"]))
    row = conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle["id"],)).fetchone()
    _emit({**_where(run), "cycle": row["id"], "n": row["n"], "reviewer": row["reviewer"],
           "seconds": _seconds(row["started_at"], row["ended_at"]), "tokens": row["tokens"]})


def findings(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Record a cycle's findings from a JSON list of objects.

    Each object: ``severity`` (reviewer's P0-P3, required), ``title`` (required),
    optional ``location``. Claude's own assessment starts equal to the
    reviewer's and is revised with ``finding-update --severity`` after
    reproduction.
    """
    run, cycle = _resolve_cycle(conn, args, open_only=False)
    if args.reviewer is not None and args.reviewer != cycle["reviewer"]:
        sys.exit(f"cycle {cycle['id']} belongs to reviewer {cycle['reviewer']},"
                 f" not {args.reviewer}")
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
                run["id"],
                cycle["id"],
                severity,
                severity,
                str(item["title"]),
                item.get("location"),
                _now(),
            ),
        )
        ids.append(cur.lastrowid)
    _emit({**_where(run), "cycle": cycle["id"], "n": cycle["n"], "reviewer": cycle["reviewer"],
           "findings": ids})


def finding_update(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    finding = _require(conn, "findings", args.id)
    run = _owning_run(conn, args, f"finding {args.id}", finding["run_id"], allow_ended=True)
    if args.repeat_of is not None:
        earlier = _require(conn, "findings", args.repeat_of)
        if earlier["run_id"] != run["id"]:
            sys.exit(f"finding {args.repeat_of} belongs to run {earlier['run_id']},"
                     f" not run {run['id']}")
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
    row = conn.execute("SELECT * FROM findings WHERE id = ?", (args.id,)).fetchone()
    _emit({**_where(run), **dict(row)})


def ledger(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    run = _resolve_run(conn, args, allow_ended=True)
    rows = conn.execute(
        "SELECT f.id, c.reviewer, c.n AS cycle, f.reviewer_severity, f.severity, f.verified,"
        " f.disposition, f.settled_by, f.fix_commit, f.repeat_of, f.location, f.title, f.reason"
        " FROM findings f JOIN cycles c ON c.id = f.cycle_id WHERE f.run_id = ?"
        " ORDER BY f.id",
        (run["id"],),
    ).fetchall()
    _emit({**_where(run), "outcome": run["outcome"], "findings": [dict(row) for row in rows]})


def run_end(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    run = _resolve_run(conn, args, allow_ended=True)
    # An ended run keeps its outcome. Repeating that outcome only adds a PR URL,
    # for a run that stopped at the cap and was opened as a PR anyway.
    if run["ended_at"] and args.outcome != run["outcome"]:
        sys.exit(f"run {run['id']} ended at {run['ended_at']} ({run['outcome']}); repeat"
                 f" --outcome {run['outcome']} to add a --pr-url")
    conn.execute(
        "UPDATE runs SET ended_at = COALESCE(ended_at, ?), outcome = ?,"
        " pr_url = COALESCE(?, pr_url) WHERE id = ?",
        (_now(), args.outcome, args.pr_url, run["id"]),
    )
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone()
    _emit({**_where(run), "outcome": run["outcome"], "pr_url": run["pr_url"],
           "seconds": _seconds(run["started_at"], run["ended_at"])})


def escape(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    run = _resolve_run(conn, args, allow_ended=True)
    note = _load_json(args.file) if args.file else None
    cur = conn.execute(
        "INSERT INTO escapes (run_id, found_by, severity, note, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (run["id"], args.found_by, args.severity, None if note is None else str(note), _now()),
    )
    _emit({**_where(run), "escape": cur.lastrowid})


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


def _select_run(p: argparse.ArgumentParser) -> None:
    """--repo/--branch name the open run; --run names one by ID (checked if both)."""
    p.add_argument("--repo")
    p.add_argument("--branch")
    p.add_argument("--run", type=int)


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
    _select_run(p)
    p.add_argument("--reviewer", choices=REVIEWERS, required=True)
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--sha")
    p.set_defaults(func=cycle_start)

    p = sub.add_parser("cycle-end")
    _select_run(p)
    p.add_argument("--cycle", type=int)
    p.add_argument("--tokens", type=int,
                   help="tokens the reviewer reported (Codex 'tokens used'; /code-review subagent_tokens)")
    p.add_argument("--replace-tokens", action="store_true",
                   help="correct a recorded count; needs --cycle and --repo/--branch")
    p.set_defaults(func=cycle_end)

    p = sub.add_parser("findings")
    _select_run(p)
    p.add_argument("--cycle", type=int)
    p.add_argument("--reviewer", choices=REVIEWERS,
                   help="refuse unless the cycle is this reviewer's")
    p.add_argument("--file", required=True)
    p.set_defaults(func=findings)

    p = sub.add_parser("finding-update")
    _select_run(p)
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
    _select_run(p)
    p.set_defaults(func=ledger)

    p = sub.add_parser("run-end")
    _select_run(p)
    p.add_argument("--outcome", choices=OUTCOMES, required=True)
    p.add_argument("--pr-url")
    p.set_defaults(func=run_end)

    p = sub.add_parser("escape")
    _select_run(p)
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
        # Each command checks, then writes ("no open run yet", "no open cycle").
        # Take the write lock first, so concurrent sessions can't both pass a check.
        conn.execute("BEGIN IMMEDIATE")
        args.func(conn, args)


if __name__ == "__main__":
    main()
