-- MySQL 8.0+
-- Execute after backing up kb_document and kb_chunk.

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
    CHECK (status IN ('parsing', 'completed', 'failed', 'deleting', 'delete_failed'));
