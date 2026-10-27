"""Webhook HTTP 层测试：平台差异化 ACK 格式 + ark 数组信封拆包入队。

回归防护：小红书 ark 推送要求 {"success": true} 格式 ACK，固定返回 {"code": 0}
会被平台判定失败并反复重推；数组信封必须逐条入队，否则批量推送整批丢失。
"""
import json

from fastapi.testclient import TestClient

from app.main import app
from app.models import QueueTask

client = TestClient(app)


class TestAckFormat:
    def test_xhs_ack_format(self):
        """小红书 ark 推送 ACK：{"success": true, "error_code": 0, "error_msg": ""}。"""
        resp = client.post("/webhooks/xiaohongshu", json={"test": True})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "error_code": 0, "error_msg": ""}

    def test_douyin_ack_format(self):
        """抖音 ACK 保持 {"code": 0}。"""
        resp = client.post("/webhooks/douyin", json={"event": "im_receive_msg"})
        assert resp.status_code == 200
        assert resp.json() == {"code": 0}

    def test_mock_ack_format(self):
        resp = client.post("/webhooks/mock", json={"user_id": "u1", "content": "在吗"})
        assert resp.status_code == 200
        assert resp.json() == {"code": 0}


class TestArkBatchEnvelope:
    def test_xhs_batch_envelope_enqueued(self, db):
        """ark 数组信封 [{msgTag, sellerId, data}] 逐条入队，并注入 platform 标识。"""
        before = db.query(QueueTask).count()
        payload = [
            {"msgTag": "im_receive_msg", "sellerId": "s1",
             "data": json.dumps({"msgId": "batch_1", "conversationId": "c1",
                                 "fromUserId": "u1", "content": "在吗"})},
            {"msgTag": "im_receive_msg", "sellerId": "s1",
             "data": json.dumps({"msgId": "batch_2", "conversationId": "c1",
                                 "fromUserId": "u1", "content": "多少钱"})},
        ]
        resp = client.post("/webhooks/xiaohongshu", json=payload)
        assert resp.json()["success"] is True

        tasks = db.query(QueueTask).filter(QueueTask.id > before).all()
        assert len(tasks) == 2
        assert all(t.payload.get("platform") == "xiaohongshu" for t in tasks)
        assert {t.payload.get("msgTag") for t in tasks} == {"im_receive_msg"}

    def test_xhs_single_dict_still_works(self, db):
        """扁平 dict 推送（联调期/非 ark 形态）正常入队。"""
        before = db.query(QueueTask).count()
        resp = client.post("/webhooks/xiaohongshu",
                           json={"msg_id": "flat_1", "conversation_id": "c1",
                                 "content": "你好", "sender": {"user_id": "u1"}})
        assert resp.json()["success"] is True
        tasks = db.query(QueueTask).filter(QueueTask.id > before).all()
        assert len(tasks) == 1
        assert tasks[0].payload["platform"] == "xiaohongshu"
