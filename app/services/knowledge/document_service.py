import logging
from uuid import uuid4
from fastapi import UploadFile
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import UTC, datetime
from app.config.settings import Settings
from app.core.clamav_client import scan_file, VirusScannerUnavailableError, VirusFoundError
from app.core.minio_client import upload_file, delete_file
from app.models import KnowledgeDocument, KnowledgeChunk
from app.models.user import User
from app.security.auth import accessible_permission_level
from app.services.knowledge.document_parser import parse_and_chunk
from app.services.knowledge.file_validation import stage_validate_upload, ALLOWED_CONTENT_TYPES

log = logging.getLogger(__name__)

ALLOWED_STATUS = "completed", "embedding", "embedded"
class DocumentNotFoundError(LookupError):
    pass

class DocumentProcessingError(RuntimeError):
    def __init__(self,doc_id: str) -> None:
        self.doc_id = doc_id
        super().__init__(f"文档处理错误 {doc_id}")


class DuplicateDocumentVersionError:
    pass


async def create_document(
        *,
        upload: UploadFile,
        doc_id: str,
        title: str,
        permission_level: str,
        version: str,
        user: User,
        db: AsyncSession,
        settings: Settings
) -> KnowledgeDocument:

    # 阶段1：验证上传文件
    staged = await stage_validate_upload(upload, settings)
    resolved_doc_id = doc_id or _generate_doc_id()

    duplicate = await db.scalar(
        select(KnowledgeDocument.id).where(
            KnowledgeDocument.doc_id == resolved_doc_id,
            KnowledgeDocument.version == version,
        )
    )
    if duplicate is not None:
        raise DuplicateDocumentVersionError(
            f"{resolved_doc_id}:{version}"
        )

    deleting = await db.scalar(
        select(KnowledgeDocument.id).where(
            KnowledgeDocument.doc_id == resolved_doc_id,
            KnowledgeDocument.status.in_(
                ("deleting", "delete_failed")
            ),
        )
    )
    if deleting is not None:
        raise DocumentProcessingError(resolved_doc_id)

    object_key = (
        f"knowledge/{resolved_doc_id}/{version}/"
        f"source{staged.suffix}"
    )

    # 阶段2：创建文档记录
    document = KnowledgeDocument(
        doc_id=resolved_doc_id,
        title=title.strip(),
        source_file=staged.file_name,
        object_key=object_key,
        file_type=staged.suffix.lstrip("."),
        file_size=staged.file_size,
        sha256=staged.sha256,
        permission_level=permission_level,
        version=version,
        chunk_count=0,
        status="parsing",
        uploaded_by=user.user_id
    )

    db.add(document)
    await db.commit()
    await db.refresh(document)

    object_uploaded = False

    try:
        # 阶段3：扫描文件病毒
        await scan_file(staged.path,settings)

        # 阶段4：上传文档至MinIO
        await upload_file(local_path=staged.path,
                          object_key=object_key,
                          content_type=staged.content_type,
                          sha256=staged.sha256
                          )
        object_uploaded = True

        # 阶段5：解析文档并生成块
        parsed_chunks = await parse_and_chunk(file_path=str(staged.path),
                                            doc_id=resolved_doc_id,
                                            permission_level=permission_level,
                                            document_title=title.strip(),
                                            token_encoding=settings.knowledge_token_encoding,
                                            ocr_enabled=settings.knowledge_ocr_enabled,
                                            ocr_language=settings.knowledge_ocr_language,
                                            ocr_dpi=settings.knowledge_ocr_dpi,
                                            ocr_min_chars=settings.knowledge_ocr_min_chars,)

        if not parsed_chunks:
            raise DocumentProcessingError(doc_id)

        db.add_all([
            KnowledgeChunk(
                doc_id=resolved_doc_id,
                doc_version=version,
                chunk_index=chunk.chunk_index,
                title_path=chunk.title_path,
                chunk_text=chunk.chunk_text,
                token_count=chunk.token_count,
                permission_level=permission_level,
                source_file=staged.file_name,
                milvus_id=None,
                embedding_status="pending",
                is_active=False,
            )
            for chunk in parsed_chunks
        ])

        document.chunk_count = len(parsed_chunks)
        document.status = "completed"
        document.embedding_attempts = 0
        document.embedding_started_at = None
        document.embedded_at = None

        await db.commit()
        await db.refresh(document)

        return document
    except VirusFoundError:
        await _mark_failed(db, resolved_doc_id, "virus_detected")
        if object_uploaded:
            await _best_effort_delete(object_key)
        raise
    except VirusScannerUnavailableError:
        await _mark_failed(db, resolved_doc_id, "virus_scanner_unavailable")
        if object_uploaded:
            await _best_effort_delete(object_key)
        raise
    except Exception as exc:
        log.exception("文档处理失败 doc_id=%s", doc_id)
        await _mark_failed(db, resolved_doc_id, "document_processing_failed")
        if object_uploaded:
            await _best_effort_delete(object_key)

        if isinstance(exc, DocumentProcessingError):
            raise
        raise DocumentProcessingError(resolved_doc_id) from exc
    finally:
        staged.path.unlink(missing_ok=True)

