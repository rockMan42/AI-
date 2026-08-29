from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department
from app.services.feishu import get_feishu_department_detail


async def get_or_create_department(
    feishu_department_id: str,
    db: AsyncSession,
) -> Department:
    # 根据飞书部门 ID 查询
    result = await db.execute(
        Select(Department).where(
            Department.feishu_department_id
            == feishu_department_id
        )
    )
    department = result.scalar_one_or_none()

    if department:
        return department

    # 本地不存在，调用飞书接口获取详情
    detail = await get_feishu_department_detail(
        feishu_department_id
    )

    parent_feishu_id = detail.get("parent_department_id")

    # 根部门没有本地父部门
    parent_id = None
    if parent_feishu_id not in (
        None,
        "",
        "0",
        feishu_department_id,
    ):
        parent = await get_or_create_department(
            str(parent_feishu_id),
            db,
        )
        parent_id = parent.department_id

    department = Department(
        feishu_department_id=feishu_department_id,
        name=detail["name"],
        parent_id=parent_id,
        manager_user_id=None,
        sort_order=0,
    )

    db.add(department)
    await db.flush()  # 获得本地自增 department_id

    return department