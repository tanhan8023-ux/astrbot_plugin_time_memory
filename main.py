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
QUIET_ON_RE = re.compile(
    r"(?:"
    r"(?:记住|以后|这个群|群里|让(?:它|他|她|你)?|叫(?:它|他|她|你)?|让bot|bot|机器人).{0,20}"
    r"(?:少说话|少回复|少发言|少吭声|安静点|别太吵|别一直说|闭嘴|少插话)"
    r"|(?:少说话|少回复|少发言|安静点|别太吵)"
    r")"
)
QUIET_OFF_RE = re.compile(
    r"(?:恢复|取消|不用|解除|关闭).{0,20}(?:少说话|少回复|安静|沉默|闭嘴|少插话)"
    r"|(?:正常回复|恢复回复|可以说话了|多说点)"
)
KEYWORD_ADD_RE = re.compile(r"^(?:添加|新增|加入|记住)(?:关键词|关键字)\s*[:：#]?\s*(.+)$")
KEYWORD_DELETE_RE = re.compile(r"^(?:删除|移除|取消|屏蔽|删掉?|去掉?)(?:关键词|关键字)\s*[:：#]?\s*(.+)$")
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
        self.trusted_user_ids = self._normalize_id_set(self.config.get("trusted_user_ids", ""))
        self.recent_message_limit = int(self.config.get("recent_message_limit", 80))
        self.keyword_extract_interval = int(self.config.get("keyword_extract_interval", 30))
        self.auto_keyword_limit_per_group = int(self.config.get("auto_keyword_limit_per_group", 50))
        self.quiet_reply_probability = float(self.config.get("quiet_reply_probability", 0.18))
        self.default_group_mode = str(self.config.get("default_group_mode") or "normal")
        self.panel_keywords = self._normalize_panel_mapping(self.config.get("panel_keywords", ""))
        self.panel_deleted_keywords = self._normalize_panel_mapping(self.config.get("panel_deleted_keywords", ""))

        self.memory_path = self.data_dir / "group_memory.json"
        self.keywords_path = self.data_dir / "group_keywords.json"
        self.rules_path = self.data_dir / "group_rules.json"

        self.group_memory: dict[str, dict] = _safe_json_load(self.memory_path, {})
        self.group_keywords: dict[str, dict] = _safe_json_load(self.keywords_path, {})
        self.group_rules: dict[str, dict] = _safe_json_load(self.rules_path, {})
        self.extracting_groups: set[str] = set()
        self.speaker_aliases: dict[str, dict[str, str]] = {}
        self.pending_keyword_hits: dict[str, str] = {}
        if self._sanitize_group_memory():
            self._save_memory()

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

        items = self._sorted_keyword_items(group_id)[:30]
        lines = ["当前群关键词:"]
        for index, item in enumerate(items, start=1):
            mark = "手动" if item.get("manual") else "自动"
            heat = float(item.get("heat", 0))
            count = int(item.get("occurrences", 0))
            lines.append(f"{index}. {item.get('keyword')} ({mark}, 热度 {heat:.2f}, {count} 次)")
        lines.append("删除可用: /删除关键词 2 或 /删除关键词 <词>")
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

        keyword_arg = self._extract_command_arg(event, {"delete_keyword", "删除关键词"})
        keyword = self._resolve_keyword_arg(group_id, keyword_arg)
        if not keyword:
            yield event.plain_result("用法: /删除关键词 <编号或词>")
            return

        removed = self._delete_keyword(group_id, keyword)
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
        quiet_updated_at = self._format_ts(rules.get("quiet_updated_at"))
        quiet_updated_by = rules.get("quiet_updated_by") or "无记录"
        yield event.plain_result(
            "当前群规则\n"
            f"少说话: {quiet}\n"
            f"少说话更新时间: {quiet_updated_at}\n"
            f"少说话设置人: {quiet_updated_by}\n"
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
        active_count = self._active_count(memory)
        yield event.plain_result(
            "当前群记忆\n"
            f"近期消息: {recent_count}\n"
            f"活跃人数估计: {active_count}\n"
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

        handled = await self._handle_natural_rule_command(event, group_id, text)
        if handled:
            return

        matched = self._match_keyword(group_id, text)
        if matched:
            self.pending_keyword_hits[event.unified_msg_origin] = matched

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

        keyword_hit = self.pending_keyword_hits.pop(event.unified_msg_origin, "")
        if not keyword_hit:
            keyword_hit = self._peek_keyword(group_id, self._message_text(event))
        runtime_context = self._build_runtime_context(group_id, keyword_hit=keyword_hit)
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

    def _format_ts(self, ts: Any) -> str:
        try:
            ts_float = float(ts)
        except Exception:
            return "无记录"
        if ts_float <= 0:
            return "无记录"
        try:
            tz = ZoneInfo(self.timezone_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        dt = datetime.fromtimestamp(ts_float, tz)
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"

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

    def _normalize_id_set(self, raw: Any) -> set[str]:
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return set()
            try:
                decoded = json.loads(text)
            except Exception:
                decoded = re.split(r"[,，、\s\n]+", text)
            raw = decoded
        if isinstance(raw, (list, tuple, set)):
            return {str(x).strip() for x in raw if str(x).strip()}
        if raw:
            return {str(raw).strip()}
        return set()

    def _normalize_panel_mapping(self, raw: Any) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return result
            try:
                raw = json.loads(text)
            except Exception:
                result = self._parse_panel_lines(text)
                return result
        if isinstance(raw, dict):
            items = raw.items()
        elif isinstance(raw, list):
            items = []
            for entry in raw:
                if isinstance(entry, dict):
                    group_id = entry.get("group_id") or entry.get("group") or "*"
                    keywords = entry.get("keywords") or entry.get("words") or []
                    items.append((group_id, keywords))
        else:
            items = []

        for group_id, keywords in items:
            key = str(group_id or "*")
            if isinstance(keywords, str):
                parts = re.split(r"[,，、\n]+", keywords)
            elif isinstance(keywords, list):
                parts = keywords
            else:
                continue
            cleaned = {self._normalize_keyword(str(x)) for x in parts}
            cleaned.discard("")
            if cleaned:
                result[key] = cleaned
        return result

    def _parse_panel_lines(self, text: str) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        current_group = "*"
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            group_match = re.match(r"^(?:群|group)\s*[:：]\s*(.+)$", line, re.I)
            if group_match:
                current_group = group_match.group(1).strip() or "*"
                result.setdefault(current_group, set())
                continue
            inline_group = re.match(r"^(.+?)\s*[:：]\s*(.+)$", line)
            if inline_group and re.search(r"\d|\*", inline_group.group(1)):
                group = inline_group.group(1).strip() or "*"
                words_text = inline_group.group(2)
            else:
                group = current_group
                words_text = line
            words = {
                word
                for word in (self._normalize_keyword(x) for x in re.split(r"[,，、;；\n]+", words_text))
                if word
            }
            if words:
                result.setdefault(group, set()).update(words)
        return result

    def _panel_words_for_group(self, mapping: dict[str, set[str]], group_id: str) -> set[str]:
        words = set(mapping.get("*", set()))
        words.update(mapping.get(group_id, set()))
        return words

    def _ensure_memory(self, group_id: str) -> dict:
        memory = self.group_memory.setdefault(group_id, {})
        memory.setdefault("recent_messages", [])
        memory.setdefault("active_count_window", [])
        memory.setdefault("total_messages", 0)
        memory.setdefault("message_count_since_extract", 0)
        memory.setdefault("summary", "")
        memory.setdefault("updated_at", 0)
        return memory

    def _sanitize_group_memory(self) -> bool:
        changed = False
        for memory in self.group_memory.values():
            if not isinstance(memory, dict):
                continue
            if "active_users" in memory:
                memory.pop("active_users", None)
                changed = True
            sanitized = []
            for msg in memory.get("recent_messages", []) or []:
                if not isinstance(msg, dict):
                    continue
                clean_msg = {
                    "time": msg.get("time", _now_ts()),
                    "speaker": msg.get("speaker") or "群友",
                    "content": _clean_text(str(msg.get("content") or ""), 300),
                }
                if clean_msg != msg:
                    changed = True
                sanitized.append(clean_msg)
            if sanitized != memory.get("recent_messages", []):
                changed = True
            memory["recent_messages"] = sanitized[-self.recent_message_limit:]
            memory.setdefault("active_count_window", [])
        return changed

    def _ensure_keyword_bucket(self, group_id: str) -> dict:
        bucket = self.group_keywords.setdefault(group_id, {})
        bucket.setdefault("keywords", {})
        return bucket

    def _get_keywords(self, group_id: str) -> dict:
        keywords = self._ensure_keyword_bucket(group_id)["keywords"]
        self._apply_panel_keywords(group_id, keywords)
        return keywords

    def _keyword_count(self, group_id: str) -> int:
        return len(self._get_keywords(group_id)) if group_id else 0

    def _sorted_keyword_items(self, group_id: str) -> list[dict]:
        return sorted(
            self._get_keywords(group_id).values(),
            key=lambda x: (bool(x.get("manual")), float(x.get("heat", 0)), int(x.get("occurrences", 0))),
            reverse=True,
        )

    def _resolve_keyword_arg(self, group_id: str, raw_arg: str) -> str:
        arg = _clean_text(raw_arg, 60).strip("#＃ ")
        if not arg:
            return ""
        items = self._sorted_keyword_items(group_id)
        if arg.isdigit():
            index = int(arg)
            if 1 <= index <= len(items):
                return str(items[index - 1].get("keyword") or "")
        normalized = self._normalize_keyword(arg)
        keywords = self._get_keywords(group_id)
        if normalized in keywords:
            return normalized
        matches = [
            key for key in keywords
            if normalized and (normalized in key or key in normalized)
        ]
        if len(matches) == 1:
            return matches[0]
        return normalized

    def _apply_panel_keywords(self, group_id: str, keywords: dict) -> None:
        deleted_words = set(self._ensure_rules(group_id).get("deleted_keywords", {}).keys())
        deleted_words.update(self._panel_words_for_group(self.panel_deleted_keywords, group_id))
        for keyword in deleted_words:
            keywords.pop(keyword, None)
        for keyword in self._panel_words_for_group(self.panel_keywords, group_id):
            if keyword in deleted_words:
                continue
            if keyword not in keywords:
                keywords[keyword] = {
                    "keyword": keyword,
                    "source_summary": "AstrBot 面板配置添加",
                    "occurrences": 0,
                    "heat": 1.0,
                    "first_seen": _now_ts(),
                    "last_seen": _now_ts(),
                    "manual": True,
                    "panel": True,
                }

    def _ensure_rules(self, group_id: str) -> dict:
        rules = self.group_rules.setdefault(group_id, {})
        rules.setdefault("quiet", self.default_group_mode == "quiet")
        rules.setdefault("deleted_keywords", {})
        return rules

    def _record_group_message(self, event: AstrMessageEvent, group_id: str, text: str) -> None:
        memory = self._ensure_memory(group_id)
        user_id = str(event.get_sender_id() if hasattr(event, "get_sender_id") else "")
        speaker = self._speaker_alias(group_id, user_id)
        now = _now_ts()

        memory["recent_messages"].append({
            "time": now,
            "speaker": speaker,
            "content": text[:300],
        })
        while len(memory["recent_messages"]) > self.recent_message_limit:
            memory["recent_messages"].pop(0)

        active_window = memory.setdefault("active_count_window", [])
        active_window.append({"speaker": speaker, "time": now})
        memory["active_count_window"] = [
            item for item in active_window
            if now - float(item.get("time", now)) <= 300
        ][-self.recent_message_limit:]
        memory["total_messages"] = int(memory.get("total_messages", 0)) + 1
        memory["message_count_since_extract"] = int(memory.get("message_count_since_extract", 0)) + 1
        memory["updated_at"] = now
        self._save_memory()

    def _speaker_alias(self, group_id: str, user_id: str) -> str:
        group_aliases = self.speaker_aliases.setdefault(group_id, {})
        key = user_id or f"anonymous-{len(group_aliases) + 1}"
        if key not in group_aliases:
            group_aliases[key] = f"群友{len(group_aliases) + 1}"
        return group_aliases[key]

    def _active_count(self, memory: dict) -> int:
        now = _now_ts()
        active_window = memory.get("active_count_window") or []
        speakers = {
            item.get("speaker")
            for item in active_window
            if item.get("speaker") and now - float(item.get("time", now)) <= 300
        }
        if speakers:
            return len(speakers)
        return 0

    async def _handle_natural_rule_command(self, event: AstrMessageEvent, group_id: str, text: str) -> bool:
        add_match = KEYWORD_ADD_RE.match(text.strip())
        if add_match:
            if not self._is_trusted(event):
                await event.send(event.plain_result("只有白名单用户可以添加关键词。"))
                event.stop_event()
                return True
            keyword = self._normalize_keyword(add_match.group(1))
            if not keyword:
                await event.send(event.plain_result("没看清要添加哪个关键词。"))
                event.stop_event()
                return True
            self._upsert_keyword(
                group_id,
                keyword,
                source_summary="白名单用户自然语言添加",
                heat=1.0,
                manual=True,
            )
            self._save_keywords()
            await event.send(event.plain_result(f"已添加关键词: {keyword}"))
            event.stop_event()
            return True

        delete_match = KEYWORD_DELETE_RE.match(text.strip())
        if delete_match:
            if not self._is_trusted(event):
                await event.send(event.plain_result("只有白名单用户可以删除关键词。"))
                event.stop_event()
                return True
            keyword = self._resolve_keyword_arg(group_id, delete_match.group(1))
            if not keyword:
                await event.send(event.plain_result("没看清要删除哪个关键词。"))
                event.stop_event()
                return True
            removed = self._delete_keyword(group_id, keyword)
            if removed:
                await event.send(event.plain_result(f"已删除关键词: {keyword}"))
            else:
                await event.send(event.plain_result(f"关键词库里没有「{keyword}」，但我已记录短期不要自动加入它。"))
            event.stop_event()
            return True

        rules = self._ensure_rules(group_id)
        if QUIET_ON_RE.search(text):
            if not self._is_trusted(event):
                await event.send(event.plain_result("只有白名单用户可以修改少说话规则。"))
                event.stop_event()
                return True
            self._set_quiet_rule(event, group_id, True, text)
            self._save_rules()
            await event.send(event.plain_result("记住了，这个群我会少说话。"))
            event.stop_event()
            return True
        if QUIET_OFF_RE.search(text):
            if not self._is_trusted(event):
                await event.send(event.plain_result("只有白名单用户可以修改少说话规则。"))
                event.stop_event()
                return True
            self._set_quiet_rule(event, group_id, False, text)
            self._save_rules()
            await event.send(event.plain_result("好，这个群我恢复正常回复。"))
            event.stop_event()
            return True
        return False

    def _set_quiet_rule(self, event: AstrMessageEvent, group_id: str, quiet: bool, source_text: str) -> None:
        rules = self._ensure_rules(group_id)
        rules["quiet"] = quiet
        rules["quiet_updated_at"] = _now_ts()
        rules["quiet_updated_by"] = "白名单用户"
        rules.pop("quiet_updated_by_name", None)
        rules["quiet_source"] = _clean_text(source_text, 120)

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
                name = self._message_speaker(msg)
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
        self._ensure_rules(group_id).setdefault("deleted_keywords", {}).pop(keyword, None)
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

    def _delete_keyword(self, group_id: str, keyword: str) -> bool:
        removed = self._get_keywords(group_id).pop(keyword, None)
        rules = self._ensure_rules(group_id)
        rules.setdefault("deleted_keywords", {})[keyword] = _now_ts()
        self._save_keywords()
        self._save_rules()
        return removed is not None

    def _is_deleted_keyword(self, group_id: str, keyword: str) -> bool:
        if keyword in self._panel_words_for_group(self.panel_deleted_keywords, group_id):
            return True
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

    def _build_runtime_context(self, group_id: str, keyword_hit: str = "") -> str:
        memory = self._ensure_memory(group_id)
        keywords = list(self._get_keywords(group_id).keys())[:20]
        rules = self._ensure_rules(group_id)
        sections = [
            "【给系尔的场景辅助】\n"
            "这些信息只用于判断群聊节奏和是否接话，不要复述标签，不要解释规则，不要主动提蛇妖、人设或主人。"
        ]
        sections.append(f"【当前时间】\n{self._format_current_time()}")
        if memory.get("summary"):
            sections.append(f"【群聊近期摘要】\n{memory['summary']}")
        atmosphere = self._build_group_atmosphere(memory)
        if atmosphere:
            sections.append(f"【群聊场景】\n{atmosphere}")
        strategy_lines = []
        if rules.get("quiet"):
            strategy_lines.append("这个群设置了少说话：像系尔那样保持低存在感，能不展开就不展开，能一句说清就一句。")
        if keyword_hit:
            strategy_lines.append(f"当前消息命中了群关键词「{keyword_hit}」：可以自然接一句，优先给关键答案；不要说自己命中了关键词。")
        strategy_lines.append("技术/小手机相关问题先给关键答案；普通闲聊保持温和克制，不追问、不说教、不硬找话题。")
        if strategy_lines:
            sections.append("【系尔接话策略】\n" + "\n".join(strategy_lines))
        if keywords:
            sections.append("【群关键词】\n" + "、".join(keywords))
        recent = self._format_recent_messages(group_id, limit=6)
        if recent:
            sections.append("【最近群聊】\n" + recent)
        return "<time_memory_context>\n" + "\n\n".join(sections) + "\n</time_memory_context>"

    def _build_group_atmosphere(self, memory: dict) -> str:
        recent = memory.get("recent_messages", [])
        now = _now_ts()
        recent_minute = [
            msg for msg in recent
            if now - float(msg.get("time", now)) <= 60
        ]
        message_rate = len(recent_minute)
        active_count = self._active_count(memory)
        if message_rate >= 12:
            mood = "热闹"
            desc = "群里现在比较热闹，系尔不适合抢话，短一点接住重点就好。"
        elif message_rate <= 1:
            mood = "安静"
            desc = "群里比较安静，可以自然回应，但不要硬找话题。"
        else:
            mood = "正常"
            desc = "群里聊天节奏正常。"
        return f"{desc}氛围: {mood}。近1分钟消息数: {message_rate}。近5分钟活跃人数估计: {active_count}。"

    def _format_recent_messages(self, group_id: str, limit: int = 8) -> str:
        memory = self._ensure_memory(group_id)
        lines = []
        for msg in memory.get("recent_messages", [])[-limit:]:
            name = self._message_speaker(msg)
            content = _clean_text(msg.get("content", ""), 90)
            if content:
                lines.append(f"{name}: {content}")
        return "\n".join(lines)

    def _message_speaker(self, msg: dict) -> str:
        speaker = str(msg.get("speaker") or "").strip()
        if speaker:
            return speaker
        # Compatibility for old persisted data; avoid exposing stored IDs/nicknames.
        return "群友"

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
