from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(StrEnum):
    """
    定义三种角色类型
    """
    EMPLOYEE = "Employee"
    MANAGER = "Manager"
    HR_ADMIN = "HRAdmin"


class Principal(BaseModel):
    """
    保存当前操作者得身份，权限等信息
    """
    model_config = ConfigDict(frozen=True)

    user_id: int
    open_id: str
    role: Role
    department_id: int
    managed_user_ids: tuple[int, ...] = ()
    source: Literal["auto", "manual"] = "auto"
    version: int


class RuleInput(BaseModel):
    """
    校验角色映射规则的输入，限制部门规则只能配置为 HR
    """
    model_config = ConfigDict(extra="forbid")

    kind: Literal["user", "department"]
    subject: str = Field(min_length=1, max_length=64)
    role: Role
    enabled: bool = True

    @model_validator(mode="after")
    def validate_department_role(self):
        if self.kind == "department" and self.role != Role.HR_ADMIN:
            raise ValueError("部门规则仅用于指定 HR 部门")
        return self


class RefreshInput(BaseModel):
    """
    接收刷新令牌，并校验长度
    """
    refresh_token: str = Field(min_length=40, max_length=200)


class AccessDenied(PermissionError):
    """
    表示没有操作权限
    """
    pass


class PermissionUnavailable(RuntimeError):
    """
    表示权限服务不可用
    """
    pass