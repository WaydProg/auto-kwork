# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram bot (aiogram 3, Python 3.14) that monitors new projects on kwork.ru with Selenium (undetected-chromedriver), notifies admins in Telegram with inline buttons (open / respond / ignore), and submits responses ("отклики") through the browser. Code comments, bot messages and README are in Russian. See README.md for setup, `.env` options and anti-blocking notes.

## Commands

No tests or linter are configured.

```bash
source .venv/bin/activate
pip install -r requirements.txt
python bot.py              # run the bot
python parser.py           # standalone smoke test: print projects from page 1 (no Telegram)
python parser.py --login   # open Chrome with the bot's profile to log in to Kwork manually
```

Useful env overrides for local experiments: `HEADLESS=1`, `CHROME_PROFILE_DIR=<tmp dir>`, `DB_PATH=<tmp db>`. Setting `email= password=` in the environment disables auto-login (dotenv doesn't override existing env vars).

## Architecture

- **Data source:** Kwork embeds the project list as JSON in `window.stateData` (`wantsListData.wants` plus `pagination.last_page`). `parser.KworkBrowser.fetch_page` reads that JSON via `execute_script` instead of scraping card markup. `_normalize` maps a raw "want" to the project dict used everywhere (`id, title, description, price, url, files`).
- **Threading model:** all Selenium calls go through `Monitor.run_in_browser`, which runs them on a single-worker `ThreadPoolExecutor`. This serializes the parse loop and response submission on one driver. Never call `KworkBrowser` methods directly from async code.
- **Lifecycle:** `Monitor.start/stop` manage one `asyncio.Task` guarded by an `asyncio.Lock`. `stop()` cancels the task and then queues `browser.close` on the executor, so the close runs after any in-flight Selenium call. The driver is created lazily, so `send_offer` works even when monitoring is stopped (handlers close the browser afterwards in that case).
- **Dedup/status flow (`database.py`, raw aiosqlite):** a project is inserted as `new` before notifying, and `mark_sent` sets `sent` and stores Telegram `message_ids` (used by the ignore handler to delete file and text messages). Anything not `new` is skipped. Pagination stops at the first page with no fresh projects.
- **DI:** `bot.py` puts the `Monitor` in `dp['monitor']`, and aiogram injects it into handlers as the `monitor` argument. The router is restricted to `config.LIST_ADMINS_ID`.
- **Response FSM:** `respond_{id}` callback → `RespondState.waiting_text` → `KworkBrowser.send_offer`. Optional `Цена: N` / `Срок: N` lines in the text override price and days (`handlers._extract_overrides`).

## Gotchas

- uc misdetects Chrome's version and then fails with `session not created`. `_detect_chrome_major()` passes `version_main` explicitly (can be overridden with `CHROME_VERSION_MAIN`).
- Attachment URLs 302-redirect to an HTML page unless the user is logged in. `_download_files` disables redirects and rejects HTML, so failed files fall back to links in the message.
- The `send_offer` form selectors (textarea / price / duration / submit button matched by text) were written without access to a logged-in offer page and are unverified. On failure a screenshot is saved to `logs/`.
- Telegram command names can't contain `-`, so `/start_work` is the canonical form. Hyphenated variants are also matched.
