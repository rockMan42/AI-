"""只读本地接口基准；不输出身份凭证或考勤正文，不发送飞书消息。"""
import argparse
import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import time

import httpx
import jwt
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import init_db, create_session, close_db
from app.models.user import User


async def main(args):
    settings = get_settings()
    await init_db(settings)
    try:
        async with create_session() as db:
            user = await db.scalar(select(User).where(User.user_id == args.user_id))
            if user is None:
                raise RuntimeError("测试用户不存在")
            now = int(time.time())
            token = jwt.encode({"sub": user.feishu_open_id, "iat": now,
                                "exp": now + 1800, "iss": settings.auth_jwt_issuer,
                                "aud": settings.auth_jwt_audience},
                               settings.auth_jwt_secret.get_secret_value(), algorithm="HS256")
    finally:
        await close_db()
    report = {"samples_per_scenario": args.samples, "results": []}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=5,
                                headers={"Authorization": f"Bearer {token}"}) as client:
        for endpoint, params in [("punch-records", {"month": "2025-08"}),
                                 ("late-stats", {"month": "2025-08"}),
                                 ("late-stats", {}),
                                 ("leave-balance", {"year": 2025})]:
            for concurrency in (1, 5, 10):
                await client.get(f"/api/v1/attendance/{endpoint}", params=params)
                semaphore = asyncio.Semaphore(concurrency)
                async def request():
                    async with semaphore:
                        started = time.perf_counter()
                        status, cached = 0, None
                        try:
                            response = await client.get(f"/api/v1/attendance/{endpoint}", params=params)
                            status = response.status_code
                            payload = response.json()
                            if status == 200 and isinstance(payload, dict):
                                cached = payload.get("late_stats", {}).get("cached")
                            elif status == 200:
                                status = -1
                        except (httpx.HTTPError, ValueError):
                            status = 0
                        return (time.perf_counter() - started) * 1000, status, cached
                rows = await asyncio.gather(*(request() for _ in range(args.samples)))
                successful = sorted(ms for ms, code, _ in rows if code == 200)
                def percentile(p):
                    return round(successful[max(0, math.ceil(len(successful)*p)-1)], 2) if successful else None
                report["results"].append({"endpoint": endpoint, "params": params,
                    "concurrency": concurrency, "status_counts": dict(Counter(str(code) for _,code,_ in rows)),
                    "error_rate": sum(code != 200 for _,code,_ in rows) / len(rows),
                    "success_p50_ms": percentile(.5), "success_p95_ms": percentile(.95),
                    "max_ms_all": round(max(ms for ms,_,_ in rows), 2),
                    "cache_hits": sum(cached is True for _,_,cached in rows)})
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", type=int, default=3)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as exc:
        print(f"基准未完成：{type(exc).__name__}（不输出连接信息）")
        raise SystemExit(1)