def _generate_doc_id() -> str:
    now = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"doc_{now}_{uuid4().hex[:12]}"

async def _mark_failed(
        db: AsyncSession,
        doc_id: str,
        error_code: str
) -> None:
    await db.rollback()

    smtp = select(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id)
    result = await db.execute(smtp)

    document = result.scalar_one_or_none()

    if not document:
        return

    document.status = "failed"
    document.error_message = error_code[:512]
    document.chunk_count = 0
    await db.commit()


async def _best_effort_delete(object_key: str) -> None:
    try:
        await delete_file(object_key)
    except Exception as e:
        log.exception("删除MinIO文件失败 object_key=%s", object_key)


async def get_document_chunks(
        *,
        doc_id,
        db: AsyncSession,
        user: User,
        offset: int,
        limit: int
) -> tuple[int,list[KnowledgeChunk],]:

    allowed_permission = await accessible_permission_level(user)
    result = await db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.doc_id == doc_id,
                           KnowledgeDocument.status.in_(ALLOWED_STATUS),
                           KnowledgeDocument.permission_level.in_(allowed_permission)))

    document = result.scalar_one_or_none()
    if document is None:
        log.info("文档未找到 doc_id=%s", doc_id)
        raise DocumentNotFoundError(doc_id)

    total = await db.scalar(select(func.count(KnowledgeChunk.id))
                              .where(KnowledgeChunk.doc_id == document.doc_id,
                                                 KnowledgeChunk.permission_level.in_(allowed_permission),
                                                 KnowledgeChunk.is_active.is_(True)))

    result = await db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.doc_id == doc_id,
               KnowledgeChunk.permission_level.in_(allowed_permission),
               KnowledgeChunk.is_active.is_(True))
        .order_by(KnowledgeChunk.chunk_index)
        .offset(offset)
        .limit(limit))

    return int(total or 0),list(result.scalars().all())


async def delete_document(
        *,
        doc_id: str,
        db: AsyncSession
) -> None:
    result = await db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.doc_id == doc_id)
        .with_for_update()
    )
    document = result.scalar_one_or_none()

    if document is None:
        raise DocumentNotFoundError(doc_id)

    document.status = "deleting"
    document.error_message = None
    await db.commit()

    try:
        await delete_file(document.object_key)
        await db.delete(document)
        await db.commit()
    except Exception:
        log.exception("删除文档失败，等待补偿任务重试 doc_id=%s", doc_id)
        await db.rollback()
        result = await db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.doc_id == doc_id)
            .with_for_update()
        )
        failed_document = result.scalar_one_or_none()
        if failed_document is not None:
            failed_document.status = "delete_failed"
            failed_document.error_message = "delete_cleanup_failed"
            await db.commit()
        raise
