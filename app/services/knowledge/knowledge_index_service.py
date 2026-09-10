import asyncio
import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import and_
from app.utils.time import utc_now
from app.config.settings import Settings
from app.core.database import create_session
from app.core.embedding_client import embed_texts
from app.core.milvus_client import (
    deactivate_old_versions,
    delete_version_vectors,
    insert_vectors,
    search_vectors,
)
from app.models import KnowledgeDocument, KnowledgeChunk
from app.models.user import User
from app.security.auth import accessible_permission_level

log = logging.getLogger(__name__)

_worker: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


class DocumentNotReadyError(RuntimeError):
    pass

class DocumentNotEmbeddingError(LookupError):
    pass

class DocumentEmbeddingNotFoundError(LookupError):
    pass


async def request_document_embedding(
        *,
        doc_id: str,
        version: str | None,
        force: bool,
        db: AsyncSession
) -> KnowledgeDocument:

    statement = (
        select(KnowledgeDocument)
        .where(KnowledgeDocument.doc_id == doc_id)
        .with_for_update()
    )

    if version:
        statement = statement.where(
            KnowledgeDocument.version == version
        )
    else:
        statement = statement.order_by(
            KnowledgeDocument.created_at.desc(),
            KnowledgeDocument.id.desc(),
        ).limit(1)

    statement = statement.with_for_update()

    result = await db.execute(statement)
    document = result.scalar_one_or_none()

    if document is None:
        raise DocumentNotEmbeddingError(f"文档{doc_id}未找到")

    if document.status in {
        "parsing",
        "failed",
        "deleting",
        "delete_failed",
    }:
        raise DocumentNotReadyError(f"当前文档{doc_id}状态不允许向量化")

    if document.status == "embedded" and not force:
        return document

    document.status = "completed"
    document.embedding_attempts = 0
    document.embedding_started_at = None
    document.error_message = None

    await db.execute(
        update(KnowledgeChunk)
        .where(
            KnowledgeChunk.doc_id == doc_id,
            KnowledgeChunk.doc_version == document.version
        )
        .values(
            embedding_status = "pending"
        )
    )

    await db.commit()
    await db.refresh(document)

    return document


async def _load_chunk_snapshot(document_id: int) -> list[KnowledgeChunk]:
    async with create_session() as db:
        document = await db.get(KnowledgeDocument, document_id)

        if document is None or document.status != "embedding":
            return []

        result = await db.execute(select(KnowledgeChunk)
                        .where(KnowledgeChunk.doc_id == document.doc_id,
                        KnowledgeChunk.doc_version == document.version).order_by(
                        KnowledgeChunk.created_at, KnowledgeChunk.id))

        return list(result.scalars())


def _validate_snapshot(chunks: list[KnowledgeChunk], snapshot: list[KnowledgeChunk]) -> None:

    current = [
        (
            chunk.id,
            chunk.title_path,
            chunk.chunk_text
        )
        for chunk in chunks
    ]


    previous = [
        (
            chunk.id,
            chunk.title_path,
            chunk.chunk_text
        )
        for chunk in snapshot
    ]

    if current != previous:
        raise RuntimeError("文档向量化期间Chunk内容发生变化")


async def _mark_embedding_failed(
    document_id: int,
    settings: Settings,
) -> None:

    async with create_session() as db:
        document = await db.get(
            KnowledgeDocument,
            document_id
        )

        if document is None:
            return

        exhausted = (
                document.embedding_attempts >= (
            settings.knowledge_embedding_max_attempts
        )
    )

        document.status = "embedding_failed"
        document.embedding_started_at = None
        document.error_message = "向量化重试次数达到上限" if exhausted else "向量化失败"

        await db.execute(
            update(KnowledgeChunk)
            .where(
                KnowledgeChunk.doc_id == document.doc_id,
                KnowledgeChunk.doc_version == document.version
            )
        .values(
            embedding_status="failed" if exhausted else "pending"
        )
    )

        await db.commit()

async def process_pending_documents(
        settings: Settings
) -> int:
    # 把状态设置为 embedding
    document_ids = await _claim_documents(settings)

    for document_id in document_ids:
        await _process_documents(document_id, settings)

    return len(document_ids)

# 将文档状态标记为 embedding
async def _claim_documents(settings: Settings) -> list[int]:
    stale_before = utc_now() - timedelta(
        seconds=settings.knowledge_embedding_stale_seconds
    )

    # 将超时的文档标记为失败
    async with create_session() as db:
        await db.execute(
            update(KnowledgeDocument)
            .where(
                KnowledgeDocument.status == "embedding",
                KnowledgeDocument.embedding_started_at < stale_before
            )
            .values(
                status="embedding_failed",
                error_message="embedding_worker_intterrupted",
                embedding_started_at=None,
            )
        )

        result = await db.execute(
            select(KnowledgeDocument)
            .where(
                or_(KnowledgeDocument.status == "completed", and_(KnowledgeDocument.status == "embedding_failed", KnowledgeDocument.embedding_attempts < settings.knowledge_embedding_max_attempts))
            )
            .order_by(KnowledgeDocument.id)
            .limit(settings.knowledge_embedding_worker_batch_size)
            .with_for_update(skip_locked=True)
        )

        documents = list(result.scalars())

        started_at = utc_now()
        for document in documents:
            document.status = "embedding"
            document.embedding_attempts += 1
            document.embedding_started_at = started_at
            document.error_message = None

            await db.execute(
                update(KnowledgeChunk)
                .where(KnowledgeChunk.doc_id == document.doc_id, KnowledgeChunk.doc_version == document.version)
                .values(embedding_status="processing")
            )

        await db.commit()

        return [document.id for document in documents]

