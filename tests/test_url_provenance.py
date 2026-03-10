"""URL 溯源验证器测试

覆盖:
- extract_urls: 基础提取、标点清理、空输入
- check_url_provenance: 精确匹配、域名降级、死循环保护、规范化容差
- _normalize_url: query param 清理、encoding、大小写
"""
import pytest
from app.services.base_agent import extract_urls, check_url_provenance, _normalize_url


# ── extract_urls ──

class TestExtractUrls:
    def test_basic_urls(self):
        text = "Visit https://example.com and http://foo.bar/path?q=1"
        urls = extract_urls(text)
        assert "https://example.com" in urls
        assert "http://foo.bar/path?q=1" in urls

    def test_urls_with_trailing_punctuation(self):
        text = "See https://example.com/page。另外 https://foo.com/bar，还有"
        urls = extract_urls(text)
        assert "https://example.com/page" in urls
        assert "https://foo.com/bar" in urls

    def test_empty_text(self):
        assert extract_urls("") == set()
        assert extract_urls(None) == set()

    def test_no_urls(self):
        assert extract_urls("Hello world, no links here") == set()

    def test_eventbrite_url(self):
        text = "Link: https://www.eventbrite.com/e/pocket-gamer-party-tickets-1981650893112"
        urls = extract_urls(text)
        assert "https://www.eventbrite.com/e/pocket-gamer-party-tickets-1981650893112" in urls

    def test_csv_data_with_urls(self):
        text = '"Event","Link"\n"GDC Party","https://luma.com/gdc-party-2026"\n"Mixer","https://eventbrite.com/e/123"'
        urls = extract_urls(text)
        assert "https://luma.com/gdc-party-2026" in urls
        assert "https://eventbrite.com/e/123" in urls


# ── _normalize_url ──

class TestNormalizeUrl:
    def test_trailing_slash(self):
        assert _normalize_url("https://example.com/path/") == _normalize_url("https://example.com/path")

    def test_case_insensitive(self):
        assert _normalize_url("https://WWW.Example.COM/Path") == _normalize_url("https://www.example.com/path")

    def test_strips_utm_params(self):
        norm = _normalize_url("https://example.com/page?utm_source=twitter&id=123")
        assert "utm_source" not in norm
        assert "id=123" in norm

    def test_strips_fragment(self):
        norm = _normalize_url("https://example.com/page#section")
        assert "#section" not in norm

    def test_url_decoding(self):
        """%20 和空格应规范化为同一形式"""
        a = _normalize_url("https://example.com/path%20name")
        b = _normalize_url("https://example.com/path name")
        assert a == b


# ── check_url_provenance 基础 ──

class TestCheckUrlProvenance:
    def test_exempt_tools_pass(self):
        """fetch_url 等工具不检查 URL 溯源"""
        warning, flagged = check_url_provenance(
            "fetch_url",
            {"url": "https://any-url.com/page"},
            set(),
        )
        assert warning is None
        assert flagged == []

    def test_non_write_tools_pass(self):
        """非写操作工具不检查"""
        warning, flagged = check_url_provenance(
            "list_calendar_events",
            {"description": "https://fake.com/event"},
            set(),
        )
        assert warning is None

    def test_write_tool_with_seen_url_passes(self):
        """写操作中的 URL 出现在 seen_urls 中 → 通过"""
        seen = {"https://www.eventbrite.com/e/real-event-123"}
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "Link: https://www.eventbrite.com/e/real-event-123"},
            seen,
        )
        assert warning is None

    def test_write_tool_with_hallucinated_url_blocked(self):
        """写操作中的 URL 不在 seen_urls 中、域名也没有 → 硬拦截"""
        seen = {"https://www.eventbrite.com/e/real-event-123"}
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "Link: https://totally-fake-site.com/event/999"},
            seen,
        )
        assert warning is not None
        assert "⛔" in warning  # 硬拦截标记
        assert len(flagged) == 1

    def test_write_tool_no_urls_in_args_passes(self):
        """写操作参数中没有 URL → 通过"""
        warning, _ = check_url_provenance(
            "update_calendar_event",
            {"summary": "GDC Party", "description": "A fun event"},
            {"https://example.com"},
        )
        assert warning is None

    def test_empty_seen_urls_passes(self):
        """seen_urls 为空时不检查（可能是对话开始）"""
        warning, _ = check_url_provenance(
            "update_calendar_event",
            {"description": "https://example.com/event"},
            set(),
        )
        assert warning is None

    def test_case_insensitive_match(self):
        """URL 比较不区分大小写"""
        seen = {"https://WWW.Example.COM/Path"}
        warning, _ = check_url_provenance(
            "create_calendar_event",
            {"description": "https://www.example.com/path"},
            seen,
        )
        assert warning is None

    def test_trailing_slash_tolerance(self):
        """尾部斜杠不影响匹配"""
        seen = {"https://example.com/page/"}
        warning, _ = check_url_provenance(
            "create_calendar_event",
            {"description": "https://example.com/page"},
            seen,
        )
        assert warning is None

    def test_utm_param_tolerance(self):
        """追踪参数差异不影响匹配"""
        seen = {"https://example.com/page?utm_source=twitter&id=123"}
        warning, _ = check_url_provenance(
            "create_calendar_event",
            {"description": "https://example.com/page?id=123"},
            seen,
        )
        assert warning is None

    def test_create_calendar_event_checked(self):
        """create_calendar_event 也在写操作白名单中"""
        seen = {"https://real.com/event"}
        warning, flagged = check_url_provenance(
            "create_calendar_event",
            {"description": "https://hallucinated-domain.com/event-123456"},
            seen,
        )
        assert warning is not None
        assert len(flagged) > 0

    def test_send_mail_checked(self):
        """send_mail 也在写操作白名单中"""
        seen = {"https://real.com/doc"}
        warning, _ = check_url_provenance(
            "send_mail",
            {"body": "See https://fake-doc-domain.com/xxx"},
            seen,
        )
        assert warning is not None


