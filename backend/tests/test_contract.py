"""提示词契约解析测试：合法/非法 JSON、handoff 触发、reply_messages 分条。"""
import json

import pytest
from pydantic import ValidationError

from app.agent.engine import _parse_contract
from app.schemas import AgentReply


VALID = {
    "reply_messages": ["在的呢", "这款 99 哈"],
    "intent": "consult_price",
    "confidence": 0.95,
    "handoff": False,
    "handoff_reason": "",
    "tags": ["询价"],
    "lead": {"phone": "", "wechat": "", "note": ""},
    "quick_action": "none",
}


class TestParseContract:
    def test_valid_json(self):
        reply = _parse_contract(json.dumps(VALID, ensure_ascii=False))
        assert reply.reply_messages == ["在的呢", "这款 99 哈"]
        assert reply.intent == "consult_price"
        assert reply.confidence == 0.95
        assert reply.handoff is False

    def test_markdown_wrapped(self):
        """模型常见毛病：用 ```json 包裹，应容错剥掉。"""
        raw = "```json\n" + json.dumps(VALID, ensure_ascii=False) + "\n```"
        reply = _parse_contract(raw)
        assert reply.reply_messages[0] == "在的呢"

    def test_surrounding_text_tolerated(self):
        raw = "好的，这是我的回答：\n" + json.dumps(VALID, ensure_ascii=False) + "\n以上。"
        reply = _parse_contract(raw)
        assert reply.intent == "consult_price"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_contract("这不是 JSON")

    def test_invalid_enum_raises(self):
        bad = {**VALID, "intent": "不存在的意图"}
        with pytest.raises(ValidationError):
            _parse_contract(json.dumps(bad, ensure_ascii=False))

    def test_reply_messages_trimmed_and_limited(self):
        data = {**VALID, "reply_messages": ["  第一条  ", "", "  ", "第二条", "第三条", "第四条超了"]}
        reply = _parse_contract(json.dumps(data, ensure_ascii=False))
        assert reply.reply_messages == ["第一条", "第二条", "第三条"]

    def test_handoff_requires_reason(self):
        data = {**VALID, "handoff": True, "handoff_reason": "complaint"}
        reply = _parse_contract(json.dumps(data, ensure_ascii=False))
        assert reply.handoff is True
        assert reply.handoff_reason == "complaint"

    def test_defaults_fill(self):
        """最小 JSON：缺省字段自动补默认。"""
        reply = _parse_contract('{"reply_messages": ["你好"]}')
        assert reply.intent == "other"
        assert reply.confidence == 0.0
        assert reply.handoff is False
        assert reply.tags == []
        assert reply.lead.phone == ""


class TestAgentReplyModel:
    def test_confidence_range(self):
        with pytest.raises(ValidationError):
            AgentReply(reply_messages=["x"], confidence=1.5)

    def test_max_three_messages(self):
        with pytest.raises(ValidationError):
            AgentReply(reply_messages=["1", "2", "3", "4"])
