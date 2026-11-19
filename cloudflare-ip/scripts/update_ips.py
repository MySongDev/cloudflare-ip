"""Collect both public sources into cloudflare.txt, without a browser or dependencies."""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

PLAIN_SOURCE = "https://ip.164746.xyz/ipTop10.html"
UOUIN_PAGE = "https://api.uouin.com/cloudflare.html"
UOUIN_API = "https://api.uouin.com/index.php/index/Cloudflare"
GROUPS = ("ctcc", "cucc", "cmcc", "bgp")
TIMEOUT = 25
ATTEMPTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Public signing constants from the page JS supplied and inspected on 2026-11-20.
# The per-page id and per-request timestamp are discovered/generated at runtime.
SIGN_TEXT = "xj3jxQT5ux4cqapDGIP3nQg6BoH3QzTL"
SIGN_SEPARATOR = "IAGvmXaAqAppIw6RwjDct9o3rW4KwPjw"


class SourceError(RuntimeError):
    """A source failed or did not contain a complete, valid IP list."""


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def make_key(salt_id: str, timestamp_ms: int) -> str:
    return md5_hex(md5_hex(SIGN_TEXT) + SIGN_SEPARATOR + md5_hex(salt_id) + str(timestamp_ms))


class ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        src = dict(attrs).get("src") or ""
        parts = urlsplit(src)
        if parts.path.rsplit("/", 1)[-1] == "cloudflare.js":
            self.ids.update(parse_qs(parts.query, keep_blank_values=True).get("id", []))


def extract_salt_id(page: str) -> str:
    parser = ScriptParser()
    parser.feed(page)
    if len(parser.ids) != 1:
        raise SourceError("uouin page has no unambiguous cloudflare.js id")
    salt_id = next(iter(parser.ids))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", salt_id):
        raise SourceError("uouin page has an invalid cloudflare.js id")
    return salt_id


def validate_ipv4(value: object) -> str:
    if not isinstance(value, str):
        raise SourceError("source returned an IP that is not a string")
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ipaddress.AddressValueError as exc:
        raise SourceError("source returned an invalid IPv4 address") from exc


def parse_plain_ips(text: str) -> list[str]:
    tokens = re.split(r"[,\s]+", html.unescape(text).strip().lstrip("\ufeff").strip())
    if not any(tokens):
        raise SourceError("ipTop10 source returned an empty list")
    return [validate_ipv4(token) for token in tokens if token]


def parse_uouin(payload: object) -> list[str]:
    if not isinstance(payload, dict) or str(payload.get("code")) != "200":
        raise SourceError("uouin API did not report success")
    if str(payload.get("statu", "")).lower() != "true":
        raise SourceError("uouin API returned a failed status")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SourceError("uouin API has no data object")
    ips: list[str] = []
    for name in GROUPS:
        group = data.get(name)
        if not isinstance(group, dict) or str(group.get("code")) != "200":
            raise SourceError(f"uouin category {name} is missing or failed")
        records = group.get("info")
        if not isinstance(records, list) or not records:
            raise SourceError(f"uouin category {name} has no IP records")
        for record in records:
            if not isinstance(record, dict):
                raise SourceError(f"uouin category {name} contains an invalid record")
            ips.append(validate_ipv4(record.get("ip")))
    return ips


class HttpClient:
    def __init__(self) -> None:
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def get_text(self, url: str, *, referer: str | None = None, api: bool = False) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; CloudflareIPAggregator/1.0)",
            "Accept": "application/json, text/javascript, */*; q=0.01" if api else "*/*",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        if api:
            headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            with self.opener.open(Request(url, headers=headers), timeout=TIMEOUT) as response:
                if response.getcode() != 200:
                    raise SourceError(f"HTTP response was {response.getcode()}, expected 200")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SourceError("HTTP response exceeded the size limit")
                encoding = response.headers.get_content_charset() or "utf-8"
                return body.decode(encoding).lstrip("\ufeff")
        except (URLError, OSError, UnicodeError, LookupError) as exc:
            # Avoid logging full signed URLs or cookies.
            raise SourceError(f"HTTP request failed: {exc}") from exc


def fetch_uouin(client: HttpClient) -> list[str]:
    page_url = UOUIN_PAGE + "?" + urlencode({"_": int(time.time() * 1000)})
    salt_id = extract_salt_id(client.get_text(page_url))
    timestamp_ms = int(time.time() * 1000)
    api_url = UOUIN_API + "?" + urlencode({
        "key": make_key(salt_id, timestamp_ms), "time": timestamp_ms,
    })
    text = client.get_text(api_url, referer=UOUIN_PAGE, api=True)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError("uouin API response is not valid JSON") from exc
    return parse_uouin(payload)


def retry_source(name: str, operation) -> list[str]:
    for attempt in range(ATTEMPTS):
        try:
            return operation()
        except SourceError as exc:
            if attempt == ATTEMPTS - 1:
                raise SourceError(f"{name} failed after {ATTEMPTS} attempts: {exc}") from exc
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def collect_ips(client: HttpClient) -> list[str]:
    plain = retry_source("ipTop10", lambda: parse_plain_ips(client.get_text(PLAIN_SOURCE)))
    uouin = retry_source("uouin", lambda: fetch_uouin(client))
    return plain + uouin


def render_ips(ips: list[str]) -> str:
    unique = dict.fromkeys(validate_ipv4(ip) for ip in ips)
    if not unique:
        raise SourceError("refusing to publish an empty IP list")
    return "".join(f"{ip}:443\n" for ip in unique)


def write_output(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("cloudflare.txt"))
    args = parser.parse_args(argv)
    try:
        content = render_ips(collect_ips(HttpClient()))
        write_output(args.output, content)
    except (SourceError, OSError) as exc:
        print(f"IP update failed; existing TXT was preserved: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
