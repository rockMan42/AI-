import asyncio
import json
import logging
from typing import Any

from pymilvus import MilvusClient, DataType

from app.config.settings import Settings


_client: MilvusClient | None = None
_collection_name: str | None = None
_insert_batch_size: int = 500

log = logging.getLogger(__name__)

async def init_milvus(settings: Settings) -> MilvusClient:
    """初始化 Milvus 连接。"""
    global _client, _collection_name, _insert_batch_size
    if _client is not None:
        return _client

    client = await asyncio.to_thread(
        _create_client,
        settings
    )

    await asyncio.to_thread(client.list_collections)
    await _ensure_collection(client,settings)

    _client = client
    _collection_name = settings.milvus_collection
    _insert_batch_size = settings.milvus_insert_batch_size

    log.info("Milvus 连接初始化完成 collection=%s secure=%s",
             settings.milvus_collection,
                   settings.milvus_secure)

    return client


def _create_client(settings: Settings) -> MilvusClient:
    kwards: dict[str,Any] = {
        "uri": settings.milvus_uri,
        "db_name":settings.milvus_database,
        "timeout":settings.milvus_timeout_seconds
    }

    token = settings.milvus_token.get_secret_value()
    if token:
        kwards["token"] = token

    if settings.milvus_secure:
        kwards["secure"] = True

        if settings.milvus_server_pem_path:
            kwards["server_pem_path"] = (
                settings.milvus_server_pem_path
            )

        if settings.milvus_server_name:
            kwards["server_name"] = settings.milvus_server_name

    return MilvusClient(**kwards)

async def close_milvus() -> None:
    """关闭 Milvus 连接。"""
    global _client
    if _client is not None:
        _client.close()
        milvus_client = None


async def check_milvus() -> str:
    """检查 Milvus 连接状态。"""
    try:
        if _client is None:
            return "not_initialized"
        _client.list_collections()
        return "connected"
    except Exception as exc:
        return f"error:{exc}"

async def _ensure_collection(client: MilvusClient,
                             settings: Settings) -> None:
    exists = await asyncio.to_thread(
        client.has_collection,
        collection_name=settings.milvus_collection,
    )

    if exists:
        description = await asyncio.to_thread(
            client.describe_collection,
            collection_name=settings.milvus_collection,
        )
        _validate_schema(
            description,
            expected_dimension=settings.embedding_dimension,
        )
        await _validate_vector_index(client, settings)
        await asyncio.to_thread(
            client.load_collection,
            collection_name=settings.milvus_collection,
        )
        return

    schema = MilvusClient.create_schema(
        auto_id=True,
        enable_dynamic_field=False,
    )
    schema.add_field(
        field_name="id",
        datatype=DataType.INT64,
        is_primary=True,
        auto_id=True,
    )
    schema.add_field(
        field_name="doc_id",
        datatype=DataType.VARCHAR,
        max_length=128,
    )
    schema.add_field(
        field_name="chunk_index",
        datatype=DataType.INT32,
    )
    schema.add_field(
        field_name="title_path",
        datatype=DataType.VARCHAR,
        max_length=512,
    )
    schema.add_field(
        field_name="chunk_text",
        datatype=DataType.VARCHAR,
        max_length=4096,
    )
    schema.add_field(
        field_name="permission_level",
        datatype=DataType.VARCHAR,
        max_length=32,
    )
    schema.add_field(
        field_name="doc_version",
        datatype=DataType.VARCHAR,
        max_length=32,
    )
    schema.add_field(
        field_name="is_active",
        datatype=DataType.BOOL,
    )
    schema.add_field(
        field_name="embedding",
        datatype=DataType.FLOAT_VECTOR,
        dim=settings.embedding_dimension,
    )

    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_name="idx_enterprise_knowledge_embedding",
        index_type="HNSW",
        metric_type="COSINE",
        params={
            "M": settings.milvus_hnsw_m,
            "efConstruction": settings.milvus_hnsw_ef_construction,
        },
    )

    for field_name in (
            "doc_id",
            "permission_level",
            "doc_version",
            "is_active",
    ):
        index_params.add_index(
            field_name=field_name,
            index_name=f"idx_enterprise_knowledge_{field_name}",
            index_type="INVERTED",
        )

    await asyncio.to_thread(
        client.create_collection,
        collection_name=settings.milvus_collection,
        schema=schema,
        index_params=index_params,
    )
    await asyncio.to_thread(
        client.load_collection,
        collection_name=settings.milvus_collection,
    )

async def _validate_vector_index(
    client: MilvusClient,
    settings: Settings,
) -> None:
    index_name = "idx_enterprise_knowledge_embedding"
    indexes = await asyncio.to_thread(
        client.list_indexes,
        collection_name=settings.milvus_collection,
    )

    if index_name not in indexes:
        raise RuntimeError("Milvus Collection缺少Embedding索引")

    description = await asyncio.to_thread(
        client.describe_index,
        collection_name=settings.milvus_collection,
        index_name=index_name,
    )

    expected = {
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "M": str(settings.milvus_hnsw_m),
        "efConstruction": str(
            settings.milvus_hnsw_ef_construction
        ),
    }

    for key, value in expected.items():
        if str(description.get(key)) != value:
            raise RuntimeError(
                f"Milvus索引参数不匹配: {key}="
                f"{description.get(key)}, expected={value}"
            )


