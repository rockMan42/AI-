-- 物资品类字段规则表。
-- t_requisition、t_requisition_approval 为已有表，不在本迁移中创建或修改。

CREATE TABLE IF NOT EXISTS t_category_field_rule (
    id BIGINT NOT NULL AUTO_INCREMENT,
    category VARCHAR(50) NOT NULL COMMENT '物资品类',
    field_name VARCHAR(50) NOT NULL COMMENT '字段标识',
    display_name VARCHAR(50) NOT NULL COMMENT '展示名称',
    required TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否必填',
    default_value VARCHAR(100) NULL COMMENT '默认值',
    validation_rule VARCHAR(200) NULL COMMENT '校验规则',
    sort_order INT NOT NULL DEFAULT 0 COMMENT '追问顺序',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_category_field (category, field_name),
    KEY idx_category_field_rule_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='物资品类字段规则表';

INSERT INTO t_category_field_rule
    (category, field_name, display_name, required, default_value, validation_rule, sort_order)
VALUES
    ('办公用品', 'item_name', '物资名称', 1, NULL, 'non_empty', 10),
    ('办公用品', 'specification', '规格型号', 0, NULL, NULL, 20),
    ('办公用品', 'quantity', '数量', 1, '1', 'integer:1:100', 30),
    ('办公用品', 'reason', '申领原因', 0, NULL, NULL, 40),

    ('IT设备', 'item_name', '设备名称', 1, NULL, 'non_empty', 10),
    ('IT设备', 'specification', '规格型号', 1, NULL, 'non_empty', 20),
    ('IT设备', 'quantity', '数量', 1, '1', 'integer:1:10', 30),
    ('IT设备', 'reason', '申领原因', 1, NULL, 'non_empty', 40),
    ('IT设备', 'purpose', '用途说明', 1, NULL, 'non_empty', 50),
    ('IT设备', 'expected_return_date', '预计归还日期', 1, NULL, 'date', 60),

    ('劳保用品', 'item_name', '物资名称', 1, NULL, 'non_empty', 10),
    ('劳保用品', 'specification', '规格型号', 1, NULL, 'non_empty', 20),
    ('劳保用品', 'quantity', '数量', 1, '1', 'integer:1:20', 30),
    ('劳保用品', 'reason', '申领原因', 0, NULL, NULL, 40),

    ('其他', 'item_name', '物资名称', 1, NULL, 'non_empty', 10),
    ('其他', 'specification', '规格型号', 0, NULL, NULL, 20),
    ('其他', 'quantity', '数量', 1, '1', 'integer:1:20', 30),
    ('其他', 'reason', '申领原因', 1, NULL, 'non_empty', 40)
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    required = VALUES(required),
    default_value = VALUES(default_value),
    validation_rule = VALUES(validation_rule),
    sort_order = VALUES(sort_order);