"""小红书 OAuth 辅助工具：授权 code 换 token / 手动刷新 token。

前置：.env 已配置 XHS_APP_ID / XHS_APP_SECRET。

用法（在 backend/ 目录下执行）：
    python scripts/xhs_oauth.py exchange --code <授权code> [--write-env]
    python scripts/xhs_oauth.py refresh [--write-env]
    python scripts/xhs_oauth.py status

- exchange：聚光/商业开放平台授权回调拿到 code 后换取 accessToken + refreshToken，
  落 platform_tokens 表（系统运行后自动刷新，无需再管）
- refresh：手动强制刷新（一般不需要，系统有定时任务；调试用）
- status：查看当前 token 状态（过期时间、剩余有效期）
- --write-env：额外把新 token 写回 .env（可选；不写也不影响运行，库中 token 优先）
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.database import init_db  # noqa: E402
from app.adapters.xiaohongshu import (  # noqa: E402
    METHOD_GET_ACCESS_TOKEN,
    XiaohongshuAdapter,
    _parse_expire,
)

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _write_env(access_token: str, refresh_token: str):
    """把 token 写回 .env（存在则替换，不存在则追加）。"""
    if not os.path.exists(ENV_PATH):
        print(f".env 不存在（{ENV_PATH}），跳过写回")
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    updates = {"XHS_ACCESS_TOKEN": access_token, "XHS_REFRESH_TOKEN": refresh_token}
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(out)
    print(f"已写回 {ENV_PATH}")


def _print_token(access: str, refresh: str, access_exp, refresh_exp):
    def fmt(exp):
        if exp is None:
            return "未知"
        remain = exp - datetime.utcnow()
        return f"{exp.isoformat()} UTC（剩余 {remain.days} 天 {remain.seconds // 3600} 小时）"

    print(f"accessToken : {access[:12]}...  过期: {fmt(access_exp)}")
    print(f"refreshToken: {refresh[:12]}...  过期: {fmt(refresh_exp)}")


async def cmd_exchange(args):
    if not settings.xhs_app_id or not settings.xhs_app_secret:
        print("请先在 .env 配置 XHS_APP_ID / XHS_APP_SECRET")
        return 1
    adapter = XiaohongshuAdapter()
    data = await adapter._call_gateway(METHOD_GET_ACCESS_TOKEN, {"code": args.code})
    payload = data.get("data") or {}
    access = payload.get("accessToken") or ""
    if not access:
        print(f"换取失败: {data}")
        return 1
    refresh = payload.get("refreshToken") or ""
    access_exp = _parse_expire(payload.get("accessTokenExpiresAt"))
    refresh_exp = _parse_expire(payload.get("refreshTokenExpiresAt"))
    init_db()
    adapter._store_token(access, refresh, access_exp, refresh_exp)
    print("换取成功，已落 platform_tokens 表：")
    _print_token(access, refresh, access_exp, refresh_exp)
    if args.write_env:
        _write_env(access, refresh)
    return 0


async def cmd_refresh(args):
    init_db()
    adapter = XiaohongshuAdapter()
    try:
        access = await adapter.force_refresh()
    except Exception as exc:  # noqa: BLE001
        print(f"刷新失败: {exc}")
        return 1
    stored = adapter._load_stored_token()
    print("刷新成功：")
    _print_token(access, stored[1] if stored else "", stored[2] if stored else None,
                 stored[3] if stored else None)
    if args.write_env and stored:
        _write_env(stored[0], stored[1])
    return 0


def cmd_status(_args):
    init_db()
    adapter = XiaohongshuAdapter()
    access, refresh, access_exp = adapter._current_tokens()
    stored = adapter._load_stored_token()
    refresh_exp = stored[3] if stored else None
    if not access and not refresh:
        print("未配置任何 token（.env 与 platform_tokens 表均为空）")
        return 1
    _print_token(access, refresh, access_exp, refresh_exp)
    print(f"来源: {'platform_tokens 表' if stored and stored[0] else '.env'}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="小红书 OAuth 辅助工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exchange = sub.add_parser("exchange", help="授权 code 换 token")
    p_exchange.add_argument("--code", required=True, help="授权回调拿到的 code（10 分钟内有效）")
    p_exchange.add_argument("--write-env", action="store_true", help="把新 token 写回 .env")

    p_refresh = sub.add_parser("refresh", help="手动强制刷新 token")
    p_refresh.add_argument("--write-env", action="store_true", help="把新 token 写回 .env")

    sub.add_parser("status", help="查看当前 token 状态")

    args = parser.parse_args()
    if args.cmd == "status":
        raise SystemExit(cmd_status(args))
    coro = cmd_exchange(args) if args.cmd == "exchange" else cmd_refresh(args)
    raise SystemExit(asyncio.run(coro))


if __name__ == "__main__":
    main()
