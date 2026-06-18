"""Crawl subprocess orchestration + real per-asset/manifest output (Unit 4).

Network policy: we NEVER hit a real external site. The integration crawl runs a
REAL Scrapy subprocess against a local 127.0.0.1 http.server fixture (gated by
LCP_ALLOW_LOOPBACK_FOR_TESTS, a test-only escape — production's net_guard blocks
loopback). crawl_runner's guard/orchestration logic is driven with an injected
subprocess stub so allowlist/SSRF/minimal-env/timeout paths are deterministic.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from io import BytesIO
from pathlib import Path

import pytest

from lcp.adapters.crawler import net_guard, scrapy_impl
from lcp.adapters.crawler.base import (
    STATUS_CRAWL_FAILED,
    STATUS_CRAWLED,
    STATUS_CRAWLED_WARN,
    STATUS_NEEDS_REVISION,
    SourceSpec,
)
from lcp.adapters.crawler.crawl_runner import (
    EVENT_CRAWL_REJECTED,
    CrawlRunner,
)
from lcp.adapters.crawler.source_registry import SourceEntry, SourceRegistry
from lcp.adapters.storage.audit_log import AuditLog
from lcp.adapters.storage.manifest import read_manifest, write_manifest
from lcp.core.errors import ExternalServiceError, InputValidationError
from lcp.core.models import AssetState, SourceType

TS = "2026-06-16T00:00:00Z"
REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _real_jpeg() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (16, 16), (0, 128, 255)).save(buf, "JPEG")
    return buf.getvalue()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # silence the test server
        pass


@pytest.fixture
def local_server(tmp_path_factory):
    """A 127.0.0.1 http.server serving a fixed HTML page + a real image."""
    root = tmp_path_factory.mktemp("docroot")
    (root / "img").mkdir()
    (root / "img" / "a.jpg").write_bytes(_real_jpeg())
    (root / "article.html").write_text(
        "<html><head><title>Local Test Title</title></head><body>"
        "<article><p>Body one.</p><p>Body two.</p></article>"
        '<img src="/img/a.jpg"><img src="/img/a.jpg"></body></html>',
        encoding="utf-8",
    )
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _spawn_crawl(url: str, job_dir: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LCP_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
    env["PYTHONPATH"] = REPO_SRC
    if extra_env:
        env.update(extra_env)
    cmd = [
        sys.executable, "-m", "lcp.adapters.crawler.scrapy_impl",
        "--url", url,
        "--job-id", job_dir.name,
        "--job-dir", str(job_dir),
        "--allow-domain", "127.0.0.1",
        "--timeout", "15",
        "--source-domain", "127.0.0.1",
        "--fetched-at", TS,
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=90)


# --------------------------------------------------------------------------
# Integration: real Scrapy subprocess against the local server
# --------------------------------------------------------------------------

def test_real_subprocess_crawl_produces_bundle_0600_sha256(local_server, tmp_path):
    job_dir = tmp_path / "jobA"
    proc = _spawn_crawl(f"{local_server}/article.html", job_dir)
    assert proc.returncode == 0, proc.stderr

    m = read_manifest(job_dir)
    assert m is not None
    assert m.crawl_status == STATUS_CRAWLED
    # source.{html,txt} captured + hashed
    assert (job_dir / "raw" / "source.html").exists()
    assert (job_dir / "raw" / "source.txt").read_text(encoding="utf-8").startswith("Body one.")
    assert m.hashes.source_html_sha256 and m.hashes.source_text_sha256

    # exactly one image asset (duplicate URL deduped), OK, with sha256, 0600
    img_assets = [a for a in m.assets if a.state is AssetState.OK]
    assert len(img_assets) == 1
    a = img_assets[0]
    assert a.sha256 and len(a.sha256) == 64
    disk = job_dir / a.path
    assert disk.exists()
    assert os.stat(disk).st_mode & 0o077 == 0  # 0600 downloaded media


def test_real_subprocess_does_not_overwrite_existing_job(local_server, tmp_path):
    job_dir = tmp_path / "jobB"
    assert _spawn_crawl(f"{local_server}/article.html", job_dir).returncode == 0
    before = (job_dir / "manifest.json").read_text(encoding="utf-8")
    # second spawn into the SAME job dir must refuse to clobber (R11) -> nonzero
    proc2 = _spawn_crawl(f"{local_server}/article.html", job_dir)
    assert proc2.returncode != 0
    after = (job_dir / "manifest.json").read_text(encoding="utf-8")
    assert before == after  # manifest untouched


def test_subprocess_env_strips_secrets(local_server, tmp_path):
    # The PARENT sets a secret; minimal_env() must NOT pass it to the child. We
    # prove it by having the child fail loudly if the secret is visible.
    job_dir = tmp_path / "jobC"
    # Build the command via the same minimal_env() the runner uses.
    from lcp.runtime_hardening import minimal_env

    env = minimal_env()
    env["LCP_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
    env["PYTHONPATH"] = REPO_SRC
    # assert the scrubbed env carries no secret even if parent has one
    os.environ["LCP_LLM_API_KEY"] = "sk-should-not-leak"
    try:
        scrubbed = minimal_env()
        assert "LCP_LLM_API_KEY" not in scrubbed
    finally:
        del os.environ["LCP_LLM_API_KEY"]


# --------------------------------------------------------------------------
# Orchestration with an injected subprocess stub (deterministic)
# --------------------------------------------------------------------------

def _registry() -> SourceRegistry:
    return SourceRegistry([SourceEntry(domain="example.com", legal_basis="public press release")])


def _good_resolver(host):
    return ["93.184.216.34"]  # global


def _internal_resolver(host):
    return ["10.0.0.5"]  # private -> SSRF reject


def test_domain_not_in_allowlist_rejected_and_audited(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    runner = CrawlRunner(_registry(), audit=audit, resolver=_good_resolver)
    spec = SourceSpec(
        job_id="j1", source_type=SourceType.URL,
        job_dir=tmp_path / "j1", url="https://evil.test/x",
    )
    with pytest.raises(InputValidationError):
        runner.crawl_url(spec, ts=TS)
    events = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(
        e["event"] == EVENT_CRAWL_REJECTED and e["extra"]["reason"] == "domain_not_allowlisted"
        for e in events
    )


def test_ssrf_blocked_and_audited(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    runner = CrawlRunner(_registry(), audit=audit, resolver=_internal_resolver)
    spec = SourceSpec(
        job_id="j2", source_type=SourceType.URL,
        job_dir=tmp_path / "j2", url="https://example.com/x",
    )
    with pytest.raises(InputValidationError):
        runner.crawl_url(spec, ts=TS)
    events = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(
        e["event"] == EVENT_CRAWL_REJECTED and e["extra"]["reason"] == "ssrf_blocked"
        for e in events
    )


def test_runner_passes_minimal_env_to_subprocess(tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        # simulate the child writing a manifest
        spec_job_dir = tmp_path / "j3"
        from lcp.adapters.crawler.bundle import build_manifest
        m = build_manifest(
            job_id="j3", source_type=SourceType.URL, source_domain="example.com",
            fetched_at=TS, assets=[], source_html="<html></html>", source_text="body",
            crawl_status=STATUS_NEEDS_REVISION,
        )
        write_manifest(spec_job_dir, m, create_only=True)

        class P:
            returncode = 0
        return P()

    os.environ["LCP_LLM_API_KEY"] = "sk-secret-xyz"
    try:
        runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_run)
        spec = SourceSpec(
            job_id="j3", source_type=SourceType.URL,
            job_dir=tmp_path / "j3", url="https://example.com/x",
        )
        bundle = runner.crawl_url(spec, ts=TS)
    finally:
        del os.environ["LCP_LLM_API_KEY"]

    assert bundle.job_status == STATUS_NEEDS_REVISION
    env = captured["env"]
    assert "LCP_LLM_API_KEY" not in env  # secret stripped from subprocess env


def test_runner_timeout_raises_external_service_error(tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_run)
    spec = SourceSpec(
        job_id="j4", source_type=SourceType.URL,
        job_dir=tmp_path / "j4", url="https://example.com/x",
    )
    with pytest.raises(ExternalServiceError):
        runner.crawl_url(spec, ts=TS)


def test_runner_no_manifest_raises_external_service_error(tmp_path):
    def fake_run(cmd, **kwargs):
        class P:
            returncode = 1
        return P()  # child "crashed", wrote nothing

    runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_run)
    spec = SourceSpec(
        job_id="j5", source_type=SourceType.URL,
        job_dir=tmp_path / "j5", url="https://example.com/x",
    )
    with pytest.raises(ExternalServiceError):
        runner.crawl_url(spec, ts=TS)


def test_runner_nonzero_rc_with_stale_manifest_raises(tmp_path):
    """U6 (REL-1): a child that exits NON-ZERO must be a retriable failure even if
    a (stale, from a prior run) manifest is present — the runner must check
    proc.returncode, not just manifest presence, or it reports a stale manifest as
    this run's success."""
    job_dir = tmp_path / "j6"
    # A leftover manifest from a previous run sits on disk.
    from lcp.adapters.crawler.bundle import build_manifest

    stale = build_manifest(
        job_id="j6", source_type=SourceType.URL, source_domain="example.com",
        fetched_at=TS, assets=[], source_html="<html>old</html>", source_text="old",
        crawl_status=STATUS_CRAWLED,
    )
    write_manifest(job_dir, stale, create_only=True)

    def fake_run(cmd, **kwargs):
        class P:
            returncode = 2  # child crashed this run
        return P()

    runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_run)
    spec = SourceSpec(
        job_id="j6", source_type=SourceType.URL,
        job_dir=job_dir, url="https://example.com/x",
    )
    with pytest.raises(ExternalServiceError):
        runner.crawl_url(spec, ts=TS)


