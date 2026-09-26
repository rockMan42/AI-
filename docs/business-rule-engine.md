# 07i01 业务规则引擎

## 已实现的行为

- 三类规则统一发布、预览、历史、差异、候选确认及回滚。回滚生成递增新版本。
- JSON Schema 2020-12 与业务语义校验；Schema 由服务端维护。
- 当前配置、历史快照、成功审计和 Outbox 同事务提交；通过 expected_version 防止丢失更新。
- HRAdmin 权限和组织权限版本复核；旧报销管理入口委托同一服务，不再直接写旧规则表。
- 版本化 Redis 快照、单调版本指针、广播和每 5 秒主库对账。过期 10 秒且无法对账时拒绝规则请求。
- 报销保留原有标准匹配语义；新草稿保存报销/审批版本及人员路线，确认时复核。
- 审批首期仅主管、财务、出纳三个顺序阶段，接入财务模拟端与审批事件校验；旧在途单据保留原流程。
- 考勤查询一次读取月度数据，用最新规则重算后筛选和分页，统计共享同一结果；不修改原始记录。
- 飞书规则 Skill、考勤字段编辑表单、其他规则 JSON 编辑表单、预览与确认/回滚卡片。
- 制度每 15 分钟检查，待确认版本与已确认版本分离；提醒去重、失败重试。
- 多维表格是只读展示投影，每规则版本一条记录；稳定业务键和 client_token 防重复创建。

## 配置与启用顺序

以下配置已在 Settings 声明，默认全部关闭，不改现有 `.env`：

| 配置 | 用途 |
|---|---|
| BUSINESS_RULES_ENABLED | 切换报销、考勤到统一引擎；启动时要求三类首版均已发布 |
| RULES_WORKER_ENABLED | 启动 Outbox 投递和制度监控；可先于业务切换启用 |
| RULES_FEISHU_ENABLED | 允许访问飞书文档、发送提醒、同步多维表格 |
| RULES_ENVIRONMENT | Redis 键/频道环境隔离，生产必须与开发测试不同 |
| RULES_HR_OPEN_IDS | 制度提醒接收者 open_id 的 JSON 数组 |
| RULES_BITABLE_APP_TOKEN / RULES_BITABLE_TABLE_ID | 已配置只读成员权限的多维表格 |
| RULES_SENSITIVE_TYPES | 需要加密保存的规则类型数组；应在第一次发布前配置 |
| RULES_ENCRYPTION_KEY_ID | 当前写入密钥版本 |
| RULES_ENCRYPTION_KEYS | JSON 字典：版本号映射 Base64 编码的 32 字节 AES 密钥 |

密钥由环境或密钥管理系统注入。轮换时保留旧密钥供历史解密，改变当前写入版本即可。已加密规则后续发布即使从敏感类型列表移除仍继续加密；不要直接把已有明文历史视作加密完成，存量加密需要专门迁移。

迁移、初始规则发布、飞书联调、生产切换应分别经过管理员核对。代码实施不等于这些上线步骤已执行。

1. 在数据库备份和迁移窗口确认后，执行下面脚本**输出**的五张新表 DDL；不改旧业务表。
2. 冻结旧报销规则写入，导出最终的旧规则与政策，运行转换器，核对所有报错和新旧结果。
3. 使用已认证 HRAdmin 发布三类 v1；初始 `expected_version=0`。考勤班次与审批人来自 HR 已确认配置，不能直接使用测试值。
4. 先启用 `RULES_WORKER_ENABLED`，检查 `GET /api/v1/rules/_health` 和缓存传播。
5. 核对三类配置后对所有业务服务统一启用 `BUSINESS_RULES_ENABLED`。旧草稿需重新生成；切换后旧规则表只读。
6. 飞书应用权限、制度文档可访问性、多维表格字段和 HR 接收者都完成核对后，再启用 `RULES_FEISHU_ENABLED`。

```bash
.venv/bin/python migrations/20260921_02_business_rules.py
.venv/bin/python scripts/convert_business_rules.py /absolute/path/expense-export.json
```

转换输入结构：`{"policy": {原政策字段}, "rules": [旧规则对象]}`。转换器只读本地 JSON，输出验证结果及首次 PUT 请求；任何无法映射项都会阻止成功输出。工具不访问现有数据库，也不自动发布。输出包含业务标准，请按业务数据权限保存。

## API 契约

全部管理接口要求可信登录态及 HRAdmin；JSON 请求拒绝未声明字段。规则类型为 `reimbursement / approval / attendance`。

