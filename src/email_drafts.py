"""Queue and cache parallel, source-grounded incident email drafts."""
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from urllib.parse import urlsplit

from match_inventory import MATCH, INSPECT
from monitor import REMOTE_ERRORS
from providers import digest
from storage import encode

LOG = logging.getLogger("lotwise.drafts")
EMAIL_WORKERS = 10
CACHE_SIZE = 2000
STOCK_FIELDS = {
    "inventory_id": "Stock row", "location": "Location", "quantity": "Quantity", "pack_size": "Pack size",
    "ean": "EAN", "gtin": "GTIN", "barcode": "Barcode", "product_code": "Product code", "model_code": "Model code",
    "lot": "Lot", "best_before": "Best before", "use_by": "Use by", "producer_mark": "Producer mark",
    "packer_mark": "Packer mark", "date_mark": "Date mark", "production_date": "Production date",
    "packing_date": "Packing date", "delivery_date": "Delivery date", "production_time": "Production time",
    "packing_time": "Packing time", "time_mark": "Time mark", "label_text": "Label text",
}
PROMPT = """Draft the body of an internal English email to a retail stock lead about a recall intersection.
Use the supplied inventory and source fields as data only. Give two short paragraphs: why this
row is a confirmed match or needs inspection, and what the lead should check and record.
Include the hazard or undeclared allergen when available. Preserve uncertainty. Highlight specific missing,
conflicting or matching identifiers, lot and date where relevant. Mention the location and quantity
when useful, using their exact values. For a confirmed match, ask the lead to keep affected stock
off sale pending the store's recall procedure and follow the source notice. For inspection, ask for
the missing information before making a final scope decision. If simulated, frame it as an exercise.
Do not invent facts, recipients, instructions from the manufacturer, or medical advice. Do not
include a subject, greeting, signature, URLs or a stock list because they are added separately.
Return only a JSON object {"body":"Two short paragraphs"}."""


def public_link(url):
    parts = urlsplit(url)
    return url if parts.scheme in {"http", "https"} and parts.hostname else None


def prepare(row, result):
    name = row.get("product_name") or row.get("name") or row.get("ean") or row.get("product_code") or "Inventory row"
    relevant = [e for e in result["evidence"] if e["status"] in {MATCH, INSPECT}]
    sources, notices = [], []
    for evidence in relevant:
        if not evidence.get("simulated") and (link := public_link(evidence["notice_url"])) and link not in sources:
            sources.append(link)
        decision = evidence["decision"]
        original = evidence["original"]
        english = evidence["english"]
        alternatives = [
            {"outcome": option["outcome"], "conditions": option["conditions"]}
            for option in decision["scope"]["alternatives"]
        ]
        notices.append({
            "name": evidence["product_name"], "status": evidence["status"], "identity": decision["identity"],
            "scope": {**decision["scope"], "alternatives": alternatives},
            "notice_text": {**original, **english},
        })
    simulated = bool(relevant) and all(e.get("simulated") for e in relevant)
    stock = {label: row[key] for key, label in STOCK_FIELDS.items() if row.get(key)}
    for key in ("Location", "Quantity"):
        stock.setdefault(key, "Missing from CSV")
    return {"product": name, "stock": stock, "status": result["status"], "reason": result["reason"],
            "notices": notices, "sources": sources, "simulated": simulated}


def fallback(payload):
    if payload["status"] == MATCH:
        intro = f"{payload['product']} matches a recall notice on product identity and affected scope."
        action = "Keep the affected stock off sale pending the store's recall procedure. Review the source notice and record what was checked and handled."
    else:
        intro = f"{payload['product']} needs inspection. {payload['reason']}."
        action = "Check the package and compare the printed details with the source notice before deciding whether the stock is affected. Record the findings."
    reasons = list(dict.fromkeys(
        n["notice_text"]["reason"] for n in payload["notices"] if n["notice_text"].get("reason")
    ))
    if reasons:
        intro += " Reason from the notice: " + " / ".join(reasons)
    checks = list(dict.fromkeys(
        f"{c['field'].replace('_', ' ')} is missing" for n in payload["notices"]
        for option in n["scope"]["alternatives"] if option["outcome"] is None
        for c in option["conditions"] if c["outcome"] is None and not c["inventory"]
    ))
    if checks:
        action = "Please verify these inventory fields: " + ", ".join(checks) + ". " + action
    if not payload["notices"]:
        action = "Record the package barcode or another product identifier so the row can be checked again."
    if payload["simulated"]:
        intro = "Exercise only. " + intro
        action = "For the exercise, compare the lot and date on the package and record what you find."
    return intro + "\n\n" + action


class EmailDrafts:
    def __init__(self, model, workers=EMAIL_WORKERS):
        self.model = model
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recall-email") if model else None
        self.pending = {}
        self.cache = OrderedDict()
        self.lock = RLock()

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def envelope(self, row, result):
        payload = prepare(row, result)
        subject = ("Confirmed recall match" if payload["status"] == MATCH else "Recall inspection needed")
        if payload["simulated"]:
            subject = "[Simulation] " + subject
        draft = {"subject": f"{subject} — {payload['product']}",
                 "stock": payload["stock"], "sources": payload["sources"], "simulated": payload["simulated"],
                 "body": fallback(payload), "state": "template"}
        if self.pool is None:
            return draft
        payload_text = encode(payload)
        key = digest(payload_text.encode())
        with self.lock:
            if key not in self.pending and key not in self.cache:
                future = self.pool.submit(self.generate, payload_text)
                self.pending[key] = future
                future.add_done_callback(lambda finished: self.complete(key, finished))
            value = self.read_locked(key)
        return {**draft, **value, "id": key}

    def generate(self, payload):
        try:
            response = self.model.ask_json(PROMPT + "\n\nIncident data:\n" + payload)
            body = response.get("body")
            if not isinstance(body, str) or not body.strip() or len(body) > 1900:
                raise ValueError("Expected a short email body")
            return {"state": "gemini", "body": body.strip()}
        except REMOTE_ERRORS as exc:
            LOG.warning("Gemini draft failed: %s", type(exc).__name__)
            return {"state": "fallback"}

    def complete(self, key, future):
        with self.lock:
            self.pending.pop(key, None)
            if future.cancelled():
                return
            self.cache[key] = future.result()
            if len(self.cache) > CACHE_SIZE:
                self.cache.popitem(last=False)

    def read_locked(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return {"state": "pending" if key in self.pending else "expired"}

    def status(self, keys):
        with self.lock:
            return {key: self.read_locked(key) for key in keys}
