import os
import subprocess
import sys


JOBS = {
    "daily_scan": [sys.executable, "daily_scan_telegram.py"],
    "telegram_sat_bot": [sys.executable, "telegram_sat_bot.py", "--once"],
    "sat_telegram_bot": [sys.executable, "telegram_sat_bot.py", "--once"],
}


def main() -> int:
    job = os.getenv("RAILWAY_JOB", "telegram_sat_bot").strip().lower()
    command = JOBS.get(job)
    if command is None:
        valid_jobs = ", ".join(sorted(JOBS))
        print(f"Unknown RAILWAY_JOB={job!r}. Valid values: {valid_jobs}")
        return 2

    print(f"Starting Railway job: {job}")
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