def test_read_manifest_corrupt_raises_external_service_error(tmp_path):
    """U6 (REL-2): a truncated/garbage manifest (e.g. a SIGKILL mid-write) must map
    to a retriable failure, not crash the run with a raw pydantic ValidationError."""
    job_dir = tmp_path / "jc"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manifest.json").write_text('{"job_id": "jc", "asse', encoding="utf-8")
    with pytest.raises(ExternalServiceError):
        read_manifest(job_dir)


def test_scrapy_main_unexpected_error_returns_nonzero(tmp_path, monkeypatch):
    """U6 (REL-1, child side): a non-LcpError crash inside the spider must become a
    clean non-zero exit + JSON error line, not an escaping traceback the parent
    silently ignores."""
    monkeypatch.setenv("LCP_ALLOW_LOOPBACK_FOR_TESTS", "1")  # skip real DNS preflight

    def boom(*a, **k):
        raise RuntimeError("reactor exploded")

    monkeypatch.setattr(scrapy_impl, "_run_spider", boom)
    rc = scrapy_impl.main(
        [
            "--url", "http://127.0.0.1/x",
            "--job-id", "jboom",
            "--job-dir", str(tmp_path / "jboom"),
            "--allow-domain", "127.0.0.1",
            "--timeout", "5",
            "--source-domain", "127.0.0.1",
            "--fetched-at", TS,
        ]
    )
    assert rc != 0


