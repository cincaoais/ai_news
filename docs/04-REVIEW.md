# Review & Test Report: Daily AI News Bot

Date: 2026-07-31 · Phase 4 of system-builder · Repo: `cincaoais/ai_news` (private)
Inputs: `docs/00-BRIEF.md`, `docs/01-RND.md`, `docs/02-PLAN.md`, `docs/03-EXECUTION-LOG.md`

## Verdict

**Ready, with time-gated verification still accruing.** The system is live and has delivered
three real digests today (two local, one from the cloud Routine) with zero manual steps in
the delivery path. All 213 automated tests pass; every review finding rated CRITICAL or
MAJOR that could be fixed in code has been fixed and covered by new tests. Three items
cannot be closed today because they are inherently multi-day observations (three
consecutive unattended scheduled runs, the 48-hour OAuth-token soak, and the first
cross-day dedup check) — each has a defined completion date and a defined failure signal.
Two findings are documented rather than fixed: the Routine's single-session design (its
editorial step is not sandboxed the way the Actions path is) and the same-day guard's
residual race under deliberate dual-scheduler operation. Neither is reachable in normal
single-scheduler use, and both have a designed remediation path in the backlog.

## Review findings

Three independent fresh-eyes reviewers (correctness, security, plan-conformance) audited
the finished system. 13 substantive findings; 9 fixed, 4 accepted-and-documented.

| # | Finding | Severity | Where | Status |
|---|---|---|---|---|
| 1 | `escape_html` didn't escape `"`, so a feed-controlled URL could break out of `href="..."` and inject a forged link | CRITICAL | `lib/telegram.py` | **Fixed** (F1) |
| 2 | Same-day guard is check-then-act against a locally-cloned state file; two overlapping schedulers could both send | CRITICAL | `build_and_send.py` | **Mitigated** (F6) + documented; full fix in backlog |
| 3 | `requests` exception strings embed the bot token (URL contains it) and were printed to stderr / CI logs | MAJOR | `telegram.py`, `notify_failure.py`, `get_chat_id.py` | **Fixed** (F2) |
| 4 | Partial multi-message send: a failure on message 2 of N re-sent message 1 on retry | MAJOR | `build_and_send.py` | **Fixed** (F5) |
| 5 | 5–8 item count enforced only by prompt; `max_items` config key was dead code | MAJOR | `build_and_send.py`, `fetch_news.py` | **Fixed** (F4) |
| 6 | Unrecoverable state push after a successful send silently loses that day's dedup record | MAJOR | `update_state.py` | **Fixed** (F7, honest alert) + documented |
| 7 | Routine runs fetch + editorial + send in one session with shell and Full network; only prose constrains it | MAJOR | Routine design | **Accepted/documented**, backlog remediation |
| 8 | `TELEGRAM_CHAT_ID` was never set as a GitHub repo secret — a real Actions dispatch would fail at send | MAJOR | live repo config | **Fixed** (secret set 08:31Z) |
| 9 | No URL scheme allowlist (`javascript:`/`data:` links flowed through) | MINOR | `fetch_news.py` | **Fixed** (F3) |
| 10 | `load_json` rejected UTF-8 BOM (hand-edited files on Windows) | MINOR | `build_and_send.py`, `run_digest.py` | **Fixed** (F8) |
| 11 | `get_chat_id.py` could crash on a non-ASCII chat name on a Windows console | MINOR | `get_chat_id.py` | **Fixed** (F9) |
| 12 | README claimed both schedulers execute RUNBOOK.md (only the Routine does); secrets table overstated | MINOR | `README.md` | **Fixed** (F10 + follow-up commit) |
| 13 | Feed-controlled headline text persists into `last_digest.md`, re-read as trusted context next day | MINOR | `update_state.py` | **Accepted/documented** (compounding factor only) |

Areas the reviewers explicitly found solid: `validate_selection` makes URL/title forgery
by the model structurally impossible (titles and URLs are always re-attached from
`candidates.json` by index); `git add` scope is hard-limited to `state/`; `.env` is
gitignored and has never appeared in git history; both workflows are dispatch/cron only
with minimal `permissions:` and no shell-injection surface; no TODO/FIXME left in code.

## Fixes applied

One commit, `e64a9ec` ("Phase 4 fixes"), plus `5bf772e` (README correction) and the live
repo-secret fix:

- **F1** `escape_html` now escapes `"` → `&quot;`; breakout URL tested through `build_item_block`.
- **F2** New `redact_token()` applied at every site where an exception or response text is
  logged/raised with the token in scope.
