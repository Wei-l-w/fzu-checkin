"""Only explicit personal date ranges are used; invalid data fails closed."""
import datetime
from zoneinfo import ZoneInfo

from src.config import validated_ranges

_TZ_CN = ZoneInfo("Asia/Shanghai")


def today_cn() -> datetime.date:
    return datetime.datetime.now(_TZ_CN).date()


def matched_range(cfg: dict, today: datetime.date | None = None):
    """Validate every range before matching, including later malformed entries."""
    ranges = validated_ranges(cfg)
    today = today if today is not None else today_cn()
    for start, end, name in ranges:
        if start <= today <= end:
            return name
    return None
