"""Phase 2 测试：抖音 API 发布通道（respx mock）/ 小红书 RPA 通道 / 图文成片校验 / 异步对账。"""
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pytest
import respx
import httpx

from app import models
from app.config import settings
from app.core import crypto


def _make_item(db, status="approved"):
    item = models.ContentItem(title="测试内容", topic="主题", selling_points=["卖点"],
                              status=status)
    db.add(item)
    db.commit()
    return item


def _make_douyin_account(db):
    acc = models.MatrixAccount(
        platform="douyin", account_name=f"抖音号_{uuid.uuid4().hex[:6]}",
        auth_type="api", open_id=f"openid_{uuid.uuid4().hex[:8]}", status="active",
        credentials_enc=crypto.encrypt_json({
            "access_token": "old_token", "refresh_token": "refresh_token_1",
            "expires_in": 7200, "saved_at": datetime.utcnow().isoformat(),
        }))
    db.add(acc)
    db.commit()
    return acc


def _make_video_version(db, item, tmp_path):
    """造一个带真实视频文件的版本。"""
    video_path = os.path.join(tempfile.mkdtemp(prefix="test_media_"), "v.mp4")
    with open(video_path, "wb") as f:
        f.write(b"\x00" * 1024)
    m = models.Material(kind="video", path=video_path, mime="video/mp4", size=1024)
    db.add(m)
    db.flush()
    v = models.ContentVersion(
        content_item_id=item.id, platform="douyin", content_type="video",
        title="测试视频", body="简介", tags=["好物"], compliance_status="passed",
        material_ids=[m.id])
    db.add(v)
    db.commit()
    return v


class TestDouyinApiChannel:
    @pytest.mark.asyncio
    @respx.mock
    async def test_upload_and_create_success(self, db):
        from app.publisher.channels.douyin_api import (VIDEO_CREATE_URL,
                                                       VIDEO_UPLOAD_URL,
                                                       DouyinApiPublishChannel)

        respx.post(VIDEO_UPLOAD_URL).mock(
            return_value=httpx.Response(200, json={"data": {"error_code": 0, "video_id": "v123"}}))
        respx.post(VIDEO_CREATE_URL).mock(
            return_value=httpx.Response(200, json={"data": {"error_code": 0, "item_id": "item_9"}}))

        item = _make_item(db)
        version = _make_video_version(db, item, None)
        account = _make_douyin_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow())
        db.add(task)
        db.commit()

        post_id, url = await DouyinApiPublishChannel().publish(db, task, version, account)
        assert post_id == "item_9"
        assert "item_9" in url

    @pytest.mark.asyncio
    @respx.mock
    async def test_business_error_raises(self, db):
        """平台业务错误码非 0 → 抛异常（调度器计重试）。"""
        from app.publisher.channels.douyin_api import (VIDEO_UPLOAD_URL,
                                                       DouyinApiPublishChannel)

        respx.post(VIDEO_UPLOAD_URL).mock(
            return_value=httpx.Response(200, json={"data": {"error_code": 2190008,
                                                            "description": "视频违规"}}))
        item = _make_item(db)
        version = _make_video_version(db, item, None)
        account = _make_douyin_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow())
        db.add(task)
        db.commit()

        with pytest.raises(RuntimeError, match="视频上传失败"):
            await DouyinApiPublishChannel().publish(db, task, version, account)

    @pytest.mark.asyncio
    @respx.mock
    async def test_token_expired_refresh_and_retry(self, db):
        """token 失效错误码 → 自动刷新 → 重放成功。"""
        from app.publisher.channels.douyin_api import (TOKEN_REFRESH_URL,
                                                       VIDEO_CREATE_URL,
                                                       VIDEO_UPLOAD_URL,
                                                       DouyinApiPublishChannel)

        respx.post(VIDEO_UPLOAD_URL).mock(
            return_value=httpx.Response(200, json={"data": {"error_code": 0, "video_id": "v1"}}))
        create_calls = {"n": 0}

        def create_side_effect(request):
            create_calls["n"] += 1
            if request.headers.get("access-token") == "old_token":
                return httpx.Response(200, json={"data": {"error_code": 2100004}})
            return httpx.Response(200, json={"data": {"error_code": 0, "item_id": "item_new"}})

        respx.post(VIDEO_CREATE_URL).mock(side_effect=create_side_effect)
        respx.post(TOKEN_REFRESH_URL).mock(
            return_value=httpx.Response(200, json={"data": {
                "error_code": 0, "access_token": "new_token",
                "refresh_token": "new_refresh", "expires_in": 7200}}))

        item = _make_item(db)
        version = _make_video_version(db, item, None)
        account = _make_douyin_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow())
        db.add(task)
        db.commit()

        post_id, _ = await DouyinApiPublishChannel().publish(db, task, version, account)
        assert post_id == "item_new"
        # 新 token 已加密落库
        creds = crypto.decrypt_json(db.get(models.MatrixAccount, account.id).credentials_enc)
        assert creds["access_token"] == "new_token"

    @pytest.mark.asyncio
    @respx.mock
    async def test_daily_quota_raises_quota_exceeded(self, db):
        """75 条上限错误码 → QuotaExceeded（调度器推迟次日不重试）。"""
        from app.publisher.channels.douyin_api import (VIDEO_UPLOAD_URL,
                                                       DouyinApiPublishChannel,
                                                       QuotaExceeded)

        respx.post(VIDEO_UPLOAD_URL).mock(
            return_value=httpx.Response(200, json={"data": {"error_code": 2114007}}))
        item = _make_item(db)
        version = _make_video_version(db, item, None)
        account = _make_douyin_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow())
        db.add(task)
        db.commit()

        with pytest.raises(QuotaExceeded):
            await DouyinApiPublishChannel().publish(db, task, version, account)

    @pytest.mark.asyncio
    async def test_no_video_material_raises(self, db):
        """无视频素材 → 明确报错提示走图文成片。"""
        from app.publisher.channels.douyin_api import DouyinApiPublishChannel

        item = _make_item(db)
        version = models.ContentVersion(
            content_item_id=item.id, platform="douyin", content_type="video",
            title="t", compliance_status="passed", material_ids=[])
        db.add(version)
        db.commit()
        account = _make_douyin_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow())
        db.add(task)
        db.commit()
        with pytest.raises(RuntimeError, match="视频素材"):
            await DouyinApiPublishChannel().publish(db, task, version, account)