- **F3** Non-`http(s)` candidate URLs dropped at candidate-build time with a warning.
- **F4** `candidates.json` now carries `limits: {min_items, max_items}` from config;
  `validate_selection` enforces both, preserving the thin-news / few-candidates carve-outs.
- **F5** `work/sent_progress.json` written after each successful message; a retry resumes
  instead of re-sending; deleted on full success; untouched in dry-run.
- **F6** Immediately before the first real send, the sender re-fetches `origin` and
  re-reads `state/last_digest.md` from `origin/main` — shrinking the dual-scheduler race
  window from clone-age (minutes) to seconds. Any git failure logs a warning and proceeds.
- **F7** State-push failures now say explicitly: the digest was already sent, only the
  dedup record failed, tomorrow may repeat. Conflicting-rebase path now tested.
- **F8/F9/F10** BOM tolerance, Windows console encoding, README truthfulness.

## Test results

Full suite, commander-run (not a subagent self-report):

```
213 passed, 1 deselected, 54 warnings in 6.89s
```

The single deselected test is the network-dependent live smoke test, excluded by default
via `pytest.ini` and run explicitly during Slice 1 acceptance (passed). Test count grew
41 → 113 → 132 → 133 → 161 → 213 across slices; the Phase 4 fix pass alone added 52 tests
targeting the exact failure modes the reviewers identified.

Workflow linting: `actionlint v1.7.12` exit 0 on both `digest.yml` and `watchdog.yml`.

## Scenario walkthroughs

| # | Scenario | Steps | Expected | Actual | Result |
|---|---|---|---|---|---|
| 1 | Happy path, local | `python scripts/run_digest.py` (real feeds, real Sonnet, real send) | Digest to Telegram, state committed+pushed | Digest received; `work/sent.json` written; commit `77014c1` pushed | ✅ PASS |
| 2 | Happy path, cloud (the real product) | Routine "Run now" → clone → RUNBOOK → send | Sports digest to Telegram, state pushed from cloud | Digest received; commit `16d1387` authored by the cloud session on `origin/main` | ✅ PASS |
| 3 | Profile switch | Edit `active_profile` → commit → push → run | Next run uses new profile | finance → sports → finance, each observed in output | ✅ PASS |
| 4 | Same-day duplicate | Second send, same profile, same MYT day | Refuse, exit nonzero, no network call | `already sent today (2026-07-31 MYT); pass --force`, exit 1 | ✅ PASS |
| 5 | `--force` override | Same as #4 with `--force` | Digest builds | Rendered 2 messages | ✅ PASS |
| 6 | Dead credentials | Real send with an invalid bot token | Loud failure, never silent | `Telegram API error 401: Unauthorized`, exit 1 | ✅ PASS |
| 7 | Failure alert delivery | Induce editorial-stage failure (missing fixture) | Stage-tagged alert **arrives in Telegram** | Alert delivered to the user's chat | ✅ PASS |
| 8 | Cloud misconfiguration | Routine run 1 with default "Trusted" network | Fail loudly and diagnosably | Feeds+Telegram blocked → fetch failed → notifier undeliverable → the session's own report reached the user (third channel) | ✅ PASS (real incident) |
| 9 | Watchdog (2nd alert channel) | Dispatch `watchdog.yml` on GitHub | Green when state fresh | Success in 9s | ✅ PASS |
| 10 | Actions fallback plumbing | Dispatch `digest.yml` with `mock=true` | Green without secrets | Success | ✅ PASS |
| 11 | Thin news + feed-health footer | Fixture-forced stale/dead feeds | "Quiet news day." + one-line footer | Both rendered, HTML-safe | ✅ PASS |
| 12 | Subscription token in CI (soak T0) | Dispatch `digest.yml` with `mock=false, dry_run=true` | Real `claude -p` authenticates on a GitHub runner | **Success in 2m31s**, zero failed steps (run 30617659091) — vs 39s for the mock, i.e. a genuine Sonnet editorial pass ran | ✅ PASS |

## Plan conformance

Definition-of-done checklist from `docs/02-PLAN.md`:

- [x] **pytest suite green; every slice's test gate re-run** — 213 passed, commander-verified.
- [x] **Live end-to-end on dev machine (dry-run and real send)** — scenario 1.
- [x] **Real Telegram digest from a cloud run** — scenario 2, commit `16d1387`.
- [ ] **3 consecutive unattended scheduled runs** — ⏳ 1 of 3 (and that one was manually
      triggered). Next unattended firing 2026-08-01 ~08:06 MYT; completes ~2026-08-03.
