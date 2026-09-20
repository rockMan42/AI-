
from app.models.knowledge_document import KnowledgeDocument
from app.models.requisition import (
    CategoryFieldRule,
    Requisition,
    RequisitionApproval,
)
from app.models.expense_flow import (
    BusinessTrip,
    ExpenseRule,
    ExpensePolicy,
    ExpenseInvoiceClaim,
    ExpenseRuleAudit,
    FinanceExpenseMock,
)
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.user import User
from app.models.department import Department
from app.models.knowledge_search_log import KnowledgeSearchLog
from app.models.holiday_notice import HolidayNotice
from app.models.notice_receipt import NoticeReceipt
from app.models.leave_request import LeaveRequest
from app.models.leave_balance import LeaveBalance
from app.models.leave_workflow import (
    ApprovalRecord,
    LeaveDraft,
    LeaveNotification,
    WorkCalendarDay,
)
from app.models.lead import Lead, LeadFollowUp
from app.models.expense import Expense, ExpenseItem, Invoice
from app.models.expense_approval import (
    ExpenseApprovalLog,
    ExpenseNotification,
)

"""
统一导入入口：其他代码可以写 from app.models import HolidayNotice。
让 SQLAlchemy 发现模型：生成建表 SQL、处理外键关系或进行迁移比较时，能够找到这些表的定义。
"""

__all__ = [
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeSearchLog",
    "Department",
    "User",
    "CategoryFieldRule",
    "Requisition",
    "RequisitionApproval",
]
