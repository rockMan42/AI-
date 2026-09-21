from sqlalchemy import select

from app.core.database import create_session
from app.models.permission import OrganizationState, PermissionEvent


CONTACT_EVENTS = {
    "contact.user.created_v3",
    "contact.user.updated_v3",
    "contact.user.deleted_v3",
    "contact.department.created_v3",
    "contact.department.updated_v3",
    "contact.department.deleted_v3",
}


async def invalidate_organization(event_id: str):
    async with create_session() as db, db.begin():
        state = await db.scalar(
            select(OrganizationState)
            .where(OrganizationState.id == 1)
            .with_for_update()
        )

        if await db.get(PermissionEvent, event_id):
            return

        db.add(PermissionEvent(id=event_id))
        state.version += 1
        state.dirty = True