# RND Report: Daily AI News Bot

Date: 2026-07-31 · Phase 1 of system-builder · Brief: `docs/00-BRIEF.md`

## Executive summary

Six parallel research missions investigated scheduling, prior art, news sources, Claude Code
headless capabilities, Telegram mechanics, and community failure reports. The single most
important takeaway: **Anthropic ships a first-party feature — Claude Code Routines — that does
exactly what this project needs**: subscription-authenticated (Pro & Max), daily-scheduled,
cloud-run Claude Code sessions that work with the PC off, can run shell commands in a cloned
GitHub repo, push commits, and — critically — **can use the user's claude.ai connectors
(Bigdata.com, FMP, Firecrawl)**, which are otherwise unreachable from any headless CI run.
The second key takeaway is architectural: community evidence shows Claude's WebFetch is
structurally unreliable headless and hallucinated links are the #1 quality failure of news
bots, so the pipeline must be **RSS-first**: a script fetches verified feeds and market data,
Claude only selects and summarizes from fetched content, and the model never types a URL from
memory. All required data sources were live-verified today as free and keyless. Recommended
direction: a GitHub repo of config profiles + Python scripts, driven daily by a Routine
(Sonnet), with a GitHub Actions + `claude setup-token` workflow as the dormant fallback
scheduler. Telegram delivery via BotFather bot, HTML parse mode, stdlib HTTP.

## Research missions

| Mission | Subagent role | Sources covered | Status |
|---|---|---|---|
| Cloud scheduling options | Infrastructure scout | Anthropic docs, GitHub docs, The Register, community forums | ✅ Complete |
| Similar open-source repos | Open-source scout | GitHub (9 repos read via raw files), Medium | ✅ Complete |
| Free news sources | Data sources researcher | 30+ feeds/APIs live-fetched with curl | ✅ Complete |
| Claude Code headless | Platform researcher | code.claude.com official docs (quoted) | ✅ Complete |
| Telegram delivery | Messaging researcher | core.telegram.org official docs (quoted) | ✅ Complete |
| Community pitfalls | Community listener | GitHub issues, HN, Reddit, news outlets, builder blogs | ✅ Complete |

Commander spot-checks performed on the two most load-bearing claims (token lifetime,
Routines capabilities) by fetching the official docs directly — see Theme A/B.

## Key findings by theme

### Theme A — Scheduling: Routines is the sanctioned path; GitHub Actions is the fallback

