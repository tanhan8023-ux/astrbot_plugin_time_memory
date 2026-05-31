import asyncio
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - older AstrBot versions may not expose TextPart.
    TextPart = None


logger = logging.getLogger("time_memory")


WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
COMMAND_PREFIX_RE = re.compile(r"^[\s/!！]+")
TIME_QUERY_RE = re.compile(
    r"(现在几点|几点了|现在什么时间|今天几号|今天星期几|现在时间|报时|北京时间)"
)
QUIET_ON_RE = re.compile(r"(记住|以后|这个群|群里).{0,12}(少说话|少回复|安静点|别太吵)")
QUIET_OFF_RE = re.compile(r"(恢复|取消|不用).{0,12}(少说话|少回复|安静|沉默)")
MENTION_RE = re.compile(r"@\S+")


def _now_ts() -> float:
    return time.time()


def _clean_text(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _safe_json_load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("[TimeMemory] 读取数据失败: %s", path)
        return default


def _atomic_json_save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


class TimeMemoryPlugin(Star):
    """群聊时间与自动关键词记忆插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

        plugin_dir = Path(__file__).resolve().parent
        self.data_dir = plugin_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.timezone_name = str(self.config.get("timezone") or "Asia/Shanghai")
        self.trusted_user_ids = {str(x) for x in self.config.get("trusted_user_ids", [])}
        self.recent_message_limit = int(self.config.get("recent_message_limit", 80))
        self.keyword_extract_interval = int(self.config.get("keyword_extract_interval", 30))
        self.auto_keyword_limit_per_group = int(self.config.get("auto_keyword_limit_per_group", 50))
        self.quiet_reply_probability = float(self.config.get("quiet_reply_probability", 0.18))
        self.default_group_mode = str(self.config.get("default_group_mode") or "normal")

        self.memory_path = self.data_dir / "group_memory.json"
        self.keywords_path = self.data_dir / "group_keywords.json"
        self.rules_path = self.data_dir / "group_rules.json"

        self.group_memory: dict[str, dict] = _safe_json_load(self.memory_path, {})
        self.group_keywords: dict[str, dict] = _safe_json_load(self.keywords_path, {})
        self.group_rules: dict[str, dict] = _safe_json_load(self.rules_path, {})
        self.extracting_groups: set[str] = set()

        logger.info("[TimeMemory] 插件已加载，时区=%s", self.timezone_name)

    async def initialize(self):
        logger.info("[TimeMemory] 插件已启用")

    async def terminate(self):
        self._save_all()
        logger.info("[TimeMemory] 插件已停用，数据已保存")

    @filter.command("time", alias={"时间", "现在几点", "今天几号"})
    async def cmd_time(self, event: AstrMessageEvent):
        yield event.plain_result(self._format_current_time())

    @filter.command("time_memory_status")
    async def cmd_status(self, event: AstrMessageEvent):
        group_id = self._get_group_key(event)
        group_count = len(self.group_memory)
        keyword_count = self._keyword_count(group_id) if group_id else sum(
            len(v.get("keywords", {})) for v in self.group_keywords.values()
        )
        trusted_state = "已配置" if self.trusted_user_ids else "未配置"
        yield event.plain_result(
            "TimeMemory 状态\n"
            f"时区: {self.timezone_name}\n"
            f"白名单: {trusted_state}\n"
            f"已记录群: {group_count}\n"
            f"当前范围关键词: {keyword_count}\n"
            f"关键词提取间隔: {self.keyword_extract_interval} 条消息"
        )

    @filter.command("keywords", alias={"关键词"})
    async def cmd_keywords(self, event: AstrMessageEvent):
        group_id = self._require_group(event)
        if not group_id:
            yield event.plain_result("这个命令需要在群聊里使用。")
            return

        keywords = self._get_keywords(group_id)
        if not keywords:
            yield event.plain_result("这个群还没有关键词。聊一会儿后我会自己提取，也可以用 /添加关键词 <词> 手动添加。")
            return

        items = sorted(
            keywords.values(),
            key=lambda x: (bool(x.get("manual")), float(x.get("heat", 0)), int(x.get("occurrences", 0))),
            reverse=True,
        )[:30]
        lines = ["当前群关键词:"]
        for item in items:
            mark = "手动" if item.get("manual") else "自动"
            heat = float(item.get("heat", 0))
            count = int(item.get("occurrences", 0))
            lines.append(f"- {item.get('keyword')} ({mark}, 热度 {heat:.2f}, {count} 次)")
        yield event.plain_result("\n".join(lines))

    @filter.command("add_keyword", alias={"添加关键词"})
    async def cmd_add_keyword(self, event: AstrMessageEvent):
        group_id = self._require_group(event)
        if not group_id:
            yield event.plain_result("这个命令需要在群聊里使用。")
            return
        if not self._is_trusted(event):
            yield event.plain_result("只有白名单用户可以添加关键词。")
            return

        keyword = self._extract_command_arg(event, {"add_keyword", "添加关键词"})
        keyword = self._normalize_keyword(keyword)
        if not keyword:
            yield event.plain_result("用法: /添加关键词 <词>")
            return

        self._upsert_keyword(
            group_id,
            keyword,
            source_summary="白名单用户手动添加",
            heat=1.0,
            manual=True,
        )
        self._save_keywords()
        yield event.plain_result(f"已添加关键词: {keyword}")

    @filter.command("delete_keyword", alias={"删除关键词"})
    async def cmd_delete_keyword(self, event: AstrMessageEvent):
        group_id = self._require_group(event)
        if not group_id:
            yield event.plain_result("这个命令需要在群聊里使用。")
            return
        if not self._is_trusted(event):
            yield event.plain_result("只有白名单用户可以删除关键词。")
            return

        keyword = self._extract_command_arg(event, {"delete_keyword", "删除关键词"})
        keyword = self._normalize_keyword(keyword)
        if not keyword:
            yield event.plain_result("用法: /删除关键词 <词>")
            return

        removed = self._get_keywords(group_id).pop(keyword, None)
        rules = self._ensure_rules(group_id)
        rules.setdefault("deleted_keywords", {})[keyword] = _now_ts()
        self._save_keywords()
        self._save_rules()
        if removed:
            yield event.plain_result(f"已删除关键词: {keyword}")
        else:
            yield event.plain_result(f"关键词库里没有「{keyword}」，但我已记录短期不要自动加入它。")

    @filter.command("group_rules", alias={"群规则"})
    async def cmd_group_rules(self, event: AstrMessageEvent):
        group_id = self._require_group(event)
        if not group_id:
            yield event.plain_result("这个命令需要在群聊里使用。")
            return

        rules = self._ensure_rules(group_id)
        quiet = "开启" if rules.get("quiet") else "关闭"
        keywords = self._get_keywords(group_id)
        deleted = rules.get("deleted_keywords", {})
        yield event.plain_result(
            "当前群规则\n"
            f"少说话: {quiet}\n"
            f"关键词数量: {len(keywords)}\n"
            f"删除黑名单: {len(deleted)}\n"
            "管理: /添加关键词 <词>，/删除关键词 <词>"
        )

    @filter.command("group_memory", alias={"群记忆"})
    async def cmd_group_memory(self, event: AstrMessageEvent):
        group_id = self._require_group(event)
        if not group_id:
            yield event.plain_result("这个命令需要在群聊里使用。")
            return

        memory = self._ensure_memory(group_id)
        summary = memory.get("summary") or "还没有形成稳定摘要。"
        recent_count = len(memory.get("recent_messages", []))
        active_users = len(memory.get("active_users", {}))
        yield event.plain_result(
            "当前群记忆\n"
            f"近期消息: {recent_count}\n"
            f"活跃成员: {active_users}\n"
            f"摘要: {summary}"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        text = _clean_text(self._message_text(event), 500)
        if not text:
            return

        group_id = self._get_group_key(event)
        if not group_id:
            return

        if self._looks_like_command(text):
            return

        self._record_group_message(event, group_id, text)
        self._maybe_schedule_keyword_extract(group_id, event.unified_msg_origin)

        if TIME_QUERY_RE.search(text):
            await event.send(event.plain_result(self._format_current_time()))
            event.stop_event()
            return

        if self._is_trusted(event):
            handled = await self._handle_natural_rule_command(event, group_id, text)
            if handled:
                return

        matched = self._match_keyword(group_id, text)
        if matched:
            reply = await self._generate_keyword_reply(event, group_id, text, matched)
            if reply:
                await event.send(event.plain_result(reply))
                event.stop_event()
                return

        if self._should_quiet_stop(event, group_id, text):
            event.stop_event()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request):
        group_id = self._get_group_key(event)
        if not group_id:
            return

        if self._should_quiet_stop(event, group_id, self._message_text(event)):
            event.stop_event()
            return

        runtime_context = self._build_runtime_context(group_id)
        if runtime_context and TextPart is not None and hasattr(request, "extra_user_content_parts"):
            if request.extra_user_content_parts is None:
                request.extra_user_content_parts = []
            part = TextPart(text=runtime_context)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            request.extra_user_content_parts.append(part)

    def _format_current_time(self) -> str:
        try:
            tz = ZoneInfo(self.timezone_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        weekday = WEEKDAYS_CN[now.weekday()]
        return f"现在是 {now.year}年{now.month}月{now.day}日 {weekday} {now.hour:02d}:{now.minute:02d}。"

    def _message_text(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_message_str"):
            return event.get_message_str() or ""
        if hasattr(event, "message_str"):
            return event.message_str or ""
        msg_obj = getattr(event, "message_obj", None)
        return getattr(msg_obj, "message_str", "") or ""

    def _get_group_key(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        group_id = getattr(msg_obj, "group_id", None) if msg_obj else None
        if group_id:
            return str(group_id)
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _require_group(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)
        group_id = getattr(msg_obj, "group_id", None) if msg_obj else None
        return str(group_id) if group_id else ""

    def _is_trusted(self, event: AstrMessageEvent) -> bool:
        if not self.trusted_user_ids:
            return False
        user_id = str(event.get_sender_id() if hasattr(event, "get_sender_id") else "")
        return user_id in self.trusted_user_ids

    def _looks_like_command(self, text: str) -> bool:
        return text.strip().startswith(("/", "!", "！"))

    def _extract_command_arg(self, event: AstrMessageEvent, names: set[str]) -> str:
        text = self._message_text(event).strip()
        text = COMMAND_PREFIX_RE.sub("", text)
        for name in sorted(names, key=len, reverse=True):
            if text.startswith(name):
                return text[len(name):].strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _normalize_keyword(self, keyword: str) -> str:
        keyword = _clean_text(keyword, 30)
        keyword = keyword.strip("「」『』[]【】()（）:：,，.。!！?？")
        if len(keyword) < 2:
            return ""
        if keyword.startswith("/"):
            return ""
        return keyword

    def _ensure_memory(self, group_id: str) -> dict:
        memory = self.group_memory.setdefault(group_id, {})
        memory.setdefault("recent_messages", [])
        memory.setdefault("active_users", {})
        memory.setdefault("total_messages", 0)
        memory.setdefault("message_count_since_extract", 0)
        memory.setdefault("summary", "")
        memory.setdefault("updated_at", 0)
        return memory

    def _ensure_keyword_bucket(self, group_id: str) -> dict:
        bucket = self.group_keywords.setdefault(group_id, {})
        bucket.setdefault("keywords", {})
        return bucket

    def _get_keywords(self, group_id: str) -> dict:
        return self._ensure_keyword_bucket(group_id)["keywords"]

    def _keyword_count(self, group_id: str) -> int:
        return len(self._get_keywords(group_id)) if group_id else 0

    def _ensure_rules(self, group_id: str) -> dict:
        rules = self.group_rules.setdefault(group_id, {})
        rules.setdefault("quiet", self.default_group_mode == "quiet")
        rules.setdefault("deleted_keywords", {})
        return rules

    def _record_group_message(self, event: AstrMessageEvent, group_id: str, text: str) -> None:
        memory = self._ensure_memory(group_id)
        user_id = str(event.get_sender_id() if hasattr(event, "get_sender_id") else "")
        user_name = str(event.get_sender_name() if hasattr(event, "get_sender_name") else user_id)
        now = _now_ts()

        memory["recent_messages"].append({
            "time": now,
            "user_id": user_id,
            "user_name": user_name,
            "content": text[:300],
        })
        while len(memory["recent_messages"]) > self.recent_message_limit:
            memory["recent_messages"].pop(0)

        active = memory["active_users"].setdefault(user_id, {"name": user_name, "count": 0, "last_seen": now})
        active["name"] = user_name
        active["count"] = int(active.get("count", 0)) + 1
        active["last_seen"] = now
        memory["total_messages"] = int(memory.get("total_messages", 0)) + 1
        memory["message_count_since_extract"] = int(memory.get("message_count_since_extract", 0)) + 1
        memory["updated_at"] = now
        self._save_memory()

    async def _handle_natural_rule_command(self, event: AstrMessageEvent, group_id: str, text: str) -> bool:
        rules = self._ensure_rules(group_id)
        if QUIET_ON_RE.search(text):
            rules["quiet"] = True
            self._save_rules()
            await event.send(event.plain_result("记住了，这个群我会少说话。"))
            event.stop_event()
            return True
        if QUIET_OFF_RE.search(text):
            rules["quiet"] = False
            self._save_rules()
            await event.send(event.plain_result("好，这个群我恢复正常回复。"))
            event.stop_event()
            return True
        return False

    def _maybe_schedule_keyword_extract(self, group_id: str, umo: str) -> None:
        memory = self._ensure_memory(group_id)
        if int(memory.get("message_count_since_extract", 0)) < self.keyword_extract_interval:
            return
        if group_id in self.extracting_groups:
            return
        self.extracting_groups.add(group_id)
        asyncio.create_task(self._extract_keywords_for_group(group_id, umo))

    async def _extract_keywords_for_group(self, group_id: str, umo: str) -> None:
        try:
            memory = self._ensure_memory(group_id)
            recent = memory.get("recent_messages", [])[-self.recent_message_limit:]
            if len(recent) < 8:
                return

            chat_lines = []
            for msg in recent[-50:]:
                name = msg.get("user_name") or msg.get("user_id") or "群友"
                content = _clean_text(msg.get("content", ""), 120)
                if content:
                    chat_lines.append(f"{name}: {content}")

            system_prompt = (
                "你是群聊关键词提取器。请从聊天记录里提取适合机器人后续接话的关键词、话题词或群内常聊梗。"
                "不要提取普通语气词、单字、表情、命令、用户 ID、过于隐私的个人信息。"
                "只输出 JSON，不要解释。"
            )
            prompt = (
                "从下面群聊记录提取 3 到 8 个关键词。输出格式:\n"
                '{"summary":"一句话群聊摘要","keywords":[{"keyword":"词","reason":"为什么值得记","heat":0.0}]}\n\n'
                "群聊记录:\n" + "\n".join(chat_lines)
            )
            raw = await self._call_llm(prompt, system_prompt=system_prompt, umo=umo)
            data = self._parse_json_object(raw)
            if not isinstance(data, dict):
                return

            summary = _clean_text(str(data.get("summary") or ""), 180)
            if summary:
                memory["summary"] = summary

            added = 0
            for item in data.get("keywords") or []:
                if not isinstance(item, dict):
                    continue
                keyword = self._normalize_keyword(str(item.get("keyword") or ""))
                if not keyword or self._is_deleted_keyword(group_id, keyword):
                    continue
                reason = _clean_text(str(item.get("reason") or "LLM 自动提取"), 120)
                heat = self._coerce_float(item.get("heat"), 0.55)
                self._upsert_keyword(group_id, keyword, source_summary=reason, heat=heat, manual=False)
                added += 1

            memory["message_count_since_extract"] = 0
            self._prune_keywords(group_id)
            self._save_all()
            logger.info("[TimeMemory] 群 %s 自动提取关键词 %s 个", group_id, added)
        except Exception:
            logger.exception("[TimeMemory] 自动提取关键词失败: %s", group_id)
        finally:
            self.extracting_groups.discard(group_id)

    def _upsert_keyword(
        self,
        group_id: str,
        keyword: str,
        source_summary: str,
        heat: float,
        manual: bool,
    ) -> None:
        now = _now_ts()
        keywords = self._get_keywords(group_id)
        existing = keywords.get(keyword)
        if existing:
            existing["occurrences"] = int(existing.get("occurrences", 0)) + 1
            existing["heat"] = max(float(existing.get("heat", 0)), heat)
            existing["last_seen"] = now
            existing["source_summary"] = source_summary or existing.get("source_summary", "")
            existing["manual"] = bool(existing.get("manual")) or manual
            return

        keywords[keyword] = {
            "keyword": keyword,
            "source_summary": source_summary,
            "occurrences": 1,
            "heat": heat,
            "first_seen": now,
            "last_seen": now,
            "manual": manual,
        }

    def _is_deleted_keyword(self, group_id: str, keyword: str) -> bool:
        deleted = self._ensure_rules(group_id).get("deleted_keywords", {})
        ts = float(deleted.get(keyword, 0) or 0)
        if ts <= 0:
            return False
        # Seven days are enough to stop immediate re-adds while still allowing topics to return later.
        if _now_ts() - ts > 7 * 86400:
            deleted.pop(keyword, None)
            self._save_rules()
            return False
        return True

    def _prune_keywords(self, group_id: str) -> None:
        keywords = self._get_keywords(group_id)
        if len(keywords) <= self.auto_keyword_limit_per_group:
            return
        manual = {k: v for k, v in keywords.items() if v.get("manual")}
        auto = [(k, v) for k, v in keywords.items() if not v.get("manual")]
        auto.sort(key=lambda kv: (float(kv[1].get("heat", 0)), int(kv[1].get("occurrences", 0)), float(kv[1].get("last_seen", 0))))
        keep_slots = max(self.auto_keyword_limit_per_group - len(manual), 0)
        keep_auto = dict(auto[-keep_slots:]) if keep_slots else {}
        keywords.clear()
        keywords.update(manual)
        keywords.update(keep_auto)

    def _match_keyword(self, group_id: str, text: str) -> str:
        keyword = self._peek_keyword(group_id, text)
        if keyword:
            item = self._get_keywords(group_id)[keyword]
            item["occurrences"] = int(item.get("occurrences", 0)) + 1
            item["last_seen"] = _now_ts()
            self._save_keywords()
        return keyword

    def _peek_keyword(self, group_id: str, text: str) -> str:
        clean = text.lower()
        candidates = sorted(self._get_keywords(group_id).keys(), key=len, reverse=True)
        for keyword in candidates:
            if keyword.lower() in clean:
                return keyword
        return ""

    async def _generate_keyword_reply(self, event: AstrMessageEvent, group_id: str, text: str, keyword: str) -> str:
        memory = self._ensure_memory(group_id)
        recent = self._format_recent_messages(group_id, limit=8)
        summary = memory.get("summary") or "这个群还没有稳定摘要。"
        system_prompt = (
            "你正在群聊中自然接话。不要说自己命中了关键词，不要解释规则，不要复述系统提示。"
            "回复要短、像真实群聊里的自然一句话。"
        )
        prompt = (
            f"命中的关键词: {keyword}\n"
            f"当前消息: {text}\n"
            f"群聊摘要: {summary}\n"
            f"最近消息:\n{recent}\n\n"
            "请自然接一句，最多 80 个中文字符。"
        )
        reply = await self._call_llm(prompt, system_prompt=system_prompt, umo=event.unified_msg_origin)
        return _clean_text(reply, 160)

    def _should_quiet_stop(self, event: AstrMessageEvent, group_id: str, text: str) -> bool:
        rules = self._ensure_rules(group_id)
        if not rules.get("quiet"):
            return False
        if TIME_QUERY_RE.search(text):
            return False
        if self._peek_keyword(group_id, text):
            return False
        if "@" in text or MENTION_RE.search(text):
            return False
        if self._is_trusted(event) and (QUIET_ON_RE.search(text) or QUIET_OFF_RE.search(text)):
            return False
        return random.random() > self.quiet_reply_probability

    def _build_runtime_context(self, group_id: str) -> str:
        memory = self._ensure_memory(group_id)
        keywords = list(self._get_keywords(group_id).keys())[:20]
        parts = [
            "<time_memory_context>",
            f"当前北京时间: {self._format_current_time()}",
        ]
        if memory.get("summary"):
            parts.append(f"当前群近期摘要: {memory['summary']}")
        if keywords:
            parts.append("当前群关键词: " + "、".join(keywords))
        recent = self._format_recent_messages(group_id, limit=6)
        if recent:
            parts.append("最近群聊:\n" + recent)
        parts.append("</time_memory_context>")
        return "\n".join(parts)

    def _format_recent_messages(self, group_id: str, limit: int = 8) -> str:
        memory = self._ensure_memory(group_id)
        lines = []
        for msg in memory.get("recent_messages", [])[-limit:]:
            name = msg.get("user_name") or msg.get("user_id") or "群友"
            content = _clean_text(msg.get("content", ""), 90)
            if content:
                lines.append(f"{name}: {content}")
        return "\n".join(lines)

    async def _call_llm(self, prompt: str, system_prompt: str = "", umo: str | None = None) -> str:
        try:
            if hasattr(self.context, "llm_generate") and hasattr(self.context, "get_current_chat_provider_id") and umo:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                return self._response_text(response)

            provider = None
            if hasattr(self.context, "get_using_provider"):
                provider = self.context.get_using_provider()
            elif hasattr(self.context, "provider_manager"):
                pm = self.context.provider_manager
                if hasattr(pm, "get_using_provider"):
                    provider = pm.get_using_provider()
                elif hasattr(pm, "providers") and pm.providers:
                    provider = pm.providers[0]

            if provider is None:
                for attr_name in dir(self.context):
                    if "provider" in attr_name.lower():
                        attr = getattr(self.context, attr_name, None)
                        if attr and callable(getattr(attr, "text_chat", None)):
                            provider = attr
                            break

            if provider is None:
                logger.error("[TimeMemory] 无法获取 LLM provider")
                return ""

            if hasattr(provider, "text_chat"):
                response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
                return self._response_text(response)
            if hasattr(provider, "chat"):
                response = await provider.chat(prompt=prompt, system_prompt=system_prompt)
                return self._response_text(response)

            logger.error("[TimeMemory] Provider 没有可用聊天方法: %s", type(provider))
            return ""
        except Exception:
            logger.exception("[TimeMemory] 调用 LLM 失败")
            return ""

    def _response_text(self, response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        for attr in ("completion_text", "text", "content"):
            value = getattr(response, attr, None)
            if value:
                return str(value)
        return str(response)

    def _parse_json_object(self, raw: str) -> dict | None:
        if not raw:
            return None
        text = raw.strip()
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            logger.warning("[TimeMemory] LLM 返回不是合法 JSON: %s", raw[:300])
            return None

    def _coerce_float(self, value: Any, default: float) -> float:
        try:
            num = float(value)
        except Exception:
            return default
        return max(0.0, min(1.0, num))

    def _save_memory(self) -> None:
        _atomic_json_save(self.memory_path, self.group_memory)

    def _save_keywords(self) -> None:
        _atomic_json_save(self.keywords_path, self.group_keywords)

    def _save_rules(self) -> None:
        _atomic_json_save(self.rules_path, self.group_rules)

    def _save_all(self) -> None:
        self._save_memory()
        self._save_keywords()
        self._save_rules()
