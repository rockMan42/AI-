import shlex
import sys
from pathlib import Path

from cron.jobs import create_job, list_jobs
from hermes_constants import get_hermes_home
from hermes_time import get_timezone


PROJECT_ROOT = Path(__file__).resolve().parents[1]

TASKS = (
    ("priority", "0 2 * * *"),
    ("due_soon", "30 8 * * *"),
    ("daily", "0 9 * * *"),
)


def main():
    if str(get_timezone()) != "Asia/Shanghai":
        raise RuntimeError(
            "请先将 Hermes timezone 配置为 Asia/Shanghai"
        )

    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    existing = {
        job["name"]
        for job in list_jobs(include_disabled=True)
    }

    for task, schedule in TASKS:
        name = f"lead-{task}"
        script = scripts_dir / f"{name}.sh"
        content = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd {shlex.quote(str(PROJECT_ROOT))}\n"
            f"exec {shlex.quote(sys.executable)} "
            f"-m app.cron.lead_tasks {shlex.quote(task)}\n"
        )

        if script.exists():
            if script.read_text(encoding="utf-8") != content:
                raise RuntimeError(f"已有同名脚本内容不同：{script}")
        else:
            with script.open("x", encoding="utf-8") as stream:
                stream.write(content)

        if name in existing:
            print(f"已存在，跳过：{name}")
            continue

        job = create_job(
            prompt=None,
            schedule=schedule,
            name=name,
            script=script.name,
            no_agent=True,
            deliver="local",
        )
        print(f"已注册：{name}，ID={job['id']}")


if __name__ == "__main__":
    main()