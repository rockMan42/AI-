from sqlalchemy.ext.asyncio import AsyncSession

from app.services.requisition.category_rule_service import (
    CategoryRuleService,
)


async def load_category_template(
    db: AsyncSession,
    category: str,
) -> list[dict]:
    return await CategoryRuleService().get_fields(db, category)