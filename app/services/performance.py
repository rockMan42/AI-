from collections import defaultdict
from datetime import date, datetime
from math import floor
from statistics import mean, pstdev

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department
from app.models.performance import PerformanceReview, PerformanceStats
from app.models.user import User
from app.schemas.permission import Principal, Role
from app.security.permission import authorize
from app.utils.time import utc_now


FINAL_STATUSES = {
    "completed", "archived", "finalized",
    "定稿", "最终确定", "敲定",
}
DRAFT_STATUSES = {"draft", "DRAFT", "草稿"}


class PerformanceError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def percentile(value: float, values: list[float]) -> float:
    if not values:
        return 0.0
    return round(
        sum(item <= value for item in values) * 100 / len(values),
        2,
    )


def quantile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * ratio
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def score_stats(values: list[float]) -> dict:
    if not values:
        return {
            "avg_score": 0.0,
            "std_dev": 0.0,
            "min_score": 0.0,
            "max_score": 0.0,
            "p25": 0.0,
            "p50": 0.0,
            "p75": 0.0,
            "total_count": 0,
        }
    return {
        "avg_score": round(mean(values), 2),
        "std_dev": round(pstdev(values), 2),
        "min_score": min(values),
        "max_score": max(values),
        "p25": round(quantile(values, .25), 2),
        "p50": round(quantile(values, .50), 2),
        "p75": round(quantile(values, .75), 2),
        "total_count": len(values),
    }


def detect_anomalies(records: list[dict]) -> list[dict]:
    if len(records) < 2:
        return []

    scores = [float(item["score"]) for item in records]
    anomalies = []

    if len(scores) >= 5:
        average = mean(scores)
        deviation = pstdev(scores)
        if deviation == 0:
            return []
        for item in records:
            z_score = abs(float(item["score"]) - average) / deviation
            if z_score > 2:
                anomalies.append({
                    "user_id": item["user_id"],
                    "score": item["score"],
                    "reason": f"偏离团队均值 {z_score:.1f}σ",
                })
        return anomalies

    q1 = quantile(scores, .25)
    q3 = quantile(scores, .75)
    spread = q3 - q1
    lower = q1 - 1.5 * spread
    upper = q3 + 1.5 * spread
    for item in records:
        score = float(item["score"])
        if score < lower or score > upper:
            anomalies.append({
                "user_id": item["user_id"],
                "score": item["score"],
                "reason": "超出 IQR 正常范围",
            })
    return anomalies


def score_distribution(values: list[float]) -> list[dict]:
    if not values:
        return []
    if max(values) <= 5:
        ranges = [
            ("1.0-1.9", 1, 2),
            ("2.0-2.9", 2, 3),
            ("3.0-3.9", 3, 4),
            ("4.0-4.9", 4, 5),
            ("5.0", 5, float("inf")),
        ]
    else:
        ranges = [
            ("0-59", 0, 60),
            ("60-69", 60, 70),
            ("70-79", 70, 80),
            ("80-89", 80, 90),
            ("90-100", 90, float("inf")),
        ]
    return [
        {
            "range": label,
            "count": sum(lower <= value < upper for value in values),
        }
        for label, lower, upper in ranges
    ]


def low_score_threshold(values: list[float]) -> float:
    return 3.0 if not values or max(values) <= 5 else 60.0


