<p align="center">
  <img src="src/web/lotwise.svg" alt="LotWise app icon" width="96" height="96">
</p>

# LotWise

**Turn changing food recall notices into clear stock actions.**

Identify affected batches, flag items for inspection and show where to find them.

[Run locally](#run-locally) · [Repository structure](#repository-structure) · [Parser benchmark](#parser-benchmark) · [Presentation materials](#presentation-materials)

## The problem

A food recall appears online. Does it affect anything on your shelves?

Recall notices identify affected products through different combinations of names, barcodes, batch codes and dates. Their formats vary, and their scope can change after publication. Retailers need to understand both whether a notice applies to their stock and whether that answer has changed.

**LotWise connects public recall notices to a retailer's inventory.** It turns scattered product information and batch restrictions into traceable stock decisions, helping staff identify what needs attention and where to find it.

## What we built

LotWise is a local web application focused on (not only) Finnish recall notices. Upload an inventory CSV with product names, identifiers, available batch and date information, quantities and shelf locations. The dashboard compares each row with structured recall records and returns one of three outcomes.

| Outcome | Meaning |
| --- | --- |
| **Confirmed match** | The product identity and affected batch conditions match a notice. |
| **Needs inspection** | Missing, conflicting or ambiguous information requires verification. |
| **No match found** | No match was found in the available notice records using the supplied inventory details. |

Each incident shows the stock quantity and location, recall reason, source instructions and links to the original notice. The matching-evidence view compares inventory values with the notice's identifiers and batch conditions.

Staff can search, filter and sort results, acknowledge new incidents, resolve completed stock responses, undo resolutions and revisit incident history. A new match or a previously uncertain match becoming confirmed triggers a new incident and can reopen a resolved item.

LotWise also prepares email drafts using the stock details and recall evidence. Staff can review and copy them. The application does not send emails.

## How it works

**AI helps interpret notices. Explicit matching rules determine the inventory result.**

| Component | Role |
| --- | --- |
| Background monitor | Checks the Finnish Food Authority's recall listing and discovered articles for changes using HTML hashes. |
| Gemini | Helps identify recall article links, translates recall text into English and writes incident email drafts. |
| Context.dev | Extracts product identifiers, affected batches, date conditions, reasons and actions into a structured schema. |
| SQLite | Stores notice versions, extraction results and supporting source information. |
| Rule-based matcher | Compares product identity and batch conditions with inventory rows, exposing the evidence behind each decision. |
| Browser dashboard | Presents inventory, incidents, matching evidence, drafts and workflow history. |

The application uses Python, Flask, SQLite and a browser interface built with HTML, CSS and JavaScript. The configured recall source is [Ruokavirasto, the Finnish Food Authority](https://www.ruokavirasto.fi/takaisinvedot).

## Repository structure

| Path | Contents |
| --- | --- |
| `README.md` | Project overview, setup instructions and repository guide. |
| [`src/`](src/) | Application source, recall schema, sample inventory, stored notice snapshot and benchmark tools. |
| [`src/web/`](src/web/) | Dashboard HTML, CSS, JavaScript and the `lotwise.svg` app icon. |
| [`src/examples/`](src/examples/) | Sample inventory CSV files. |
| [`src/benchmark/`](src/benchmark/) | Synthetic recall articles, expected records and HTML benchmark pages. |
| [`presentation_materials/`](presentation_materials/) | Project presentation and video demonstration for the hackathon submission. |

The main application entry point is `src/server.py`. Monitoring lives in `src/monitor.py`, inventory matching in `src/match_inventory.py`, and email drafting in `src/email_drafts.py`. Provider integrations and database access are in `src/providers.py` and `src/storage.py`.

## Run locally

Requires **Python 3.9 or newer**. Run these commands from the repository root.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/requirements.txt
python src/server.py --no-monitor
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` instead.

Open **[http://127.0.0.1:8000](http://127.0.0.1:8000)** and choose **Load sample inventory** or **Upload CSV**.

The included SQLite snapshot contains 101 Finnish Food Authority notices, including historical recalls. The sample stock quantities and locations are fictional. This demo mode requires no API keys. Email templates remain available when Gemini is not configured.

### Enable live monitoring and Gemini drafts

Set the API keys as environment variables, then start the server without `--no-monitor`.

```bash
export CONTEXT_DEV_API_KEY="your-context-dev-key"
export GEMINI_API_KEY="your-gemini-key"
python src/server.py
```

The server starts the background monitor automatically when both keys are configured. The default source-check interval is 15 minutes. You can change it with `--interval`, for example `python src/server.py --interval 60`.

While open, the dashboard checks the local notice database every five seconds. Manual refresh checks that database and does not force an upstream scrape.

### Upload your inventory

Use a UTF-8 CSV with a header, up to 1,000 rows and a maximum size of 512 KB. Useful columns include:

```text
inventory_id,product_name,ean,product_code,pack_size,lot,best_before,use_by,producer_mark,quantity,location
```

Use the [sample inventory files](src/examples/) as a starting point. Available identifiers and batch details determine how precisely stock can be matched. Additional CSV columns remain visible in the Inventory view.

Inventory and incident history persist in the same browser's local storage. The server processes inventory rows for matching but does not save the uploaded inventory in the recall database.

## Try the incident demo

1. Start the dashboard and load the sample inventory.
2. Open a **Needs inspection** row and examine its matching evidence.
3. Review a **Confirmed match**, inspect its email draft and try resolving it and using Undo.
4. In another terminal, with the virtual environment active and from the repository root, enable the simulation:

   ```bash
   python src/demo_recall.py on
   ```

5. Wait for the next dashboard check. A clearly labelled simulated incident appears for Harbor Demo Lemon Oat Biscuits, lot `HB-2611`. Lot `HB-2612` remains outside scope.
6. Open and acknowledge the incident, then turn the simulation off:

   ```bash
   python src/demo_recall.py off
   ```

The simulation leaves the recall database untouched. Its labelled event remains in the incident history after the synthetic match is removed.

## Parser benchmark

We prepared a dataset of **20 fictional Finnish-language recall articles** to evaluate the parser. We deliberately presented varied product information, identifiers and complex batch conditions in messy, inconsistent formats to resemble the real recall notices we observed as closely as possible.

The dataset includes paragraphs, lists, tables, bilingual text, multiple products and pack sizes, date ranges, month-only dates, paired lot and date conditions, time windows, producer and packer marks, and identifiers printed on incorrect labels.

| Measure | Result |
| --- | ---: |
| Valid structured extractions | 20/20 |
| Exact cases under automated structured scoring | 15/20 (75%) |
| Exact structured cases after semantic review | 17/20 (85%) |

Semantic review accepted equivalent product grouping and a Finnish brand-name variation. The remaining differences involved scope classification, missing lot conditions and a product category.

We suspect the product-category mismatch reflects difficulty interpreting Finnish, potentially because Finnish is underrepresented in the training data of the underlying model Context.dev uses for semantic extraction. This is a hypothesis rather than an established cause. These results measure structured extraction on this synthetic benchmark.

The benchmark source is in [`src/benchmark/`](src/benchmark/), with the runner at [`src/benchmark.py`](src/benchmark.py). Evaluation uses the same Context.dev adapter, extraction instructions and JSON schema as the monitor. It requires publicly reachable benchmark pages and a Context.dev API key.

## Presentation materials

The [`presentation_materials/`](presentation_materials/) directory contains the project slides and a video demonstration for the hackathon submission.

## Our goal

LotWise aims to help retailers respond faster, understand uncertainty and avoid unnecessary removal of stock outside a recall's stated scope.
