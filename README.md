# seattle_nws_summary

Archives every **Area Forecast Discussion** issued by the National Weather Service in Seattle
and explains it in plain language, along with summaries of how the forecast changed over the
past week and the past month.

### 👉 [steinbrennerlab.github.io/seattle_nws_summary](https://steinbrennerlab.github.io/seattle_nws_summary/)

The page updates once a day and always shows when it last checked, so a stale date means
something is broken. A failed check is reported on the page itself.

## What it does

| Step | Source / output |
|---|---|
| Daily fetch | `api.weather.gov/products/types/AFD/locations/SEW`, the official NWS API |
| Backfill and fallback | Iowa Environmental Mesonet AFOS archive (`AFDSEW`) |
| Archive | `docs/archive/YYYY/MM/AFDSEW_YYYYMMDDHHMM.txt`, one file per discussion, UTC in the name |
| Summaries | Claude, in three levels (below), cached as JSON in `docs/data/` |
| Page | `docs/index.html` and `docs/archive.html`, served by GitHub Pages |

The archive sits inside `docs/`, so every saved discussion has a public URL and the page links
straight to it. The same discussion fetched from both sources is saved once: products are
compared with whitespace collapsed.

### Summaries

```
each discussion  ->  each Pacific day  ->  past 7 days
                                       ->  past 30 days
```

Each discussion is explained once and never redone. A day is summarized from its
discussions' explanations, and redone only when that day gains a discussion. The week and
month summaries are built from day summaries, so no request ever holds a month of raw text.
A typical day makes about six requests.

The week and month summaries describe the **forecasting**: what forecasters expected, when
they changed their minds, and what held steady. They do not describe observed weather.

Aviation and marine sections are passed to the model last, labeled as lower priority, and the
instructions tell it to mention them only when they matter to the general public.

## Setup

1. Create the repository `steinbrennerlab/seattle_nws_summary` and push this code.
2. **Settings → Pages**: deploy from branch `main`, folder `/docs`.
3. **Settings → Actions → General → Workflow permissions**: make sure workflows can write
   (the workflow requests `contents: write`, but an organization setting can override it).
4. **Actions → Daily update → Run workflow**, with *backfill_days* set to `35`. This loads the
   past month from the Iowa archive, then runs a normal update.
5. Later, add the API key: **Settings → Secrets and variables → Actions → New repository
   secret**, named `ANTHROPIC_API_KEY`. The next run summarizes every archived discussion from
   the past 35 days that does not yet have a summary.

Until the key is added, the page lists and links the archived discussions and says the
explanations have not been generated yet.

Explanations are written in batches (10 discussions by default; set *batch_size* when running
the workflow by hand). Each batch is committed and pushed before the next starts, so a long
catch-up can be followed in three places: the run's summary page in Actions lists every batch,
the commit history shows one commit per batch, and the site shows "N of M discussions" until
all are done. If a run hits its time limit, only the batch in progress is lost, and the next run
continues from there. Day summaries wait until all of that day's discussions are explained,
and the week and month summaries wait until everything is.

To use a different model, set `AFD_MODEL` in the workflow's `env`.

## Local use

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m afd backfill --days 35   # load the past month
.venv/bin/python -m afd update               # fetch new, summarize (if ANTHROPIC_API_KEY is set), rebuild
.venv/bin/python -m afd summarize --batch 10  # explain up to 10 discussions; exit code 3 = more waiting
.venv/bin/python -m afd render               # rebuild the page only
.venv/bin/python -m pytest                   # offline tests with a fake model
```

## Layout

```
afd/
  config.py      office, time zone, model, summary windows
  fetch.py       NWS API and IEM requests, with retries
  parse.py       split products, read issuance time, split sections
  store.py       archive files and deduplication
  summarize.py   prompts and the three summary levels
  render.py      builds docs/ from the archive and summaries
  templates/     page templates and stylesheet
docs/            the published site (generated; committed)
tests/           parsing, storage, caching, and rendering tests
```
