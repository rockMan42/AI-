from app.skill_executor.attendance import AttendanceSKillExecutor
from app.skill_executor.base import BaseSkillExecutor
from app.skill_executor.leave import LeaveSkillExecutor
from app.skill_executor.policy import PolicySkillExecutor
from app.skill_executor.performance import PerformanceSkillExecutor
from app.skill_executor.requisition import RequisitionSkillExecutor
from app.skill_executor.lead import LeadSkillExecutor
from app.skill_executor.expense import ExpenseSkillExecutor


class ExecutorRegistry:

    def __init__(self):
        self._executors = {
            "attendance_query": AttendanceSKillExecutor(),
            "leave_apply": LeaveSkillExecutor(),
            "policy_query": PolicySkillExecutor(),
            "performance_query": PerformanceSkillExecutor(),
            "requisition_apply": RequisitionSkillExecutor(),
            "lead_query": LeadSkillExecutor(),
            "expense_reimburse": ExpenseSkillExecutor(),
        }

    def get_executor(self,executor_name: str) -> BaseSkillExecutor | None:
        return self._executors.get(executor_name)

