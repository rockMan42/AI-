"""授权后执行的本机 HTTP 验收；新增隔离库，保留证据，不清空任何已有库。

运行：.venv/bin/python scripts/rules_http_acceptance.py --apply-missing-rule-tables
当前库只创建缺失规则表。样本、会话和全部业务写入在 rules_http_* 库。
"""

import argparse
import asyncio
import copy
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import get_settings
from app.models.attendance import Attendance
from app.models.business_rule import (
    BusinessRule,
    RuleChangeRequest,
    RuleDocBinding,
    RuleOutbox,
    RuleVersionHistory,
)
from app.models.department import Department
from app.models.expense import Invoice
from app.models.leave_request import LeaveRequest
from app.models.leave_workflow import WorkCalendarDay
from app.models.permission import (
    AuthSession,
    OrganizationState,
    RoleMappingRule,
    UserRole,
)
from app.models.user import User
from app.schemas.expense import ExpenseRuleInput
from app.schemas.permission import Principal, Role
from app.services.auth import access_token, digest

ATT = dict(
    timezone="Asia/Shanghai",
    start_time="09:00",
    end_time="18:00",
    late_threshold_minutes=15,
    early_leave_threshold_minutes=15,
)
RULE_TABLES = [
    m.__table__
    for m in (
        BusinessRule,
        RuleVersionHistory,
        RuleDocBinding,
        RuleChangeRequest,
        RuleOutbox,
    )
]


def rule_body(data, version=0):
    return dict(
        expected_version=version,
        rule_data=copy.deepcopy(data),
        change_summary="HTTP 隔离验收",
        bindings=[],
    )


def approval():
    nodes = [
        dict(id="m", stage="manager", actor={"kind": "direct_manager"}),
        dict(id="f", stage="finance", actor={"kind": "user", "user_id": 5}),
        dict(id="c", stage="cashier", actor={"kind": "user", "user_id": 6}),
    ]
    return {
        "chains": [
            dict(
                id=i,
                request_type="reimbursement",
                min_amount=lo,
                max_amount=hi,
                nodes=copy.deepcopy(nodes),
                edges=[{"from": "m", "to": "f"}, {"from": "f", "to": "c"}],
            )
            for i, lo, hi in [("small", "0", "1000"), ("large", "1000", None)]
        ],
        "default_chain": None,
    }


def reimbursement():
    rules = []
    for i, (field, op, val) in enumerate(
        [
            ("buyer_name", "eq", "$company_name"),
            ("buyer_tax_id", "eq", "$company_tax_id"),
            ("amount", "lte", "$max_amount"),
        ],
        1,
    ):
        r = ExpenseRuleInput(
            rule_name=field,
            rule_category="all",
            expense_type="all",
            max_amount="100",
            effective_date="2026-01-01",
            conflict_group=field,
            condition_json={"field": field, "operator": op, "value": val},
            error_message="不符合测试标准",
        ).model_dump(mode="json")
        rules.append({"id": i, **r})
    return {
        "policy": {
            "company_name": "HTTP测试公司",
            "company_tax_id": "HTTP-TEST-ONLY",
            "position_map": {"P5": "employee"},
        },
        "rules": rules,
    }


