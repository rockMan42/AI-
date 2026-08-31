import yaml
import logging
from pathlib import Path
from dataclasses import dataclass, field

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

@dataclass
class SlotDefinition:
    """槽位定义"""
    name: str
    type: str
    required: bool
    description: str
    enum: list[str] = field(default_factory=list)


@dataclass
class SkillSchema:
    """Skill定义"""
    name: str
    description: str
    triggers: list[str]
    priority: int
    slots: list[SlotDefinition]


# 注册skill
class SkillRegistry:
    "Skill注册类"
    def __init__(self):
        self._skills: dict[str,SkillSchema] = {}

    def load_from_directory(self,skills_dir: str):
        skill_path = Path(skills_dir)
        for yaml_file in skill_path.glob("*.yaml"):
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                slots = [SlotDefinition(**slot) for slot in data.get("slots", [])]

                skill = SkillSchema(
                    name=data["name"],
                    description=data["description"],
                    triggers=data.get("triggers", []),
                    priority=data.get("priority",0),
                    slots=slots
                )
                self._skills[skill.name] = skill
                log.info(f"Skill注册成功:{skill.name}")


    def get_skill(self,intent_code: str) -> SkillSchema | None:
        """根据意图码获取对应的skill"""
        return self._skills.get(intent_code)

    def get_all_skills(self) -> dict[str,SkillSchema]:
        """"获取所有skill"""
        return self._skills.copy()

# 创建Skill注册实例
register = SkillRegistry()

def get_skill_register() -> SkillRegistry:
    return register