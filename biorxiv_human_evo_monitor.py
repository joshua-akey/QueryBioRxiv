#!/usr/bin/env python3
"""Monitor bioRxiv for new human evolutionary genomics papers."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


SEARCH_QUERY = '"human evolutionary genomics" OR "ancient DNA" OR paleogenomics OR archaeogenetics'
BASE_URL = "https://www.biorxiv.org"
API_URL = "https://api.biorxiv.org/details/biorxiv"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
SUMMARY_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_AI_SUMMARY_PAPERS = 10
DEFAULT_PUBLICATION_WINDOW_DAYS = 7


@dataclass
class Paper:
    title: str
    authors: list[str]
    url: str
    doi: str
    posted_date: str
    abstract: str
    category: str = ""


@dataclass
class DebugStats:
    api_records_seen: int = 0
    api_records_with_minimal_fields: int = 0
    query_matched_before_dedup: int = 0
    candidate_papers_after_dedup: int = 0
    skipped_already_summarized: int = 0
    skipped_outside_publication_window: int = 0
    final_new_papers: int = 0


def api_detail_url(start_date: dt.date, end_date: dt.date, cursor: int) -> str:
    return f"{API_URL}/{start_date.isoformat()}/{end_date.isoformat()}/{cursor}/json"


def require_requests() -> None:
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required to run the monitor. "
            "Install dependencies from environment.yml or requirements.txt first."
        )


def build_paper_url(doi: str, version: str) -> str:
    version_suffix = f"v{version}" if version else ""
    return f"{BASE_URL}/content/{doi}{version_suffix}"


def query_terms(query: str) -> list[str]:
    parts = re.split(r"\s+OR\s+", query, flags=re.IGNORECASE)
    terms: list[str] = []
    for part in parts:
        cleaned = part.strip().strip("()").strip()
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]
        cleaned = clean_text(cleaned)
        if cleaned:
            terms.append(cleaned.lower())
    return terms


def matches_search_query(paper: Paper, query: str) -> bool:
    haystack = " ".join([paper.title, paper.abstract, paper.category]).lower()
    terms = query_terms(query)
    if not terms:
        return True
    return any(term in haystack for term in terms)


def fetch_candidate_papers(
    session: requests.Session,
    query: str,
    limit_days: int,
    debug_stats: DebugStats | None = None,
) -> list[Paper]:
    require_requests()
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=limit_days)
    cursor = 0
    papers: list[Paper] = []

    while True:
        response = session.get(api_detail_url(start_date, end_date, cursor), timeout=30)
        response.raise_for_status()
        payload = response.json()
        records = payload.get("collection", [])
        if not records:
            break

        for record in records:
            if debug_stats is not None:
                debug_stats.api_records_seen += 1
            paper = Paper(
                title=clean_text(record.get("title", "")),
                authors=parse_api_authors(record.get("authors", "")),
                url=build_paper_url(record.get("doi", ""), str(record.get("version", "")).strip()),
                doi=clean_text(record.get("doi", "")),
                posted_date=clean_text(record.get("date", "")),
                abstract=clean_text(record.get("abstract", "")),
                category=clean_text(record.get("category", "")),
            )
            if paper.doi and paper.title and paper.abstract:
                if debug_stats is not None:
                    debug_stats.api_records_with_minimal_fields += 1
                if matches_search_query(paper, query):
                    if debug_stats is not None:
                        debug_stats.query_matched_before_dedup += 1
                    papers.append(paper)

        cursor += len(records)
        total_items = parse_total_items(payload)
        if total_items is not None and cursor >= total_items:
            break

    deduped: dict[str, Paper] = {}
    for paper in papers:
        deduped[paper.doi] = paper
    if debug_stats is not None:
        debug_stats.candidate_papers_after_dedup = len(deduped)
    return list(deduped.values())


def parse_total_items(payload: dict) -> int | None:
    messages = payload.get("messages", [])
    if not messages:
        return None
    total = messages[0].get("total")
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def parse_api_authors(value: str) -> list[str]:
    if not value:
        return []
    authors = [clean_text(author) for author in value.split(";")]
    return [author for author in authors if author]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def load_summarized_papers(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if "summarized_papers" in data:
        return data["summarized_papers"]
    # Backward compatibility with the older DOI-only state file.
    return {doi: {} for doi in data.get("seen_ids", [])}


def save_summarized_papers(path: Path, summarized_papers: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summarized_papers": summarized_papers}, indent=2, sort_keys=True))


def parse_posted_date(value: str) -> dt.date | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"^posted\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.split(";")[0].strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def is_recent_paper(paper: Paper, publication_window_days: int) -> bool:
    posted_date = parse_posted_date(paper.posted_date)
    if posted_date is None:
        return False
    age = (dt.date.today() - posted_date).days
    return 0 <= age <= publication_window_days


def should_use_ai_summaries(new_papers: list[Paper], max_ai_summary_papers: int) -> bool:
    return (
        len(new_papers) <= max_ai_summary_papers
        and OpenAI is not None
        and bool(os.getenv("OPENAI_API_KEY"))
    )


def summarize_papers(papers: list[Paper], max_ai_summary_papers: int) -> tuple[dict[str, str], str]:
    use_ai = should_use_ai_summaries(papers, max_ai_summary_papers)
    mode = "ai" if use_ai else "extractive"
    summaries: dict[str, str] = {}
    if not use_ai:
        for paper in papers:
            summaries[paper.doi] = summarize_extractively(paper.abstract)
        return summaries, mode

    try:
        for paper in papers:
            summaries[paper.doi] = summarize_with_openai(paper)
        return summaries, mode
    except Exception as exc:
        fallback_reason = clean_text(str(exc)) or exc.__class__.__name__
        print(
            "OpenAI summarization failed; falling back to extractive summaries. "
            f"Reason: {fallback_reason}",
            file=sys.stderr,
        )
        summaries = {paper.doi: summarize_extractively(paper.abstract) for paper in papers}
        return summaries, f"extractive (OpenAI fallback: {fallback_reason})"


def summarize_with_openai(paper: Paper) -> str:
    client = OpenAI()
    prompt = textwrap.dedent(
        f"""
        Summarize this bioRxiv preprint in 4-6 bullet points for a scientist tracking
        human evolutionary genomics. Focus on dataset, method, population/time period,
        and main findings. If a point is unclear from the abstract, do not guess.

        Title: {paper.title}
        Authors: {", ".join(paper.authors[:12])}
        Posted: {paper.posted_date}
        DOI: {paper.doi}
        Abstract:
        {paper.abstract}
        """
    ).strip()
    response = client.responses.create(model=SUMMARY_MODEL, input=prompt)
    return response.output_text.strip()


def summarize_extractively(abstract: str) -> str:
    sentences = [
        fragment.strip()
        for fragment in re.split(r"(?<=[.!?])\s+", abstract)
        if fragment.strip()
    ]
    if not sentences:
        return "- No abstract available."

    priorities = [
        r"\b(we show|we found|our results|results indicate|demonstrate|reveal)\b",
        r"\b(genome|DNA|ancestry|selection|admixture|population|migration|Neanderthal|Denisovan)\b",
        r"\b(method|approach|dataset|sample|sequenc|ancient)\b",
    ]
    scored: list[tuple[int, str]] = []
    for sentence in sentences:
        score = 0
        lower = sentence.lower()
        for idx, pattern in enumerate(priorities):
            if re.search(pattern, lower):
                score += 3 - idx
        score += min(len(sentence) // 80, 2)
        scored.append((score, sentence))

    selected: list[str] = []
    for _, sentence in sorted(scored, key=lambda item: item[0], reverse=True):
        if sentence not in selected:
            selected.append(sentence)
        if len(selected) == 4:
            break

    return "\n".join(f"- {sentence}" for sentence in selected)


def write_report(
    path: Path,
    papers: list[Paper],
    summaries: dict[str, str],
    summary_mode: str,
    max_ai_summary_papers: int,
    publication_window_days: int,
    search_query: str,
    debug_stats: DebugStats | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# bioRxiv Human Evolutionary Genomics Monitor ({day})",
        "",
        f"Query: `{search_query}`",
        f"Publication window: last `{publication_window_days}` days",
        f"Summary mode: `{summary_mode}`",
        f"AI summary cap: `{max_ai_summary_papers}` papers",
        "",
        f"New papers found: {len(papers)}",
        "",
    ]
    if debug_stats is not None:
        lines.extend([
            "## Debug Counts",
            "",
            f"- API records seen: {debug_stats.api_records_seen}",
            f"- API records with DOI/title/abstract: {debug_stats.api_records_with_minimal_fields}",
            f"- Query matches before DOI deduplication: {debug_stats.query_matched_before_dedup}",
            f"- Candidate papers after DOI deduplication: {debug_stats.candidate_papers_after_dedup}",
            f"- Skipped as already summarized: {debug_stats.skipped_already_summarized}",
            f"- Skipped outside publication window: {debug_stats.skipped_outside_publication_window}",
            f"- Final new papers summarized: {debug_stats.final_new_papers}",
            "",
        ])
    for paper in papers:
        lines.extend([
            f"## {paper.title}",
            "",
            f"- Authors: {', '.join(paper.authors[:20]) or 'Unknown'}",
            f"- Posted: {paper.posted_date or 'Unknown'}",
            f"- DOI: {paper.doi or 'Unknown'}",
            f"- URL: {paper.url}",
            "",
            "### Main Findings",
            "",
            summaries[paper.doi],
            "",
            "### Abstract",
            "",
            textwrap.fill(paper.abstract, width=100),
            "",
        ])
    path.write_text("\n".join(lines))


def run_once(
    state_path: Path,
    output_dir: Path,
    limit_days: int,
    max_ai_summary_papers: int,
    publication_window_days: int,
    search_query: str,
    debug_counts: bool,
) -> int:
    require_requests()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL,
        }
    )

    summarized_papers = load_summarized_papers(state_path)
    debug_stats = DebugStats() if debug_counts else None
    try:
        candidate_papers = fetch_candidate_papers(
            session,
            search_query,
            limit_days=limit_days,
            debug_stats=debug_stats,
        )
    except requests.HTTPError as exc:
        raise RuntimeError(
            "bioRxiv API request failed. "
            f"Status: {exc.response.status_code if exc.response is not None else 'unknown'}."
        ) from exc

    new_papers: list[Paper] = []
    for paper in candidate_papers:
        if paper.doi in summarized_papers:
            if debug_stats is not None:
                debug_stats.skipped_already_summarized += 1
            continue
        if not is_recent_paper(paper, publication_window_days):
            if debug_stats is not None:
                debug_stats.skipped_outside_publication_window += 1
            continue
        new_papers.append(paper)
    if debug_stats is not None:
        debug_stats.final_new_papers = len(new_papers)

    summaries, summary_mode = summarize_papers(new_papers, max_ai_summary_papers)
    report_path = output_dir / f"biorxiv-human-evo-{dt.datetime.now():%Y-%m-%d}.md"
    write_report(
        report_path,
        new_papers,
        summaries,
        summary_mode,
        max_ai_summary_papers,
        publication_window_days,
        search_query,
        debug_stats,
    )

    summarized_at = dt.datetime.now().isoformat(timespec="seconds")
    for paper in new_papers:
        summarized_papers[paper.doi] = {
            "title": paper.title,
            "url": paper.url,
            "posted_date": paper.posted_date,
            "summarized_at": summarized_at,
        }
    save_summarized_papers(state_path, summarized_papers)

    print(f"Wrote report: {report_path}")
    print(f"New papers: {len(new_papers)}")
    print(f"Summary mode: {summary_mode}")
    print(f"AI summary cap: {max_ai_summary_papers}")
    print(f"Publication window (days): {publication_window_days}")
    if debug_stats is not None:
        print("Debug counts:")
        print(f"  API records seen: {debug_stats.api_records_seen}")
        print(f"  API records with DOI/title/abstract: {debug_stats.api_records_with_minimal_fields}")
        print(f"  Query matches before DOI deduplication: {debug_stats.query_matched_before_dedup}")
        print(f"  Candidate papers after DOI deduplication: {debug_stats.candidate_papers_after_dedup}")
        print(f"  Skipped as already summarized: {debug_stats.skipped_already_summarized}")
        print(f"  Skipped outside publication window: {debug_stats.skipped_outside_publication_window}")
        print(f"  Final new papers summarized: {debug_stats.final_new_papers}")
    return len(new_papers)


def sleep_until(hour: int, minute: int) -> None:
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    time.sleep((target - now).total_seconds())


def parse_schedule(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    if not match:
        raise argparse.ArgumentTypeError("Schedule must be HH:MM in 24-hour time.")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default="data/summarized_biorxiv_papers.json")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument(
        "--search-query",
        default=SEARCH_QUERY,
        help="bioRxiv search query to use. Defaults to the built-in human evolutionary genomics query.",
    )
    parser.add_argument(
        "--limit-days",
        type=int,
        default=DEFAULT_PUBLICATION_WINDOW_DAYS,
        help="How many recent days of bioRxiv search results to inspect.",
    )
    parser.add_argument(
        "--publication-window-days",
        type=int,
        default=DEFAULT_PUBLICATION_WINDOW_DAYS,
        help="Only summarize papers posted within this many days.",
    )
    parser.add_argument(
        "--max-ai-summary-papers",
        type=int,
        default=MAX_AI_SUMMARY_PAPERS,
        help="Use AI summaries only when the number of new papers is at or below this cap.",
    )
    parser.add_argument(
        "--debug-counts",
        action="store_true",
        help="Print filter counts and include them in the report to help diagnose discrepancies.",
    )
    parser.add_argument("--schedule", type=parse_schedule, help="Run every day at HH:MM local time.")
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state_file)
    output_dir = Path(args.output_dir)

    if args.run_once or not args.schedule:
        run_once(
            state_path,
            output_dir,
            args.limit_days,
            args.max_ai_summary_papers,
            args.publication_window_days,
            args.search_query,
            args.debug_counts,
        )
        return 0

    hour, minute = args.schedule
    while True:
        sleep_until(hour, minute)
        try:
            run_once(
                state_path,
                output_dir,
                args.limit_days,
                args.max_ai_summary_papers,
                args.publication_window_days,
                args.search_query,
                args.debug_counts,
            )
        except Exception as exc:  # pragma: no cover
            print(f"Run failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
