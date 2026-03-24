# bioRxiv Human Evolutionary Genomics Monitor

This project monitors [bioRxiv](https://www.biorxiv.org) for new preprints related to human evolutionary genomics and writes a Markdown report summarizing each paper it has not already summarized.

The script is designed to avoid the common first-run problem where a monitor accidentally backfills the entire archive. By default, it only considers papers posted within the last 7 days, and it stores a persistent record of summarized papers so the same paper is not summarized twice.

The current implementation pulls recent paper metadata from the official bioRxiv API and then applies the configured query locally against each paper's title, abstract, and category.

## What The Script Does

On each run, the script:

1. Searches bioRxiv for papers matching a human evolutionary genomics query.
2. Fetches recent paper metadata and abstracts from the bioRxiv API.
3. Filters out papers that:
   - were already summarized in a previous run, or
   - were posted outside the configured publication window.
4. Summarizes the remaining papers.
5. Writes a dated Markdown report.
6. Updates a local JSON state file with the papers summarized in that run.

## Current Default Behavior

Out of the box, the script behaves like this:

- Search topic: human evolutionary genomics / ancient DNA / paleogenomics / archaeogenetics
- Search query override: available via `--search-query`
- Publication window: last 7 days
- Deduplication: enabled using a local JSON state file
- Summary mode:
  - If 10 or fewer new papers are found and `OPENAI_API_KEY` is set, use OpenAI summaries
  - If more than 10 new papers are found, use extractive summaries for all of them
  - If `OPENAI_API_KEY` is not set, use extractive summaries
  - If OpenAI summarization fails during a run, the script falls back to extractive summaries instead of crashing
- Output format: Markdown report in the `reports/` directory

If you want a machine-specific version of these instructions with local absolute paths, see `README_local.md`.

## Files

- Script: [`biorxiv_human_evo_monitor.py`](biorxiv_human_evo_monitor.py)
- Conda environment: [`environment.yml`](environment.yml)
- Optional pip requirements: [`requirements.txt`](requirements.txt)
- State file default path: `data/summarized_biorxiv_papers.json`
- Report output directory default: `reports/`

## Installation With Conda

Create the environment:

```bash
conda env create -f environment.yml
```

Activate it:

```bash
conda activate biorxiv-monitor
```

If you prefer to update the environment after editing `environment.yml`:

```bash
conda env update -f environment.yml --prune
```

## OpenAI API Key

If you want AI-generated summaries, set your API key in `zsh`:

```bash
export OPENAI_API_KEY="your_api_key_here"
```

You can also optionally choose the model:

```bash
export OPENAI_MODEL="gpt-4.1-mini"
```

If `OPENAI_API_KEY` is not set, the script still works and falls back to extractive abstract summaries.

If the API key is set but OpenAI returns a quota, billing, rate-limit, or similar request error, the script now falls back to extractive summaries for that run.

## Basic Usage

Run once immediately:

```bash
python3 biorxiv_human_evo_monitor.py --run-once
```

Run once with a custom AI-summary cap:

```bash
python3 biorxiv_human_evo_monitor.py --run-once --max-ai-summary-papers 5
```

Run once with a custom publication window:

```bash
python3 biorxiv_human_evo_monitor.py --run-once --publication-window-days 14
```

Run once with a custom bioRxiv search query:

```bash
python3 biorxiv_human_evo_monitor.py --run-once --search-query '"archaic introgression" OR Neanderthal OR Denisovan'
```

Run once with custom output and state paths:

```bash
python3 biorxiv_human_evo_monitor.py \
  --run-once \
  --output-dir custom_reports \
  --state-file custom_data/summarized.json
```

Run once with debug filter counts:

```bash
python3 biorxiv_human_evo_monitor.py --run-once --debug-counts
```

## Daily Scheduling

The script supports an internal daily scheduler:

```bash
python3 biorxiv_human_evo_monitor.py --schedule 06:00
```

That process stays alive and runs the monitor each day at 6:00 AM local time.

For long-term reliability, it is usually better to use an OS-level scheduler such as `launchd` on macOS, `cron`, or a workflow manager, and invoke the script with `--run-once`.

Example scheduled command:

```bash
python3 biorxiv_human_evo_monitor.py --run-once
```

## Command-Line Options

### `--run-once`

Runs immediately and exits.

### `--schedule HH:MM`

Runs every day at the specified local time in 24-hour format.

Example:

```bash
--schedule 06:00
```

### `--publication-window-days N`

Only summarizes papers posted within the last `N` days.

Default:

```bash
7
```

This is the main protection against summarizing the full historical archive on the first run.

### `--search-query QUERY`

Overrides the default search string used to match recent bioRxiv papers.

Default:

```bash
"human evolutionary genomics" OR "ancient DNA" OR paleogenomics OR archaeogenetics
```

Example:

```bash
--search-query '"archaic introgression" OR Neanderthal OR Denisovan'
```

This is useful if you want to monitor a narrower topic, broaden the scope, or reuse the script for a related domain.

Current behavior:

- The script splits the query on `OR`
- Each term or quoted phrase is matched against the paper title, abstract, and category
- This is intentionally simple and works well with queries like the default one

### `--limit-days N`

Limits how many recent days of bioRxiv search results to inspect.

Default:

```bash
7
```

In most cases this should be kept at least as large as `--publication-window-days`. If `--limit-days` is smaller than the publication window, some eligible recent papers may never be fetched from search results.

Practical rule:

- Keep `--limit-days >= --publication-window-days`

### `--max-ai-summary-papers N`

Uses OpenAI summaries only if the number of newly eligible papers is less than or equal to `N`.

Default:

```bash
10
```

If more than `N` new papers are found, the script switches the entire run to extractive summaries.

### `--state-file PATH`

Path to the JSON file that stores which papers have already been summarized.

Default:

```bash
data/summarized_biorxiv_papers.json
```

### `--debug-counts`

Prints internal filter counts to stdout and includes them in the generated report.

This is useful when diagnosing discrepancies between:

- the number of papers returned by the script
- the number of papers returned by a manual bioRxiv website search

The debug section reports counts for:

- API records seen
- records with enough metadata to evaluate
- query matches before DOI deduplication
- candidate papers after DOI deduplication
- papers skipped because they were already summarized
- papers skipped because they were outside the publication window
- final papers summarized

### `--output-dir PATH`

Directory where Markdown reports are written.

Default:

```bash
reports
```

## How Deduplication Works

The script stores summarized papers in a JSON file. Each paper is keyed by DOI and includes metadata such as:

- title
- url
- posted date
- summarized timestamp

If a paper’s DOI is already present in the state file, the script skips it even if it still falls within the publication window.

This means:

- A paper is summarized at most once per state file
- Re-running the script on the same day is safe
- The first run will only summarize recent papers, not the full archive

The script also includes backward compatibility for an older state-file format containing only a `seen_ids` list.

## Report Format

Each run writes a Markdown file named like:

```text
reports/biorxiv-human-evo-YYYY-MM-DD.md
```

Each report includes:

- the search query
- the publication window used
- the summary mode used (`ai`, `extractive`, or an explicit OpenAI fallback message)
- the AI-summary cap used
- the number of new papers found
- one section per paper with metadata, summary, and abstract

## Summary Modes

### AI summaries

AI summaries are used only when both of the following are true:

- `OPENAI_API_KEY` is set
- the number of new papers in the run is less than or equal to `--max-ai-summary-papers`

### Extractive summaries

Extractive summaries are used when:

- there are more new papers than the configured AI cap, or
- the OpenAI package is unavailable, or
- `OPENAI_API_KEY` is not set, or
- OpenAI summarization fails during the run

The extractive summarizer scores sentences in the abstract and returns a short bullet list based on the most relevant ones.

## Example Workflows

### Typical first run

This only summarizes papers from the last week and records them in the state file:

```bash
python3 biorxiv_human_evo_monitor.py --run-once
```

### Conservative low-cost run

This disables most AI use by setting a low cap:

```bash
python3 biorxiv_human_evo_monitor.py --run-once --max-ai-summary-papers 3
```

### Wider publication window

If you want to catch papers from the last two weeks:

```bash
python3 biorxiv_human_evo_monitor.py --run-once --publication-window-days 14 --limit-days 14
```

### Different scientific focus

If you want to reuse the script for a different search theme:

```bash
python3 biorxiv_human_evo_monitor.py \
  --run-once \
  --search-query '"human population genetics" OR admixture OR demography'
```

### Diagnose count discrepancies

If the website search and script results differ a lot:

```bash
python3 biorxiv_human_evo_monitor.py \
  --run-once \
  --debug-counts \
  --publication-window-days 7 \
  --limit-days 7
```

Then compare the debug counts in stdout or the report to see whether the drop happens at:

- API retrieval
- local query matching
- DOI deduplication
- publication-window filtering
- prior-summary filtering

### Dedicated output location

```bash
python3 biorxiv_human_evo_monitor.py \
  --run-once \
  --output-dir reports \
  --state-file data/summarized_biorxiv_papers.json
```

## Notes And Caveats

- The script depends on the current bioRxiv API response format. If bioRxiv changes that API, the script may need to be updated.
- Date parsing is based on common metadata formats from bioRxiv pages. If a page exposes an unexpected date format, that paper may be skipped.
- Papers without a parseable date are treated as not eligible for summarization.
- The query is intentionally broad enough to catch human evolutionary genomics and ancient DNA work, but you may want to tune it over time.
- The internal scheduler is simple and useful, but OS-level scheduling is more robust for unattended operation.

## Troubleshooting

### No papers found

Possible causes:

- no relevant papers were posted in the publication window
- papers were already summarized in a prior run
- the search query is too narrow
- bioRxiv page structure changed
- the bioRxiv API changed or was temporarily unavailable

### The report is empty

This can happen if:

- all recent matching papers were already present in the state file, or
- all matching papers were outside the publication window

### AI summaries are not being used

Check:

- `OPENAI_API_KEY` is set
- the `openai` package is installed in the active environment
- the number of new papers is at or below `--max-ai-summary-papers`

If those are set correctly but your account has insufficient quota or billing is not active, the script should now continue and use extractive summaries instead.

In that case, the report will also make the fallback explicit in its `Summary mode` line.

### I want to re-summarize papers

Delete or edit the state file:

- `data/summarized_biorxiv_papers.json`

If you remove a DOI from that file, the script can summarize it again on a future run as long as it still falls within the publication window.
