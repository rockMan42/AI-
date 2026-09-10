import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.services.knowledge.base_parser import DocumentParser
from app.services.knowledge.document_types import Section


class WordParser(DocumentParser):
    """按 Word 原始内容顺序提取标题、段落和表格。"""

    _HEADING_STYLE_PATTERN = re.compile(
        r"^(?:heading|标题)\s*([1-9])(?:\D.*)?$",
        re.IGNORECASE,
    )
    _CHAPTER_PATTERN = re.compile(
        r"^第[一二三四五六七八九十百零〇0-9]+章(?:\s|$)"
    )
    _SECTION_PATTERN = re.compile(
        r"^第[一二三四五六七八九十百零〇0-9]+节(?:\s|$)"
    )
    _DECIMAL_HEADING_PATTERN = re.compile(
        r"^(\d+(?:\.\d+){1,2})[\s、.]"
    )

    def parse(self, file_path: str) -> list[Section]:
        document = Document(file_path)
        sections: list[Section] = []
        stack: list[Section] = []
        current_section: Section | None = None
        preamble: list[str] = []

        for block in document.iter_inner_content():
            if isinstance(block, Table):
                text = self._table_to_markdown(block)
                if text:
                    self._append_content(current_section, preamble, text)
                continue

            if not isinstance(block, Paragraph):
                continue

            text = block.text.strip()
            if not text:
                continue

            level = self._heading_level(block, before_first_heading=not stack)
            if level is None:
                self._append_content(current_section, preamble, text)
                continue

            section = Section(level=level, title=text)
            while stack and stack[-1].level >= level:
                stack.pop()
            if stack:
                stack[-1].children.append(section)
            else:
                sections.append(section)
            stack.append(section)
            current_section = section

        if preamble:
            sections.insert(0, Section(level=0, title="", paragraphs=preamble))
        return sections

    def _heading_level(
        self,
        paragraph: Paragraph,
        *,
        before_first_heading: bool,
    ) -> int | None:
        style_name = paragraph.style.name.strip()
        if style_name.casefold() in {"title", "标题"}:
            return 0

        style_match = self._HEADING_STYLE_PATTERN.match(style_name)
        if style_match:
            return int(style_match.group(1))

        outline_level = self._outline_level(paragraph)
        if outline_level is not None:
            return outline_level + 1

        text = paragraph.text.strip()
        if before_first_heading and self._looks_like_document_title(paragraph):
            return 0
        if len(text) > 100:
            return None
        if self._CHAPTER_PATTERN.match(text):
            return 1
        if self._SECTION_PATTERN.match(text):
            return 2

        decimal_match = self._DECIMAL_HEADING_PATTERN.match(text)
        if decimal_match:
            return min(decimal_match.group(1).count(".") + 1, 3)

        font_size = self._font_size(paragraph)
        if self._is_bold(paragraph) and font_size is not None:
            if font_size >= 18 and before_first_heading:
                return 0
            if font_size >= 14:
                return 1
            if font_size >= 12:
                return 2
        return None

    @staticmethod
    def _outline_level(paragraph: Paragraph) -> int | None:
        properties = paragraph._p.pPr
        if properties is None or properties.outlineLvl is None:
            return None
        return int(properties.outlineLvl.val)

    @classmethod
    def _looks_like_document_title(cls, paragraph: Paragraph) -> bool:
        text = paragraph.text.strip()
        size = cls._font_size(paragraph)
        return bool(text and len(text) <= 100 and size is not None and size >= 18)

    @staticmethod
    def _font_size(paragraph: Paragraph) -> float | None:
        direct_sizes = [
            run.font.size.pt
            for run in paragraph.runs
            if run.font.size is not None
        ]
        if direct_sizes:
            return max(direct_sizes)
        if paragraph.style.font.size is not None:
            return paragraph.style.font.size.pt
        return None

    @staticmethod
    def _is_bold(paragraph: Paragraph) -> bool:
        runs = [run for run in paragraph.runs if run.text.strip()]
        return bool(runs and all(run.bold or run.style.font.bold for run in runs))

    @staticmethod
    def _append_content(
        current_section: Section | None,
        preamble: list[str],
        text: str,
    ) -> None:
        target = current_section.paragraphs if current_section else preamble
        target.append(text)

    @staticmethod
    def _table_to_markdown(table: Table) -> str:
        rows: list[list[str]] = []
        for row in table.rows:
            cells: list[str] = []
            seen_cells: set[int] = set()
            for cell in row.cells:
                identity = id(cell._tc)
                if identity in seen_cells:
                    continue
                seen_cells.add(identity)
                cells.append(" ".join(cell.text.split()).replace("|", "\\|"))
            if any(cells):
                rows.append(cells)

        if not rows:
            return ""

        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = [f"| {' | '.join(rows[0])} |"]
        lines.append(f"| {' | '.join(['---'] * width)} |")
        lines.extend(f"| {' | '.join(row)} |" for row in rows[1:])
        return "\n".join(lines)