# ── 域名降级 ──

class TestDomainFallback:
    def test_same_domain_different_path_is_soft_warning(self):
        """同域名但不同路径 → 软警告（⚠️）而非硬拦截（⛔）"""
        seen = {"https://www.eventbrite.com/e/real-event-123"}
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "https://www.eventbrite.com/e/fake-event-999"},
            seen,
        )
        assert warning is not None
        assert "⚠️" in warning  # 软警告，不是 ⛔
        assert len(flagged) == 1

    def test_same_domain_still_blocked_first_time(self):
        """同域名 URL 第一次仍然被拦截（只是降级为软警告）"""
        seen = {"https://luma.com/real-party"}
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "https://luma.com/fabricated-party"},
            seen,
        )
        assert warning is not None
        assert len(flagged) == 1

    def test_unknown_domain_is_hard_block(self):
        """完全未见过的域名 → 硬拦截（⛔）"""
        seen = {"https://eventbrite.com/e/real"}
        warning, _ = check_url_provenance(
            "update_calendar_event",
            {"description": "https://never-seen-domain.xyz/page"},
            seen,
        )
        assert warning is not None
        assert "⛔" in warning


# ── 死循环保护 ──

class TestDeadLoopProtection:
    def test_first_block_works(self):
        """第一次拦截正常"""
        seen = {"https://real.com/a"}
        blocked: set[str] = set()
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "https://unknown-domain.com/fake"},
            seen, blocked,
        )
        assert warning is not None
        assert len(flagged) == 1

    def test_second_attempt_same_url_passes(self):
        """同一 URL 第二次尝试 → 放行（防止死循环）"""
        seen = {"https://real.com/a"}
        blocked = {"https://unknown-domain.com/fake"}  # 已被拦截过
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "https://unknown-domain.com/fake"},
            seen, blocked,
        )
        assert warning is None  # 放行

    def test_different_url_still_blocked(self):
        """被拦截过 URL A，新的 URL B 仍然正常拦截"""
        seen = {"https://real.com/a"}
        blocked = {"https://unknown-domain.com/fake-1"}  # 之前拦截的是另一个
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "https://another-unknown.com/fake-2"},
            seen, blocked,
        )
        assert warning is not None
        assert len(flagged) == 1

    def test_domain_match_url_also_gets_loop_protection(self):
        """同域名 URL（软警告）也有死循环保护"""
        seen = {"https://eventbrite.com/e/real-event"}
        blocked = {"https://eventbrite.com/e/fabricated-event"}  # 之前被软警告拦截
        warning, flagged = check_url_provenance(
            "update_calendar_event",
            {"description": "https://eventbrite.com/e/fabricated-event"},
            seen, blocked,
        )
        assert warning is None  # 第二次放行


# ── 前缀/反向前缀匹配 ──

class TestPrefixMatching:
    def test_prefix_match_with_extra_params(self):
        """已见 URL 的 path 是当前 URL 的前缀（LLM 加了 query param）"""
        seen = {"https://example.com/events/12345"}
        warning, _ = check_url_provenance(
            "update_calendar_event",
            {"description": "https://example.com/events/12345?ref=calendar"},
            seen,
        )
        assert warning is None

    def test_reverse_prefix_match(self):
        """当前 URL 是已见 URL 的前缀（LLM 截掉了 query param）"""
        seen = {"https://example.com/events/12345?utm_source=sheet&id=abc"}
        warning, _ = check_url_provenance(
            "update_calendar_event",
            {"description": "https://example.com/events/12345"},
            seen,
        )
        assert warning is None

    def test_multiple_urls_mixed_real_and_fake(self):
        """参数中有多个 URL，真假混合"""
        seen = {"https://real.com/event-1", "https://real.com/event-2"}
        warning, flagged = check_url_provenance(
            "send_feishu_message",
            {"content": "真: https://real.com/event-1 假: https://totally-new-domain.org/event-3"},
            seen,
        )
        assert warning is not None
        # 只有 fake 被标记
        assert any("totally-new-domain" in u for u in flagged)
        assert not any("real.com" in u for u in flagged)