| 方法和相对路径 | 行为 |
|---|---|
| GET `/rules` | 当前版本列表 |
| GET `/rules/_health` | 当前进程版本、数据库版本、事件积压和制度监控状态 |
| GET `/rules/{type}/schema` | 首次发布前获取服务端 Schema |
| GET `/rules/{type}` | 当前完整配置及制度监控状态 |
| PUT `/rules/{type}` | 发布，必须带 `Idempotency-Key` |
| POST `/rules/{type}/preview` | 字段校验、差异及可选 samples 新旧试算，不产生版本 |
| POST `/rules/{type}/change-requests` | 保存候选/回滚目标，必须带 `Idempotency-Key` |
| POST `/rules/{type}/confirm` | 用 `change_id + token` 确认发布 |
| POST `/rules/{type}/rollback` | 只接受回滚候选的 `change_id + token` |
| GET `/rules/{type}/versions?offset=0&limit=20` | 历史元信息 |
| GET `/rules/{type}/versions/{version}` | 不可变历史快照 |
| GET `/rules/{type}/diff?from=1&to=2` | 字段及制度绑定差异 |
| PUT `/rules/{type}/bindings` | 使用基准版本发布绑定变更，必须带幂等键 |

前缀为项目 `APP_PREFIX`（通常 `/api/v1`）。

PUT 请求形状：

```json
{
  "expected_version": 1,
  "change_summary": "填写真实变更原因",
  "rule_data": {},
  "bindings": [{"doc_id": "实际文档ID", "doc_title": "制度名称", "acknowledged_revision": 2}]
}
```

`rule_data` 按 Schema 填写完整配置，`bindings` 是完整绑定列表，空列表表示解除全部绑定。成功返回 `rule_type/version/content_hash/propagation`。`propagation=pending` 表示数据库已提交，由后台投递保证最终同步，不表示业务提交失败。

回滚先建立候选：`{"expected_version":8,"rollback_version":3,"change_summary":"填写原因"}`，再携带返回的 `change_id/token` 调用 rollback。结果为 v9，数据及确认的制度绑定恢复为 v3；若制度已继续更新，监控状态仍保留“待确认”。

候选有效期 15 分钟，绑定操作者、基准版本及完整内容。使用相同幂等键重试返回同一次结果；相同键换参数返回 409。卡片的操作者来自验签事件，不信任按钮自带的用户编号。

错误响应包含 `detail.code/message/fields`。422 提供 JSON 字段路径；409 表示版本/幂等/确认冲突；403 表示无权限；503 表示不能取得可信规则或依赖暂不可用。

旧报销规则和政策写接口部署后即委托新服务，旧表不再接收写入；请将新表迁移与旧配置写入口切换放在同一维护窗口，避免后台管理短时不可用。

## 执行语义与边界

**报销**：`policy + rules` 的整份配置共同版本化。保留原冲突组、优先级、职级/城市映射、有效期、超标授权和基础票据校验。金额为十进制字符串。复合条件支持 all/any/not；公司抬头、税号及日期等基础校验必须独立正向配置，不能用逻辑组合绕过。

**审批**：金额区间 `[min_amount,max_amount)`，上界 null 表示无限；仅显式配置 default_chain 时兜底。节点顺序固定 manager/finance/cashier，引用可为直属主管或具体用户。快照人员在提交时解析；人员失效阻止继续处理，不能跳过节点或随意改下一审批人。财务 MCP 使用提交快照，无需依赖最新规则缓存。真实财务系统尚需独立适配和合同联调。

**考勤**：每次查询固定当前规则版本，保留 source_status、原始打卡与原始工时。`status` 是兼容摘要，`issues` 可同时包含 late/early_leave；按异常集合筛选和计数。整日请假、休息日、跨夜均单独处理；缺日历、部分请假、单边缺卡、倒置或未来打卡标记待核实。没有记录的日期不推断缺勤。班次未结束不提前判断缺勤。历史月份结果随新规则改变，卡片明确显示版本及重算说明。

**缓存**：MySQL 主库是唯一事实源；Redis 只提供已由主库版本/摘要确认的不可变快照。订阅断线由 5 秒对账修复。普通请求固定单版本；关键报销事务持规则读锁直到提交，发布获得写锁后才变更。健康进程目标 10 秒传播；超过可信时限失败关闭。

**制度**：新版文档的整数 revision_id 作为技术修订号。发布校验文档访问权限与版本，不能把请求中的版本直接当事实。没有真实授权时保持飞书开关关闭；有绑定的发布会明确返回集成未启用，不能假装核对成功。

## 飞书部署与运维

使用现有验签卡片回调入口 `/feishu/callback/card`，`module=rules` 分发。自然语言 Skill 名为 `business_rule_manage`；编辑、历史、回滚和“制度变化无需修改”均通过该入口操作。

多维表格需预先建立字段：`规则版本`（文本，业务唯一标识）、`规则类型`（文本）、`版本号`（数字）、`内容摘要`（文本）、`制度文档`（文本）、`修改人`（文本）、`修改时间`（文本）、`制度状态`（文本）。按规则类型分组、版本号倒序展示；详细差异通过管理 API 或规则卡片查看。不要把敏感规则正文同步到表格。成员只读权限需在飞书后台设置。

