import logging
import re
from copy import deepcopy
from datetime import date, datetime
from typing import Any
from app.services.conversation_engine.register_skill import SkillSchema
from app.services.conversation_engine.session_store import SessionStore
from app.services.conversation_engine.slot_manage import MAX_TURNS, SessionSlots, SlotState
from app.core.database import create_session
from app.services.conversation_engine.category_template import (
    load_category_template,
)

logger = logging.getLogger(__name__)
POSITIVE_NUMBER_SLOTS = {"amount", "duration", "quantity"}


class SlotCollector:
    """槽位收集器：合并、校验、补默认值并决定是否继续追问。"""

    def __init__(self, store: SessionStore):
        self.store = store

    def parse_pending_slot_reply(
        self,
        session: SessionSlots,
        user_reply: str,
    ) -> dict | None:
        """确定性解析当前待补槽位，避免短回答再次进入全局意图识别。"""
        if not session.pending_slot:
            return None

        slot = session.slots.get(session.pending_slot)
        text = str(user_reply or "").strip()
        if slot is None or slot.filled or not text:
            return None

        if session.intent_code == "lead_follow_up":
            if session.pending_slot == "follow_up_type":
                methods = {
                    "电话": "phone", "邮件": "email", "拜访": "visit",
                    "微信": "wechat", "演示": "demo",
                }
                method = re.fullmatch(
                    r"(?:通过|使用|用)?(电话|邮件|拜访|微信|演示)(?:跟进)?[。！!]?",
                    text,
                )
                if method:
                    return {"follow_up_type": methods[method.group(1)]}
                # 用户也可以在回答方式时，一次提供完整的跟进信息。
                method = re.match(
                    r"^(?:这次|本次)?(?:通过|使用|用)"
                    r"(电话|邮件|拜访|微信|演示)(?:跟进|联系|沟通)",
                    text,
                )
                if method:
                    values = self._extract_lead_follow_up_reply(text)
                    values["follow_up_type"] = methods[method.group(1)]
                    return values
            if session.pending_slot == "content":
                return self._extract_lead_follow_up_reply(text)

        if session.intent_code == "requisition_apply":
            requisition_slots = self._extract_requisition_reply(
                session,
                text,
            )
            if requisition_slots:
                return requisition_slots

        raw_value = self._extract_direct_value(slot, text)
        if raw_value is None:
            return None

        try:
            self._coerce_value(slot, raw_value)
        except (TypeError, ValueError):
            return None

        return {slot.name: raw_value}

    @staticmethod
    def _extract_lead_follow_up_reply(text: str) -> dict:
        fields = {
            "下一步行动": "next_action",
            "下一步": "next_action",
            "下次跟进时间": "next_follow_up",
            "跟进结果": "outcome",
        }
        parts = re.split(
            r"(?:[，,。；;\n]\s*|^)(下一步行动|下一步|下次跟进时间|跟进结果)"
            r"\s*(?:是|为|[:：])?\s*",
            text,
        )
        result = {}
        content = parts[0].strip(" ，,。；;\n")
        if content:
            result["content"] = content
        for index in range(1, len(parts), 2):
            field = fields[parts[index]]
            value = parts[index + 1].strip(" ，,。；;\n")
            if not value:
                raise ValueError(f"请补充{parts[index]}。")
            if field == "next_follow_up":
                value = value.replace("年", "-").replace("月", "-").replace("日", "")
                try:
                    value = datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
                except ValueError:
                    raise ValueError(
                        "下次跟进时间无法识别，请重新发送本次跟进内容，"
                        "并使用日期格式，例如：下次跟进时间是2026-11-30。"
                    ) from None
            if field == "outcome":
                value = {
                    "积极": "positive", "一般": "neutral",
                    "消极": "negative", "未接通": "no_answer",
                }.get(value, value)
                if value not in {"positive", "neutral", "negative", "no_answer"}:
                    raise ValueError("跟进结果请填写积极、一般、消极或未接通，并重新发送跟进内容。")
            result[field] = value
        return result

    def _extract_requisition_reply(
        self,
        session: SessionSlots,
        text: str,
    ) -> dict:
        """解析一次性补充的多个物资申领字段。"""
        extracted = {}
        clauses = [
            item.strip()
            for item in re.split(r"[，,。；;\n]+", text)
            if item.strip()
        ]

        patterns = {
            "specification": re.compile(
                r"^(?:规格(?:型号)?|型号)\s*(?:是|为|[:：])?\s*(.+)$"
            ),
            "reason": re.compile(
                r"^(?:申领|申请)?原因\s*(?:是|为|[:：])?\s*(.+)$"
            ),
            "purpose": re.compile(
                r"^(?:用途|使用目的)\s*(?:是|为|[:：])?\s*(.+)$"
            ),
        }

        for clause in clauses:
            date_match = re.search(
                r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
                clause,
            )
            if (
                date_match
                and any(word in clause for word in ("预计", "归还", "返还"))
                and self._slot_exists(session, "expected_return_date")
            ):
                extracted["expected_return_date"] = (
                    f"{int(date_match.group(1)):04d}-"
                    f"{int(date_match.group(2)):02d}-"
                    f"{int(date_match.group(3)):02d}"
                )
                continue

            matched = False
            for name, pattern in patterns.items():
                match = pattern.match(clause)
                if match and self._slot_exists(session, name):
                    extracted[name] = match.group(1).strip()
                    matched = True
                    break
            if matched:
                continue

            if (
                self._slot_needs_value(session, "purpose")
                and (
                    clause.startswith("用于")
                    or clause.endswith("使用")
                    or clause.endswith("用途")
                )
            ):
                extracted["purpose"] = clause

        return extracted

    @staticmethod
    def _slot_needs_value(
        session: SessionSlots,
        slot_name: str,
    ) -> bool:
        slot = session.slots.get(slot_name)
        return bool(slot is not None and not slot.filled)

    @staticmethod
    def _slot_exists(
        session: SessionSlots,
        slot_name: str,
    ) -> bool:
        return slot_name in session.slots

    async def collect(
        self,
        session: SessionSlots,
        skill: SkillSchema,
        user_reply_or_slots: str | dict | None,
        llm_extract_fn=None,
    ) -> dict:
        self._sync_static_slots(session, skill)

        extracted_slots = await self._extract_slots(
            session,
            user_reply_or_slots,
            llm_extract_fn,
        )

        invalid_slots = self._merge_extracted_slots(
            session,
            extracted_slots,
        )

        await self._sync_category_slots(session)
        self._apply_defaults(session)

        missing_slots = [
            slot
            for slot in session.slots.values()
            if slot.required and not slot.filled
        ]

        if not missing_slots:
            session.pending_slot = None
            session.status = "completed"
            session.turn_count = 0
            await self.store.save(session)
            return {
                "action": "execute",
                "message": "",
                "slots": session.filled_values(),
            }

        session.turn_count += 1
        max_turns = (
            3
            if session.intent_code == "requisition_apply"
            else MAX_TURNS
        )

        if session.turn_count > max_turns:
            session.pending_slot = None
            session.status = "expired"
            session.state = "failed"
            session.workflow_state = "failed"
            await self.store.save(session)
            return {
                "action": "fallback",
                "message": "信息收集已超过最大轮次，请重新完整描述需求。",
                "slots": session.filled_values(),
            }

        session.pending_slot = missing_slots[0].name
        session.status = "collecting"

        for slot in missing_slots:
            slot.asked = True
            slot.asked_count += 1

        await self.store.save(session)

        if session.intent_code == "requisition_apply":
            questions = [
                f"• {slot.description or slot.name}"
                for slot in missing_slots
            ]
            return {
                "action": "ask",
                "message": (
                        "还需要补充以下信息：\n"
                        + "\n".join(questions)
                        + "\n请一次性告诉我。"
                ),
                "slots": session.filled_values(),
            }

        next_slot = missing_slots[0]
        ask_message = self._build_ask_message(next_slot)

        if next_slot.error:
            ask_message = f"{next_slot.error}\n{ask_message}"

        return {
            "action": "ask",
            "message": ask_message,
            "slots": session.filled_values(),
        }

    async def _extract_slots(
        self,
        session: SessionSlots,
        user_reply_or_slots: str | dict | None,
        llm_extract_fn,
    ) -> dict:
        """
        兼容两种入口：
        1. 当前项目传入意图分类阶段已经提取的槽位字典；
        2. 附件方案传入用户原话和异步 LLM 提取函数。
        """
        if isinstance(user_reply_or_slots, dict):
            return user_reply_or_slots

        if llm_extract_fn is None:
            return {}

        extracted = await llm_extract_fn(
            user_message=str(user_reply_or_slots or ""),
            expected_slots=[
                {
                    "name": slot.name,
                    "type": slot.type,
                    "description": slot.description,
                    "enum": slot.enum,
                }
                for slot in session.slots.values()
            ],
        )
        if not isinstance(extracted, dict):
            return {}
        if isinstance(extracted.get("extracted_slots"), dict):
            return extracted["extracted_slots"]
        return extracted

    def _sync_static_slots(
        self,
        session: SessionSlots,
        skill: SkillSchema,
    ) -> None:
        """用最新 YAML 定义补全旧会话元数据，同时保留已收集值。"""
        for definition in skill.slots:
            slot = session.slots.get(definition.name)
            if slot is None:
                slot = SlotState(name=definition.name)
                session.slots[definition.name] = slot

            slot.type = definition.type
            slot.required = definition.required
            slot.description = definition.description
            slot.enum = list(definition.enum)
            slot.default = deepcopy(definition.default)
            slot.follow_up_prompt = definition.follow_up_prompt

    def _merge_extracted_slots(
        self,
        session: SessionSlots,
        extracted_slots: dict,
    ) -> list[str]:
        invalid_slots = []

        for slot_name, raw_value in extracted_slots.items():
            slot = session.slots.get(slot_name)
            if slot is None or raw_value is None:
                continue

            try:
                value = self._coerce_value(slot, raw_value)
            except (TypeError, ValueError) as exc:
                slot.value = None
                slot.filled = False
                slot.error = str(exc)
                invalid_slots.append(slot_name)
                continue

            slot.value = value
            slot.filled = True
            slot.error = None

        return invalid_slots

    async def _sync_category_slots(
            self,
            session: SessionSlots,
    ) -> None:
        if session.intent_code != "requisition_apply":
            return

        category_slot = session.slots.get("item_category")
        if category_slot is None or not category_slot.filled:
            return

        category = str(category_slot.value)

        try:
            async with create_session() as db:
                definitions = await load_category_template(
                    db,
                    category,
                )
        except ValueError as exc:
            category_slot.error = str(exc)
            category_slot.filled = False
            return

        # 每轮都重新应用规则。
        # _sync_static_slots 会恢复 YAML，所以不能因品类未变化而直接返回。
        dynamic_names = set()

        for definition in definitions:
            name = definition["name"]
            dynamic_names.add(name)

            slot = session.slots.get(name)
            if slot is None:
                slot = SlotState(name=name)
                session.slots[name] = slot

            slot.type = definition.get("type", "string")
            slot.required = bool(
                definition.get("required", False)
            )
            slot.description = definition.get(
                "description",
                name,
            )
            slot.default = definition.get("default")
            slot.follow_up_prompt = definition.get(
                "follow_up_prompt",
            )

        session.dynamic_category = category
        session.dynamic_slot_names = sorted(dynamic_names)

    def _apply_defaults(self, session: SessionSlots) -> None:
        for slot in session.slots.values():
            if slot.filled or slot.default is None or slot.error:
                continue

            if (
                session.intent_code == "attendance_query"
                and slot.name == "query_date"
                and self._is_filled(session, "query_month")
            ):
                continue

            default_value = self._resolve_default(slot.default)
            try:
                slot.value = self._coerce_value(slot, default_value)
            except (TypeError, ValueError) as exc:
                logger.error(
                    "槽位默认值无效 intent=%s slot=%s error=%s",
                    session.intent_code,
                    slot.name,
                    exc,
                )
                slot.error = f"槽位 {slot.name} 的默认值配置无效"
                continue

            slot.filled = True
            slot.error = None

    def _pick_invalid_slot(
        self,
        session: SessionSlots,
        invalid_slots: list[str],
    ) -> SlotState | None:
        if (
            session.pending_slot
            and session.pending_slot in invalid_slots
        ):
            return session.slots[session.pending_slot]

        if invalid_slots:
            return session.slots[invalid_slots[0]]
        return None

    def _coerce_value(
        self,
        slot: SlotState,
        raw_value: Any,
    ) -> Any:
        slot_type = slot.type.lower()

        if slot_type in {"string", "str"}:
            value = str(raw_value).strip()
            if not value:
                raise ValueError("输入内容不能为空")
        elif slot_type in {"integer", "int", "bigint"}:
            if isinstance(raw_value, bool):
                raise ValueError("请输入正确的整数")
            value = int(raw_value)
        elif slot_type in {"float", "number"}:
            if isinstance(raw_value, bool):
                raise ValueError("请输入正确的数字")
            value = float(raw_value)
        elif slot_type == "date":
            value = date.fromisoformat(str(raw_value)).isoformat()
        elif slot_type == "datetime":
            normalized = str(raw_value).replace("Z", "+00:00")
            value = datetime.fromisoformat(normalized).isoformat()
        elif slot_type == "list":
            if not isinstance(raw_value, list):
                raise ValueError("请提供列表格式的数据")
            value = raw_value
        else:
            value = raw_value

        if slot.enum and value not in slot.enum:
            options = " / ".join(slot.enum)
            raise ValueError(f"请输入有效选项：{options}")

        if (
            slot.name in POSITIVE_NUMBER_SLOTS
            and isinstance(value, (int, float))
            and value <= 0
        ):
            raise ValueError("请输入大于 0 的数字")

        return value

    def _extract_direct_value(
        self,
        slot: SlotState,
        text: str,
    ) -> Any:
        slot_type = slot.type.lower()

        if slot.enum:
            normalized = re.sub(r"[\s，。！？、,.!?]", "", text)
            for prefix in (
                "请假类型是",
                "类型是",
                "我要请",
                "我想请",
                "我要休",
                "选择",
                "选",
                "是",
                "请",
            ):
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix):]
                    break
            normalized = normalized.removesuffix("吧").removesuffix("谢谢")

            for option in slot.enum:
                if normalized == re.sub(r"\s", "", str(option)):
                    return option
            return None

        if slot_type in {"integer", "int", "bigint", "float", "number"}:
            matched = re.fullmatch(
                r"([+-]?\d+(?:\.\d+)?)\s*(?:个|件|台|天|元|次)?",
                text,
            )
            if matched:
                return matched.group(1)
            return None

        if slot_type in {"date", "datetime"}:
            try:
                self._coerce_value(slot, text)
            except (TypeError, ValueError):
                return None
            return text

        if slot_type in {"string", "str"}:
            return text

        return None

    def _resolve_default(self, default: Any) -> Any:
        if default == "today":
            return date.today().isoformat()
        return deepcopy(default)

    def _is_filled(
        self,
        session: SessionSlots,
        slot_name: str,
    ) -> bool:
        slot = session.slots.get(slot_name)
        return bool(slot and slot.filled)

    def _build_ask_message(self, slot: SlotState) -> str:
        if slot.follow_up_prompt:
            return slot.follow_up_prompt

        ask_message = slot.description or f"请提供 {slot.name}"
        if slot.enum:
            options = " / ".join(slot.enum)
            ask_message = f"{ask_message}（可选：{options}）"
        return ask_message
