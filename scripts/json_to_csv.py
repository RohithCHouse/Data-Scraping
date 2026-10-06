#!/usr/bin/env python3
"""Combine per-case JSON files into CSV files.

Walks INPUT_DIR recursively, reads every .json file (PDFs and other files are
ignored) and writes:

  cases.csv     one row per JSON file; every top-level scalar field
                (AssessmentYear, CaseNumber, District, ...) becomes a column.
                Nested lists/objects are summarised (e.g. HistoryOfCaseHearings
                becomes HistoryOfCaseHearings_count) and kept as JSON text.
  hearings.csv  one row per entry in HistoryOfCaseHearings, linked back to the
                case by CaseNumber.

Columns are the union of keys across all files, so a field missing from some
files is simply left blank.

Usage:
    python scripts/json_to_csv.py INPUT_DIR [-o OUTPUT_DIR]
"""
import argparse
import csv
import json
import sys
from pathlib import Path

LIST_FIELD = "HistoryOfCaseHearings"


def load_json(path):
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(path, encoding=encoding) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    raise ValueError("could not decode file")


def flatten_case(data):
    row = {}
    for key, value in data.items():
        if isinstance(value, list):
            row[f"{key}_count"] = len(value)
            row[key] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                row[f"{key}.{sub_key}"] = (
                    json.dumps(sub_value, ensure_ascii=False)
                    if isinstance(sub_value, (list, dict))
                    else sub_value
                )
        else:
            row[key] = value
    return row


def ordered_columns(rows, first):
    columns = list(first)
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def write_csv(path, rows, first):
    columns = ordered_columns(rows, first)
    # utf-8-sig so Excel shows non-ASCII characters correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
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

    cases, hearings, errors = [], [], []
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
            row = flatten_case(record)
            row["SourceFolder"] = path.parent.name
            row["SourceFile"] = str(path.relative_to(args.input_dir))
            cases.append(row)
            for hearing in record.get(LIST_FIELD) or []:
                if isinstance(hearing, dict):
                    hearings.append({"CaseNumber": record.get("CaseNumber"), **hearing})

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / "cases.csv", cases,
              ["CaseNumber", "AssessmentYear", "CaseTypeLabel"])
    write_csv(args.output_dir / "hearings.csv", hearings, ["CaseNumber"])

    print(f"Read {len(json_files)} JSON files -> {len(cases)} cases, {len(hearings)} hearings")
    print(f"Wrote {args.output_dir / 'cases.csv'} and {args.output_dir / 'hearings.csv'}")
    for path, err in errors:
        print(f"  skipped {path}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
