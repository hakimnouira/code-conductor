"""Web Model Adapter for CloakBrowser.

Enables Code Conductor to execute tasks using web-based AI models (e.g. ChatGPT,
Claude, DeepSeek, LMSYS Arena, Manus) through an active CloakBrowser session
with persistent logins. Streams DOM updates back as token events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx
import websockets

from app.adapters.base import BaseAgentAdapter
from app.browser.cloak_launcher import is_cdp_connected
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
        connected = await is_cdp_connected(timeout=1.5)
        if not connected:
            return (
                AvailabilityState.MISSING_CLI,
                f"CloakBrowser is not currently attached on {self.cdp_base}. Click 'Launch CloakBrowser' to connect.",
            )

        # Check if tab is open
        try:
            domain = urlparse(self.config.url).netloc
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get(f"{self.cdp_base}/json/list")
                tabs = res.json()
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
        except Exception as exc:
            return (
                AvailabilityState.DEGRADED,
                f"CloakBrowser connected but failed to query active tabs: {exc}",
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
            await asyncio.sleep(2.0)
            return tab_data

    async def cancel_task(self, task_id: str) -> bool:
        self._active_tasks[task_id] = False
        return True

    async def stream_task(self, task_id: str, request: TaskCreateRequest) -> AsyncIterator[AgentEvent]:
        self._active_tasks[task_id] = True

        yield AgentEvent(
            event_type=EventType.STEP_UPDATE,
            text=f"Connecting to {self.config.name} in CloakBrowser...",
            data={"agent_id": self.agent_id, "url": self.config.url},
        )

        try:
            tab = await self._get_or_open_tab()
        except Exception as exc:
            yield AgentEvent(
                event_type=EventType.ERROR,
                text=f"Failed to access CloakBrowser tab: {exc}. Ensure CloakBrowser is launched.",
                data={"error": str(exc)},
            )
            return

        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            yield AgentEvent(
                event_type=EventType.ERROR,
                text="Tab does not provide a WebSocket debugging interface.",
            )
            return

        # Prepare injection script that inputs prompt and starts generation
        prompt_escaped = json.dumps(request.prompt)
        input_sel = json.dumps(self.config.input_selector or "")
        send_sel = json.dumps(self.config.send_selector or "")

        injection_script = f"""
        (function() {{
            const promptText = {prompt_escaped};
            const inputSelector = {input_sel};
            const sendSelector = {send_sel};

            // 1. Locate prompt input element
            let inputEl = null;
            if (inputSelector) {{
                inputEl = document.querySelector(inputSelector);
            }}
            if (!inputEl) {{
                inputEl = document.querySelector(
                    '#prompt-textarea, textarea[placeholder*="message" i], textarea[placeholder*="ask" i], textarea, div[contenteditable="true"], div[role="textbox"]'
                );
            }}

            if (!inputEl) {{
                return {{ success: false, error: "Prompt input element not found in DOM. Ensure you are on the chat page." }};
            }}

            // 2. Insert text
            inputEl.focus();
            if (inputEl.tagName === "TEXTAREA" || inputEl.tagName === "INPUT") {{
                inputEl.value = promptText;
                inputEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
                inputEl.dispatchEvent(new Event("change", {{ bubbles: true }}));
            }} else if (inputEl.isContentEditable) {{
                inputEl.innerText = promptText;
                inputEl.dispatchEvent(new InputEvent("input", {{ bubbles: true, inputType: "insertText", data: promptText }}));
            }}

            // 3. Trigger submit
            let sent = false;
            if (sendSelector) {{
                const btn = document.querySelector(sendSelector);
                if (btn && !btn.disabled) {{
                    btn.click();
                    sent = true;
                }}
            }}
            if (!sent) {{
                const sendBtn = document.querySelector(
                    'button[data-testid="send-button"], button[aria-label*="Send" i], button[aria-label*="Submit" i], button:has(svg)'
                );
                if (sendBtn && !sendBtn.disabled) {{
                    sendBtn.click();
                    sent = true;
                }}
            }}
            if (!sent) {{
                // Fallback to Enter key event
                inputEl.dispatchEvent(new KeyboardEvent("keydown", {{ key: "Enter", code: "Enter", keyCode: 13, bubbles: true }}));
                sent = true;
            }}

            return {{ success: true, sent: sent }};
        }})()
        """

        try:
            async with websockets.connect(ws_url, close_timeout=5.0) as ws:
                # Send injection command
                req_id = 1
                await ws.send(json.dumps({
                    "id": req_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": injection_script,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                }))
                raw_resp = await ws.recv()
                inject_res = json.loads(raw_resp).get("result", {}).get("result", {}).get("value", {})

                if not inject_res.get("success"):
                    err_msg = inject_res.get("error", "Failed to find chat input field.")
                    yield AgentEvent(
                        event_type=EventType.ERROR,
                        text=f"{self.config.name}: {err_msg} Please ensure you are logged into your account in CloakBrowser.",
                    )
                    return

                yield AgentEvent(
                    event_type=EventType.STEP_UPDATE,
                    text=f"Prompt submitted to {self.config.name}. Reading response stream...",
                )

                # Script to extract the latest assistant response text
                resp_sel = json.dumps(self.config.response_selector or "")
                extract_script = f"""
                (function() {{
                    const respSelector = {resp_sel};
                    let elements = [];
                    if (respSelector) {{
                        elements = Array.from(document.querySelectorAll(respSelector));
                    }}
                    if (elements.length === 0) {{
                        elements = Array.from(document.querySelectorAll(
                            'div[data-message-author-role="assistant"], .font-claude-message, .ds-markdown, div.markdown, .prose, .chat-message'
                        ));
                    }}
                    if (elements.length === 0) {{
                        return {{ text: "", is_generating: false }};
                    }}
                    const latest = elements[elements.length - 1];
                    const text = latest.innerText || latest.textContent || "";
                    
                    // Check if generation is still in progress
                    const stopBtn = document.querySelector(
                        'button[aria-label*="Stop" i], button[data-testid*="stop" i], button:has(svg[aria-label*="Stop" i])'
                    );
                    const is_generating = Boolean(stopBtn);
                    return {{ text: text.trim(), is_generating: is_generating }};
                }})()
                """

                # Poll and stream tokens
                last_seen_length = 0
                full_text = ""
                still_count = 0
                max_polls = 120  # Up to 60 seconds of generation

                for _ in range(max_polls):
                    if not self._active_tasks.get(task_id, True):
                        yield AgentEvent(
                            event_type=EventType.STEP_UPDATE,
                            text="Task cancelled by user.",
                        )
                        break

                    await asyncio.sleep(0.5)
                    req_id += 1
                    await ws.send(json.dumps({
                        "id": req_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": extract_script,
                            "returnByValue": True,
                            "awaitPromise": True,
                        },
                    }))
                    raw = await ws.recv()
                    val = json.loads(raw).get("result", {}).get("result", {}).get("value", {})
                    current_text = val.get("text", "")
                    is_generating = val.get("is_generating", False)

                    if len(current_text) > last_seen_length:
                        new_chunk = current_text[last_seen_length:]
                        last_seen_length = len(current_text)
                        full_text = current_text
                        still_count = 0
                        yield AgentEvent(
                            event_type=EventType.TOKEN,
                            text=new_chunk,
                        )
                    else:
                        if full_text:
                            still_count += 1

                    # Stop condition: not generating anymore and text has stabilized
                    if full_text and not is_generating and still_count >= 3:
                        break
                    # Or stabilized for over 5 seconds
                    if full_text and still_count >= 10:
                        break

                if not full_text:
                    full_text = "Prompt submitted to web session. Please check your CloakBrowser window."

                yield AgentEvent(
                    event_type=EventType.COMPLETED,
                    text=full_text,
                    data={"agent_id": self.agent_id, "model": self.config.name},
                )

        except Exception as exc:
            logger.exception("Web model execution error")
            yield AgentEvent(
                event_type=EventType.ERROR,
                text=f"Error communicating with web session: {exc}",
            )
        finally:
            self._active_tasks.pop(task_id, None)