class TestXhsRpaChannel:
    @pytest.mark.asyncio
    async def test_publish_writes_outbox(self, db):
        """发布写 outbox：account/msg_type/content JSON 字段正确，返回临时 outbox 标识。"""
        from app.publisher.channels.xhs_rpa import XhsRpaPublishChannel

        item = _make_item(db)
        version = models.ContentVersion(
            content_item_id=item.id, platform="xiaohongshu", content_type="note",
            title="小红书标题", body="正文", tags=["种草"], material_ids=[7],
            compliance_status="passed")
        db.add(version)
        db.commit()
        account = models.MatrixAccount(
            platform="xiaohongshu", account_name="小红书主号", auth_type="rpa",
            rpa_account="xhs_main", status="active")
        db.add(account)
        db.commit()
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow())
        db.add(task)
        db.commit()

        post_id, url = await XhsRpaPublishChannel().publish(db, task, version, account)
        assert post_id.startswith("outbox:")
        assert url == ""

        outbox_id = int(post_id.split(":")[1])
        row = db.get(models.RpaOutbox, outbox_id)
        assert row is not None
        assert row.account == "xhs_main"
        assert row.platform == "xiaohongshu"
        assert row.msg_type == "publish_note"
        payload = json.loads(row.content)
        assert payload["title"] == "小红书标题"
        assert payload["material_ids"] == [7]
        assert payload["task_id"] == task.id


