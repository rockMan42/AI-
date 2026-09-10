import asyncio
import logging
import struct
from pathlib import Path

from app.config.settings import Settings


log = logging.getLogger(__name__)
STREAM_CHUNK_SIZE = 64 * 1024


class VirusFoundError(Exception):
    pass


class VirusScannerUnavailableError(Exception):
    pass


async def scan_file(path: Path, settings: Settings) -> None:
    try:
        async with asyncio.timeout(settings.clamav_timeout_seconds):
            reader, writer = await asyncio.open_connection(
                settings.clamav_host,
                settings.clamav_port,
            )
            try:
                writer.write(b"zINSTREAM\0")

                with path.open("rb") as stream:
                    while block := stream.read(STREAM_CHUNK_SIZE):
                        writer.write(struct.pack("!I", len(block)))
                        writer.write(block)
                        await writer.drain()

                writer.write(struct.pack("!I", 0))
                await writer.drain()

                response = (
                    await reader.readuntil(b"\0")
                ).rstrip(b"\0").decode("utf-8", errors="replace")
            finally:
                writer.close()
                await writer.wait_closed()
    except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
        if settings.virus_scan_required:
            raise VirusScannerUnavailableError(
                "病毒扫描服务不可用"
            ) from exc

        log.warning("病毒扫描服务不可用，按配置跳过扫描")
        return

    if response.endswith(" OK"):
        return

    if response.endswith(" FOUND"):
        raise VirusFoundError("检测到恶意文件")

    if settings.virus_scan_required:
        raise VirusScannerUnavailableError(
            "病毒扫描服务返回异常"
        )

    log.warning("病毒扫描返回未知结果: %s", response)