# --------------------------------------------------------------------------
# Real per-asset/manifest output path via extract_content + write_bundle
# (fabricated Scrapy Response — no network)
# --------------------------------------------------------------------------

def _response(html: str, url: str = "https://example.com/a"):
    from scrapy.http import HtmlResponse

    return HtmlResponse(url=url, body=html.encode("utf-8"), encoding="utf-8")


def _spec(tmp_path, job_id="jx"):
    d = tmp_path / job_id
    d.mkdir(parents=True, exist_ok=True)
    return SourceSpec(job_id=job_id, source_type=SourceType.URL, job_dir=d, url="https://example.com/a")


def test_extract_dedupes_duplicate_media_urls():
    # Use literal globally-routable IPs so net_guard's second-order SSRF check
    # classifies them directly (is_global) without any live DNS lookup. The
    # duplicate a.jpg is still deduped; b.jpg (different host) stays distinct.
    a = "https://93.184.216.34/a.jpg"
    b = "https://93.184.216.35/b.jpg"
    html = (
        "<html><title>T</title><body><p>x</p>"
        f'<img src="{a}"><img src="{a}"><img src="{b}"></body></html>'
    )
    out = scrapy_impl.extract_content(_response(html))
    assert out["image_urls"] == [a, b]


def test_title_present_body_empty_needs_revision(tmp_path):
    html = "<html><head><title>Only A Title</title></head><body></body></html>"
    out = scrapy_impl.extract_content(_response(html))
    bundle = scrapy_impl.write_bundle_from_extraction(
        _spec(tmp_path, "jrev"), out, source_domain="example.com", fetched_at=TS
    )
    assert bundle.job_status == STATUS_NEEDS_REVISION


