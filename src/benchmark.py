#!/usr/bin/env python3
"""Generate synthetic recall pages, or measure Context.dev against their gold JSON."""
import argparse
import csv
import html
import json
import re
import sys
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import markdown
from jsonschema import Draft202012Validator

from match_inventory import pack_size
from monitor import REMOTE_ERRORS, make_scraper
from providers import web_url
from storage import BASE, encode

DATA = BASE / "benchmark" / "data"
DEFAULT_HTML = BASE / "benchmark" / "html"
MONTHS = ("tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta",
          "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta")
PRODUCT_FIELDS = ("name_raw", "brand_raw", "pack_size_raw", "product_kind", "scope_basis")
TEXT_FIELDS = ("reason", "action", "scope_raw", "review_reason")
GROUPS = {
    "publication": "Publication and update dates",
    "product": "Product records",
    "identity": "Name, brand, pack and kind",
    "identifier": "Identifiers and roles",
    "scope_basis": "Scope basis",
    "clause": "Batch alternatives",
    "predicate": "Individual scope conditions",
}
ESC = html.escape


def load_corpus():
    cases = json.loads((DATA / "cases.json").read_text(encoding="utf-8"))
    schema = json.loads((BASE / "LotWise_recall.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    ids = set()
    for case in cases:
        case_id = case["id"]
        if not re.fullmatch(r"ss-[0-9]{2}", case_id) or case_id in ids:
            raise ValueError(f"Invalid or repeated case ID: {case_id}")
        ids.add(case_id)
        validator.validate(case["expected"])
    return cases


def render_body(body, layout):
    if layout == "definition":
        body = re.sub(r"(?m)^([^\n|]+) \| (.+?)(?:\n|$)",
                      lambda m: f"<dt>{ESC(m[1])}</dt><dd>{ESC(m[2])}</dd>\n", body)
        body = re.sub(r"(?m)(^<dt>.*\n)+", lambda m: f"<dl>\n{m[0]}</dl>", body)
    rendered = markdown.markdown(body, extensions=["tables", "nl2br"])
    # Some screens use insertions typical of authority articles, without adding product facts.
    if layout == "aside":
        rendered = rendered.replace("<blockquote>", "<aside class='indented'>").replace("</blockquote>", "</aside>")
    if layout == "figure":
        rendered += "<figure><div class='figure-placeholder'>Pakkausmerkinnät</div><figcaption>Havainnekuvan paikka</figcaption></figure>"
    return rendered


@lru_cache(maxsize=1)
def benchmark_styles():
    # Embed shared tokens so generated pages and reports work as standalone HTML.
    return "\n".join((BASE / "web" / name).read_text(encoding="utf-8")
                     for name in ("tokens.css", "benchmark.css"))


def render_case(case):
    title = ESC(case["title"])
    body = render_body((DATA / (case["id"] + ".md")).read_text(encoding="utf-8"), case["layout"])
    date = case["expected"]["published_date"]
    shown_date = f'{date["day"]}. {MONTHS[date["month"] - 1]} {date["year"]}'
    iso = "{year:04d}-{month:02d}-{day:02d}".format(**date)
    return f"""<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<title>{title} | LotWise-koeaineisto</title>
<style>{benchmark_styles()}</style>
</head>
<body class="newspage">
<header class="topstripe"><strong>LotWise</strong> · Harjoitusjulkaisut</header>
<main class="page content-main" id="content-main">
<nav class="trail" aria-label="Sijainti">Etusivu / Elintarvikkeet / Harjoitustiedotteet</nav>
<div class="main-title"><h1>{title}</h1></div>
<p class="mark">Kuvitteellinen testitiedote. Tuotteet ja erät ovat harjoitusaineistoa.</p>
<time class="date" datetime="{iso}">{shown_date}</time>
<div class="row"><article>{body}</article>
<aside class="sidebar"><h2>Harjoitustiedotteet</h2><p>Tätä sivua käytetään tietojen poiminnan testaamiseen.</p></aside>
</div>
</main>
<footer>LotWise · Harjoitusaineisto</footer>
</body></html>
"""


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate(cases, out):
    out.mkdir(parents=True, exist_ok=True)
    for case in cases:
        (out / (case["id"] + ".html")).write_text(render_case(case), encoding="utf-8")
    links = "\n".join(f'<li><a href="{c["id"]}.html">{ESC(c["title"])}</a> ({c["id"]})</li>' for c in cases)
    (out / "index.html").write_text(f"""<!doctype html>
<html lang="fi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>LotWise-koeaineisto</title><style>{benchmark_styles()}</style></head><body>
<header class="topstripe"><strong>LotWise</strong> · Harjoitusjulkaisut</header>
<main class="page"><h1>LotWise-koeaineisto</h1><p>Kuvitteelliset harjoitustiedotteet.</p>
<section class="index-section" aria-label="Harjoitustiedotteet"><ul>{links}</ul></section></main></body></html>
""", encoding="utf-8")
    print(f"Generated {len(cases)} synthetic notices in {out}")


def normal(value):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value or "").casefold()))


