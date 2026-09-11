import asyncio
import logging
from contextlib import nullcontext
from app.core.rag_context import current_budget, query_scope, phase, remaining_seconds
from app.config.settings import Settings
from app.core.database import create_session
from app.core.rag_client import RAGClient, RAGError
from app.models import User, KnowledgeSearchLog, KnowledgeChunk, KnowledgeDocument
from app.schemas.scheme.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse, KnowledgeReference
from app.security.auth import search_permission_levels
from app.security.knowledge import KnowledgeSecurity
from app.services.knowledge.knowledge_index_service import search_knowledge
from sqlalchemy import select
from sqlalchemy import tuple_
CANDIDATE_TOP_K = 20

LOW_CONFIDENCE_REPLY = (
    "抱歉，我在企业知识库中没有找到与您问题高度相关的内容。"
    "建议您联系相关部门获取更准确的信息。"
)

log = logging.getLogger(__name__)

class RAGService:
    def __init__(self, settings: Settings):
        if settings.rag_rerank_min_score > settings.rag_rerank_high_score:
            raise RAGError("重排下限不能高于高相关性阈值")
        self.settings = settings
        self.security = KnowledgeSecurity(settings)
        self.client = RAGClient(settings)


    async def close(self) -> None:
        await self.client.close()

    async def search(self,
                     request: KnowledgeSearchRequest,
                     user: User) -> KnowledgeSearchResponse:

        if self.settings.knowledge_maintenance:
            raise RAGError("知识库维护中")
        scope = (nullcontext() if current_budget() else
                 query_scope(self.settings.rag_query_timeout_seconds))
        try:
            with scope, phase("service_total"):
                async with asyncio.timeout(remaining_seconds(self.settings.rag_query_timeout_seconds)):
                    return await self._search(request, user)
        except PermissionError:
            raise
        except TimeoutError:
            raise RAGError("知识检索超时，请稍后重试") from None
        except RAGError:
            raise
        except Exception as exc:
            log.error("knowledge_search_failed error_type=%s", type(exc).__name__)
            raise RAGError("知识检索服务暂不可用") from None

    async def _search(self, request: KnowledgeSearchRequest,user: User) -> KnowledgeSearchResponse:
        # 获取用户允许的知识权限级别
        with phase("authorization"):
            allowed = await search_permission_levels(user, request.permission_level.value)


        # 搜索知识库
        hits = await search_knowledge(query=request.query,
                                      user=user,
                                      settings=self.settings,
                                      limit=CANDIDATE_TOP_K,
                                      permission_level=request.permission_level.value)


        # 加载候选知识
        with phase("mysql_validation", candidates=len(hits)):
            candidates = await self._load_candidates(hits, allowed)

        log.info("knowledge_search_hits num_hits=%d", len(candidates))

        with phase("rerank", candidates=len(candidates), model=self.settings.rerank_model):
            ranked = await self.client.rerank(
                request.query,
                [f"{item['title_path']}\n{item['chunk_text']}" for item in candidates],
                request.top_k,
            ) if candidates else []

        top_score = ranked[0][1] if ranked else 0.0
        selected = [
            (candidates[index], score)
            for index, score in ranked
            if score >= self.settings.rag_rerank_min_score
        ]

        if not selected:
            response = KnowledgeSearchResponse(
                answer=LOW_CONFIDENCE_REPLY,
                references=[],
                is_low_confidence=True
            )
        else:
            context = [
                {
                    "source_id": f"S{source_id}",
                    "内容": item["chunk_text"],
                    "来源": self._source_label(
                        item["doc_title"],
                        item["title_path"],
                    ),
                }
                for source_id, (item, _) in enumerate(selected, start=1)
            ]

            with phase("generation", model=self.settings.rag_llm_model,
                       relevance="high" if top_score >= self.settings.rag_rerank_high_score else "borderline"):
                answer, used_sources = await self.client.generate_answer(request.query, context)

            references = [
                KnowledgeReference(
                    doc_id=item["doc_id"],
                    doc_version=item["doc_version"],
                    doc_title=self.security.filter_text(
                        item["doc_title"]
                    ),
                    title_path=self.security.filter_text(
                        item["title_path"]
                    ),
                    chunk_index=item["chunk_index"],
                    score=score,
                )
                for source_id, (item, score) in enumerate(selected, start=1)
                if f"S{source_id}" in used_sources
            ]

            sources = [
                f"[{self._source_label(ref.doc_title, ref.title_path)}]"
                for ref in references
            ]

            response = KnowledgeSearchResponse(
                answer=(
                        self.security.filter_text(answer)
                        + "\n\n参考来源：\n"
                        + "\n".join(sources)
                ) if references else LOW_CONFIDENCE_REPLY,
                references=references,
                is_low_confidence=not references,
            )

        with phase("log_commit"):
            await self._save_log(user=user, query=request.query,
                                 top_score=top_score, response=response)

        log.info(
            "knowledge_search_completed low=%s count=%s top_score=%.4f",
            response.is_low_confidence,
            len(response.references),
            top_score,
        )

        return response

    async def _load_candidates(
            self,
            hits: list[dict],
            allowed: tuple[str, ...],
    ) -> list[dict]:
        if not hits:
            return []

        keys = [
            (hit["doc_id"], hit["doc_version"], hit["chunk_index"])
            for hit in hits
        ]

        statement = (
            select(KnowledgeChunk, KnowledgeDocument.title)
            .join(
                KnowledgeDocument,
                (
                        KnowledgeChunk.doc_id == KnowledgeDocument.doc_id
                )
                & (
                        KnowledgeChunk.doc_version
                        == KnowledgeDocument.version
                ),
            )
            .where(
                tuple_(
                    KnowledgeChunk.doc_id,
                    KnowledgeChunk.doc_version,
                    KnowledgeChunk.chunk_index,
                ).in_(keys),
                KnowledgeChunk.is_active.is_(True),
                KnowledgeChunk.embedding_status == "embedded",
                KnowledgeChunk.permission_level.in_(allowed),
                KnowledgeDocument.status == "embedded",
                KnowledgeDocument.permission_level.in_(allowed),
            )
        )

        async with create_session() as db:
            rows = (await db.execute(statement)).all()

            current = {
                (chunk.doc_id, chunk.doc_version, chunk.chunk_index): {
                    "doc_id": chunk.doc_id,
                    "doc_version": chunk.doc_version,
                    "chunk_index": chunk.chunk_index,
                    "doc_title": title,
                    "title_path": chunk.title_path,
                    "chunk_text": chunk.chunk_text,
                    "milvus_id": chunk.milvus_id,
                }
                for chunk, title in rows
            }

        candidates = {}
        scores = {}
        seen = set()

        for hit, key in zip(hits, keys):
            item = current.get(key)

            if (
                    item is None
                    or (hit["route"], key) in seen
                    or item["milvus_id"] != str(hit["id"])
                    or not item["chunk_text"].strip()
            ):
                continue

            seen.add((hit["route"], key))
            candidates[key] = item
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + hit["rank"])

        return [candidates[key] for key in sorted(scores, key=scores.get, reverse=True)[:CANDIDATE_TOP_K]]

    async def _save_log(
            self,
            *,
            user: User,
            query: str,
            top_score: float,
            response: KnowledgeSearchResponse,
    ) -> None:
        async with create_session() as db:
            db.add(
                KnowledgeSearchLog(
                    user_id=self.security.user_identifier(user.user_id),
                    query=self.security.encrypt_query(query),
                    top_score=top_score,
                    result_count=len(response.references),
                    is_low_confidence=response.is_low_confidence,
                )
            )
            await db.commit()

    @staticmethod
    def _source_label(title: str, title_path: str) -> str:
        title = title.strip()
        sections = [
            part.strip()
            for part in title_path.split(">")
            if part.strip()
        ]

        if sections and sections[0] == title:
            sections = sections[1:]

        return " > ".join([title, *sections])
