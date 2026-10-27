"""抖音官方发布通道：video.create.bind（上传视频 → 创建作品）。

接口纪律：httpx + access-token 头 + 错误码非 0 抛异常 + 网络异常重试 3 次（core/http）。
token 失效（错误码 2100004 类）先刷新再重试 1 次。
每日 75 条上限（错误码 2114007）→ 抛 QuotaExceeded，调度器推迟次日不重试。
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ...core import crypto
from ...core.http import post_json
from ...models import ContentVersion, MatrixAccount, PublishTask
from .base import PublishChannel

logger = logging.getLogger(__name__)

# TODO: 确认实际接口地址（分片接口序列联调时对照官方文档核对）
VIDEO_UPLOAD_URL = "https://open.douyin.com/api/douyin/v1/video/upload/"
VIDEO_CREATE_URL = "https://open.douyin.com/api/douyin/v1/video/create_video/"
# TODO: 确认实际接口地址（视频审核状态查询接口）
VIDEO_STATUS_URL = "https://open.douyin.com/api/douyin/v1/video/status/"
TOKEN_REFRESH_URL = "https://open.douyin.com/oauth/refresh_token/"

CHUNK_THRESHOLD = 20 * 1024 * 1024  # 20MB 以上走分片（TODO: 分片接口序列联调核对）

DAILY_LIMIT_ERRNO = 2114007      # 每日发布上限
TOKEN_EXPIRED_ERRNOS = {2100004, 2100007, 2100009}  # token 失效类错误码


class QuotaExceeded(RuntimeError):
    """当日发布配额耗尽：调度器捕获后推迟次日，不计重试。"""


class DouyinApiPublishChannel(PublishChannel):
    platform = "douyin"

    async def publish(self, db: Session, task: PublishTask, version: ContentVersion,
                      account: MatrixAccount) -> tuple[str, str]:
        video_path = self._resolve_video(db, version)

        async def _do() -> tuple[str, str]:
            token = await self._valid_token(db, account)
            video_id = await self._upload_video(account, video_path, token)
            item_id = await self._create_video(account, version, video_id, token)
            return item_id, f"https://www.douyin.com/video/{item_id}"

        try:
            return await _do()
        except TokenExpired:
            # token 失效：刷新后整体重放 1 次
            creds = crypto.decrypt_json(account.credentials_enc)
            await self._do_refresh(db, account, creds)
            return await _do()

    # ============ 素材解析 ============

    @staticmethod
    def _resolve_video(db: Session, version: ContentVersion) -> str:
        """取版本关联的第一个视频素材。无视频素材抛异常（应先走图文成片 composer）。"""
        from ...models import Material
        for mid in version.material_ids or []:
            m = db.get(Material, mid)
            if m is not None and m.kind == "video" and os.path.exists(m.path):
                return m.path
        raise RuntimeError(
            "该版本没有可用视频素材：请先上传视频或用「图文成片」生成（composer.compose_video）")

    # ============ token 管理 ============

    async def _valid_token(self, db: Session, account: MatrixAccount) -> str:
        creds = crypto.decrypt_json(account.credentials_enc)
        if not creds.get("access_token"):
            raise RuntimeError("账号未授权或凭证缺失，请先在账号页完成 OAuth")
        # 提前 10 分钟视为过期
        saved_at = creds.get("saved_at")
        expires_in = int(creds.get("expires_in") or 0)
        if saved_at and expires_in:
            try:
                expire_at = datetime.fromisoformat(saved_at) + timedelta(seconds=expires_in)
                if datetime.utcnow() >= expire_at - timedelta(minutes=10):
                    creds = await self._do_refresh(db, account, creds)
            except ValueError:
                pass
        return creds["access_token"]

    async def _do_refresh(self, db: Session, account: MatrixAccount, creds: dict) -> dict:
        from ...config import settings
        if not creds.get("refresh_token"):
            raise RuntimeError("token 已过期且无 refresh_token，请重新授权")
        data = await post_json(TOKEN_REFRESH_URL, params={
            "client_key": settings.douyin_client_key,
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        })
        inner = data.get("data") or {}
        if inner.get("error_code", 0) not in (0, None):
            account.status = "expired"
            db.commit()
            raise RuntimeError(f"token 刷新失败（账号已标记过期，请重新授权）: {data}")
        creds.update({
            "access_token": inner.get("access_token", ""),
            "refresh_token": inner.get("refresh_token", creds.get("refresh_token", "")),
            "expires_in": inner.get("expires_in", 0),
            "saved_at": datetime.utcnow().isoformat(),
        })
        account.credentials_enc = crypto.encrypt_json(creds)
        db.commit()
        logger.info("抖音 token 自动刷新成功 account_id=%s", account.id)
        return creds

    # ============ API 调用 ============

    async def _upload_video(self, account: MatrixAccount, video_path: str,
                            access_token: str) -> str:
        size = os.path.getsize(video_path)
        if size > CHUNK_THRESHOLD:
            # TODO: 确认实际接口地址（分片 init/upload_part/complete 序列联调核对）
            raise RuntimeError("视频超过 20MB，分片上传待联调（请压缩后重试）")
        with open(video_path, "rb") as f:
            data = await post_json(
                VIDEO_UPLOAD_URL,
                headers={"access-token": access_token},
                content=f.read(),
                timeout=120.0,
            )
        inner = data.get("data") or {}
        err = inner.get("error_code", data.get("error_code", 0))
        if err == DAILY_LIMIT_ERRNO:
            raise QuotaExceeded("抖音当日发布已达 75 条上限")
        if err in TOKEN_EXPIRED_ERRNOS:
            raise TokenExpired(f"token 失效: {data}")
        if err not in (0, None):
            raise RuntimeError(f"视频上传失败: {data}")
        video_id = inner.get("video_id") or data.get("video_id")
        if not video_id:
            raise RuntimeError(f"上传响应缺少 video_id: {data}")
        return video_id

    async def _create_video(self, account: MatrixAccount, version: ContentVersion,
                            video_id: str, access_token: str) -> str:
        text = version.title
        if version.tags:
            text += " " + " ".join(f"#{t}" for t in version.tags)
        data = await post_json(
            VIDEO_CREATE_URL,
            headers={"access-token": access_token},
            json_body={"video_id": video_id, "text": text[:55]},
            timeout=30.0,
        )
        inner = data.get("data") or {}
        err = inner.get("error_code", data.get("error_code", 0))
        if err == DAILY_LIMIT_ERRNO:
            raise QuotaExceeded("抖音当日发布已达 75 条上限")
        if err in TOKEN_EXPIRED_ERRNOS:
            raise TokenExpired(f"token 失效: {data}")
        if err not in (0, None):
            raise RuntimeError(f"创建作品失败: {data}")
        item_id = inner.get("item_id") or data.get("item_id")
        if not item_id:
            raise RuntimeError(f"创建响应缺少 item_id: {data}")
        return str(item_id)


class TokenExpired(RuntimeError):
    """抖音 token 失效（触发刷新重试）。"""
