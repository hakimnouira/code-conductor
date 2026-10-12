"""Web Model Adapter for CloakBrowser.

Enables Code Conductor to execute tasks using web-based AI models (e.g. ChatGPT,
Claude, DeepSeek, LMSYS Arena, Manus) through an active CloakBrowser session
with persistent logins. Streams DOM updates back as token events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx
import websockets

from app.adapters.base import BaseAgentAdapter
from app.browser.cloak_launcher import is_cdp_connected, launch_cloakbrowser
from app.models.schemas import (
    AgentCapabilities,
    AgentEvent,
    AvailabilityState,
    EventType,
    TaskCreateRequest,
)
from app.web_models_store import WebModelConfig

logger = logging.getLogger(__name__)

CDP_BASE_URL = "http://127.0.0.1:9222"
_CDP_TABS_CACHE: dict[str, Any] = {"ts": 0.0, "connected": False, "tabs": []}


async def _get_cached_cdp_status(cdp_base: str = CDP_BASE_URL) -> tuple[bool, list[dict[str, Any]]]:
    now = time.time()
    if (now - _CDP_TABS_CACHE["ts"]) < 3.0:
        return _CDP_TABS_CACHE["connected"], _CDP_TABS_CACHE["tabs"]
    connected = await is_cdp_connected(timeout=0.6)
    tabs: list[dict[str, Any]] = []
    if connected:
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                res = await client.get(f"{cdp_base}/json/list")
                tabs = res.json()
        except Exception:
            pass
    _CDP_TABS_CACHE["ts"] = now
    _CDP_TABS_CACHE["connected"] = connected
    _CDP_TABS_CACHE["tabs"] = tabs
    return connected, tabs


class WebBrowserModelAdapter(BaseAgentAdapter):
    def __init__(self, model_config: WebModelConfig, cdp_base: str = CDP_BASE_URL):
        self.config = model_config
        self.cdp_base = cdp_base.rstrip("/")
        self._active_tasks: dict[str, bool] = {}

    @property
    def agent_id(self) -> str:
        return self.config.id

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def description(self) -> str:
        return self.config.description

    async def get_capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            supports_streaming=True,
            supports_code_edits=True,
            supports_terminal_execution=False,
            supports_subagents=False,
            supported_models=[self.config.id, f"{self.config.id}-default"],
        )

    async def check_availability(self) -> tuple[AvailabilityState, str]:
        connected, tabs = await _get_cached_cdp_status(self.cdp_base)
        if not connected:
            return (
                AvailabilityState.MISSING_CLI,
                f"CloakBrowser is not currently attached on {self.cdp_base}. Click 'Launch CloakBrowser' to connect.",
            )

        # Check if tab is open
        domain = urlparse(self.config.url).netloc
        matching = [t for t in tabs if domain.lower() in t.get("url", "").lower()]
        if matching:
            return (
                AvailabilityState.READY,
                f"{self.config.name} tab is active and ready in CloakBrowser.",
            )
        return (
            AvailabilityState.READY,
            f"CloakBrowser connected. Tab for {self.config.name} will open automatically on demand.",
        )

    async def _get_or_open_tab(self) -> dict:
        """Finds an existing tab for the model's domain, or opens a new one."""
        domain = urlparse(self.config.url).netloc
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{self.cdp_base}/json/list")
            tabs = res.json()
            for t in tabs:
                if t.get("type") == "page" and domain.lower() in t.get("url", "").lower():
                    return t

            # Tab not found, open a new one
            encoded_url = urllib.parse.quote(self.config.url, safe=":/?&=#%")
            create_res = await client.put(f"{self.cdp_base}/json/new?{encoded_url}")
            tab_data = create_res.json()
            # Give the new tab a moment to navigate
            await asyncio.sleep(2.5)
            return tab_data

    async def cancel_task(self, task_id: str) -> bool:
        self._active_tasks[task_id] = False
        return True

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        self._active_tasks[task_id] = True

        yield AgentEvent(
            task_id=task_id,
            event_type=EventType.STARTED,
            text=f"Connecting to {self.config.name} in CloakBrowser...",
            data={"agent_id": self.agent_id, "url": self.config.url},
            timestamp=time.time(),
        )

        # Auto-launch CloakBrowser if it's not currently running
        if not await is_cdp_connected(timeout=1.0):
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.STARTED,
                text="CloakBrowser is offline. Auto-launching browser with your session profile...",
                timestamp=time.time(),
            )
            try:
                await launch_cloakbrowser(open_url=self.config.url)
                await asyncio.sleep(2.5)
            except Exception as launch_exc:
                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.ERROR,
                    text=f"Could not launch CloakBrowser: {launch_exc}. Click 'Launch CloakBrowser' in the control center.",
                    timestamp=time.time(),
                )
                return

        try:
            tab = await self._get_or_open_tab()
        except Exception as exc:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Failed to access CloakBrowser tab: {exc}. Ensure CloakBrowser is open.",
                data={"error": str(exc)},
                timestamp=time.time(),
            )
            return

        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text="Tab does not provide a WebSocket debugging interface.",
                timestamp=time.time(),
            )
            return

        async def _cdp_eval(ws, expression: str, req_id: int, timeout: float = 15.0) -> dict:
            await ws.send(json.dumps({
                "id": req_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
            }))
            start_t = asyncio.get_event_loop().time()
            while True:
                rem = timeout - (asyncio.get_event_loop().time() - start_t)
                if rem <= 0:
                    return {}
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=rem)
                    msg = json.loads(raw)
                    if msg.get("id") == req_id:
                        res = msg.get("result", {})
                        if "exceptionDetails" in res:
                            logger.warning(f"CDP JS exception: {res.get('exceptionDetails')}")
                            return {"success": False, "error": str(res.get("exceptionDetails"))}
                        val = res.get("result", {}).get("value")
                        if val is not None:
                            return val if isinstance(val, dict) else {"value": val}
                        return {}
                except asyncio.TimeoutError:
                    return {}
                except Exception as exc:
                    return {}

        prompt_json = json.dumps(request.prompt)
        input_sel = json.dumps(self.config.input_selector or "")
        send_sel = json.dumps(self.config.send_selector or "")
        resp_sel = json.dumps(self.config.response_selector or "")

        measure_script = f"""
        (function() {{
            const respSelector = {resp_sel};
            const customCount = respSelector ? document.querySelectorAll(respSelector).length : 0;
            const h4Count = document.querySelectorAll('h4[data-conversation-role="assistant"]').length;
            const elsCount = document.querySelectorAll(
                '.font-claude-message, div[data-message-author-role="assistant"], .ds-markdown, div.markdown, .prose, .chat-message'
            ).length;
            return {{ customCount: customCount, h4Count: h4Count, elsCount: elsCount }};
        }})()
        """

        injection_script = f"""
        (function() {{
            try {{
                const promptText = {prompt_json};
                const inputSelector = {input_sel};
                const sendSelector = {send_sel};

                // 1. Locate input element
                let inputEl = null;
                if (inputSelector) {{
                    inputEl = document.querySelector(inputSelector);
                }}
                if (!inputEl) {{
                    inputEl = document.querySelector(
                        '#prompt-textarea, div.tiptap.ProseMirror, div.ProseMirror[contenteditable="true"], textarea[placeholder*="message" i], textarea[placeholder*="ask" i], textarea, div[contenteditable="true"], div[role="textbox"]'
                    );
                }}

                if (!inputEl) {{
                    return {{ success: false, error: "Prompt input field not found. Please log in to your account in the CloakBrowser window." }};
                }}

                // 2. Insert text
                inputEl.focus();
                if (inputEl.tagName === "TEXTAREA" || inputEl.tagName === "INPUT") {{
                    try {{
                        document.execCommand("selectAll", false, null);
                        document.execCommand("insertText", false, promptText);
                    }} catch(e) {{}}
                    inputEl.value = promptText;
                    inputEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
                    inputEl.dispatchEvent(new Event("change", {{ bubbles: true }}));
                }} else if (inputEl.isContentEditable || inputEl.classList.contains("ProseMirror")) {{
                    try {{
                        inputEl.dispatchEvent(new InputEvent("beforeinput", {{
                            bubbles: true,
                            cancelable: true,
                            inputType: "insertText",
                            data: promptText
                        }}));
                    }} catch(e) {{}}
                    document.execCommand("selectAll", false, null);
                    document.execCommand("insertText", false, promptText);
                    inputEl.dispatchEvent(new InputEvent("input", {{ bubbles: true }}));
                    inputEl.dispatchEvent(new KeyboardEvent("keyup", {{ key: "e", bubbles: true }}));
                }}

                // 3. Trigger submit
                let sent = false;
                if (sendSelector) {{
                    const btn = document.querySelector(sendSelector);
                    if (btn && !btn.disabled && (!btn.className || !btn.className.includes("disabled"))) {{
                        btn.click();
                        sent = true;
                    }}
                }}
                if (!sent) {{
                    const sendBtn = document.querySelector(
                        'button[data-testid="send-button"], button[data-testid="chat-input-send"], button[class*="accent-send-button"], div.ds-button--circle:not(.ds-button--disabled), div[role="button"].ds-button--primary:not(.ds-button--disabled), button[aria-label*="Send" i], button[aria-label*="Submit" i]'
                    );
                    if (sendBtn && !sendBtn.disabled && (!sendBtn.className || !sendBtn.className.includes("disabled"))) {{
                        sendBtn.click();
                        sent = true;
                    }}
                }}
                if (!sent) {{
                    inputEl.dispatchEvent(new KeyboardEvent("keydown", {{ key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }}));
                    sent = true;
                }}

                return {{ success: true, sent: sent }};
            }} catch (err) {{
                return {{ success: false, error: err.toString() }};
            }}
        }})()
        """

        try:
            async with websockets.connect(ws_url, close_timeout=5.0, max_size=50*1024*1024) as ws:
                req_id = 1
                
                # Baseline measurement
                baseline = await _cdp_eval(ws, measure_script, req_id)
                base_custom = baseline.get("customCount", 0)
                base_h4 = baseline.get("h4Count", 0)
                base_els = baseline.get("elsCount", 0)

                # Inject prompt
                req_id += 1
                inject_res = await _cdp_eval(ws, injection_script, req_id)

                if not inject_res.get("success"):
                    err_msg = inject_res.get("error", "Chat input not found.")
                    yield AgentEvent(
                        task_id=task_id,
                        event_type=EventType.ERROR,
                        text=f"{self.config.name}: {err_msg} Check your CloakBrowser window to ensure you are logged in.",
                        timestamp=time.time(),
                    )
                    return

                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.STARTED,
                    text=f"Prompt submitted to {self.config.name}. Reading response stream...",
                    timestamp=time.time(),
                )

                # Script to extract the new assistant response text
                extract_script = f"""
                (function() {{
                    const respSelector = {resp_sel};
                    let text = "";

                    // Stop button indicator
                    const stopBtn = document.querySelector(
                        'button[aria-label*="Stop" i], button[data-testid*="stop" i], button:has(svg[aria-label*="Stop" i])'
                    );
                    const is_generating = Boolean(stopBtn);

                    // Method 1: Custom configured selector
                    if (respSelector) {{
                        const customEls = Array.from(document.querySelectorAll(respSelector));
                        if (customEls.length > {base_custom}) {{
                            const latest = customEls[customEls.length - 1];
                            if (latest.tagName === "H4" && latest.parentElement) {{
                                let rep = "";
                                for (const ch of latest.parentElement.children) {{
                                    if (ch !== latest) rep += (ch.innerText || "") + "\\n";
                                }}
                                text = rep.trim();
                            }} else {{
                                text = (latest.innerText || latest.textContent || "").trim();
                            }}
                        }}
                    }}
                    if (text === "ChatGPT said:") text = "";

                    // Method 2: ChatGPT latest UI (h4 with data-conversation-role="assistant")
                    if (!text) {{
                        const h4List = Array.from(document.querySelectorAll('h4[data-conversation-role="assistant"]'));
                        if (h4List.length > {base_h4}) {{
                            const lastH4 = h4List[h4List.length - 1];
                            const parent = lastH4.parentElement;
                            let reply = "";
                            for (const child of parent.children) {{
                                if (child !== lastH4) {{
                                    reply += (child.innerText || "") + "\\n";
                                }}
                            }}
                            text = reply.trim();
                        }}
                    }}

                    // Method 3: Claude (.font-claude-message), DeepSeek, Arena, Manus, Muse, and standard selectors
                    if (!text) {{
                        const elements = Array.from(document.querySelectorAll(
                            '.font-claude-message, div[data-message-author-role="assistant"], .ds-markdown, div.markdown, .prose, .hatch-chat-groupable-bubble, [class*="hatch-chat-"], .chat-message'
                        ));
                        if (elements.length > {base_els}) {{
                            const latest = elements[elements.length - 1];
                            text = (latest.innerText || latest.textContent || "").trim();
                        }}
                    }}

                    // Fallback if message count didn't increment but text exists in last element
                    if (!text && is_generating) {{
                        const elements = Array.from(document.querySelectorAll(
                            '.font-claude-message, div[data-message-author-role="assistant"], .ds-markdown, div.markdown, .prose, .hatch-chat-groupable-bubble, [class*="hatch-chat-"], .chat-message'
                        ));
                        if (elements.length > 0) {{
                            const latest = elements[elements.length - 1];
                            text = (latest.innerText || latest.textContent || "").trim();
                        }}
                    }}

                    return {{ text: text, is_generating: is_generating }};
                }})()
                """

                last_seen_length = 0
                full_text = ""
                still_count = 0
                max_polls = 140

                for _ in range(max_polls):
                    if not self._active_tasks.get(task_id, True):
                        yield AgentEvent(
                            task_id=task_id,
                            event_type=EventType.CANCELLED,
                            text="Task cancelled by user.",
                            timestamp=time.time(),
                        )
                        break

                    await asyncio.sleep(0.5)
                    req_id += 1
                    val = await _cdp_eval(ws, extract_script, req_id)
                    current_text = val.get("text", "")
                    is_generating = val.get("is_generating", False)

                    if len(current_text) > last_seen_length:
                        new_chunk = current_text[last_seen_length:]
                        last_seen_length = len(current_text)
                        full_text = current_text
                        still_count = 0
                        yield AgentEvent(
                            task_id=task_id,
                            event_type=EventType.TOKEN,
                            text=new_chunk,
                            timestamp=time.time(),
                        )
                    else:
                        if full_text:
                            still_count += 1

                    if full_text and not is_generating and still_count >= 3:
                        break
                    if full_text and still_count >= 10:
                        break

                if not full_text:
                    full_text = "Prompt submitted to web session. Please check your CloakBrowser window."

                yield AgentEvent(
                    task_id=task_id,
                    event_type=EventType.COMPLETED,
                    text=full_text,
                    data={"agent_id": self.agent_id, "model": self.config.name},
                    timestamp=time.time(),
                )

        except Exception as exc:
            logger.exception("Web model execution error")
            yield AgentEvent(
                task_id=task_id,
                event_type=EventType.ERROR,
                text=f"Error communicating with web session: {exc}",
                timestamp=time.time(),
            )
        finally:
            self._active_tasks.pop(task_id, None)
