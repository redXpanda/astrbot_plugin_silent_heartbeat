from __future__ import annotations

import asyncio
import importlib
import json
import time
from contextlib import suppress
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register


@register(
    name="astrbot_plugin_silent_heartbeat",
    author="local",
    desc="A memory-aware heartbeat that defaults to silence.",
    version="0.2.0",
)
class SilentHeartbeatPlugin(Star):
    """Run authorized memory reviews and send only validated actions."""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_sent_at: dict[str, float] = {}

    async def initialize(self) -> None:
        """Start the configured heartbeat worker."""
        if not self._enabled() or not self._private_session_id():
            logger.info("[silent_heartbeat] disabled because enabled or private_session_id is not configured")
            return
        self._task = asyncio.create_task(self._run_loop())

    async def terminate(self) -> None:
        """Stop the heartbeat worker before the plugin is unloaded."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[silent_heartbeat] heartbeat execution failed")
            await asyncio.sleep(max(1, self._interval_minutes()) * 60)

    async def _run_once(self) -> None:
        """Review authorized memory domains and perform one validated action."""
        if not self._enabled():
            return
        bridge = self._memory_bridge()
        if bridge is None:
            logger.warning("[silent_heartbeat] MemoryCompanion bridge is unavailable; skipping heartbeat")
            return
        domains = self._authorized_domains()
        if not domains:
            logger.warning("[silent_heartbeat] no authorized memory domains are configured")
            return
        contexts = await self._compose_memory_contexts(bridge, domains)
        if not contexts:
            logger.debug("[silent_heartbeat] no authorized memory context is available")
            return
        provider = self._provider()
        if provider is None:
            logger.warning("[silent_heartbeat] no LLM provider is available")
            return
        response = await provider.text_chat(
            prompt=self._prompt(contexts, domains),
            system_prompt=str(self.config.get("system_prompt", "")).strip(),
        )
        decision = self._parse_decision(getattr(response, "completion_text", ""))
        if decision is None or decision["action"] == "silent":
            logger.debug("[silent_heartbeat] decision is silent")
            return
        target = domains.get(decision["target"])
        if target is None or target["kind"] != decision["action"]:
            logger.warning("[silent_heartbeat] rejected an unauthorized heartbeat target")
            return
        if self._in_cooldown(target["session_id"]):
            logger.debug("[silent_heartbeat] target is in cooldown: %s", decision["target"])
            return
        await self.context.send_message(target["session_id"], MessageChain().message(decision["message"]))
        self._last_sent_at[target["session_id"]] = time.monotonic()
        logger.info("[silent_heartbeat] sent %s action to %s", decision["action"], decision["target"])

    def _memory_bridge(self) -> Any | None:
        """Return the active public MemoryCompanion bridge when available."""
        for module_name in (
            "data.plugins.astrbot_plugin_memory_companion.main",
            "astrbot_plugin_memory_companion.main",
        ):
            try:
                module = importlib.import_module(module_name)
                bridge = getattr(module, "get_active_bridge")()
            except Exception:
                continue
            if bridge is not None and callable(getattr(bridge, "compose_context", None)):
                return bridge
        return None

    async def _compose_memory_contexts(self, bridge: Any, domains: dict[str, dict[str, str]]) -> list[str]:
        """Read each authorized domain separately to preserve memory ACL boundaries."""
        contexts: list[str] = []
        for key, domain in domains.items():
            try:
                text = await asyncio.wait_for(
                    bridge.compose_context(
                        query="当前是否存在值得主动处理的未完成话题、承诺、风险、提醒或自然延续？",
                        session_context={
                            "session_id": domain["session_id"],
                            "scope": domain["kind"],
                            "platform": domain["session_id"].split(":", 1)[0],
                            "group_id": domain.get("group_id", ""),
                            "message_text": "心跳主动决策",
                            "strict_session_only": True,
                        },
                        top_k=self._context_top_k(),
                        max_chars=self._context_max_chars(),
                    ),
                    timeout=self._context_timeout_seconds(),
                )
            except Exception:
                logger.debug("[silent_heartbeat] memory context read failed: %s", key, exc_info=True)
                continue
            if text := str(text or "").strip():
                contexts.append(f"[{key}]\n{text}")
        return contexts

    def _provider(self) -> Any | None:
        provider_id = str(self.config.get("provider_id", "")).strip()
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider(umo=self._private_session_id())

    def _authorized_domains(self) -> dict[str, dict[str, str]]:
        """Build private and group targets from explicit configuration only."""
        domains: dict[str, dict[str, str]] = {}
        private_session_id = self._private_session_id()
        if private_session_id:
            domains["private"] = {"kind": "private", "session_id": private_session_id}
        group_sessions = self.config.get("authorized_group_session_ids", self.config.get("group_sessions", []))
        if not isinstance(group_sessions, list):
            return domains
        for item in group_sessions:
            session_id = str(item or "").strip()
            if ":GroupMessage:" not in session_id:
                continue
            group_id = session_id.rsplit(":", 1)[-1].strip()
            if not group_id:
                continue
            domains[f"group:{group_id}"] = {
                "kind": "group",
                "session_id": session_id,
                "group_id": group_id,
            }
        return domains

    def _prompt(self, contexts: list[str], domains: dict[str, dict[str, str]]) -> str:
        targets = ", ".join(domains)
        return (
            "Review only the authorized memory excerpts below. Default to silence. "
            "Choose an action only when it is concrete, timely, useful, and safe to send now. "
            "Do not invent facts, obligations, or events. Do not disclose private memory to a group. "
            f"Allowed targets: {targets}.\n"
            "Return exactly one JSON object, without markdown:\n"
            '{"action":"silent","target":"","message":""}\n'
            'or {"action":"private","target":"private","message":"concise Chinese message"}\n'
            'or {"action":"group","target":"group:<configured group id>","message":"concise Chinese message"}\n\n'
            "Authorized memory excerpts:\n" + "\n\n".join(contexts)
        )

    @staticmethod
    def _parse_decision(text: Any) -> dict[str, str] | None:
        try:
            payload = json.loads(str(text or "").strip())
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        action = str(payload.get("action", "")).strip().lower()
        target = str(payload.get("target", "")).strip()
        message = str(payload.get("message", "")).strip()
        if action == "silent":
            return {"action": "silent", "target": "", "message": ""}
        if action not in {"private", "group"} or not target or not message or len(message) > 500:
            return None
        return {"action": action, "target": target, "message": message}

    def _in_cooldown(self, session_id: str) -> bool:
        return time.monotonic() - self._last_sent_at.get(session_id, 0.0) < self._target_cooldown_minutes() * 60

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def _private_session_id(self) -> str:
        return str(self.config.get("private_session_id", self.config.get("session_id", ""))).strip()

    def _interval_minutes(self) -> int:
        return self._bounded_int("interval_minutes", 60, 1, 1440)

    def _target_cooldown_minutes(self) -> int:
        return self._bounded_int("target_cooldown_minutes", 180, 1, 10080)

    def _context_top_k(self) -> int:
        return self._bounded_int("context_top_k", 5, 1, 10)

    def _context_max_chars(self) -> int:
        return self._bounded_int("context_max_chars", 900, 240, 1800)

    def _context_timeout_seconds(self) -> float:
        try:
            return max(0.2, min(float(self.config.get("context_timeout_seconds", 3.0)), 10.0))
        except (TypeError, ValueError):
            return 3.0

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(self.config.get(key, default)), maximum))
        except (TypeError, ValueError):
            return default
