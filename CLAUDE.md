# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram bot (aiogram 3, Python 3.14) that monitors new projects on kwork.ru with Selenium (undetected-chromedriver), notifies admins in Telegram with inline buttons (open / manual respond / AI respond / ignore), generates response drafts via OpenRouter (`ai_service.py`), and submits responses ("отклики") through the browser. Code comments, bot messages and README are in Russian. See README.md for setup, `.env` options and anti-blocking notes.

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
- **DI:** `bot.py` puts `Monitor` and `AIService` in `dp['monitor']` / `dp['ai']`, and aiogram injects them into handlers by argument name. The router is restricted to `config.LIST_ADMINS_ID`.
- **AI responses:** `ai_service.AIService` (httpx) calls OpenRouter with a `models` fallback list and retries 429/5xx and error-in-200 bodies. `AIServiceError` messages are user-facing (Russian). Callbacks: `ai_respond_`, `regenerate_ai_`, `confirm_ai_`, `edit_ai_` (FSM `RespondState.editing_ai_draft`), `cancel_ai_`. Preview buttons `title_ai_`/`resettitle_ai_` (FSM `editing_title`), `price_ai_`/`setprice_ai_{id}_{n}` (FSM `RespondState.editing_price` for typed input) and `days_ai_`/`setdays_ai_{id}_{n}` rewrite `Цена:`/`Срок:`/`Название:` lines in the draft via `offer.set_param` and re-render the preview in place; `back_ai_` restores the keyboard. Param lines survive regeneration (`offer.param_lines`). The current draft lives in the `projects.draft` column, not FSM data, so it survives restarts and `state.clear()`. `init_db` adds the column to older databases via `PRAGMA table_info`.
- **Submitting:** both the manual and AI paths go through `handlers._submit_offer` → `offer.parse_offer` (strips the `Цена:/Срок:/Название:/Оплата:/Задача:` lines, applies defaults, validates Kwork limits and raises a user-facing `OfferError`) → `KworkBrowser.send_offer(project_id, Offer)`. `_submitting` guards against double submits; on success the status becomes `responded` and the project card keyboard is replaced.
- **Offer form (`/new_offer?project=ID`) fill order matters:** description → `#offer-custom-price` (typed key by key) → the payment block `.offer-payment-type__item` appears → pick [0] full / [1] stages → then the order title. The payment block only renders when the typed price is at least the project's `getPriceThreshold` (stored as `projects.price_threshold`, usually 4000). Below it the title field shows right away, so `_wait_payment_or_title` waits for whichever appears. `Offer.has_payment_choice` mirrors this for the preview and validation. Explicit stages below the threshold raise `OfferError`, while a config-default of stages silently falls back to full. After that comes the order title (a trumbowyg textarea with placeholder «Введите название заказа») or `.stages__stage` rows (`textarea[name^=stageTitle]` + `.stages__stage-price-input`; 3 rows by default, extras deleted via `.stages__stage-controls-delete`) → duration `.v-select.duration-select` (vue-select, fixed options). Rich-text fields are trumbowyg: write through `_SET_TRUMBOWYG_JS` (editor div plus hidden textarea). The hidden textarea is not `is_displayed()`, so wait for presence. `python parser.py --dry-run-offer ID [file]` fills the form without submitting.

## Gotchas

- uc misdetects Chrome's version and then fails with `session not created`. `_detect_chrome_major()` passes `version_main` explicitly (can be overridden with `CHROME_VERSION_MAIN`).
- Attachment URLs 302-redirect to an HTML page unless the user is logged in. `_download_files` disables redirects and rejects HTML, so failed files fall back to links in the message.
- Form filling was verified with dry runs against the real logged-in form. The final «Предложить» click and the success detection (URL change or a confirmation text) are not verified. On failure a screenshot is saved to `logs/`.
- vue-select option `.text` is empty for items outside the menu's visible area, so read `textContent` instead.
- Telegram command names can't contain `-`, so `/start_work` is the canonical form. Hyphenated variants are also matched.
