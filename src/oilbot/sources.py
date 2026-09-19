from __future__ import annotations

import base64
import re
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from .clock import instant, stamp, utc_now
from .schema import NewsItem
from .store import Journal


class ParseFailure(ValueError):
    pass


def clean(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header"]):
        node.decompose()
    return soup.get_text(" ", strip=True)


def published(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return instant(value).isoformat()
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else None
        except (ValueError, TypeError):
            return None


def origin(text: str) -> str | None:
    # Attribution is conservative; mentioning a wire does not establish origin.
    for name in ("Reuters", "Bloomberg", "Associated Press", "AFP"):
        if re.search(rf"(?:\(\s*{name}\s*\)|by\s+{name}\b|according to\s+{name}\b)", text, re.I):
            return name.lower().replace(" ", "_")
    return None


class RSSAdapter:
    def parse(self, payload: bytes, url: str, content_type: str) -> list[NewsItem]:
        if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
            raise ParseFailure("XML entities are not permitted")
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ParseFailure("invalid RSS/Atom") from exc
        if root.tag.split("}")[-1] not in {"rss", "feed", "RDF"}:
            raise ParseFailure("not a feed")
        result = []
        for node in root.iter():
            if node.tag.split("}")[-1] not in {"item", "entry"}:
                continue
            fields = {child.tag.split("}")[-1]: child for child in node}
            def text(key):
                child = fields.get(key)
                return "".join(child.itertext()).strip() if child is not None else ""
            link_node = fields.get("link")
            raw_link = (link_node.get("href") or text("link")) if link_node is not None else ""
            link = urljoin(url, raw_link)
            title = clean(text("title"))
            body = clean(text("encoded") or text("content") or text("description") or text("summary"))
            if not title or not (text("guid") or text("id") or raw_link):
                raise ParseFailure("feed item missing identity/title")
            native = text("guid") or text("id") or link
            status = text("status").lower()
            if status not in {"update", "correction", "withdrawal"}:
                status = "correction" if re.match(r"^CORRECTION\b", title, re.I) else "update"
            result.append(NewsItem(native, link, title, title + "\n" + body,
                                   published(text("pubDate") or text("updated") or text("published")),
                                   status=status, origin=origin(title + " " + body)))
        return result


class ListingAdapter:
    def __init__(self, kind: str):
        self.kind = kind

    def parse(self, payload: bytes, url: str, content_type: str) -> list[NewsItem]:
        if "pdf" in content_type:
            # Raw PDF is retained. No fabricated text or confirmation from a filename.
            raise ParseFailure("PDF_TEXT_EXTRACTION_REQUIRED")
        soup = BeautifulSoup(payload, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        page = soup.get_text(" ", strip=True)
        if re.search(r"checking your browser|attention required|access denied|just a moment", page, re.I):
            raise ParseFailure("ACCESS_CHALLENGE")
        is_detail = self.kind == "adnoc" and re.search(r"/press-releases/\d{4}/", url)
        if is_detail:
            main = soup.find("article") or soup.find("main") or soup
            title = soup.find("h1")
            body = main.get_text(" ", strip=True)
            if not title or len(body) < 80:
                raise ParseFailure("incomplete article")
            return [NewsItem(url, url, title.get_text(" ", strip=True), body)]
        result, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = urldefrag(urljoin(url, a["href"]))[0]
            label = a.get_text(" ", strip=True)
            match = (
                self.kind == "adnoc" and bool(re.search(r"/press-releases/\d{4}/", href))
                or self.kind == "fujairah" and (".pdf" in href.lower() or bool(re.search(r"\bNTM\s*\d", label, re.I)))
                or self.kind == "ukmto" and bool(re.search(r"\b(?:WARNING|ADVISORY|UPDATE)\s+\d", label, re.I))
            )
            if not match or href in seen or not label:
                continue
            seen.add(href)
            row = a.find_parent("tr")
            text = row.get_text(" ", strip=True) if row else label
            result.append(NewsItem(href, href, label, text, links=(href,)))
        if not result:
            raise ParseFailure("INCOMPLETE_LISTING: no recognizable publication links")
        return result


def adapter(source: dict):
    return RSSAdapter() if source["adapter"] == "rss" else ListingAdapter(source["adapter"])


def allowed(url: str, source: dict) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in source["allowed_hosts"]
            and parsed.port in (None, 443) and not parsed.username and not parsed.password)


class HTTPFetcher:
    def __init__(self):
        self.sessions: dict[str, requests.Session] = {}

    def __call__(self, source: dict, url: str, cursor: dict) -> dict:
        if not allowed(url, source):
            raise ValueError("unregistered URL")
        session = self.sessions.setdefault(source["id"], requests.Session())
        session.trust_env = False  # Do not inherit proxy credentials or .netrc.
        headers = {"User-Agent": "OilObservationPilot/1.0 (research; 60s minimum polling)"}
        if cursor.get("etag"):
            headers["If-None-Match"] = cursor["etag"]
        if cursor.get("last_modified"):
            headers["If-Modified-Since"] = cursor["last_modified"]
        started = stamp()
        for _ in range(4):
            with session.get(url, headers=headers, timeout=(5, 15), allow_redirects=False, stream=True) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("Location", ""))
                    if not allowed(url, source):
                        raise ValueError("unregistered redirect")
                    continue
                chunks, first = [], None
                size = 0
                for chunk in response.iter_content(65536):
                    if time.monotonic_ns() - started["monotonic_ns"] > 30_000_000_000:
                        raise ValueError("source response exceeded total deadline")
                    if first is None:
                        first = stamp()
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise ValueError("source payload exceeds 8 MiB")
                    chunks.append(chunk)
                return {"url": url, "status": response.status_code,
                        "content_type": response.headers.get("Content-Type", ""),
                        "headers": {k: response.headers[k] for k in ("ETag", "Last-Modified", "Retry-After") if k in response.headers},
                        "started": started, "first_byte": first, "received": stamp(), "body": b"".join(chunks)}
        raise ValueError("redirect limit")


def retry_delay(value: str | None, now: str) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        date = published(value)
        return max(0, (instant(date) - instant(now)).total_seconds()) if date else 0


class NewsCollector:
    def __init__(self, store: Journal, sources: list[dict], fetcher=None):
        self.store, self.sources = store, sources
        self.fetcher = fetcher or HTTPFetcher()

    def poll_once(self, *, force=False) -> dict:
        self.recover_unparsed()
        now = utc_now()
        groups = {}
        summary = {"responses": 0, "revisions": 0, "errors": 0}
        for source in self.sources:
            if not source["enabled"]:
                continue
            cursor = self.store.cursor("source:" + source["id"], {})
            if not force and cursor.get("next_poll") and instant(cursor["next_poll"]) > instant(now):
                continue
            groups.setdefault(urlparse(source["url"]).hostname, []).append(source)

        def domain_worker(sources):
            # Each response is committed *inside* the sequential domain worker.
            # A later slow request cannot delay an earlier durable observation.
            results = []
            for source in sources:
                results.append(self.fetch_source(source))
            return results

        with ThreadPoolExecutor(max_workers=max(1, min(4, len(groups)))) as pool:
            for future in as_completed([pool.submit(domain_worker, group) for group in groups.values()]):
                for result in future.result():
                    for key in summary:
                        summary[key] += result[key]
        return summary

    def recover_unparsed(self):
        """A crash after raw commit must not strand a transient source revision."""
        completed = {rid for row in self.store.records() if row["kind"] in {"parse_receipt", "source_health"}
                     for rid in row["payload"].get("input_revision_ids", [])}
        registry = {source["id"]: source for source in self.sources}
        for row in self.store.records("observation"):
            if row["id"] in completed:
                continue
            payload = row["payload"]
            source = registry.get(payload["source_id"])
            if not source:
                continue
            try:
                if payload["status"] not in {200, 304}:
                    raise ParseFailure("RECOVERED_HTTP_" + str(payload["status"]))
                body = base64.b64decode(payload["body_b64"])
                if payload["status"] == 304:
                    items = []
                elif "pdf" in payload["content_type"]:
                    items = [self.pdf_item(body, payload["url"])]
                else:
                    items = adapter(source).parse(body, payload["url"], payload["content_type"])
                    if source["adapter"] == "adnoc" and not re.search(r"/press-releases/\d{4}/", payload["url"]):
                        items = []
                self.store.accept_items(source, row["id"], items, self.store.cursor("source:" + source["id"], {}))
                state = "RECOVERED_UNPARSED_RESPONSE"
            except (ValueError, subprocess.SubprocessError) as exc:
                state = "PARSE_RECOVERY_FAILED:" + type(exc).__name__
            self.store.append("source_health", {"source_id": source["id"], "status": state, "input_revision_ids": [row["id"]]})

    @staticmethod
    def pdf_item(body: bytes, url: str) -> NewsItem:
        parsed = subprocess.run([sys.executable, "-m", "oilbot.pdftext"], input=body,
                                capture_output=True, timeout=10)
        if parsed.returncode:
            raise ParseFailure("PDF_CAPTURED_TEXT_UNAVAILABLE")
        extracted = json.loads(parsed.stdout)
        return NewsItem(url + "#document", url, extracted["text"].splitlines()[0], extracted["text"])

    def fetch_source(self, source: dict) -> dict:
        cursor = self.store.cursor("source:" + source["id"], {})
        result = {"responses": 0, "revisions": 0, "errors": 0}
        response = None
        observation_id = None
        health = "NETWORK_FAILURE"
        try:
            response = self.fetcher(source, source["url"], cursor)
            observation_id = self.store.capture(source, response)
            result["responses"] += 1
            status = response["status"]
            if status == 304:
                health = "UNCHANGED"
                items = []
            elif status in (401, 403):
                raise ParseFailure("ACCESS_DENIED")
            elif status != 200:
                raise ParseFailure(f"HTTP_{status}")
            else:
                items = adapter(source).parse(response["body"], response["url"], response["content_type"])
                health = "OK" if items else "EMPTY"
            next_cursor = {"etag": response["headers"].get("ETag", cursor.get("etag")),
                           "last_modified": response["headers"].get("Last-Modified", cursor.get("last_modified")),
                           "failures": 0, "next_poll": (instant(utc_now()) + timedelta(seconds=source["poll_seconds"])).isoformat()}
            # Listings are discovery observations. They must not repeatedly replace
            # a full article at the same URL with its shorter headline on every poll.
            accepted = [] if source["adapter"] == "adnoc" else items
            result["revisions"] = len(self.store.accept_items(source, observation_id, accepted, next_cursor))
            # Follow only registered listing links, never arbitrary URLs in prose.
            # Detail fetches happen after the listing commit. Their own failures
            # cannot discard or delay the already captured listing.
            if source["adapter"] in {"adnoc", "fujairah"}:
                self.fetch_details(source, items, result)
        except (requests.RequestException, ValueError) as exc:
            result["errors"] = 1
            health = str(exc) if isinstance(exc, ParseFailure) else type(exc).__name__
            failures = cursor.get("failures", 0) + 1
            delay = min(900, source["poll_seconds"] * 2 ** min(failures, 10))
            if response:
                delay = max(delay, retry_delay(response["headers"].get("Retry-After"), utc_now()))
            with self.store.transaction() as db:
                self.store.set_cursor(db, "source:" + source["id"], {**cursor, "failures": failures,
                    "next_poll": (instant(utc_now()) + timedelta(seconds=delay)).isoformat()})
        if result["errors"] and health == "OK":
            health = "OK_DETAIL_INCOMPLETE"
        self.store.append("source_health", {"source_id": source["id"], "status": health,
                                           "input_revision_ids": [observation_id] if observation_id else []})
        return result

    def fetch_details(self, source: dict, items: list[NewsItem], result: dict):
        links = list(dict.fromkeys(link for item in items for link in item.links if allowed(link, source)))
        queue_key = "details:" + source["id"]
        previous = self.store.cursor(queue_key, {"links": [], "offset": 0})
        links = links or previous["links"]  # A 304 must not strand previously queued detail pages.
        offset = previous["offset"] % max(1, len(links))
        for index in range(min(2, len(links))):
            url = links[(offset + index) % len(links)]
            detail_key = "detail:" + url
            detail_cursor = self.store.cursor(detail_key, {})
            oid = None
            try:
                response = self.fetcher(source, url, detail_cursor)
                oid = self.store.capture(source, response)
                result["responses"] += 1
                if response["status"] == 304:
                    continue
                if response["status"] != 200:
                    raise ParseFailure("DETAIL_HTTP_" + str(response["status"]))
                # The raw document is already durable. Keep its HTTP validators
                # even if it is a scan we cannot interpret, avoiding repeated full
                # downloads. Unfinished parsing is recovered from the raw journal.
                with self.store.transaction() as db:
                    self.store.set_cursor(db, detail_key, {"etag": response["headers"].get("ETag"),
                                                          "last_modified": response["headers"].get("Last-Modified")})
                if "pdf" in response["content_type"]:
                    document = self.pdf_item(response["body"], url)
                    result["revisions"] += len(self.store.accept_items(source, oid, [document],
                                                self.store.cursor("source:" + source["id"], {})))
                else:
                    detail_items = adapter(source).parse(response["body"], url, response["content_type"])
                    # Preserve main source scheduling while committing detail revisions.
                    result["revisions"] += len(self.store.accept_items(source, oid, detail_items,
                                                self.store.cursor("source:" + source["id"], {})))
            except (requests.RequestException, ValueError, subprocess.SubprocessError) as exc:
                result["errors"] += 1
                self.store.append("source_health", {"source_id": source["id"], "status": "DETAIL_FAILED",
                                                    "reason": str(exc) if isinstance(exc, ParseFailure) else type(exc).__name__, "url": url,
                                                    "input_revision_ids": [oid] if oid else []})
        with self.store.transaction() as db:
            self.store.set_cursor(db, queue_key, {"links": links, "offset": offset + min(2, len(links))})
