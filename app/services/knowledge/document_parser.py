import asyncio
from pathlib import Path

from app.services.knowledge.base_parser import DocumentParser
from app.services.knowledge.semantic_chunker import SemanticChunker
from app.services.knowledge.document_types import Chunk, Section
from app.services.knowledge.pdf_parser import PDFParser
from app.services.knowledge.word_parser import WordParser


def get_parser(
    file_path: str,
    *,
    token_encoding: str = "cl100k_base",
    ocr_enabled: bool = True,
    ocr_language: str = "chi_sim+eng",
    ocr_dpi: int = 200,
    ocr_min_chars: int = 20,
) -> DocumentParser:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".pdf":
        return PDFParser(
            token_encoding=token_encoding,
            ocr_enabled=ocr_enabled,
            ocr_language=ocr_language,
            ocr_dpi=ocr_dpi,
            ocr_min_chars=ocr_min_chars,
        )
    if suffix == ".docx":
        return WordParser(token_encoding=token_encoding)
    raise ValueError(f"不支持的文件格式: {suffix}")


async def parse_and_chunk(
    file_path: str,
    doc_id: str,
    permission_level: str = "internal",
    document_title: str = "",
    *,
    token_encoding: str = "cl100k_base",
    target_tokens: int = 384,
    max_tokens: int = 512,
    min_tokens: int = 50,
    ocr_enabled: bool = True,
    ocr_language: str = "chi_sim+eng",
    ocr_dpi: int = 200,
    ocr_min_chars: int = 20,
) -> list[Chunk]:
    """在线程中完成 CPU/同步 I/O 密集型解析，避免阻塞事件循环。"""
    parser = get_parser(
        file_path,
        token_encoding=token_encoding,
        ocr_enabled=ocr_enabled,
        ocr_language=ocr_language,
        ocr_dpi=ocr_dpi,
        ocr_min_chars=ocr_min_chars,
    )
    parser.chunker = SemanticChunker(encoding_name=token_encoding, target_tokens=target_tokens,
                                     max_tokens=max_tokens, min_tokens=min_tokens)
    sections = await asyncio.to_thread(parser.parse, file_path)
    return await asyncio.to_thread(
        parser.chunk_sections,
        sections,
        doc_id,
        permission_level,
        Path(file_path).name,
        document_title.strip(),
    )


__all__ = [
    "Chunk",
    "DocumentParser",
    "PDFParser",
    "Section",
    "WordParser",
    "get_parser",
    "parse_and_chunk",
]
