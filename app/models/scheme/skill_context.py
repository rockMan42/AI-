from dataclasses import dataclass

@dataclass
class SkillContext:
    user_id: str
    open_id: str
    role: str
    department_id: str
    session_id: str
    message_id:str

