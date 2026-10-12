"""OpenAI-Compatible Chat Completions API.

Enables Code Conductor to serve as an OpenAI-compatible API gateway,
allowing any tool, client, or script to use CloakBrowser web models
(or any registered agent) via standard /v1/chat/completions requests.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.registry import AgentRegistry
from app.core.security import require_bearer_token
from app.models.schemas import EventType, TaskCreateRequest
from app.web_models_store import load_web_models

router = APIRouter(tags=["openai-compat"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="Target model ID, e.g. web-chatgpt, web-claude, groq, etc.")
    messages: list[ChatMessage] = Field(..., description="Conversation messages")
    stream: bool = Field(default=False, description="Whether to stream response chunks via SSE")
    temperature: float | None = Field(default=0.7)
    max_tokens: int | None = Field(default=None)


@router.get("/v1/models")
@router.get("/models")
async def list_models(_: str = Depends(require_bearer_token)) -> dict[str, Any]:
    """Lists all available models including CloakBrowser web models."""
    web_models = load_web_models()
    data = []
    for wm in web_models:
        data.append({
            "id": wm.id,
            "object": "model",
            "created": int(wm.created_at),
            "owned_by": "cloakbrowser-web",
            "permission": [],
            "root": wm.id,
        })
    # Also add standard agents
    for standard_id in ["groq", "codex", "copilot", "ollama", "antigravity", "multimodel"]:
        data.append({
            "id": standard_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "code-conductor",
            "permission": [],
            "root": standard_id,
        })
    return {"object": "list", "data": data}


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    req: Request,
    _: str = Depends(require_bearer_token),
) -> Any:
    """OpenAI-compatible chat completions endpoint."""
    registry = get_agent_registry()
    adapter = registry.get(payload.model)

    if not adapter:
        # Check if dynamic web model was newly added
        from app.adapters.web_model import WebBrowserModelAdapter
        web_models = {m.id: m for m in load_web_models()}
        if payload.model in web_models:
            adapter = WebBrowserModelAdapter(web_models[payload.model])
            registry.register(adapter)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model '{payload.model}' not found in registered agents or web models.",
            )

    # Format user prompt from messages
    prompt = "\n".join(f"{m.role.capitalize()}: {m.content}" for m in payload.messages)
    task_req = TaskCreateRequest(
        prompt=prompt,
        agent_id=payload.model,
    )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_time = int(time.time())

    if payload.stream:
        async def event_generator() -> AsyncGenerator[str, None]:
            try:
                async for event in adapter.stream_task(completion_id, task_req):
                    if event.event_type == EventType.TOKEN and event.text:
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": payload.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": event.text},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    elif event.event_type == EventType.ERROR:
                        err_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": payload.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": f"\n\n[Error: {event.text}]"},
                                    "finish_reason": "error",
                                }
                            ],
                        }
                        yield f"data: {json.dumps(err_chunk)}\n\n"
                        break

                # Final finish chunk
                finish_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(finish_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                err_data = {"error": {"message": str(exc), "type": "server_error"}}
                yield f"data: {json.dumps(err_data)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream",
            },
        )

    # Non-streaming response: collect all tokens
    full_content = []
    async for event in adapter.stream_task(completion_id, task_req):
        if event.event_type == EventType.TOKEN and event.text:
            full_content.append(event.text)
        elif event.event_type == EventType.COMPLETED and event.text:
            if not full_content:
                full_content.append(event.text)
        elif event.event_type == EventType.ERROR:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Agent execution error: {event.text}",
            )

    result_text = "".join(full_content) if full_content else "(Empty response)"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_time,
        "model": payload.model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(result_text.split()),
            "total_tokens": len(prompt.split()) + len(result_text.split()),
        },
    }
