-- MySQL 8.0+
-- 执行前核对当前表结构并备份。
-- 该迁移由你手动执行，不在应用启动时自动执行。

CREATE TABLE kb_search_log (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL COMMENT '用户ID的HMAC标识',
    query TEXT NOT NULL COMMENT 'Fernet加密后的问题',
    top_score FLOAT NOT NULL,
    result_count INT NOT NULL,
    is_low_confidence TINYINT(1) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 使文档状态约束与当前 KnowledgeDocument 模型一致。
SET @drop_status_check = (
    SELECT IF(
        EXISTS(
            SELECT 1
            FROM information_schema.TABLE_CONSTRAINTS
            WHERE CONSTRAINT_SCHEMA = DATABASE()
              AND TABLE_NAME = 'kb_document'
              AND CONSTRAINT_NAME = 'ck_kb_document_status'
              AND CONSTRAINT_TYPE = 'CHECK'
        ),
        'ALTER TABLE kb_document DROP CHECK ck_kb_document_status',
        'DO 0'
    )
);

PREPARE stmt FROM @drop_status_check;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

ALTER TABLE kb_document
    ADD CONSTRAINT ck_kb_document_status
    CHECK (
        status IN (
            'parsing',
            'completed',
            'embedding',
            'embedded',
            'embedding_failed',
            'failed',
            'deleting',
            'delete_failed'
        )
    );