# 07b02 回退发现

- 当前工作区包含大量既有暂存和未暂存改动，不能使用整文件 Git 回退。
- 07b02 与 `settings.py`、`main.py`、知识 API、文档服务和模型共享文件，需要逐项移除引用。
- 用户确认仅回退 07b02；数据库和 Milvus 中已有数据不在本次删除范围。
- 新的 07b02 实现只在会话输出，不写回当前项目。
- 原 07b01 需求明确要求 `kb_chunk.milvus_id` 和 `kb_chunk.is_active` 作为后续向量化预留字段，因此这两个模型字段必须保留。
- `kb_document.index_status`、`kb_index_task`、Embedding 客户端、索引 Worker、搜索路由和 Milvus Collection 操作属于 07b02，应移除。
- `document_cleanup.py` 和 `deleting/delete_failed` 属于此前确认保留的安全删除能力，不回退。
- Git 暂存区中的多个 07b01 文件只是空占位，无法作为自动恢复基线；共享文件必须手工做字段级回退。
- 当前百炼官方文档显示 `text-embedding-v3/v4` 同步接口单次最多10条，不是需求文档中的25条；新参考实现应使用10条并保留1024维和8192 Token限制。
- Milvus当前官方客户端支持自定义Schema、HNSW、AutoID Collection的全实体upsert，以及TLS证书参数；旧版本失活需要查询完整实体后修改 `is_active` 再upsert。
