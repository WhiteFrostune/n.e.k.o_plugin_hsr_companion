"""Scheduling coverage for recognized static details, never answer evidence.

The central model is decorative; the complete information panel (including
unread rows), controls, names and every known proof remain monitored.
"""
import math

STATIC_CONTENT = {
    "character_detail": ((.015, .01, .22, .65), (.65, .05, .995, .95)),
    "relic_detail": ((.01, .01, .22, .65), (.50, .02, .995, .96)),
}
KEYS = ("left", "top", "right", "bottom")


def static_content_regions(raw):
    page = (raw.get("page") or {}).get("page_type")
    controls = (raw.get("recognition") or {}).get("scene_regions") or []
    if page not in STATIC_CONTENT or not (raw.get("scene") or {}).get("verified") or len(controls) < 2:
        return []
    screen = raw.get("screen_facts") or {}
    proofs = [*controls, *(raw.get("characters") or []), *(screen.get("visible_text") or [])]
    proofs.extend(p for f in screen.get("facts") or [] for p in f.get("evidence") or [])
    regions = []
    for item in proofs:
        region = item.get("region")
        if not isinstance(region, dict):
            continue
        values = tuple(region.get(k, -1) for k in KEYS)
        if (not all(type(v) in {int, float} and math.isfinite(v) and 0 <= v <= 1 for v in values)
                or values[0] >= values[2] or values[1] >= values[3]):
            return []
        regions.append(values)
    if not any(r[0] > .5 and r[1] < .9 for r in regions):
        return []  # A title/sidebar alone is not a complete-content watch.
    panes = list(STATIC_CONTENT[page])
    numeric = [p["region"] for f in screen.get("facts") or []
               if f.get("kind") in {"stat", "main_stat", "sub_stat"}
               for p in f.get("evidence") or [] if p.get("region")]
    if numeric:
        # Advanced attributes can extend further left than the basic panel.
        left = min([panes[1][0], *[r["left"] for r in numeric]])
        if left < .25:
            return []  # An unfamiliar content layout keeps periodic full reads.
        panes[1] = (max(0., left-.01), *panes[1][1:])
    extras = [r for r in regions if not any(a <= r[0] and b <= r[1] and c >= r[2] and d >= r[3]
                                           for a, b, c, d in panes)]
    return list(dict.fromkeys([*panes, *extras]))
