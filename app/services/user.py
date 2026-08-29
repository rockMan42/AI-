
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.core.database import get_session
from app.models.user import User
from app.services.department import get_or_create_department
from app.services.feishu import get_feishu_user_detail


# 获取或者创建用户
async def get_or_create_user(feishu_open_id: str,db: AsyncSession= Depends(get_session)):
    # 查询用户是否存在
    result = await db.execute(Select(User).where(User.feishu_open_id == feishu_open_id))
    user = result.scalar_one_or_none()
    if user:
        return user

    try:
        # 调用飞书API查询用户详情
        user_detail = await get_feishu_user_detail(feishu_open_id)
        feishu_department_id = str(user_detail["department_ids"][0])

        # 获取或者创建部门
        department = await get_or_create_department(feishu_department_id, db)

        # 创建用户
        user = User(
            feishu_open_id=feishu_open_id,
            name=user_detail["name"],
            # 使用本地自增主键
            department_id=department.department_id,
            role="employee"
        )

        db.add(user)
        await db.commit()
        await db.refresh(user)

    except Exception as e:
        print(f"Error creating user or department: {e}")
        await db.rollback()
        raise e
    # 返回用户
    return user