权限至少覆盖文档元信息读取、应用发消息和目标多维表格记录读写；最终以应用后台及官方 API 当前要求为准。本次未登录后台、未申请权限、未发送真实消息。

告警入口：`rules_reconcile_failed`、`rules_subscription_failed`、`rule_outbox_retry`、`rule_delivery_sla_exceeded`、`rule_document_check_failed`。健康接口可供监控检查每个实例版本、对账年龄、Outbox 最老事件和制度最后检查时间。监控必须按实例收集，不能只请求负载均衡后的一个实例。

事件默认指数退避、最高 5 分钟；租约 60 秒、单次外部处理预算 40 秒；崩溃后租约过期可重新领取。飞书事件使用稳定 UUID，表格使用稳定版本键与 client_token。飞书关闭时保留待处理事件，恢复后继续；不可将关闭状态视为两小时提醒已通过。

业务配置回退使用规则回滚产生新版本。应用回退只选择兼容新存储和快照的版本；不要把业务读源切回已经停止更新的旧表。旧密钥、历史快照及原始业务数据必须保留。

## 验证方式

普通测试不访问真实外部系统：

```bash
.venv/bin/python -m pytest -q
```

集成测试仅在显式指定本机 `rules_test` 前缀数据库时运行；每例创建独立随机测试数据库，不清空已有数据库。使用独立 Redis 容器，不填项目现有连接：

```bash
RULES_TEST_DATABASE_URL='mysql+aiomysql://root@127.0.0.1:测试端口/rules_test' \
RULES_TEST_REDIS_URL='redis://127.0.0.1:测试端口/0' \
.venv/bin/python -m pytest -q -s tests/test_business_rules_integration.py
```

覆盖真实 MySQL 事务失败回滚、多人并发 CAS、确认身份/过期/撤权、Redis 乱序及重试、三个独立进程漏广播恢复、31 天真实记录重算和原数据保护。飞书 API、卡片操作和投影重试使用 Mock；这不等于飞书真实联调已通过。

### 本次本地验证（2026-09-21）

- 全量回归：149 项通过，包含 9 项隔离 MySQL/Redis 集成测试。
- 故意漏掉发布广播：3 个独立进程在约 5.103 秒内均取得 v2。
- 隔离 MySQL 中 31 天记录查询重算约 0.008 秒；该数字不代表完整生产接口的 P95。
- 迁移生成器输出的 DDL 在专用 MySQL 8.0 测试库实际创建五张表成功。
- 新增代码静态检查、Python 编译及 diff 空白检查通过。
- 测试有 5 条既有 SWIG 依赖弃用警告，不影响通过。
- 未执行现有数据库迁移，未设置任何生产规则，未发送真实飞书消息，未验证真实财务。

### HTTP 全自动验收（2026-09-22）

用户授权后，可使用下列脚本完成建表、隔离数据准备、真实 HTTP 测试与资源停机：

```bash
.venv/bin/python scripts/rules_http_acceptance.py --apply-missing-rule-tables
```

脚本读取现有配置，仅支持本机 MySQL。带上述参数时，在当前库创建缺失的五张规则表，不修改既有表和规则数据；每次另建 `rules_http_*` 数据库，复制当前表的列与索引，填入专用员工、会话、31 天考勤、工作日历、请假及发票样本。`CREATE TABLE LIKE` 不复制外键，因此验收不证明旧表外键迁移兼容性。

需要本机 Docker、已有 `redis:latest` 镜像，以及 MySQL 建库、建表和创建触发器权限。脚本会启动专用 Redis 容器和随机本机端口的 Uvicorn 服务；不修改 `.env`。HTTP 使用原应用路由、中间件、真实鉴权和业务实现，不覆盖身份依赖。验收生命周期只启动数据库、Redis、规则 Worker、财务模拟 MCP、报销投递和审批同步，避免无关 Worker 发送消息。

覆盖规则发布/预览/幂等/回滚/并发、权限与撤权、候选过期、Outbox 故障事务回滚、三进程版本传播、Redis 断开与重试恢复、历史考勤重算/分页/统计/原始数据保护、报销额度/缺标准/草稿失效，以及固定审批路线直到模拟打款。认证使用数据库准备的测试身份和有效会话，另测 HTTP 刷新、重放撤销、退出；不代表真实飞书 OAuth 已通过。

报告与脱敏响应保存到 `artifacts/rules_http_<时间>_<随机标识>/`。结束时撤销测试会话、停止验收 HTTP 进程及专用 Redis，保留数据库和容器供核查，不自动删除数据。真实飞书文档、卡片、多维表格和真实财务不在此自动验收范围；飞书关闭期间表格 Outbox 保留待投递。

本轮 HTTP 发现并修复：旧报销管理接口将 `AccessDenied` 包装为 503，现在交由应用统一返回 403；权限本身原先已拒绝，不存在成功越权写入。已增加回归用例。
