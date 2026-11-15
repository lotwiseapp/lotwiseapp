# LotWise application

Flask application for matching inventory against Finnish recall notices. See the [project README](../README.md) for the overview and benchmark results.

Run the commands below from this directory (`src/`).

## Setup

Requires Python 3.9 or newer.

```bash
python3 -m venv ../.venv
source ../.venv/bin/activate
python -m pip install -r requirements.txt
python server.py --no-monitor
```

On Windows PowerShell, activate the environment with `..\.venv\Scripts\Activate.ps1`.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Load a sample inventory or upload a CSV. The bundled `lotwise.sqlite3` contains 101 notices, including historical recalls, and supports use without API keys.

## Configuration

| Environment variable | Purpose |
| --- | --- |
| `CONTEXT_DEV_API_KEY` | Structured extraction from recall articles. |
| `GEMINI_API_KEY` | Article discovery, English translations and incident email drafts. |

To enable live monitoring, set both keys and start the server without `--no-monitor`.

```bash
export CONTEXT_DEV_API_KEY="your-context-dev-key"
export GEMINI_API_KEY="your-gemini-key"
python server.py
```

| Server option | Default | Purpose |
| --- | --- | --- |
| `--port` | `8000` | Local HTTP port. |
| `--db` | `lotwise.sqlite3` | Existing recall database. |
| `--interval` | `900` | Seconds between monitoring cycles. |
| `--no-monitor` | Disabled | Serve stored notices without upstream polling. |

Source URLs and model settings are defined in `monitor.py`. The server manages the monitor process. Run only one monitor per database.

The dashboard checks the local database every five seconds while open. Its refresh button does not trigger an upstream scrape. Email drafts use templates when Gemini is unavailable. The application does not send emails.

## Inventory and matching

Uploads must be UTF-8 CSV files with a header, up to 1,000 rows and 512 KB. Comma, semicolon and tab delimiters are supported. Use ISO or Finnish numeric dates.

Common columns:

```text
inventory_id,product_name,ean,product_code,pack_size,lot,best_before,use_by,producer_mark,quantity,location
```

See [`examples/`](examples/) for sample files. Include at least a product name, barcode, product code or producer/packer mark. Additional columns remain visible in the dashboard.

The matcher returns `MATCH`, `NEEDS_INSPECTION` or `NO_MATCH`. It evaluates product identity and batch conditions using explicit rules. Missing or ambiguous information can require inspection. A no-match result is limited to the available notice records and stock identifiers.

Inventory and workflow history are saved in browser local storage. The server processes uploaded rows but does not store them in the recall database. When Gemini drafts are enabled, relevant stock fields and notice evidence are sent to the model.

To match a CSV from the command line:

```bash
python match_inventory.py examples/market_inventory.csv --format json
```

Use `--db PATH` for another database or `--output PATH` to save the results. CSV is the default output format.

## Incident simulation

Load the sample inventory and wait for the initial results, then run:

```bash
python demo_recall.py on
```

The next dashboard check adds a labelled simulated recall for Harbor Demo Lemon Oat Biscuits, lot `HB-2611`. Lot `HB-2612` remains outside scope.

Use `python demo_recall.py off` to remove the synthetic match or `python demo_recall.py status` to check its state. The simulation leaves the recall database unchanged and retains the labelled event in incident history.

## Extraction benchmark

The benchmark contains 20 synthetic Finnish recall articles with varied layouts and batch conditions. It uses the same Context.dev adapter, extraction instructions and schema as the monitor.

Regenerate the HTML pages if needed:

```bash
python benchmark.py generate
```

Host the contents of `benchmark/html/` at URLs reachable by Context.dev. Copy `benchmark/url_map.example.json` to `url_map.json` and replace the example URLs with the hosted URLs.

With `CONTEXT_DEV_API_KEY` configured, run:

```bash
python benchmark.py evaluate url_map.json
```

Results are written to `benchmark/results/` as `report.html`, `results.json` and `scores.csv`. Use `--out PATH` to change the destination or repeat `--case ID` to select cases. Scores cover structured fields. Evaluation does not modify the recall database.

## Source map

| Path | Responsibility |
| --- | --- |
| `server.py` | HTTP API and monitor lifecycle. |
| `monitor.py` | Notice discovery, change detection and extraction. |
| `providers.py` | HTTP, Context.dev and language model adapters. |
| `match_inventory.py` | CSV parsing, matching rules and evidence. |
| `email_drafts.py` | Draft generation, caching and template fallback. |
| `storage.py` | SQLite schema and database access. |
| `web/` | Dashboard interface and static assets. |
| `LotWise_recall.schema.json` | Structured recall schema. |
| `LotWise_context_request.json` | Extraction request and instructions. |
| `benchmark.py`, `benchmark/` | Benchmark runner and fixtures. |
| `demo_recall.py` | Reversible incident simulation. |
