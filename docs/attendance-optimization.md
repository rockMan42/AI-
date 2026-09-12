# 考勤查询优化交付与启用

## 已实施

- 修复卡片返回早于业务完成状态的问题；Tool 使用正确的 Future 超时类型并取消任务。
- 授权会话先释放，再执行独立会话查询。考勤 HTTP 认证会话提前关闭；webhook 用户解析使用独立短会话，其他技能仍使用原来的业务会话。
- 飞书 AsyncClient 在应用生命周期内复用（20 连接、10 空闲连接，连接/排队 1 秒，读写 5 秒），令牌刷新使用进程内锁及二次缓存检查。
- 新增本人查询完整句式路由，现有待补槽、重试和未结束会话优先；未命中继续调用模型。所有结果仍经统一执行器、Tool 和权限校验。
- Agent 延迟到模型线程内创建；每次独立实例。SDK chat.completions.create 使用剩余预算覆盖 Hermes 自带长超时，不改全局模型/provider 配置；Hermes 总尝试次数设为 1（包含首次请求，不进行额外重试）；不能设为 0，否则不会发起模型请求。空模型回复按失败记录。
- 模型最多 5 并发，排队最多 1 秒，执行等待 10 秒。外层超时或取消会请求 Agent 中断；名额在线程真正结束后归还。Python 无法强制杀死线程，底层网络超时及中断是协作式的。
- 新增请求和阶段日志；嵌套请求复用追踪标识，不记录考勤正文。HTTP 总计时涵盖认证并记录状态码；服务阶段计时包含连接获取、授权、查询及缓存命中。

## 配置

```dotenv
# 默认 false；在测试账号验证后开启，关闭即可回到原模型路由。
ATTENDANCE_FAST_PATH_ENABLED=true
INTENT_TIMEOUT_SECONDS=10
INTENT_QUEUE_TIMEOUT_SECONDS=1
INTENT_CONCURRENCY=5
```

这些参数从 Settings 读取，需要重启应用生效；未修改现有 .env，也未默认开启快速路径。
并发限制为每个进程独立，多个 worker 的总并发会累加。模型执行器也用于非卡片结果的文字生成，失败时仍回退技能原始消息。

首批句式：查本月/上月考勤、查今天/昨天打卡、查本月/上月迟到次数或早退次数、查今年/去年/2025年/25年假期余额。允许明确的礼貌前后缀；不支持的句子回退模型，不做模糊匹配。只有“XX年假期余额”的两位年份补为 20XX。

## 验证与复现

在项目根目录执行：

```bash
PYTHONPATH=. .venv/bin/python -B -m pytest -q tests/test_attendance_optimization.py
PYTHONPATH=. .venv/bin/python -B scripts/benchmark_attendance.py \
  --user-id 3 --samples 100 \
  --output /tmp/attendance-benchmark.json
```

基准脚本读取本地配置、为指定已有用户生成短期 JWT，仅请求本地考勤 GET 接口。不输出 JWT 或响应正文，不发送飞书消息。使用不同环境时需明确更改 base-url；只对有权访问的测试环境执行。

接口前后对比及实测边界见 `artifacts/attendance-optimization/report.md`。测试使用 MockTransport、内存会话及模拟数据库对象验证边界，不能替代真实飞书/模型耗时验收。

上线前使用测试账号在机器人私聊验证命中与回退场景，观察 `attendance_fast_path`、`agent_create`、`model_queue`、`model_execution`、`attendance_pool_acquire`、`attendance_cache`、`feishu_send` 和 `feishu_total`。飞书平台接受消息不等于客户端已显示。

## 回退

规则误判时先设置 `ATTENDANCE_FAST_PATH_ENABLED=false` 并重启。资源优化和状态修复与规则开关独立；如需撤回代码，仅撤回本次对应改动，不重置工作区。没有数据库迁移或新增业务缓存，不需要数据回滚。

## 模型分类链路修正

意图分类现使用同一模型和服务地址的一次 OpenAI SDK JSON 请求，只传业务分类提示词和用户输入，不进入 Hermes 通用 Agent 工具循环，不携带工具定义。仍通过 ModelRunner 限制并发和等待时间；SDK 不额外重试。Hermes Tool 注册和后续业务执行不变。非卡片业务结果的文字生成仍沿用原 Agent 路径。

已使用真实模型验证“统计一下8月份的迟到和早退”：一次分类耗时 7.8 秒，识别为 attendance_query，参数 query_type=late_count、query_month=2026-08。未执行飞书发送；这不代表端到端性能达标。未指定年份按当前年份解释，查询历史年度应明确年份。