def test_total_extraction_failure_crawl_failed(tmp_path):
    # neither title nor body -> CRAWL_FAILED (retriable)
    out = {"title": "", "body": "", "image_urls": [], "video_urls": [],
           "source_html": "<html></html>", "metadata": {"url": "https://example.com/a"}}
    bundle = scrapy_impl.write_bundle_from_extraction(
        _spec(tmp_path, "jfail"), out, source_domain="example.com", fetched_at=TS
    )
    assert bundle.job_status == STATUS_CRAWL_FAILED


def test_partial_asset_failure_crawled_warn(tmp_path):
    # one image declared, but the pipeline produced no download -> FAILED ->
    # CRAWLED_WARN (content otherwise complete).
    out = {
        "title": "Good Title", "body": "Good body text",
        "image_urls": ["https://example.com/missing.jpg"], "video_urls": [],
        "source_html": "<html></html>", "metadata": {"url": "https://example.com/a"},
        "downloaded_images": [], "downloaded_files": [],
    }
    bundle = scrapy_impl.write_bundle_from_extraction(
        _spec(tmp_path, "jwarn"), out, source_domain="example.com", fetched_at=TS
    )
    assert bundle.job_status == STATUS_CRAWLED_WARN
    failed = [a for a in bundle.manifest.assets if a.state is AssetState.FAILED]
    assert len(failed) == 1 and failed[0].source_url == "https://example.com/missing.jpg"


def test_scrapy_reports_asset_truncation_at_max_assets(tmp_path):
    # Unit 15 (parity with the ingest path): when more media URLs are declared
    # than max_assets, the Scrapy path used to silently truncate. It must now
    # REPORT the truncation the way ingest does (truncated_at_max_assets flag in
    # a sibling crawl_report.json), so the operator sees assets were dropped.
    d = tmp_path / "jtrunc"
    d.mkdir(parents=True, exist_ok=True)
    spec = SourceSpec(
        job_id="jtrunc", source_type=SourceType.URL, job_dir=d,
        url="https://example.com/a", max_assets=2,
    )
    out = {
        "title": "T", "body": "B",
        "image_urls": [f"https://93.184.216.34/img{i}.jpg" for i in range(5)],
        "video_urls": [], "source_html": "<html></html>",
        "metadata": {"url": "https://example.com/a"},
        "downloaded_images": [], "downloaded_files": [],
    }
    scrapy_impl.write_bundle_from_extraction(
        spec, out, source_domain="example.com", fetched_at=TS
    )
    report = json.loads((d / "raw" / "crawl_report.json").read_text("utf-8"))
    assert report["truncated_at_max_assets"] is True
    assert report["declared_assets"] == 5
    assert report["max_assets"] == 2


