from abc import ABC, abstractmethod

from app.services.knowledge.document_types import Chunk, Section
from app.services.knowledge.semantic_chunker import SemanticChunker


class DocumentParser(ABC):
    def __init__(self, *, token_encoding: str = "cl100k_base") -> None:
        self.chunker = SemanticChunker(encoding_name=token_encoding)

    @abstractmethod
    def parse(self, file_path: str) -> list[Section]:
        raise NotImplementedError

    def chunk_sections(
        self,
        sections: list[Section],
        doc_id: str,
        permission_level: str = "internal",
        source_file: str = "",
        document_title: str = "",
    ) -> list[Chunk]:
        return self.chunker.build(
            sections,
            doc_id=doc_id,
            permission_level=permission_level,
            source_file=source_file,
            document_title=document_title,
        )
