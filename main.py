from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, StarTools, register


@register(
    name="astrbot_plugin_silent_heartbeat",
    author="local",
    desc="A memory-aware heartbeat with one private output.",
    version="0.5.0",
)
class SilentHeartbeatPlugin(Star):
    """Review authorized memories and optionally message one private session."""

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_sent_at: dict[str, float] = {}
        self._diagnostics_path = Path(StarTools.get_data_dir("astrbot_plugin_silent_heartbeat")) / "diagnostics.json"
        self._diagnostics = self._load_diagnostics()

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
        started = time.perf_counter()
        trace: dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "outcome": "unknown",
            "stage": "start",
            "domains_configured": 0,
            "domains_with_context": 0,
            "domains_without_context": 0,
            "domain_failures": 0,
            "preflight_reason": "",
            "model_decision": "",
            "review_reason": "",
            "error_type": "",
        }
        companion_api = None
        private_session_id = self._private_session_id()
        token = ""
        try:
            if not self._enabled():
                trace.update(outcome="skipped", stage="configuration", preflight_reason="plugin_disabled")
                return
            trace["stage"] = "memory_bridge"
            memory_bridge = self._memory_bridge()
            if memory_bridge is None:
                trace.update(outcome="blocked", preflight_reason="memory_companion_bridge_unavailable")
                return
            trace["stage"] = "private_companion_api"
            companion_api = self._private_companion_api()
            if companion_api is None:
                trace.update(outcome="blocked", preflight_reason="private_companion_api_unavailable")
                return
            trace["stage"] = "preflight"
            prepared = await companion_api.prepare_proactive_chat(private_session_id)
            trace["preflight_reason"] = str(prepared.get("reason", "") or "")[:160]
            if not prepared.get("enabled") or not prepared.get("allowed") or not prepared.get("token"):
                trace.update(outcome="blocked", stage="preflight")
                return
            token = str(prepared["token"])
            domains = self._memory_domains()
            trace["domains_configured"] = len(domains)
            if not domains:
                trace.update(outcome="blocked", stage="memory_domains", preflight_reason="no_authorized_memory_domains")
                return
            trace["stage"] = "memory_context"
            contexts, domain_failures = await self._compose_memory_contexts(memory_bridge, domains)
            trace["domains_with_context"] = len(contexts)
            trace["domains_without_context"] = len(domains) - len(contexts) - domain_failures
            trace["domain_failures"] = domain_failures
            if not contexts:
                trace.update(outcome="silent", preflight_reason="no_memory_context")
                return
            trace["stage"] = "provider"
            provider = self._provider()
            if provider is None:
                trace.update(outcome="blocked", preflight_reason="llm_provider_unavailable")
                return
            trace["stage"] = "model_decision"
            response = await provider.text_chat(
                prompt=self._prompt(contexts, str(prepared.get("prompt_fragment", ""))),
                system_prompt=str(self.config.get("system_prompt", "")).strip(),
            )
            decision = self._parse_decision(getattr(response, "completion_text", ""))
            if decision is None:
                trace.update(outcome="silent", model_decision="invalid_model_output")
                return
            trace["model_decision"] = decision["action"]
            if decision["action"] == "silent":
                trace.update(outcome="silent")
                return
            if self._in_cooldown(private_session_id):
                trace.update(outcome="blocked", stage="local_cooldown", preflight_reason="heartbeat_target_cooldown")
                return
            trace["stage"] = "private_companion_review"
            reviewed = await companion_api.review_proactive_chat_message(
                private_session_id,
                decision["message"],
                token=token,
            )
            message = str(reviewed.get("text", "")).strip()
            trace["review_reason"] = str(reviewed.get("reason", "") or "")[:160]
            if not reviewed.get("ok") or not message:
                trace.update(outcome="blocked", stage="private_companion_review")
                return
            trace["stage"] = "send"
            await self.context.send_message(private_session_id, MessageChain().message(message))
            self._last_sent_at[private_session_id] = time.monotonic()
            await companion_api.notify_proactive_chat_sent(private_session_id, message, token=token)
            trace.update(outcome="sent", stage="complete")
        except Exception as exc:
            trace.update(outcome="failed", error_type=type(exc).__name__)
            raise
        finally:
            if companion_api is not None and token:
                try:
                    await companion_api.cancel_proactive_chat(private_session_id, token=token)
                except Exception as exc:
                    trace["cancel_error_type"] = type(exc).__name__
            trace["duration_ms"] = round((time.perf_counter() - started) * 1000)
            self._record_diagnostic(trace)

    def _memory_bridge(self) -> Any | None:
        """Return the active public MemoryCompanion bridge when available."""
        inspected_modules: set[int] = set()
        for module_name in (
            "data.plugins.astrbot_plugin_memory_companion.main",
            "astrbot_plugin_memory_companion.main",
        ):
            module = sys.modules.get(module_name)
            if module is not None:
                inspected_modules.add(id(module))
            try:
                bridge = getattr(module, "get_active_bridge")()
            except Exception:
                continue
            if bridge is not None and callable(getattr(bridge, "compose_context", None)):
                return bridge

        get_all_stars = getattr(self.context, "get_all_stars", None)
        if callable(get_all_stars):
            try:
                stars = list(get_all_stars() or [])
            except Exception:
                stars = []
            for metadata in stars:
                identity = " ".join(
                    str(getattr(metadata, field, "") or "")
                    for field in ("name", "display_name", "root_dir_name", "module_path")
                ).lower()
                if "astrbot_plugin_memory_companion" not in identity:
                    continue
                instance = getattr(metadata, "star_cls", None)
                bridge = getattr(instance, "extension_api", None)
                if bridge is not None and callable(getattr(bridge, "compose_context", None)):
                    return bridge
                module = getattr(metadata, "module", None)
                if module is not None:
                    inspected_modules.add(id(module))
                    try:
                        bridge = getattr(module, "get_active_bridge")()
                    except Exception:
                        bridge = None
                    if bridge is not None and callable(getattr(bridge, "compose_context", None)):
                        return bridge

        for module in list(sys.modules.values()):
            if module is None or id(module) in inspected_modules:
                continue
            module_name = str(getattr(module, "__name__", "")).lower()
            module_file = str(getattr(module, "__file__", "")).replace("\\", "/").lower()
            if "astrbot_plugin_memory_companion" not in module_name and "astrbot_plugin_memory_companion" not in module_file:
                continue
            try:
                bridge = getattr(module, "get_active_bridge")()
            except Exception:
                continue
            if bridge is not None and callable(getattr(bridge, "compose_context", None)):
                return bridge
        return None

    def _private_companion_api(self) -> Any | None:
        """Return the public PrivateCompanion API when its heartbeat hooks are ready."""
        required = (
            "prepare_proactive_chat",
            "review_proactive_chat_message",
            "notify_proactive_chat_sent",
            "cancel_proactive_chat",
        )
        inspected_modules: set[int] = set()

        # AstrBot may load plugins under a generated module name. The active
        # star instance is therefore the authoritative source after hot reload.
        get_all_stars = getattr(self.context, "get_all_stars", None)
        if callable(get_all_stars):
            try:
                stars = list(get_all_stars() or [])
            except Exception:
                stars = []
            for metadata in stars:
                identity = " ".join(
                    str(getattr(metadata, field, "") or "")
                    for field in ("name", "display_name", "root_dir_name", "module_path")
                ).lower()
                if "astrbot_plugin_private_companion" not in identity:
                    continue
                instance = getattr(metadata, "star_cls", None)
                api = getattr(instance, "extension_api", None)
                if api is not None and all(callable(getattr(api, name, None)) for name in required):
                    return api
                module = getattr(metadata, "module", None)
                if module is not None:
                    inspected_modules.add(id(module))
                    try:
                        api = getattr(module, "get_private_companion_api")()
                    except Exception:
                        api = None
                    if api is not None and all(callable(getattr(api, name, None)) for name in required):
                        return api

        for module_name in (
            "data.plugins.astrbot_plugin_private_companion.main",
            "astrbot_plugin_private_companion.main",
        ):
            module = sys.modules.get(module_name)
            if module is not None:
                inspected_modules.add(id(module))
            try:
                api = getattr(module, "get_private_companion_api")()
            except Exception:
                continue
            if api is not None and all(callable(getattr(api, name, None)) for name in required):
                return api

        for module in list(sys.modules.values()):
            if module is None or id(module) in inspected_modules:
                continue
            module_name = str(getattr(module, "__name__", "")).lower()
            module_file = str(getattr(module, "__file__", "")).replace("\\", "/").lower()
            if "astrbot_plugin_private_companion" not in module_name and "astrbot_plugin_private_companion" not in module_file:
                continue
            try:
                api = getattr(module, "get_private_companion_api")()
            except Exception:
                continue
            if api is not None and all(callable(getattr(api, name, None)) for name in required):
                return api
        return None

    async def _compose_memory_contexts(self, bridge: Any, domains: dict[str, dict[str, str]]) -> tuple[list[str], int]:
        """Read each authorized domain separately to preserve memory ACL boundaries."""
        contexts: list[str] = []
        failures = 0
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
                failures += 1
                continue
            if text := str(text or "").strip():
                contexts.append(f"[{key}]\n{text}")
        return contexts, failures

    def _load_diagnostics(self) -> list[dict[str, Any]]:
        """Load bounded heartbeat diagnostics without failing plugin startup."""
        try:
            payload = json.loads(self._diagnostics_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, ValueError):
            return []

    def _record_diagnostic(self, trace: dict[str, Any]) -> None:
        """Persist redacted heartbeat outcomes for post-restart troubleshooting."""
        self._diagnostics.append(dict(trace))
        self._diagnostics = self._diagnostics[-self._diagnostic_history_limit() :]
        try:
            self._diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._diagnostics_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._diagnostics_path)
        except OSError:
            logger.warning("[silent_heartbeat] failed to persist heartbeat diagnostics")
        logger.info(
            "[silent_heartbeat] result=%s stage=%s preflight=%s model=%s review=%s domains=%s/%s empty=%s failures=%s duration_ms=%s error=%s",
            trace.get("outcome"),
            trace.get("stage"),
            trace.get("preflight_reason") or "-",
            trace.get("model_decision") or "-",
            trace.get("review_reason") or "-",
            trace.get("domains_with_context"),
            trace.get("domains_configured"),
            trace.get("domains_without_context"),
            trace.get("domain_failures"),
            trace.get("duration_ms"),
            trace.get("error_type") or "-",
        )

    def _provider(self) -> Any | None:
        provider_id = str(self.config.get("provider_id", "")).strip()
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider(umo=self._private_session_id())

    def _memory_domains(self) -> dict[str, dict[str, str]]:
        """Build private and group memory sources from explicit configuration only."""
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

    def _prompt(self, contexts: list[str], persona_context: str) -> str:
        return (
            "Review the authorized private and group memory excerpts below as one owner-only heartbeat context. "
            "Your purpose is to identify one concrete thing worth doing now for the owner: following up on a real "
            "commitment, sharing a specific relevant group development, delivering a timely reminder, or naturally "
            "continuing an important thread. Default to silence when there is no such action. "
            "Never invent facts, obligations, events, tool results, or urgency. Group excerpts are private decision "
            "material for the owner only; never address or send anything to a group.\n"
            "Return exactly one JSON object, without markdown:\n"
            '{"action":"silent","target":"","message":""}\n'
            'or {"action":"message","target":"private","message":"one concise Chinese message for the owner"}\n\n'
            "PrivateCompanion persona and runtime context:\n"
            + persona_context[:5200]
            + "\n\n"
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
        message = str(payload.get("message", "")).strip()
        if action == "silent":
            return {"action": "silent", "target": "", "message": ""}
        if action != "message" or str(payload.get("target", "")).strip() != "private" or not message or len(message) > 500:
            return None
        return {"action": "message", "target": "private", "message": message}

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

    def _diagnostic_history_limit(self) -> int:
        return self._bounded_int("diagnostic_history_limit", 50, 10, 500)

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(self.config.get(key, default)), maximum))
        except (TypeError, ValueError):
            return default