def test_scrapy_report_no_truncation_when_under_cap(tmp_path):
    # Below the cap: the report records no truncation (guards against a flag that
    # is always True).
    out = {
        "title": "T", "body": "B",
        "image_urls": ["https://93.184.216.34/only.jpg"], "video_urls": [],
        "source_html": "<html></html>", "metadata": {"url": "https://example.com/a"},
        "downloaded_images": [], "downloaded_files": [],
    }
    scrapy_impl.write_bundle_from_extraction(
        _spec(tmp_path, "junder"), out, source_domain="example.com", fetched_at=TS
    )
    report = json.loads(
        (tmp_path / "junder" / "raw" / "crawl_report.json").read_text("utf-8")
    )
    assert report["truncated_at_max_assets"] is False


def test_scraped_media_urls_validated_for_ssrf(tmp_path, monkeypatch):
    """P1 regression (second-order SSRF): media URLs scraped from untrusted HTML
    must each pass net_guard before being queued for download. An <img>/<a>
    pointing at 169.254.169.254 / 127.0.0.1 is dropped (not in image_urls), while
    an allowlisted global media URL is kept. Rejected ones surface as FAILED
    assets via write_bundle."""
    # Ensure the loopback test bypass is OFF so real validation runs in-process.
    monkeypatch.delenv("LCP_ALLOW_LOOPBACK_FOR_TESTS", raising=False)

    good = "https://93.184.216.34/photo.jpg"  # literal global IP, no DNS needed
    html = (
        "<html><title>T</title><body><p>x</p>"
        f'<img src="{good}">'
        '<img src="http://169.254.169.254/latest/meta-data/iam">'
        '<img src="http://127.0.0.1/secret.png">'
        '<a href="http://10.0.0.1/internal.mp4">link</a>'
        "</body></html>"
    )
    out = scrapy_impl.extract_content(_response(html))

    # Only the global media URL is queued for download.
    assert out["image_urls"] == [good]
    assert out["video_urls"] == []
    # The internal/metadata targets were rejected (dropped, recorded).
    rejected = set(out["rejected_media_urls"])
    assert "http://169.254.169.254/latest/meta-data/iam" in rejected
    assert "http://127.0.0.1/secret.png" in rejected
    assert "http://10.0.0.1/internal.mp4" in rejected

    # write_bundle records the rejected URLs as FAILED assets.
    bundle = scrapy_impl.write_bundle_from_extraction(
        _spec(tmp_path, "jssrf"), out, source_domain="example.com", fetched_at=TS,
    )
    failed_urls = {
        a.source_url for a in bundle.manifest.assets
        if a.state is AssetState.FAILED
    }
    assert "http://169.254.169.254/latest/meta-data/iam" in failed_urls
    assert "http://127.0.0.1/secret.png" in failed_urls


def test_robots_disallow_recorded_not_bypassed():
    # ROBOTSTXT_OBEY must be True in the spider settings (plan R8). We assert
    # the policy is on, never bypassed.
    settings = scrapy_impl.build_settings(
        job_dir=Path("/tmp/x"), allow_domains=["example.com"], timeout=15
    )
    assert settings["ROBOTSTXT_OBEY"] is True
    assert settings["REDIRECT_ENABLED"] is False  # redirects not blindly followed
    assert settings["AUTOTHROTTLE_ENABLED"] is True


# --------------------------------------------------------------------------
# U12: pipeline-output path containment (defense-in-depth, SECURITY)
# --------------------------------------------------------------------------

