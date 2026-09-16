import server


def test_root_serves_index_html_verbatim(client):
    r = client.get("/")
    assert r.status == 200
    assert r.body == server.INDEX_HTML.encode()
    assert r.headers["Content-Type"] == "text/html"


def test_index_html_has_no_mobile_bits():
    """The desktop page must not grow mobile markup. Guards the core constraint."""
    for marker in ("/page/", "manifest.webmanifest", "apple-mobile-web-app", "/m'", '/m"'):
        assert marker not in server.INDEX_HTML, f"{marker!r} leaked into the desktop page"


def test_desktop_still_uses_the_iframe_viewer():
    """Desktop PDF rendering is unchanged; only the mobile shell uses page images."""
    assert "createElement('iframe')" in server.INDEX_HTML
