import json
import time

from app.core.redis_client import set_cache, get_cache, delete_cache
from app.services.conversation_engine.slot_manage import SessionSlots, SESSION_TTL


class SessionStore:
    """基于 Redis 的统一会话持久化入口。"""

    KEY_PREFIX = "dep:sess:"

    def _key(self, session_id: str) -> str:
        return f"{self.KEY_PREFIX}{session_id}"

    async def save(self, session: SessionSlots) -> None:
        session.last_active_at = time.time()
        await set_cache(
            self._key(session.session_id),
            json.dumps(session.to_dict(), ensure_ascii=False),
            expire=SESSION_TTL,
        )

    async def load(self, session_id: str) -> SessionSlots | None:
        cache = await get_cache(self._key(session_id))
        if not cache:
            return None

        return SessionSlots.from_dict(json.loads(cache))

    async def suspend(self, session_id: str) -> None:
        session = await self.load(session_id)
        if session is None:
            return

        session.status = "suspended"
        await self.save(session)

    async def clear(self, session_id: str) -> None:
        await delete_cache(self._key(session_id))