async def _process_documents(document_id: int, settings: Settings) -> None:
    try:
        # 只读取 embedding 状态的文档
        snapshots = await _load_chunk_snapshot(document_id)

        if not snapshots:
            raise RuntimeError("没有可向量化的Chunk")

        texts = [
            f"{chunk.title_path}\n{chunk.chunk_text}"
            for chunk in snapshots
        ]

        vectors = await embed_texts(texts, settings)

        async with create_session() as db:
            document = await db.get(
                KnowledgeDocument,
                document_id
            )

            if document is None or document.status != "embedding":
                return

            # 锁定相同业务doc_id的全部版本，保证版本切换串行
            versions_result = await db.execute(
                                                select(KnowledgeDocument)
                                                .where(KnowledgeDocument.doc_id == document.doc_id)
                                                .order_by(KnowledgeDocument.id)
                                                .with_for_update())
            versions = list(versions_result.scalars())

            chunk_result = await db.execute(select(KnowledgeChunk)
                                            .where(KnowledgeChunk.doc_id == document.doc_id,
                                            KnowledgeChunk.doc_version == document.version)
                                            .order_by(KnowledgeChunk.chunk_index))
            chunks = list(chunk_result.scalars())

            _validate_snapshot(chunks, snapshots)

            latest_document = max(
                versions,
                key=lambda item:(
                    item.created_at,
                    item.id
                )
            )

            is_latest_version = (latest_document.id == document.id)

            rows = []

            for chunk, vector in zip(chunks, vectors,strict=True):
                _validate_milvus_fields(chunk)
                rows.append({
                    "doc_id": chunk.doc_id,
                    "chunk_index": chunk.chunk_index,
                    "title_path": chunk.title_path,
                    "chunk_text": chunk.chunk_text,
                    "permission_level": chunk.permission_level,
                    "doc_version": chunk.doc_version,
                    "is_active": is_latest_version,
                    "embedding": vector,
                })

            # 重试时先删除当前版本，避免AutoID产生重复问题
            await delete_version_vectors(document.doc_id,document.version)

            milvus_ids = await insert_vectors(rows)

            if len(milvus_ids) != len(rows):
                raise RuntimeError("Milvus返回的ID数量和Chunk数量不一致")

            if is_latest_version:
                await deactivate_old_versions(document.doc_id,document.version)

                await db.execute(
                    update(KnowledgeChunk)
                    .where(KnowledgeChunk.doc_id == document.doc_id,
                           KnowledgeChunk.doc_version != document.version)
                    .values(is_active=False)
                )

            for chunk, milvus_id in zip(
                chunks,
                milvus_ids,
                strict=True
            ):
                chunk.milvus_id = str(milvus_id)
                chunk.embedding_status = "embedded"
                chunk.is_active = is_latest_version

            document.status = "embedded"
            document.embedded_at = utc.now()
            document.embedding_started_at = None
            document.error_message = None

            await db.commit()

    except Exception as exc:
        log.exception(
            "文档向量化失败 document_id=%s error_type=%s",
            document_id,
            type(exc).__name__,
        )
        await _mark_embedding_failed(
            document_id,
            settings,
        )




def _validate_milvus_fields(
    chunk: KnowledgeChunk,
) -> None:
    fields = {
        "doc_id": (chunk.doc_id, 128),
        "title_path": (chunk.title_path, 512),
        "chunk_text": (chunk.chunk_text, 4096),
        "permission_level": (
            chunk.permission_level,
            32,
        ),
        "doc_version": (chunk.doc_version, 32),
    }

    for field_name, (value, max_length) in fields.items():
        if len(value) > max_length:
            raise ValueError(
                f"{field_name}超过Milvus限制{max_length}"
            )

async def search_knowledge(
        *,
        query: str,
        user: User,
        settings: Settings,
        limit: int = 10,
        doc_id: str | None = None
):

    allowed_permissions = await accessible_permission_level(user)
    vectors = await (embed_texts([query],settings))
    if not vectors:
        raise RuntimeError("向量化失败")
    query_vector = vectors[0]

    hits = await search_vectors(query_vector=query_vector,
                                  allowed_permissions=allowed_permissions,
                                  limit=limit,
                                  search_ef=settings.milvus_search_ef,
                                  doc_id=doc_id)

    return [
        {
            "id": int(hit["id"]),
            "score": float(
                hit.get(
                    "distance",
                    hit.get("score", 0.0),
                )
            ),
            **hit.get("entity", {}),
        }
        for hit in hits
    ]


async def start_embedding_worker(
    settings: Settings,
) -> None:
    global _worker, _stop_event

    if (
        not settings.knowledge_embedding_worker_enabled
        or _worker is not None
    ):
        return

    _stop_event = asyncio.Event()
    _worker = asyncio.create_task(
        _worker_loop(settings),
        name="knowledge-embedding-worker",
    )


async def stop_embedding_worker() -> None:
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
            await process_pending_documents(settings)
        except Exception:
            log.exception("知识向量化Worker轮询失败")

        try:
            await asyncio.wait_for(
                _stop_event.wait(),
                timeout=settings.knowledge_embedding_poll_seconds,
            )
        except TimeoutError:
            pass
