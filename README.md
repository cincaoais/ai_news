# ai-news

> **WIP -- partial README.** This file currently documents only the GitHub Actions
> fallback deployment (Slice 4 of `docs/02-PLAN.md`). The full architecture overview,
> profile-switching guide, scheduler-switching guide, and troubleshooting checklist are
> added in Slice 6. Until then, see `docs/02-PLAN.md` and `RUNBOOK.md` for the complete
> picture.

## Deploying the GitHub Actions fallback

`.github/workflows/digest.yml` is a **dormant fallback** scheduler. The primary
scheduler is a Claude Code Routine (Slice 5); this workflow exists so the digest can
still run if the Routine is unavailable. Its daily cron trigger ships **commented out**
-- see the note inside the workflow file for why, and how to enable it.

### 1. Secrets to create

Before any *real* (non-mock) dispatch, create these repository secrets (Settings ->
Secrets and variables -> Actions -> New repository secret):

| Secret | How to get it |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Run `claude setup-token` locally (requires an active Claude subscription) and paste the printed token. Documented to last ~1 year; see the annual refresh note below. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather when the bot was created (Checkpoint A). |
| `TELEGRAM_CHAT_ID` | Run `python scripts/get_chat_id.py` locally after messaging the bot once, and copy the printed chat id. |

Mock dispatches (see below) need **none** of these -- they never call the real
`claude` CLI or send a real Telegram message.

### 2. Running a mock dispatch (no secrets needed)

From the repo's Actions tab -> "Daily digest (Actions fallback)" -> "Run workflow":

- leave `mock` checked (default `true`)
- `dry_run` is ignored in mock mode (always forced true internally)

This runs `python scripts/run_digest.py --mock-claude tests/fixtures/mock_selection.json
--dry-run --skip-state` -- fetches real news, substitutes a fixture selection instead of
calling `claude`, prints the digest instead of sending it, and skips the state commit
entirely. A green run here (including the run step, with no secrets set) is part of the
Slice 4 test gate.

### 3. Running a real dispatch

Same "Run workflow" button, with `mock` unchecked. Leave `dry_run` checked to validate a
real `claude` call + real digest formatting without sending to Telegram or committing
state; uncheck both for the real end-to-end path (sends to Telegram, commits + pushes
`state/` with a `[skip ci]`-tagged message so the state commit cannot re-trigger this
workflow). This requires all three secrets above to be set.

### 4. Enabling the cron

The workflow only runs on `workflow_dispatch` until the fallback is proven usable (see
Slice 4's test gate below). To turn on the daily schedule, uncomment the `schedule:`
block in `.github/workflows/digest.yml`:

```yaml
schedule:
  - cron: "47 23 * * *"   # = 07:47 MYT (UTC+8) the next day
```

Do this only after:
- a green mock `workflow_dispatch` run (no secrets), and
- a green real `workflow_dispatch` run (with secrets, producing an actual Telegram
  message), and
- the 48-hour token soak test below has passed.

### 5. The 48-hour token soak test

`docs/01-RND.md` flagged a conflict in the docs about how long a `CLAUDE_CODE_OAUTH_TOKEN`
actually stays valid (1 year vs. 24 hours, depending on source). This is settled
empirically, not by reading more docs:

1. **T0**: with all three secrets set, dispatch a real run (`mock` unchecked, `dry_run`
   checked is fine -- the point is only that the `claude` CLI authenticates and runs).
   Confirm the run goes green and the "Run digest" step's logs show a real `claude`
   invocation succeeding (not an auth error).
2. **T0 + 48h**: dispatch the same real run again, same secrets, no changes. Confirm it
   *also* authenticates successfully.
3. Only once **both** dispatches are green is the Actions fallback considered usable as
   a scheduler. If the token has expired by T0+48h, the "1 year" claim is wrong and the
   fallback needs a shorter, documented refresh cadence instead -- update this section
   with whatever is actually observed.

### 6. Annual token refresh

Whatever the soak test settles on, `CLAUDE_CODE_OAUTH_TOKEN` will eventually expire.
Re-run `claude setup-token` locally and update the `CLAUDE_CODE_OAUTH_TOKEN` repository
secret with the new value. (Slice 6 will add a recurring reminder for this; for now,
treat it as an annual manual task.)