**Claude Code Routines** (research preview; public beta ~June 2026): saved prompt + GitHub
repo(s) + connectors, run on Anthropic-managed cloud on a schedule. Verified from
[the official routines doc](https://code.claude.com/docs/en/routines) (commander-fetched):
- Available on **Pro, Max, Team, Enterprise** (with Claude Code on the web enabled).
- Schedule presets incl. **daily**; min interval 1 hour; times entered in local timezone;
  runs may start a few minutes late (consistent stagger).
- Runs are **full Claude Code cloud sessions**: shell commands, skills from the cloned repo,
  no permission prompts. Per-routine **model selector** → Sonnet ✓ (brief requirement).
- **Repo write-back works**: repos cloned each run; pushes to `claude/`-prefixed branches
  always accepted; pushes to other branches accepted unless protected / someone else's PR /
  foreign-authored commits. → dedup state can live in the repo.
- **Connectors work**: "Routines can use your connected MCP connectors… traffic is routed
  through Anthropic's servers", no allowlist changes needed. → Bigdata.com/FMP usable here.
- **Network**: Default env = "Trusted" allowlist which would 403 `api.telegram.org` and news
  sites; must set env network access to **Custom** (allowlist our domains) or **Full**.
- Usage: draws normal subscription usage + a daily per-account routine-run cap (scout
  reported Pro 5/day, Max 15/day — plan-tier numbers not re-verified; 1 run/day is safely
  inside either). Green run status ≠ task success — transcript must be checked / the digest
  itself is the heartbeat.

**GitHub Actions + `claude setup-token`** (fallback): officially documented
([authentication doc](https://code.claude.com/docs/en/authentication), commander-fetched):
one-year OAuth token "for CI pipelines and scripts", requires Pro/Max/Team/Enterprise,
consumed via `CLAUDE_CODE_OAUTH_TOKEN`. Caveats: **`--bare` does not read this token**;
`ANTHROPIC_API_KEY` outranks it if present. Free minutes ample (2,000/mo private repos).
Known quirks: cron delays 20 min–4 h (worst at :00 — schedule off-hour), scheduled workflows
auto-disable after 60 days without commits (solved automatically because our job commits
state daily), no secrets-expiry alerts.

**Conflict resolved (token lifetime):** community reports of "~24 h OAuth expiry in CI"
([claude-code-action#727](https://github.com/anthropics/claude-code-action/issues/727),
[claude-code#42904](https://github.com/anthropics/claude-code/issues/42904)) conflict with
the official 1-year documentation. Resolution: the short-lived credentials are the
interactive `/login` session tokens (stored in `.credentials.json`, refresh-based); the
`setup-token` product is documented as one-year and separate. Treated as: **documented
1-year, empirically verify during build** (cheap: mint token, wait 48 h, run workflow).

**ToS assessment:** Consumer ToS prohibits automated access "except via an Anthropic API
key or where we otherwise explicitly permit it." Routines **is** explicit first-party
permission → zero ambiguity. The Actions+setup-token path is officially documented for CI
but unattended-cron use is un-litigated; Feb 2026 enforcement targeted third-party clients
impersonating Claude Code ([The Register](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/)),
not solo devs on the real CLI. → Routines primary; Actions fallback = low residual risk.

**Ruled out:** Cloudflare Workers / Deno Deploy / Val.town (sandboxes can't run the CLI);
Oracle Always-Free VPS (works but highest setup burden + idle-reclaim risk); API-key path
(~$1–5/mo, rejected by brief).

### Theme B — Claude Code headless: what actually works

From official docs ([headless](https://code.claude.com/docs/en/headless.md),
[cli-reference](https://code.claude.com/docs/en/cli-reference)):
- `claude -p` with `--output-format json`, `--model sonnet`, `--max-turns N`,
  `--permission-mode dontAsk` + explicit `--allowedTools "Bash,Read,Write,WebSearch"`.
- **Do not use `--bare` with the OAuth token** (not read — verified quote). Do not use the
  `claude-code-action` wrapper for the fallback: it hardcodes WebSearch/WebFetch into
  DISALLOWED_TOOLS ([action#690](https://github.com/anthropics/claude-code-action/issues/690));
  drive the CLI directly in a workflow step.
- **claude.ai connectors are NOT available under `CLAUDE_CODE_OAUTH_TOKEN`** — verified
  quote: the token "can only make model requests … can't fetch claude.ai connectors."
  Connectors exist only in the Routines/web path (Theme A). Brief assumption A6: confirmed
  for GitHub Actions, overturned (positively) for Routines.
- **WebFetch must not be load-bearing**: headless runs hit a claude.ai domain-verification
  endpoint behind Cloudflare bot protection; Anthropic closed the issue "not planned"
  ([claude-code#39896](https://github.com/anthropics/claude-code/issues/39896)). WebSearch
  has no documented plan/geo gate but has recurring backend-regression reports. → Treat both
  as optional enrichment; the pipeline must produce a full digest from pre-fetched RSS alone.
- Usage-limit hits surface as plain text in the JSON `result` field ("You've hit your …
  limit · resets …"), no distinct exit code → wrapper must string-check, and success must be
  verified by the artifact existing (digest file written / message sent), not exit code 0.
- `CLAUDE_CODE_RETRY_WATCHDOG=1` for unattended retry of 429/529; wrap with `timeout` for a
  hard deadline; only documented exit code is 143 (SIGTERM).

### Theme C — Content pipeline: RSS-first, model never types URLs

The #1 documented failure of LLM news bots is **hallucinated headlines/links**
([Nieman Lab](https://www.niemanlab.org/2024/06/chatgpt-is-hallucinating-fake-links-to-its-news-partners-biggest-investigations/),
[Ahrefs](https://ahrefs.com/blog/how-often-do-ai-assistants-hallucinate-links)). The proven
mitigation, found independently in Perplexity's digest cookbook and multiple builder repos:
**fetch first, reference by index** — a script fetches articles, hands Claude a numbered
list, Claude returns selected indices + summaries, and the script re-attaches the real URLs.
The model's job is "summarize and categorize, not generate facts."

**Dedup (two-tier, from 3+ independent builders):** (1) hard: persist sent-article URLs/
hashes in a state file committed back to the repo (doubles as the GH-Actions keepalive);
(2) soft: include recent covered headlines in the prompt with "skip pure repeats; genuine
follow-ups must state what's new." Recurring-event lookalikes (e.g. earnings) are a
reasoning problem — soft dedup handles what hashing can't.

**Feed hygiene:** HTTP 200 ≠ alive (MarketWatch MarketPulse returns valid RSS frozen since
Jul 2025) → always check newest pubDate; always send a real browser User-Agent (Yahoo 429s
without one); parse defensively (Claude sometimes prefixes JSON with prose).

### Theme D — Verified free sources (all live-fetched 2026-07-31)

- **Finance (default):** CNBC Top News + Markets RSS, Yahoo Finance RSS (fresh, 20–42
  items), Investing.com Commodities/Metals RSS (best free gold feed). Market snapshot:
  **Yahoo chart API** (keyless, browser-UA required) — returned live ^GSPC 7437.63,
  ^IXIC 25122.18, GC=F 4133.70, USDMYR=X 4.085 today. FMP/Bigdata connectors available as
  enrichment on the Routines path only.
- **Malaysia:** Free Malaysia Today, NST, Bernama, Malay Mail — all fresh, English.
- **Sports/Ent:** BBC Sport, Variety, Deadline, FMT Sports.
- **Search fallback:** Google News RSS `?q=<topic>+when:1d` (the `when:1d` is required —
  default sort is relevance, not recency). DuckDuckGo/Bing HTML are captcha-walled.
- **Dead/trap (do not use):** Reuters RSS (dead), Kitco RSS (dead), Stooq (bot-walled),
  MarketWatch MarketPulse (frozen), ESPN RSS (WAF-blocked), The Star & The Edge RSS (dead —
  substitute Google News `site:` queries).

### Theme E — Telegram mechanics (official docs, quoted)

- BotFather bot: free forever; chat_id via `getUpdates` after user messages the bot.
- **HTML parse mode** (escape only `< > &`) over MarkdownV2 (20+ escape chars) for
  LLM-generated text. `disable_web_page_preview` no longer exists → `link_preview_options:
  {"is_disabled": true}`.
- 4096-char limit counted in UTF-16 code units → split at ~3500 on whole-item boundaries,
  never mid-tag. Plain stdlib HTTPS suffices; no bot library.
- **`/topic` command reading is feasible but fragile**: `getUpdates` retains updates only
  **24 h**; a command sent during any gap in daily runs is silently lost; offset must be
  persisted across runs. → fine as a v2 convenience, config file stays source of truth (A4 ✓).
- Retry: 429 → sleep `retry_after`; 5xx → backoff ×2–3; other 4xx → fail loudly (malformed
  HTML / blocked bot won't fix themselves).

## Existing solutions & prior art

| Name | Link | Does well | Weaknesses/gaps | License/pricing |
|---|---|---|---|---|
| Di-kairos/news_vibe_coding | [GitHub](https://github.com/Di-kairos/news_vibe_coding) | **Exact pattern proof**: Claude Code CLI + OAuth token in GH Actions cron → Telegram; per-topic failure isolation; config.sh | Skill-dependent; no dedup state | OSS, free |
| chein206/ai-daily-digest | [GitHub](https://github.com/chein206/ai-daily-digest) | Best config-as-data topic profiles; 2-tier dedup (7-day URL log + soft prompt dedup); state committed by workflow | Uses Anthropic API key | OSS, free |
| azalio/azalio_tech_summary | [GitHub](https://github.com/azalio/azalio_tech_summary) | Most mature dedup (SQLite URL TTL + embeddings); yesterday-digest fed into prompt | Gemini/Codex CLI, hourly (overkill) | OSS, free |
| regrow1123/market-digest | [GitHub](https://github.com/regrow1123/market-digest) | Clean `claude -p` subprocess pattern; rules in CLAUDE.md; verifies output file exists | Self-hosted NAS, not cloud | OSS, free |
| SeanLF/claude-rss-news-digest | [GitHub](https://github.com/SeanLF/claude-rss-news-digest) | Pre-fetch RSS + opaque-ID URL referencing (anti-hallucination gold standard) | — | OSS, free |
| viochris/daily-ai-news-digest | [GitHub](https://github.com/viochris/daily-ai-news-digest) | Documents snippet-vs-scrape accuracy tradeoff; msg chunking | **No dedup at all**; hardcoded topic/persona; Gemini API key; DDG scraping | OSS, free |
| chhaewxn/Daily-News-Fetcher | [GitHub](https://github.com/chhaewxn/Daily-News-Fetcher) | Jaccard near-dup detection; config in Sheets | Apps Script triggers outside VCS; dedup resets each run; email not Telegram | OSS, free |

No large battle-tested product exists in this niche (top repo ≈19 stars) — building it
ourselves is justified; we compose the validated patterns above.

## Community insights

- **OAuth-in-CI anxiety is real but conflated** — issue threads mix `/login` session tokens
  (short-lived) with `setup-token` (1-year); no report found of account action against a solo
  dev running the official CLI on their own token. Empirical 48-h token test still mandatory.
- **March 2026 quota-burn anomaly**: Max users reported 5-h windows exhausted in minutes
  ([claude-code#38335](https://github.com/anthropics/claude-code/issues/38335), unresolved).
  A tail risk for any subscription-quota job → digest-absent = user checks `/status`.
- **Silent-death cluster**: cron drift (GitHub staff confirmed worsening mid-2026), 60-day
  disable, secret expiry with no alerts → failure notification via Telegram itself is
  near-mandatory, plus "no digest by 9 AM = something broke" as the human backstop.
- **Builders converge** on: pre-fetch + index-referencing, state-file dedup, prompt-fed
  recent-headlines, JSON-tolerant parsing, artifact-existence verification.

## Recommended direction & tech options

### Option 1 — Claude Code Routines + repo scripts (RECOMMENDED)

GitHub repo: `config/profiles/*.yaml` (topic profiles), `scripts/fetch_news.py` (RSS +
Yahoo chart API → `candidates.json` with indexed items), `scripts/send_telegram.py`
(HTML build, split, send, verify), `scripts/update_state.py` (seen-URLs log), `CLAUDE.md`
(digest rules & output schema). A **daily Routine (Sonnet, ~8:00 AM MYT)** clones the repo,
follows the runbook: fetch → select/summarize by index → send → commit state.
Environment: network **Custom** (news domains + `api.telegram.org`) or Full;
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` as environment variables. Connectors (FMP,
Bigdata.com) optionally enrich the finance profile — never load-bearing.
- Effort: low-moderate. Risk: research-preview feature may change (mitigated by Option 2 in
  the same repo). Fit: highest — sanctioned, free, PC-off, plugins usable, Sonnet-selectable.

### Option 2 — GitHub Actions + `claude setup-token` (fallback, built into same repo)

Workflow `digest.yml`, cron off-hour (e.g. `47 23 * * *` UTC ≈ 07:47 MYT), runs the same
scripts with `claude -p` directly (no action wrapper, no `--bare`), `CLAUDE_CODE_OAUTH_TOKEN`
secret. No connectors, WebSearch/WebFetch unreliable → RSS-only mode (pipeline already
supports this by design). Kept dormant (`workflow_dispatch` + disabled cron) until needed.
- Effort: low. Risk: un-litigated ToS posture for unattended cron; annual token refresh.
  Fit: good; identical repo contents makes switching a one-line change.

### Option 3 — Always-free VPS + cron (not recommended)

Full control, no preview-feature risk, but highest setup/maintenance burden, idle-reclaim
risk, and same subscription-auth questions as Option 2. Only if 1+2 both fail.

**Recommendation: build the repo once (Option 1's and Option 2's shared body), schedule via
Routines, keep the Actions workflow as the tested-but-dormant fallback.** The scripts don't
care who invoked them — scheduler choice stays a config decision, reversible in minutes.

## Risks & unknowns

| Risk | Likelihood | Mitigation |
|---|---|---|
| Routines is research-preview; behavior/limits may change | Med | Dormant Actions fallback in same repo; scripts scheduler-agnostic |
| Routine can't push state to `main` in practice | Low-Med | Verified-by-docs `claude/` branches always accepted; spike slice tests real push; fallback = state on `claude/state` branch |
| `setup-token` dies early despite 1-year docs | Low-Med | 48-h empirical test during build; annual refresh reminder in README |
| Quota anomaly bug hits mid-run | Low | Sonnet + one short run/day; digest-absence is the alarm; retry next day is acceptable per brief |
| Feeds die/freeze silently over months | High (eventually) | Freshness check per feed (newest pubDate); multi-feed redundancy per profile; Google News RSS fallback |
| Hallucinated links | Near-certain w/o mitigation | Index-referencing design makes it structurally impossible; validator rejects unknown URLs |
| Telegram message malformed (bad HTML escape) | Med | Script builds HTML (not the model); escape `< > &`; 4xx fails loudly |
| Missed run = silently lost `/topic` command (v2) | Med | v1 = config file only; v2 documents the 24-h caveat |

Unknowns research could not settle: exact Routines daily-run-cap numbers per plan tier;
`claude -p` exit codes besides 143; whether the Routines stagger keeps ~8 AM MYT within
minutes (observe during test week).

## Open questions for the user

None blocking. Two decisions default as follows unless overridden at the Phase 2 gate:
1. Routine network access starts as **Full** (simplest), tightened to Custom allowlist after
   the pipeline stabilizes.
2. The repo will be **private** (recommended: bot config + state are personal), which is
   fine within GH free tier.

## Sources

Scheduling/platform: [Routines](https://code.claude.com/docs/en/routines) · [Cloud environments](https://code.claude.com/docs/en/cloud-environments) · [Authentication](https://code.claude.com/docs/en/authentication) · [Headless](https://code.claude.com/docs/en/headless) · [CLI reference](https://code.claude.com/docs/en/cli-reference) · [MCP](https://code.claude.com/docs/en/mcp) · [Errors](https://code.claude.com/docs/en/errors) · [GitHub Actions billing](https://docs.github.com/billing/managing-billing-for-github-actions/about-billing-for-github-actions) · [Cron delay discussion](https://github.com/orgs/community/discussions/156282) · [60-day disable](https://github.com/orgs/community/discussions/86087) · [The Register on ToS enforcement](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/)
Issues: [action#727](https://github.com/anthropics/claude-code-action/issues/727) · [action#690](https://github.com/anthropics/claude-code-action/issues/690) · [action#814](https://github.com/anthropics/claude-code-action/issues/814) · [claude-code#39896](https://github.com/anthropics/claude-code/issues/39896) · [claude-code#42904](https://github.com/anthropics/claude-code/issues/42904) · [claude-code#38335](https://github.com/anthropics/claude-code/issues/38335)
Prior art: repos in table above · [Feng Liu digest-bot writeup](https://medium.com/@fengliu_367/build-your-own-ai-news-bot-automated-daily-digests-with-claude-and-github-actions-bc3d48e67d98) · [Perplexity dedup cookbook](https://docs.perplexity.ai/docs/cookbook/articles/news-dedupe-digest/README)
Quality: [Nieman Lab](https://www.niemanlab.org/2024/06/chatgpt-is-hallucinating-fake-links-to-its-news-partners-biggest-investigations/) · [Ahrefs link hallucination](https://ahrefs.com/blog/how-often-do-ai-assistants-hallucinate-links)
Telegram: [Bot API](https://core.telegram.org/bots/api) · [Bot FAQ](https://core.telegram.org/bots/faq) · [Bot features](https://core.telegram.org/bots/features)
News sources: all feed URLs listed in Theme D were fetched live 2026-07-31 by the data-sources mission.
