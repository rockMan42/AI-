# RAG 发布、重建与回退

本轮代码不改变 HTTP 请求/响应和知识工具名称。默认仍用旧 Collection 的纯向量检索；开启混合检索前必须完成维护窗口迁移。旧 Collection 不自动删除。本手册中的数据库恢复、数据重建和配置切换由维护负责人确认后执行。

## 1. 当前发布前置条件

- 2026-09-11 只读核实：Milvus 服务端 v2.5.4、PyMilvus 3.0.1、MCP 1.26.0、Hermes 0.19.0。
- 当前服务端调用 `RunAnalyzer` 返回 `UNIMPLEMENTED`。这不证明 BM25 本身不可用，但无法完成本计划要求的中文分析器检查。代码在创建新集合之前阻止发布，不能只比较版本号后放行。
- 先单独安排数据库兼容性变更，在隔离环境验证中文分析器、BM25 Function、稀疏索引、异步搜索及现有稠密索引都兼容，再重新安排本手册的迁移。不要在本次重建脚本中升级数据库。
- 必须准备经人工标注的 100 题与员工、管理者账号；原方案的基线尚未采集。本轮代码已经优化，不能把优化后的结果叫作原方案基线。原方案应从保留的旧部署/变更前快照采集，不能回滚覆盖当前工作区。
- 5 秒是失败截止时间，超时返回错误不算性能通过；外部模型的时延和配额必须实测。

