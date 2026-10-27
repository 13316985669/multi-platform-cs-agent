"""工具调用（function calling）：LLM 通过契约中的 tool_call 字段触发业务工具。

架构：
- Tool 基类：name / description / args_schema / run()
- TOOL_REGISTRY：全局注册表，提示词中自动注入工具说明
- execute_tool：统一执行入口，异常兜底 + 监控埋点
- ToolContext：携带会话上下文（conversation/customer/platform），供工具读写业务数据

内置示例工具（生产环境替换 run() 内的模拟实现为真实接口即可）：
- query_order(phone)：按手机号查订单
- query_logistics(order_id)：按订单号查物流
- create_ticket(type, title, content)：创建工单（真实落库 + WS 广播）
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx

from ..config import settings
from ..database import SessionLocal
from ..models import (Conversation, Customer, FunnelEvent, MatrixAccount, Ticket,
                      WecomChannelCode)

logger = logging.getLogger(__name__)


class ToolContext:
    """工具执行上下文：当前会话/客户/平台。"""

    def __init__(self, conversation_id: int, customer_id: int, platform: str):
        self.conversation_id = conversation_id
        self.customer_id = customer_id
        self.platform = platform


class Tool:
    """业务工具基类。"""
    name: str = ""
    description: str = ""
    args_schema: dict[str, str] = {}  # 参数名 -> 中文说明（注入提示词用）

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """执行工具，返回 {"ok": bool, "data": ..., "error": ...}。"""
        raise NotImplementedError


TOOL_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool):
    TOOL_REGISTRY[tool.name] = tool


def get_tool(name: str) -> Tool | None:
    return TOOL_REGISTRY.get(name)


def tools_prompt_text() -> str:
    """生成提示词中的工具说明段落。"""
    if not TOOL_REGISTRY:
        return "（当前无可用工具）"
    lines = []
    for tool in TOOL_REGISTRY.values():
        args = "、".join(f"{k}（{v}）" for k, v in tool.args_schema.items()) or "无参数"
        lines.append(f"- {tool.name}：{tool.description}。参数：{args}")
    return "\n".join(lines)


async def execute_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """统一执行入口：未知工具/异常均兜底为 ok=False，绝不抛出。"""
    from ..core import monitor  # 延迟导入避免循环依赖

    tool = get_tool(name)
    if tool is None:
        logger.warning("LLM 请求了未注册的工具: %s", name)
        monitor.record("tool_failure", f"未知工具 {name}")
        return {"ok": False, "data": None, "error": f"工具 {name} 不存在"}
    try:
        result = await tool.run(args, ctx)
        logger.info("工具执行 %s args=%s ok=%s", name, args, result.get("ok"))
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("工具执行异常 %s", name)
        monitor.record("tool_failure", f"{name}: {exc}")
        return {"ok": False, "data": None, "error": "工具暂时不可用"}


# ============ 内置工具 ============

class QueryOrderTool(Tool):
    name = "query_order"
    description = "按手机号查询用户最近的订单列表（下单时间/商品/金额/订单号/状态）"
    args_schema = {"phone": "用户手机号"}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        phone = (args.get("phone") or "").strip()
        if not phone or len(phone) != 11 or not phone.startswith("1"):
            return {"ok": False, "data": None, "error": "手机号格式不对，请向用户确认 11 位手机号"}
        if not settings.order_api_url:
            return {"ok": False, "data": None, "error": "订单接口未接入"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(settings.order_api_url, params={"phone": phone})
                resp.raise_for_status()
                return {"ok": True, "data": resp.json(), "error": ""}
        except Exception:  # noqa: BLE001
            logger.exception("订单接口调用失败")
            return {"ok": False, "data": None, "error": "订单接口暂时不可用"}


class QueryLogisticsTool(Tool):
    name = "query_logistics"
    description = "按订单号查询物流轨迹（快递公司/运单号/最新物流状态）"
    args_schema = {"order_id": "订单号"}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        order_id = (args.get("order_id") or "").strip()
        if not order_id:
            return {"ok": False, "data": None, "error": "缺少订单号，请先通过 query_order 查到订单号"}
        if not settings.logistics_api_url:
            return {"ok": False, "data": None, "error": "物流接口未接入"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(settings.logistics_api_url, params={"order_id": order_id})
                resp.raise_for_status()
                return {"ok": True, "data": resp.json(), "error": ""}
        except Exception:  # noqa: BLE001
            logger.exception("物流接口调用失败")
            return {"ok": False, "data": None, "error": "物流接口暂时不可用"}


class CreateTicketTool(Tool):
    name = "create_ticket"
    description = "创建售后/物流工单，交给人工线下跟进（用户要退货/换货/投诉处理时使用）"
    args_schema = {"type": "工单类型：refund 退款退货 | logistics 物流异常 | other 其他",
                   "title": "一句话标题", "content": "问题详情（订单号/诉求/用户联系方式等）"}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        ticket_type = args.get("type") or "other"
        if ticket_type not in ("refund", "logistics", "other"):
            ticket_type = "other"
        title = (args.get("title") or "").strip()[:120] or "用户售后诉求"
        content = (args.get("content") or "").strip()[:2000]

        ticket_no = f"T{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        db = SessionLocal()
        try:
            conv = db.get(Conversation, ctx.conversation_id)
            if conv is None:
                return {"ok": False, "data": None, "error": "会话不存在"}
            ticket = Ticket(
                ticket_no=ticket_no,
                conversation_id=ctx.conversation_id,
                customer_id=ctx.customer_id,
                type=ticket_type, title=title, content=content,
                status="open", assignee_id=conv.assignee_id,
            )
            db.add(ticket)
            db.commit()
            db.refresh(ticket)
        finally:
            db.close()

        # WS 广播，工单页实时刷新
        try:
            from ..api.ws import manager
            await manager.broadcast("ticket_created", {
                "ticket_no": ticket_no, "conversation_id": ctx.conversation_id, "title": title,
            })
        except Exception:  # noqa: BLE001
            logger.exception("工单创建广播失败")

        return {"ok": True, "data": {"ticket_no": ticket_no, "status": "open"}, "error": ""}


class PushWecomCodeTool(Tool):
    name = "push_wecom_code"
    description = ("用户已留资（手机号/微信号）且明确愿意加微信时，推送企业微信添加方式。"
                   "不要在用户未同意时主动推送")
    args_schema = {}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if not settings.wecom_configured:
            # 企微未配置：返回 ok=false，由 LLM 改口引导留资入库
            return {"ok": False, "data": None,
                    "error": "企业微信未配置，请引导用户留下微信号/手机号，由人工后续添加"}

        db = SessionLocal()
        try:
            customer = db.get(Customer, ctx.customer_id)
            # 活码选择：优先与客户来源平台匹配的绑定账号活码，否则取最新活码
            # TODO 联调校准：精准归因应生成一次性 state 的活码/口令，当前用共享活码近似
            code = None
            if customer is not None and customer.platform:
                code = (
                    db.query(WecomChannelCode)
                    .join(MatrixAccount, WecomChannelCode.bound_account_id == MatrixAccount.id)
                    .filter(MatrixAccount.platform == customer.platform)
                    .order_by(WecomChannelCode.id.desc())
                    .first()
                )
            if code is None:
                code = db.query(WecomChannelCode).order_by(WecomChannelCode.id.desc()).first()
            if code is None:
                return {"ok": False, "data": None,
                        "error": "暂无可用企业微信活码，请引导用户留下微信号/手机号，由人工后续添加"}

            db.add(FunnelEvent(
                stage="lead",
                platform=customer.platform if customer else "",
                account_id=code.bound_account_id,
                post_id=0, comment_id=0,
                customer_id=ctx.customer_id,
                guide_code=(customer.source_guide_code if customer else "") or ""))
            db.commit()

            # TODO 联调校准：抖音/小红书私信可能屏蔽外链，
            #   必要时改为「复制微信号 XXX 添加」口令形态（在企微后台查看成员微信号）
            text = f"点击链接添加我的企业微信，通过后发你专属福利：{code.qr_url}"
            return {"ok": True, "data": {
                "text": text, "qr_url": code.qr_url, "code_name": code.name,
            }, "error": ""}
        finally:
            db.close()


# 注册内置工具
register(QueryOrderTool())
register(QueryLogisticsTool())
register(CreateTicketTool())
register(PushWecomCodeTool())


def tool_result_text(name: str, result: dict[str, Any], *, allow_chain: bool) -> str:
    """把工具结果渲染为注入提示词的文本。

    allow_chain=True 时允许再调用另一个工具（如 query_order 拿到订单号后再调 query_logistics）；
    False 时（已达调用上限）要求直接输出最终回复。
    """
    chain_note = (
        "如果还需要调用另一个工具才能回答（例如刚拿到订单号、还需查物流），可以继续输出 tool_call；"
        "信息已经足够就直接输出最终回复。同一工具不要重复调用。"
        if allow_chain else
        "已达工具调用上限，这次必须直接输出最终回复 JSON，不要再输出 tool_call。"
    )
    return (
        f"\n\n【工具调用结果】你刚才调用了工具 {name}，返回如下：\n"
        f"{json.dumps(result, ensure_ascii=False)}\n"
        "请基于工具结果回答用户：结果 ok=true 就把关键信息用口语化短句告诉用户；"
        "ok=false 就安抚用户并转人工（handoff=true）。"
        f"{chain_note}"
    )
