"""Parallel manual/UID inputs, one read projection, no network or pixel claims.

UID snapshots stay intact. Explicit manual fields are a separate overlay and
cannot be overwritten by a later UID refresh. Sets are not six invented relics.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
from collections import Counter
from copy import deepcopy
from pathlib import Path

from plugin.sdk.plugin import Err, unwrap_or

from .uid_import import digest, roster_view, utcnow
from .uid_service import uid_service

STORE_KEY = "manual_player_builds"
PANEL_FIELDS = {
    "hp": ("生命值", "", 1000000),
    "atk": ("攻击力", "", 1000000),
    "def": ("防御力", "", 1000000),
    "spd": ("速度", "", 10000),
    "crit_rate": ("暴击率", "%", 1000),
    "crit_dmg": ("暴击伤害", "%", 10000),
    "break_effect": ("击破特攻", "%", 10000),
    "effect_hit": ("效果命中", "%", 10000),
    "effect_res": ("效果抵抗", "%", 10000),
    "energy_regen": ("能量恢复效率", "%", 1000),
    "healing_bonus": ("治疗量加成", "%", 1000),
    "damage_bonus": ("对应属性伤害提高", "%", 10000),
    "elation": ("欢愉度", "%", 10000),
}
EDITABLE = frozenset({"level", "eidolon", "light_cone", "sets", "panel"})


class BuildError(ValueError):
    pass


def selection_options(catalog, registry, signature_rows=()):
    """Compact local choices, not another copy of the knowledge database."""
    forms, cones, outer, inner = [], [], [], []
    signatures = {}
    for row in signature_rows:
        if row.get("signature_id") and row.get("form_id") and row.get("name"):
            signatures.setdefault(row["signature_id"], []).append(row)
    for role in registry.records:
        for raw_id in role.get("source_ids", []):
            form_id = "character_form." + str(raw_id)
            record = catalog.get(form_id) or {}
            path_id = record.get("path_id") or ""
            path = catalog.get("path." + path_id) or {}
            name = role["name"]
            forms.append(
                {
                    "id": form_id,
                    "name": name,
                    "path_id": path_id,
                    "label": name + (" · " + path.get("name", path_id) if path_id else " · " + str(raw_id)),
                }
            )
    database = getattr(catalog, "database_path", None)
    if database:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM records WHERE entity_type IN (?, ?) ORDER BY id LIMIT 1000",
                ("light_cone", "relic_set"),
            ).fetchall()
        for (raw,) in rows:
            record = json.loads(raw)
            choice = {"id": record["id"], "name": record["name"]}
            if record["entity_type"] == "light_cone":
                path_id = record.get("path", "")
                cones.append({
                    **choice, "path_id": path_id,
                    "path_name": (catalog.get("path." + path_id) or {}).get("name", ""),
                    "rarity": record.get("rarity"),
                    "signature_for": [r["name"] for r in signatures.get(record["id"], [])],
                    "signature_form_ids": [r["form_id"] for r in signatures.get(record["id"], [])],
                })
            elif len(record.get("desc", [])) == 2:
                outer.append({**choice, "effects": [re.sub(r"<[^>]*>", "", text) for text in record["desc"]]})
            elif len(record.get("desc", [])) == 1:
                inner.append({**choice, "effects": [re.sub(r"<[^>]*>", "", text) for text in record["desc"]]})
    return {"characters": forms, "light_cones": cones, "outer_sets": outer, "inner_sets": inner}


def integer(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BuildError("invalid_number")
    return value


def panel_value(value, maximum):
    # Percent fields use game-display units (8.2 means 8.2%), not ratios.
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,7}(?:\.\d{1,3})?", value.strip()):
        raise BuildError("invalid_panel")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= maximum:
        raise BuildError("invalid_panel")
    return value.strip()


def validate_changes(changes, options):
    if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE:
        raise BuildError("invalid_changes")
    result = deepcopy(changes)
    for key in ("level", "eidolon"):
        if key in result:
            result[key] = (
                None
                if result[key] is None
                else integer(result[key], 1 if key == "level" else 0, 80 if key == "level" else 6)
            )
    if "light_cone" in result:
        cone = result["light_cone"]
        if cone is not None:
            if not isinstance(cone, dict) or set(cone) != {"id", "level", "superimposition"}:
                raise BuildError("invalid_cone")
            record = next((r for r in options["light_cones"] if r["id"] == cone["id"]), None)
            if not record:
                raise BuildError("unknown_cone")
            result["light_cone"] = {
                **cone,
                "name": record["name"],
                "level": None if cone["level"] is None else integer(cone["level"], 1, 80),
                "superimposition": None if cone["superimposition"] is None else integer(cone["superimposition"], 1, 5),
            }
    if "sets" in result:
        sets = result["sets"]
        if not isinstance(sets, dict) or set(sets) != {"outer", "inner"}:
            raise BuildError("invalid_sets")
        allowed = {r["id"]: r["name"] for r in options["outer_sets"]}
        outside = sets["outer"]
        if outside is not None:
            if not isinstance(outside, list) or len(outside) > 2:
                raise BuildError("invalid_sets")
            seen, bound = set(), []
            for row in outside:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"id", "pieces"}
                    or not isinstance(row["id"], str)
                    or row["id"] not in allowed
                    or row["id"] in seen
                    or type(row["pieces"]) is not int
                    or row["pieces"] not in (2, 4)
                ):
                    raise BuildError("invalid_sets")
                seen.add(row["id"])
                bound.append({**row, "name": allowed[row["id"]]})
            if bound and sorted(r["pieces"] for r in bound) not in ([4], [2], [2, 2]):
                raise BuildError("invalid_sets")
            sets["outer"] = bound
        if sets["inner"] is not None:
            record = next((r for r in options["inner_sets"] if r["id"] == sets["inner"]), None)
            if not record:
                raise BuildError("invalid_sets")
            sets["inner"] = {"id": record["id"], "name": record["name"], "pieces": 2}
    if "panel" in result:
        panel = result["panel"]
        if not isinstance(panel, dict) or set(panel) - PANEL_FIELDS.keys():
            raise BuildError("invalid_panel")
        result["panel"] = {key: panel_value(value, PANEL_FIELDS[key][2]) for key, value in panel.items()}
    return result


def _imported_sets(build):
    relics = build.get("relics", [])
    names = {r.get("set_id"): r.get("set_name") for r in relics if r.get("set_id")}
    outside = Counter(r.get("set_id") for r in relics if r.get("slot_id") in (1, 2, 3, 4) and r.get("set_id"))
    inside = Counter(r.get("set_id") for r in relics if r.get("slot_id") in (5, 6) and r.get("set_id"))
    return {
        "outer": [
            {"id": key, "name": names[key], "pieces": 4 if count == 4 else 2}
            for key, count in outside.items()
            if count >= 2
        ]
        or None,
        "inner": next(
            ({"id": key, "name": names[key], "pieces": 2} for key, count in inside.items() if count == 2), None
        ),
    }


def combined_roster(uid_state, manual_state, *, now=None, details=False, names=None, editing=False):
    now = now or utcnow()
    original = roster_view(uid_state, now=now, details=True)
    rows = {r["form_id"]: r for r in original["characters"]}
    overlays = manual_state.get("characters", {})
    account = uid_state.get("uid") or manual_state.get("active_account_uid")
    applicable = {}
    for saved in sorted(overlays.values(), key=lambda r: r.get("account_uid") is not None):
        if saved.get("account_uid") not in (None, account):
            continue
        form_id = saved["form_id"]
        old = applicable.get(form_id, {})
        applicable[form_id] = {
            **deepcopy(saved),
            "changes": {**deepcopy(old.get("changes", {})), **deepcopy(saved["changes"])},
            "field_updated_at": {
                **deepcopy(old.get("field_updated_at", {})),
                **deepcopy(saved.get("field_updated_at", {})),
            },
        }
    # Backwards compatible UID-only contract, unless editor revision data is needed.
    if not applicable and not editing:
        return roster_view(uid_state, now=now, details=details, names=names)
    for form_id, saved in applicable.items():
        row = rows.setdefault(
            form_id,
            {
                "form_id": form_id,
                "identity_id": saved["identity_id"],
                "name": saved["name"],
                "path_id": saved.get("path_id"),
                "element_id": saved.get("element_id"),
                "level": None,
                "eidolon": None,
                "light_cone": None,
                "relics": [],
                "traces": [],
                "missing": [],
                "panel": {"state": "not_provided"},
                "visually_confirmed": False,
            },
        )
        changes = saved["changes"]
        for key in ("level", "eidolon", "light_cone"):
            if key in changes:
                row[key] = deepcopy(changes[key])
        row["sets"] = deepcopy(changes.get("sets", _imported_sets(row)))
        if "sets" in changes:
            # Old per-item rows no longer describe the player's declared set.
            row["relics"] = []
            row["missing"] = list(dict.fromkeys([*row.get("missing", []), "manual_sets_not_individual_relics"]))
        if "panel" in changes:
            values = changes["panel"]
            row["panel"] = {
                "state": "provided"
                if all(k in values for k in ("hp", "atk", "def", "spd", "crit_rate", "crit_dmg"))
                else "partial"
                if values
                else "not_provided",
                "source": "player_manual",
                "context": "out_of_combat",
                "values": deepcopy(values),
                "stats": [
                    {"key": key, "label": PANEL_FIELDS[key][0], "value": value + PANEL_FIELDS[key][1]}
                    for key, value in values.items()
                ],
                "updated_at": saved.get("field_updated_at", {}).get("panel", saved["updated_at"]),
                "includes_equipment_bonuses": True,
            }
        row["manual_fields"] = sorted(changes)
        row["field_sources"] = {
            key: "player_manual"
            if key in changes
            else "uid_snapshot"
            if form_id in rows and row.get("fetched_at")
            else "unknown"
            for key in EDITABLE
        }
        row["source"] = "player_manual_and_uid" if row.get("fetched_at") else "player_manual"
        row["manual_updated_at"] = saved["updated_at"]
        row["missing"] = list(
            dict.fromkeys(
                [
                    *row.get("missing", []),
                    *(key for key in ("level", "eidolon", "light_cone") if row.get(key) is None),
                    *(
                        "panel:" + key
                        for key in ("hp", "atk", "def", "spd", "crit_rate", "crit_dmg")
                        if key not in row.get("panel", {}).get("values", {})
                    ),
                ]
            )
        )
    selected = []
    for form_id, row in rows.items():
        if names and row["name"] not in names and form_id not in names:
            continue
        row.setdefault("sets", _imported_sets(row))
        row["edit_revision"] = digest(
            {
                "uid": uid_state.get("uid"),
                "form_id": form_id,
                "uid_digest": row.get("content_digest"),
                "overlay": applicable.get(form_id),
            }
        )
        if not details:
            relics = row.pop("relics", [])
            row["equipped_relic_count"] = len(relics) if "sets" not in row.get("manual_fields", []) else None
            row["complete_relic_count"] = sum(r["complete"] for r in relics)
            row["trace_count"] = len(row.pop("traces", []))
        row["relic_sets"] = list(
            dict.fromkeys(
                [r["name"] for r in row["sets"].get("outer") or []]
                + ([row["sets"]["inner"]["name"]] if row["sets"].get("inner") else [])
            )
        )
        selected.append(row)
    return {
        **original,
        "source": ("player_manual_and_uid" if uid_state.get("characters") else "player_manual")
        if applicable
        else original["source"],
        "source_url": original["source_url"] if uid_state.get("characters") else None,
        "scope": "player_confirmed_builds" if applicable else original["scope"],
        "characters": selected,
        "new_character_revision": digest(
            {"uid": uid_state.get("uid"), "manual_account": manual_state.get("active_account_uid")}
        ),
        "instruction": "这是玩家保存的养成资料，不是当前画面或实时同步。手动字段优先，UID刷新不会覆盖。"
        "缺项保持未知，可按本次问题使用已填字段，不必等待全部可选属性；套装记录不等于单件词条，不能用旧UID单件填补手动换装。"
        "手填面板是非战斗最终属性，已含装备收益，不得再次叠加光锥或套装加成。",
    }


class PlayerBuildService:
    def __init__(self, owner):
        self.owner, self.lock = owner, asyncio.Lock()
        self._options_key, self._options = None, None

    async def read(self):
        stored = await self.owner._get_store(STORE_KEY)
        if isinstance(stored, Err):
            raise RuntimeError("MANUAL_BUILDS_READ_FAILED")
        value = unwrap_or(stored, {}) or {}
        if not isinstance(value, dict) or not isinstance(value.get("characters", {}), dict):
            raise RuntimeError("MANUAL_BUILDS_INVALID")
        return deepcopy(value)

    def options(self):
        catalog = self.owner._catalog
        key = (catalog.revision, str(getattr(catalog, "database_path", "")))
        if key != self._options_key:
            try:
                # Reviewed game metadata only. Never infer ownership from this
                # directory, and never write player edits into the game pack.
                book_path = Path(__file__).resolve().parents[1] / "resources/guidance.cn-4.6.json"
                try:
                    book = json.loads(book_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    book = {}  # Missing optional notes must not disable manual input.
                self._options = selection_options(catalog, self.owner._characters, book.get("characters", []))
            except (sqlite3.Error, OSError) as exc:
                raise RuntimeError("LOCAL_BUILD_CATALOG_UNAVAILABLE") from exc
            self._options_key = key
        return deepcopy(self._options)

    async def roster(self, *, details=False, names=None, editing=False):
        return combined_roster(
            await uid_service(self.owner).read(), await self.read(), details=details, names=names, editing=editing
        )

    async def save(self, form_id, changes, expected_revision):
        async with self.lock:
            generation = getattr(self.owner, "_uid_import_generation", 0)
            if not isinstance(form_id, str) or not isinstance(expected_revision, str):
                raise BuildError("invalid_changes")
            options = self.options()
            option = next((row for row in options["characters"] if row["id"] == form_id), None)
            if not option:
                raise BuildError("unknown_character")
            checked = validate_changes(changes, options)
            state, uid = await self.read(), await uid_service(self.owner).read()
            current = combined_roster(uid, state, details=True, editing=True)
            row = next((r for r in current["characters"] if r["form_id"] == form_id), None)
            if expected_revision != (row["edit_revision"] if row else current["new_character_revision"]):
                raise BuildError("stale_edit")
            form = self.owner._catalog.get(form_id) or {}
            identity = next(
                r for r in self.owner._characters.records if form_id.split(".")[-1] in map(str, r.get("source_ids", []))
            )
            # Separate account slots prevent overwriting a hidden previous account's edits.
            account = uid.get("uid") or state.get("active_account_uid")
            key = (account or "local") + ":" + form_id
            old = state.get("characters", {}).get(key, {})
            updated_at = utcnow().isoformat()
            saved = {
                "form_id": form_id,
                "identity_id": "character." + identity["tag"],
                "name": identity["name"],
                "path_id": form.get("path_id"),
                "element_id": form.get("element_id"),
                "account_uid": account,
                "updated_at": updated_at,
                "field_updated_at": {
                    **deepcopy(old.get("field_updated_at", {})),
                    **{field: updated_at for field in checked},
                },
                "changes": {**deepcopy(old.get("changes", {})), **checked},
            }
            result = deepcopy(state)
            result.setdefault("characters", {})[key] = saved
            result["schema_version"] = 1
            result["active_account_uid"] = account
            if generation != getattr(self.owner, "_uid_import_generation", 0):
                raise BuildError("stale_edit")
            await self.owner._store_value(STORE_KEY, result)
            receipt = {"status": "saved", "form_id": form_id, "readback_state": "unavailable"}
            # Storage acknowledgement and successful read-back are distinct.
            # A read-back error must not invite duplicate writes after a save.
            try:
                persisted = await self.read()
                if persisted.get("characters", {}).get(key) == saved:
                    row = next(r for r in combined_roster(uid, persisted, editing=True)["characters"]
                               if r["form_id"] == form_id)
                    receipt.update(readback_state="confirmed", build=row)
            except (RuntimeError, OSError):
                pass
            return receipt


def player_build_service(owner):
    service = getattr(owner, "_player_build_service", None)
    if service is None:
        owner._player_build_service = service = PlayerBuildService(owner)
    return service
