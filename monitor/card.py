"""Read the publisher's dataset card as data, so claims can be tested, not quoted.

The card is markdown, but it asserts numbers: row counts per file, an event count,
a publication window, a documented column list and a missing-value rate per
attribute. Parsing it turns those assertions into the `expected` side of the
contract checks. If the card changes, the checks change with it - no claim is
transcribed into Python by hand, which is how a reconciliation usually rots.
"""

from __future__ import annotations

import re
from pathlib import Path

CARD_PATH = Path(__file__).resolve().parent.parent / "docs" / "upstream" / "dataset_card.md"

# The funnel columns in publication order; used to expand the card's shorthand
# `spontaneous_application_date ... 4th_interview_date` into real column names.
STAGES = [
    "spontaneous_application_date",
    "shortlist_date",
    "qualification_date",
    "resume_sent_to_company_date",
    "1st_interview_date",
    "2nd_interview_date",
    "3rd_interview_date",
    "4th_interview_date",
    "job_offer_proposed_date",
    "job_offer_accepted_date",
]

TABLES = ("candidates", "jobs", "history")

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}


def _cells(line: str) -> list[str]:
    # The card escapes pipes inside cells (`YYYY-MM-DD` \| `NaN`). Protect them
    # before splitting or a declared type reads as two columns and the NaN
    # allowance silently lands in the next cell over.
    protected = line.strip().replace("\\|", "\x00")
    raw = protected.strip().strip("|").split("|")
    return [c.replace("\x00", "|").strip() for c in raw]


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\|[\s:|-]+\|$", line.strip()))


def _md_tables(text: str) -> list[list[list[str]]]:
    """Group consecutive pipe lines into tables of cell-rows."""
    out, current = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            if not _is_separator(line):
                current.append(_cells(line))
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _unquote(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _expand_documented(cell: str) -> list[str]:
    """`a` / `b` -> [a, b];  `x` ... `y` -> the funnel columns from x to y."""
    body = cell.strip().strip("`")
    if "/" in body and "..." not in body and "…" not in body:
        return [_unquote(p) for p in body.split("/")]
    parts = re.split(r"\s*(?:\u2026|\.\.\.)\s*", body)
    if len(parts) == 2 and parts[0].strip("`") in STAGES:
        lo, hi = STAGES.index(parts[0].strip("`")), STAGES.index(parts[1].strip("`"))
        return STAGES[lo : hi + 1]
    return [_unquote(body)]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading) :]
    m = re.search(r"\n#{3,4} ", rest)
    return rest[: m.start()] if m else rest


def parse(path: Path = CARD_PATH) -> dict:
    text = path.read_text(encoding="utf-8")
    claims: dict = {"source": path.name, "card_sha_hint": len(text)}

    # --- abstract: "**37,554 candidates**, **6,011 vacancies** and
    #     **42,288 interaction trajectories** (comprising **78,195 specific events**)"
    abstract = text.split("###")[1] if "###" in text else text
    for needle, key in [
        (r"\*\*([\d,]+)\s+candidates\*\*", "candidates"),
        (r"\*\*([\d,]+)\s+vacancies\*\*", "jobs"),
        (r"\*\*([\d,]+)\s+interaction trajectories\*\*", "trajectories"),
        (r"\*\*([\d,]+)\s+specific events\*\*", "events"),
    ]:
        m = re.search(needle, abstract)
        if m:
            claims.setdefault("counts", {})[key] = int(m.group(1).replace(",", ""))
    m = re.search(r"\(([A-Z][a-z]+) (\d{4}) [–-] ([A-Z][a-z]+) (\d{4})\)", abstract)
    if m:
        import calendar

        sm, sy, em, ey = MONTHS[m.group(1)], int(m.group(2)), MONTHS[m.group(3)], int(m.group(4))
        claims["window"] = {
            "label": f"{m.group(1)} {sy} - {m.group(3)} {ey}",
            "start": f"{sy}-{sm:02d}-01",
            "end": f"{ey}-{em:02d}-{calendar.monthrange(ey, em)[1]:02d}",
        }

    # --- Files & Sizes table -> row count per file
    files_tbl = _md_tables(_section(text, "### • Files & Sizes"))
    claims["file_rows"] = {}
    for row in files_tbl[0][1:]:
        name = _unquote(row[0])
        table = name.removesuffix(".csv")
        if table in TABLES:
            claims["file_rows"][table] = int(row[1].replace(",", ""))

    # --- Schema: documented columns and their declared types, per table
    claims["columns"] = {}
    claims["types"] = {}
    claims["notes"] = {}
    for table in TABLES:
        try:
            section = _section(text, f"#### `{table}.csv`")
        except ValueError:
            continue
        cols: list[str] = []
        types: dict[str, str] = {}
        notes: dict[str, str] = {}
        for tbl in _md_tables(section):
            for row in tbl[1:]:
                if len(row) < 2 or not row[0].startswith("`"):
                    continue
                names = _expand_documented(row[0])
                declared = row[1]
                note = row[2] if len(row) > 2 else ""
                for n in names:
                    cols.append(n)
                    types[n] = declared
                    notes[n] = note
        claims["columns"][table] = cols
        claims["types"][table] = types
        claims["notes"][table] = notes

    # --- Missing-Value Rates: attribute -> {candidates: pct, jobs: pct}
    try:
        mv = _md_tables(_section(text, "### • Missing-Value Rates"))[0]
    except (ValueError, IndexError):
        mv = [[]]
    claims["missing_rates"] = {}
    for row in mv[1:]:
        if len(row) < 3:
            continue
        attr = _unquote(row[0])
        pct = {}
        for side, cell in zip(("candidates", "jobs"), row[1:3]):
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", cell)
            if m:
                pct[side] = float(m.group(1))
        if pct:
            claims["missing_rates"][attr] = pct

    # --- grain statement for history.csv
    m = re.search(r"#### `history\.csv`.*?\n\n(.+)", text, re.S)
    claims["grain_history"] = m.group(1).strip() if m else ""
    return claims


# The card names the salary attributes differently per table; map claim -> column.
CLAIM_COLUMN = {
    "create_date": {"candidates": "create_date", "jobs": "create_date"},
    "skills": {"candidates": "skills", "jobs": "skills"},
    "expertise_area": {"candidates": "expertise_area", "jobs": "expertise_area"},
    "job_category": {"candidates": "job_category", "jobs": "job_category"},
    "years_experience": {"candidates": "years_experience", "jobs": "years_experience"},
    "salary": {"candidates": "actual_salary", "jobs": "salary"},
    "daily_rate": {"candidates": "actual_daily_salary", "jobs": "daily_rate"},
    "contract_type": {"candidates": "contract_type", "jobs": "contract_type"},
    "zipcode": {"candidates": "zipcode", "jobs": "zipcode"},
}


if __name__ == "__main__":
    import json

    print(json.dumps(parse(), indent=2, ensure_ascii=False))
