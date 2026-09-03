from enum import Enum


class IntentState(str,Enum):
    INTENT_PENDING = "pending"
    INTENT_CLASSIFYING = "class"
    INTENT_MATCHED = "matched"
    INTENT_AMBIGUOUS = "ambiguous"
    INTENT_UNKNOWN = "unknown"
    SKILL_EXECUTING = "executing"

