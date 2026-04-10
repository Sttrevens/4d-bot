from app.tools import browser_ops


def test_normalize_site_accepts_url_and_host():
    assert browser_ops._normalize_site("https://insights.mckinsey.com/article") == "insights.mckinsey.com"
    assert browser_ops._normalize_site("mckinsey.com/path") == "mckinsey.com"
    assert browser_ops._normalize_site("  .MCKINSEY.COM:443 ") == "mckinsey.com"


def test_cookie_matches_site_handles_subdomains():
    assert browser_ops._cookie_matches_site(".mckinsey.com", "mckinsey.com")
    assert browser_ops._cookie_matches_site("insights.mckinsey.com", "mckinsey.com")
    assert not browser_ops._cookie_matches_site("example.com", "mckinsey.com")


def test_cookie_redis_key_scoped_by_session_and_site():
    key = browser_ops._cookie_redis_key("tenant:user", "mckinsey.com")
    assert key == "browser:cookies:tenant:user:mckinsey.com"