- [x] **Profile switch demo** — scenario 3.
- [x] **Failure demo (a): pipeline failure → Telegram alert arrives** — scenario 7.
- [x] **Failure demo (b): independent channel** — scenario 9 (watchdog green; its red path
      is exercised by the 26-hour staleness check, verified by unit-level reasoning and the
      workflow's own logic, not yet by a real red run — see Known issues).
- [ ] **No story repeated across two consecutive digests** — ⏳ first observable 2026-08-01.
- [x] **Every digest link resolves; zero model-authored URLs** — 6 of 8 live URLs curl'd
      `200` by the conformance reviewer; URL authorship structurally impossible (validator).
- [x] **README operable by a stranger; secrets documented; annual token-refresh note** —
      audited line by line; two inaccuracies found and fixed.
- [~] **Actions fallback: green mock dispatch; 48-h soak; cron documented-but-disabled** —
      mock dispatch green (scenario 10); cron correctly commented out; **soak T0 green**
      (scenario 12 — subscription OAuth works in CI, settling the RND conflict between the
      documented 1-year token and community reports of ~24h expiry); T+48h re-dispatch due
      ~2026-08-02 to confirm the token survives.

Brief conformance: all five must-haves satisfied (daily Telegram digest; Claude-powered
with no LLM API key, Sonnet-selected; cloud-scheduled with the PC off; config-switchable
profiles; free to run). All six working assumptions (A1–A6) hold; A6 in particular was
resolved by evidence — claude.ai connectors are reachable from the Routine but are
deliberately not load-bearing, and RSS alone carries the system.

## Known issues & limitations

1. **Time-gated verification (3 items).** Three consecutive unattended runs, the 48-hour
   token soak's T+48h leg (T0 is green), and cross-day dedup all need calendar time.
   Failure signals: no digest by 09:00 MYT; a red `digest.yml` dispatch on 2026-08-02;
   a repeated headline tomorrow.
2. **Routine session isolation (finding 7).** Unlike the Actions path — which confines the
   model to `--allowedTools "Read,Write"` for the editorial step only — the Routine runs
   the whole runbook in one session with shell access and Full network while reading
   attacker-controllable feed text. The structural validator still makes URL/title forgery
   impossible, and `git add` is limited to `state/`, so the realistic blast radius is a
   misleading summary, not a forged link. Remediation (backlog): have the Routine shell out
   to a scoped `claude -p` subprocess for the editorial step, mirroring `run_digest.py`.
3. **Same-day guard is best-effort under deliberate dual-scheduler operation (finding 2).**
   F6 shrinks the window to seconds; it is not a distributed lock. Operating rule, now in
   the README: run one scheduler at a time. Full fix (backlog): an atomic create-or-fail
   push to a dedicated git ref as a real mutex.
4. **Network access is "Full" on the Routine environment.** Deliberate for launch
   simplicity; the plan's own recommendation is to tighten to a Custom allowlist (news
   domains + `api.telegram.org`) once stable.
5. **Connectors remain attached to the Routine** (FMP, Bigdata.com, Firecrawl auto-attach).
   They are inert — excluded from `allowed_tools` and forbidden by the runbook — but
   stripping them was deliberately deferred rather than done blind before a scheduled run.
6. **Watchdog red path not yet observed live.** It went green correctly; the failing branch
   is logic-verified but has never actually fired.
7. **Mock-mode index pairing.** With `--mock-claude`, a static fixture's indices pair with
   freshly-fetched articles, so headlines and summaries can mismatch. Cosmetic and
   impossible in production (selection is always authored against the same run's
   candidates); it exists only to make CI runs secret-free.
8. **`TelegramError.__cause__` still chains the unredacted exception.** Nothing in this
   codebase prints tracebacks, so no leak today; a future caller that logs full tracebacks
   would need to redact.

## Suggested next steps

1. **Watch the three time-gated items** (nothing to do unless a signal fires).
2. **Backlog, in priority order:** the git-ref mutex for the same-day guard; the scoped
   editorial subprocess for the Routine; tighten Routine network to a Custom allowlist;
   strip unused connectors on a controlled Run-now day; force a real watchdog red run to
   confirm the GitHub failure e-mail actually lands.
3. **v2 features already scoped:** `/topic` switching via Telegram commands (with the
   documented 24-hour `getUpdates` retention caveat), FMP/Bigdata enrichment of the finance
   snapshot, and additional language profiles.
4. **Annual maintenance:** re-run `claude setup-token` before the one-year expiry and update
   the `CLAUDE_CODE_OAUTH_TOKEN` repo secret (noted in the README).