class TestAsyncReconcile:
    @pytest.mark.asyncio
    async def test_outbox_acked_marks_success(self, db):
        """Worker 回执 acked（带笔记 URL）→ 任务 success + Post 落库。"""
        from app.publisher.scheduler import reconcile_async_tasks

        item = _make_item(db)
        version = models.ContentVersion(
            content_item_id=item.id, platform="xiaohongshu", content_type="note",
            title="t", compliance_status="passed")
        db.add(version)
        db.commit()
        account = models.MatrixAccount(
            platform="xiaohongshu", account_name="x", auth_type="rpa",
            open_id=f"openid_{uuid.uuid4().hex[:8]}",
            rpa_account=f"xhs_{uuid.uuid4().hex[:6]}", status="active")
        db.add(account)
        db.commit()
        outbox = models.RpaOutbox(account="xhs_main", platform="xiaohongshu",
                                  msg_type="publish_note", content="{}",
                                  status="acked", result="https://xhs.local/note/abc")
        db.add(outbox)
        db.flush()
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow(), status="publishing",
                                  platform_post_id=f"outbox:{outbox.id}")
        db.add(task)
        db.commit()

        await reconcile_async_tasks()
        db.expire_all()
        task = db.get(models.PublishTask, task.id)
        assert task.status == "success"
        assert task.post_url == "https://xhs.local/note/abc"
        post = db.query(models.Post).filter(models.Post.publish_task_id == task.id).first()
        assert post is not None

    @pytest.mark.asyncio
    async def test_outbox_failed_marks_failed(self, db):
        from app.publisher.scheduler import reconcile_async_tasks

        item = _make_item(db)
        version = models.ContentVersion(
            content_item_id=item.id, platform="xiaohongshu", content_type="note",
            title="t", compliance_status="passed")
        db.add(version)
        db.commit()
        account = models.MatrixAccount(
            platform="xiaohongshu", account_name="x", auth_type="rpa",
            open_id=f"openid_{uuid.uuid4().hex[:8]}",
            rpa_account=f"xhs_{uuid.uuid4().hex[:6]}", status="active")
        db.add(account)
        db.commit()
        outbox = models.RpaOutbox(account="xhs_main", platform="xiaohongshu",
                                  msg_type="publish_note", content="{}",
                                  status="failed", error="登录过期")
        db.add(outbox)
        db.flush()
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow(), status="publishing",
                                  platform_post_id=f"outbox:{outbox.id}")
        db.add(task)
        db.commit()

        await reconcile_async_tasks()
        db.expire_all()
        task = db.get(models.PublishTask, task.id)
        assert task.status == "failed"
        assert "登录过期" in task.error

    @pytest.mark.asyncio
    async def test_outbox_timeout_marks_failed(self, db):
        """Worker 超时未回执（30 分钟）→ failed。"""
        from app.publisher.scheduler import reconcile_async_tasks

        item = _make_item(db)
        version = models.ContentVersion(
            content_item_id=item.id, platform="xiaohongshu", content_type="note",
            title="t", compliance_status="passed")
        db.add(version)
        db.commit()
        account = models.MatrixAccount(
            platform="xiaohongshu", account_name="x", auth_type="rpa",
            open_id=f"openid_{uuid.uuid4().hex[:8]}",
            rpa_account=f"xhs_{uuid.uuid4().hex[:6]}", status="active")
        db.add(account)
        db.commit()
        outbox = models.RpaOutbox(account="xhs_main", platform="xiaohongshu",
                                  msg_type="publish_note", content="{}", status="leased")
        db.add(outbox)
        db.flush()
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow(), status="publishing",
                                  platform_post_id=f"outbox:{outbox.id}")
        db.add(task)
        db.commit()
        # 强制 updated_at 到 40 分钟前
        db.execute(
            models.PublishTask.__table__.update()
            .where(models.PublishTask.id == task.id)
            .values(updated_at=datetime.utcnow() - timedelta(minutes=40))
        )
        db.commit()

        await reconcile_async_tasks()
        db.expire_all()
        assert db.get(models.PublishTask, task.id).status == "failed"


class TestComposerValidation:
    def test_douyin_video_spec(self, db):
        from app.creator.composer import validate_material

        ok = models.Material(kind="video", mime="video/mp4", duration_seconds=60)
        assert validate_material("douyin", ok) == []
        bad_mime = models.Material(kind="video", mime="video/avi", duration_seconds=60)
        assert any("mp4" in p for p in validate_material("douyin", bad_mime))
        too_long = models.Material(kind="video", mime="video/mp4", duration_seconds=1000)
        assert any("15 分钟" in p for p in validate_material("douyin", too_long))

    def test_xhs_image_count_limit(self, db):
        from app.creator.composer import validate_version_materials

        item = _make_item(db)
        materials = []
        for _ in range(10):
            m = models.Material(kind="image", path="/tmp/x.png", mime="image/png")
            db.add(m)
            materials.append(m)
        db.flush()
        version = models.ContentVersion(
            content_item_id=item.id, platform="xiaohongshu", content_type="note",
            title="t", material_ids=[m.id for m in materials])
        db.add(version)
        db.commit()
        problems = validate_version_materials("xiaohongshu", version, db)
        assert any("9 张" in p for p in problems)

    def test_compose_args_structure(self):
        """ffmpeg 命令构造：输入/滤镜/输出映射齐全。"""
        from app.creator.composer import _build_compose_args

        args = _build_compose_args(["/a.png", "/b.png"], None, None, "/out.mp4")
        assert args[0] == "-y"
        assert args.count("-loop") == 2
        assert "concat=n=2" in " ".join(args)
        assert "1080x1920" in " ".join(args)
        # 带字幕与音频
        args2 = _build_compose_args(["/a.png"], "D:/tmp/s.srt", "/tts.mp3", "/out.mp4")
        joined = " ".join(args2)
        assert "subtitles" in joined and "D\\:/tmp/s.srt" in joined
        assert "-shortest" in joined
