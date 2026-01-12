#!/usr/bin/env python3
"""
Greenhouse EU-remote scanner (location-first)

- Reads boards from boards.yaml:
    boards:
      - name: OpenZeppelin
        board: openzeppelin
        base_url: https://boards-api.greenhouse.io/v1/boards   (optional)

- Fetches jobs from:
    https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true&page=1...

- Filters EU-remote using LOCATION primarily (and optionally offices/departments).
  Avoids using full HTML content because it causes massive false positives.

- Writes:
    output/jobs.json
    output/jobs.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import yaml

DEFAULT_BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

# --- Heuristics (tuned to be conservative) ---

REMOTE_TOKENS = [
    "remote",
    "work from home",
    "distributed",
]

# EU-ish indicators we allow. (Keep this tight; we can expand later.)
EU_TOKENS = [
    "europe",
    "eu",
    "eea",
    "emea",
    "cet",
    "cest",
    "gmt+1",
    "gmt+2",
    "utc+1",
    "utc+2",
]

# Strong “global/no region” signals: accept only if paired with EU signal elsewhere (offices/departments).
GLOBAL_REMOTE_LOCATIONS = {
    "remote",
    "fully remote",
    "remote (global)",
    "remote - global",
    "remote, global",
    "remote worldwide",
    "remote (worldwide)",
    "remote - worldwide",
}

# Exclusions: if these appear in location/offices/departments text -> reject.
EXCLUDE_TOKENS = [
    "remote - us",
    "remote (us)",
    "remote, us",
    "united states",
    "u.s.",
    "usa",
    "canada",
    "latam",
    "latin america",
    "south america",
    "apac",
    "australia",
    "new zealand",
    "india",   # often separate region; keep conservative
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def contains_any(text: str, tokens: Iterable[str]) -> bool:
    t = norm(text)
    return any(tok in t for tok in tokens)


def is_remote(text: str) -> bool:
    t = norm(text)
    # include some common "hybrid remote" variants
    return contains_any(t, REMOTE_TOKENS) or ("hybrid" in t and "remote" in t)


def is_euish(text: str) -> bool:
    return contains_any(text, EU_TOKENS)


def is_excluded(text: str) -> bool:
    return contains_any(text, EXCLUDE_TOKENS)


def eu_remote_match(location: str, secondary_text: str) -> bool:
    """
    Conservative, location-first rule:
    - Exclude if location/secondary includes US/Canada/APAC/LatAm etc.
    - Must be remote-ish in location OR (location is "Hybrid/Remote" style)
    - Must be EU-ish in location, OR:
        - location is global remote, AND EU-ish appears in secondary_text
    """
    loc = norm(location)
    sec = norm(secondary_text)

    if is_excluded(loc) or is_excluded(sec):
        return False

    remote_in_loc = is_remote(loc)
    eu_in_loc = is_euish(loc) or ("emea" in loc)  # emea is also in EU_TOKENS but keep explicit

    if not remote_in_loc:
        return False

    # If location explicitly mentions Europe/EMEA/CET -> accept
    if eu_in_loc:
        return True

    # If location is generic "Remote" or "Remote (Global)" then require EU signal elsewhere
    if loc in GLOBAL_REMOTE_LOCATIONS:
        return is_euish(sec) or ("emea" in sec)

    # Otherwise: not EU-specific
    return False


# --- Greenhouse API ---

def gh_jobs_endpoint(board: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/{board}/jobs"


def fetch_all_jobs(board: str, base_url: str, session: requests.Session, max_pages: int = 200) -> List[Dict[str, Any]]:
    """
    Fetches all pages until empty or max_pages reached.
    """
    all_jobs: List[Dict[str, Any]] = []
    page = 1
    while True:
        url = gh_jobs_endpoint(board, base_url)
        params = {"content": "true", "page": page}
        r = session.get(url, params=params, timeout=45)

        # If board doesn't exist / not public
        if r.status_code == 404:
            return []

        r.raise_for_status()
        data = r.json() or {}
        jobs = data.get("jobs", []) or []
        if not jobs:
            break

        all_jobs.extend(jobs)
        page += 1
        if page > max_pages:
            break

    return all_jobs


def load_boards(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    boards = doc.get("boards") or []
    out: List[Dict[str, str]] = []

    for b in boards:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name") or b.get("board") or "").strip()
        board = str(b.get("board") or "").strip()
        base_url = str(b.get("base_url") or DEFAULT_BASE_URL).strip()
        if not board:
            continue
        if not name:
            name = board
        out.append({"name": name, "board": board, "base_url": base_url})
    return out


def job_location(job: Dict[str, Any]) -> str:
    loc = job.get("location") or {}
    if isinstance(loc, dict):
        return str(loc.get("name") or "")
    return str(loc or "")


def offices_text(job: Dict[str, Any]) -> str:
    offices = job.get("offices") or []
    if not isinstance(offices, list):
        return ""
    return " ".join([str(o.get("name") or "") for o in offices if isinstance(o, dict)])


def departments_text(job: Dict[str, Any]) -> str:
    depts = job.get("departments") or []
    if not isinstance(depts, list):
        return ""
    return " ".join([str(d.get("name") or "") for d in depts if isinstance(d, dict)])


def normalize_job(board_name: str, board_slug: str, job: Dict[str, Any]) -> Dict[str, Any]:
    # Keep it stable + small for repo storage
    return {
        "board_name": board_name,
        "board": board_slug,
        "id": job.get("id"),
        "title": job.get("title") or "",
        "location": job_location(job),
        "updated_at": job.get("updated_at") or job.get("created_at") or None,
        "url": job.get("absolute_url") or "",
    }


# --- Output ---

def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_md(path: str, items: List[Dict[str, Any]], generated_at: str) -> None:
    lines: List[str] = []
    lines.append("# Greenhouse EU-remote jobs\n\n")
    lines.append(f"_Generated: {generated_at}_\n\n")
    lines.append(f"Total: **{len(items)}**\n\n")

    if not items:
        lines.append("No matching jobs found.\n")
    else:
        lines.append("| Company | Title | Location | Updated | Link |\n")
        lines.append("|---|---|---|---:|---|\n")
        for j in items:
            company = str(j.get("board_name", "")).replace("|", "\\|")
            title = str(j.get("title", "")).replace("|", "\\|")
            location = str(j.get("location", "")).replace("|", "\\|")
            updated = str(j.get("updated_at", "") or "")
            url = str(j.get("url", ""))
            link = f"[open]({url})" if url else ""
            lines.append(f"| {company} | {title} | {location} | {updated} | {link} |\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# --- Main ---

@dataclass
class ScanStats:
    raw_per_board: List[Tuple[str, int]]
    matched_per_board: List[Tuple[str, int]]
    top_locations_raw: List[Tuple[str, int]]
    top_locations_matched: List[Tuple[str, int]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", default="boards.yaml", help="Path to boards.yaml")
    ap.add_argument("--outdir", default="output", help="Output directory")
    ap.add_argument("--debug", action="store_true", help="Print debug stats to stdout")
    ap.add_argument("--max-total", type=int, default=1500, help="Cap total matched jobs written (repo safety)")
    ap.add_argument("--max-per-board", type=int, default=600, help="Cap matched jobs per board written")
    args = ap.parse_args()

    boards = load_boards(args.boards)
    if not boards:
        print("No boards found in boards.yaml", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "greenhouse-eu-remote-scanner/2.0 (GitHub Actions)",
            "Accept": "application/json",
        }
    )

    generated_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    raw_counts: List[Tuple[str, int]] = []
    matched_counts: List[Tuple[str, int]] = []

    raw_loc_counter = Counter()
    matched_loc_counter = Counter()

    # Collect matched jobs with dedupe
    all_matched: List[Dict[str, Any]] = []
    seen_keys = set()

    for b in boards:
        name = b["name"]
        slug = b["board"]
        base_url = b["base_url"]

        try:
            jobs = fetch_all_jobs(slug, base_url, session)
        except Exception as e:
            print(f"[WARN] Failed fetching board={slug}: {e}", file=sys.stderr)
            raw_counts.append((slug, 0))
            matched_counts.append((slug, 0))
            continue

        raw_counts.append((slug, len(jobs)))

        board_matched: List[Dict[str, Any]] = []
        for job in jobs:
            loc = job_location(job)
            raw_loc_counter[norm(loc) or "(empty)"] += 1

            # Secondary structured fields only
            sec = " ".join([offices_text(job), departments_text(job)])

            if eu_remote_match(loc, sec):
                nj = normalize_job(name, slug, job)
                # dedupe key: url if present else (board,id)
                key = nj.get("url") or f"{slug}:{nj.get('id')}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                board_matched.append(nj)
                matched_loc_counter[norm(loc) or "(empty)"] += 1

        # sort within board: updated desc (string compare ok enough), then title
        board_matched.sort(key=lambda x: (str(x.get("updated_at") or ""), norm(str(x.get("title") or ""))), reverse=True)

        # cap per board
        board_matched = board_matched[: max(0, args.max_per_board)]
        matched_counts.append((slug, len(board_matched)))
        all_matched.extend(board_matched)

    # final sort: updated desc, company
    all_matched.sort(
        key=lambda x: (str(x.get("updated_at") or ""), norm(str(x.get("board_name") or "")), norm(str(x.get("title") or ""))),
        reverse=True,
    )

    # cap total output
    all_matched = all_matched[: max(0, args.max_total)]

    payload = {
        "generated_at": generated_at,
        "counts": {
            "boards": len(boards),
            "raw_per_board": raw_counts,
            "matched_per_board": matched_counts,
            "matches_written": len(all_matched),
        },
        "jobs": all_matched,
    }

    write_json(os.path.join(args.outdir, "jobs.json"), payload)
    write_md(os.path.join(args.outdir, "jobs.md"), all_matched, generated_at)

    if args.debug:
        debug_obj = {
            "boards": len(boards),
            "raw_per_board": raw_counts,
            "matched_per_board": matched_counts,
            "matches_written": len(all_matched),
            "top_locations_raw": raw_loc_counter.most_common(12),
            "top_locations_matched": matched_loc_counter.most_common(12),
        }
        print(json.dumps(debug_obj, indent=2))

    print(f"OK: wrote {len(all_matched)} matches to {args.outdir}/jobs.json and {args.outdir}/jobs.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
