"""@brief FogMoe typed 数据分析界面 / FogMoe typed analytics surface."""

from .api import DashboardClient
from .domain.models import TimeWindow

__all__ = ["DashboardClient", "TimeWindow"]