def normal_pack(value):
    normalized = pack_size(value or "")
    if normalized:
        count, amount, unit = normalized
        return [count, str(amount.normalize()), unit]
    return normal(value)


def ids(product, include_role=False):
    values = set()
    for entry in product.get("identifiers", []):
        raw = entry["value_raw"].strip()
        if raw:
            value = (entry["type"], raw, entry["role"]) if include_role else (entry["type"], raw)
            values.add(value)
    return values


def product_key(a, b):
    a_name, b_name = normal(a.get("name_raw")), normal(b.get("name_raw"))
    a_pack, b_pack = normal_pack(a.get("pack_size_raw")), normal_pack(b.get("pack_size_raw"))
    same_size = bool(a_pack and a_pack == b_pack)
    same_name = bool(a_name and b_name and (a_name == b_name or
                    a_name == " ".join(filter(None, (normal(b.get("brand_raw")), b_name))) or
                    b_name == " ".join(filter(None, (normal(a.get("brand_raw")), a_name)))))
    same_id = bool(ids(a) & ids(b))
    if same_id:
        return 12 + 4 * same_size + 2 * same_name
    if same_name:
        return 5 + 3 * same_size
    return 0


def pair_products(expected, actual):
    """Best one-to-one alignment. The benchmark has at most three expected products."""
    @lru_cache(maxsize=None)
    def assign(index, used):
        if index == len(actual):
            return 0, ()
        best = assign(index + 1, used)
        for i, target in enumerate(expected):
            if used & (1 << i):
                continue
            score = product_key(target, actual[index])
            if score:
                tail, pairs = assign(index + 1, used | (1 << i))
                if score + tail > best[0]:
                    best = score + tail, ((i, index),) + pairs
        return best

    _, pairs = assign(0, 0)
    assigned = dict(pairs)
    used = set(assigned.values())
    return [(i, assigned.get(i)) for i in range(len(expected))] + [
        (None, j) for j in range(len(actual)) if j not in used
    ]


def atomize(product):
    clauses = set()
    for alternative in product.get("scope_clauses", []):
        variants = {()}
        for predicate in alternative["all_of"]:
            field, op = predicate["field"], predicate["operator"]
            dates = predicate.get("date_values", [])
            raw = predicate.get("values_raw", [])
            values = dates or [v.strip() for v in raw]
            if dates and raw:
                values = [dates, raw]
            alternatives = [(field, "equals", [value]) for value in values] if op == "one_of" and values and not (dates and raw) else [(field, op, values)]
            variants = {tuple(sorted(set(v + (encode(cond),)))) for v in variants for cond in alternatives}
        clauses.update(variants)
    return clauses


