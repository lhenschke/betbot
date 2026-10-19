# BetBot

Places the value bets found by the [arbhunterpro](https://arbhunterpro.com) feed at soft sportsbooks:

- clearly valuable lines (`arbsize` ≥ 1 by default) are bet **automatically**;
- borderline lines (−3 to 0) go to a **one-click queue** on a local dashboard.

It starts with **Ozoon (`OZ`)**. Books are modules that can be switched on and off, and new league feeds need only a line of config.

Each PC runs its own copy with its own book account. The bot works through a **real Chrome window that you are logged into**. It logs back in by itself when needed, and behaves like a person: curved mouse movements, uneven typing, pauses, idle browsing and bet pacing.

> Start in **dry run** (the default). The bot then does everything except submit the bet, so you can watch what it would do.

## How it works

```
arbhunterpro feeds ──► poller (1s) ──► engine ──► book adapter (OZ) ──► Chrome (logged in)
                                         │              ▲
                                         ▼              │ team-name matcher (shared)
                                     dashboard ◄────────┘  SQLite ledger (bets, decisions)
```

1. **Feed.** Every URL under `feeds:` is polled every second. Rows for enabled books are put into one of three groups by `arbsize`: auto, borderline or ignored.
2. **Match.** The row is matched to the book's event using start time (±90 min) and team names, with per-league alias files in `betbot/matching/aliases/`. Abbreviations like `NWE` are checked against the opponent.
   - A doubtful match is shown as **unmatched** with the reason. It is never guessed.
   - Leagues are found automatically from the book's own league list, e.g. NHL → `/hockey/nhl`.
3. **Re-check, just before betting:**
   - fetch the event's current odds from the book;
   - the spread or total must equal the feed's line exactly;
   - the market must be open;
   - **arbsize is recomputed** from the book's current price against the feed's latest Pinnacle price, and must still meet the threshold;
   - live rows must have a Pinnacle price no older than `max_live_age_s`.
4. **Stake.** The lowest of:
   - your stake;
   - the feed's `book_limit`;
   - the book's max stake;
   - the per-event and daily caps.

   It is then rounded down to a "nice" multiple of `round_to`, and skipped if below `min_stake`.
5. **Place.** The bet is placed at the exact price that was checked, so the book rejects it if the line moved in the meantime. A price, spread or total change is re-quoted once and retried only if it still qualifies.
   - **Auto bets (`api` mode, ~1–2s):** the event is put on screen, then the same calls the site's bet slip makes are run from inside the page: suggestion → max stake → place → status poll through the live delay.
   - **One-click bets (`ui` mode, ~3–5s):** the bot clicks the outcome with a curved mouse path, types the stake, and clicks Place. If a page element can't be found before Place is clicked, it falls back to `api` mode.

### What `arbsize` means here

`arbsize` is the gap in American-odds cents between the book's price and Pinnacle's price on the other side. +106 against −108 gives −2; −105 against +109 gives +4. This reproduces every row of the feed (see `tests/test_odds.py`).

## Setup on each Windows PC

1. Install **Python 3.12 or newer** (python.org, tick "Add to PATH"; the Microsoft Store version also works), **Google Chrome**, and **Git for Windows** (<https://git-scm.com/download/win>, default options).
2. Get the code with Git, not a ZIP, so it can update itself. In a Command Prompt, in the folder you want it in:
   ```
   git clone -b claude/magical-keller-l3f7qy https://github.com/lhenschke/betbot.git
   ```
   For a private repo, a GitHub sign-in window appears the first time; sign in once and Windows remembers it.
3. Double-click **`run.bat`**. The first time, it installs everything, creates `config.yaml` and opens it.
   - Set `books.OZ.account` to a label for this PC's account, e.g. `lee-desktop`.
4. Double-click **`set-credentials.bat`** and enter the OZ email and password. They are stored in **Windows Credential Manager**, never in a file.
5. Double-click **`run.bat`** again.
   - A Chrome window opens on Ozoon, and the dashboard opens at <http://127.0.0.1:8765>.
   - The bot logs in by itself. If a captcha, 2FA code or device check appears, it pauses betting and alerts you: finish the login in the Chrome window and it carries on.
6. Leave Chrome open; minimizing it is fine. The bot doesn't use the PC's mouse or keyboard except inside that Chrome window, and only in `ui` mode.

Each PC keeps its own `config.yaml`, `data/` (ledger, logs, aliases added from the dashboard) and `profiles/` (Chrome profile), so several PCs with different accounts run independently. None of these are in Git, so updates never touch them.

### Updates

With a Git copy, every PC updates itself:

- **On start,** `run.bat` pulls the latest version and installs any new packages.
- **While running,** the bot checks GitHub every `update_check_minutes` (default 10). When there's a new version, it waits until no bet is in progress and none was placed in the last minute, then restarts into it. Chrome stays logged in across restarts.
- **On the dashboard,** the header shows "up to date" or an **Update now** button. Set `auto_update: false` to only update when you press it or restart.

So publishing a change is just pushing it to the branch the PCs track.

### First real bets (please do these in order)

Steps 1 and 2 confirm two things this code couldn't test directly: the bet request format, which was taken from Ozoon's own bet slip code, and the bet slip page elements. Ozoon is geo-blocked from the cloud machine this was built on.

1. **Capture mode.** Run `capture.bat` and place one small bet by hand in the Chrome window that opens. The bot records the bet slip requests and which elements you clicked to `data/capture-OZ.json`. Compare them with `build_bet_payload()` and `DEFAULT_SELECTORS` in `betbot/books/bodog.py`. If they differ, update those, or override the selectors with `books.OZ.ui_selectors`.
2. **Dry run.** Watch the dashboard for a while. Queued and auto rows should show the right OZ selection, and "Recent bets" should list "(dry)" entries.
3. **One real minimum bet.** Untick **Dry run**, set **Stake** to the minimum, and press **Place** on a queued row. Check it in Ozoon's "My Bets".
4. **Then auto,** with a small stake and tight caps, raising them as you gain confidence.

## Dashboard

- **Header:**
  - book status (logged in, balance, paused and why, with a **Resume** button);
  - **Auto betting** and **Dry run** switches;
  - **Stake** and the two thresholds;
  - **Stop auto** (kill switch).
- **One-click queue:**
  - borderline rows with the matched selection and the current recomputed arbsize;
  - **Place** (humanlike clicking), **Fast** (API) and **Dismiss** buttons;
  - a sound and a desktop notification when a new one arrives.
- **Feed rows:** every row for enabled books, with its category, state, and why it was skipped, unmatched or not mapped.
- **Recent bets:** stake, price, arbsize at the time of the bet, mode, result, and time from start to accepted.

The dashboard also has endpoints to fix matching without editing files:

- `POST /api/alias {"league":"NCAAF","canonical":"Texas","variant":"Texas Longhorns"}`
- `POST /api/league {"book":"OZ","feed_league":"CFL","path":"/football/cfl"}`

## Adding things

- **A league:** add its feed URL under `feeds:`. If the book names it differently, add it to `league_names` in `betbot/books/bodog_markets.yaml`, or set `books.OZ.leagues`.
- **Team spellings:** add to `betbot/matching/aliases/<LEAGUE>.yaml`. A league with no file still works through fuzzy matching.
- **Bet types or periods** (e.g. `1H ATS`): add them to `bettypes` / `periods` in `bodog_markets.yaml`. Unknown types are skipped and shown on the dashboard.
- **Bovada (`BV`):** copy the `OZ` block under `books:` as `BV` with `base_url: https://www.bovada.lv`. It's the same platform.
- **A different platform:** add `betbot/books/<name>.py` with a `BookAdapter` subclass decorated with `@register("<name>")`. It needs `resolve_league`, `events`, `quote`, `place_api` and `place_ui`; the matcher, engine and dashboard are shared.

## Commands

```
python -m betbot run                 # normal operation
python -m betbot run --no-browser    # read-only: match and quote rows, never bets
python -m betbot check               # one pass: how every feed row matches at each enabled book
python -m betbot set-credentials OZ
python -m betbot capture OZ
python -m pytest                     # tests (pip install -e .[dev])
```

## Safety

- **Pre-bet checks:** dry run is on by default. Every bet is re-checked first, and selections already bet are never bet again on the same account.
- **Limits and pauses:**
  - per-event and daily caps;
  - per-minute and per-hour limits;
  - automatic pause after repeated rejections or when logged out;
  - at most 3 automatic login attempts per hour, so the account doesn't get locked;
  - a kill switch.
- **Logging:** every decision and bet is logged in `data/betbot.sqlite3`, plus `data/betbot.log`.

Sportsbooks generally prohibit automated betting and limit winning players. The humanlike behaviour lowers that risk but does not remove it.
