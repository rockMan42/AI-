import json
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client as redis_module
from app.models.requisition import CategoryFieldRule


CACHE_PREFIX = "dep:cache:category_rules"
CACHE_TTL_SECONDS = 24 * 60 * 60

FIELD_TYPES = {
    "quantity": "integer",
    "expected_return_date": "date",
}


class CategoryRuleError(ValueError):
    pass


class CategoryRuleService:
    async def list_categories(
        self,
        db: AsyncSession,
    ) -> list[str]:
        client = redis_module.redis_client
        cache_key = f"{CACHE_PREFIX}:__list__"

        if client is not None:
            cached = await client.get(cache_key)
            if cached:
                return json.loads(cached)

        rows = await db.scalars(
            select(CategoryFieldRule.category)
            .distinct()
            .order_by(CategoryFieldRule.category)
        )
        categories = list(rows.all())

        if client is not None and categories:
            await client.setex(
                cache_key,
                CACHE_TTL_SECONDS,
                json.dumps(categories, ensure_ascii=False),
            )

        return categories

    async def get_fields(
        self,
        db: AsyncSession,
        category: str,
    ) -> list[dict]:
        category = str(category or "").strip()
        if not category:
            raise CategoryRuleError("物资品类不能为空")

        client = redis_module.redis_client
        cache_key = f"{CACHE_PREFIX}:{category}"

        if client is not None:
            cached = await client.get(cache_key)
            if cached:
                return json.loads(cached)

        scalar_result = await db.scalars(
            select(CategoryFieldRule)
            .where(CategoryFieldRule.category == category)
            .order_by(
                CategoryFieldRule.sort_order,
                CategoryFieldRule.id,
            )
        )
        rules = list(scalar_result.all())

        if not rules:
            raise CategoryRuleError("暂不支持该物资品类")

        fields = [
            {
                "name": rule.field_name,
                "field_name": rule.field_name,
                "type": FIELD_TYPES.get(rule.field_name, "string"),
                "required": bool(rule.required),
                "display_name": rule.display_name,
                "description": rule.display_name,
                "default": self._parse_default(rule),
                "default_value": rule.default_value,
                "validation_rule": rule.validation_rule,
                "follow_up_prompt": f"请补充{rule.display_name}。",
                "sort_order": rule.sort_order,
            }
            for rule in rules
        ]

        if client is not None:
            await client.setex(
                cache_key,
                CACHE_TTL_SECONDS,
                json.dumps(fields, ensure_ascii=False),
            )

        return fields

    async def replace_category_rules(
        self,
        db: AsyncSession,
        category: str,
        fields: list[dict],
    ) -> None:
        category = str(category or "").strip()
        if not category:
            raise CategoryRuleError("物资品类不能为空")
        if not fields:
            raise CategoryRuleError("至少保留一个字段规则")

        names = [
            str(field.get("field_name") or "").strip()
            for field in fields
        ]
        if any(not name for name in names):
            raise CategoryRuleError("字段名不能为空")
        if len(names) != len(set(names)):
            raise CategoryRuleError("同一品类的字段名不能重复")

        allowed_fields = {
            "item_name",
            "specification",
            "quantity",
            "reason",
            "purpose",
            "expected_return_date",
        }
        if any(name not in allowed_fields for name in names):
            raise CategoryRuleError("包含不支持的字段")

        old_rows = await db.scalars(
            select(CategoryFieldRule)
            .where(CategoryFieldRule.category == category)
            .with_for_update()
        )
        for row in old_rows.all():
            await db.delete(row)

        for index, item in enumerate(fields, start=1):
            display_name = str(
                item.get("display_name") or ""
            ).strip()
            if not display_name:
                raise CategoryRuleError("展示名称不能为空")

            db.add(
                CategoryFieldRule(
                    category=category,
                    field_name=names[index - 1],
                    display_name=display_name,
                    required=bool(item.get("required", False)),
                    default_value=self._dump_default(
                        item.get("default_value"),
                    ),
                    validation_rule=(
                        str(item["validation_rule"]).strip()
                        if item.get("validation_rule")
                        else None
                    ),
                    sort_order=int(
                        item.get("sort_order", index * 10)
                    ),
                )
            )

        await db.commit()
        await self.invalidate(category)

    async def invalidate(
        self,
        category: str | None = None,
    ) -> None:
        client = redis_module.redis_client
        if client is None:
            return

        keys = [f"{CACHE_PREFIX}:__list__"]
        if category:
            keys.append(f"{CACHE_PREFIX}:{category}")

        await client.delete(*keys)

    def validate(
        self,
        rules: list[dict],
        values: dict,
    ) -> None:
        for rule in rules:
            name = rule["name"]
            value = values.get(name)

            if rule["required"] and value in (None, ""):
                raise CategoryRuleError(
                    f"{rule['display_name']}不能为空"
                )

            if value in (None, ""):
                continue

            expression = rule.get("validation_rule") or ""

            if expression == "non_empty" and not str(value).strip():
                raise CategoryRuleError(
                    f"{rule['display_name']}不能为空"
                )

            if expression == "date":
                try:
                    date.fromisoformat(str(value))
                except ValueError as exc:
                    raise CategoryRuleError(
                        f"{rule['display_name']}必须为 YYYY-MM-DD"
                    ) from exc

            if expression.startswith("integer:"):
                parts = expression.split(":")
                if len(parts) != 3:
                    raise CategoryRuleError("数量校验规则配置错误")

                _, minimum, maximum = parts
                try:
                    number = int(value)
                except (TypeError, ValueError) as exc:
                    raise CategoryRuleError(
                        f"{rule['display_name']}必须为整数"
                    ) from exc

                if not int(minimum) <= number <= int(maximum):
                    raise CategoryRuleError(
                        f"{rule['display_name']}必须在 "
                        f"{minimum}-{maximum} 之间"
                    )

    @staticmethod
    def _parse_default(rule: CategoryFieldRule) -> Any:
        value = rule.default_value
        if value in (None, ""):
            return None
        if rule.field_name == "quantity":
            return int(value)
        return value

    @staticmethod
    def _dump_default(value: Any) -> str | None:
        return None if value is None else str(value)