def tokens(article, product_slots):
    grouped = {key: Counter() for key in GROUPS}
    if article.get("published_date"):
        grouped["publication"][encode(["published_date", article["published_date"]])] += 1
    for value in article.get("updated_dates", []):
        grouped["publication"][encode(["updated_dates", value])] += 1
    for slot, product in product_slots:
        grouped["product"][encode([slot])] += 1
        for key in PRODUCT_FIELDS:
            value = product.get(key)
            if value:
                if key == "pack_size_raw":
                    value = normal_pack(value)
                elif key in {"name_raw", "brand_raw"}:
                    value = normal(value)
                grouped["scope_basis" if key == "scope_basis" else "identity"][encode([slot, key, value])] += 1
        for key in ids(product, include_role=True):
            grouped["identifier"][encode([slot, *key])] += 1
        clauses = atomize(product)
        for clause in clauses:
            grouped["clause"][encode([slot, [json.loads(c) for c in clause]])] += 1
        for condition in {cond for clause in clauses for cond in clause}:
            grouped["predicate"][encode([slot, json.loads(condition)])] += 1
    return grouped


def score_case(expected, actual):
    gold, extracted = expected["products"], actual["products"]
    pairing = pair_products(gold, extracted)
    gold_tokens = tokens(expected, [(f"P{i + 1}", gold[i]) for i, _ in pairing if i is not None])
    actual_tokens = tokens(actual, [
        (f"P{i + 1}" if i is not None else f"Extra{j + 1}", extracted[j])
        for i, j in pairing if j is not None
    ])
    metrics, differences = {}, {}
    for key in GROUPS:
        g, a = gold_tokens[key], actual_tokens[key]
        metrics[key] = {"correct": sum((g & a).values()), "expected": sum(g.values()), "returned": sum(a.values())}
        if g != a:
            differences[key] = {"missing": [json.loads(x) for x in sorted((g - a).elements())],
                                "extra": [json.loads(x) for x in sorted((a - g).elements())]}
    text_review = []
    for i, j in pairing:
        old, new = gold[i] if i is not None else {}, extracted[j] if j is not None else {}
        if any(old.get(k) or new.get(k) for k in TEXT_FIELDS):
            text_review.append({"expected_product": old.get("name_raw"), "returned_product": new.get("name_raw"),
                                "expected": {k: old[k] for k in TEXT_FIELDS if old.get(k)},
                                "returned": {k: new[k] for k in TEXT_FIELDS if new.get(k)}})
    return metrics, differences, text_review


def ratio(good, total):
    return good / total if total else None


def summary(results):
    groups = {}
    for group, label in GROUPS.items():
        counts = {field: sum(row["metrics"][group][field] for row in results)
                  for field in ("correct", "expected", "returned")}
        hits, gold, produced = (counts[key] for key in ("correct", "expected", "returned"))
        groups[group] = {**counts, "label": label, "precision": ratio(hits, produced),
                         "recall": ratio(hits, gold), "f1": ratio(2 * hits, gold + produced)}
    return {
        "cases": len(results), "valid_extractions": sum(row["error"] is None for row in results),
        "exact_cases": sum(row["exact"] for row in results), "groups": groups,
    }


def percent(value):
    return "—" if value is None else f"{value:.1%}"


def html_report(path, totals, results):
    bars = []
    for group in totals["groups"].values():
        label = ESC(group["label"])
        value = group["f1"]
        filled = f'<progress value="{value or 0:.4f}" max="1" aria-label="{label} F1"></progress>'
        bars.append(f'<tr><th scope="row">{label}</th><td>{filled}</td><td>{percent(value)}</td>'
                    f"<td>{percent(group['precision'])}</td><td>{percent(group['recall'])}</td></tr>")
    rows = []
    for row in results:
        result = "Exact" if row["exact"] else "Error" if row["error"] else "Differences"
        more = json.dumps({"error": row["error"], "field_diffs": row["differences"],
                           "free_text_for_review": row["text_review"]}, ensure_ascii=False, indent=2)
        rows.append(f"<tr><td>{ESC(row['id'])}</td><td>{ESC(row['title'])}<small>{ESC(row['layout'])}</small></td>"
                    f'<td><span class="report-status {result.lower()}">{result}</span></td><td>{row["seconds"]:.1f}s</td>'
                    f"<td><details><summary>Inspect</summary><pre>{ESC(more)}</pre></details></td></tr>")
    path.write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>LotWise benchmark</title>
