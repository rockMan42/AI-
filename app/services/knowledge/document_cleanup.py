import asyncio
import logging

from sqlalchemy import select

from app.config.settings import Settings
from app.core.database import create_session
from app.models import KnowledgeDocument
from app.services.knowledge.document_service import delete_document


log = logging.getLogger(__name__)
_worker: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


async def resume_pending_deletions(settings: Settings) -> int:
    async with create_session() as db:
        result = await db.execute(
            select(KnowledgeDocument.doc_id)
            .where(KnowledgeDocument.status.in_(("deleting", "delete_failed")))
            .order_by(KnowledgeDocument.updated_at)
            .limit(settings.knowledge_cleanup_batch_size)
        )
        doc_ids = list(result.scalars())

    for doc_id in doc_ids:
        try:
            async with create_session() as db:
                await delete_document(doc_id=doc_id, db=db)
        except Exception:
            log.exception("恢复文档删除失败 doc_id=%s", doc_id)
    return len(doc_ids)


async def start_cleanup_worker(settings: Settings) -> None:
    global _worker, _stop_event

    if settings.knowledge_maintenance:
        return
    if _worker is not None:
        return
    _stop_event = asyncio.Event()
    _worker = asyncio.create_task(_worker_loop(settings), name="knowledge-cleanup-worker")


async def stop_cleanup_worker() -> None:
    global _worker, _stop_event
    if _worker is None:
        return
    assert _stop_event is not None
    _stop_event.set()
    await _worker
    _worker = None
    _stop_event = None


async def _worker_loop(settings: Settings) -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await resume_pending_deletions(settings)
        except Exception:
            log.exception("文档删除恢复任务轮询失败")
        try:
            await asyncio.wait_for(
                _stop_event.wait(),
                timeout=settings.knowledge_cleanup_poll_seconds,
            )
        except TimeoutError:
            pass
