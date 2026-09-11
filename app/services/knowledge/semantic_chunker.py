import re
from functools import lru_cache

import tiktoken

from app.services.knowledge.document_types import Chunk, Section


@lru_cache(maxsize=8)
def _get_encoding(name: str):
    return tiktoken.get_encoding(name)


class SemanticChunker:
    """格式无关的语义切块器，保证所有输出都不超过硬上限。"""

    def __init__(
        self,
        *,
        encoding_name: str = "cl100k_base",
        target_tokens: int = 384,
        max_tokens: int = 512,
        min_tokens: int = 50,
    ) -> None:
        if not 0 < min_tokens < target_tokens <= max_tokens:
            raise ValueError("Token 限制配置不合法")
        self.encoding = _get_encoding(encoding_name)
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens

    def build(
        self,
        sections: list[Section],
        *,
        doc_id: str,
        permission_level: str = "internal",
        source_file: str = "",
        document_title: str = "",
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        has_document_root = any(
            section.level == 0 and section.title for section in sections
        )

        for title_path, blocks in self._flatten_sections(sections):
            if document_title and not has_document_root:
                title_path = (
                    f"{document_title} > {title_path}"
                    if title_path else document_title
                )

            section_chunks = []
            for chunk_text in self._split_blocks(blocks):
                section_chunks.append(Chunk(
                    doc_id=doc_id,
                    chunk_index=len(chunks),
                    title_path=title_path,
                    chunk_text=chunk_text,
                    token_count=self.count_tokens(chunk_text),
                    permission_level=permission_level,
                    source_file=source_file,
                ))

            chunks.extend(self._merge_short_chunks(section_chunks))

        for index, chunk in enumerate(chunks):
            chunk.chunk_index = index
        return chunks

    def count_tokens(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))

    def _flatten_sections(
        self,
        sections: list[Section],
        parent_path: str = "",
    ) -> list[tuple[str, list[str]]]:
        result: list[tuple[str, list[str]]] = []
        for section in sections:
            current_path = parent_path
            if section.title:
                current_path = (
                    f"{parent_path} > {section.title}"
                    if parent_path else section.title
                )
            if section.paragraphs:
                result.append((current_path, section.paragraphs))
            result.extend(self._flatten_sections(section.children, current_path))
        return result

    def _split_blocks(self, blocks: list[str]) -> list[str]:
        result: list[str] = []
        current = ""
        for block in self._clause_blocks(blocks):
            if self._is_markdown_table(block):
                if current:
                    result.append(current)
                    current = ""
                result.extend(self._split_table(block))
                continue

            for part in self._split(block):
                candidate = f"{current}\n{part}" if current else part
                if current and self.count_tokens(candidate) > self.target_tokens:
                    result.append(current)
                    current = part
                else:
                    current = candidate

        if current:
            result.append(current)
        return result

    @staticmethod
    def _clause_blocks(blocks: list[str]) -> list[str]:
        # 同一编号后的补充段落、条件和例外随条款一起切分。
        marker = r"(?:第[零〇一二三四五六七八九十百千万0-9]+条|[0-9]+[、．.]|[一二三四五六七八九十]+、)"
        result = []
        clause = ""
        for block in blocks:
            if SemanticChunker._is_markdown_table(block):
                if clause:
                    result.append(clause)
                    clause = ""
                result.append(block)
                continue
            for part in re.split(r"(?m)(?=^[ \t]*" + marker + r")", block):
                if not part:
                    continue
                if re.match(r"^[ \t]*" + marker, part):
                    if clause:
                        result.append(clause)
                    clause = part
                elif clause:
                    clause += "\n" + part
                else:
                    result.append(part)
        if clause:
            result.append(clause)
        return result

    def _split(self, text: str) -> list[str]:
        if self.count_tokens(text) <= self.max_tokens:
            return [text]

        segments = [
            value for value in re.findall(r".+?(?:[。！？!?]+|\.(?=\s|$)|\n+|$)", text, re.S)
            if value
        ]
        result: list[str] = []
        current = ""

        for segment in segments:
            if self.count_tokens(segment) > self.max_tokens:
                if current:
                    result.append(current)
                    current = ""
                result.extend(self._hard_split(segment))
                continue

            candidate = current + segment
            if current and self.count_tokens(candidate) > self.max_tokens:
                result.append(current)
                current = segment
            else:
                current = candidate

        if current:
            result.append(current)
        return result

    def _hard_split(self, text: str, *, prefix: str = "") -> list[str]:
        # 以 Unicode 字符边界切，不解码截断的 BPE token 字节。
        result = []
        while text:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if self.count_tokens(prefix + text[:middle]) <= self.max_tokens:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise ValueError("表头或单个字符超过分块硬上限")
            result.append(prefix + text[:low])
            text = text[low:]
        return result

    def _split_table(self, table: str) -> list[str]:
        lines = [line for line in table.splitlines() if line.strip()]
        if len(lines) < 3 or self.count_tokens(table) <= self.max_tokens:
            return self._split(table)

        header = "\n".join(lines[:2])
        result = []
        current = header
        for row in lines[2:]:
            candidate = f"{current}\n{row}"
            if self.count_tokens(candidate) <= self.target_tokens:
                current = candidate
                continue
            if current != header:
                result.append(current)
            candidate = f"{header}\n{row}"
            if self.count_tokens(candidate) <= self.max_tokens:
                current = candidate
            else:
                result.extend(self._hard_split(row, prefix=header + "\n"))
                current = header
        if current != header:
            result.append(current)
        return result

    @staticmethod
    def _is_markdown_table(text: str) -> bool:
        lines = text.lstrip().splitlines()
        return len(lines) >= 2 and lines[0].startswith("|") and "---" in lines[1]

    def _merge_short_chunks(self, chunks: list[Chunk]) -> list[Chunk]:
        if not chunks:
            return []

        merged = [chunks[0]]
        for chunk in chunks[1:]:
            previous = merged[-1]
            candidate = f"{previous.chunk_text}\n{chunk.chunk_text}"
            can_merge = (
                previous.title_path == chunk.title_path
                and (
                    previous.token_count < self.min_tokens
                    or chunk.token_count < self.min_tokens
                )
                and not self._is_markdown_table(previous.chunk_text)
                and not self._is_markdown_table(chunk.chunk_text)
                and self.count_tokens(candidate) <= self.max_tokens
            )
            if can_merge:
                previous.chunk_text = candidate
                previous.token_count = self.count_tokens(candidate)
            else:
                merged.append(chunk)

        for index, chunk in enumerate(merged):
            chunk.chunk_index = index
        return merged
