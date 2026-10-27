"""可靠性测试：幂等去重、频控、违禁词过滤、上下文窗口。"""
import pytest

from app.core import contentfilter, idempotency, ratelimit
from app.agent import humanize


class TestIdempotency:
    def test_first_seen_not_duplicate(self):
        assert idempotency.is_duplicate("test:event:001") is False

    def test_second_seen_is_duplicate(self):
        idempotency.is_duplicate("test:event:002")
        assert idempotency.is_duplicate("test:event:002") is True

    def test_empty_key_passes(self):
        assert idempotency.is_duplicate("") is False


class TestRateLimit:
    def setup_method(self):
        ratelimit.reset_all()

    def test_under_limit_allowed(self):
        allowed, _ = ratelimit.check_and_count("douyin", "conv_1", count=3)
        assert allowed is True

    def test_over_limit_blocked(self):
        # 抖音规则：24h 最多 6 条
        for _ in range(6):
            allowed, _ = ratelimit.check_and_count("douyin", "conv_2")
            assert allowed is True
        allowed, rule = ratelimit.check_and_count("douyin", "conv_2")
        assert allowed is False
        assert rule == "reply_24h"

    def test_user_message_resets_reply_window(self):
        for _ in range(6):
            allowed, _ = ratelimit.check_and_count("douyin", "conv_reset")
            assert allowed is True
        ratelimit.reset_reply_window("douyin", "conv_reset")
        allowed, _ = ratelimit.check_and_count("douyin", "conv_reset")
        assert allowed is True
        for _ in range(5):
            ratelimit.check_and_count("douyin", "conv_reset")
        allowed, rule = ratelimit.check_and_count("douyin", "conv_reset")
        assert allowed is False
        assert rule == "reply_24h"

    def test_reset_keeps_minute_cap(self):
        for _ in range(6):
            ratelimit.check_and_count("douyin_rpa", "conv_min")
        ratelimit.reset_reply_window("douyin", "conv_min")
        allowed, rule = ratelimit.check_and_count("douyin_rpa", "conv_min")
        assert allowed is False
        assert rule == "rpa_minute"

    def test_different_conversation_isolated(self):
        for _ in range(6):
            ratelimit.check_and_count("douyin", "conv_3")
        allowed, _ = ratelimit.check_and_count("douyin", "conv_4")
        assert allowed is True


class TestContentFilter:
    def test_clean_text_untouched(self):
        text = "这款 99 元，两件九折"
        cleaned, hits = contentfilter.sanitize(text)
        assert cleaned == text
        assert hits == []

    def test_extreme_word_replaced(self):
        cleaned, hits = contentfilter.sanitize("这是全网最低的价格")
        assert "全网最低" not in cleaned
        assert "全网最低" in hits

    def test_multiple_words(self):
        cleaned, hits = contentfilter.sanitize("我们是第一品牌，绝对好用")
        assert "第一" not in cleaned
        assert "绝对" not in cleaned

    def test_phone_masked(self):
        assert "138****5678" == contentfilter.mask_lead("13812345678")


class TestHumanize:
    def test_short_message_not_split(self):
        assert humanize.split_long_message("在的呢") == ["在的呢"]

    def test_long_message_split_at_sentence(self):
        text = "这是第一句话。" * 10 + "这是第二句话。" * 10
        chunks = humanize.split_long_message(text)
        assert len(chunks) >= 2
        assert all(len(c) <= humanize.MAX_CHUNK_LEN for c in chunks)

    def test_typing_delay_scales_with_length(self):
        assert humanize.typing_delay_ms("长" * 20) > humanize.typing_delay_ms("短")
