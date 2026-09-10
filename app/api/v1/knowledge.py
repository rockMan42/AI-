
from typing import Annotated

from fastapi import APIRouter, status, UploadFile, File, Form, Depends, HTTPException
from fastapi.params import Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.config.settings import Settings, get_settings
from app.core.clamav_client import VirusScannerUnavailableError, VirusFoundError
from app.core.database import get_session
from app.models.scheme import knowledge
from app.models.scheme.knowledge import (
    ChunkListResponse,
    DocumentResponse,
    PermissionLevel,
)
from app.core.milvus_client import get_collection_status
from app.models.scheme.knowledge import (
    CollectionStatusResponse,
    EmbedRequest,
    EmbedResponse,
)
from app.services.knowledge.knowledge_index_service import (
    DocumentEmbeddingNotFoundError,
    DocumentNotReadyError,
    request_document_embedding, DocumentNotEmbeddingError,
)
from app.models.user import User
from app.security.auth import require_admin, get_current_user
from app.services.knowledge.document_service import create_document, DocumentProcessingError, get_document_chunks, \
    DocumentNotFoundError, delete_document
from app.services.knowledge.file_validation import InvalidDocumentError

router = APIRouter(prefix="/knowledge/documents")
collection_router = APIRouter(prefix="/knowledge/collection")

@router.post("/upload",response_model=DocumentResponse,status_code=status.HTTP_201_CREATED)
async def upload_document(
        file: Annotated[UploadFile, File(description="上传pdf或者docx文件")],
        title: Annotated[str, Form(min_length=1, max_length=256)],
        permission_level: Annotated[PermissionLevel, Form()] = PermissionLevel.INTERNAL,
        version: Annotated[str,Form(min_length=1,max_length=32)] = "v1.0",
        doc_id: Annotated[
            str | None,
            Form(
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
            ),
        ] = None,
        user: User = Depends(require_admin),
        db: AsyncSession = Depends(get_session),
        settings: Settings = Depends(get_settings)
) -> DocumentResponse:

    try:
        document = await create_document(upload=file,
                                         title=title,
                                         doc_id=doc_id,
                                         permission_level=permission_level.value,
                                         version=version,
                                         user=user,
                                         db=db,
                                         settings=settings)

        return DocumentResponse.model_validate(document)
    except InvalidDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except VirusFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件未通过安全扫描",
        ) from exc
    except VirusScannerUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="文件安全扫描服务暂不可用",
        ) from exc
    except DocumentProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "文档解析失败",
                "doc_id": exc.doc_id,
            },
        ) from exc
    finally:
        await file.close()

@router.get("/chunks")
async def list_document_chunks(
        doc_id: str,
        offset: Annotated[int,Query(ge=0)] = 0,
        limit: Annotated[int,Query(ge=1,le=200)] = 100,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_session),
) -> ChunkListResponse:
    try:
        total,chunks = await get_document_chunks(doc_id=doc_id, db=db, user=user, offset=offset, limit=limit)
    except DocumentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在"
        ) from e

    return ChunkListResponse(
        doc_id=doc_id,
        total=total,
        offset=offset,
        limit=limit,
        items=chunks
    )


@router.delete("/{doc_id}")
async def remove_document(
        doc_id: str,
        user: User = Depends(require_admin),
        db: AsyncSession = Depends(get_session),
):
    try:
        await delete_document(doc_id=doc_id, db=db)
        return {"message": "文档删除成功"}
    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在",
        ) from exc
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="文档删除失败，请稍后重试",
        ) from exc


@collection_router.post("/{doc_id}/embed",response_model=EmbedResponse,status_code=status.HTTP_202_ACCEPTED)
async def embed_document(
        doc_id: str,
        request: EmbedRequest,
        user: User = Depends(require_admin),
        db: AsyncSession = Depends(get_session)
) -> EmbedResponse:

    try:
        document = await request_document_embedding(doc_id=doc_id, version=request.version, force=request.force, db=db)
        return EmbedResponse.model_validate(document)
    except DocumentNotReadyError as exec:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="文档未准备好"
        ) from exec
    except DocumentNotEmbeddingError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文档不存在",
        ) from exc



@collection_router.get(
    "/status",
    response_model=CollectionStatusResponse,
)
async def collection_status(
    user: User = Depends(require_admin),
) -> CollectionStatusResponse:
    try:
        data = await get_collection_status()
        return CollectionStatusResponse.model_validate(data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Milvus Collection状态查询失败",
        ) from exc