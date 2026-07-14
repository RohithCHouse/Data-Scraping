#!/usr/bin/env python3
"""
analyze_bail_orders.py
======================

Loop over bail-application order PDFs, read each one with Claude, and write a CSV
with the following columns for every order:

    1. Case number
    2. Language of order
    3. Outcome (Bail allowed / Bail denied / Other)
    4. Name of the judge
    5. Name of the accused
    6. Reasoning for the outcome

Plus a `source_file` column so every row is traceable back to its PDF.

The script is built to run LOCALLY on the machine that actually holds the PDFs
(e.g. your Windows box), because that is where the files live.

Two modes
---------
1. Full analysis (default): extract text from each PDF and send it to the Claude
   API, which fills in all six fields. Requires an ANTHROPIC_API_KEY.

2. Extract-only (--extract-only): just pull the text out of each PDF into .txt
   files (no API key needed). Useful if you'd rather have Claude Code read the
   text files interactively instead of calling the API.

Scanned / image-only PDFs (common for Hindi orders) are OCR'd automatically if
Tesseract + Poppler are installed; otherwise those pages are skipped with a
warning (see README for setup).

Usage examples
--------------
    # First 20 orders, full analysis
    python analyze_bail_orders.py --limit 20

    # Point at a specific folder and output file
    python analyze_bail_orders.py --input "C:\\Users\\Admin\\Downloads\\Bail Study Orders\\output-set\\order_copies\\Bihar\\Patna\\Patna Sadar\\Sessions\\2023" --output orders.csv --limit 20

    # Just dump text, no API calls
    python analyze_bail_orders.py --extract-only --limit 20
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------

DEFAULT_INPUT = (
    r"C:\Users\Admin\Downloads\Bail Study Orders\output-set\order_copies"
    r"\Bihar\Patna\Patna Sadar\Sessions\2023"
)
DEFAULT_OUTPUT = "bail_orders_analysis.csv"
DEFAULT_MODEL = "claude-opus-4-8"

CSV_COLUMNS = [
    "source_file",
    "case_number",
    "language",
    "outcome",
    "judge",
    "accused",
    "reasoning",
]

# ----------------------------------------------------------------------------
# Optional dependencies -- imported lazily so --extract-only works even if the
# anthropic package isn't installed, and so a missing OCR stack degrades nicely.
# ----------------------------------------------------------------------------


def _lazy_import_pdf():
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit(
            "PyMuPDF is required for text extraction.\n"
            "Install it with:  pip install pymupdf"
        )
    return fitz


def _lazy_import_ocr():
    """Return (pytesseract, convert_from_path) or (None, None) if unavailable."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
        return pytesseract, convert_from_path
    except ImportError:
        return None, None


# ----------------------------------------------------------------------------
# Text extraction
# ----------------------------------------------------------------------------


def extract_text(pdf_path: Path, ocr: bool = True, ocr_lang: str = "hin+eng") -> str:
    """Extract text from a PDF, falling back to OCR for image-only pages."""
    fitz = _lazy_import_pdf()
    text_parts: list[str] = []
    needs_ocr_pages: list[int] = []

    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            page_text = page.get_text("text").strip()
            if page_text:
                text_parts.append(page_text)
            else:
                # Likely a scanned image page.
                needs_ocr_pages.append(page_index)

    if needs_ocr_pages and ocr:
        pytesseract, convert_from_path = _lazy_import_ocr()
        if pytesseract is None:
            print(
                f"  [warn] {pdf_path.name}: {len(needs_ocr_pages)} image page(s) "
                f"need OCR but pytesseract/pdf2image not installed -- skipping them.",
                file=sys.stderr,
            )
        else:
            try:
                # Convert only the pages that need OCR (1-indexed for pdf2image).
                for page_index in needs_ocr_pages:
                    images = convert_from_path(
                        str(pdf_path),
                        first_page=page_index + 1,
                        last_page=page_index + 1,
                        dpi=300,
                    )
                    for img in images:
                        ocr_text = pytesseract.image_to_string(img, lang=ocr_lang)
                        if ocr_text.strip():
                            text_parts.append(ocr_text.strip())
            except Exception as exc:  # noqa: BLE001 - OCR stack errors are varied
                print(
                    f"  [warn] {pdf_path.name}: OCR failed ({exc}). "
                    f"Continuing with whatever text was extractable.",
                    file=sys.stderr,
                )

    return "\n\n".join(text_parts).strip()


