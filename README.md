# Milton A fixtures calendar

A calendar feed of Milton A's fixtures that rebuilds itself from
[FA Full-Time](https://fulltime.thefa.com/displayTeam.html?id=695509665) every day.

**Subscribe:** `webcal://bizley92.github.io/milton-a-calendar/fixtures.ics`

On iPhone: Settings → Calendar → Accounts → Add Account → Other → Add Subscribed Calendar,
paste the link, then turn **Remove Alerts** off so the reminders come through.

GitHub Pages must be set to deploy from the `main` branch, `/docs` folder.

## What's in each event

- Title with opponent, home/away, competition (for cups) and kick-off time
- Starts at the meet-up time (1 hour before kick-off), ends 2 hours after kick-off
- Location as ground name and full address, with a map pin so it opens in Maps
- Pitch number in the notes
- Alerts the day before and 1 hour before the meet-up

## How it updates

`.github/workflows/update-calendar.yml` runs every morning (and on demand from the
Actions tab → *Update fixtures calendar* → *Run workflow*). It opens the Full-Time team page
in a headless browser, reads **Upcoming Fixtures**, and merges it with `docs/fixtures.json`:

- moved games keep the same event, so they move in your calendar rather than duplicating
- new fixtures (e.g. the next cup round) are added
- games withdrawn from Full-Time are removed
- games already played stay in the calendar

If Full-Time can't be read, the run fails without touching the calendar and GitHub emails you.

## Settings

- Meet-up time, event length and alerts: constants at the top of `build_calendar.py`
- Ground addresses and map pins: `venues.json` (key is the Full-Time venue name in capitals,
  without the pitch number). Grounds that aren't listed show the Full-Time name with no map pin;
  the run log lists them.

## Run locally

```sh
pip install -r requirements.txt
python -m playwright install chromium
python build_calendar.py                    # fetch from Full-Time
python build_calendar.py --from-text FILE   # or parse a pasted Upcoming Fixtures list
```