class Run:
    def __init__(self):
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
        self.name = "rules_http_" + self.run_id
        self.output = ROOT / "artifacts" / self.name
        self.output.mkdir(parents=True)
        self.records, self.processes, self.logs, self.tokens, self.refresh = (
            [],
            [],
            [],
            {},
            {},
        )
        self.settings = get_settings()
        self.container = "codex-" + self.name.replace("_", "-")
        self.factory = self.engine = None
        self.env = os.environ.copy()

    def record(self, name, ok, **evidence):
        item = {"name": name, "passed": bool(ok), **evidence}
        self.records.append(item)
        print(("PASS " if ok else "FAIL ") + name, flush=True)
        (self.output / "results.json").write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2, default=str)
        )
        if not ok:
            raise AssertionError(name + ": " + str(evidence)[:1400])

    async def request(
        self, name, method, path, body=None, status=200, actor=1, key=None, base=None
    ):
        headers = {"Authorization": "Bearer " + self.tokens[actor]} if actor else {}
        if key:
            headers["Idempotency-Key"] = key
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.request(
                method,
                (base or self.base) + "/api/v1" + path,
                json=body,
                headers=headers,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:1500]}
        safe = copy.deepcopy(payload)
        if isinstance(safe, dict):
            for k in ("token", "access_token", "refresh_token"):
                if k in safe:
                    safe[k] = "[REDACTED]"
        self.record(
            name,
            response.status_code == status,
            method=method,
            path=path,
            status=response.status_code,
            expected_status=status,
            elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
            response=safe,
        )
        return payload

    async def setup(self, apply):
        source = make_url(self.settings.database_url)
        if source.host not in {"localhost", "127.0.0.1"}:
            raise RuntimeError("脚本仅支持本机数据库")
        admin = create_async_engine(source, hide_parameters=True)
        async with admin.begin() as conn:
            before = set((await conn.execute(text("SHOW TABLES"))).scalars())
            if apply:
                for table in RULE_TABLES:
                    await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
            after = set((await conn.execute(text("SHOW TABLES"))).scalars())
            self.record(
                "当前库缺失规则表迁移",
                all(t.name in after for t in RULE_TABLES),
                created_tables=sorted(after - before),
            )
            await conn.execute(
                text(
                    f"CREATE DATABASE `{self.name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
            for table in sorted(after):
                if not table.replace("_", "").isalnum():
                    raise ValueError("非法表名")
                await conn.execute(
                    text(
                        f"CREATE TABLE `{self.name}`.`{table}` LIKE `{source.database}`.`{table}`"
                    )
                )
        await admin.dispose()
        self.url = source.set(database=self.name).render_as_string(hide_password=False)
        self.engine = create_async_engine(
            self.url,
            hide_parameters=True,
            connect_args={"init_command": "SET time_zone = '+00:00'"},
        )
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        with socket.socket() as redis_socket:
            redis_socket.bind(("127.0.0.1", 0))
            redis_port = redis_socket.getsockname()[1]
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.container,
                "-p",
                f"127.0.0.1:{redis_port}:6379",
                "redis:latest",
            ],
            check=True,
            capture_output=True,
        )
        port = (
            subprocess.check_output(
                ["docker", "port", self.container, "6379/tcp"], text=True
            )
            .strip()
            .rsplit(":", 1)[1]
        )
        self.env.update(
            DATABASE_URL=self.url,
            REDIS_URL=f"redis://127.0.0.1:{port}/0",
            RULES_ENVIRONMENT=self.name,
            RULES_WORKER_ENABLED="true",
            RULES_FEISHU_ENABLED="false",
            BUSINESS_RULES_ENABLED="false",
            EXPENSE_MOCK_ENABLED="true",
            PYTHONPATH=str(ROOT),
            PYTHONUNBUFFERED="1",
            RULES_SENSITIVE_TYPES="[]",
            RULES_HR_OPEN_IDS="[]",
            PERMISSION_ALERT_OPEN_IDS="[]",
        )
        (self.output / "environment.json").write_text(
            json.dumps(
                {
                    "database": self.name,
                    "source_database": source.database,
                    "mysql_host": source.host,
                    "mysql_port": source.port,
                    "redis_container": self.container,
                    "redis_port": int(port),
                    "scope": "原 app/router/鉴权 + 验收生命周期；实际 HTTP、MySQL、Redis、财务模拟 MCP；不启动飞书发送任务",
                    "schema_note": "CREATE TABLE LIKE 复制当前表结构及索引，不复制外键；未复制业务数据",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        async with self.factory() as db, db.begin():
            db.add(
                Department(
                    department_id=1,
                    feishu_department_id="http-test",
                    name="HTTP验收部门",
                    manager_user_id=4,
                )
            )
            await db.flush()
            snapshot = {
                "users": {},
                "leaders": {},
                "departments": {
                    "http-test": {"name": "HTTP验收部门", "parent_id": "0"}
                },
            }
            for uid in range(1, 8):
                oid = f"http-test-{uid}"
                role = (
                    Role.HR_ADMIN
                    if uid in {1, 2}
                    else Role.MANAGER
                    if uid == 4
                    else Role.EMPLOYEE
                )
                db.add(
                    User(
                        user_id=uid,
                        feishu_open_id=oid,
                        name=f"HTTP测试用户{uid}",
                        department_id=1,
                        role="admin" if uid == 1 else "员工",
                        position_level="P5",
                        phone="",
                        email="",
                        status="活动",
                    )
                )
                p = Principal(
                    user_id=uid,
                    open_id=oid,
                    role=role,
                    department_id=1,
                    version=1,
                    managed_user_ids=(3,) if uid == 4 else (),
                )
                db.add(
                    UserRole(
                        user_id=uid,
                        feishu_open_id=oid,
                        role=role.value,
                        role_source="manual",
                        version=1,
                        profile=p.model_dump(mode="json"),
                    )
                )
                db.add(
                    RoleMappingRule(
                        kind="user", subject=oid, role=role.value, enabled=True
                    )
                )
                sid, secret = uuid4().hex, secrets.token_urlsafe(32)
                db.add(
                    AuthSession(
                        id=sid,
                        user_id=uid,
                        refresh_hash=digest(secret),
                        expires_at=int(time.time()) + 3600,
                        revoked=False,
                    )
                )
                self.tokens[uid] = access_token(p, sid)
                self.refresh[uid] = sid + "." + secret
                snapshot["users"][oid] = {
                    "feishu_user_id": f"http-user-{uid}",
                    "departments": ["http-test"],
                    "active": True,
                    "leader": "http-test-4" if uid == 3 else "",
                }
            db.add(
                OrganizationState(
                    id=1,
                    version=1,
                    dirty=False,
                    synced_at=int(time.time()),
                    snapshot=snapshot,
                )
            )
            for d in range(1, 32):
                day = date(2026, 8, d)
                db.add(WorkCalendarDay(day=day, is_workday=d != 5, remark="HTTP测试"))
                cin = (
                    datetime(2026, 8, d, 1, 20)
                    if d in (1, 2)
                    else datetime(2026, 8, d, 1)
                )
                cout = (
                    datetime(2026, 8, d, 9, 30) if d == 2 else datetime(2026, 8, d, 10)
                )
                if d in (3, 4, 5, 6):
                    cout = None
                if d in (4, 5, 6):
                    cin = None
                db.add(
                    Attendance(
                        user_id=3,
                        date=day,
                        clock_in_time=cin,
                        clock_out_time=cout,
                        status="normal",
                        work_hours=8,
                        remark="HTTP原始样本",
                    )
                )
            db.add(
                LeaveRequest(
                    user_id=3,
                    leave_type="annual",
                    start_time=datetime(2026, 8, 6, 1),
                    end_time=datetime(2026, 8, 6, 10),
                    duration=1,
                    status="approved",
                    reason="HTTP整日假",
                    approver_id=4,
                )
            )
            for i, amount in enumerate(
                ["100.00", "100.01", "50.00", "60.00", "70.00", "80.00", "90.00"], 1
            ):
                db.add(
                    Invoice(
                        id=i,
                        image_url="http-test://invoice/" + str(i),
                        verified=True,
                        verified_by=3,
                        ocr_result_json={
                            "_meta": {"user_id": 3},
                            "_expense": {"expense_type": "other", "city": "北京"},
                            "invoice_number": f"HTTP{i:08d}",
                            "invoice_code": "HTTP20260922",
                            "invoice_date": "2026-09-21",
                            "total_amount": amount,
                            "buyer_name": "HTTP测试公司",
                            "buyer_tax_id": "HTTP-TEST-ONLY",
                        },
                    )
                )
        async with self.engine.connect() as c:
            self.raw_before = [
                tuple(r)
                for r in await c.execute(text("SELECT * FROM t_attendance ORDER BY id"))
            ]
        self.record("隔离身份和业务数据准备", True, users=7, attendance=31, invoices=7)

    async def start(self, enabled=False):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**self.env, "BUSINESS_RULES_ENABLED": str(enabled).lower()}
        log = (self.output / f"server-{port}.log").open("w")
        self.logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.rules_http_server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(timeout=2) as c:
            for _ in range(90):
                if process.poll() is not None:
                    raise RuntimeError(f"服务启动失败，见 server-{port}.log")
                try:
                    if (await c.get(base + "/openapi.json")).status_code == 200:
                        return base
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError("HTTP 服务启动超时")

    async def publish(self, kind, data):
        current = await self.request("读取发布基准 " + kind, "GET", "/rules/" + kind)
        return await self.request(
            "发布 " + kind,
            "PUT",
            "/rules/" + kind,
            rule_body(data, current["version"]),
            key=uuid4().hex,
        )

    async def rules(self):
        await self.request("无凭据拒绝", "GET", "/rules", status=401, actor=None)
        await self.request("普通员工不能管理规则", "GET", "/rules", status=403, actor=3)
        await self.request("主管不能管理规则", "GET", "/rules", status=403, actor=4)
        await self.request("真实会话角色", "GET", "/auth/role")
        for kind, data in [
            ("attendance", ATT),
            ("approval", approval()),
            ("reimbursement", reimbursement()),
        ]:
            await self.request(
                "首版前读取 Schema " + kind, "GET", f"/rules/{kind}/schema"
            )
            await self.request(
                "首版不存在 " + kind, "GET", f"/rules/{kind}", status=404
            )
            result = await self.request(
                "首版 HTTP 发布 " + kind,
                "PUT",
                f"/rules/{kind}",
                rule_body(data),
                key="seed-" + kind,
            )
            self.record(kind + " 首版 v1", result["version"] == 1)
        await self.request(
            "发布相同幂等键重试",
            "PUT",
            "/rules/attendance",
            rule_body(ATT),
            key="seed-attendance",
        )
        await self.request(
            "同幂等键换内容拒绝",
            "PUT",
            "/rules/attendance",
            rule_body({**ATT, "late_threshold_minutes": 20}),
            key="seed-attendance",
            status=409,
        )
        await self.request(
            "过期基准拒绝",
            "PUT",
            "/rules/attendance",
            rule_body(ATT),
            key="stale",
            status=409,
        )
        for name, change in [
            ("非法时间", {"start_time": "25:00"}),
            ("未知字段", {"unexpected": 1}),
            ("负数宽限", {"late_threshold_minutes": -1}),
            ("错误类型", {"late_threshold_minutes": "15"}),
        ]:
            await self.request(
                name,
                "POST",
                "/rules/attendance/preview",
                rule_body({**ATT, **change}, 1),
                status=422,
            )
        sample = {
            "work_date": "2026-08-01",
            "clock_in_time": "2026-08-01T09:20:00+08:00",
            "clock_out_time": "2026-08-01T18:00:00+08:00",
            "now": "2026-09-22T12:00:00+08:00",
            "is_workday": True,
        }
        prev = await self.request(
            "新旧考勤试算",
            "POST",
            "/rules/attendance/preview",
            {
                **rule_body({**ATT, "late_threshold_minutes": 30}, 1),
                "samples": [sample],
            },
        )
        self.record(
            "试算迟到变正常",
            prev["samples"][0]["before"]["issues"] == ["late"]
            and prev["samples"][0]["after"]["issues"] == [],
        )
        current = await self.request("预览后版本不变", "GET", "/rules/attendance")
        self.record("预览不发布", current["version"] == 1)
        await self.publish("attendance", {**ATT, "late_threshold_minutes": 30})
        old = await self.request("历史快照", "GET", "/rules/attendance/versions/1")
        self.record("历史不可变", old["rule_data"] == ATT)
        await self.request("字段差异", "GET", "/rules/attendance/diff?from=1&to=2")
        await self.request(
            "历史分页", "GET", "/rules/attendance/versions?offset=0&limit=1"
        )
        cand = await self.request(
            "回滚候选",
            "POST",
            "/rules/attendance/change-requests",
            {
                "expected_version": 2,
                "rollback_version": 1,
                "change_summary": "HTTP回滚",
            },
            key="rollback",
        )
        confirm = {k: cand[k] for k in ("change_id", "token")}
        await self.request(
            "他人确认拒绝",
            "POST",
            "/rules/attendance/rollback",
            confirm,
            status=403,
            actor=2,
        )
        r = await self.request(
            "回滚确认", "POST", "/rules/attendance/rollback", confirm
        )
        self.record("回滚递增为 v3", r["version"] == 3)
        await self.request(
            "重复确认幂等", "POST", "/rules/attendance/rollback", confirm
        )
        r = await self.publish("attendance", ATT)
        self.record("相同配置不增版", r["version"] == 3 and r["unchanged"])
        data = approval()
        data["chains"][0]["max_amount"] = "1001"
        await self.request(
            "审批金额区间重叠拒绝",
            "POST",
            "/rules/approval/preview",
            rule_body(data, 1),
            status=422,
        )
        data = approval()
        data["chains"][0]["edges"].append({"from": "c", "to": "m"})
        await self.request(
            "审批环路拒绝",
            "POST",
            "/rules/approval/preview",
            rule_body(data, 1),
            status=422,
        )
        data = approval()
        data["chains"][0]["nodes"][1]["actor"]["user_id"] = 999999
        await self.request(
            "无效审批人拒绝",
            "PUT",
            "/rules/approval",
            rule_body(data, 1),
            key="bad-actor",
            status=422,
        )
        p = await self.request(
            "审批金额边界",
            "POST",
            "/rules/approval/preview",
            {
                **rule_body(approval(), 1),
                "samples": [
                    {"request_type": "reimbursement", "amount": a}
                    for a in ("999.99", "1000")
                ],
            },
        )
        self.record(
            "左闭右开路线",
            [s["after"]["route_id"] for s in p["samples"]] == ["small", "large"],
        )
        data = reimbursement()
        data["rules"].append({**data["rules"][-1], "id": 4, "rule_name": "conflict"})
        await self.request(
            "报销同优先级冲突拒绝",
            "POST",
            "/rules/reimbursement/preview",
            rule_body(data, 1),
            status=422,
        )
        await self.request(
            "未启用飞书不能伪确认文档",
            "PUT",
            "/rules/attendance/bindings",
            {
                "expected_version": 3,
                "change_summary": "绑定拒绝测试",
                "bindings": [{"doc_id": "test-doc", "acknowledged_revision": 1}],
            },
            key="binding",
            status=503,
        )

    async def business(self):
        self.base = await self.start(True)
        base = "/attendance/punch-records?month=2026-08&limit=100"
        r = await self.request("历史月真实考勤查询", "GET", base, actor=3)
        items = {i["date"]: i for i in r["punch_records"]["items"]}
        self.record(
            "异常集合及缺卡休息请假",
            items["2026-08-02"]["issues"] == ["late", "early_leave"]
            and [items[f"2026-08-{d:02}"]["status"] for d in (3, 4, 5, 6)]
            == ["needs_review", "absent", "rest", "leave"],
        )
        stat = await self.request(
            "统计与异常一致", "GET", "/attendance/late-stats?month=2026-08", actor=3
        )
        self.record(
            "迟到2次早退1次",
            stat["late_stats"]["late_count"] == 2
            and stat["late_stats"]["early_leave_count"] == 1,
        )
        a = await self.request(
            "重算后筛选分页1", "GET", base + "&status_filter=late&offset=0", actor=3
        )
        self.record("迟到筛选2条", len(a["punch_records"]["items"]) == 2)
        a = await self.request(
            "重算后分页2",
            "GET",
            "/attendance/punch-records?month=2026-08&status_filter=late&limit=1&offset=1",
            actor=3,
        )
        self.record(
            "偏移分页正确",
            len(a["punch_records"]["items"]) == 1
            and not a["punch_records"]["has_more"],
        )
        await self.request(
            "员工禁止查他人", "GET", base + "&user_id=1", actor=3, status=403
        )
        await self.publish("attendance", {**ATT, "late_threshold_minutes": 30})
        a = await self.request("规则发布后历史重算", "GET", base, actor=3)
        self.record(
            "历史迟到消失",
            [i for i in a["punch_records"]["items"] if "late" in i["issues"]] == [],
        )
        stat = await self.request(
            "重算后统计", "GET", "/attendance/late-stats?month=2026-08", actor=3
        )
        self.record("新统计迟到0", stat["late_stats"]["late_count"] == 0)
        async with self.engine.connect() as c:
            rows = [
                tuple(r)
                for r in await c.execute(text("SELECT * FROM t_attendance ORDER BY id"))
            ]
        self.record("原始打卡状态工时逐字段未变", rows == self.raw_before)
        for iid in (1, 2):
            r = await self.request(
                "报销额度边界 " + str(iid),
                "POST",
                "/expense/rule/check",
                {"invoice_ids": [iid]},
                actor=3,
            )
            self.record(
                "额度边界判定 " + str(iid),
                r["data"]["all_passed"] == (iid == 1),
                response=r,
            )
        draft = await self.request(
            "创建报销草稿", "POST", "/expense/prepare", {"invoice_ids": [3]}, actor=3
        )
        self.expense_id = (
            draft["data"]["expense_id"]
            if "expense_id" in draft["data"]
            else draft["data"]["id"]
        )
        data = reimbursement()
        data["rules"][-1]["max_amount"] = "110"
        await self.publish("reimbursement", data)
        await self.request(
            "草稿规则变化阻止旧确认",
            "POST",
            "/expense/submit",
            {"expense_id": self.expense_id},
            actor=3,
            status=409,
        )
        await self.request(
            "取消旧草稿", "POST", f"/expense/{self.expense_id}/cancel", actor=3
        )
        draft = await self.request(
            "按最新规则重建草稿",
            "POST",
            "/expense/prepare",
            {"invoice_ids": [3]},
            actor=3,
        )
        self.expense_id = draft["data"].get("expense_id", draft["data"].get("id"))
        await self.request(
            "提交报销",
            "POST",
            "/expense/submit",
            {"expense_id": self.expense_id},
            actor=3,
        )
        await self.await_expense("submitted")
        await self.request(
            "HTTP 财务 MCP 查询", "GET", f"/expense/{self.expense_id}/finance", actor=3
        )
        data = approval()
        for chain in data["chains"]:
            chain["nodes"][1]["actor"]["user_id"] = 7
        await self.publish("approval", data)
        path = f"/admin/expense/{self.expense_id}/mock-approval"
        await self.request(
            "在途单拒绝换新规则审批人",
            "POST",
            path,
            {
                "request_id": "wrong-next",
                "to_status": "manager_approved",
                "next_approver_id": 7,
            },
            status=409,
        )
        await self.request(
            "固定主管转原财务",
            "POST",
            path,
            {
                "request_id": "manager",
                "to_status": "manager_approved",
                "next_approver_id": 5,
            },
        )
        await self.await_expense("manager_approved")
        await self.request(
            "审批事件重复幂等",
            "POST",
            path,
            {
                "request_id": "manager",
                "to_status": "manager_approved",
                "next_approver_id": 5,
            },
        )
        await self.request(
            "财务转出纳",
            "POST",
            path,
            {
                "request_id": "finance",
                "to_status": "finance_approved",
                "next_approver_id": 6,
            },
        )
        await self.await_expense("finance_approved")
        await self.request(
            "模拟打款",
            "POST",
            path,
            {
                "request_id": "cashier",
                "to_status": "paid",
                "paid_amount": "50.00",
                "paid_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        await self.await_expense("paid")

    async def await_expense(self, status):
        path = f"/expense/{self.expense_id}/approval-status"
        async with httpx.AsyncClient(timeout=20) as c:
            for _ in range(35):
                r = await c.get(
                    self.base + "/api/v1" + path,
                    headers={"Authorization": "Bearer " + self.tokens[3]},
                )
                value = r.json()
                if r.status_code == 200 and value["data"]["current_status"] == status:
                    self.record("异步财务状态 " + status, True, response=value)
                    return
                await asyncio.sleep(1)
        self.record("异步财务状态 " + status, False, response=value)

    async def advanced(self):
        current = await self.request("并发基准", "GET", "/rules/attendance")
        async with httpx.AsyncClient(timeout=25) as c:
            responses = await asyncio.gather(
                *[
                    c.put(
                        self.base + "/api/v1/rules/attendance",
                        json=rule_body(
                            {**ATT, "late_threshold_minutes": n}, current["version"]
                        ),
                        headers={
                            "Authorization": "Bearer " + self.tokens[actor],
                            "Idempotency-Key": "concurrent-" + str(actor),
                        },
                    )
                    for actor, n in [(1, 21), (2, 22)]
                ]
            )
        self.record(
            "两个 HR 同版本并发只有一次成功",
            sorted(r.status_code for r in responses) == [200, 409],
            responses=[{"status": r.status_code, "body": r.json()} for r in responses],
        )
        await self.request(
            "兼容报销接口不能绕过权限",
            "PUT",
            "/admin/expense-policy",
            reimbursement()["policy"],
            actor=3,
            status=403,
        )

        async def candidate(label, actor=1):
            cur = await self.request(
                label + "基准", "GET", "/rules/attendance", actor=actor
            )
            update_body = rule_body(
                {**ATT, "late_threshold_minutes": 31}, cur["version"]
            )
            body = {
                "expected_version": cur["version"],
                "update": update_body,
                "change_summary": label,
            }
            result = await self.request(
                label,
                "POST",
                "/rules/attendance/change-requests",
                body,
                key=uuid4().hex,
                actor=actor,
            )
            return {k: result[k] for k in ("change_id", "token")}

        cand = await candidate("普通修改候选")
        await self.request("普通修改确认", "POST", "/rules/attendance/confirm", cand)
        cand = await candidate("过期候选")
        async with self.factory() as db, db.begin():
            await db.execute(
                update(RuleChangeRequest)
                .where(RuleChangeRequest.id == cand["change_id"])
                .values(
                    expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(minutes=1)
                )
            )
        await self.request(
            "过期确认被拒绝", "POST", "/rules/attendance/confirm", cand, status=409
        )
        cand = await candidate("陈旧候选")
        await self.publish("attendance", {**ATT, "late_threshold_minutes": 32})
        await self.request(
            "基准已改变旧候选拒绝",
            "POST",
            "/rules/attendance/confirm",
            cand,
            status=409,
        )
        cand = await candidate("撤权候选", actor=2)
        async with self.factory() as db, db.begin():
            await db.execute(
                update(RoleMappingRule)
                .where(RoleMappingRule.subject == "http-test-2")
                .values(role=Role.EMPLOYEE.value)
            )
            state = await db.get(OrganizationState, 1)
            state.version += 1
        await self.request(
            "HR撤权后旧确认失效",
            "POST",
            "/rules/attendance/confirm",
            cand,
            actor=2,
            status=403,
        )

        # 用独立库触发器制造 Outbox 写失败，HTTP 500 后检查事务四部分均未提交。
        async def counts():
            async with self.engine.connect() as c:
                return {
                    t: int(
                        (await c.execute(text(f"SELECT COUNT(*) FROM `{t}`"))).scalar()
                    )
                    for t in [
                        "rule_version_history",
                        "rule_outbox",
                        "audit_log",
                        "rule_change_request",
                    ]
                }

        cur = await self.request("事务故障基准", "GET", "/rules/attendance")
        before = await counts()
        async with self.engine.begin() as c:
            await c.execute(
                text(
                    "CREATE TRIGGER http_outbox_failure BEFORE INSERT ON rule_outbox FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='HTTP acceptance injected outbox failure'"
                )
            )
        try:
            await self.request(
                "Outbox写入故障HTTP失败",
                "PUT",
                "/rules/attendance",
                rule_body({**ATT, "late_threshold_minutes": 33}, cur["version"]),
                key="transaction-fault",
                status=500,
            )
            self.record("失败发布历史审计候选事件全部回滚", before == await counts())
        finally:
            async with self.engine.begin() as c:
                await c.execute(text("DROP TRIGGER http_outbox_failure"))
        after = await self.request("事务故障后当前版本", "GET", "/rules/attendance")
        self.record("失败发布当前版本不变", after["version"] == cur["version"])

        # 三个独立 Uvicorn 进程；由真实 HTTP 健康接口读取各进程缓存版本。
        peers = [self.base, await self.start(True), await self.start(True)]
        for base in peers:
            await self.request("进程启动对账", "GET", "/rules/_health", base=base)
        r = await self.publish("attendance", {**ATT, "late_threshold_minutes": 34})
        start = time.perf_counter()
        versions = []
        async with httpx.AsyncClient(timeout=15) as c:
            for _ in range(40):
                results = await asyncio.gather(
                    *[
                        c.get(
                            base + "/api/v1/rules/_health",
                            headers={"Authorization": "Bearer " + self.tokens[1]},
                        )
                        for base in peers
                    ]
                )
                versions = [
                    x.json().get("worker_versions", {}).get("attendance")
                    for x in results
                ]
                if versions == [r["version"]] * 3:
                    break
                await asyncio.sleep(0.2)
        elapsed = time.perf_counter() - start
        self.record(
            "3个HTTP进程10秒内热更新",
            versions == [r["version"]] * 3 and elapsed < 10,
            elapsed_seconds=round(elapsed, 3),
            versions=versions,
        )
        self.record(
            "健康HTTP考勤低于1.5秒",
            all(
                x["elapsed_ms"] < 1500
                for x in self.records
                if x.get("path", "").startswith("/attendance/")
                and x.get("status") == 200
            ),
            max_elapsed_ms=max(
                x["elapsed_ms"]
                for x in self.records
                if x.get("path", "").startswith("/attendance/")
                and x.get("status") == 200
            ),
        )

        subprocess.run(
            ["docker", "stop", self.container], check=True, capture_output=True
        )
        try:
            # 停 Redis 不停共享 MySQL；请求与 Worker 靠主库恢复。
            r = await self.publish("attendance", {**ATT, "late_threshold_minutes": 35})
            await asyncio.sleep(6)
            for base in peers:
                result = await self.request(
                    "Redis断开HTTP考勤回源",
                    "GET",
                    "/attendance/punch-records?month=2026-08&limit=1",
                    actor=3,
                    base=base,
                )
                self.record(
                    "漏通知后主库对账追上版本", result["rule_version"] == r["version"]
                )
        finally:
            subprocess.run(
                ["docker", "start", self.container], check=True, capture_output=True
            )
        await asyncio.sleep(6)
        async with httpx.AsyncClient(timeout=10) as client:
            for base in peers:
                probes = []
                for _ in range(10):
                    response = await client.get(
                        base + "/api/v1/_acceptance/redis",
                        headers={"Authorization": "Bearer " + self.tokens[1]},
                    )
                    probes.append(
                        {"status": response.status_code, "body": response.json()}
                    )
                    if response.status_code == 200:
                        break
                    await asyncio.sleep(1)
                self.record(
                    "应用Redis实际连接恢复", response.status_code == 200, probes=probes
                )
        health = await self.request("Redis恢复健康检查", "GET", "/rules/_health")
        self.record(
            "恢复后缓存新鲜",
            health["fresh"] and health["worker_versions"]["attendance"] == r["version"],
        )
        # 本地对账恢复不等于可靠事件已投递，另等 Outbox 退避重试完成。
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=10) as client:
            while any(e["kind"] == "cache" for e in health["pending_events"]):
                if time.perf_counter() - started > 90:
                    break
                await asyncio.sleep(1)
                response = await client.get(
                    self.base + "/api/v1/rules/_health",
                    headers={"Authorization": "Bearer " + self.tokens[1]},
                )
                response.raise_for_status()
                health = response.json()
        self.record(
            "Redis恢复后Outbox缓存事件全部重试成功",
            not any(e["kind"] == "cache" for e in health["pending_events"]),
            recovery_wait_seconds=round(time.perf_counter() - started, 3),
            pending_events=health["pending_events"],
        )

        # 会话续期、旧刷新凭据重放、退出登录均通过真实 HTTP。
        response = await self.request(
            "HTTP刷新会话",
            "POST",
            "/auth/refresh",
            {"refresh_token": self.refresh[7]},
            actor=None,
        )
        self.tokens[7] = response["access_token"]
        await self.request("刷新后的会话有效", "GET", "/auth/role", actor=7)
        await self.request(
            "旧刷新凭据重放拒绝",
            "POST",
            "/auth/refresh",
            {"refresh_token": self.refresh[7]},
            actor=None,
            status=401,
        )
        await self.request(
            "重放后整个会话撤销", "GET", "/auth/role", actor=7, status=401
        )
        await self.request("HTTP退出登录", "POST", "/auth/logout", actor=6)
        await self.request("退出后Token失效", "GET", "/auth/role", actor=6, status=401)

    async def boundary_matrix(self):
        cur = await self.request("边界试算基准", "GET", "/rules/attendance")
        sample = {
            "work_date": "2026-08-01",
            "clock_in_time": "2026-08-01T09:00:00+08:00",
            "clock_out_time": "2026-08-01T18:00:00+08:00",
            "now": "2026-09-22T12:00:00+08:00",
            "is_workday": True,
        }
        cases = [
            (
                "恰好宽限无异常",
                {
                    "clock_in_time": "2026-08-01T09:15:00+08:00",
                    "clock_out_time": "2026-08-01T17:45:00+08:00",
                },
                "normal",
                [],
            ),
            (
                "超过宽限双异常",
                {
                    "clock_in_time": "2026-08-01T09:16:00+08:00",
                    "clock_out_time": "2026-08-01T17:44:00+08:00",
                },
                "late",
                ["late", "early_leave"],
            ),
            (
                "未下班不判缺勤",
                {
                    "clock_in_time": None,
                    "clock_out_time": None,
                    "now": "2026-08-01T12:00:00+08:00",
                },
                "pending",
                [],
            ),
            (
                "日历缺失待核实",
                {"is_workday": None},
                "needs_review",
                ["calendar_missing"],
            ),
            (
                "部分请假待核实",
                {"partial_leave": True},
                "needs_review",
                ["partial_leave_review"],
            ),
            (
                "倒置打卡待核实",
                {"clock_in_time": "2026-08-01T19:00:00+08:00"},
                "needs_review",
                ["invalid_punch_order"],
            ),
        ]
        response = await self.request(
            "考勤边界批量HTTP试算",
            "POST",
            "/rules/attendance/preview",
            {
                **rule_body(ATT, cur["version"]),
                "samples": [{**sample, **overrides} for _, overrides, _, _ in cases],
            },
        )
        for (label, _, status, issues), r in zip(cases, response["samples"]):
            self.record(
                label, r["after"]["status"] == status and r["after"]["issues"] == issues
            )
        response = await self.request(
            "跨午夜HTTP试算",
            "POST",
            "/rules/attendance/preview",
            {
                **rule_body(
                    {**ATT, "start_time": "22:00", "end_time": "06:00"}, cur["version"]
                ),
                "samples": [
                    {
                        **sample,
                        "clock_in_time": "2026-08-01T22:00:00+08:00",
                        "clock_out_time": "2026-08-02T06:00:00+08:00",
                    }
                ],
            },
        )
        self.record("跨午夜正常", response["samples"][0]["after"]["status"] == "normal")
        await self.request(
            "必须携带幂等键",
            "PUT",
            "/rules/attendance",
            rule_body(ATT, cur["version"]),
            status=422,
        )
        await self.request(
            "拒绝客户端指定操作者",
            "PUT",
            "/rules/attendance",
            {**rule_body(ATT, cur["version"]), "user_id": 1},
            key="forged",
            status=422,
        )
        await self.request(
            "不存在历史版本", "GET", "/rules/attendance/versions/99999", status=404
        )
        await self.request("规则类型白名单", "GET", "/rules/salary", status=422)
        await self.request(
            "配置256KiB限制",
            "POST",
            "/rules/attendance/preview",
            rule_body({**ATT, "large": "x" * (256 * 1024)}, cur["version"]),
            status=413,
        )

        cur = await self.request("审批无匹配基准", "GET", "/rules/approval")
        data = cur["rule_data"]
        data["chains"] = data["chains"][:1]
        r = await self.request(
            "审批缺路线试算",
            "POST",
            "/rules/approval/preview",
            {
                **rule_body(data, cur["version"]),
                "samples": [{"request_type": "reimbursement", "amount": "1000"}],
            },
        )
        self.record(
            "缺路线明确配置错误",
            r["samples"][0]["after"]["error"] == "RULE_NOT_CONFIGURED",
        )
        data = copy.deepcopy(cur["rule_data"])
        data["chains"][0]["min_amount"] = "0.001"
        await self.request(
            "金额精度超过两位拒绝",
            "POST",
            "/rules/approval/preview",
            rule_body(data, cur["version"]),
            status=422,
        )
        data = copy.deepcopy(cur["rule_data"])
        data["chains"][0]["nodes"].append(
            {
                "id": "extra",
                "stage": "director",
                "actor": {"kind": "user", "user_id": 1},
            }
        )
        await self.request(
            "第四审批阶段拒绝",
            "POST",
            "/rules/approval/preview",
            rule_body(data, cur["version"]),
            status=422,
        )

        cur = await self.request("报销边界基准", "GET", "/rules/reimbursement")
        data = copy.deepcopy(cur["rule_data"])
        data["rules"][-1]["condition_json"]["value"] = "$remote_secret"
        await self.request(
            "报销条件引用白名单",
            "POST",
            "/rules/reimbursement/preview",
            rule_body(data, cur["version"]),
            status=422,
        )
        data = copy.deepcopy(cur["rule_data"])
        condition = data["rules"][-1]["condition_json"]
        for _ in range(10):
            condition = {"not": condition}
        data["rules"][-1]["condition_json"] = condition
        await self.request(
            "条件嵌套深度限制",
            "POST",
            "/rules/reimbursement/preview",
            rule_body(data, cur["version"]),
            status=422,
        )
        data = copy.deepcopy(cur["rule_data"])
        data["rules"][-1]["status"] = "inactive"
        await self.publish("reimbursement", data)
        response = await self.request(
            "费用缺标准不能放行",
            "POST",
            "/expense/rule/check",
            {"invoice_ids": [4]},
            actor=3,
        )
        self.record(
            "缺标准报告阻断",
            not response["data"]["all_passed"] and not response["data"]["can_override"],
        )
        await self.publish("reimbursement", cur["rule_data"])

        # 确认审批规则版本变化也能拦截草稿。
        draft = await self.request(
            "审批版本草稿", "POST", "/expense/prepare", {"invoice_ids": [4]}, actor=3
        )
        eid = draft["data"]["expense_id"]
        cur = await self.request("审批草稿变更基准", "GET", "/rules/approval")
        data = copy.deepcopy(cur["rule_data"])
        data["chains"][0]["id"] = "small-updated"
        await self.publish("approval", data)
        await self.request(
            "审批版本改变阻止草稿提交",
            "POST",
            "/expense/submit",
            {"expense_id": eid},
            actor=3,
            status=409,
        )
        await self.request(
            "取消审批版本旧草稿", "POST", f"/expense/{eid}/cancel", actor=3
        )
        # 数据库仅改变隔离样本，HTTP 验证执行器对无效人员的实际处理。
        async with self.factory() as db, db.begin():
            await db.execute(
                update(User).where(User.user_id == 7).values(status="不活动")
            )
        try:
            await self.request(
                "停用审批人阻止业务申请",
                "POST",
                "/expense/prepare",
                {"invoice_ids": [4]},
                actor=3,
                status=409,
            )
        finally:
            async with self.factory() as db, db.begin():
                await db.execute(
                    update(User).where(User.user_id == 7).values(status="活动")
                )
        data = copy.deepcopy(cur["rule_data"])
        for chain in data["chains"]:
            chain["nodes"][1]["actor"]["user_id"] = 3
        await self.publish("approval", data)
        await self.request(
            "申请人不能审批自己",
            "POST",
            "/expense/prepare",
            {"invoice_ids": [4]},
            actor=3,
            status=409,
        )
        await self.publish("approval", cur["rule_data"])

        await self.request(
            "普通员工无模拟审批权限",
            "POST",
            f"/admin/expense/{self.expense_id}/mock-approval",
            {"request_id": "forbidden", "to_status": "paid"},
            actor=3,
            status=403,
        )

    async def finish(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
        for p in self.processes:
            try:
                await asyncio.to_thread(p.wait, 20)
            except subprocess.TimeoutExpired:
                p.kill()
                await asyncio.to_thread(p.wait)
        for log in self.logs:
            log.close()
        if self.factory:
            async with self.factory() as db, db.begin():
                await db.execute(update(AuthSession).values(revoked=True))
        if self.engine:
            await self.engine.dispose()
        subprocess.run(["docker", "stop", self.container], capture_output=True)
        count = sum(r["passed"] for r in self.records)
        report = (
            f"# HTTP 验收 {self.run_id}\n\n通过 {count}/{len(self.records)} 项。\n\n"
        )
        report += "- 当前库只补齐缺失规则表；测试数据保留在 " + self.name + "。\n"
        report += "- 真实 TCP HTTP + 原应用路由/鉴权 + MySQL/Redis + 原财务模拟 MCP。\n"
        report += "- 认证采用隔离样本用户和持久化会话，未跳过认证依赖；未执行真实飞书 OAuth 授权。\n"
        report += "- 隔离旧业务表使用 CREATE TABLE LIKE 复制索引和列，不复制外键；本报告不证明旧表外键迁移兼容性。\n"
        report += (
            "- 验收生命周期不启动无关 Worker 和飞书发送；没有模拟 HTTP 业务响应。\n"
        )
        report += "- 测试会话全部撤销，验收 HTTP 进程退出，专用 Redis 容器已停止；库和日志保留。\n"
        report += "- 未测试真实飞书 OAuth 登录、文档提醒、卡片、表格或真实财务；不能据此宣称这些联调通过。\n\n"
        report += "| 检查 | 结果 |\n|---|---|\n" + "".join(
            f"| {r['name']} | {'通过' if r['passed'] else '失败'} |\n"
            for r in self.records
        )
        (self.output / "report.md").write_text(report)
        print("REPORT " + str(self.output / "report.md"), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-missing-rule-tables", action="store_true")
    args = parser.parse_args()
    run = Run()
    try:
        await run.setup(args.apply_missing_rule_tables)
        run.base = await run.start()
        await run.rules()
        await run.business()
        await run.boundary_matrix()
        await run.advanced()
    except Exception as exc:
        run.records.append(
            {
                "name": "验收执行完成",
                "passed": False,
                "error": type(exc).__name__,
                "message": str(exc)[:1800],
            }
        )
        (run.output / "results.json").write_text(
            json.dumps(run.records, ensure_ascii=False, indent=2, default=str)
        )
        raise
    finally:
        await run.finish()


if __name__ == "__main__":
    asyncio.run(main())
