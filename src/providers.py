"""HTTP adapters for public HTML, Context.dev, and interchangeable text LLMs."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator
from lxml import etree, html as lxml_html

from storage import BASE


def digest(content):
    return hashlib.sha256(content).hexdigest()


def web_url(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError(f"Expected an HTTP(S) URL without credentials: {url}")
    if any(ch.isspace() for ch in url):
        raise ValueError("URL contains whitespace")
    port = parts.port
    host = parts.hostname.encode("idna").decode("ascii")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def site_hosts(url):
    host = urlsplit(url).hostname
    if host and "." in host:
        bare = host.removeprefix("www.")
        return {bare, f"www.{bare}"}
    return {host}


class SiteRedirects(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = web_url(newurl)
        if urlsplit(newurl).hostname not in self.hosts:
            raise ValueError(f"Redirect outside configured hosts: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Page:
    url: str
    body: bytes
    encoding: Optional[str]

    @property
    def hash(self):
        return digest(self.body)


def fetch_page(url, hosts, timeout, max_bytes):
    url = web_url(url)
    if urlsplit(url).hostname not in hosts:
        raise ValueError(f"URL outside configured hosts: {url}")
    request = Request(url, headers={
        "User-Agent": "LotWise/1.0 (recall notice monitor)",
        "Accept": "text/html,application/xhtml+xml",
        "Cache-Control": "no-cache",
    })
    with build_opener(SiteRedirects(hosts)).open(request, timeout=timeout) as response:
        if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(f"Expected HTML, received {response.headers.get_content_type()}")
        body = response.read(max_bytes + 1)
        if not body or len(body) > max_bytes:
            raise ValueError(f"Empty HTML or size exceeds {max_bytes} bytes")
        return Page(web_url(response.geturl()), body, response.headers.get_content_charset())


def compact(value, length):
    return " ".join(value.split())[:length]


def article_candidates(page, hosts):
    """Send link text and local context to the LLM. URLs always come from the DOM."""
    try:
        parser = lxml_html.HTMLParser(encoding=page.encoding) if page.encoding else None
    except LookupError:
        parser = None
    try:
        tree = lxml_html.fromstring(page.body, parser=parser)
    except etree.ParserError as exc:
        raise ValueError(f"Could not parse overview HTML: {exc}") from exc
    base_href = tree.xpath("string((//base[@href])[1]/@href)")
    base = urljoin(page.url, base_href) if base_href else page.url
    seen = {}
    for anchor in tree.xpath("//a[@href]"):
        href = anchor.get("href").strip()
        if not href or href.startswith("#") or anchor.get("download") is not None:
            continue
        try:
            url = web_url(urljoin(base, href))
        except (ValueError, UnicodeError):
            continue
        if urlsplit(url).hostname not in hosts or url == page.url:
            continue
        path = urlsplit(url).path
        if re.search(r"\.(?:pdf|docx?|xlsx?|csv|zip|jpg|jpeg|webp|png|gif|svg|mp[34]|xml)$", path, re.I):
            continue
        label = compact(anchor.text_content(), 200)
        if not label:
            label = compact(anchor.get("aria-label") or anchor.get("title") or
                            " ".join(anchor.xpath(".//img/@alt")), 200)
        regions = [node.tag for node in anchor.iterancestors()
                   if node.tag in {"main", "article", "nav", "header", "footer", "aside"}]
        block = next((node for node in anchor.iterancestors()
                      if node.tag in {"li", "tr", "p"}), anchor.getparent())
        context = compact(block.text_content(), 225) if block is not None else ""
        candidate = {"url": url, "label": label, "nearby": context, "region": "/".join(reversed(regions))}
        rank = (not any(tag in regions for tag in ("nav", "header", "footer")),
                "main" in regions, "article" in regions, bool(label))
        if url not in seen or rank > seen[url][0]:
            seen[url] = rank, candidate
    title = compact(tree.xpath("string((//h1)[1])") or tree.xpath("string(//title)"), 220)
    return title, [candidate for _, candidate in seen.values()]


def post_json(url, headers, payload, timeout):
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
            result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("Expected a JSON object from the API")
            return result
    except HTTPError as exc:
        raise ValueError(f"API HTTP {exc.code}: {exc.read(600).decode(errors='replace')}") from exc


def parse_model_json(text):
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    result = json.loads(fenced.group(1) if fenced else text)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object from the model")
    return result


class TextModel:
    """Plug in a provider by implementing ask_json(prompt) and a cache identity."""

    def __init__(self, provider, base_url, model, api_key, timeout):
        if provider not in {"gemini", "openai-compatible"}:
            raise ValueError(f"Unsupported LLM provider: {provider}")
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.identity = [provider, self.base_url, model]

    def ask_json(self, prompt):
        if self.provider == "gemini":
            result = post_json(
                f"{self.base_url}/models/{quote(self.model, safe='')}:generateContent",
                {"x-goog-api-key": self.api_key},
                {"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                self.timeout,
            )
            candidates = result.get("candidates", [])
            if not candidates or candidates[0].get("finishReason") not in (None, "STOP"):
                raise ValueError("Gemini API did not return a complete answer")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        else:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            result = post_json(
                f"{self.base_url}/chat/completions", headers,
                {"model": self.model, "messages": [{"role": "user", "content": prompt}]},
                self.timeout,
            )
            choices = result.get("choices", [])
            if not choices or choices[0].get("finish_reason") not in (None, "stop"):
                raise ValueError("Chat Completions API did not return a complete answer")
            text = choices[0].get("message", {}).get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("LLM returned no text")
        return parse_model_json(text)


class ContextScraper:
    def __init__(self, endpoint, api_key, timeout, min_interval=0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.min_interval = min_interval
        self.last_request = None
        self.template = json.loads((BASE / "LotWise_context_request.json").read_text(encoding="utf-8"))
        schema = json.loads((BASE / "LotWise_recall.schema.json").read_text(encoding="utf-8"))
        self.template["jsonParams"]["schema"] = schema
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema)
        if timeout <= self.template["timeoutOpts"]["milliseconds"] / 1000:
            raise ValueError("Context.dev client timeout must exceed its scrape deadline")

    def extract(self, url):
        if self.last_request is not None:
            remaining = self.min_interval - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request = time.monotonic()
        result = post_json(self.endpoint, {"Authorization": f"Bearer {self.api_key}"},
                           {**self.template, "url": url}, self.timeout)
        request_id = result.get("request_id")
        structured = result.get("json") or {}
        if structured.get("success") is not True or structured.get("isTruncated"):
            code = structured.get("error_code") or "incomplete structured result"
            detail = structured.get("message") or ""
            raise ValueError(f"Context.dev request {request_id}: {code} {detail}")
        data = structured.get("data")
        error = next(self.validator.iter_errors(data), None)
        if error:
            raise ValueError(f"Context.dev request {request_id}: invalid schema at {error.json_path}")
        if not data["products"]:
            raise ValueError(f"Context.dev request {request_id}: missing products")
        markdown = result.get("markdown") or {}
        snapshot = markdown.get("data") if markdown.get("success") is True else None
        if not isinstance(snapshot, str) or not snapshot.strip():
            snapshot = None
        metadata = {"context_url": result.get("url"), "metadata": result.get("metadata"),
                    "cache_metadata": result.get("cache_metadata"),
                    "markdown_error": markdown.get("error_code") or markdown.get("message")}
        return data, snapshot, metadata, request_id
