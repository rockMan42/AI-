from datetime import datetime
from enum import StrEnum

from plugins.image_gen import fal
from pydantic import ConfigDict, Field,BaseModel


class PermissionLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"

class DocumentStatus(StrEnum):
    PARSING = "parsing"
    COMPLETED = "completed"
    EMBEDDING = "embedding"
    EMBEDDED = "embedded"
    EMBEDDING_FAILED = "embedding_failed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETE_FAILED = "delete_failed"


class DocumentResponse(BaseModel):

    doc_id: str
    title: str
    source_file: str
    file_type: str
    file_size: int
    permission_level: PermissionLevel
    version: str
    chunk_count: int
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    embedded_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ChunkResponse(BaseModel):
    doc_id: str
    chunk_index: int
    title_path: str
    chunk_text: str
    token_count: int
    permission_level: PermissionLevel
    doc_version: str
    source_file: str

    model_config = ConfigDict(from_attributes=True)

class ChunkListResponse(BaseModel):
    doc_id: str
    total: int
    offset: int = Field(ge=0)
    limit: int= Field(ge=1,le=200)
    items: list[ChunkResponse]

class EmbedRequest(BaseModel):
    version: str | None = Field(default=None,min_length=1,max_length=32,pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    force: bool = False

class EmbedResponse(BaseModel):
    doc_id: str
    version: str
    status: str
    chunk_count: int
    embedded_at: datetime | None


class CollectionStatusResponse(BaseModel):
    name: str
    total_entities: int
    fields: list[str]
    indexes: list[str]
    load_state: str

class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1,max_length=1024)
    top_k: int = Field(default=5,ge=1,le=20,strict=True)
    permission_level: PermissionLevel = PermissionLevel.INTERNAL

    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra='forbid'
    )

class KnowledgeReference(BaseModel):
    doc_id: str
    doc_version: str
    doc_title: str
    title_path: str
    chunk_index: int
    score: float = Field(default=0,ge=0,le=1,allow_inf_nan=False)

class KnowledgeSearchResponse(BaseModel):
    answer: str
    references: list[KnowledgeReference]
    is_low_confidence: bool
