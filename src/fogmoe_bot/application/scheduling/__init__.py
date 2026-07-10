"""@brief 后台调度应用入口 / Background-scheduling application entry points."""

from .daemon import register_scheduling_daemon, run_scheduling_daemon_tick

__all__ = ["register_scheduling_daemon", "run_scheduling_daemon_tick"]
