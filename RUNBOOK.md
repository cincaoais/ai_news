# RUNBOOK: daily digest procedure

Exact procedure for a cloud session (Claude Code Routine) with shell access. Follow these
steps in order. Do not improvise beyond this runbook. All commands run from the repo root.

## 0. Setup (only if needed)

```
pip install -r requirements.txt
```

## 1. Fetch news

```
python scripts/fetch_news.py
```

- Exit code 0 -> `work/candidates.json` was written. Continue to step 2.
- Exit code nonzero -> run:
  ```
  python scripts/notify_failure.py "fetch: <reason from the fetch_news stderr output>"
  ```
  Then **STOP**. Do not continue to any later step.

## 2. Editorial selection

Read `work/candidates.json` and `state/last_digest.md`. Follow `CLAUDE.md` in this
repository exactly (role, schema, hard rules, editorial guidance). Write your selection to
`work/selection.json`.

Do not send anything and do not touch `state/` in this step.

## 3. Validate and send

```
python scripts/build_and_send.py
```

(No `--dry-run` -- this is the real daily send.)

- Exit code 0 -> digest sent. Continue to step 4.
- Exit code nonzero because of a **validation error** in `work/selection.json` (unknown
  index, empty/oversized summary, a URL found in your output, etc.): fix
  `work/selection.json` yourself, following the printed error and `CLAUDE.md`'s rules, and
  retry this step **ONCE**.
- Still failing after that one retry, or the send itself fails (Telegram error): run:
  ```
  python scripts/notify_failure.py "send: <reason from the build_and_send stderr output>"
  ```
  Then **STOP**.

## 4. Update state

```
python scripts/update_state.py
```

- Exit code 0 -> state committed (and pushed, if a remote is configured). Done.
- Exit code nonzero -> run:
  ```
  python scripts/notify_failure.py "state: <reason from the update_state stderr output>"
  ```
  Then **STOP**.

## 5. Done

The run is complete once step 4 succeeds. Do not do anything beyond this runbook -- no
extra summaries, no additional commits, no further tool calls.
