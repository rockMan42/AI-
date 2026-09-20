import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.config.settings import get_settings
from app.core.database import close_db, create_session, init_db
from app.schemas.lead import FollowUpInput, LeadQuery
from app.security.lead import LeadError, verify_crm_token
from app.services.lead import LeadService


@asynccontextmanager
async def lifespan(server):
    await init_db(get_settings())
    try:
        yield {}
    finally:
        await close_db()


mcp_server = FastMCP(
    name="crm_lead_mcp",
    lifespan=lifespan,
)

service = LeadService()


async def authorize(db, token, tool_name, lead_id=None):
    user_id = verify_crm_token(token, tool_name, lead_id)
    return await service.actor(db, user_id)


@mcp_server.tool(name="query_leads")
async def query_leads(
    identity_token: str,
    params: LeadQuery,
) -> dict:
    """查询本人或授权团队的线索，支持筛选、统计和分页。"""
    try:
        async with create_session() as db:
            actor = await authorize(db, identity_token, "query_leads")
            return await service.query(db, actor, params)
    except LeadError as exc:
        raise ToolError(str(exc)) from None


@mcp_server.tool(name="get_lead_detail")
async def get_lead_detail(
    lead_id: int,
    identity_token: str,
) -> dict:
    """查询线索详情和完整跟进记录。"""
    try:
        async with create_session() as db:
            actor = await authorize(
                db, identity_token, "get_lead_detail", lead_id,
            )
            return await service.detail(db, actor, lead_id)
    except LeadError as exc:
        raise ToolError(str(exc)) from None


@mcp_server.tool(name="update_follow_up")
async def update_follow_up(
    lead_id: int,
    identity_token: str,
    body: FollowUpInput,
) -> dict:
    """追加跟进记录，并在同一事务中更新线索跟进时间。"""
    try:
        async with create_session() as db:
            actor = await authorize(
                db, identity_token, "update_follow_up", lead_id,
            )
            return await service.follow_up(db, actor, lead_id, body)
    except LeadError as exc:
        raise ToolError(str(exc)) from None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp_server.run(transport="stdio")