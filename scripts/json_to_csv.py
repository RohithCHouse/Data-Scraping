#!/usr/bin/env python3
"""Combine per-case JSON files into CSV files.

Walks INPUT_DIR recursively, reads every .json file (PDFs and other files are
ignored) and writes into OUTPUT_DIR:

  cases.csv     one row per case. Every top-level scalar field (AssessmentYear,
                CaseNumber, Cnr, FilingDate, HearingCount, ...) is a column.
                Lists of names (Judges, Petitioners, RespondentAdvocates, ...)
                are joined into one cell with " | ". Lists of records
                (HistoryOfCaseHearings, BusinessOnDateDetails, ...) are written
                to their own CSV and only their count is kept here.
  hearings.csv  one row per entry in HistoryOfCaseHearings.
  business_on_date.csv
                one row per entry in BusinessOnDateDetails.
  <Field>.csv   the same for any other list of records that has data in at
                least one file (InterimOrders, TaggedMatters, Notices, ...).

Every child CSV starts with Cnr and CaseNumber so it can be joined back to
cases.csv. Columns are the union of keys across all files, so a field missing
from some files is left blank.

Usage:
    python scripts/json_to_csv.py INPUT_DIR [-o OUTPUT_DIR]
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

LIST_SEPARATOR = " | "

CHILD_FILE_NAMES = {
    "HistoryOfCaseHearings": "hearings.csv",
    "BusinessOnDateDetails": "business_on_date.csv",
}

# Columns shown first in cases.csv (when present); everything else follows in
# the order it is first seen.
CASE_COLUMNS_FIRST = [
    "Cnr", "CaseNumber", "CaseType", "CaseTypeRaw", "CaseTypeLabel",
    "AssessmentYear", "CourtName", "BenchName", "District", "State",
    "CaseStatus", "StageOfCase", "StageOfCaseRaw", "FilingDate",
    "FirstHearingDate", "LastHearingDate", "LastBusinessDate",
    "NextHearingDate", "FilingToFirstHearingDays", "Petitioners",
    "PetitionerAdvocates", "Respondents", "RespondentAdvocates", "Judges",
]
KEY_COLUMNS = ["Cnr", "CaseNumber"]


def load_json(path):
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(path, encoding=encoding) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    raise ValueError("could not decode file")


def cell(value):
    """Turn a nested value into something that fits in one CSV cell."""
    if isinstance(value, list) and all(not isinstance(v, (list, dict)) for v in value):
        return LIST_SEPARATOR.join(str(v) for v in value if v is not None)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def split_case(record):
    """Return (case_row, {list_field: [child rows]}) for one case."""
    row, children = {}, {}
    keys = {k: record.get(k) for k in KEY_COLUMNS}
    for key, value in record.items():
        if isinstance(value, list) and any(isinstance(v, dict) for v in value):
            row[f"{key}_count"] = len(value)
            children[key] = [
                {**keys, **{k: cell(v) for k, v in item.items()}}
                for item in value if isinstance(item, dict)
            ]
        elif isinstance(value, list) and not value:
            # Empty list: could be names or records, we can't tell. Leave the
            # cell blank rather than adding a column of zeros.
            row[key] = ""
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                row[f"{key}.{sub_key}"] = cell(sub_value)
        else:
            row[key] = cell(value)
    return row, children


def ordered_columns(rows, first):
    seen = {key for row in rows for key in row}
    columns = [c for c in first if c in seen]
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def write_csv(path, rows, first):
    columns = ordered_columns(rows, first)
    # utf-8-sig so Excel shows Hindi and other non-ASCII text correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()

    json_files = sorted(args.input_dir.rglob("*.json"))
    if not json_files:
        sys.exit(f"No .json files found under {args.input_dir}")

    cases, children, errors = [], defaultdict(list), []
    for path in json_files:
        try:
            data = load_json(path)
        except (ValueError, OSError) as e:
            errors.append((path, e))
            continue
        records = data if isinstance(data, list) else [data]
        for record in records:
            if not isinstance(record, dict):
                errors.append((path, "not a JSON object"))
                continue
            row, case_children = split_case(record)
            row["SourceFolder"] = path.parent.name
            row["SourceFile"] = str(path.relative_to(args.input_dir))
            cases.append(row)
            for key, rows in case_children.items():
                children[key].extend(rows)

    # A list field that holds records in one file but is empty in another must
    # not be blank in some rows and a count in others.
    for key in children:
        for row in cases:
            if row.get(key) == "":
                del row[key]
                row[f"{key}_count"] = 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "cases.csv", cases, CASE_COLUMNS_FIRST)
    print(f"Read {len(json_files)} JSON files -> {len(cases)} cases")
    print(f"  cases.csv: {len(cases)} rows")
    for key, rows in children.items():
        name = CHILD_FILE_NAMES.get(key, f"{key}.csv")
        write_csv(args.output_dir / name, rows, KEY_COLUMNS)
        print(f"  {name}: {len(rows)} rows")
    for path, err in errors:
        print(f"  skipped {path}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
