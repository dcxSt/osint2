"""Bounded, evidence-first public profile research. Python 3.9+, stdlib only."""
import collections
import datetime
import gzip
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser

AGENT = "FieldnotesBot"
USER_AGENT = "FieldnotesBot/1.0 (public professional profile research; respects robots.txt)"
MAX_BYTES = 1_500_000
SOCIAL = {"github.com", "gitlab.com", "linkedin.com", "www.linkedin.com", "bsky.app", "twitter.com", "x.com", "medium.com", "www.youtube.com", "youtube.com", "dev.to", "www.behance.net", "dribbble.com", "orcid.org"}
ASSETS = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|pdf|zip|mp4|mp3|css|js|ico|woff2?)(?:$|\?)", re.I)
NO_PATHS = re.compile(r"/(?:login|signin|signup|logout|search|settings|messages|intent|share|privacy|terms|followers|following|stargazers)(?:/|$)", re.I)
PAGE_WORDS = re.compile(r"\b(?:about|bio|biography|work|projects|portfolio|publications|research|experience|writing|resume|résumé|cv)\b", re.I)
OFFICIAL_WORDS = re.compile(r"^(?:my |personal |official )?(?:website|homepage|home page|site|portfolio|blog)$", re.I)
SENSITIVE = re.compile(r"\b(?:diagnosed|diagnosis|religion|religious|sexual orientation|sexuality|married|spouse|children|date of birth|born on|home address|voted for|political affiliation)\b", re.I)


class FetchError(Exception):
    pass


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def public_text(text):
    """Do not turn contact details into report evidence."""
    text = clean(text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[email omitted]", text)
    text = re.sub(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)", "[number omitted]", text)
    text = re.sub(r"\b\d{1,6}\s+(?:[\w.-]+\s+){0,5}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd)\b\.?", "[street address omitted]", text, flags=re.I)
    return text


def is_public_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return is_public_ip(str(address.ipv4_mapped))
    if isinstance(address, ipaddress.IPv4Address):
        if any(address in ipaddress.ip_network(net) for net in ("192.0.0.0/24", "192.88.99.0/24")):
            return False
    return True


def normalize_url(value, base=None):
    if not isinstance(value, str) or len(value) > 2048:
        raise FetchError("Invalid or overly long URL")
    value = value.strip()
    if any(ord(c) < 32 for c in value) or "\\" in value:
        raise FetchError("Invalid URL characters")
    if base:
        value = urllib.parse.urljoin(base, value)
    elif "://" not in value:
        value = "https://" + value
    try:
        p = urllib.parse.urlsplit(value)
        host = (p.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = p.port
    except (ValueError, UnicodeError):
        raise FetchError("Invalid URL")
    if p.scheme not in ("http", "https") or not host or p.username or p.password:
        raise FetchError("Use a public HTTP(S) URL without credentials")
    if port and port != (443 if p.scheme == "https" else 80):
        raise FetchError("Only standard web ports are supported")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise FetchError("Local and private hosts cannot be crawled")
    try:
        if not is_public_ip(host):
            raise FetchError("Private network addresses cannot be crawled")
    except ValueError:
        pass
    netloc = "[" + host + "]" if ":" in host else host
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}])
    path = urllib.parse.quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((p.scheme, netloc, path, query, ""))


def public_addresses(host, port):
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError:
        raise FetchError("DNS lookup failed")
    if not addresses or any(not is_public_ip(ip) for ip in addresses):
        raise FetchError("Host resolves to a private or reserved network")
    return addresses


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure):
        super().__init__(host, port, timeout=9)
        self.address, self.secure = address, secure

    def connect(self):
        # Connect to the checked IP, retaining the hostname for TLS and Host.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


