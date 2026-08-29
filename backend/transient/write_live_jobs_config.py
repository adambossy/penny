"""One-off: materialize the 4-job [[jobs]] schedule into the live workspace.

Non-canonical (transient/): merges the schedule + jobs config into whatever
config.toml the resolved workspace already holds, via penny.settings, so
nothing existing is clobbered. Deletable once run.
"""

from penny.settings import SCHEDULE_DEFAULTS, load_config, write_config

cfg = load_config()
env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}
schedule = {**SCHEDULE_DEFAULTS, **(cfg.get("schedule") or {}), "max_emails_per_day": 1}

me = "adambossy@gmail.com"
both = [me, "jloleary0@gmail.com"]
jobs = [
    {"name": "daily", "period": "daily", "hour": 8, "recipients": [me], "priority": 1},
    {
        "name": "weekly",
        "period": "weekly",
        "weekday": 1,
        "hour": 8,
        "recipients": both,
        "priority": 2,
    },
    {
        "name": "monthly",
        "period": "monthly",
        "day_of_month": 1,
        "hour": 8,
        "recipients": both,
        "priority": 3,
    },
    {
        "name": "annual",
        "period": "annual",
        "month": 1,
        "day_of_month": 1,
        "hour": 8,
        "recipients": both,
        "priority": 4,
    },
]
path = write_config(env, schedule, jobs)
print(path)