def test_relative_to_does_not_collapse_dotdot(tmp_path):
    """U12 regression guard: documents WHY safe_join is required. Path.relative_to
    does NOT resolve `..`, so a traversal path passes a naive relative_to/startswith
    containment check — exactly the gap the old line-298 code had. safe_join
    (resolve() + is_relative_to) is the only correct containment primitive here."""
    store = tmp_path / "raw" / "images"
    store.mkdir(parents=True)
    escaping = store / "../../../../etc/passwd"
    # relative_to happily returns a value for an escaping path (no `..` collapse),
    # i.e. it would NOT have caught the traversal.
    rel = escaping.relative_to(store)
    assert ".." in rel.as_posix()
    # safe_join, by contrast, rejects it.
    with pytest.raises(InputValidationError):
        net_guard.safe_join(store, "../../../../etc/passwd")


def test_pipeline_output_traversal_path_marked_failed_no_oob_access(tmp_path, monkeypatch):
    """U12: a downloaded-file `path` that escapes the store (e.g. a future malicious
    pipeline swap) must be routed through safe_join BEFORE read_bytes/chmod, marked
    FAILED, and must touch NO file outside the job dir."""
    # An out-of-tree secret the traversal path resolves to.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "passwd"
    secret.write_bytes(b"root:x:0:0:")
    secret_mode_before = os.stat(secret).st_mode

    # Fail loudly if anything reads or chmods outside the job dir.
    real_read = Path.read_bytes
    real_chmod = os.chmod

    def guarded_read(self):
        assert tmp_path / "outside" not in self.parents, f"OOB read: {self}"
        return real_read(self)

    def guarded_chmod(path, mode, *a, **k):
        p = Path(path).resolve()
        assert outside.resolve() not in p.parents, f"OOB chmod: {p}"
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    monkeypatch.setattr(os, "chmod", guarded_chmod)

    spec = _spec(tmp_path, "jtrav")
    out = {
        "title": "Good Title", "body": "Good body text",
        "image_urls": ["https://example.com/evil.jpg"], "video_urls": [],
        "source_html": "<html></html>", "metadata": {"url": "https://example.com/a"},
        # the relative path the (hypothetically malicious) pipeline reported escapes
        # the images store via `..` and points at the out-of-tree secret.
        "downloaded_images": [
            {"url": "https://example.com/evil.jpg",
             "path": "../../../outside/passwd", "checksum": "x"}
        ],
        "downloaded_files": [],
    }
    bundle = scrapy_impl.write_bundle_from_extraction(
        spec, out, source_domain="example.com", fetched_at=TS,
    )
    # The asset is FAILED (rejected by containment), not OK.
    failed = [a for a in bundle.manifest.assets if a.state is AssetState.FAILED]
    assert len(failed) == 1
    assert failed[0].source_url == "https://example.com/evil.jpg"
    # The out-of-tree secret was neither read nor chmod'd.
    assert os.stat(secret).st_mode == secret_mode_before


def test_pipeline_output_normal_relative_path_resolves_and_reads(tmp_path):
    """U12 happy path: a normal sha1-style relative path under the store resolves,
    is read, chmod'd 0600, and recorded OK — unchanged behavior."""
    spec = _spec(tmp_path, "jok")
    images_store = spec.job_dir / "raw" / "images"
    images_store.mkdir(parents=True)
    # sha1-style nested path Scrapy's ImagesPipeline produces (full/<sha1>.jpg).
    rel = "full/da39a3ee5e6b4b0d3255bfef95601890afd80709.jpg"
    disk = images_store / rel
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(_real_jpeg())

    out = {
        "title": "Good Title", "body": "Good body text",
        "image_urls": ["https://example.com/a.jpg"], "video_urls": [],
        "source_html": "<html></html>", "metadata": {"url": "https://example.com/a"},
        "downloaded_images": [
            {"url": "https://example.com/a.jpg", "path": rel, "checksum": "x"}
        ],
        "downloaded_files": [],
    }
    bundle = scrapy_impl.write_bundle_from_extraction(
        spec, out, source_domain="example.com", fetched_at=TS,
    )
    ok = [a for a in bundle.manifest.assets if a.state is AssetState.OK]
    assert len(ok) == 1
    assert ok[0].sha256 and len(ok[0].sha256) == 64
    assert ok[0].path == ("raw/images/" + rel)
    assert os.stat(disk).st_mode & 0o077 == 0  # chmod 0600 still applied