def detect_language(text: str) -> str:
    """Best-effort language guess; returns a human-readable name."""
    try:
        from langdetect import detect  # type: ignore
    except ImportError:
        # Fall back to a crude Devanagari check.
        if any("ऀ" <= ch <= "ॿ" for ch in text):
            return "Hindi"
        return "English"

    if not text.strip():
        return "Unknown"

    code_to_name = {
        "en": "English",
        "hi": "Hindi",
        "mr": "Marathi",
        "bn": "Bengali",
        "ur": "Urdu",
        "ne": "Nepali",
    }
    try:
        code = detect(text)
    except Exception:  # noqa: BLE001
        return "Unknown"
    return code_to_name.get(code, code)


# ----------------------------------------------------------------------------
# Claude analysis
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a legal analyst assistant helping study Indian bail \
application orders (district / sessions courts). You will be given the full text \
of one court order. Extract structured information faithfully. Do NOT invent \
facts: if something is genuinely not stated in the order, return an empty string \
for that field. Base every answer strictly on the text provided."""

USER_PROMPT_TEMPLATE = """Analyse the following bail-application order and return \
a JSON object with EXACTLY these keys:

- "case_number": The case number / case registration number (e.g. "Bail Appln. No. 1234/2023" or "Cr. Misc. No. ..."). Prefer the primary bail application number.
- "language": The language the order is written in (e.g. "English", "Hindi").
- "outcome": One of exactly "Bail allowed", "Bail denied", or "Other". Use "Bail allowed" if bail/anticipatory bail is granted, "Bail denied" if rejected/dismissed, and "Other" only if the order neither grants nor rejects (e.g. disposed as withdrawn, adjourned).
- "judge": Full name of the judge / presiding officer who passed the order.
- "accused": Name(s) of the accused / applicant / petitioner seeking bail.
- "reasoning": A concise (2-4 sentence) summary of the court's actual reasoning for the outcome -- the grounds it relied on (e.g. gravity of offence, prima facie evidence, period of custody, parity, flight risk, nature of allegations).

Return ONLY the JSON object, no markdown, no commentary.

