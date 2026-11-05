# TheNitPick

A [Claude Code](https://claude.com/claude-code) skill that reviews your branch before it becomes a pull request. Claude reviews first, then Codex. Claude checks each finding, fixes the real ones with tests that show the fix works, and opens one PR when the review comes back clean.

## What does this do?

TheNitPick reviews your work, both the code and its design, before it becomes a pull request. Claude reviews first. Then Codex gives it a second set of eyes, so you no longer have to invoke another agent by hand to get an independent review. Claude verifies each finding, fixes the real ones with tests that prove the fix, and repeats until the review comes back clean.

All of this happens after you commit but before anything is pushed. Review feedback gets addressed locally instead of through push after push of fixes, so CI runs once per PR rather than once per round of review.

## Why not just ask Codex (or Claude, or Gemini, or …) to review it myself?

You can, and it works. Asking another model "please review my branch" is useful, and it's how this skill started. Done by hand, though, the same few things keep slipping:

- **It's ad hoc.** The prompt, the scope (the latest commit or the whole branch?), and how carefully you read the answer vary from one time to the next. TheNitPick runs the same procedure every time.
- **You become the relay.** You start the review, wait, copy findings back, ask for fixes, then start another review. Here, Claude drives the loop and you're only pulled in to make a decision.
- **Findings get accepted on faith, or waved away.** A model's review contains real bugs, plausible-sounding non-bugs, and severity labels that are often wrong. TheNitPick makes every fix earn its place with a reproducing test, and every rejection earn its place with evidence.
- **Loops don't converge on their own.** "Fix it and review again" can run indefinitely, because a reviewer asked often enough always finds something. TheNitPick caps the cycles, detects repeated concerns, and hands non-convergence back to you instead of silently stopping.
- **The reviewer can be steered by the code it reviews.** Agent-instruction files (`AGENTS.md`, `CLAUDE.md`, `.codex/`) live in the repo, and an AI reviewer reads them automatically. TheNitPick takes them from your base branch, so the branch under review can't rewrite its reviewer's instructions. It also never runs git checkout machinery (hooks, filters) on branch content.
- **Every push can cost a CI run.** All review happens locally, so the branch is pushed once.
- **You can't tell whether it's worth it.** The metrics database records what each reviewer found, what that cost in time and tokens, and what still slipped through.

## Why this skill instead of others?

You don't have to use this over anything else, or at all. There are good alternatives, from built-in reviewers (Claude Code's `/code-review` and Code Review, Codex's `/review`) to well-maintained community skill packs and plugins, several with their own review loops. If one of them works for you, keep using it.

TheNitPick is distilled from about a year of building two startups and a number of personal projects with coding agents. It records what I learned about getting AI review to catch real bugs without slowing everything down.

The core of it is the habit I kept coming back to: when a branch looked done, I'd invoke Codex's Review Agent by hand for an independent, defect-first read, then work through what it found. TheNitPick keeps the best parts of that: an independent model, findings limited to concrete and actionable defects, and the full branch as the scope. It drops the need to invoke and shepherd the review yourself, and adds the verification, loop control, isolation and record-keeping that doing it by hand never quite got.

## How to use it

Install it (see [Install](#install)), then use one of the two prompts below. You never need to commit, push or start a reviewer by hand. Claude does all of that, and asks you only when a finding needs your judgment.

### You already have changes, and want them reviewed and turned into a PR

Your changes can be uncommitted, committed on a branch, or both. In Claude Code, from the repository:

```text
/thenitpick
```

Or in plain words, which works the same:

```text
Run TheNitPick on my changes on this branch. Commit anything uncommitted,
run the review loop, and open the PR against main when the review is clean.
```

If you've already committed and only want the review and the PR, say so:

```text
My commits on this branch are done. Run TheNitPick on the branch against main
and open the PR when it's clean.
```

### You want Claude to build something, and review it on the way to the PR

Name the work, and ask for TheNitPick before the PR:

```text
Implement <the feature or fix>. When it's working and the tests pass, use the
thenitpick skill to get it ready for a PR: commit, run the review loop, fix
what it finds, then push once and open the PR.
```

To make this the default for every PR in a project, so you don't have to ask each time, add this to the project's `CLAUDE.md` (or to `~/.claude/CLAUDE.md` for all your projects):

```markdown
## Pull requests
Before opening any pull request, use the thenitpick skill: commit the work,
run its review loop until it's clean, then push once and open the PR with
its review packet.
```

### What you'll hear about

- A finding Claude can't settle on evidence alone, and needs you to decide.
- A review that isn't converging. The loop stops after 5 Codex cycles and summarizes what keeps coming up, and you decide whether to keep going, change approach, or open the PR anyway.
- The PR, once it's open.

## What happens behind the scenes

When the change is ready, TheNitPick runs a review loop on your machine, before anything is pushed:

1. **Claude reviews the change** once (`/code-review high`) and fixes what's real.
2. **The work is committed.** Claude commits it, unless you've already committed it yourself. Your pre-commit hooks must pass.
3. **Codex reviews the whole branch** using its Review Agent, a defect-first reviewer that reports only concrete, actionable findings, ranked P0–P3. It runs in a disposable, locked-down copy of your repo where it can run your tests.
4. **Claude settles each finding on evidence.** It reproduces the bug with a test that fails, fixes it, confirms the test passes, and commits the fix. If it disagrees with a finding, it needs a failed reproduction or a targeted Codex re-check before it may decline it.
5. **Codex re-reviews**, up to 5 cycles, until a cycle comes back clean.
6. **One push, one PR**, with a single "review packet" comment: every finding, how it was resolved, the fixing commit, and what wasn't verified.

Every run is logged to a local SQLite database: time spent, tokens used, findings by severity and outcome, and bugs that escaped review. Over time you can see whether the loop pays for itself.

## Requirements

- [Claude Code](https://claude.com/claude-code)
- The [Codex CLI](https://developers.openai.com/codex), signed in. TheNitPick calls the Review Agent skill (`$review-agent`) that ships with Codex.
- git
- Python 3.9 or later (standard library only)
- Linux, macOS, or Windows 11 via WSL2. Native Windows is experimental (see [Platforms](#platforms)).

## Install

Clone into your personal Claude Code skills directory:

```bash
git clone https://github.com/a4899e/TheNitPick ~/.claude/skills/thenitpick
```

On Windows, the same directory is `%USERPROFILE%\.claude\skills\thenitpick`.

Claude Code picks the skill up automatically. Invoke it with `/thenitpick`, or tell Claude a branch is ready for a PR.

If the `codex` binary isn't on your `PATH`, set `THENITPICK_CODEX` to its location.

## Metrics

To see what the loop has done for you over time:

```bash
python3 ~/.claude/skills/thenitpick/scripts/metrics.py report
python3 ~/.claude/skills/thenitpick/scripts/metrics.py report --repo myrepo --since 2026-11-01
```

When a bug the loop missed turns up later in CI, a human review or production, record it as an escape (see `SKILL.md`). That's the number that tells you how well review is really working.

## Configuration

| What | Default | Change it |
|---|---|---|
| Codex model and effort | `gpt-6.1-sol`, `high` | `--model` / `--effort` on `codex_review.py`; edit `SKILL.md` to change the default |
| Max Codex cycles | 5 | `SKILL.md` |
| Base branch | `origin/main` | `--base`; Claude uses your repo's default branch |
| Metrics database | `~/.thenitpick/metrics.db` | `THENITPICK_DB` |
| Codex binary | `$THENITPICK_CODEX`, then `codex` on `PATH` | `--codex` |

High effort is a deliberate default. In my experience xhigh costs a lot more without adding much for most changes.

## Platforms

- **Linux and macOS:** supported natively.
- **Windows 11: use WSL2.** Run Claude Code, git and Codex inside WSL2, where TheNitPick behaves exactly as on Linux.
- **Native Windows: experimental, opt-in.** The wrapper refuses to run on native Windows unless `THENITPICK_EXPERIMENTAL_WINDOWS=1` is set. The Windows-specific code paths (junctions, reparse points, `codex.cmd` shims, Win32 filename rules) have been reviewed and unit-tested with Windows simulated, but never run on a real Windows machine. Please open an issue if you try it. Known differences:
  - use `py -3` where the docs say `python3`;
  - without Developer Mode, symlinks in the reviewed tree are written as small text files, git's own `core.symlinks=false` behaviour;
  - edit detection compares content but not file modes;
  - how strong the sandbox is depends on Codex's native Windows sandbox.

  Known open issues on native Windows:
  - **NTFS 8.3 short names.** On volumes with 8.3 names enabled, a branch can write into a pinned instruction directory through its short alias (for example `AGENTS~1` for `.agents`), getting around instruction pinning.
  - **Symlink type detection.** Detection ignores Windows case-insensitivity and backslash separators, so some directory symlinks are created as file symlinks.
  - **Links to restored instruction directories.** A link to a pinned instruction directory that only exists on the base branch is created as a file symlink.

## How the isolation works

Codex reviews a throwaway worktree of your branch:

- **No checkout.** Every file is written byte-for-byte from git's raw objects, so no hook, attribute, or smudge or process filter runs on branch content on your machine.
- **Instructions come from the base branch.** Agent-instruction files are taken from the base branch, and nothing is ever written through a symlink.
- **Codex is locked down.** It runs with a minimal environment (no cloud or API credentials), network off, writes confined to the worktree and a private temp directory, no user config or rules, no memories, and no persisted session.
- **Edits are detected.** Anything Codex changes in the worktree is reported, and the worktree is deleted afterwards. Changes to the reviewed files are listed first, one by one. Gitignored build output from running tests is counted per directory, so it doesn't bury them.

These protections were hardened by running TheNitPick on itself. See PR #1. Codex raised 11 issues there, most of them ways a branch under review could run code on the host or steer its reviewer before the sandbox applied. The design changed until a full cycle found nothing.

## Limitations

- Review recall is partial for every model and tool. TheNitPick lowers the odds of a bug slipping through; it doesn't make that impossible. Track escapes.
- Claude both writes the fixes and settles the findings. That's why every decision has to rest on a reproduction or a Codex re-check rather than Claude's judgment alone.
- Submodules appear as empty directories, and Git LFS files as their pointers.
- With network off, Codex can't run tests that start a local server: the sandbox refuses even localhost listeners. Claude runs those tests itself, and the review packet says which ones Codex couldn't.
- The loop runs locally, so teammates see the review packet but can't independently verify that the loop ran.

## Files

- `SKILL.md`: the procedure Claude follows.
- `scripts/codex_review.py`: runs one locked-down Codex review cycle.
- `scripts/materialize.py`: builds the review worktree from raw git objects.
- `scripts/metrics.py`: the metrics database and findings ledger.
- `tests/`: standard-library tests. Run them with `python3 -m unittest discover -s tests`.

## License

Copyright 2026 Trevor Tonn. Licensed under the Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Future ideas

Not built yet. These are directions I may take TheNitPick, recorded here so the reasoning isn't lost.

### A final "craft" pass for idiom and standards

Agents today still sometimes miss, or deprioritize, the things an experienced engineer would flag on sight: code that isn't idiomatic for its language, overly broad exception handling, swallowed errors, mutable defaults, and other best practices that matter but rarely surface as "bugs." The idea is a final, single pass, run after the Codex loop converges and before the push:

- **A narrow, checklist-driven reviewer.** A fresh Claude subagent per language in the diff, given only the final diff and a short standards checklist for that language (`standards/general.md`, `standards/python.md`, …). Focused reviewers with explicit "what to flag, what not to flag" lists hold up better in practice than a general "review this."
- **Every finding cites a rule ID** (for example `PY-EXC-1: no broad catch without a justifying comment`). That keeps the pass from drifting into generic nits, and lets the metrics show which rules actually fire.
- **Linters first where they can do the job.** Many of these rules map to existing linter rules (ruff `BLE001`/`E722`, `B006`; ESLint `no-floating-promises`, …). Running those on the changed lines first leaves only the judgment calls (idiom, naming, structure, error design) to the model.
- **Per-project standards** in `.thenitpick/standards.md`, read from the base branch, so a branch can't change its own rules.
- **Severity and loop control.** Findings default to P2 or P3. P2s get fixed; P3s get fixed if cheap, otherwise listed in the review packet. If the fixes change behavior, one more Codex cycle runs on them, without restarting the main loop.
- **Learning over time.** Per-rule counts in the metrics database show which rules should become linter rules or project instructions, and which can be retired.
