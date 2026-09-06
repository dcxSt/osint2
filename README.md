# Fieldnotes

A local, evidence-first tool for researching someone's **public professional profile**. Paste a social link or personal website, follow explicit profile connections and relevant same-site pages, and export a sourced brief.

## Run

Requires Python 3.9 or newer. No packages, API keys, paid services, or build step.

```sh
python3 app.py
```

Open **http://127.0.0.1:8765**. Change the port with `--port 9000`.

Or use the CLI:

```sh
python3 app.py --url https://github.com/YOUR_USERNAME --pages 10 --depth 2
python3 app.py --url https://example.com/about --format json --output report.json
```

## What it does

- Fetches real public pages with a bounded breadth-first crawl (1–20 attempted pages, 0–3 link levels).
- Extracts HTML metadata, readable paragraphs, top-level `Person` structured data, explicit `rel="me"` links, and publisher-provided image alt text.
- Follows explicit profile/website links and same-site biography, work, and project pages. Other social links appear separately as unconfirmed candidates; username reuse and social connections are not proof of identity.
- Uses GitHub's public API for individual profiles and up to 12 recently updated owned repositories; excludes forks and private repositories. This is a sample, not a full contribution or authorship history.
- Groups exact source excerpts into work, projects, education, and explicitly stated background. Every excerpt links to its source. If no statement about where someone grew up appears, the tool leaves that unknown.
- Shows a source trail, live activity, cancellation, access failures, and Markdown/JSON exports. Partial results remain exportable.
- Retains up to 20 sessions in server memory. The browser remembers the current session ID for refresh recovery. No database, telemetry, or persistent profile cache; export reports if you want to keep them.

## Boundaries and limitations

This is public professional research, not exhaustive personal surveillance. It does not perform facial identification, reverse-image identity matching, private-address lookup, breach searches, contact harvesting, login bypass, or relationship mapping. Image captions are text supplied by publishers, **not image recognition**. Simple redaction removes common contact details from extracted text; it is not a general-purpose sensitive-data classifier.

The report uses extractive summaries, not an LLM. Source statements may be outdated, incomplete, or wrong. A linked page establishes provenance, not verified identity; review the source trail before relying on the report. Pages can discuss people other than the subject. The tool does not claim to independently verify employment or education.

Many social sites require login or JavaScript, or prohibit automated access. Fieldnotes records these as gaps instead of bypassing them. GitHub and readable personal websites are the strongest starting points. There is no general search-engine discovery, username enumeration, JavaScript browser rendering, PDF extraction, or automated visual analysis in this version.

## Access and network controls

- Checks `robots.txt` on each origin, including redirect destinations; fails closed when permission cannot be established. A missing robots file (404/410) allows fetching.
- Uses an identifiable user agent, at least one second between requests to a host, and supported robots crawl delays/request rates. Long crawl delays produce coverage gaps.
- Allows HTTP(S) on standard ports only. Rejects local/private/reserved IP addresses, revalidates DNS per request, and connects to the validated IP with hostname-verified TLS to prevent DNS rebinding into a private network.
- Bounds response size, redirects, request count, and crawl duration. The three-minute crawl budget is checked between pages; an in-flight network operation can extend it. Stops cooperatively on cancellation.
- Binds only to `127.0.0.1`; checks Host and Origin, rejects cross-site requests, serves a strict content security policy, and renders source text through DOM text nodes.

Do not expose the local development server on a public network without authentication and operational hardening. Sites you research receive normal outbound HTTP requests from your machine.

## Checks

```sh
python3 -B -m unittest discover -s tests -v
node --check static/app.js
```

Tests cover recursive traversal, deduplication, depth/page limits, evidence attribution, unconfirmed profile separation, GitHub extraction, cancellation, robots failures, redirect validation, and private-network protections. Tests use synthetic pages and mocked network responses; the app itself contains no sample reports or simulated research.

## Files

- `app.py`: local HTTP server and CLI.
- `research.py`: URL validation, HTTP transport, robots handling, extraction, traversal, and reports.
- `static/`: responsive interface with progressive results.
- `tests/`: deterministic crawler and security checks.
