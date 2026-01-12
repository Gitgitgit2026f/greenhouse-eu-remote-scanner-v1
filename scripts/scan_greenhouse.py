#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml


DEFAULT_BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


# --- Filtering helpers (EU-remote heuristic) ---

EU_HINTS = [
    "europe",
    "eu",
    "eea",
    "emea",
    "uk & europe",
    "uk/europe",
    "remote - europe",
    "remote (europe)",
    "remote, europe",
    "remote within europe",
    "remote in europe",
    "european time",
    "cet",
    "cest",
    "gmt+1",
    "gmt+2",
]

REMOTE_HINTS = [
    "remote",
    "work from home",
    "distributed",
    "anywhere",
]

EXCLUDE_HINTS = [
    "remote - us",
    "remote (us)",
    "remote, us",
    "united states",
    "u.s.",
    "usa",
    "canada",
    "latam",
    "latin america",
    "apac",
    "australia",
    "new zealand",
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def is_remote_eu(location_text: str, job_text_blob: str) -> bool:
    """
    Heuristic:
    - must look remote-ish somewhere (location or blob)
    - must have EU-ish hint somewhere
    - must not be explicitly US-only etc.
    """
    loc = norm(location_text)
    blob = norm(job_text_blob)

    # explicit exclusions first
    for bad in EXCLUDE_HINTS:
        if bad in loc or bad in blob:
            return False

    remoteish = any(h in loc or h in blob for h in REMOTE_HINTS)
    euish = any(h in loc or h in blob for h in EU_HINTS)

    # Also accept common pattern "Remote, EMEA" or "Remote - EMEA"
    if ("remote" in loc or "remote" in blob) and ("emea" in loc or "emea" in blob):
        euish = True

    return remoteish and euish


# --- Greenhouse API helpers ---

def gh_jobs_endpoint(board: str, base_url: str) -> str:
    # boards-api: /v1/boards/{board}/jobs
    return f"{base_url.rstrip('/')}/{board}/jobs"


def fetch_all_jobs(board: str, base_url: str, session: requests.Session) -> List[Dict[str, Any]]:
    """
    Greenhouse boards API supports pagination via ?page=
    We'll keep paging until empty.
    """
    all_jobs: List[Dict[str, Any]] = []
    page = 1

    while True:
        url = gh_jobs_endpoint(board, base_url)
        params = {"content": "true", "page": page}
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 404:
            # board not found or not public
            return []
        r.raise_for_status()

        data = r.json()
        jobs = data.get("jobs", [])
        if not jobs:
            break

        all_jobs.extend(jobs)
        page += 1

        # safety valve
        if page > 200:
            break

    return all_jobs


def load_boards(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    boards = doc.get("boards") or []
    out = []
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


def job_location_text(job: Dict[str, Any]) -> str:
    # Greenhouse jobs typically have "location": {"name": "..."}
    loc = job.get("location") or {}
    if isinstance(loc, dict):
        return str(loc.get("name") or "")
    return str(loc or "")


def job_text_blob(job: Dict[str, Any]) -> str:
    # Use title + departments + content fields (if present)
    parts: List[str] = []
    parts.append(str(job.get("title") or ""))

    # departments can be list
    depts = job.get("departments") or []
    if isinstance(depts, list):
        parts.extend([str(d.get("name") or "") for d in depts if isinstance(d, dict)])

    # offices can also help
    offices = job.get("offices") or []
    if isinstance(offices, list):
        parts.extend([str(o.get("name") or "") for o in offices if isinstance(o, dict)])

    # content is usually HTML; keep it as plain-ish
    content = job.get("content") or ""
    if isinstance(content, str):
        parts.append(content)

    return " ".join([p for p in parts if p])


def normalize_job(board_name: str, board_slug: str, job: Dict[str, Any]) -> Dict[str, Any]:
    job_id = job.get("id")
    title = job.get("title") or ""
    absolute_url = job.get("absolute_url") or ""
    updated_at = job.get("updated_at") or job.get("created_at") or None
    location = job_location_text(job)

    departments = []
    for d in job.get("departments") or []:
        if isinstance(d, dict) and d.get("name"):
            departments.append(d["name"])

    return {
        "board_name": board_name,
        "board": board_slug,
        "id": job_id,
        "title": title,
        "location": location,
        "departments": departments,
        "updated_at": updated_at,
        "url": absolute_url,
    }


def write_json(path: str, payload: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_md(path: str, items: List[Dict[str, Any]], generated_at: str) -> None:
    lines: List[str] = []
    lines.append(f"# Greenhouse EU-remote jobs\n")
    lines.append(f"_Generated: {generated_at}_\n")
    lines.append(f"Total: **{len(items)}**\n")

    if not items:
        lines.append("\nNo matching jobs found.\n")
    else:
        lines.append("\n| Company | Title | Location | Updated | Link |\n")
        lines.append("|---|---|---|---:|---|\n")
        for j in items:
            company = str(j.get("board_name", ""))
            title = str(j.get("title", "")).replace("|", "\\|")
            location = str(j.get("location", "")).replace("|", "\\|")
            updated = str(j.get("updated_at", "") or "")
            url = str(j.get("url", ""))
            link = f"[open]({url})" if url else ""
            lines.append(f"| {company} | {title} | {location} | {updated} | {link} |\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", default="boards.yaml", help="Path to boards.yaml")
    ap.add_argument("--outdir", default="output", help="Output directory")
    ap.add_argument("--debug", action="store_true", help="Print debug info")
    args = ap.parse_args()

    boards = load_boards(args.boards)
    if not boards:
        print("No boards found in boards.yaml", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "greenhouse-eu-remote-scanner/1.0 (GitHub Actions)",
            "Accept": "application/json",
        }
    )

    results: List[Dict[str, Any]] = []
    raw_counts: List[Tuple[str, int]] = []

    for b in boards:
        name = b["name"]
        slug = b["board"]
        base_url = b["base_url"]

        try:
            jobs = fetch_all_jobs(slug, base_url, session)
        except Exception as e:
            print(f"[WARN] Failed fetching board={slug}: {e}", file=sys.stderr)
            continue

        raw_counts.append((slug, len(jobs)))

        for job in jobs:
            location_text = job_location_text(job)
            blob = job_text_blob(job)

            if is_remote_eu(location_text, blob):
                results.append(normalize_job(name, slug, job))

    # sort: company then updated_at desc-ish (string sort is ok for ISO-like values)
    results.sort(key=lambda x: (norm(str(x.get("board_name", ""))), str(x.get("updated_at", ""))), reverse=True)

    generated_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    payload = {
        "generated_at": generated_at,
        "counts": {"boards": len(boards), "raw_per_board": raw_counts, "matches": len(results)},
        "jobs": results,
    }

    write_json(os.path.join(args.outdir, "jobs.json"), payload)
    write_md(os.path.join(args.outdir, "jobs.md"), results, generated_at)

    if args.debug:
        print(json.dumps(payload["counts"], indent=2))

    print(f"OK: {len(results)} matches. Wrote {args.outdir}/jobs.json and {args.outdir}/jobs.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
