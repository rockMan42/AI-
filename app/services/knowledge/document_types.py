from dataclasses import dataclass, field


@dataclass(slots=True)
class Chunk:
    doc_id: str
    chunk_index: int
    title_path: str
    chunk_text: str
    token_count: int
    permission_level: str = "internal"
    source_file: str = ""


@dataclass(slots=True)
class Section:
    level: int
    title: str
    paragraphs: list[str] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)
