from dataclasses import dataclass, field


@dataclass
class SkillResult:
    """技能执行结果"""
    success: bool
    message: str
    data: dict = field(default_factory=dict)