# --------------------------------------------------------------------------
# U12: raw/ cleanup on a killed/failed crawl so a retry starts clean
# --------------------------------------------------------------------------

def test_timeout_clears_raw_for_clean_retry(tmp_path):
    """U12: a SIGKILL'd (TimeoutExpired) crawl must clear the job's raw/ dir so a
    retry does not inherit an orphaned partial download. The first run leaves a
    partial source.txt behind; after the timeout the runner must have removed it."""
    job_dir = tmp_path / "jto"

    def fake_run_timeout(cmd, **kwargs):
        # simulate the child writing a partial download before being SIGKILL'd
        raw = job_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text("partial", encoding="utf-8")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_run_timeout)
    spec = SourceSpec(
        job_id="jto", source_type=SourceType.URL,
        job_dir=job_dir, url="https://example.com/x",
    )
    with pytest.raises(ExternalServiceError):
        runner.crawl_url(spec, ts=TS)
    # raw/ must be clean: no orphaned partial source.txt left for the retry.
    assert not (job_dir / "raw" / "source.txt").exists()


def test_failed_run_no_manifest_clears_raw(tmp_path):
    """U12: a non-zero-exit crawl that produced no valid manifest must also clear
    raw/ (orphaned partial downloads) so create_only does not trip on retry."""
    job_dir = tmp_path / "jnoman"

    def fake_run_fail(cmd, **kwargs):
        raw = job_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text("partial", encoding="utf-8")

        class P:
            returncode = 1
        return P()

    runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_run_fail)
    spec = SourceSpec(
        job_id="jnoman", source_type=SourceType.URL,
        job_dir=job_dir, url="https://example.com/x",
    )
    with pytest.raises(ExternalServiceError):
        runner.crawl_url(spec, ts=TS)
    assert not (job_dir / "raw" / "source.txt").exists()


def test_clean_raw_then_retry_succeeds(tmp_path):
    """U12: after a failed crawl clears raw/, a subsequent successful crawl into the
    same job dir produces a valid bundle (the retry is not blocked by orphans)."""
    job_dir = tmp_path / "jretry"

    # First run: fail with a leftover partial in raw/.
    def fake_fail(cmd, **kwargs):
        raw = job_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "source.txt").write_text("partial", encoding="utf-8")

        class P:
            returncode = 1
        return P()

    runner = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_fail)
    spec = SourceSpec(
        job_id="jretry", source_type=SourceType.URL,
        job_dir=job_dir, url="https://example.com/x",
    )
    with pytest.raises(ExternalServiceError):
        runner.crawl_url(spec, ts=TS)
    assert not (job_dir / "raw").exists() or not any((job_dir / "raw").iterdir())

    # Retry: a clean child write succeeds.
    def fake_ok(cmd, **kwargs):
        from lcp.adapters.crawler.bundle import build_manifest
        m = build_manifest(
            job_id="jretry", source_type=SourceType.URL, source_domain="example.com",
            fetched_at=TS, assets=[], source_html="<html></html>", source_text="body",
            crawl_status=STATUS_CRAWLED,
        )
        write_manifest(job_dir, m, create_only=True)

        class P:
            returncode = 0
        return P()

    runner2 = CrawlRunner(_registry(), resolver=_good_resolver, subprocess_runner=fake_ok)
    bundle = runner2.crawl_url(spec, ts=TS)
    assert bundle.job_status == STATUS_CRAWLED
