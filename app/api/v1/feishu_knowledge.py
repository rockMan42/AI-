import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models import KnowledgeChunk, KnowledgeDocument, User
from app.security.auth import ACTIVE_STATUSES, accessible_permission_level
from app.security.knowledge import KnowledgeSecurity
from app.services.knowledge.cards import source_card


log = logging.getLogger(__name__)


async def handle_knowledge_card_data(data: dict) -> dict:
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    if value.get("operation") != "source":
        return {"toast": {"type": "error", "content": "来源卡片已失效，请重新查询"}}
    doc_id = value.get("doc_id")
    version = value.get("version")
    chunk_index = value.get("chunk_index")
    if (not isinstance(doc_id, str) or not isinstance(version, str)
            or type(chunk_index) is not int or chunk_index < 0):
        return {"toast": {"type": "error", "content": "来源卡片已失效，请重新查询"}}

    try:
        async with asyncio.timeout(3):
            async with create_session() as db:
                actor = await db.scalar(select(User).where(User.feishu_open_id == open_id))
                if actor is None or actor.status not in ACTIVE_STATUSES:
                    raise HTTPException(403, "用户不存在或已停用")
                allowed = await accessible_permission_level(actor)
                row = await db.scalar(select(KnowledgeDocument).where(
                    KnowledgeDocument.doc_id == doc_id,
                    KnowledgeDocument.version == version,
                    KnowledgeDocument.status == "embedded",
                    KnowledgeDocument.permission_level.in_(allowed),
                ))
                if row is None:
                    return {"toast": {"type": "error", "content":
                            "来源已更新或您无权查看，请重新查询"}}
                chunk = await db.scalar(select(KnowledgeChunk).where(
                    KnowledgeChunk.doc_id == doc_id,
                    KnowledgeChunk.doc_version == version,
                    KnowledgeChunk.chunk_index == chunk_index,
                    KnowledgeChunk.is_active.is_(True),
                    KnowledgeChunk.permission_level.in_(allowed),
                ))
                if chunk is None:
                    return {"toast": {"type": "error", "content":
                            "来源已更新，请重新查询"}}
                security = KnowledgeSecurity(get_settings())
                result_card = source_card(
                    security.filter_text(row.title),
                    security.filter_text(chunk.title_path),
                    security.filter_text(chunk.chunk_text),
                )
        return {"toast": {"type": "success", "content": "已打开引用来源"},
                "card": {"type": "raw", "data": result_card}}
    except TimeoutError:
        return {"toast": {"type": "warning", "content": "来源加载超时，请重试"}}
    except HTTPException:
        raise
    except Exception:
        log.exception("knowledge_source_card_failed")
        return {"toast": {"type": "error", "content": "来源暂时无法查看，请稍后重试"}}
