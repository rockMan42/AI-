from datetime import UTC, datetime, timedelta

import jwt

secret = "12312312312312312312312312312312312312"

now = datetime.now(UTC)
token = jwt.encode(
    {
        # 必须是数据库 t_user.feishu_open_id 中存在的用户
        # "sub": "ou_1591428b852eda1206e1efa32a3dcc8c",
        "sub": "expense_test_20260918_01_admin", # admin
        # "sub": "expense_test_20260918_01_manager", # manger
        # "sub": "expense_test_20260918_01_finance", # finance
        # "sub": "expense_test_20260918_01_cashier", # cashier
        # "sub": "expense_test_20260918_01_applicant", # applicant
        "iat": now,
        "exp": now + timedelta(hours=1),
        "iss": "ai-digital-employee",
        "aud": "knowledge-api",
    },
    secret,
    algorithm="HS256",
)

print(token)