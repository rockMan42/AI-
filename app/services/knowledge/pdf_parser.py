import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from statistics import median

import fitz

from app.services.knowledge.base_parser import DocumentParser
from app.services.knowledge.document_types import Section


log = logging.getLogger(__name__)


@dataclass(slots=True)
class _PageItem:
    text: str
    page_number: int
    y0: float
    y1: float
    font_size: float
    bold: bool
    is_table: bool = False


class PDFParser(DocumentParser):
    """结合版面、字体、编号和重复位置信息解析 PDF。"""

    _CHAPTER_PATTERN = re.compile(
        r"^第[一二三四五六七八九十百零〇0-9]+章(?:\s|$)"
    )
    _SECTION_PATTERN = re.compile(
        r"^第[一二三四五六七八九十百零〇0-9]+节(?:\s|$)"
    )
    _DECIMAL_PATTERN = re.compile(r"^(\d+(?:\.\d+){1,2})[\s、.]")

    def __init__(
        self,
        *,
        token_encoding: str = "cl100k_base",
        ocr_enabled: bool = True,
        ocr_language: str = "chi_sim+eng",
        ocr_dpi: int = 200,
        ocr_min_chars: int = 20,
    ) -> None:
        super().__init__(token_encoding=token_encoding)
        self.ocr_enabled = ocr_enabled
        self.ocr_language = ocr_language
        self.ocr_dpi = ocr_dpi
        self.ocr_min_chars = ocr_min_chars

    def parse(self, file_path: str) -> list[Section]:
        with fitz.open(file_path) as document:
            pages = [
                self._extract_page(page, index + 1)
                for index, page in enumerate(document)
            ]

        items = [item for page_items in pages for item in page_items]
        if not items:
            return []

        repeated_margins = self._repeated_margin_texts(pages)
        items = [
            item for item in items
            if item.is_table or (
                self._normalize(item.text) not in repeated_margins
                and not self._is_page_number(item)
            )
        ]
        text_items = [item for item in items if not item.is_table]
        body_size = median(item.font_size for item in text_items) if text_items else 11.0

        sections: list[Section] = []
        stack: list[Section] = []
        current_section: Section | None = None
        preamble: list[str] = []

        for item in items:
            level = None if item.is_table else self._heading_level(
                item,
                body_size=body_size,
                before_first_heading=not stack,
            )
            if level is None:
                target = current_section.paragraphs if current_section else preamble
                target.append(item.text)
                continue

            section = Section(level=level, title=item.text)
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

    def _extract_page(self, page: fitz.Page, page_number: int) -> list[_PageItem]:
        text_page = None
        plain_text = page.get_text("text").strip()
        if self.ocr_enabled and len(plain_text) < self.ocr_min_chars:
            try:
                text_page = page.get_textpage_ocr(
                    language=self.ocr_language,
                    dpi=self.ocr_dpi,
                    full=True,
                )
            except Exception as exc:
                log.warning("PDF OCR 不可用 page=%s error=%s", page_number, exc)

        table_items, table_boxes = self._extract_tables(page, page_number)
        page_dict = page.get_text("dict", textpage=text_page)
        line_items: list[_PageItem] = []

        for block in page_dict.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(span.get("text", "") for span in spans).strip()
                if not text:
                    continue
                bbox = line.get("bbox", (0, 0, 0, 0))
                if any(self._inside_table(bbox, box) for box in table_boxes):
                    continue
                line_items.append(_PageItem(
                    text=text,
                    page_number=page_number,
                    y0=float(bbox[1]) / max(page.rect.height, 1),
                    y1=float(bbox[3]) / max(page.rect.height, 1),
                    font_size=max(float(span.get("size", 0)) for span in spans),
                    bold=any(
                        "bold" in str(span.get("font", "")).casefold()
                        or bool(int(span.get("flags", 0)) & 16)
                        for span in spans
                    ),
                ))

        return sorted(
            line_items + table_items,
            key=lambda item: (item.y0, item.is_table),
        )

    def _extract_tables(
        self,
        page: fitz.Page,
        page_number: int,
    ) -> tuple[list[_PageItem], list[tuple[float, float, float, float]]]:
        try:
            tables = page.find_tables().tables
        except Exception as exc:
            log.debug("PDF 表格识别跳过 page=%s error=%s", page_number, exc)
            return [], []

        items: list[_PageItem] = []
        boxes: list[tuple[float, float, float, float]] = []
        for table in tables:
            markdown = self._table_to_markdown(table.extract())
            if not markdown:
                continue
            bbox = tuple(float(value) for value in table.bbox)
            boxes.append(bbox)
            items.append(_PageItem(
                text=markdown,
                page_number=page_number,
                y0=bbox[1] / max(page.rect.height, 1),
                y1=bbox[3] / max(page.rect.height, 1),
                font_size=0,
                bold=False,
                is_table=True,
            ))
        return items, boxes

    @staticmethod
    def _inside_table(
        line_box: tuple[float, float, float, float],
        table_box: tuple[float, float, float, float],
    ) -> bool:
        center_x = (line_box[0] + line_box[2]) / 2
        center_y = (line_box[1] + line_box[3]) / 2
        return (
            table_box[0] <= center_x <= table_box[2]
            and table_box[1] <= center_y <= table_box[3]
        )

    def _repeated_margin_texts(self, pages: list[list[_PageItem]]) -> set[str]:
        counts: Counter[str] = Counter()
        for items in pages:
            values = {
                self._normalize(item.text)
                for item in items
                if not item.is_table and (item.y0 <= 0.1 or item.y1 >= 0.9)
            }
            counts.update(value for value in values if value)

        threshold = max(2, math.ceil(len(pages) * 0.5))
        return {value for value, count in counts.items() if count >= threshold}

    def _heading_level(
        self,
        item: _PageItem,
        *,
        body_size: float,
        before_first_heading: bool,
    ) -> int | None:
        text = item.text.strip()
        if len(text) > 100:
            return None
        if before_first_heading and item.page_number == 1 and (
            item.font_size >= max(16, body_size * 1.45)
        ):
            return 0
        if self._CHAPTER_PATTERN.match(text):
            return 1
        if self._SECTION_PATTERN.match(text):
            return 2

        decimal_match = self._DECIMAL_PATTERN.match(text)
        if decimal_match:
            return min(decimal_match.group(1).count(".") + 1, 3)
        if item.font_size >= body_size * 1.45:
            return 1
        if item.bold and item.font_size >= body_size * 1.15:
            return 2
        return None

    @staticmethod
    def _table_to_markdown(rows: list[list[str | None]]) -> str:
        cleaned = [
            [" ".join((cell or "").split()).replace("|", "\\|") for cell in row]
            for row in rows
            if any(cell for cell in row)
        ]
        if not cleaned:
            return ""
        width = max(len(row) for row in cleaned)
        cleaned = [row + [""] * (width - len(row)) for row in cleaned]
        lines = [f"| {' | '.join(cleaned[0])} |"]
        lines.append(f"| {' | '.join(['---'] * width)} |")
        lines.extend(f"| {' | '.join(row)} |" for row in cleaned[1:])
        return "\n".join(lines)

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text).casefold()

    @staticmethod
    def _is_page_number(item: _PageItem) -> bool:
        if not (item.y0 <= 0.1 or item.y1 >= 0.9):
            return False
        return bool(re.fullmatch(r"(?:第\s*)?\d+\s*(?:页)?", item.text.strip()))