ORDER TEXT:
\"\"\"
{order_text}
\"\"\"
"""

# Guard against enormous inputs blowing the context / cost. ~120k chars is
# comfortably within a large-context model and covers essentially all orders.
MAX_CHARS = 120_000


def analyze_with_claude(client, model: str, order_text: str) -> dict:
    """Send order text to Claude and parse the JSON response."""
    truncated = order_text[:MAX_CHARS]
    prompt = USER_PROMPT_TEMPLATE.format(order_text=truncated)

    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text").strip()

    # Strip accidental code fences.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lstrip().startswith("json"):
            raw = raw.lstrip()[4:]
    raw = raw.strip().strip("`").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to locate the first {...} block.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(raw[start : end + 1])
        else:
            raise
    return data


# ----------------------------------------------------------------------------
# Main pipeline
# ----------------------------------------------------------------------------


def find_pdfs(input_dir: Path) -> list[Path]:
    return sorted(input_dir.rglob("*.pdf"))


def load_existing(output_path: Path) -> set[str]:
    """Return the set of source_file names already present in the output CSV."""
    if not output_path.exists():
        return set()
    done: set[str] = set()
    with output_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("source_file"):
                done.add(row["source_file"])
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse bail order PDFs into a CSV.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Folder containing the PDFs (searched recursively).")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output CSV path.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N PDFs (0 = all).")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model id.")
    parser.add_argument("--extract-only", action="store_true", help="Only extract text to .txt files; no API calls.")
    parser.add_argument("--text-dir", default="extracted_text", help="Where --extract-only writes .txt files.")
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback for scanned pages.")
    parser.add_argument("--ocr-lang", default="hin+eng", help="Tesseract language(s) for OCR.")
    parser.add_argument("--resume", action="store_true", help="Skip PDFs already present in the output CSV.")
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.exists():
        sys.exit(
            f"Input folder not found:\n  {input_dir}\n\n"
            "Are you running this on the machine that holds the PDFs? "
            "Pass the correct path with --input."
        )

    pdfs = find_pdfs(input_dir)
    if not pdfs:
        sys.exit(f"No PDF files found under: {input_dir}")

    print(f"Found {len(pdfs)} PDF(s) under {input_dir}")

    output_path = Path(args.output)
    done = load_existing(output_path) if args.resume else set()

    # ---- Extract-only mode -------------------------------------------------
    if args.extract_only:
        text_dir = Path(args.text_dir)
        text_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for pdf in pdfs:
            if args.limit and count >= args.limit:
                break
            out_txt = text_dir / (pdf.stem + ".txt")
            print(f"[{count + 1}] extracting {pdf.name}")
            text = extract_text(pdf, ocr=not args.no_ocr, ocr_lang=args.ocr_lang)
            out_txt.write_text(text, encoding="utf-8")
            count += 1
        print(f"\nDone. Wrote {count} text file(s) to {text_dir}/")
        return

    # ---- Full analysis mode ------------------------------------------------
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "ANTHROPIC_API_KEY is not set.\n"
            "Set it and re-run, e.g. (Windows PowerShell):\n"
            '  $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
            "Or use --extract-only to skip the API and just dump text."
        )

    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("The anthropic package is required. Install it with:  pip install anthropic")

    client = Anthropic(api_key=api_key)

    # Open CSV in append mode if resuming, else write a fresh header.
    write_header = not (args.resume and output_path.exists())
    mode = "a" if (args.resume and output_path.exists()) else "w"
    with output_path.open(mode, encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
            fh.flush()

        processed = 0
        for pdf in pdfs:
            if args.limit and processed >= args.limit:
                break
            if pdf.name in done:
                print(f"[skip] already done: {pdf.name}")
                continue

            processed += 1
            print(f"[{processed}] {pdf.name}")
            try:
                text = extract_text(pdf, ocr=not args.no_ocr, ocr_lang=args.ocr_lang)
                if not text.strip():
                    row = _empty_row(pdf, note="No extractable text (image-only PDF and OCR unavailable?)")
                else:
                    data = analyze_with_claude(client, args.model, text)
                    row = _row_from_data(pdf, data, text)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print(f"  [error] {exc}", file=sys.stderr)
                row = _empty_row(pdf, note=f"ERROR: {exc}")

            writer.writerow(row)
            fh.flush()  # incremental save so a crash doesn't lose progress
            time.sleep(0.5)  # gentle pacing

    print(f"\nDone. Wrote {processed} row(s) to {output_path}")


def _row_from_data(pdf: Path, data: dict, text: str) -> dict:
    lang = (data.get("language") or "").strip() or detect_language(text)
    return {
        "source_file": pdf.name,
        "case_number": (data.get("case_number") or "").strip(),
        "language": lang,
        "outcome": (data.get("outcome") or "").strip(),
        "judge": (data.get("judge") or "").strip(),
        "accused": (data.get("accused") or "").strip(),
        "reasoning": (data.get("reasoning") or "").strip(),
    }


def _empty_row(pdf: Path, note: str = "") -> dict:
    return {
        "source_file": pdf.name,
        "case_number": "",
        "language": "",
        "outcome": "",
        "judge": "",
        "accused": "",
        "reasoning": note,
    }


if __name__ == "__main__":
    main()
