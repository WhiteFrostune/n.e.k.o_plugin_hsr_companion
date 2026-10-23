"""Account build snapshots, explicitly separate from current-screen evidence.

No OCR, automatic account login, SDK dependencies or final-panel calculator.
The Enka wire format is mapped by documented field names, not copied SDK code.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from hashlib import sha256
from uuid import uuid4

from ..adapters.showcase import PROVIDER, PROVIDER_URL, ShowcaseError, validate_uid

SCHEMA_VERSION = 1
STORE_KEY = "uid_import_state"
PREVIEW_SECONDS = 1800
REVIEW_AFTER_SECONDS = 24 * 3600
SLOTS = {1: "头部", 2: "手部", 3: "躯干", 4: "脚部", 5: "连结绳", 6: "位面球"}
PROPERTIES = {
    "BaseHP": ("生命值", ""),
    "HPDelta": ("生命值", ""),
    "HPAddedRatio": ("生命值", "%"),
    "BaseAttack": ("攻击力", ""),
    "AttackDelta": ("攻击力", ""),
    "AttackAddedRatio": ("攻击力", "%"),
    "BaseDefence": ("防御力", ""),
    "DefenceDelta": ("防御力", ""),
    "DefenceAddedRatio": ("防御力", "%"),
    "SpeedDelta": ("速度", ""),
    "CriticalChance": ("暴击率", "%"),
    "CriticalDamage": ("暴击伤害", "%"),
    "StatusProbability": ("效果命中", "%"),
    "StatusResistance": ("效果抵抗", "%"),
    "BreakDamageAddedRatio": ("击破特攻", "%"),
    "SPRatioBase": ("能量恢复效率", "%"),
    "HealRatioBase": ("治疗量加成", "%"),
    "PhysicalAddedRatio": ("物理属性伤害提高", "%"),
    "FireAddedRatio": ("火属性伤害提高", "%"),
    "IceAddedRatio": ("冰属性伤害提高", "%"),
    "ThunderAddedRatio": ("雷属性伤害提高", "%"),
    "WindAddedRatio": ("风属性伤害提高", "%"),
    "QuantumAddedRatio": ("量子属性伤害提高", "%"),
    "ImaginaryAddedRatio": ("虚数属性伤害提高", "%"),
    "ElationDamageAddedRatio": ("欢愉度", "%"),
    "CriticalChanceBase": ("暴击率", "%"),
    "CriticalDamageBase": ("暴击伤害", "%"),
    "BreakDamageAddedRatioBase": ("击破特攻", "%"),
    "StatusProbabilityBase": ("效果命中", "%"),
    "StatusResistanceBase": ("效果抵抗", "%"),
}


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def remaining(value, now):
    end = stamp(value)
    return max(0, math.ceil((end - now).total_seconds())) if end else 0


def number(value, low, high, *, default=None):
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ShowcaseError("unsupported_response")
    return value


def identifier(value):
    if isinstance(value, bool):
        raise ShowcaseError("unsupported_response")
    text = str(value)
    if not text.isascii() or not text.isdigit() or not 1 <= len(text) <= 9:
        raise ShowcaseError("unsupported_response")
    return text


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stat(row):
    if not isinstance(row, dict):
        raise ShowcaseError("unsupported_response")
    kind, value = row.get("type"), row.get("value")
    if (
        not isinstance(kind, str)
        or len(kind) > 64
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1e9
    ):
        raise ShowcaseError("unsupported_response")
    label, unit = PROPERTIES.get(kind, ("未收录属性", "unknown"))
    amount = Decimal(str(value)) * (100 if unit == "%" else 1)
    precision = Decimal("0.1") if unit == "%" else Decimal("1")
    # Keep the provider's raw number; only remove sub-picounit binary noise
    # before the game's truncating display (e.g. 32.399999999999995 -> 32.4).
    displayed = format((amount + Decimal("0.000000000001")).quantize(precision, rounding=ROUND_FLOOR), "f")
    return {
        "property": kind,
        "label": label,
        "raw_value": value,
        "unit": unit,
        "display": displayed + ("%" if unit == "%" else "") if unit != "unknown" else kind,
        "known_property": unit != "unknown",
    }


def _lookup(catalog, entity_id, fallback):
    record = catalog.get(entity_id) if catalog else None
    return str((record or {}).get("name") or fallback)


def normalize_showcase(payload, *, uid, registry, catalog=None, now=None, ttl=60):
    """Whitelist fields. Nickname, birthday, signature, UID and owner omitted."""
    uid, now = validate_uid(uid), now or utcnow()
    details = payload.get("detailInfo") if isinstance(payload, dict) else None
    if not isinstance(details, dict) or str(details.get("uid", payload.get("uid", ""))) != uid:
        raise ShowcaseError("uid_mismatch")
    if str(payload.get("uid", uid)) != uid or payload.get("region") not in (None, "CN"):
        raise ShowcaseError("uid_mismatch")
    raw = details.get("avatarDetailList", [])
    if not isinstance(raw, list) or len(raw) > 24:
        raise ShowcaseError("unsupported_response")
    if not raw:
        raise ShowcaseError("showcase_empty")
    if details.get("isDisplayAvatar") is False:
        raise ShowcaseError("details_private")
    by_id = {
        str(source_id): record for record in registry.get("records", []) for source_id in record.get("source_ids", [])
    }
    builds, conflicts = {}, set()
    for avatar in raw:
        if not isinstance(avatar, dict):
            raise ShowcaseError("unsupported_response")
        form_id = identifier(avatar.get("avatarId"))
        known = by_id.get(form_id, {})
        record = catalog.get("character_form." + form_id) if catalog else None
        build = {
            "form_id": "character_form." + form_id,
            "identity_id": "character." + known["tag"] if known else None,
            "name": known.get("name") or "未收录角色（" + form_id + "）",
            "path_id": (record or {}).get("path_id"),
            "element_id": (record or {}).get("element_id"),
            "level": number(avatar.get("level"), 1, 80),
            "promotion": number(avatar.get("promotion"), 0, 6, default=0),
            "eidolon": number(avatar.get("rank"), 0, 6, default=0),
            "enhancement_id": number(avatar.get("enhancedId", avatar.get("enhanceId")), 0, 1000000, default=0),
            "light_cone": None,
            "relics": [],
            "traces": [],
            "panel": {
                "state": "not_provided",
                "source": "none",
                "reason": "公开接口未直接提供最终面板；不把光锥基础属性或遗器合计冒充角色面板",
            },
            "missing": [] if known else ["character_registry"],
        }
        cone = avatar.get("equipment")
        if cone:
            if not isinstance(cone, dict):
                raise ShowcaseError("unsupported_response")
            cone_id = "light_cone." + identifier(cone.get("tid"))
            build["light_cone"] = {
                "id": cone_id,
                "name": _lookup(catalog, cone_id, "光锥ID " + cone_id.split(".")[-1]),
                "level": number(cone.get("level"), 1, 80),
                "promotion": number(cone.get("promotion"), 0, 6, default=0),
                "superimposition": number(cone.get("rank"), 1, 5, default=1),
            }
        relics = avatar.get("relicList", [])
        traces = avatar.get("skillTreeList", [])
        if not isinstance(relics, list) or len(relics) > 6 or not isinstance(traces, list) or len(traces) > 64:
            raise ShowcaseError("unsupported_response")
        slots = set()
        for relic in relics:
            if not isinstance(relic, dict):
                raise ShowcaseError("unsupported_response")
            slot = number(relic.get("type"), 1, 6)
            if slot in slots:
                raise ShowcaseError("conflicting_relic_slot")
            slots.add(slot)
            flat = relic.get("_flat") or {}
            if not isinstance(flat, dict):
                raise ShowcaseError("unsupported_response")
            props = flat.get("props", [])
            if not isinstance(props, list) or len(props) > 5:
                raise ShowcaseError("unsupported_response")
            stats = [_stat(row) for row in props]
            set_id = "relic_set." + identifier(flat["setID"]) if flat.get("setID") is not None else None
            name = _lookup(catalog, set_id, "套装ID " + set_id.split(".")[-1]) if set_id else "未确认套装"
            level = number(relic.get("level"), 0, 15, default=0)
            complete = bool(stats) and all(row["known_property"] for row in stats) and (level != 15 or len(stats) == 5)
            build["relics"].append(
                {
                    "id": identifier(relic.get("tid")),
                    "slot_id": slot,
                    "slot": SLOTS[slot],
                    "set_id": set_id,
                    "set_name": name,
                    "level": level,
                    "main_stat": stats[0] if stats else None,
                    "sub_stats": stats[1:],
                    "complete": complete,
                }
            )
            if not complete:
                build["missing"].append("relic_stats:" + str(slot))
        if len(slots) != 6:
            build["missing"].append("six_equipped_relics")
        points = set()
        for trace in traces:
            if not isinstance(trace, dict):
                raise ShowcaseError("unsupported_response")
            point = identifier(trace.get("pointId"))
            if point in points:
                raise ShowcaseError("unsupported_response")
            points.add(point)
            build["traces"].append({"id": point, "level": number(trace.get("level"), 1, 30)})
        build["relics"].sort(key=lambda row: row["slot_id"])
        build["traces"].sort(key=lambda row: row["id"])
        build["content_digest"] = digest(build)
        if form_id in builds and builds[form_id]["content_digest"] != build["content_digest"]:
            conflicts.add(form_id)
        else:
            builds[form_id] = build
    for form_id in conflicts:
        builds.pop(form_id, None)
    if not builds:
        raise ShowcaseError("conflicting_characters")
    fetched = now.isoformat()
    for build in builds.values():
        build.update(
            fetched_at=fetched,
            source_updated_at=None,
            source=PROVIDER,
            source_url=PROVIDER_URL,
            visually_confirmed=False,
            catalog_revision=str(
                getattr(catalog, "meta", {}).get("upstream_revision")
                or registry.get("meta", {}).get("upstream_revision")
                or ""
            ),
        )
    return {
        "preview_id": uuid4().hex,
        "uid": uid,
        "source": PROVIDER,
        "fetched_at": fetched,
        "source_updated_at": None,
        "next_query_at": (now + timedelta(seconds=max(60, ttl))).isoformat(),
        "expires_at": (now + timedelta(seconds=PREVIEW_SECONDS)).isoformat(),
        "characters": builds,
        "conflicting_form_ids": sorted(conflicts),
        "scope": "public_showcase_only",
        "confirmation_required": True,
    }


def confirm_preview(state, preview_id, *, now=None):
    now = now or utcnow()
    preview = state.get("preview") or {}
    if preview.get("preview_id") != preview_id or not remaining(preview.get("expires_at"), now):
        raise ShowcaseError("preview_expired")
    uid = validate_uid(preview.get("uid"))
    if state.get("uid") and state["uid"] != uid:
        raise ShowcaseError("different_account")
    result = deepcopy(state)
    characters = result.get("characters", {})
    for form_id, incoming in preview["characters"].items():
        # Replace one whole build; never merge old and new equipment/stat rows.
        characters[form_id] = {**deepcopy(incoming), "confirmed_at": now.isoformat()}
    result.update(
        schema_version=SCHEMA_VERSION, uid=uid, characters=characters, last_confirmed_at=now.isoformat(), preview=None
    )
    return result


def roster_view(state, *, now=None, details=False, names=None):
    now, rows = now or utcnow(), []
    for build in state.get("characters", {}).values():
        if names and build["name"] not in names and build["form_id"] not in names:
            continue
        row = deepcopy(build)
        fetched = stamp(row.get("fetched_at"))
        row["freshness"] = (
            "refresh_recommended"
            if not fetched or (now - fetched).total_seconds() > REVIEW_AFTER_SECONDS or fetched > now
            else "recent_snapshot_not_live"
        )
        if not details:
            relics = row.pop("relics", [])
            row["relic_sets"] = list(dict.fromkeys(r["set_name"] for r in relics))
            row["equipped_relic_count"] = len(relics)
            row["complete_relic_count"] = sum(r["complete"] for r in relics)
            row["trace_count"] = len(row.pop("traces", []))
            # Routine advice gets a compact account index, not every equipped
            # stat or repeated provenance blob. Explicit detail reads keep all.
            row = {
                key: row[key]
                for key in (
                    "form_id",
                    "identity_id",
                    "name",
                    "level",
                    "eidolon",
                    "enhancement_id",
                    "path_id",
                    "element_id",
                    "light_cone",
                    "fetched_at",
                    "freshness",
                    "missing",
                    "relic_sets",
                    "equipped_relic_count",
                    "complete_relic_count",
                    "trace_count",
                )
                if key in row
            }
            row["panel"] = {"state": build["panel"]["state"]}
        rows.append(row)
    return {
        "source": PROVIDER,
        "source_url": PROVIDER_URL,
        "scope": "public_showcase_only",
        "latest_verified": False,
        "roster_complete": False,
        "visually_confirmed": False,
        "characters": rows,
        "instruction": "这是玩家确认导入的配装存档，不是当前画面或实时账号同步；未导入不表示未拥有。"
        "fetched_at是查询时间，上游未提供配装修改时间。换装后需要刷新。"
        "panel.state=not_provided时不得编造最终面板；不能再次把装备属性加到已有最终面板。",
    }


def profile_with_roster(profile, roster):
    result = deepcopy(profile)
    imported = roster.get("characters", [])
    names = [row["name"] for row in imported if row.get("identity_id")]
    if names:
        result["owned_characters"] = list(dict.fromkeys([*result.get("owned_characters", []), *names]))
        result["imported_builds"] = deepcopy(roster)
    return result
