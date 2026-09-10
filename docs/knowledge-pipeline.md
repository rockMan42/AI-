# 文档解析与切块

## 执行顺序

1. 备份 `kb_document`、`kb_chunk`。
2. 执行 `migrations/20260908_01_knowledge_pipeline.sql`。
3. 按 `.env.example` 补齐 MinIO、ClamAV 和 OCR 配置。
4. 启动应用并重新上传旧文档，使其使用新的解析与切块逻辑。

## API

```text
POST   /api/v1/knowledge/documents/upload
GET    /api/v1/knowledge/documents/{doc_id}/chunks
DELETE /api/v1/knowledge/documents/{doc_id}
```

## OCR

扫描 PDF 使用 PyMuPDF 的 Tesseract OCR 接口，只处理文本量不足的页面。部署环境必须安装 Tesseract 及 `chi_sim`、`eng` 语言包。未安装时普通文本 PDF 不受影响，扫描件会因无法得到有效文本而解析失败。

## 一致性

- 删除先将文档标记为 `deleting`，再清理 MinIO，最后删除 MySQL 数据。
- 清理失败标记为 `delete_failed`，后台 Worker 自动恢复。
