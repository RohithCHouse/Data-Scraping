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

  fields_report.csv
                every field found in any JSON file (including fields inside
                lists), how many cases have it, how many have a non-empty
                value, and which CSV file and column it was written to.

Every child CSV starts with Cnr and CaseNumber so it can be joined back to
cases.csv. Columns are the union of keys across all files, so a field missing
from some files is left blank.

Usage:
    python scripts/json_to_csv.py INPUT_DIR [-o OUTPUT_DIR]
"""
import argparse
import csv
import json
import os
import sys
import time
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
    "NextHearingDate", "DecisionDate", "FilingToFirstHearingDays", "Petitioners",
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


def iter_json_files(folder):
    """Yield .json files under folder one at a time (no big list in memory)."""
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                yield from iter_json_files(entry.path)
            elif entry.name.lower().endswith(".json"):
                yield Path(entry.path)


def iter_cases(input_dir, errors=None):
    """Yield (path, case_row, children) for every case, reading files lazily."""
    for path in iter_json_files(input_dir):
        try:
            data = load_json(path)
        except (ValueError, OSError) as e:
            if errors is not None:
                errors.append((path, e))
            continue
        records = data if isinstance(data, list) else [data]
        for record in records:
            if not isinstance(record, dict):
                if errors is not None:
                    errors.append((path, "not a JSON object"))
                continue
            row, children = split_case(record)
            row["SourceFolder"] = path.parent.name
            row["SourceFile"] = str(path.relative_to(input_dir))
            yield path, record, row, children


SOURCE_COLUMNS = ["SourceFolder", "SourceFile"]


def ordered(keys, first):
    """Known columns first, then every other field A-Z, then counts and source.

    Sorting the rest keeps the order the same on every run and makes a field
    like DecisionDate easy to find, wherever in the folders it first appears.
    """
    rest = [k for k in keys if k not in first and k not in SOURCE_COLUMNS]
    counts = sorted(k for k in rest if k.endswith("_count"))
    rest = sorted((k for k in rest if not k.endswith("_count")), key=str.lower)
    return ([c for c in first if c in keys] + rest + counts
            + [c for c in SOURCE_COLUMNS if c in keys])


def is_empty(value):
    return value is None or value == "" or value == [] or value == {}


def field_locations(record):
    """Yield (field path, value, csv file, column) for every field in a case."""
    for key, value in record.items():
        if isinstance(value, list) and any(isinstance(v, dict) for v in value):
            yield key, value, "cases.csv", f"{key}_count"
            name = CHILD_FILE_NAMES.get(key, f"{key}.csv")
            for item in value:
                if isinstance(item, dict):
                    for sub_key, sub_value in item.items():
                        yield f"{key}[].{sub_key}", sub_value, name, sub_key
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                yield f"{key}.{sub_key}", sub_value, "cases.csv", f"{key}.{sub_key}"
        else:
            yield key, value, "cases.csv", key


def open_csv(path, columns):
    # utf-8-sig so Excel shows Hindi and other non-ASCII text correctly
    f = open(path, "w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(f, fieldnames=columns)
    writer.writeheader()
    return f, writer


def write_fields_report(path, fields, n_cases):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Field", "CasesWithField", "CasesWithValue",
                         "PercentWithValue", "WrittenToFile", "Column",
                         "ExampleFileWithValue"])
        for name in sorted(fields, key=str.lower):
            has, filled, csv_file, column, example = fields[name]
            writer.writerow([name, has, filled, f"{100 * filled / n_cases:.1f}%",
                             csv_file, column, example])


def elapsed(started):
    minutes, seconds = divmod(int(time.time() - started), 60)
    return f"{minutes} min {seconds} s"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        sys.exit(f"Input folder not found: {args.input_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    temp_path = args.output_dir / "_json_to_csv_temp.jsonl"
    started = time.time()

    # Pass 1: read every JSON file once. Save what was read to one temporary
    # file and note every column name, so each CSV gets a complete header.
    # Only the column names are kept in memory, not the data.
    print("Pass 1/2: reading JSON files ...")
    case_keys, child_keys = {}, defaultdict(dict)
    # field path -> [cases that have it, cases with a value, file, column, example]
    fields = {}
    errors, n_cases = [], 0
    with open(temp_path, "w", encoding="utf-8") as temp:
        for path, record, row, children in iter_cases(args.input_dir, errors):
            n_cases += 1
            seen, filled = set(), set()
            for name, value, csv_file, column in field_locations(record):
                if name not in fields:
                    fields[name] = [0, 0, csv_file, column, ""]
                seen.add(name)
                if not is_empty(value):
                    filled.add(name)
                    if not fields[name][4]:
                        fields[name][4] = str(path.relative_to(args.input_dir))
            for name in seen:
                fields[name][0] += 1
            for name in filled:
                fields[name][1] += 1
            case_keys.update(dict.fromkeys(row))
            for key, rows in children.items():
                for child in rows:
                    child_keys[key].update(dict.fromkeys(child))
            temp.write(json.dumps([row, children], ensure_ascii=False) + "\n")
            if n_cases % 10000 == 0:
                print(f"  {n_cases} cases read ({elapsed(started)})")
    if not n_cases:
        temp_path.unlink()
        sys.exit(f"No readable .json files found under {args.input_dir}")

    # A list field that holds records in some files but is empty in others
    # becomes a count column everywhere.
    for key in child_keys:
        case_keys.pop(key, None)
        case_keys[f"{key}_count"] = None
        if key in fields:
            fields[key][3] = f"{key}_count"

    write_fields_report(args.output_dir / "fields_report.csv", fields, n_cases)

    # Pass 2: read the temporary file (one file, so this is quick) and write
    # each row straight to the CSVs.
    print(f"Pass 2/2: writing CSV files ... ({elapsed(started)})")
    files = {}
    case_file, case_writer = open_csv(args.output_dir / "cases.csv",
                                      ordered(case_keys, CASE_COLUMNS_FIRST))
    child_writers, child_counts = {}, defaultdict(int)
    for key, keys in child_keys.items():
        name = CHILD_FILE_NAMES.get(key, f"{key}.csv")
        files[key], child_writers[key] = open_csv(args.output_dir / name,
                                                  ordered(keys, KEY_COLUMNS))
    try:
        written = 0
        with open(temp_path, encoding="utf-8") as temp:
            for line in temp:
                row, children = json.loads(line)
                for key in child_keys:
                    # Empty or missing in this case: a count of 0, not blank
                    if row.get(key) == "":
                        del row[key]
                    row.setdefault(f"{key}_count", 0)
                case_writer.writerow(row)
                for key, rows in children.items():
                    child_writers[key].writerows(rows)
                    child_counts[key] += len(rows)
                written += 1
                if written % 50000 == 0:
                    print(f"  {written} cases written ({elapsed(started)})")
    finally:
        case_file.close()
        for f in files.values():
            f.close()
    temp_path.unlink()

    print(f"Done in {elapsed(started)}. Output in {args.output_dir}")
    print(f"  cases.csv: {written} rows")
    print(f"  fields_report.csv: {len(fields)} fields (where each JSON field went)")
    for key in child_keys:
        print(f"  {CHILD_FILE_NAMES.get(key, key + '.csv')}: {child_counts[key]} rows")
    for path, err in errors:
        print(f"  skipped {path}: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