原生全文检索的字段、函数和索引要求见 [Milvus 官方文档](https://milvus.io/docs/v2.5.x/full-text-search.md)，中文分析器配置见 [Chinese analyzer](https://milvus.io/docs/v2.5.x/chinese-analyzer.md)。

## 2. 配置与发布顺序

第一阶段保持原 `MILVUS_COLLECTION`，设置：

```dotenv
KNOWLEDGE_RETRIEVAL_MODE=dense
MILVUS_BM25_ENABLED=false
RAG_QUERY_TIMEOUT_SECONDS=5
RAG_QUERY_CONCURRENCY=5
RAG_MAX_TOKENS=512
QUERY_EMBEDDING_CACHE_ENABLED=true
QUERY_EMBEDDING_CACHE_TTL=86400
QUERY_EMBEDDING_CACHE_TIMEOUT_SECONDS=0.05
KNOWLEDGE_CHUNK_TARGET_TOKENS=384
KNOWLEDGE_CHUNK_MAX_TOKENS=512
KNOWLEDGE_CHUNK_MIN_TOKENS=50
```

父进程把实际配置传入 MCP 子进程，不分别编辑子进程环境。修改环境后重启整个 API 进程及其 MCP 子进程；已有 `.env` 的显式值优先于代码默认值。第一阶段不触发重新向量化，不改原有索引里的分块。

先采集“原方案”，再采集“性能优化＋旧索引”。迁移后同一新索引先设置 `MILVUS_BM25_ENABLED=true`、`KNOWLEDGE_RETRIEVAL_MODE=dense` 采集“新切块纯向量”，最后改为 `hybrid` 采集混合检索。四次使用同一题库、权限、模型、top_k 和固定文档版本，保存每次配置与结果，分别解释收益。

## 3. 维护与备份

1. 在网关关闭知识库查询、上传、删除、向量化入口，暂停飞书知识查询。设置 `KNOWLEDGE_MAINTENANCE=true` 并重启；知识 API 返回 503，MCP 知识查询返回工具错误，两个知识 Worker 不启动。
2. 对所有旧进程优雅停机，等待请求、解析、清理、向量化任务结束；确认没有其他实例或独立 Worker 写入。维护配置只影响读取了该配置的新进程，不能替代关闭旧实例。
3. 将所有文档处理到稳定的 `embedded` 状态。迁移脚本遇到中间状态、缺块、缺少向量 ID、同版本块的激活状态不一致时停止；先查明原因，不在迁移时猜测修复。
4. 用受保护的 MySQL 连接配置完成 `kb_document`、`kb_chunk`、`kb_search_log` 一致性备份；保存现有配置、原 Collection 名称。下面命令中的登录配置和数据库名必须由维护负责人填写，密码不写进命令行：

```sh
umask 077
mysqldump --login-path=knowledge --single-transaction --no-tablespaces DATABASE_NAME kb_document kb_chunk kb_search_log > /SECURE_BACKUP/knowledge-before.sql
```

5. 在隔离恢复库验证备份能恢复、文档/版本/块数和 `milvus_id` 一致。保留该备份与旧 Collection，维护期间禁止知识业务写入。
6. 重建脚本会额外生成权限为 0600 的原始文件清单和旧向量 ID 映射，包含 MinIO 对象版本和 SHA-256。请将清单和 SQL 备份保存在受控备份目录，不提交 Git。

## 4. 重建与核对

从项目根目录执行（示例目标名可替换，但必须是不存在的新集合）：

```sh
KNOWLEDGE_MAINTENANCE=true PYTHONPATH=. .venv/bin/python -B scripts/rebuild_knowledge_index.py \
  --target enterprise_knowledge_bm25_v1 \
  --mysql-backup /SECURE_BACKUP/knowledge-before.sql \
  --manifest /SECURE_BACKUP/knowledge-before-manifest.json \
  --apply
```

脚本不会替你创建或证明 SQL 备份有效。它会拒绝覆盖已有目标集合/清单，固定 MinIO 对象版本读取原始文件，校验摘要，重新切块，写新集合，核对新向量字段和 ID 后逐文档事务更新 MySQL 分块记录，保留文档版本、权限和激活状态。失败可能已提交前面的文档，因此必须保持维护并执行回退；不要对同一目标直接重跑，也不要把半成品作为可查询索引。

切块以条款及段落为边界，无固定重叠。小于 50 tokens 的独立章节或不能安全合并的块允许保留，不能为满足下限跨章节拼接。超过硬上限的单句按 Unicode 字符边界切；超长表格行重复表头，若连表头加单个字符都装不下则明确失败，交由人工整理源文件。

完成后额外人工核对：每个版本的激活状态、权限、文档 `chunk_count` 与 SQL 行数一致；每个 `milvus_id` 对应相同的 doc_id/version/chunk_index；新集合总行数等于 SQL 分块总数；旧 Collection 仍保留。检查企业术语、英文编号、数字范围的分析器输出，再确认混合搜索能召回这些条款。

## 5. 维护窗口内验收与切换

网关继续关闭，Worker 继续停止。为验收单独启动本机调用方调试会话，只初始化 `init_db` 和 `init_hermes_agent`，使用现有 `call_knowledge_search` 入口；不要运行会启动 Worker 的 `app.main` lifespan。设置新 `MILVUS_COLLECTION`、`MILVUS_BM25_ENABLED=true`。该隔离调用方需 `KNOWLEDGE_MAINTENANCE=false` 才能查询，它的 MCP 子进程也不启动 Worker。结束时调用 `shutdown_hermes_agent` 和 `close_db`。日志按正常链路提交，不绕过日志检查。HTTP 权限接口在第一阶段单独联调，维护窗口的性能验收走实际 MCP 调用方。

人工验收记录保存在 `docs/rag-acceptance.md`。至少完成：

1. 100 题标注题号、类别、权限、正确来源、必要条件、数字和例外、是否应拒答。按 60 有答案/20 无答案/10 权限边界/10 版本状态分组。
2. 调试断点检查 MySQL 校验后的候选与 Top-20 证据；不要把问题、候选正文或凭证写入普通日志。按题号保存人工判定。
3. 对比四阶段 Top-20 证据召回率和 Top-5 命中率。召回率不得下降，至少修复三个基线失败题；若基线没有失败则全部保持通过。答案中的条件、数字、例外逐项核对。
4. 员工请求 confidential 必须返回权限错误；合法权限请求也不能把越权、过期、删除的块送入 Rerank/LLM。无答案题不能无依据确定作答。
5. 先预热服务/模型，再仅删除本题库对应的查询向量缓存键。键计算为 HMAC-SHA256：密钥为 `KNOWLEDGE_LOG_KEY`，消息为 `json.dumps(["query-v1", embedding_model, dimension, query.strip()], ensure_ascii=False).encode()`，前缀 `dep:kb:embedding:`。不要清空 Redis 数据库；不要只禁用缓存来冒充缓存未命中验收。
6. 在现有调用方调试/压测控制台对 80 个正常检索问题使用五个持续工作的客户端槽位；每完成一题立即提交下一题，同一 MCP 连接，计时从调用 `call_knowledge_search` 之前开始，到完整返回之后结束。记录每题首次耗时、返回完整性和异常。确认日志中 80 题均为 cache_hit=False。全部必须 <5000ms，错误、超时或截断任一出现即失败。
7. 单独检查命中缓存、Redis 不可用、模型超时、MCP 断连、日志写入失败。故障注入只在隔离进程/专用验收资源进行，不停共享生产 Redis、不破坏真实凭证；MCP 断连在途请求应失败，新连接恢复后仅接受新请求。日志提交失败不能返回正常业务结果。
8. 记录 P50/P95/P99/max 和错误率，不删除失败样本，也不以百分位代替逐题达标。飞书耗时只看 `feishu_intent`、`feishu_send`，不计入 MCP 5 秒。

验收全部通过后停止隔离进程，将新集合与 hybrid 配置同时发布到正式 API/MCP，恢复知识 Worker 与网关访问。没有通过则保持维护并回退。

## 6. 回退

维护负责人确认仍没有重新开放知识业务写入后，停止所有 API/MCP/Worker，用已验证的 SQL 备份恢复知识表：

```sh
mysql --login-path=knowledge DATABASE_NAME < /SECURE_BACKUP/knowledge-before.sql
```

恢复旧 `MILVUS_COLLECTION`、`MILVUS_BM25_ENABLED=false`、`KNOWLEDGE_RETRIEVAL_MODE=dense` 及其他原配置；核对原向量 ID 映射、权限和版本，隔离验证后再恢复流量。新旧 Collection 都不由脚本自动删除。验收期间新增的检索日志如需保留，应在恢复前另行归档。

**重新开放业务写入后不能执行上述快照回退。** 必须先制定并验证新增文档、删除和版本变更的增量迁移方案，避免旧快照覆盖新数据。

## 7. 耗时日志

`rag_stage` 按随机请求 ID 关联调用方与 MCP：caller_total、mcp_wait、authentication、authorization、embedding、milvus_dense、milvus_bm25、mysql_validation、rerank、generation、log_commit、service_total。mcp_wait 记录连接内的并发槽位等待，调用方线程调度等待仍包含在 caller_total 中。并行阶段耗时不能简单相加。

日志只包含耗时、状态、候选数量、模型名和模型返回的 token 用量。问题以现有 Fernet 密钥加密后写检索日志表；查询向量缓存键不包含明文。正常返回前仍必须提交检索日志。`RAG_MAX_TOKENS=512` 是生成上限，finish_reason 非 stop 一律失败。
