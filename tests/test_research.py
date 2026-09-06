import json
import socket
import threading
import unittest
from unittest.mock import patch

from research import (Fetcher, FetchError, Job, PinnedHTTP, github_profile,
                      github_work, is_public_ip, link_candidates, make_summary,
                      markdown_report, normalize_url, public_addresses,
                      public_text, run_job, source_from_html)


PROFILE = '''<html><head><title>Alex Example — engineer</title>
<meta name="description" content="I am Alex Example, an engineer building accessible tools.">
<script type="application/ld+json">{"@type":"Person","name":"Alex Example","sameAs":["https://other.example/alex"]}</script>
</head><body><nav><a href="/about">About</a></nav>
<p>I grew up in Bristol and studied computer science at Example University.</p>
<p>I built an open source tool for collaborative software development.</p>
<a href="https://github.com/somebody">A colleague</a>
<a href="/login">Login</a><a href="/projects/archive.pdf">Projects PDF</a>
<img src="portrait.jpg" alt="Alex speaking about accessible software">
<footer><a rel="me" href="https://third.example/alex">My profile</a></footer>
<script>my secret javascript must not be indexed</script></body></html>'''


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise FetchError("HTTP 404")
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return url, value, "text/html"


class URLSecurityTests(unittest.TestCase):
    def test_private_and_non_web_urls_rejected(self):
        for url in ["http://127.0.0.1", "http://10.2.3.4", "http://169.254.169.254", "http://[::1]", "http://224.0.0.1", "http://localhost", "https://test.local", "https://example.com:8080", "https://user:pass@example.com", "file:///etc/passwd", "https://example.com\\@127.0.0.1", "https://example.com\nHost: local"]:
            with self.subTest(url=url), self.assertRaises(FetchError):
                normalize_url(url)

    def test_normalization(self):
        self.assertEqual(normalize_url("Example.COM/alex?utm_source=thing&tab=work#bio"), "https://example.com/alex?tab=work")
        self.assertEqual(normalize_url("/about", "https://example.com/alex"), "https://example.com/about")

    def test_mixed_dns_results_rejected(self):
        results = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)), (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('research.socket.getaddrinfo', return_value=results), self.assertRaises(FetchError):
            public_addresses("example.com", 443)

    def test_pinned_connection_uses_validated_ip(self):
        conn = PinnedHTTP("example.com", 80, "93.184.216.34", False)
        with patch('research.socket.create_connection') as connect:
            conn.connect()
            connect.assert_called_once_with(("93.184.216.34", 80), 9)

    def test_special_ranges(self):
        for ip in ["100.64.0.1", "192.0.0.8", "192.88.99.1", "::ffff:127.0.0.1", "ff02::1", "0.0.0.0"]:
            self.assertFalse(is_public_ip(ip), ip)
        self.assertTrue(is_public_ip("1.1.1.1"))

    def test_robots_disallow_prevents_page_request(self):
        fetcher = Fetcher()
        with patch.object(fetcher, 'raw', return_value=(200, {}, b'User-agent: *\nDisallow: /private')) as raw:
            with self.assertRaisesRegex(FetchError, "Disallowed"):
                fetcher.get("https://example.com/private")
            self.assertEqual(raw.call_count, 1)

    def test_robots_failure_is_not_permission(self):
        fetcher = Fetcher()
        with patch.object(fetcher, 'raw', return_value=(503, {}, b'')) as raw:
            with self.assertRaisesRegex(FetchError, "permission"):
                fetcher.get("https://example.com/alex")
            self.assertEqual(raw.call_count, 1)

    def test_redirect_cannot_reach_local_network(self):
        fetcher = Fetcher()
        with patch.object(fetcher, 'allowed'), patch.object(fetcher, 'raw', return_value=(302, {'location': 'http://127.0.0.1/secrets'}, b'')) as raw:
            with self.assertRaises(FetchError):
                fetcher.get("https://example.com/alex")
            self.assertEqual(raw.call_count, 1)

    def test_redirect_rechecks_robots_on_destination(self):
        fetcher = Fetcher()
        with patch.object(fetcher, 'allowed') as allowed, patch.object(fetcher, 'raw', side_effect=[(302, {'location': 'https://other.example/alex'}, b''), (200, {'content-type': 'text/html'}, b'<p>bio</p>')]):
            fetcher.get("https://example.com/alex")
            self.assertEqual([c.args[0] for c in allowed.call_args_list], ["https://example.com/alex", "https://other.example/alex"])


class ExtractionTests(unittest.TestCase):
    def test_metadata_and_biography(self):
        source = source_from_html("https://example.com/", PROFILE)
        self.assertEqual(source['name'], 'Alex Example')
        self.assertIn('I grew up in Bristol', ' '.join(source['paragraphs']))
        self.assertNotIn('secret javascript', str(source['paragraphs']))
        self.assertEqual(len(source['image_captions']), 1)

    def test_link_selection_and_navigation(self):
        source = source_from_html("https://example.com/", PROFILE)
        candidates = {url: priority for url, _, priority in link_candidates(source, 0)}
        self.assertEqual(candidates['https://other.example/alex'], 0)
        self.assertEqual(candidates['https://third.example/alex'], 0)
        self.assertEqual(candidates['https://example.com/about'], 1)
        self.assertEqual(candidates['https://github.com/somebody'], 3)
        self.assertNotIn('https://example.com/login', candidates)
        self.assertNotIn('https://example.com/projects/archive.pdf', candidates)

    def test_article_author_not_subject(self):
        html = '<script type="application/ld+json">{"@type":"Article","author":{"@type":"Person","name":"Wrong subject","sameAs":"https://wrong.example/"}}</script>'
        source = source_from_html("https://example.com/", html)
        self.assertEqual(source['name'], '')
        self.assertEqual(source['links'], [])

    def test_summary_is_cited_and_background_not_invented(self):
        source = source_from_html("https://example.com/", PROFILE)
        source['id'] = 'S1'
        summary = make_summary([source])
        background = next(g for g in summary if g['title'] == 'Background')
        self.assertIn('grew up in Bristol', background['items'][0]['text'])
        for group in summary:
            for item in group['items']:
                self.assertEqual(item['source_id'], 'S1')
                self.assertIn(item['text'], source['paragraphs'])

    def test_contact_redaction_and_sensitive_summary_exclusion(self):
        redacted = public_text('Contact alex@example.com or +1 (212) 555-1212 at 123 Main Street.')
        self.assertNotIn('alex@example.com', redacted)
        self.assertNotIn('555', redacted)
        self.assertNotIn('123 Main', redacted)
        source = source_from_html('https://example.com/', '<p>I work as a developer and was diagnosed with a condition.</p>')
        source['id'] = 'S1'
        self.assertEqual(make_summary([source]), [])

    def test_github_profile_and_non_fork_repos(self):
        profile_url = 'https://github.com/alex'
        profile_api = 'https://api.github.com/users/alex'
        repo_api = profile_api + '/repos?sort=updated&per_page=12&type=owner'
        fetcher = FakeFetcher({profile_api: json.dumps({'type': 'User', 'name': 'Alex Example', 'bio': 'Engineer building accessible tools', 'blog': 'example.com', 'email': 'private@example.com'}), repo_api: json.dumps([{'name': 'Original', 'html_url': 'https://github.com/alex/original', 'fork': False}, {'name': 'Fork', 'fork': True}])})
        source = github_profile(profile_url, fetcher)
        self.assertEqual(source['name'], 'Alex Example')
        self.assertEqual(source['links'][0]['href'], 'https://example.com/')
        self.assertNotIn('private@example.com', str(source))
        self.assertEqual([w['title'] for w in github_work(profile_url, fetcher)], ['Original'])


class CrawlTests(unittest.TestCase):
    def test_recursion_dedup_and_uncertain_profiles(self):
        seed = 'https://example.com/'
        fake = FakeFetcher({seed: PROFILE, seed + 'about': '<p>I am Alex Example and I work on inclusive software.</p><a href="/about">About</a>', 'https://other.example/alex': '<p>Alex Example is a software engineer and project maintainer.</p>'})
        job = Job('test', seed, 10, 1)
        run_job(job, fake)
        report = job.snapshot()
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(report['sources']), 3)
        self.assertEqual(fake.calls.count(seed + 'about'), 1)
        self.assertNotIn('https://github.com/somebody', fake.calls)
        self.assertEqual(len(report['candidates']), 1)
        self.assertEqual(len(report['gaps']), 1)
        self.assertEqual(report['sources'][1]['parent'], 'S1')
        self.assertIn('Source: [S1]', markdown_report(report))

    def test_page_budget_counts_failures(self):
        fake = FakeFetcher({'https://example.com/': PROFILE})
        job = Job('test', 'https://example.com/', 2, 3)
        run_job(job, fake)
        self.assertEqual(len(fake.calls), 2)

    def test_depth_zero_stays_at_seed(self):
        fake = FakeFetcher({'https://example.com/': PROFILE})
        job = Job('test', 'https://example.com/', 20, 0)
        run_job(job, fake)
        self.assertEqual(len(fake.calls), 1)

    def test_cancellation(self):
        fake = FakeFetcher({})
        job = Job('test', 'https://example.com/', 20, 3)
        job.cancel.set()
        run_job(job, fake)
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(fake.calls, [])

    def test_blocked_page_is_a_gap_not_a_finding(self):
        fake = FakeFetcher({'https://example.com/': FetchError('Login required')})
        job = Job('test', 'https://example.com/', 5, 1)
        run_job(job, fake)
        self.assertEqual(job.summary, [])
        self.assertEqual(job.sources, [])
        self.assertEqual(job.gaps[0]['reason'], 'Login required')


if __name__ == '__main__':
    unittest.main()
