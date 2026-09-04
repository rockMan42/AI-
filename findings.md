# Findings & Decisions

## Requirements
- 移除 `intent_session.py`，统一使用 `SessionStore`。
- 融合当前项目与用户附件中的多轮追问代码，不能脱离现状自行编造。
- 最终实现多轮对话、槽位默认值、缺失必填槽位追问、槽位管理和执行状态管理。
- 上一轮要求不修改业务源码；本轮用户已明确授权直接实施，并要求不运行测试。

## Research Findings
- 2026-09-04 截图中同一句“我想请明天一天的年假”出现了两个独立的用户消息气泡，各自都有“1 条回复”；不是一个消息气泡收到两次回复。
- 新日志记录了两个不同的飞书消息 ID：`om_x100b6697da3f3088b22ace5675cd448` 与 `om_x100b6697dacc247cb1035e5beb6653c`，在约 1 秒内并发进入，因此现有按 message_id 去重不会把它们视为重复推送。
- 两个请求共用 Hermes 内部 session `20260904_112149_5ebafc`；日志明确告警 concurrent turns，并连续出现 `Streaming attempt superseded by a newer stream`。
- 被抢占的请求耗尽空响应重试后返回无法解析的文本，网关按 unknown 路由发送 fallback；另一请求随后正确识别 leave_apply、完成槽位收集并提交申请编号 9。
- 日志末尾第三次打印相同文本后立即 200 且没有新的 Agent turn，符合相同 message_id 重投被现有 Redis 去重拦截的表现。
- `app/hermes/agent.py` 在应用启动时只创建一个全局 `_agent`，所有 webhook 请求都从 `get_agent()` 取得同一实例；传入不同 `task_id` 没有隔离该实例的内部会话/流状态。
- 当前 Redis 去重实现是先 `GET` 再普通 `SET`，不是原子占位；同一个 message_id 高并发重投仍理论上可能同时通过，但本次事故的两个主请求本身就是不同 message_id。
- 当前应用入口未声明 worker 数量；无论单进程还是多进程，生产级修复都应依赖 Redis 原子幂等/分布式串行，而不能只依赖进程内锁。
- 实施开始时工作区已有大量未提交改动；相关会话、槽位和网关文件本身也处于修改/新增状态，必须在当前内容上做最小补丁，不能覆盖用户改动。
- 当前 `leave.py` 已由用户从空实现改为返回成功，但文案误写成“找到了您的考勤记录”，需要按请假业务纠正。
- 当前网关无论是否存在 `pending_slot` 都先调用全局 LLM 分类和 `IntentRouter`；因此待补槽位短回答仍可能在写入前被中置信度确认或 fallback 拦截。
- 当前 `SlotCollector.collect()` 已能校验/合并槽位字典，但网关只传模型的 `slots` 字典，原始短回答不会被用作当前枚举槽位的确定性补全。
- 当前 `ConversationManager._can_continue()` 把 workflow=`failed` 视为终态，因此执行失败后同意图也会重建事务并丢弃已收集槽位。
- 所有 Skill YAML 均未定义用户可读意图名；`SkillSchema` 也只有内部 `name` 和长描述，适合新增带默认兼容的 `display_name`。
- `LeaveRequest` ORM 模型已经存在，但项目没有请假创建服务；其他写操作通常由当前请求的 `AsyncSession` 执行并提交。为避免把“仅收集完成”误报为“已提交”，请假执行器应创建并提交 `LeaveRequest` 后再返回成功。
- 请假数据表注释使用英文假别编码，而 YAML 给出中文枚举；执行器需要做明确映射，并在缺少必填值或时间范围不合法时返回失败结果。
- `SkillSchema` 仅在注册器中实例化，新增带默认值的 `display_name` 并由 YAML 加载不会破坏项目内其他构造调用。
- 数据库依赖不会自动提交事务，请假执行器若创建申请必须显式 `commit`，并在异常时 `rollback` 后继续抛出，由网关统一标记执行失败。
- 实施后的网关已将显式重试和可确定解析的 `pending_slot` 放到 LLM 分类之前；对必须由 LLM 解析的自然语言日期，则在模型返回当前意图时绕过中置信度确认，直接交给槽位校验。
- 执行器返回 `success=False` 时不能继续标记 completed；网关已改为标记 execution_failed 并直接返回业务错误消息。
- 附件定义了 `SlotCollector.collect()`：LLM 提取槽位、更新槽位状态、判断是否齐全、生成下一轮追问。
- 附件定义了物资品类动态模板：IT设备追加 `device_model/asset_tag`，劳保用品追加 `size`。
- 附件定义了 `ConversationManager`：按用户和意图创建独立会话，并支持挂起和切换。
- 附件验收标准覆盖单轮提取、多轮补全、动态槽位、恢复、超时、多事务隔离、校验、最大轮次和 Redis 持久化。
- 附件不能原样复制：`app.hermes.skills.registry` 与当前项目路径不符；`get_redis()` 当前不存在；`SlotState(name=...)` 与当前必填构造参数不符；附件调用 `get_next_unfilled_required()`，当前模型只有 `get_next_required_filled()`；`asked` 类型也不一致。
- 需要保留附件的流程思想并适配当前项目，而不是机械复制。
- 当前飞书网关每轮消息都先调用意图分类器，尚无 `pending_slot` 优先处理分支；因此“年假”“明天”等短回答可能被路由为 unknown。
- 当前网关依赖 `intent_session` 的四个函数：读取、保存、意图切换、更新执行状态。移除文件前必须替换这些调用。
- 当前 `SessionSlots` 只表达单个意图及槽位字典，没有置信度、workflow 状态、pending_slot、追问轮次、挂起事务和 unknown_count。
- 当前 `SlotState` 的 `filled`、`asked` 没有默认值，附件按 `SlotState(name=...)` 初始化会失败；`asked` 当前标注为 `str`，实际逻辑按布尔值使用。
- 当前 `SessionStore` 同时写 `dep:sess:slots:*` 与 `dep:sess:meta:*`，后者会覆盖 intent_session 数据；统一后应由它持久化完整会话对象。
- 当前网关已经有 message_id 去重，可继续复用 AC-10 之外的幂等保护。
- 当前 Skill YAML 已包含 `default` 与 `follow_up_prompt`，注册模型也已接受这两个字段。
- 当前执行器多数还是空实现；多轮引擎可以在槽位齐全后正确调用执行器，但不能让尚未实现的业务执行器自动产生真实业务结果。
- 当前测试目录只有空的 `conftest.py`，没有可复用的会话或槽位测试。
- 当前项目有大量既存未提交改动和 `__pycache__` 文件；本轮不会覆盖或清理这些用户内容。
- 新日志显示首次输入“请明天一天的年假”时，模型已一次提取 `leave_type/start_time/end_time/duration`，SlotCollector 返回 execute；槽位抽取本身成功。
- 首次执行失败的直接原因是当时 `LeaveSkillExecutor.executor()` 返回 None，网关主动抛出 `RuntimeError`，会话随后变成 workflow failed、slot status completed。
- 第二次输入同类请假请求时，分类 Prompt 仍携带旧 completed 会话；`ConversationManager._can_continue()` 返回 false，随后 `_start_intent()` 清空槽位，新请求又从 leave_type 开始追问。
- 用户回复“年假”时，一轮模型输出 intent=leave_apply/confidence=0.85 但 extracted_slots 为空，因此 Collector 无法填入 leave_type，再次追问同一问题。
- 另一轮“年假”输出 confidence=0.6，落入 IntentRouter 的 confirm_intent 分支；当前文案直接展示内部编码 `leave_apply`，不符合用户语言。
- 当前 `IntentRouter` 没有意图展示名，只能用 intent code 拼确认文案；应使用 SkillSchema.description 或新增 display_name。
- 日志中的模型推理多次声称 continuation_context 仍为 completed 且含旧值，需要进一步核对 Redis 快照，排除会话保存未生效、进程并发或模型未遵循上下文。

