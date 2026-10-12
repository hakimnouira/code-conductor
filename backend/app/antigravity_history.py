"""
Antigravity Chat History and Session Inspector.
Discovers and extracts historical conversations, transcripts, and artifacts
from the local Antigravity IDE brain store.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import require_bearer_token

router = APIRouter(prefix="/antigravity/history", tags=["antigravity-history"])


def get_brain_directory() -> pathlib.Path:
    """Finds the local Antigravity brain directory across common user paths."""
    candidates = [
        pathlib.Path.home() / ".gemini" / "antigravity-ide" / "brain",
        pathlib.Path(os.environ.get("USERPROFILE", "")) / ".gemini" / "antigravity-ide" / "brain",
        pathlib.Path(os.environ.get("HOME", "")) / ".gemini" / "antigravity-ide" / "brain",
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    # Default to home fallback
    return pathlib.Path.home() / ".gemini" / "antigravity-ide" / "brain"


class ConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: float
    updated_time: str
    message_count: int
    first_prompt: str
    workspace_path: str | None = None
    project_name: str | None = None


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str
    step: int | None = None
    timestamp: float | None = None


class ArtifactItem(BaseModel):
    name: str
    size: int
    modified_time: str


class ConversationDetail(BaseModel):
    id: str
    title: str
    updated_at: float
    updated_time: str
    message_count: int
    messages: list[ChatMessage]
    artifacts: list[ArtifactItem]
    workspace_path: str | None = None
    project_name: str | None = None


def extract_workspace_from_transcript(log_file: pathlib.Path) -> tuple[str | None, str | None]:
    """Extracts project workspace directory and project name from transcript log."""
    if not log_file.exists():
        return None, None
    found_path = None
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                # 1. Check user_information mapping: e.g. [URI] -> [CorpusName]
                m_map = re.search(r'([a-zA-Z]:\\[^\r\n"<>]+?)\s*->', line)
                if m_map:
                    cand = m_map.group(1).strip()
                    if not any(x in cand for x in [".gemini", "AppData", "Temp"]):
                        found_path = cand
                        break

                # 2. Check tool_calls args
                if '"tool_calls"' in line:
                    try:
                        rec = json.loads(line)
                        for tc in rec.get("tool_calls", []):
                            args = tc.get("args", {})
                            for k in ["Cwd", "SearchPath", "DirectoryPath", "TargetFile"]:
                                val = args.get(k)
                                if isinstance(val, str):
                                    val = val.strip(' "\'')
                                    if ":" in val and ("\\" in val or "/" in val):
                                        p = pathlib.Path(val)
                                        if k == "TargetFile" or (p.suffix and not p.is_dir()):
                                            p = p.parent
                                        clean_str = str(p)
                                        if not any(x in clean_str for x in [".gemini", "AppData", "Temp"]):
                                            found_path = clean_str
                                            break
                            if found_path:
                                break
                    except Exception:
                        pass
                if found_path:
                    break
    except Exception:
        pass

    if found_path:
        norm = str(pathlib.Path(found_path))
        pname = pathlib.Path(norm).name or norm
        return norm, pname
    return None, None


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(_: str = Depends(require_bearer_token)) -> list[ConversationSummary]:
    brain_dir = get_brain_directory()
    if not brain_dir.exists():
        return []

    summaries: list[ConversationSummary] = []

    for item in brain_dir.iterdir():
        if not item.is_dir():
            continue
        log_file = item / ".system_generated" / "logs" / "transcript.jsonl"
        if not log_file.exists():
            continue

        try:
            st = log_file.stat()
            mtime = st.st_mtime
            first_prompt = ""
            msg_count = 0

            with open(log_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue

                    rtype = record.get("type")
                    if rtype == "USER_INPUT":
                        msg_count += 1
                        if not first_prompt:
                            raw = record.get("content", "")
                            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw, re.DOTALL)
                            prompt_text = m.group(1).strip() if m else raw.strip()
                            # Clean leading meta tags
                            prompt_text = re.sub(r"\[ARTIFACT:.*?\]", "", prompt_text).strip()
                            first_prompt = prompt_text[:140]
                    elif rtype == "PLANNER_RESPONSE" and record.get("content"):
                        msg_count += 1

            title = first_prompt or f"Conversation {item.name[:8]}"
            # Keep clean single-line title
            clean_title = " ".join(title.splitlines()).strip()
            if len(clean_title) > 90:
                clean_title = clean_title[:87] + "..."

            formatted_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            ws_path, proj_name = extract_workspace_from_transcript(log_file)

            summaries.append(
                ConversationSummary(
                    id=item.name,
                    title=clean_title or "Untitled Conversation",
                    updated_at=mtime,
                    updated_time=formatted_time,
                    message_count=msg_count,
                    first_prompt=first_prompt,
                    workspace_path=ws_path,
                    project_name=proj_name,
                )
            )
        except Exception:
            continue

    summaries.sort(key=lambda x: x.updated_at, reverse=True)
    return summaries


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str, _: str = Depends(require_bearer_token)
) -> ConversationDetail:
    # Security check: conversation_id must be a valid hex/uuid/safe string
    if not re.match(r"^[a-zA-Z0-9_\-]+$", conversation_id):
        raise HTTPException(status_code=400, detail="Invalid conversation ID format.")

    brain_dir = get_brain_directory()
    conv_dir = (brain_dir / conversation_id).resolve()

    if not conv_dir.exists() or not conv_dir.is_dir() or brain_dir not in conv_dir.parents:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    log_file = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="No transcript found for conversation.")

    messages: list[ChatMessage] = []
    first_prompt = ""
    mtime = log_file.stat().st_mtime

    with open(log_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue

            rtype = record.get("type")
            step = record.get("step_index")

            if rtype == "USER_INPUT":
                raw = record.get("content", "")
                m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw, re.DOTALL)
                clean_text = m.group(1).strip() if m else raw.strip()
                # Remove large context prefixes if present
                clean_text = re.sub(r"# Resuming from a compaction.*?</summary>", "", clean_text, flags=re.DOTALL).strip()
                if not first_prompt:
                    first_prompt = clean_text[:120]
                messages.append(ChatMessage(role="user", content=clean_text, step=step))

            elif rtype == "PLANNER_RESPONSE":
                content = record.get("content", "").strip()
                if content:
                    messages.append(ChatMessage(role="assistant", content=content, step=step))

    # Discover any artifacts
    artifacts: list[ArtifactItem] = []
    for f in conv_dir.iterdir():
        if f.is_file() and not f.name.startswith("."):
            artifacts.append(
                ArtifactItem(
                    name=f.name,
                    size=f.stat().st_size,
                    modified_time=time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                )
            )

    clean_title = " ".join((first_prompt or f"Conversation {conversation_id[:8]}").splitlines()).strip()
    formatted_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    ws_path, proj_name = extract_workspace_from_transcript(log_file)

    return ConversationDetail(
        id=conversation_id,
        title=clean_title,
        updated_at=mtime,
        updated_time=formatted_time,
        message_count=len(messages),
        messages=messages,
        artifacts=artifacts,
        workspace_path=ws_path,
        project_name=proj_name,
    )
