"""Time validity is not proof of newest data. No implicit remote updates."""

from datetime import datetime, timezone

from .uid_import import stamp


def guidance_freshness(book, *, now=None):
    now = now or datetime.now(timezone.utc)
    data = book.data
    checked = stamp(str(data.get("checked_at") or "") + "T00:00:00+08:00")
    active, upcoming = [], []
    for period in data.get("periods", []):
        start, end = stamp(period.get("starts_at")), stamp(period.get("ends_at"))
        if start and end:
            row = {
                "id": period["id"],
                "name": period.get("name"),
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
            }
            if start <= now < end:
                active.append(row)
            elif now < start:
                upcoming.append(row)
    reviewed_recently = bool(checked and 0 <= (now - checked).total_seconds() <= 7 * 86400)
    return {
        "server": data.get("server"),
        "game_version": data.get("game_version"),
        "revision": data.get("revision"),
        "checked_at": data.get("checked_at"),
        "active_periods": active,
        "upcoming_periods": upcoming,
        "status": "within_review_window" if reviewed_recently and active else "review_due",
        "latest_verified": False,
        "automatic_remote_update": False,
        "instruction": "已核对资料有有效期，不代表保证最新。只用起止时间内的期次回答本期；"
        "换期后没有已核对新资料时说明缺失，不能沿用上期敌人和效果。"
        "基础资料按已验证版本快照更新，不从任意网页自动替换攻略。",
    }


def data_freshness(catalog, book, *, now=None):
    meta = catalog.meta
    return {
        "knowledge": {
            "game_version": meta.get("game_version"),
            "snapshot_date": meta.get("snapshot_date"),
            "revision": meta.get("upstream_revision") or meta.get("data_revision"),
            "latest_verified": False,
            "policy": "verified_versioned_snapshot",
        },
        "guidance": guidance_freshness(book, now=now),
    }
