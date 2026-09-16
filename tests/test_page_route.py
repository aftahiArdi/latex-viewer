import os
import subprocess
import threading
from pathlib import Path

import pytest

import server
from conftest import FAKE_PDF, LOG_2_PAGES, make_project

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"


@pytest.fixture
def fake_pdftoppm(monkeypatch):
    """Stand in for poppler: write one PNG into the output directory."""
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] != "pdftoppm":
            return real_run(cmd, **kwargs)
        calls.append(cmd)
        Path(cmd[-1] + "-1.png").write_bytes(PNG)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail loudly if anything shells out. Used by the rejection tests."""
    def explode(*a, **kw):
        pytest.fail(f"must not shell out: {a!r}")

    monkeypatch.setattr(server.subprocess, "run", explode)


def test_serves_a_rendered_page(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    r = client.get("/page/doc/1.png")
    assert r.status == 200
    assert r.body == PNG
    assert r.headers["Content-Type"] == "image/png"
    assert len(fake_pdftoppm) == 1


def test_renders_the_requested_page_at_the_configured_dpi(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    client.get("/page/doc/2.png")
    cmd = fake_pdftoppm[0]
    assert cmd[cmd.index("-f") + 1] == "2"
    assert cmd[cmd.index("-l") + 1] == "2"
    assert cmd[cmd.index("-r") + 1] == str(server.PAGE_DPI)


def test_second_request_is_served_from_cache(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/doc/1.png").body == PNG
    assert client.get("/page/doc/1.png").body == PNG
    assert len(fake_pdftoppm) == 1, "cached page was re-rendered"


def test_cache_lives_in_a_subdirectory_of_build(client, tmp_path, fake_pdftoppm):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    client.get("/page/doc/1.png")
    cache = d / server.BUILD_DIRNAME / server.PAGES_DIRNAME
    assert cache.is_dir()
    assert list(cache.glob("*.png"))
    # A compile retry unlinks files directly in .build/ but not subdirectories.
    assert not list((d / server.BUILD_DIRNAME).glob("*.png"))


def test_a_new_pdf_mtime_re_renders_and_sweeps_the_old_pages(client, tmp_path, fake_pdftoppm):
    d = make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    client.get("/page/doc/1.png")
    cache = d / server.BUILD_DIRNAME / server.PAGES_DIRNAME
    stale = sorted(p.name for p in cache.glob("*.png"))

    os.utime(d / "main.pdf", (2_000_000_000, 2_000_000_000))
    client.get("/page/doc/1.png")

    assert len(fake_pdftoppm) == 2
    fresh = sorted(p.name for p in cache.glob("*.png"))
    assert fresh != stale
    assert len(fresh) == 1, "stale page images were not swept"


def test_rejects_a_project_outside_documents(client, tmp_path, no_subprocess):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/..%2F..%2Fetc/1.png").status == 404


def test_rejects_an_unknown_project(client, no_subprocess):
    assert client.get("/page/nope/1.png").status == 404


def test_rejects_a_non_numeric_page(client, tmp_path, no_subprocess):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/doc/x.png").status == 404


def test_rejects_a_page_beyond_the_document(client, tmp_path, no_subprocess):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    assert client.get("/page/doc/3.png").status == 404
    assert client.get("/page/doc/0.png").status == 404
    assert client.get("/page/doc/-1.png").status == 404


def test_returns_502_when_the_renderer_fails(client, tmp_path, monkeypatch):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("pdftoppm")),
    )
    assert client.get("/page/doc/1.png").status == 502


def test_concurrent_requests_render_once(client, tmp_path, fake_pdftoppm):
    make_project(tmp_path, "doc", pdf=FAKE_PDF, log=LOG_2_PAGES)
    bodies = []
    threads = [threading.Thread(target=lambda: bodies.append(client.get("/page/doc/1.png").body))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert bodies == [PNG] * 4
    assert len(fake_pdftoppm) == 1, "the per-project lock did not serialise rendering"
