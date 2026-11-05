"""Find file-download links in text or on a page the user pasted.

The PC does this part. The addresses it returns are what the PS5 downloads.
Only http and https. One page is read; links on that page are not crawled.
"""

import posixpath
import re
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

UA = "PS5Cable/0.2"
MAX_PROBES = 12
MAX_LINKS = 40
MAX_HTML = 2_000_000
MAX_URL = 8000
TIMEOUT = 15

# Extensions a pasted page is likely offering as a download, not a navigation link.
FILE_EXT = {
    "7z", "apk", "bin", "bz2", "dat", "deb", "dmg", "elf", "exfat", "exe",
    "ffpfs", "ffpfsc", "ffpkg", "fpkg", "gz", "img", "iso", "json", "msi",
    "part", "pdf", "pkg", "png", "rar", "rpm", "tar", "tgz", "txt", "xz", "zip",
}

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_BAD_NAME = re.compile(r'[\x00-\x1f<>:"|?*\\/]')
_ALLOWED_HEADERS = {"referer", "cookie", "user-agent", "authorization"}


def safe_name(name):
    name = unquote(name or "").replace("\x00", "")
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = _BAD_NAME.sub("_", name).strip(" .")
    if name in ("", ".", ".."):
        return "download"
    return name[:180]


def name_from_url(url):
    base = posixpath.basename(unquote(urlparse(url).path or ""))
    return safe_name(base)


def unique_names(links):
    """Give every link its own file name. `links` is modified."""
    seen = set()
    for link in links:
        name = safe_name(link.get("name"))
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        else:
            ext = "." + ext
        candidate = name
        n = 2
        while candidate.lower() in seen:
            candidate = f"{stem}-{n}{ext}"
            n += 1
        seen.add(candidate.lower())
        link["name"] = candidate
    return links


def check_url(url):
    if not isinstance(url, str) or len(url) > MAX_URL or "\n" in url or "\r" in url:
        raise ValueError("link must be an http or https address")
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("link must be an http or https address")
    return url.strip()


def clean_headers(text):
    """Keep only the few headers the payload will forward to the download."""
    out = []
    for line in (text or "").split("\n"):
        line = line.strip().strip("\r")
        if not line or "\r" in line or "\n" in line or ":" not in line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name not in _ALLOWED_HEADERS:
            continue
        out.append(line)
        if len(out) == 8:
            break
    blob = "\n".join(out)
    return blob if len(blob) <= 8000 else ""


def _ext(url):
    base = posixpath.basename(urlparse(url).path or "").lower()
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1]


def looks_like_file(url):
    return _ext(url) in FILE_EXT


class _Page(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.found = []

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): v for k, v in attrs if k}
        if tag == "meta" and (d.get("http-equiv") or "").lower() == "refresh":
            content = d.get("content") or ""
            m = re.search(r"url\s*=\s*([^;]+)", content, re.I)
            if m:
                self.found.append((urljoin(self.base, m.group(1).strip(" '\"")), None))
            return
        href = d.get("href") if tag in ("a", "area") else None
        if tag in ("source", "video", "audio") and d.get("src"):
            href = d.get("src")
        if not href or href.startswith(("#", "javascript:", "mailto:", "data:")):
            return
        self.found.append((urljoin(self.base, href), d.get("download")))


def _opener(jar):
    # Direct connection. A proxy env var would send 127.0.0.1 at the proxy
    # during tests, and the PS5 will not be using this PC's proxy anyway.
    return build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))


def _cookie_header(jar, url):
    req = Request(url)
    jar.add_cookie_header(req)
    return req.get_header("Cookie")


def _disposition_name(value):
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", value)
    if m:
        return safe_name(unquote(m.group(1).strip()))
    m = re.search(r'filename\s*=\s*"([^"]+)"', value) or re.search(
        r"filename\s*=\s*([^;]+)", value)
    if m:
        return safe_name(m.group(1).strip())
    return None


def _headers_for(url, jar, referer):
    lines = []
    if referer and referer != url:
        ref = referer.replace("\r", "").replace("\n", "")
        if ref:
            lines.append("Referer: " + ref)
    cookie = _cookie_header(jar, url)
    if cookie and "\n" not in cookie and "\r" not in cookie:
        lines.append("Cookie: " + cookie)
    return clean_headers("\n".join(lines))


def _html_type(ctype):
    return (ctype or "").split(";", 1)[0].strip().lower() in ("text/html", "application/xhtml+xml")


