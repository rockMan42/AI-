import asyncio
import logging
import random

import httpx
import tiktoken
from sqlalchemy import Sequence

from app.config.settings import Settings

log = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

class EmbeddingError(RuntimeError):
    """"Embedding服务调用失败"""

class EmbeddingInputTooLongError(EmbeddingError):
    """输入超过模型上下文限制"""

class EmbeddingClient:
    def __init__(self,
                 settings: Settings,
                 *,
                 transport: httpx.AsyncBaseTransport | None = None
                 ) -> None:
        self._api_key = settings.dashscope_api_key
        self._model = settings.embedding_model
        self._endpoint = f"{settings.embedding_base_url.rstrip("/")}/embeddings"
        self._dimension = settings.embedding_dimension
        self._batch_size = settings.embedding_batch_size
        self._max_tokes = settings.embedding_max_tokens
        self._max_retries = settings.embedding_max_retries
        self._backoff_seconds = settings.embedding_backoff_seconds

        self._encoding = tiktoken.get_encoding(settings.knowledge_token_encoding)
        self._client = httpx.AsyncClient(
            timeout=settings.embedding_timeout_seconds,
            transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def embed_texts(self,
                          texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        if not self._api_key:
            raise EmbeddingError("Embedding的API密钥不能为空")

        normalized = [self._validate_text(text) for text in texts]
        result: list[list[float]] = []

        for start in range(0,len(normalized),self._batch_size):
            batch = normalized[start:start + self._batch_size]
            result.extend(await self._request_with_retry(batch))

        return result

    # 校验文本
    def _validate_text(self,text: str) -> str:
        normalized = text.strip()
        if not normalized:
            raise EmbeddingError("Embedding的输入不能为空")

        token_count = len(self._encoding.encode(normalized, disallowed_special=()))

        if token_count > self._max_tokes:
            raise EmbeddingError(f"Embedding的输入不能超过{self._max_tokes}个token")

        return normalized

    # 带重试的请求
    async def _request_with_retry(self,texts: list[str]) -> list[list[float]]:
        try:
            for attempt in range(0,self._max_retries + 1):
                response = await self._client.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "input": texts,
                        "dimensions":self._dimension,
                        "encoding_format": "float"
                    },
                )

                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    await self._wait_before_retry(response,attempt)
                    continue

                response.raise_for_status()
                return self._parse_response(response,len(texts))
        except(httpx.TimeoutException,
               httpx.NetworkError) as exec:
            if attempt >= self._max_retries:
                raise EmbeddingError("Embedding服务网络调用失败") from exec
            await self._wait_before_retry(response, attempt)
        except httpx.HTTPStatusError as exec:
            raise EmbeddingError(f"Embedding服务返回HTTP{exec.response.status_code}") from exec

        raise EmbeddingError("Embedding服务调用失败")

    # 等待重试
    async def _wait_before_retry(self, response: httpx.Response | None, attempt: int) -> None:
        retry_after: float = 0.0

        if response is not None:
            raw_retry_after = response.headers.get("Retry_After")
            if raw_retry_after:
                try:
                    retry_after = float(raw_retry_after)
                except Exception as e:
                    retry_after = 0.0

        exponential = self._backoff_seconds * (2 ** attempt)
        jitter = random.uniform(0,self._backoff_seconds * 0.2)
        delay = max(retry_after, exponential + jitter)

        log.warning(
            "Embedding调用失败，准备重试 attempt = %s batch_size = %s",
            attempt + 1,
            len(response.request.content) if response is not None else 0
        )

        await asyncio.sleep(delay)


    def _parse_response(self,
                        response: httpx.Response,
                        expected_count: int) -> list[list[float]]:
        try:
            payload = response.json()

            rows = sorted(payload["data"],
                          key=lambda item: int(item["index"]))
            vectors = [row["embedding"] for row in rows]
        except (KeyError,TypeError,ValueError) as exec:
            raise EmbeddingError("Embedding响应的数据结构不匹配") from exec

        if len(vectors) != expected_count:
            raise EmbeddingError("Embedding返回的数量不匹配")

        if any(
            not isinstance(vectors,list)
            or
            len(vector) != self._dimension
            for vector in vectors
        ):
            raise EmbeddingError("Embedding返回的维度不匹配")

        return vectors

_embedding_client: EmbeddingClient | None = None

# 初始化向量化客户端
async def init_embedding(settings: Settings) -> EmbeddingClient:
    global _embedding_client

    if _embedding_client is None:
        _embedding_client = EmbeddingClient(settings)

    return _embedding_client

# 关闭向量化客户端
async def close_embedding() -> None:
    global _embedding_client
    client = _embedding_client
    _embedding_client = None

    if client is not None:
        await client.close()

# 向量化文本
async def embed_texts(
        texts: Sequence[str],
        settings: Settings
) -> list[list[str]]:

    # 初始化向量化客户端
    client = await init_embedding(settings)

    # 调用向量化接口
    return await client.embed_texts(texts)


