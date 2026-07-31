# 00 — BRIEF: Daily AI News Bot

Date: 2026-07-31 · Phase 0 confirmed by user ("okay looks good. lets cook")

## Goal

A fully-automated daily news digest system. Every morning the user receives one short,
straight-to-the-point Telegram message summarizing important news for the currently
selected topic profile. Claude (via the user's subscription, **not** a paid LLM API key)
does the intelligence: searching, crawling, filtering, and summarizing.

## User

Single user (the owner). Malaysia-based (MYT, UTC+8). Telegram is the delivery channel.

## Core requirements (must-have)

1. **Daily digest to Telegram** — one concise message per day; headline → 1–2 line
   "why it matters" → source link per item.
2. **Claude-powered, no LLM API key** — intelligence comes from Claude Code running under
   the user's subscription (Max now, possibly Pro later). Daily job should run on a cheap
   model (Sonnet) — prompts must be designed for it.
3. **Fully cloud-scheduled** — must run with the user's PC off. No Windows Task Scheduler,
   no terminal left running. Candidates to verify in RND: GitHub Actions with Claude
   subscription OAuth (`claude setup-token`), Claude Code scheduled cloud agents/routines.
4. **Switchable topic profiles via config** — default profile: US stocks, Gold, and global
   news that affects stocks & metals. Other profiles (entertainment, sports, Malaysia +
   global politics, …) selectable by changing a config value. Adding a new profile should
   not require code changes.
5. **Free to run** — free tiers only (GitHub Actions free tier, Telegram Bot API,
   free news sources/RSS). The Telegram bot token (BotFather) is acceptable — it is a
   free messaging credential, not an LLM API key.

## Nice-to-have (later, not v1)

- Switch topic profile by sending a Telegram command (e.g. `/topic sports`), picked up
  on the next scheduled run.
- Market snapshot enrichment beyond the basics.

## Confirmed configuration

| Setting | Value |
|---|---|
| Gate mode | **Full auto** — Approver subagent reviews each phase doc; user notified, not blocked. Phase 4 still ends with the user. |
| Phase 3 execution style | **Sequential** — one slice fully built + tested before the next. |
| Claude plan | Max during development; possibly Pro later. Daily job targets **Sonnet**. |
| Scheduling | **Fully online/cloud** — must not depend on the user's PC. |

## Working assumptions (user approved defaults; each is cheap to change)

- **A1** Delivery ~8:00 AM MYT, every day incl. weekends (weekend edition may be lighter).
- **A2** Telegram bot created via @BotFather during the build; token stored as a secret.
- **A3** 5–8 news items per digest, English, single message (split only if over Telegram's
  4096-char limit).
- **A4** Topic switching v1 = edit config file in the repo; Telegram-command switching is
  a later nice-to-have.
- **A5** Default finance profile includes a short market snapshot (US indices, gold,
  USD/MYR) **if** a free data path is reachable from the headless run; otherwise news only.
- **A6** claude.ai-connected plugins (Bigdata.com, FMP) may be unreachable from headless/
  cloud runs — RND must verify; free fallbacks (RSS, Yahoo Finance, web search) required.

## Success criteria

Every morning at the set time, a short, accurate, non-repetitive digest lands in the
user's Telegram without any manual step. The user can switch topic profiles themself.
A missed day should be visible (failure notification), not silent. The same story should
not be repeated across consecutive days.

## Out of scope (v1)

- Multi-user support, web UI, databases beyond a simple seen-stories state file,
  paid data sources, non-Telegram channels.

## Reference material from user

- https://github.com/viochris/daily-ai-news-digest (liked the shape; uses LLM API key — rejected pattern)
- https://github.com/chhaewxn/Daily-News-Fetcher
- GitHub search keywords: news-summarization, ai news bot, telegram news digest