def _sniff_html(body):
    return body.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<a "))


def _fetch_info(url, jar, method="GET", read_html=False):
    """Headers, and the body only when the response is an HTML page.

    A file is not saved on this PC. `None` means HEAD is not supported.
    """
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"}, method=method)
    try:
        opened = _opener(jar).open(req, timeout=TIMEOUT)
    except HTTPError as e:
        if method == "HEAD" and e.code in (403, 405, 501):
            return None
        raise
    with opened as resp:
        ctype = resp.headers.get("Content-Type") or ""
        body = b""
        if read_html and _html_type(ctype):
            body = resp.read(MAX_HTML)
        elif read_html and not ctype:
            sniff = resp.read(256)
            if _sniff_html(sniff):
                body = sniff + resp.read(MAX_HTML - len(sniff))
        length = resp.headers.get("Content-Length")
        try:
            size = int(length) if length is not None else None
        except ValueError:
            size = None
        if size is not None and size < 0:
            size = None
        return {
            "url": resp.geturl(),
            "type": ctype,
            "disposition": resp.headers.get("Content-Disposition") or "",
            "size": size,
            "body": body,
        }


def _is_html(info):
    return _html_type(info["type"]) or (not info["type"] and _sniff_html(info["body"]))


def _as_link(info, jar, referer, name=None):
    disp = _disposition_name(info.get("disposition"))
    return {
        "url": info["url"],
        "name": disp or name or name_from_url(info["url"]),
        "size": info.get("size"),
        "headers": _headers_for(info["url"], jar, referer),
    }


def _probe(url):
    """Return (links, errors) for one address. `links` is a list of dicts."""
    url = check_url(url)
    jar = CookieJar()
    try:
        info = _fetch_info(url, jar, method="HEAD")
        if info is None or _is_html(info) or not info["type"]:
            info = _fetch_info(url, jar, method="GET", read_html=True)
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        return [], [f"{url}: {e}"]
    if info is None:
        return [], [f"{url}: no response"]

    if not _is_html(info):
        return [_as_link(info, jar, None)], []

    page = _Page(info["url"])
    try:
        page.feed(info["body"].decode("utf-8", "replace"))
    except Exception:
        return [], [f"{info['url']}: could not read the page"]

    links = []
    seen = set()
    for href, download in page.found:
        try:
            href = check_url(href)
        except ValueError:
            continue
        if href in seen:
            continue
        forced = isinstance(download, str) or download == ""
        if not forced and not looks_like_file(href):
            continue
        seen.add(href)
        name = safe_name(download) if isinstance(download, str) and download else name_from_url(href)
        links.append({
            "url": href,
            "name": name,
            "size": None,
            "headers": _headers_for(href, jar, info["url"]),
        })
        if len(links) >= MAX_LINKS:
            break
    if not links:
        return [], [f"No file links on {info['url']}. Paste the file's own address."]

    for link in links[:MAX_PROBES]:
        try:
            head = _fetch_info(link["url"], jar, method="HEAD")
        except (HTTPError, URLError, TimeoutError, OSError):
            continue
        if not head:
            continue
        link["url"] = head["url"]
        if head.get("size") is not None:
            link["size"] = head["size"]
        disp = _disposition_name(head.get("disposition"))
        if disp:
            link["name"] = disp
        link["headers"] = _headers_for(link["url"], jar, info["url"])
    return links, []


def extract_urls(text):
    found = []
    seen = set()
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(").,;]>\"'")
        try:
            url = check_url(url)
        except ValueError:
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def resolve(text):
    """Read `text` (a link, several links, or a blob that contains them).

    Returns {"links": [{url, name, size, headers}], "errors": [str]}.
    """
    urls = extract_urls(text)
    if not urls and (text or "").strip():
        try:
            urls = [check_url((text or "").strip())]
        except ValueError:
            return {"links": [], "errors": ["Paste an http or https link."]}
    if not urls:
        return {"links": [], "errors": ["Paste an http or https link."]}

    links = []
    errors = []
    seen = set()
    for url in urls[:MAX_PROBES]:
        found, errs = _probe(url)
        errors.extend(errs)
        for link in found:
            if link["url"] in seen:
                continue
            seen.add(link["url"])
            links.append(link)
            if len(links) >= MAX_LINKS:
                break
        if len(links) >= MAX_LINKS:
            break
    return {"links": unique_names(links), "errors": errors}
