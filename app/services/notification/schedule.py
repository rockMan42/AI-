import shlex
import sys
from pathlib import Path

from cron.jobs import create_job, list_jobs, update_job
from hermes_constants import get_hermes_home
from hermes_time import get_timezone
from sqlalchemy import select

from app.core.database import create_session
from app.models.notification import NotificationSchedule


ROOT = Path(__file__).resolve().parents[3]


def sync_scene(scene: str, *, config: NotificationSchedule | None = None):
    if str(get_timezone()) != "Asia/Shanghai":
        raise RuntimeError("Hermes timezone 必须为 Asia/Shanghai")
    if config is None:
        raise RuntimeError("同步调度时必须传入已读取的配置")
    scripts = get_hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    name = f"notification-{scene}"
    path = scripts / f"{name}.sh"
    content = (
        "#!/bin/bash\nset -e\n"
        f"cd {shlex.quote(str(ROOT))}\n"
        f"exec {shlex.quote(sys.executable)} -m app.cron.notification_tasks {shlex.quote(scene)}\n"
    )
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise RuntimeError("已有同名 Hermes 脚本与当前项目不一致")
    else:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
    existing = next((job for job in list_jobs(include_disabled=True) if job["name"] == name), None)
    if existing:
        update_job(existing["id"], {"schedule": config.cron_expr, "enabled": config.enabled})
    else:
        create_job(prompt=None, schedule=config.cron_expr, name=name,
                   script=path.name, no_agent=True, deliver="local")
        if not config.enabled:
            created = next(job for job in list_jobs(include_disabled=True) if job["name"] == name)
            update_job(created["id"], {"enabled": False})


async def sync_all():
    async with create_session() as db:
        rows = list(await db.scalars(select(NotificationSchedule)))
    for row in rows:
        sync_scene(row.scene, config=row)
