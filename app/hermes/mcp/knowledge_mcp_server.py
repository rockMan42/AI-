import asyncio
import logging
import sys
from contextlib import asynccontextmanager, AsyncExitStack

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from mcp.server.fastmcp import Context, FastMCP
from app.config.settings import get_settings
from app.core.database import init_db, close_db, create_session
from app.core.embedding_client import init_embedding, close_embedding
from app.core.milvus_client import init_milvus, close_milvus
from app.core.rag_client import RAGError
from app.schemas.knowledge import KnowledgeSearchRequest
from app.security.knowledge import resolve_identity_token, decode_identity_context
from app.core.rag_context import query_scope, phase
from app.services.knowledge.rag_service import RAGService

log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(server: FastMCP):
    settings = get_settings()

    async with AsyncExitStack() as stack:
        await init_db(settings)
        stack.push_async_callback(close_db)

        await init_milvus(settings)
        stack.push_async_callback(close_milvus)

        await init_embedding(settings)
        stack.push_async_callback(close_embedding)

        service = RAGService(settings)
        stack.push_async_callback(service.close)

        yield service

mcp_server = FastMCP(
    name="enterprise_knowledge_mcp",
    lifespan=lifespan,
)

@mcp_server.tool(name="knowledge_search")
async def knowledge_search(
        query: str,
        identity_token: str,
        ctx: Context,
        top_k: int = 5,
        permission_level: str = "internal"
) -> dict:
    """检索企业知识并生成带来源的回答。identity_token由可信后端注入。"""

    service: RAGService = ctx.request_context.lifespan_context

    try:
        claims = decode_identity_context(identity_token, service.settings)
        with query_scope(service.settings.rag_query_timeout_seconds,
                         request_id=claims["request_id"], deadline_unix=claims["deadline"]) as budget:
            async with asyncio.timeout(budget.remaining()):
                with phase("authentication"):
                    request = KnowledgeSearchRequest(query=query, top_k=top_k,
                                                     permission_level=permission_level)
                    async with create_session() as db:
                        user = await resolve_identity_token(identity_token, service.settings, db)
                result = await service.search(request, user)
                return result.model_dump(mode="json")
    except TimeoutError:
        raise ToolError("知识问答处理超时，请稍后重试") from None

    except ValidationError:
        raise ToolError("检索参数无效") from None
    except PermissionError as exc:
        raise ToolError(str(exc)) from None
    except RAGError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:
        log.error("knowledge_search_failed error_type=%s", type(exc).__name__)
        raise ToolError("知识检索服务暂不可用") from None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        force=True,
    )
    mcp_server.run(transport="stdio")