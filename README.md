# Bail Order Analysis

A local pipeline that loops over bail-application order PDFs, reads each one with
Claude, and produces a CSV with:

| Column | Meaning |
|--------|---------|
| `source_file`  | PDF filename (for traceability) |
| `case_number`  | Case / registration number |
| `language`     | Language the order is written in |
| `outcome`      | **Bail allowed** / **Bail denied** / **Other** |
| `judge`        | Presiding officer who passed the order |
| `accused`      | Accused / applicant seeking bail |
| `reasoning`    | Short summary of the court's reasoning for the outcome |

> **Run this on the machine that holds the PDFs.** The files live on your local
> `C:\` drive, so the script must run there (Claude Code on the web runs in a
> cloud container that can't see your local drive).

---

## 1. Install Python dependencies

You need Python 3.9+ installed. Then, from this folder:

```powershell
pip install -r requirements.txt
```

## 2. (Optional) Install OCR tools for scanned PDFs

Many orders are normal text PDFs and need **no** OCR. But some — especially
scanned Hindi orders — are image-only, and need OCR to read. For those, install:

- **Tesseract OCR** (with the Hindi language pack):
  https://github.com/UB-Mannheim/tesseract/wiki
  During install, tick **Hindi** under "Additional language data", or download
  `hin.traineddata` into Tesseract's `tessdata` folder.
- **Poppler for Windows** (needed by `pdf2image`):
  https://github.com/oschwartz10612/poppler-windows/releases
  Unzip it and add its `bin\` folder to your `PATH`.

If these aren't installed, the script still runs — it just skips image-only
pages and warns you. Add `--no-ocr` to disable OCR entirely.

## 3. Set your Claude API key

The full analysis calls the Claude API, so set your key (PowerShell):

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Get a key at https://console.anthropic.com/ → API Keys.

*(Don't have / don't want an API key? See "Extract-only mode" below.)*

---

## 4. Run it — first 20 orders

```powershell
python analyze_bail_orders.py --limit 20
```

By default it reads from:

```
C:\Users\Admin\Downloads\Bail Study Orders\output-set\order_copies\Bihar\Patna\Patna Sadar\Sessions\2023
```

and writes `bail_orders_analysis.csv` in the current folder. Point it elsewhere
with `--input` / `--output`:

```powershell
python analyze_bail_orders.py `
  --input "C:\Users\Admin\Downloads\Bail Study Orders\output-set\order_copies\Bihar\Patna\Patna Sadar\Sessions\2023" `
  --output orders_batch1.csv `
  --limit 20
```

Open the CSV in Excel and verify. The CSV is written **incrementally** (one row
per order, flushed immediately), so progress survives an interruption.

### Run the rest, later

After verifying, run the whole set and skip what's already done:

```powershell
python analyze_bail_orders.py --resume        # process everything not yet in the CSV
```

---

## Extract-only mode (no API key)

If you'd rather have Claude Code read the orders interactively — or you don't
have an API key — dump the text of each PDF to `.txt` files:

```powershell
python analyze_bail_orders.py --extract-only --limit 20
```

This writes `extracted_text\<name>.txt` for each PDF. You can then open those in
Claude Code and ask it to build the CSV, or inspect them yourself.

---

## Command reference

| Flag | Default | Purpose |
|------|---------|---------|
| `--input`        | the Patna Sadar 2023 path | Folder to scan (recursive). |
| `--output`       | `bail_orders_analysis.csv` | Output CSV path. |
| `--limit N`      | `0` (all) | Process at most N PDFs. |
| `--model`        | `claude-opus-4-8` | Claude model id. |
| `--resume`       | off | Skip PDFs already in the output CSV. |
| `--extract-only` | off | Only dump text; no API calls. |
| `--text-dir`     | `extracted_text` | Where `--extract-only` writes `.txt`. |
| `--no-ocr`       | off | Disable OCR fallback. |
| `--ocr-lang`     | `hin+eng` | Tesseract language(s) for OCR. |

## Notes on accuracy

- `outcome` is normalised to **Bail allowed / Bail denied / Other**. "Other"
  covers withdrawn / disposed / adjourned orders that neither grant nor reject.
- The model is instructed **not to invent facts** — genuinely missing fields
  come back blank rather than guessed.
- Always spot-check the first batch against the source PDFs before trusting a
  full run. Legal reasoning summaries are the field most worth reviewing.