class Fetcher:
    def __init__(self, cancel=None):
        self.cancel = cancel or threading.Event()
        self.robots = {}
        self.last_request = {}
        self.requests = 0

    def raw(self, url):
        if self.cancel.is_set():
            raise FetchError("Cancelled")
        if self.requests >= 100:
            raise FetchError("Request budget reached")
        self.requests += 1
        url = normalize_url(url)
        p = urllib.parse.urlsplit(url)
        port = 443 if p.scheme == "https" else 80
        addresses = public_addresses(p.hostname, port)
        elapsed = time.monotonic() - self.last_request.get(p.hostname, 0)
        if elapsed < 1 and self.cancel.wait(1 - elapsed):
            raise FetchError("Cancelled")
        self.last_request[p.hostname] = time.monotonic()
        conn = PinnedHTTP(p.hostname, port, addresses[0], p.scheme == "https")
        try:
            conn.request("GET", p.path + (("?" + p.query) if p.query else ""), headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,application/xhtml+xml,text/plain;q=0.8", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            chunks = []
            size = 0
            deadline = time.monotonic() + 15
            while size <= MAX_BYTES:
                if self.cancel.is_set():
                    raise FetchError("Cancelled")
                if time.monotonic() > deadline:
                    raise FetchError("Response read deadline exceeded")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_BYTES:
                raise FetchError("Response exceeds the 1.5 MB limit")
            if response.getheader("Content-Encoding", "").lower() == "gzip":
                import io
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                    data = stream.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise FetchError("Decompressed response exceeds the size limit")
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), data
        except (OSError, http.client.HTTPException, EOFError) as exc:
            raise FetchError("Connection failed: " + type(exc).__name__)
        finally:
            conn.close()

    def allowed(self, url):
        p = urllib.parse.urlsplit(url)
        origin = p.scheme + "://" + p.netloc
        if origin not in self.robots:
            target = origin + "/robots.txt"
            try:
                for _ in range(4):
                    status, headers, data = self.raw(target)
                    if status in (301, 302, 303, 307, 308):
                        target = normalize_url(headers.get("location", ""), target)
                        # A cross-origin robots redirect is ambiguous. Fail closed.
                        if urllib.parse.urlsplit(target).netloc != p.netloc:
                            raise FetchError("Cross-origin robots redirect")
                        continue
                    break
                if status in (404, 410):
                    self.robots[origin] = None
                elif status == 200 and "<html" not in data[:512].decode("utf-8", "ignore").lower():
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(data.decode("utf-8", "replace").splitlines())
                    self.robots[origin] = parser
                else:
                    self.robots[origin] = False
            except FetchError:
                self.robots[origin] = False
        rules = self.robots[origin]
        if rules is False:
            raise FetchError("Could not establish robots.txt permission")
        if rules is not None:
            if not rules.can_fetch(AGENT, url):
                raise FetchError("Disallowed by robots.txt")
            delay = rules.crawl_delay(AGENT) or rules.crawl_delay("*") or 1
            rate = rules.request_rate(AGENT) or rules.request_rate("*")
            if rate and rate.requests:
                delay = max(delay, rate.seconds / rate.requests)
            if delay > 30:
                raise FetchError("Site crawl delay exceeds this tool's time budget")
            remaining = delay - (time.monotonic() - self.last_request.get(p.hostname, 0))
            if remaining > 0 and self.cancel.wait(remaining):
                raise FetchError("Cancelled")

    def get(self, url):
        for _ in range(6):
            url = normalize_url(url)
            self.allowed(url)
            status, headers, data = self.raw(url)
            if status in (301, 302, 303, 307, 308):
                if not headers.get("location"):
                    raise FetchError("Redirect without a destination")
                url = normalize_url(headers["location"], url)
                continue
            if status != 200:
                raise FetchError({401: "Login required", 403: "Site denied automated access", 429: "Site rate limit reached"}.get(status, "HTTP " + str(status)))
            content_type = headers.get("content-type", "")
            if not any(t in content_type for t in ("text/html", "application/xhtml+xml", "application/json", "text/plain")):
                raise FetchError("Unsupported content type")
            charset = re.search(r"charset=([\w-]+)", content_type)
            encoding = charset.group(1) if charset else "utf-8"
            try:
                body = data.decode(encoding, "replace")
            except LookupError:
                body = data.decode("utf-8", "replace")
            return url, body, content_type
        raise FetchError("Too many redirects")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.meta = {}
        self.links = []
        self.paragraphs = []
        self.images = []
        self.schemas = []
        self._title = False
        self._skip = 0
        self._anchor = None
        self._block = None
        self._script = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._script = [] if a.get("type", "").lower() == "application/ld+json" else None
        if tag in ("script", "style", "noscript", "nav", "footer", "form", "svg"):
            self._skip += 1
        if tag == "meta":
            key = a.get("property", a.get("name", "")).lower()
            self.meta[key] = clean(a.get("content", ""))[:2000]
        if tag == "link" and "me" in a.get("rel", "").split():
            self.links.append({"href": a.get("href", ""), "text": "Linked identity", "rel": "me"})
        # Navigation/footer links often contain the actual profile connections.
        if tag == "a":
            self._anchor = {"href": a.get("href", ""), "text": "", "rel": a.get("rel", "")}
        if self._skip:
            return
        if tag == "title":
            self._title = True
        if tag in ("p", "li", "h1", "h2", "h3", "blockquote"):
            self._flush_block()
            self._block = []
        if tag == "img" and a.get("alt"):
            self.images.append(public_text(a["alt"])[:250])

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            try:
                self.schemas.append(json.loads("".join(self._script)))
            except (ValueError, RecursionError):
                pass
            self._script = None
        if tag in ("script", "style", "noscript", "nav", "footer", "form", "svg"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._anchor:
            self._anchor["text"] = clean(self._anchor["text"])[:200]
            self.links.append(self._anchor)
            self._anchor = None
        if self._skip:
            return
        if tag == "title":
            self._title = False
        if tag in ("p", "li", "h1", "h2", "h3", "blockquote"):
            self._flush_block()

    def _flush_block(self):
        if self._block:
            value = public_text(" ".join(self._block))
            if 35 <= len(value) <= 1600:
                self.paragraphs.append(value)
        self._block = None

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self._anchor is not None:
            self._anchor["text"] += data + " "
        if self._skip:
            return
        if self._title:
            self.title.append(data)
        if self._block is not None:
            self._block.append(data)


def people_in_schema(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, list):
        for item in value[:100]:
            yield from people_in_schema(item, depth + 1)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if types == "Person" or isinstance(types, list) and "Person" in types:
            yield value
        # Authors and mentioned people must not become the subject's identity.
        for key in ("@graph", "mainEntity"):
            if key in value:
                yield from people_in_schema(value[key], depth + 1)


def source_from_html(url, body):
    parser = PageParser()
    parser.feed(body)
    parser._flush_block()
    people = [p for schema in parser.schemas for p in people_in_schema(schema)]
    title = public_text(parser.meta.get("og:title") or " ".join(parser.title))[:250]
    description = public_text(parser.meta.get("description") or parser.meta.get("og:description") or "")
    # Only a sole top-level Person is usable for identity links.
    person = people[0] if len(people) == 1 else {}
    name = public_text(person.get("name", "")) if isinstance(person.get("name", ""), str) else ""
    links = parser.links
    same_as = person.get("sameAs", [])
    if isinstance(same_as, str):
        same_as = [same_as]
    for item in same_as if isinstance(same_as, list) else []:
        if isinstance(item, str):
            links.append({"href": item, "text": "Structured profile link", "rel": "me"})
    if isinstance(person.get("url"), str):
        links.append({"href": person["url"], "text": "Personal website", "rel": "me"})
    if isinstance(person.get("description"), str):
        parser.paragraphs.insert(0, public_text(person["description"]))
    paragraphs = list(dict.fromkeys(([description] if description else []) + parser.paragraphs))[:100]
    return {"url": url, "title": title or urllib.parse.urlsplit(url).hostname, "name": name, "description": description[:1200], "paragraphs": paragraphs, "links": links, "image_captions": list(dict.fromkeys(parser.images))[:12], "adapter": "Public HTML"}


def github_profile(url, fetcher):
    p = urllib.parse.urlsplit(url)
    parts = p.path.strip("/").split("/")
    if p.hostname not in ("github.com", "www.github.com") or len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", parts[0]):
        return None
    api = "https://api.github.com/users/" + parts[0]
    actual, body, _ = fetcher.get(api)
    data = json.loads(body)
    if data.get("type") != "User":
        raise FetchError("This GitHub URL is not an individual profile")
    links = []
    if data.get("blog"):
        try:
            links.append({"href": normalize_url(data["blog"]), "text": "Personal website", "rel": "me"})
        except FetchError:
            pass
    bio = public_text(data.get("bio", ""))
    name = public_text(data.get("name") or data.get("login", ""))
    paragraphs = [bio] if bio else []
    if data.get("company"):
        paragraphs.append("Profile company field: " + public_text(data["company"]))
    return {"url": url, "data_url": actual, "title": name + " · GitHub", "name": name, "description": bio, "paragraphs": paragraphs, "links": links, "image_captions": [], "adapter": "GitHub public API"}


def github_work(url, fetcher):
    user = urllib.parse.urlsplit(url).path.strip("/")
    api = "https://api.github.com/users/" + user + "/repos?sort=updated&per_page=12&type=owner"
    actual, body, _ = fetcher.get(api)
    repos = json.loads(body)
    work = []
    for repo in repos if isinstance(repos, list) else []:
        if repo.get("fork") or repo.get("private"):
            continue
        work.append({"title": public_text(repo.get("name", "")), "url": normalize_url(repo["html_url"]), "description": public_text(repo.get("description", ""))[:400], "language": repo.get("language"), "stars": repo.get("stargazers_count", 0), "updated": repo.get("updated_at", "")[:10], "evidence_url": actual})
    return work


def link_candidates(source, depth):
    current = urllib.parse.urlsplit(source["url"])
    seen = set()
    for link in source["links"]:
        href = link.get("href", "")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            url = normalize_url(href, source["url"])
        except FetchError:
            continue
        p = urllib.parse.urlsplit(url)
        if url in seen or ASSETS.search(p.path) or NO_PATHS.search(p.path) or p.query:
            continue
        seen.add(url)
        same_host = p.hostname.removeprefix("www.") == current.hostname.removeprefix("www.")
        text = link.get("text", "")
        explicit = "me" in link.get("rel", "").split()
        # Do not treat social networks' navigation or other accounts as this person.
        social = current.hostname.removeprefix("www.") in {h.removeprefix("www.") for h in SOCIAL}
        if explicit:
            yield url, "Explicit profile link", 0
        elif same_host and not social and PAGE_WORDS.search(text + " " + p.path):
            yield url, "Same-site biography or work page", 1
        elif not same_host and OFFICIAL_WORDS.fullmatch(clean(text)):
            yield url, "Website link published on source", 2
        elif not same_host and p.hostname in SOCIAL and len(p.path.strip("/").split("/")) <= 2:
            yield url, "Possible profile — not followed automatically", 3


CATEGORIES = [
    ("Background", re.compile(r"\b(?:grew up|raised in|originally from|hometown)\b", re.I)),
    ("Education", re.compile(r"\b(?:graduated|studied|degree|bachelor|master.s|ph\.?d|alumn|university|college)\b", re.I)),
    ("Work & experience", re.compile(r"\b(?:work(?:s|ed|ing)?|company|found(?:er|ed)|engineer|designer|developer|researcher|professor|artist|writer|director|built|building|lead(?:ing)?|co-founder|creator)\b", re.I)),
    ("Projects & writing", re.compile(r"\b(?:project|published|publication|paper|book|open.source|software|created|maintain|research|writing)\b", re.I)),
]


def make_summary(sources):
    groups = collections.OrderedDict((name, []) for name, _ in CATEGORIES)
    groups["Profile overview"] = []
    seen = set()
    for source in sources:
        for paragraph in source["paragraphs"]:
            if len(paragraph) < 25 or SENSITIVE.search(paragraph):
                continue
            key = paragraph.lower().strip(" .")
            if key in seen:
                continue
            seen.add(key)
            category = next((name for name, pattern in CATEGORIES if pattern.search(paragraph)), None)
            if category is None and paragraph == source["description"]:
                category = "Profile overview"
            if not category or len(groups[category]) >= 6:
                continue
            # Extracts preserve the source's wording instead of inventing a biography.
            groups[category].append({"text": paragraph[:900] + ("…" if len(paragraph) > 900 else ""), "source_id": source["id"], "source_url": source["url"], "kind": "Source excerpt"})
    return [{"title": name, "items": items} for name, items in groups.items() if items]


@dataclass
class Job:
    id: str
    seed: str
    max_pages: int
    max_depth: int
    status: str = "queued"
    events: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    work: list = field(default_factory=list)
    summary: list = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    created: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def event(self, message):
        with self.lock:
            self.events.append({"time": datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S"), "message": message})

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps({"id": self.id, "seed": self.seed, "status": self.status, "created": self.created, "limits": {"pages": self.max_pages, "depth": self.max_depth}, "events": self.events, "sources": self.sources, "gaps": self.gaps, "candidates": self.candidates, "work": self.work, "summary": self.summary, "name": next((s["name"] for s in self.sources if s["name"]), "Public profile"), "method": "Extractive summary of public sources. Profile links indicate provenance, not independently verified identity. Image captions are publisher-provided text; no face matching or identity inference."}))


def run_job(job, fetcher=None):
    fetcher = fetcher or Fetcher(job.cancel)
    with job.lock:
        job.status = "running"
    job.event("Starting public-source research")
    queue = collections.deque([(job.seed, 0, None, "Provided profile")])
    visited = set()
    started = time.monotonic()
    try:
        while queue and len(visited) < job.max_pages and not job.cancel.is_set():
            if time.monotonic() - started > 180:
                job.event("Three-minute crawl budget reached")
                break
            url, depth, parent, relationship = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            job.event("Reading " + url)
            try:
                source = github_profile(url, fetcher)
                if source is None:
                    actual, body, content_type = fetcher.get(url)
                    if "html" not in content_type:
                        raise FetchError("No readable HTML profile at this URL")
                    source = source_from_html(actual, body)
                if not source["paragraphs"] and source["adapter"] != "GitHub public API":
                    raise FetchError("No readable profile text; page may require JavaScript or login")
                source.update({"id": "S" + str(len(job.sources) + 1), "depth": depth, "parent": parent, "relationship": relationship, "retrieved": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                if any(s["url"] == source["url"] for s in job.sources):
                    continue
                with job.lock:
                    job.sources.append(source)
                    job.summary = make_summary(job.sources)
                job.event("Saved " + source["id"] + ": " + source["title"])
                if source["adapter"] == "GitHub public API":
                    try:
                        work = github_work(url, fetcher)
                        with job.lock:
                            job.work.extend(work)
                    except (FetchError, ValueError, KeyError) as exc:
                        with job.lock:
                            job.gaps.append({"url": url + "?tab=repositories", "reason": "Repository coverage: " + str(exc)})
                candidates = sorted(link_candidates(source, depth), key=lambda x: x[2])
                for target, reason, priority in candidates[:30]:
                    if target in visited:
                        continue
                    if priority == 3:
                        with job.lock:
                            if not any(c["url"] == target for c in job.candidates):
                                job.candidates.append({"url": target, "from": source["id"], "reason": reason})
                    elif depth < job.max_depth and len(queue) < 80:
                        queue.append((target, depth + 1, source["id"], reason))
            except (FetchError, ValueError, KeyError, RecursionError) as exc:
                if not job.cancel.is_set():
                    with job.lock:
                        job.gaps.append({"url": url, "reason": str(exc)})
                    job.event("Coverage gap: " + str(exc))
        with job.lock:
            job.status = "cancelled" if job.cancel.is_set() else "complete"
        if queue and not job.cancel.is_set():
            job.event("Stopped at configured crawl limits; additional links remain")
        job.event("Research " + job.status + ": " + str(len(job.sources)) + " sources")
    except Exception:
        with job.lock:
            job.status = "error"
        job.event("Research stopped after an unexpected error; collected evidence remains available")


def markdown_report(report):
    def escape(value):
        return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", str(value)).replace("\n", " ")
    lines = ["# " + escape(report["name"]), "", "Seed: " + report["seed"], "", "Created: " + report["created"], "", report["method"], ""]
    for group in report["summary"]:
        lines.extend(["## " + group["title"], ""])
        for item in group["items"]:
            lines.extend(["> " + escape(item["text"]), "", "Source: [" + item["source_id"] + "](<" + item["source_url"] + ">)", ""])
    if report["work"]:
        lines.extend(["## Public repositories", ""])
        for work in report["work"]:
            lines.append("- [" + escape(work["title"]) + "](<" + work["url"] + ">): " + escape(work["description"]))
    lines.extend(["", "## Sources", ""])
    for source in report["sources"]:
        lines.append("- " + source["id"] + " [" + escape(source["title"]) + "](<" + source["url"] + ">) — " + source["relationship"] + "; retrieved " + source["retrieved"])
    lines.extend(["", "## Coverage gaps", ""])
    lines.extend("- " + escape(gap["url"]) + ": " + escape(gap["reason"]) for gap in report["gaps"])
    lines.extend(["", "## Unconfirmed profile links", ""])
    lines.extend("- " + escape(item["url"]) + " (from " + item["from"] + "; not merged into the summary)" for item in report["candidates"])
    return "\n".join(lines) + "\n"