## Technical Decisions
| Decision | Rationale |
|----------|-----------|
| 保留 `session_store.py` 与槽位领域模型分层 | 存储和业务状态职责清晰 |
| 将附件的 Collector 和 ConversationManager 职责融合进现有服务边界 | 兼容附件能力，同时避免新增相互覆盖的会话存储实现 |
| `pending_slot` 存在时先做槽位提取，再决定是否重新分类 | 保证简短追问回答能够延续原事务 |
| 会话模型同时保存 workflow 状态与 slot collection 状态 | 两套状态语义不同，应明确分字段保留 |
| 统一 Redis Key 为 `dep:sess:{session_id}`，旧 Key 只读兼容迁移 | 新代码消除双写，已有 30 分钟会话仍可恢复 |
| 动态品类槽位保存在当前会话，并注入下一轮分类 Prompt | 保证不在静态 YAML 中的 `device_model`、`size` 等槽位也能被提取 |
| `SlotCollector.collect()` 同时接受槽位 dict 或附件式 LLM 回调 | 当前网关不重复调模型，同时保留附件调用兼容性 |
| `ConversationManager.get_or_create_session()` 的 confidence 提供默认值并保留 `switch_intent()` | 兼容附件原始三参数调用方式 |
| 修改 `intent_router.py` 的用户话术 | 本轮明确要求杜绝向用户展示 `leave_apply` 等内部编码 |

## Issues Encountered
| Issue | Resolution |
|-------|------------|
| 附件接口与当前项目不匹配 | 逐项映射到现有注册表、Redis 封装和槽位模型 |
| 辅助规划补丁上下文不匹配 | 读取实际文件后重新定位段落，业务源码未受影响 |

## Resources
- `/Users/rockman/.codex/attachments/662ada48-f8ff-4e12-a09c-38da734b19e1/pasted-text.txt`
- `app/services/session_store.py`
- `app/services/slot_manage.py`
- `app/services/intent_session.py`
- `app/api/v1/feishu_gateway_webhook.py`
- 临时候选目录：`/tmp/ai-digital-employee-design.nlR6ow`（不属于项目源码）

## Visual/Browser Findings
- 无。
