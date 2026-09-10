import hashlib
import tempfile
import unicodedata
import zipfile
from pathlib import Path

import fitz
from fastapi import UploadFile
from pydantic.dataclasses import dataclass

from app.config.settings import Settings

ALLOWED_CONTENT_TYPES = {
    ".pdf": {
        "application/pdf",
        "application/octet-stream",
    },
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
    },
}

DOCX_REQUIRED_ENTRIES = {
    "[Content_Types].xml",
    "word/document.xml",
}

COPY_CHUNK_SIZE = 1024 * 1024


class InvalidDocumentError(ValueError):
    pass

@dataclass(slots=True)
class StageUpload:
    path :Path
    file_name: str
    content_type: str
    file_size: int
    suffix: str
    sha256: str





def sanitize_filename(filename: str | None) -> str:
    if not filename:
        raise InvalidDocumentError("文件名不能为空")

    normalized = unicodedata.normalize("NFC", filename)

    if (
        not normalized
        or len(normalized) > 255
        or "/" in normalized
        or "\\" in normalized
        or any(ord(char) < 32 for char in normalized)
    ):
        raise InvalidDocumentError("文件名不合法")

    return normalized

async def stage_validate_upload(upload: UploadFile,
                                settings: Settings
                                ) -> StageUpload:

    filename = sanitize_filename(upload.filename)
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED_CONTENT_TYPES:
        raise InvalidDocumentError("只支持pdf和docx格式")

    content_type = (
            upload.content_type or "application/octet-stream"
    ).split(";", maxsplit=1)[0].strip().lower()

    if content_type not in ALLOWED_CONTENT_TYPES[suffix]:
        raise InvalidDocumentError("文件 MIME 类型与扩展名不匹配")

    temp_dir = settings.knowledge_temp_dir

    if temp_dir:
        Path(temp_dir).mkdir(mode=0o700,parents=True, exist_ok=True)

    temp_file = tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=suffix,
        prefix="kb-upload-",
        dir=temp_dir,
        delete=False,
    )

    temp_path = Path(temp_file.name)
    digest = hashlib.sha256()
    size = 0

    try:
        with temp_file:
            while data:= await upload.read(COPY_CHUNK_SIZE):
                size += len(data)
                if size > settings.knowledge_max_upload_bytes:
                    raise InvalidDocumentError("文件上传大小超出限制")

                digest.update(data)
                temp_file.write(data)

        if size == 0:
            raise InvalidDocumentError("上传的文件不能为空")

        if suffix == ".pdf":
            _validate_pdf(temp_path, settings)
        else:
            _validate_docx(temp_path,settings)
    except Exception as e:
        temp_path.unlink(missing_ok=True)
        raise e

    return StageUpload(
        path=temp_path,
        file_name=filename,
        content_type=content_type,
        file_size=size,
        suffix=suffix,
        sha256=digest.hexdigest()
    )


def _validate_pdf(path: Path, settings: Settings | None = None) -> None:
    with path.open("rb") as stream:
        header = stream.read(1024)

    if not header.lstrip().startswith(b"%PDF-"):
        raise InvalidDocumentError("文件不是有效的 PDF")

    try:
        with fitz.open(path) as document:
            if document.needs_pass:
                raise InvalidDocumentError("不支持加密 PDF")
            if document.page_count == 0:
                raise InvalidDocumentError("PDF 不包含有效页面")
            if settings and document.page_count > settings.knowledge_max_pdf_pages:
                raise InvalidDocumentError("PDF 页数超过限制")
    except InvalidDocumentError:
        raise
    except Exception as exc:
        raise InvalidDocumentError("文件不是有效的 PDF") from exc

def _validate_docx(path: Path, settings: Settings) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > settings.knowledge_max_docx_entries:
                raise InvalidDocumentError("DOCX 文件条目数量超过限制")
            names = {entry.filename for entry in entries}

            if not DOCX_REQUIRED_ENTRIES.issubset(names):
                raise InvalidDocumentError("文件内容不是有效 DOCX")

            total_compressed = 0
            total_uncompressed = 0

            for entry in entries:
                if entry.flag_bits & 0x1:
                    raise InvalidDocumentError("不支持加密 DOCX")

                total_compressed += max(entry.compress_size, 1)
                total_uncompressed += entry.file_size

            if (
                total_uncompressed
                > settings.knowledge_max_docx_uncompressed_bytes
            ):
                raise InvalidDocumentError("DOCX 解压后体积超过限制")

            ratio = total_uncompressed / max(total_compressed, 1)
            if ratio > settings.knowledge_max_docx_compression_ratio:
                raise InvalidDocumentError("DOCX 压缩比异常")

            if archive.testzip() is not None:
                raise InvalidDocumentError("DOCX 压缩包已损坏")
    except zipfile.BadZipFile as exc:
        raise InvalidDocumentError("文件内容不是有效 DOCX") from exc