<style>{benchmark_styles()}</style></head><body><main class="page">
<header class="report-head"><strong>LotWise · Synthetic extraction benchmark</strong><h1>Extraction results</h1>
<p>{totals['exact_cases']} / {totals['cases']} exact cases · {totals['valid_extractions']} valid extractions</p></header>
<section class="report-section"><h2>Structured extraction scores</h2><table><thead><tr><th>Field group</th><th>F1 chart</th><th>F1</th><th>Precision</th><th>Recall</th></tr></thead>
<tbody>{''.join(bars)}</tbody></table>
<p>Failed requests count as missing fields in recall. Free-text reasons, actions, excerpts and review notes are available for human comparison.</p></section>
<section class="report-section"><h2>Cases</h2><table><thead><tr><th>Case</th><th>Article</th><th>Result</th><th>Time</th><th>Details</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section></main></body></html>""", encoding="utf-8")


def evaluate(cases, mapping, out, scraper):
    results = []
    for case in cases:
        key = case["id"]
        url = mapping[key]
        start = perf_counter()
        request_id, error = None, None
        try:
            actual, _, _, request_id = scraper.extract(url)
        except REMOTE_ERRORS as exc:
            error, actual = str(exc), {"products": []}
        metrics, differences, text_review = score_case(case["expected"], actual)
        result = {"id": key, "title": case["title"], "layout": case["layout"], "url": url,
                  "request_id": request_id, "seconds": round(perf_counter() - start, 2),
                  "error": error, "exact": error is None and not differences,
                  "metrics": metrics, "differences": differences, "text_review": text_review,
                  "expected": case["expected"], "extracted": actual if error is None else None}
        results.append(result)
        print(f"{key} {'ERROR' if error else 'EXACT' if result['exact'] else 'DIFFERENCES'}", flush=True)
    totals = summary(results)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "results.json", {"summary": totals, "cases": results})
    with (out / "scores.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case", "exact", "seconds", "field_group", "correct", "expected", "returned"])
        for row in results:
            for group, tally in row["metrics"].items():
                writer.writerow([row["id"], row["exact"], row["seconds"], group,
                                 tally["correct"], tally["expected"], tally["returned"]])
    html_report(out / "report.html", totals, results)
    print(f"{totals['exact_cases']}/{totals['cases']} exact · {totals['valid_extractions']} valid")
    for group in totals["groups"].values():
        print(f"{group['label']} · F1 {percent(group['f1'])}")
    print(f"Results saved to {out}")
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="Generate public-ready synthetic HTML")
    gen.add_argument("--out", type=Path, default=DEFAULT_HTML)
    run = commands.add_parser("evaluate", help="Scrape hosted HTML and compare it with gold records")
    run.add_argument("mapping", type=Path, help="JSON object mapping ss-01 through ss-20 to hosted HTML URLs")
    run.add_argument("--out", type=Path, default=BASE / "benchmark" / "results")
    run.add_argument("--case", action="append", help="Evaluate only these case IDs. Repeat to pick a subset.")
    args = parser.parse_args()
    try:
        cases = load_corpus()
        if args.command == "generate":
            generate(cases, args.out)
            return 0
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            raise ValueError("The mapping must be a JSON object")
        case_ids = {c["id"] for c in cases}
        chosen = set(args.case or case_ids)
        unknown = (set(mapping) | chosen) - case_ids
        if unknown:
            raise ValueError("Unknown case IDs: " + ", ".join(sorted(unknown)))
        if missing := chosen - set(mapping):
            raise ValueError("Missing hosted URLs for: " + ", ".join(sorted(missing)))
        for key in chosen:
            if not isinstance(mapping[key], str):
                raise ValueError(f"Expected a URL string for {key}")
            mapping[key] = web_url(mapping[key])
            if urlsplit(mapping[key]).netloc in {"example.com", "example.org", "example.net"}:
                raise ValueError(f"Replace the example URL for {key} with your hosted notice")
        totals = evaluate([c for c in cases if c["id"] in chosen], mapping, args.out, make_scraper())
        return int(totals["valid_extractions"] < totals["cases"])
    except (ValueError, OSError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
