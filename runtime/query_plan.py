"""Accept one demand; grounding and delayed answers use its immutable plan.

This is deterministic routing, not another LLM call or evidence of game state.
The legacy helpers remain public for compatibility, but all rules live here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from types import MappingProxyType

TOPIC_WORDS = MappingProxyType({
    "skill": ("技能", "战技", "终结技", "天赋", "行迹", "星魂"),
    "equipment": ("装备", "遗器", "光锥", "词条", "套装"),
    "quest": ("任务", "目标", "接下来", "干嘛", "干吗", "做啥"),
    "attributes": ("属性", "面板", "攻击力", "速度", "暴击", "培养", "等级"),
    "location": ("地点", "位置", "地区", "在哪", "哪里", "哪儿"),
    "enemy": ("敌人", "怪物", "弱点", "机制"),
    "dialogue": ("对话", "剧情", "台词", "说什么"),
    "resource": ("星琼", "专票", "通票", "信用点"),
    "visible_text": ("页面文字", "界面文字", "写的什么", "写了什么", "什么内容", "页面说明", "页面内容", "界面内容"),
})
ENTITY_TOPICS = MappingProxyType({
    "skill": frozenset({"character_skill", "character_trace", "character_rank"}),
    "equipment": frozenset({"light_cone", "relic_set", "item"}),
})
SCREEN_FIELDS = ("page", "location", "quest", "characters", "controlled_character",
                 "visible_text", "skill", "equipment", "attributes", "enemy", "dialogue", "resource")
RESOURCE_LABELS = MappingProxyType({"星琼": "星琼", "专票": "星轨专票", "通票": "星轨通票", "信用点": "信用点"})
SEMANTIC_INTENTS = frozenset({"read_screen", "explain_page", "assess_relic", "assess_cone", "assess_panel",
                              "equipment_guide", "build_guide", "team_guide", "challenge_guide", "continue_consultation",
                              "build_priority", "build_explain", "build_howto", "stat_threshold", "stat_value"})
_SUBJECT_GUIDES = frozenset({"equipment_guide", "build_guide"})
_SEMANTIC_FIELDS = MappingProxyType({
    "explain_page": frozenset({"page", "visible_text"}),
    "assess_relic": frozenset({"equipment"}), "assess_cone": frozenset({"equipment"}),
    "assess_panel": frozenset({"attributes"}),
})

_RELIC_ROWS_QUESTION = re.compile(
    r"(?:请|帮我|麻烦)?(?:只|完整|逐项)?(?:读(?:出)?|报(?:出)?|列(?:出)?|告诉我)?\s*"
    r"(?:当前|这件|这个)?(?:遗器)?(?:的)?\s*"
    r"(?:主副词条|主词条(?:和|及|、|与)\s*(?:四项|四条|4项|4条)?副词条|主词条|副词条)"
    r"\s*(?:分别)?\s*(?:是什么|有哪些|是多少|多少|是什么数值)?[？?。！!\s]*"
)


def asks_relic_rows_only(query: str) -> bool:
    """Recognize an isolated row read when the tool picked item assessment."""
    return _RELIC_ROWS_QUESTION.fullmatch(query.strip()) is not None


def _page_purpose(query):
    """Legacy fallback only. The semantic entry never reparses its purpose."""
    return bool(re.search(r"(?:这个|这|当前|现在的|眼前的)(?:页面|界面|页)", query)
                and re.search(r"干[嘛吗]|什么用|做什么用|用来|怎么用", query))


def asks_controlled_identity(query: str) -> bool:
    compact = re.sub(r"\s+", "", str(query or ""))
    return bool(
        re.search(r"(?:现在|当前|我|正在).*(?:操控|控制|操作|用的|使用的|用).*(?:谁|哪个|角色|人物)", compact)
        or re.search(r"(?:场景里|场景中|站着的|正在用).*(?:谁|角色|人物)", compact)
    )


def asks_scene_only(query: str, intent: str = "conversation") -> bool:
    compact = re.sub(r"\s+", "", str(query or ""))
    location = bool(re.fullmatch(
        r"(?:喵|请|你看看|看看|告诉我)?(?:我|我们|咱们)?(?:现在|当前|这会儿)?"
        r"(?:在(?:哪|哪里|哪儿)|到哪(?:里|儿)?了)(?:呢|啊|呀|喵|哇|嘛|吧|哦)*[？?！!（）()，,。.]*", compact))
    return bool(
        (location or intent == "game_state" or bool(re.search(r"啥(?:页面|界面|场景)|(?:这是|现在是|当前是)?哪一页", compact))
         or any(word in compact for word in (
            "什么界面", "哪个界面", "什么页面", "哪个页面", "什么场景", "哪个场景", "在干什么", "在做什么")))
        and not any(word in compact for word in ("谁", "角色", "人物", "名字", "队员", "头像"))
    )


def asks_screen_identity(query: str, intent: str = "conversation") -> bool:
    compact = re.sub(r"\s+", "", str(query or "")).lower()
    followup = compact.rstrip("？?！!（）()啊呀呢哦吧哇嘛喵，,。.")
    return bool(
        asks_controlled_identity(query) or asks_scene_only(query, intent)
        or followup in {"现在", "再看看", "你再看看", "看看现在", "看到了吗", "看见了吗", "还少一个", "现在是什么界面", "现在什么界面"}
        or intent == "game_state"
        or intent == "character_identity" and any(word in compact for word in ("现在", "当前", "画面", "屏幕", "截图", "这个", "这是谁"))
        or any(word in compact for word in ("画面", "屏幕", "截图", "现在", "当前", "眼前"))
        and any(word in compact for word in ("角色", "是谁", "人物", "名字", "谁在"))
        or bool(re.search(r"(?:这个|这位|识别|认一下).*(?:角色|人物|是谁|谁)", compact))
        or any(word in compact for word in ("右侧", "右边", "左侧", "左边", "队伍栏", "头像"))
        and any(word in compact for word in ("角色", "人物", "谁", "名字", "头像"))
        or compact.rstrip("？?啊呀呢哦") in {"还有谁", "还有哪个角色", "其他人是谁", "另外几个是谁"}
        or any(word in compact for word in ("界面", "页面", "场景", "配队", "编队", "队伍", "卡池", "养成", "图鉴", "支援", "试用"))
        and any(word in compact for word in ("谁", "名字", "哪些角色", "哪位"))
    )


def asks_screen_content(query: str) -> bool:
    compact = re.sub(r"\s+", "", str(query or ""))
    if (any(word in compact for word in RESOURCE_LABELS) and "我" in compact
            and any(word in compact for word in ("有多少", "有几张", "剩多少", "剩几张"))):
        return True
    if re.fullmatch(r"(?:现在|当前|这会儿)?(?:有|是|在做)?(?:什么|哪个|哪些)任务[？?啊呀呢嘛。.]*", compact):
        return True
    if re.fullmatch(r"(?:请|你看看|看看|告诉我)?(?:我|我们|咱们)?(?:现在|当前|这会儿)?"
                    r"(?:在(?:哪|哪里|哪儿)|到哪(?:里|儿)?了)(?:呢|啊|呀|喵|哇|嘛|吧|哦)*[？?！!（）()，,。.]*", compact):
        return False
    deictic = any(word in compact for word in ("这个", "这件", "这套", "这把", "这段", "这些", "这里", "当前", "现在", "画面", "屏幕", "眼前", "接下来"))
    return deictic and (any(word in compact for words in TOPIC_WORDS.values() for word in words)
                       or any(word in compact for word in ("什么意思", "怎么样", "适合", "解释一下", "看一下")))


def asks_navigation_only(query: str) -> bool:
    topics = {topic for topic, words in TOPIC_WORDS.items() if any(word in query for word in words)}
    return bool(asks_screen_content(query) and topics and topics <= {"quest", "location"}
                and not any(word in query for word in ("角色", "人物", "谁", "名字", "头像", "队伍", "队员")))


@dataclass(frozen=True)
class QueryPlan:
    query: str
    intent: str
    screen: bool
    identity: bool
    content: bool
    scene_only: bool
    navigation: bool
    controlled: bool
    topics: frozenset[str]
    fields: frozenset[str]
    equipment_kinds: frozenset[str] = frozenset()
    planning_source: str = "legacy_keywords"
    response_detail: str = ""
    archived_guidance: bool = False

    def with_fields(self, fields, *, equipment_kinds=None):
        """Derive execution switches from ONE accepted field set.

        No semantic classification, capture, proof relaxation or history lookup.
        Guidance and structured/legacy callers share this same projection.
        """
        fields = frozenset(fields)
        scene = bool(fields) and fields <= {"page", "location"}
        navigation = "quest" in fields and fields <= {"page", "location", "quest"}
        return replace(self, fields=fields, screen=bool(fields),
            identity=bool(fields & {"page", "characters", "controlled_character"}),
            controlled="controlled_character" in fields, scene_only=scene, navigation=navigation,
            content=bool(fields - {"page", "characters", "controlled_character"}) and not scene,
            topics=frozenset(fields & TOPIC_WORDS.keys()),
            equipment_kinds=(self.equipment_kinds if equipment_kinds is None else frozenset(equipment_kinds))
                            if "equipment" in fields else frozenset())

    @property
    def required_equipment_kinds(self) -> frozenset[str]:
        if "equipment" not in self.fields:
            return frozenset()
        if self.equipment_kinds:
            return self.equipment_kinds
        if self.intent == "assess_cone":
            return frozenset()
        if self.response_detail == "full":
            return frozenset({"main_stat", "sub_stat"})
        if "主副词条" in self.query or "主词条" in self.query and "副词条" in self.query:
            return frozenset({"main_stat", "sub_stat"})
        if "主词条" in self.query:
            return frozenset({"main_stat"})
        if "副词条" in self.query:
            return frozenset({"sub_stat"})
        return frozenset({"main_stat", "sub_stat"}) if "词条" in self.query else frozenset()

    @property
    def required_resource_labels(self) -> frozenset[str]:
        return frozenset(label for word, label in RESOURCE_LABELS.items() if word in self.query)

    @property
    def requires_quest_objective(self) -> bool:
        return any(word in self.query for word in ("接下来", "做什么", "做啥", "干嘛", "干吗", "任务目标", "怎么做"))

    def refresh_args(self) -> dict:
        # Legacy OCR API's require_characters also selects the full read path.
        # Keep that contract while avoiding roster dependencies in readiness.
        return {"require_characters": not (self.scene_only or self.navigation),
                "require_content": self.content and not self.navigation,
                "require_navigation": self.navigation}

    def public(self) -> dict:
        # No query text, guessed names, paths, or observed facts.
        result = {"screen": self.screen, "fields": sorted(self.fields), "topics": sorted(self.topics),
                  "planning_source": self.planning_source,
                  "evidence_source": "current_screen" if self.screen else
                                     "saved_or_reference" if self.archived_guidance else "optional_context",
                  "read_path": "navigation" if self.navigation else "scene" if self.scene_only else "full" if self.screen else "knowledge"}
        if self.required_equipment_kinds:
            result["equipment_kinds"] = sorted(self.required_equipment_kinds)
        return result

    def relevant_facts(self, facts: list[dict]) -> list[dict]:
        topics = self.topics
        relevant = [f for f in facts if not topics or f.get("scope") in topics
                or "quest" in topics and f.get("kind") == "quest_destination"
                or "equipment" in topics and f.get("scope") == "attributes"
                or topics & {"skill", "equipment", "attributes"} and f.get("scope") == "progression"
                or f.get("kind") == "entity_label" and any(f.get("entity_type") in ENTITY_TOPICS.get(t, ()) for t in topics)]
        labels = self.required_resource_labels
        return [f for f in relevant if f.get("scope") != "resource" or not labels or f.get("label") in labels]


def build_query_plan(query: str, intent: str = "conversation", *, screen_fields=None, response_detail=None) -> QueryPlan:
    if screen_fields is not None and (not isinstance(screen_fields, (list, tuple))
                                     or any(not isinstance(f, str) or f not in SCREEN_FIELDS for f in screen_fields)):
        raise ValueError("screen_fields 必须是受支持的当前画面字段列表")
    if response_detail is not None and (not isinstance(response_detail, str) or response_detail not in {"brief", "full"}):
        raise ValueError("response_detail 必须是 brief 或 full")
    explicit = frozenset(screen_fields or ())
    query = str(query or "")
    empty = QueryPlan(query, intent, False, False, False, False, False, False, frozenset(), frozenset(),
                      response_detail=response_detail or "")
    if intent in SEMANTIC_INTENTS:
        required = _SEMANTIC_FIELDS.get(intent, frozenset())
        if intent == "read_screen" and not explicit:
            raise ValueError("read_screen 必须提供 screen_fields")
        if explicit and (required and not required <= explicit or not required and intent not in {"read_screen", "team_guide", "challenge_guide", *_SUBJECT_GUIDES}
                         or intent in _SUBJECT_GUIDES and not explicit <= {"characters"}
                         or intent in {"team_guide", "challenge_guide"} and not explicit <= {"visible_text"}):
            raise ValueError("screen_fields 与明确的 intent 不一致；请明确需求，不回退猜测")
        fields = explicit or required
        kinds = {"main_stat", "sub_stat"} if intent == "assess_relic" else frozenset()
        return replace(empty, planning_source="semantic_intent").with_fields(fields, equipment_kinds=kinds)
    if explicit:
        # Legacy field-bearing callers already supplied a demand. Do not union
        # unrelated raw-word topics into it. Guidance may refine old hints,
        # but new semantic intents above bypass that compatibility path.
        return replace(empty, planning_source="structured_fields").with_fields(explicit)
    identity = asks_screen_identity(query, intent)
    content = asks_screen_content(query)
    scene_only = asks_scene_only(query, intent) and not content
    controlled = asks_controlled_identity(query) or "controlled_character" in explicit
    screen = identity or content or bool(explicit)
    topics = {t for t, words in TOPIC_WORDS.items() if any(word in query for word in words)} if screen else set()
    fields = set(topics) | set(explicit)
    if screen and any(word in query for word in ("界面", "页面", "场景", "在干什么", "在做什么")):
        fields.add("page")
    if controlled:
        fields.add("controlled_character")
    elif identity and not asks_scene_only(query, intent):
        fields.add("characters")
    if screen and not fields:
        fields.add("page" if scene_only else "characters")
    if _page_purpose(query):
        if not any(w in query for w in ("任务", "追踪", "导航目标")):
            fields -= {"quest"}
        fields |= {"page", "visible_text"}
    return empty.with_fields(fields)