def historical_alerts(records: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for item in records:
        groups[item["user_id"]].append(item)

    alerts = []
    all_scores = [float(item["score"]) for item in records]
    threshold = low_score_threshold(all_scores)
    for user_id, items in groups.items():
        latest = sorted(items, key=lambda item: item["cycle"])[-2:]
        if (
            len(latest) == 2
            and all(float(item["score"]) < threshold for item in latest)
        ):
            alerts.append({
                "type": "consecutive_low_score",
                "user_id": user_id,
                "cycles": [item["cycle"] for item in latest],
            })
    return alerts


def tenure_group(created_at: datetime, today: date) -> str:
    years = (today - created_at.date()).days / 365.25
    if years < 1:
        return "1年以内"
    if years < 3:
        return "1-3年"
    return "3年以上"


class PerformanceService:
    async def personal(
        self,
        db: AsyncSession,
        principal: Principal,
        cycle: str,
    ) -> dict:
        await authorize(
            principal,
            "performance.read",
            target_user_id=principal.user_id,
        )
        review = await db.scalar(
            select(PerformanceReview).where(
                PerformanceReview.user_id == principal.user_id,
                PerformanceReview.period == cycle,
                PerformanceReview.status.in_(FINAL_STATUSES),
            )
        )
        if review is None:
            raise PerformanceError("未找到已完成的绩效记录", 404)

        scores = list(await db.scalars(
            select(PerformanceReview.score)
            .join(User, User.user_id == PerformanceReview.user_id)
            .where(
                User.department_id == principal.department_id,
                PerformanceReview.period == cycle,
                PerformanceReview.status.in_(FINAL_STATUSES),
            )
        ))
        return {
            "user_id": principal.user_id,
            "cycle": cycle,
            "score": review.score,
            "grade": review.grade,
            "comment": review.reviewer_comment or "",
            "department_percentile": percentile(
                float(review.score),
                [float(value) for value in scores],
            ),
        }

    async def team(
        self,
        db: AsyncSession,
        principal: Principal,
        cycle: str,
        grade: str | None = None,
    ) -> dict:
        await authorize(principal, "performance.read", scope="team")
        statement = (
            select(PerformanceReview, User.name)
            .join(User, User.user_id == PerformanceReview.user_id)
            .where(
                PerformanceReview.period == cycle,
                PerformanceReview.status.in_(FINAL_STATUSES),
            )
            .order_by(PerformanceReview.score.desc())
        )
        if principal.role == Role.MANAGER:
            if not principal.managed_user_ids:
                return {
                    "cycle": cycle,
                    "grade_filter": grade,
                    "summary": score_stats([]),
                    "members": [],
                    "anomalies": [],
                }
            statement = statement.where(
                PerformanceReview.user_id.in_(principal.managed_user_ids)
            )
        if grade:
            statement = statement.where(
                PerformanceReview.grade == grade
            )

        rows = (await db.execute(statement)).all()
        members = [
            {
                "user_id": review.user_id,
                "name": name,
                "score": review.score,
                "grade": review.grade,
                "comment": review.reviewer_comment or "",
            }
            for review, name in rows
        ]
        return {
            "cycle": cycle,
            "grade_filter": grade,
            "summary": score_stats([
                float(item["score"]) for item in members
            ]),
            "members": members,
            "anomalies": detect_anomalies(members),
        }

    async def overview(
        self,
        db: AsyncSession,
        principal: Principal,
        cycle: str,
        grade: str | None = None,
    ) -> dict:
        await authorize(principal, "performance.report")
        statement = (
            select(
                PerformanceReview,
                User.department_id,
                Department.name,
            )
            .join(User, User.user_id == PerformanceReview.user_id)
            .join(
                Department,
                Department.department_id == User.department_id,
            )
            .where(
                PerformanceReview.period == cycle,
                PerformanceReview.status.in_(FINAL_STATUSES),
            )
        )
        if grade:
            statement = statement.where(
                PerformanceReview.grade == grade
            )
        rows = (await db.execute(statement)).all()

        scores = [float(review.score) for review, _, _ in rows]
        departments = defaultdict(list)
        for review, _, department_name in rows:
            departments[department_name].append(float(review.score))

        history_rows = (await db.execute(
            select(
                PerformanceReview.user_id,
                PerformanceReview.period,
                PerformanceReview.score,
            )
            .where(PerformanceReview.status.in_(FINAL_STATUSES))
        )).all()
        history = [
            {"user_id": user_id, "cycle": period, "score": score}
            for user_id, period, score in history_rows
        ]
        alerts = historical_alerts(history)
        summary = score_stats(scores)
        concentration_limit = .25 if scores and max(scores) <= 5 else 5
        if len(scores) >= 5 and summary["std_dev"] < concentration_limit:
            alerts.append({
                "type": "score_concentration",
                "std_dev": summary["std_dev"],
            })

        return {
            "cycle": cycle,
            "grade_filter": grade,
            "summary": summary,
            "distribution": score_distribution(scores),
            "departments": [
                {"department": name, **score_stats(values)}
                for name, values in sorted(departments.items())
            ],
            "alerts": alerts,
        }

    async def refresh_stats(
        self,
        db: AsyncSession,
        cycle: str,
        dimension: str,
        today: date | None = None,
    ) -> list[dict]:
        if dimension not in {"department", "level", "tenure"}:
            raise PerformanceError("统计维度无效", 422)

        rows = (await db.execute(
            select(
                PerformanceReview.score,
                User.department_id,
                Department.name,
                User.position_level,
                User.created_at,
            )
            .join(User, User.user_id == PerformanceReview.user_id)
            .join(
                Department,
                Department.department_id == User.department_id,
            )
            .where(
                PerformanceReview.period == cycle,
                PerformanceReview.status.in_(FINAL_STATUSES),
            )
        )).all()

        groups = defaultdict(list)
        department_ids = {}
        current_date = today or date.today()
        for score, department_id, department_name, level, created_at in rows:
            if dimension == "department":
                value = department_name
                department_ids[value] = department_id
            elif dimension == "level":
                value = level or "未设置"
            else:
                value = tenure_group(created_at, current_date)
            groups[value].append(float(score))

        computed_at = utc_now()
        result = []
        for value, scores in groups.items():
            item = {
                "cycle": cycle,
                "department_id": department_ids.get(value),
                "dimension": dimension,
                "dimension_value": value,
                **score_stats(scores),
                "computed_at": computed_at,
            }
            result.append(item)
            statement = insert(PerformanceStats).values(**item)
            await db.execute(statement.on_duplicate_key_update(
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {
                        "cycle", "dimension", "dimension_value",
                    }
                }
            ))
        await db.commit()
        return sorted(result, key=lambda item: item["dimension_value"])

    async def stats_rows(
        self,
        db: AsyncSession,
        cycle: str,
        dimension: str,
    ) -> list[dict]:
        rows = (await db.scalars(
            select(PerformanceStats)
            .where(
                PerformanceStats.cycle == cycle,
                PerformanceStats.dimension == dimension,
            )
            .order_by(PerformanceStats.avg_score.desc())
        )).all()
        if not rows:
            return await self.refresh_stats(db, cycle, dimension)
        return [
            {
                "cycle": row.cycle,
                "dimension": row.dimension,
                "dimension_value": row.dimension_value,
                "avg_score": float(row.avg_score),
                "std_dev": float(row.std_dev),
                "min_score": float(row.min_score),
                "max_score": float(row.max_score),
                "p25": float(row.p25),
                "p50": float(row.p50),
                "p75": float(row.p75),
                "total_count": row.total_count,
                "computed_at": row.computed_at,
            }
            for row in rows
        ]