def _validate_schema(
    description: dict[str, Any],
    *,
    expected_dimension: int,
) -> None:
    fields = {
        field["name"]: field
        for field in description.get("fields", [])
    }

    required_fields = {
        "id",
        "doc_id",
        "chunk_index",
        "title_path",
        "chunk_text",
        "permission_level",
        "doc_version",
        "is_active",
        "embedding",
    }

    missing = required_fields - fields.keys()
    if missing:
        raise RuntimeError(
            f"Milvus Collection缺少字段: {sorted(missing)}"
        )

    dimension = int(
        fields["embedding"].get("params", {}).get("dim", 0)
    )
    if dimension != expected_dimension:
        raise RuntimeError(
            "Milvus向量维度不匹配: "
            f"actual={dimension}, expected={expected_dimension}"
        )



def _get_client() -> tuple[MilvusClient,str]:
    if _collection_name is None or _client is None:
        raise RuntimeError("Milvus未初始化成功")

    return _client,_collection_name

async def insert_vectors(rows: list[dict[str,Any]]) -> list[int]:
    if not rows:
        return []

    client, collection = _get_client()

    ids: list[int] = []

    for start in range(0,len(rows),_insert_batch_size):
        batch = rows[start:start + _insert_batch_size]
        result = await asyncio.to_thread(
            client.insert,
            collection_name = collection,
            data= batch
        )
        ids.extend(int(value) for value in result.get("ids",[]))

        await asyncio.to_thread(
            client.flush,
            collection_name=collection,
        )

    return ids

def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)

async def delete_version_vectors(
        doc_id: str,
        doc_version: str
):
    client,collection = _get_client()

    express = (
        f"doc_id == {_literal(doc_id)}"
        f"and doc_version == {_literal(doc_version)}"
    )

    result = await asyncio.to_thread(
        client.delete,
        collection_name = collection,
        filter=express
    )

    return int(result.get("delete_count", 0))

async def delete_document_vectors(doc_id: str) -> int:
    client, collection = _get_client()

    result = await asyncio.to_thread(
        client.delete,
        collection_name=collection,
        filter=f"doc_id == {_literal(doc_id)}",
    )
    return int(result.get("delete_count", 0))

async def deactivate_old_versions(
        doc_id: str,
        current_version: str,
) -> int:
    client,collection = _get_client()

    return await asyncio.to_thread(
        _deactivate_old_versions_sync,
        client,
        collection,
        doc_id,
        current_version
    )

def _deactivate_old_versions_sync(
    client: MilvusClient,
    collection: str,
    doc_id: str,
    current_version: str,
) -> int:
    expression = (
        f"doc_id == {_literal(doc_id)} "
        f"and doc_version != {_literal(current_version)} "
        "and is_active == true"
    )

    output_fields = [
        "id",
        "doc_id",
        "chunk_index",
        "title_path",
        "chunk_text",
        "permission_level",
        "doc_version",
        "is_active",
        "embedding",
    ]

    iterator = client.query_iterator(
        collection_name=collection,
        filter=expression,
        output_fields=output_fields,
        batch_size=_insert_batch_size,
    )

    updated = 0
    try:
        while True:
            rows = iterator.next()
            if not rows:
                break

            for row in rows:
                row["is_active"] = False

            client.upsert(
                collection_name=collection,
                data=rows,
            )
            updated += len(rows)
    finally:
        iterator.close()

    if updated:
        client.flush(collection_name=collection)

    return updated

async def search_vectors(
        query_vector: list[float],
        *,
        allowed_permissions: tuple[str,...],
        limit: int,
        search_ef: int,
        doc_id: str | None = None
) -> list[dict[str, Any]]:
    if not allowed_permissions:
        return []

    client,collection = _get_client()

    permission_values = ",".join(
        _literal(value)
        for value in allowed_permissions
    )

    express = (
        f"is_active = true"
        f"and permission_level in [{permission_values}]"
    )

    if doc_id:
        express += f"and doc_id == {doc_id}"

    result = await asyncio.to_thread(
        client.search,
        collection_name=collection,
        data=[query_vector],
        anns_field="embedding",
        filter=express,
        limit=limit,
        output_fields=[
            "doc_id",
            "chunk_index",
            "title_path",
            "chunk_text",
            "permission_level",
            "doc_version",
            "is_active",
        ],
        search_params={
            "metric_type": "COSINE",
            "params": {"ef": search_ef},
        },
    )

    return list(result[0]) if result else []


async def get_collection_status() -> dict[str, Any]:
    client, collection = _get_client()

    description, indexes, load_state = await asyncio.gather(
        asyncio.to_thread(
            client.describe_collection,
            collection_name=collection,
        ),
        asyncio.to_thread(
            client.list_indexes,
            collection_name=collection,
        ),
        asyncio.to_thread(
            client.get_load_state,
            collection_name=collection,
        ),
    )

    stats = await asyncio.to_thread(
        client.get_collection_stats,
        collection_name=collection,
    )

    return {
        "name": collection,
        "total_entities": int(stats.get("row_count", 0)),
        "fields": [
            field["name"]
            for field in description.get("fields", [])
        ],
        "indexes": list(indexes),
        "load_state": str(load_state.get("state", "Unknown")),
    }




