#!/usr/bin/env python3
"""Compare an inventory CSV with the latest valid notice versions in SQLite."""

import argparse
import calendar
import csv
import difflib
import io
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

from storage import DEFAULT_DB, TRANSLATED_FIELDS, connect, encode

DATE_FIELDS = {"best_before", "use_by", "date_mark", "production_date", "packing_date", "delivery_date"}
CLOCK_FIELDS = {"production_time", "packing_time", "time_mark"}
CODE_FIELDS = {"lot", "producer_mark", "packer_mark"}
BARCODE_TYPES = {"ean", "gtin", "barcode_unspecified"}
INPUT_ID_FIELDS = {"product_name", "name", "ean", "gtin", "barcode", "product_code", "model_code",
                   "producer_mark", "packer_mark"}
MATCH = "MATCH"
INSPECT = "NEEDS_INSPECTION"
NO_MATCH = "NO_MATCH"


def normalized(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def words(text):
    return " ".join(re.findall(r"[^\W_]+", normalized(text), flags=re.UNICODE))


def pack_size(text):
    value = normalized(text).replace(",", ".").replace("×", "x")
    pattern = r"\s*(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*(kg|g|l|cl|ml)\s*"
    if match := re.fullmatch(pattern, value):
        count, amount, unit = match.groups()
        factor, base = {"kg": (1000, "g"), "g": (1, "g"), "l": (1000, "ml"),
                        "cl": (10, "ml"), "ml": (1, "ml")}[unit]
        return (int(count or 1), Decimal(amount) * factor, base)
    return None


def barcode(text):
    digits = re.sub(r"[\s-]+", "", text)
    if re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", digits):
        return digits.zfill(14)
    return None


def name_candidates(row, product):
    entered = words(row.get("product_name") or row.get("name", ""))
    name = words(product.get("name_raw", ""))
    brand = words(product.get("brand_raw", ""))
    if not entered or not name:
        return False
    options = {name}
    if brand and not name.startswith(brand + " "):
        options.add(brand + " " + name)
    for option in options:
        if entered == option:
            return True
        if min(len(entered.split()), len(option.split())) >= 2:
            if re.search(rf"(?:^| ){re.escape(option)}(?: |$)", entered):
                return True
            if difflib.SequenceMatcher(None, entered, option, autojunk=False).ratio() >= 0.88:
                return True
    return False


def scope_date(value):
    year, month, day = (value.get(key) for key in ("year", "month", "day"))
    if type(year) is not int or type(month) is not int or (day is not None and type(day) is not int):
        return None
    try:
        if day is None:
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        exact = date(year, month, day)
        return exact, exact
    except ValueError:
        return None


def inventory_date(text):
    if match := re.fullmatch(r"([0-9]{4})-([0-9]{2})(?:-([0-9]{2}))?", text):
        year, month, day = match.groups()
    elif match := re.fullmatch(r"(?:([0-9]{1,2})\.)?([0-9]{1,2})\.([0-9]{4})", text):
        day, month, year = match.groups()
    else:
        return None
    return scope_date({"year": int(year), "month": int(month), "day": int(day) if day else None})


def clock(text):
    if match := re.fullmatch(r"([0-9]{1,2})[:.]([0-9]{2})", text.strip()):
        hour, minute = map(int, match.groups())
        if hour < 24 and minute < 60:
            return hour * 60 + minute
    return None


def overlaps(actual, expected):
    """True for fully covered, False for disjoint, None for partial overlap."""
    lo, hi = actual
    start, end = expected
    if end < lo or start > hi:
        return False
    if start <= lo and hi <= end:
        return True
    return None


def dates_match(actual, op, values):
    if any(value is None for value in values):
        return None
    if op in {"equals", "one_of"}:
        by_start = sorted(values)
        merged = []
        for start, end in by_start:
            if merged and start.toordinal() <= merged[-1][1].toordinal() + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        results = [overlaps(actual, interval) for interval in merged]
        return True if True in results else None if None in results else False
    start, end = values[0]
    lo, hi = actual
    if op == "between_inclusive":
        return overlaps(actual, (start, values[1][1])) if start <= values[1][0] else None
    if op == "on_or_before":
        return True if hi <= end else False if lo > end else None
    if op == "before":
        return True if hi < start else False if lo >= start else None
    if op == "on_or_after":
        return True if lo >= start else False if hi < start else None
    if op == "after":
        return True if lo > end else False if hi <= end else None
    return None


def scalar_match(actual, op, values, ordered=False):
    if any(value is None for value in values):
        return None
    if op in {"equals", "one_of"}:
        return actual in values
    if op == "contains" and isinstance(actual, str):
        return values[0] in actual
    if not ordered:
        return None
    if op == "between_inclusive":
        lo, hi = values
        return lo <= actual <= hi if lo <= hi else None
    if op == "on_or_before":
        return actual <= values[0]
    if op == "before":
        return actual < values[0]
    if op == "on_or_after":
        return actual >= values[0]
    if op == "after":
        return actual > values[0]
    return None


def evaluate_condition(row, condition):
    field, op = condition["field"], condition["operator"]
    raw = row.get(field, "")
    is_date = field in DATE_FIELDS
    values = condition.get("date_values" if is_date else "values_raw", [])
    other = condition.get("values_raw" if is_date else "date_values", [])
    expected_count = 2 if op == "between_inclusive" else 1
    if not values or other or (op != "one_of" and len(values) != expected_count):
        return None, f"{field}: incomplete or incompatible notice values"
    if not raw:
        return None, f"{field}: missing from inventory"
    if is_date:
        actual = inventory_date(raw)
        value = dates_match(actual, op, [scope_date(v) for v in values]) if actual else None
    elif field in CLOCK_FIELDS:
        actual = clock(raw)
        times = [clock(v) for v in values]
        if actual is not None and all(v is not None for v in times):
            value = scalar_match(actual, op, times, ordered=True)
        elif field == "time_mark" and op in {"equals", "one_of"}:
            value = scalar_match(normalized(raw), op, [normalized(v) or None for v in values])
        else:
            value = None
    elif field in CODE_FIELDS:
        value = scalar_match(raw, op, [v.strip() or None for v in values])
    elif field == "label_text":
        value = scalar_match(normalized(raw), op, [normalized(v) or None for v in values])
    else:
        value = None
    if value is None:
        return None, f"{field}: ambiguous or unsupported comparison ({op})"
    return value, f"{field}: {'matches' if value else 'differs'} ({op})"


def matching_identifier(row, item, scanned):
    kind, raw = item["type"], item["value_raw"]
    if kind in BARCODE_TYPES:
        return barcode(raw) in scanned
    return kind in {"product_code", "model_code"} and bool(raw.strip()) and row.get(kind, "") == raw.strip()


def identity(row, product):
    source_ids = product.get("identifiers", [])
    submitted_codes = [row.get(key, "") for key in ("ean", "gtin", "barcode") if row.get(key)]
    scanned = {code for value in submitted_codes if (code := barcode(value))}
    positive, uncertain, source_barcodes = [], [], set()
    for item in source_ids:
        kind, role, raw = item["type"], item["role"], item["value_raw"]
        if kind in BARCODE_TYPES and role == "affected_product" and (code := barcode(raw)):
            source_barcodes.add(code)
        if matching_identifier(row, item, scanned):
            (positive if role == "affected_product" else uncertain).append(kind)
    if uncertain:
        return None, "Identifier appears on an incorrect label or has an uncertain role"
    if positive:
        inv_pack = pack_size(row.get("pack_size", ""))
        pub_pack = pack_size(product.get("pack_size_raw", ""))
        if (len(scanned) > 1 or any(not barcode(code) for code in submitted_codes)
                or (inv_pack and pub_pack and inv_pack != pub_pack)):
            return None, "Matching identifier conflicts with inventory barcode or pack size"
        if source_barcodes and scanned and not scanned.intersection(source_barcodes):
            return None, "Product or model code matches but barcode differs"
        return True, f"Confirmed product identifier ({', '.join(sorted(set(positive)))})"
    if scanned and source_barcodes and all(barcode(code) for code in submitted_codes):
        return False, "Barcode differs from identified product"
    if name_candidates(row, product):
        return None, "Product name is plausible; verify identity"
    if not product.get("name_raw") and not source_ids:
        for clause in product.get("scope_clauses", []):
            for condition in clause["all_of"]:
                if condition["field"] in {"producer_mark", "packer_mark"}:
                    if evaluate_condition(row, condition)[0] is True:
                        return None, "Producer or packer mark matches an incompletely identified product"
    return False, "No plausible product identifier or name"


def scope(row, product):
    basis = product["scope_basis"]
    clauses = product.get("scope_clauses", [])
    if basis == "all_batches_explicit" and not clauses:
        return True, "Notice explicitly includes all batches"
    if basis != "specific_conditions" or not clauses:
        return None, f"Scope needs manual verification ({basis})"
    pending = []
    for clause in clauses:
        checks = [evaluate_condition(row, condition) for condition in clause["all_of"]]
        if any(result is False for result, _ in checks):
            continue
        if checks and all(result is True for result, _ in checks):
            return True, "; ".join(reason for _, reason in checks)
        pending.extend(reason for result, reason in checks if result is None)
        if not checks:
            pending.append("Notice contains an empty scope clause")
    if pending:
        return None, "; ".join(dict.fromkeys(pending))
    return False, "Inventory falls outside all listed batch alternatives"


def decision_evidence(row, product, identity_match, identity_reason, scope_match, scope_reason):
    """Expose the same comparisons used by the matcher, with both sides' values."""
    source_ids = product.get("identifiers", [])
    code_fields = ("ean", "gtin", "barcode", "product_code", "model_code")
    inventory_ids = {key: row[key] for key in code_fields if row.get(key)}
    scanned = {code for key in ("ean", "gtin", "barcode") if (code := barcode(row.get(key, "")))}
    matching = [item for item in source_ids if matching_identifier(row, item, scanned)]
    review = product.get("review_reason", "").strip()
    identity_detail = identity_reason
    if identity_match is None and identity_reason.startswith("Product name"):
        if not source_ids:
            identity_detail = "Product names are similar. The notice gives no product identifier to verify."
        elif not inventory_ids:
            identity_detail = "Product names are similar. The inventory row has no product identifier to verify."
    clauses = []
    for clause in product.get("scope_clauses", []):
        checks = []
        for predicate in clause["all_of"]:
            outcome, detail = evaluate_condition(row, predicate)
            checks.append({
                "field": predicate["field"], "operator": predicate["operator"],
                "inventory": row.get(predicate["field"], ""),
                "date_values": predicate.get("date_values", []),
                "values": predicate.get("values_raw", []), "outcome": outcome, "detail": detail,
            })
        outcomes = [check["outcome"] for check in checks]
        outcome = False if False in outcomes else True if outcomes and all(outcomes) else None
        clauses.append({"outcome": outcome, "conditions": checks})
    return {
        "identity": {"outcome": identity_match, "detail": identity_detail,
                     "inventory_ids": inventory_ids, "notice_ids": source_ids, "matching_ids": matching,
                     "inventory_name": row.get("product_name") or row.get("name"),
                     "notice_name": product.get("name_raw")},
        "scope": {"outcome": scope_match, "basis": product["scope_basis"], "detail": scope_reason,
                  "alternatives": clauses},
        "review_reason": review,
    }


def classify(row, notice):
    for index, product in enumerate(notice["data"]["products"]):
        product_match, identity_reason = identity(row, product)
        if product_match is False:
            continue
        scope_match, scope_reason = scope(row, product)
        if product.get("review_reason", "").strip():
            status = INSPECT
        elif scope_match is False:
            status = NO_MATCH
        elif scope_match is True and product_match is True:
            status = MATCH
        else:
            status = INSPECT
        translations = notice["english"].get("products", [])
        yield {
            "status": status, "notice_url": notice["url"], "version_id": notice["id"],
            "product_index": index,
            "notice_extracted_at": notice["fetched_at"], "notice_refresh_error": notice["refresh_error"],
            "product_name": product.get("name_raw"),
            "notice_product_key": encode([
                product.get("name_raw", ""), product.get("pack_size_raw", ""),
                sorted((i["type"], i["role"], i["value_raw"]) for i in product.get("identifiers", [])),
            ]),
            "identity_reason": identity_reason, "scope_reason": scope_reason,
            "decision": decision_evidence(row, product, product_match, identity_reason, scope_match, scope_reason),
            "original": {key: product[key] for key in TRANSLATED_FIELDS if key in product},
            "english": translations[index] if index < len(translations) else {},
        }


def has_usable_identity(row):
    return (bool(words(row.get("product_name") or row.get("name", "")))
            or any(barcode(row.get(key, "")) for key in ("ean", "gtin", "barcode"))
            or any(row.get(key) for key in ("product_code", "model_code", "producer_mark", "packer_mark")))


def check_row(row, notices):
    results = [evidence for notice in notices for evidence in classify(row, notice)]
    statuses = {item["status"] for item in results}
    if MATCH in statuses:
        status, reason = MATCH, "A notice matches product identity and batch scope"
    elif INSPECT in statuses:
        status, reason = INSPECT, "At least one plausible notice requires verification"
    elif not has_usable_identity(row):
        status, reason = INSPECT, "Inventory row has no usable product identifier, name, or producer/packer mark"
    elif NO_MATCH in statuses:
        status, reason = NO_MATCH, "Plausible products fall outside all applicable notice scopes"
    else:
        status, reason = NO_MATCH, "No plausible product in the database's current notice records"
    return {"inventory": row, "status": status, "reason": reason, "evidence": results}


def load_inventory(path):
    return parse_inventory(path.read_text(encoding="utf-8-sig"))[1]


def parse_inventory(content):
    content = content.removeprefix("\ufeff")
    if not content.strip():
        raise ValueError("Inventory CSV is empty")
    header = content.splitlines()[0]
    try:
        dialect = csv.Sniffer().sniff(header, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(content, newline=""), dialect, strict=True)
    headers = [field.strip().casefold() for field in next(reader)]
    if len(set(headers)) != len(headers) or not all(headers):
        raise ValueError("CSV headers must be unique and nonempty")
    if not INPUT_ID_FIELDS.intersection(headers):
        raise ValueError("Include a product_name, barcode, product code or producer/packer mark column")
    rows = []
    for cells in reader:
        if not cells or not any(cell.strip() for cell in cells):
            continue
        if len(cells) != len(headers):
            raise ValueError(f"CSV line {reader.line_num} has {len(cells)} cells; expected {len(headers)}")
        rows.append(dict(zip(headers, (cell.strip() for cell in cells))))
    return headers, rows


def read_db(db):
    db.execute("BEGIN")
    mode = db.execute("SELECT value FROM settings WHERE name = 'mode'").fetchone()[0]
    notices = [
        dict(row, data=json.loads(row["data_json"]), english=json.loads(row["translation_json"] or "{}"))
        for row in db.execute("""
            SELECT v.id, a.url, v.fetched_at, v.data_json, v.translation_json,
                   CASE WHEN a.observed_hash <> v.html_hash
                        THEN COALESCE(a.last_error, 'Changed HTML awaiting extraction')
                        ELSE a.last_error END AS refresh_error
            FROM articles a JOIN notice_versions v ON v.id = a.active_version_id
            ORDER BY a.url
        """)
    ]
    issues = [dict(row) for row in db.execute("""
        SELECT 'overview' AS kind, url, last_error AS error FROM overviews WHERE last_error IS NOT NULL
        UNION ALL
        SELECT 'article', a.url,
               COALESCE(a.last_error, CASE WHEN v.id IS NULL THEN 'No valid extraction yet'
                                           ELSE 'Changed HTML awaiting extraction' END)
        FROM articles a LEFT JOIN notice_versions v ON v.id = a.active_version_id
        WHERE a.last_error IS NOT NULL OR v.id IS NULL OR a.observed_hash <> v.html_hash
        ORDER BY 1, 2
    """)]
    return mode, notices, issues


def report(rows, mode, notices, issues, output, output_format):
    checked = [check_row(row, notices) for row in rows]
    totals = Counter(item["status"] for item in checked)
    counts = {status: totals[status] for status in (MATCH, NO_MATCH, INSPECT)}
    if output_format == "json":
        json.dump({"database_mode": mode, "notice_count": len(notices), "source_issues": issues,
                   "counts": counts, "rows": checked}, output, ensure_ascii=False, indent=2)
        output.write("\n")
    else:
        writer = csv.writer(output)
        fields = list(rows[0]) if rows else []
        writer.writerow([*fields, "database_mode", "recall_status", "recall_reason", "recall_evidence_json"])
        for checked_row in checked:
            writer.writerow([*checked_row["inventory"].values(), mode, checked_row["status"], checked_row["reason"],
                             encode(checked_row["evidence"])])
    print(f"{mode.upper()} database | {len(notices)} notice(s) | {len(issues)} source issue(s) | "
          + " | ".join(f"{status} {count}" for status, count in counts.items()), file=sys.stderr)
    for issue in issues:
        print(f"{issue['kind']} {issue['url']} — {issue['error']}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database opened read-only")
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument("--output", type=Path, help="Write to this file instead of standard output")
    args = parser.parse_args()
    try:
        if args.output and args.output.resolve() in {args.inventory.resolve(), args.db.resolve()}:
            raise ValueError("Choose an output path distinct from the inventory and database")
        rows = load_inventory(args.inventory)
        with closing(connect(args.db)) as db:
            mode, notices, issues = read_db(db)
        if args.output:
            with args.output.open("w", newline="", encoding="utf-8") as output:
                report(rows, mode, notices, issues, output, args.format)
        else:
            report(rows, mode, notices, issues, sys.stdout, args.format)
    except (OSError, sqlite3.Error, ValueError, csv.Error) as exc:
        print(f"Inventory check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
