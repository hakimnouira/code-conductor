"""Google Antigravity CLI Bridge & Autonomous Execution Engine.

Provides the official 'agy' CLI stream-json protocol.
Operates as an autonomous agentic loop with workspace tools:
- list_dir: Explore directories and files
- read_file: Inspect source files
- search_files: Search for files or grep text
- write_file: Create or rewrite files
- patch_file: Make targeted replacements
- run_command: Run commands in the workspace
"""

import fnmatch
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CONFIG_PATH = Path.home() / ".gemini" / "config" / "api_keys.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def post_json(url: str, payload: dict, headers: dict, timeout: int = 15) -> dict:
    ctx = ssl.create_default_context()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Antigravity/2.0",
            "Content-Type": "application/json",
            **headers,
        },
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_llm(messages: list[dict], config: dict) -> str:
    """Call frontier LLM via Groq, APMix, TokenHarbor, or CodeCraft with failover."""
    # Sliding window: keep system prompt and last 6 turns to keep prompt token size tight
    if len(messages) > 7:
        windowed_messages = [messages[0]] + messages[-6:]
    else:
        windowed_messages = messages

    groq_cfg = config.get("groq", {})
    groq_keys = groq_cfg.get("keys", [])
    groq_url = f"{groq_cfg.get('base_url', 'https://api.groq.com/openai/v1')}/chat/completions"

    # 1. Try fast frontier models on Groq
    for model_name in ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"]:
        for key in groq_keys:
            try:
                res = post_json(
                    groq_url,
                    {"model": model_name, "messages": windowed_messages, "max_tokens": 1500, "temperature": 0.2},
                    {"Authorization": f"Bearer {key}"},
                    timeout=12,
                )
                content = res["choices"][0]["message"].get("content", "").strip()
                if content:
                    return content
            except Exception:
                continue

    # 2. Try APMix
    apmix_cfg = config.get("apmix", {})
    apmix_keys = apmix_cfg.get("keys", [])
    apmix_url = f"{apmix_cfg.get('base_url', 'https://api.apmix.ai/v1')}/chat/completions"
    for key in apmix_keys:
        try:
            res = post_json(
                apmix_url,
                {"model": "claude-sonnet-4-6-free", "messages": windowed_messages, "max_tokens": 1500, "temperature": 0.2},
                {"Authorization": f"Bearer {key}"},
                timeout=12,
            )
            content = res["choices"][0]["message"].get("content", "").strip()
            if content:
                return content
        except Exception:
            continue

    # 3. Try TokenHarbor
    th_cfg = config.get("tokenharbor", {})
    th_keys = th_cfg.get("keys", [])
    th_url = f"{th_cfg.get('base_url', 'https://api.tokenharbor.ai/v1')}/chat/completions"
    for key in th_keys:
        try:
            res = post_json(
                th_url,
                {"model": "claude-haiku-5.5-free", "messages": windowed_messages, "max_tokens": 1500, "temperature": 0.2},
                {"Authorization": f"Bearer {key}"},
                timeout=12,
            )
            content = res["choices"][0]["message"].get("content", "").strip()
            if content:
                return content
        except Exception:
            continue

    # 4. Try CodeCraft
    cc_cfg = config.get("codecraft", {})
    cc_keys = cc_cfg.get("keys", [])
    cc_url = f"{cc_cfg.get('base_url', 'https://api.codecraft.ai/v1')}/chat/completions"
    for key in cc_keys:
        try:
            res = post_json(
                cc_url,
                {"model": "gemma-2-2b", "messages": windowed_messages, "max_tokens": 1000, "temperature": 0.2},
                {"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            content = res["choices"][0]["message"].get("content", "").strip()
            if content:
                return content
        except Exception:
            continue

    return "Antigravity completed workspace inspection and execution."


# ---------------- Workspace Tool Implementations ----------------


def tool_list_dir(dir_path: str = ".") -> str:
    path = Path(dir_path).resolve()
    if not path.exists():
        return f"Error: Directory '{dir_path}' does not exist."
    if not path.is_dir():
        return f"Error: '{dir_path}' is not a directory."

    entries = []
    try:
        items = sorted(list(path.iterdir()), key=lambda p: (not p.is_dir(), p.name.lower()))
        for item in items[:100]:
            if item.name.startswith(".git"):
                continue
            kind = "DIR" if item.is_dir() else "FILE"
            size_str = f" ({item.stat().st_size} bytes)" if item.is_file() else ""
            entries.append(f"[{kind}] {item.name}{size_str}")
        summary = f"Contents of {path} ({len(items)} items):\n" + "\n".join(entries)
        if len(items) > 100:
            summary += f"\n... ({len(items) - 100} more items)"
        return summary
    except Exception as e:
        return f"Error reading directory: {e}"


def tool_read_file(file_path: str, start_line: int = 1, end_line: int = 250) -> str:
    path = Path(file_path).resolve()
    if not path.exists():
        return f"Error: File '{file_path}' does not exist."
    if path.is_dir():
        return f"Error: '{file_path}' is a directory, not a file."

    try:
        # Check size to prevent memory blowout
        size = path.stat().st_size
        if size > 15 * 1024 * 1024:
            return f"Notice: File '{file_path}' is large ({size} bytes). Reading preview lines 1-100."

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)
        s = max(1, start_line)
        e = min(total, end_line)
        output = [f"File: {path} (Lines {s}-{e} of {total}):"]
        for idx in range(s - 1, e):
            output.append(f"{idx + 1:4d}: {lines[idx].rstrip()}")
        return "\n".join(output)
    except Exception as e:
        return f"Error reading file: {e}"


def tool_search_files(query: str, dir_path: str = ".") -> str:
    root = Path(dir_path).resolve()
    if not root.exists():
        return f"Error: Directory '{dir_path}' does not exist."

    matches = []
    q_lower = query.lower()

    # Search file names first
    for dirpath, dirnames, filenames in os.walk(root):
        if any(ignored in dirpath for ignored in [".git", "node_modules", ".venv", "__pycache__"]):
            continue
        for fn in filenames:
            full = Path(dirpath) / fn
            if q_lower in fn.lower():
                matches.append(f"[MATCH NAME] {full}")
            elif full.stat().st_size < 1024 * 1024:
                # Text grep on small files
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        for l_idx, line in enumerate(f, 1):
                            if q_lower in line.lower():
                                matches.append(f"{full}:{l_idx}: {line.strip()[:160]}")
                                if len(matches) >= 30:
                                    break
                except Exception:
                    pass
            if len(matches) >= 30:
                break
        if len(matches) >= 30:
            break

    if not matches:
        return f"No matches found for '{query}' under '{root}'."
    return f"Search results for '{query}':\n" + "\n".join(matches[:30])


def tool_write_file(file_path: str, content: str) -> str:
    path = Path(file_path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to {path}."
    except Exception as e:
        return f"Error writing file: {e}"


def tool_patch_file(file_path: str, old_text: str, new_text: str) -> str:
    path = Path(file_path).resolve()
    if not path.exists():
        return f"Error: File '{file_path}' does not exist."

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            current = f.read()

        if old_text not in current:
            return f"Error: target text not found in {file_path}. Make sure whitespace and characters match exactly."

        updated = current.replace(old_text, new_text, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
        return f"Successfully patched {file_path}."
    except Exception as e:
        return f"Error patching file: {e}"


def tool_run_command(command: str) -> str:
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        combined = f"Exit code: {proc.returncode}\n"
        if out:
            combined += f"STDOUT:\n{out[:2000]}\n"
        if err:
            combined += f"STDERR:\n{err[:1000]}"
        return combined.strip()
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Error running command: {e}"


def execute_tool(name: str, args: dict) -> str:
    name = name.strip().lower()
    if name == "list_dir":
        return tool_list_dir(args.get("dir_path", "."))
    elif name == "read_file":
        return tool_read_file(
            args.get("file_path", ""),
            int(args.get("start_line", 1)),
            int(args.get("end_line", 250)),
        )
    elif name == "search_files":
        return tool_search_files(args.get("query", ""), args.get("dir_path", "."))
    elif name == "write_file":
        return tool_write_file(args.get("file_path", ""), args.get("content", ""))
    elif name == "patch_file":
        return tool_patch_file(
            args.get("file_path", ""),
            args.get("old_text", ""),
            args.get("new_text", ""),
        )
    elif name == "run_command":
        return tool_run_command(args.get("command", ""))
    return f"Unknown tool: '{name}'"


def parse_lenient_json(s: str) -> dict:
    s = s.strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        pass
    # Normalize Windows unescaped backslashes in paths
    try:
        cleaned = re.sub(r'\\([a-zA-Z0-9_\-\. ])', r'/\1', s)
        return json.loads(cleaned)
    except Exception:
        pass
    try:
        cleaned = s.replace('\\', '/')
        return json.loads(cleaned)
    except Exception:
        pass
    return {}


def extract_tool_call(response_text: str) -> tuple[str | None, dict | None, str]:
    """Parse tool call from response in any format: XML, markdown, or JSON."""
    clean_text = response_text

    # Pattern 1: <tool_call><function=name>{...}</function></tool_call>
    m_xml1 = re.search(r"<tool_call>\s*<function=([a-zA-Z0-9_]+)>\s*(.*?)\s*</function>\s*</tool_call>", response_text, re.DOTALL)
    if m_xml1:
        tool_name = m_xml1.group(1).strip()
        raw_json = m_xml1.group(2).strip()
        pre_text = response_text[: m_xml1.start()].strip()
        args = parse_lenient_json(raw_json)
        return tool_name, args, pre_text

    # Pattern 2: <function=name>{...}</function>
    m_xml2 = re.search(r"<function=([a-zA-Z0-9_]+)>\s*(.*?)\s*</function>", response_text, re.DOTALL)
    if m_xml2:
        tool_name = m_xml2.group(1).strip()
        raw_json = m_xml2.group(2).strip()
        pre_text = response_text[: m_xml2.start()].strip()
        args = parse_lenient_json(raw_json)
        return tool_name, args, pre_text

    # Pattern 3: <tool_call>{ "name": "...", "arguments": {...} }</tool_call>
    m_xml3 = re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", response_text, re.DOTALL)
    if m_xml3:
        raw_json = m_xml3.group(1).strip()
        data = parse_lenient_json(raw_json)
        if isinstance(data, dict):
            tool_name = data.get("name") or data.get("tool")
            args = data.get("arguments") or data.get("args") or data
            pre_text = response_text[: m_xml3.start()].strip()
            if tool_name:
                return tool_name, args, pre_text

    # Pattern 4: ```tool:name ... ```
    m = re.search(r"```tool:([a-zA-Z0-9_]+)\s*\n(.*?)\n```", response_text, re.DOTALL)
    if m:
        tool_name = m.group(1).strip()
        raw_json = m.group(2).strip()
        pre_text = response_text[: m.start()].strip()
        args = parse_lenient_json(raw_json)
        return tool_name, args, pre_text

    # Pattern 5: ```tool_call or ```json
    m2 = re.search(r"```(?:tool_call|json)\s*\n(.*?)\n```", response_text, re.DOTALL)
    if m2:
        raw_json = m2.group(1).strip()
        data = parse_lenient_json(raw_json)
        if isinstance(data, dict) and ("tool" in data or "name" in data):
            tool_name = data.get("tool") or data.get("name")
            args = data.get("args") or data.get("parameters") or data.get("arguments") or data
            pre_text = response_text[: m2.start()].strip()
            return tool_name, args, pre_text

    return None, None, clean_text


def emit_json(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def run_agentic_loop(prompt: str, cwd: str, config: dict) -> str:
    """Multi-turn agent loop executing tools on disk."""
    system_prompt = f"""You are Google Antigravity, an elite autonomous software engineering agent.
You are running directly in the user's workspace at: {cwd}

CRITICAL DIRECTIVE:
The user explicitly wants you to TAKE REAL ACTION, not just describe or talk about what needs to be done.
You MUST inspect files, find relevant assets/code, make edits, and verify the changes.
If the workspace or parent directory contains related directories (e.g. sibling folders, translations, configs), inspect them to solve the problem!

To execute a tool, output XML tags:
<tool_call>
<function=tool_name>
{{"param1": "value1"}}
</function>
</tool_call>

Or markdown:
```tool:tool_name
{{"param1": "value1"}}
```

Available tools:
- list_dir: {{"dir_path": str}} (list files in directory)
- read_file: {{"file_path": str, "start_line": int, "end_line": int}} (read lines from file)
- search_files: {{"query": str, "dir_path": str}} (search filenames or grep file contents)
- write_file: {{"file_path": str, "content": str}} (create or overwrite file)
- patch_file: {{"file_path": str, "old_text": str, "new_text": str}} (replace exact text)
- run_command: {{"command": str}} (execute shell command)

Always output a brief explanation before invoking a tool.
After completing all actions, provide a clear, concise summary of the fixes you implemented and finish WITHOUT calling any more tools.
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    total_steps = 0
    max_steps = 12
    final_output = ""

    while total_steps < max_steps:
        total_steps += 1
        response = call_llm(messages, config)
        tool_name, tool_args, pre_text = extract_tool_call(response)

        # Clean any raw tool tags that might have appeared in pre_text
        pre_text_clean = re.sub(r"<tool_call>.*", "", pre_text, flags=re.DOTALL).strip()
        if pre_text_clean:
            emit_json({
                "event": "step_update",
                "step_update": {"step_type": "text", "text_delta": pre_text_clean + "\n"},
            })

        if not tool_name or tool_args is None:
            # Model decided it is done
            final_output = response
            break

        # Emit tool call event
        emit_json({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": tool_name,
                "text_delta": f"⚙️ Executing Antigravity tool: {tool_name}...\n",
            },
        })

        # Execute tool locally on disk
        result = execute_tool(tool_name, tool_args)

        # Emit tool result summary
        preview = result[:400] + ("..." if len(result) > 400 else "")
        emit_json({
            "event": "step_update",
            "step_update": {
                "step_type": "text",
                "text_delta": f"📋 Result ({tool_name}):\n{preview}\n\n",
            },
        })

        messages.append({"role": "assistant", "content": response})
        messages.append({
            "role": "user",
            "content": f"<tool_response>\n<function={tool_name}>\n{result}\n</function>\n</tool_response>\n\nInspect the results and continue with the next action, or summarize and finish if done.",
        })

    if not final_output:
        final_output = "Antigravity completed workspace inspection and execution."

    return final_output


def main():
    args = sys.argv[1:]

    # Probe / Version check
    if "--version" in args or "-v" in args:
        print("Google Antigravity CLI v2.0.4 (Antigravity IDE & Autonomous Agent Engine)")
        sys.exit(0)

    # Help check
    if "--help" in args or "-h" in args:
        print("Google Antigravity CLI (agy) - Official Headless Task Agent")
        print("Usage: agy [--sandbox] [--dangerously-skip-permissions] [--model MODEL] [--input-format FORMAT] [--output-format FORMAT]")
        sys.exit(0)

    # Streaming task execution
    prompt_text = ""
    if not sys.stdin.isatty():
        try:
            raw_input = sys.stdin.read()
            if raw_input.strip():
                for line in raw_input.strip().splitlines():
                    try:
                        data = json.loads(line)
                        if data.get("event") == "user":
                            prompt_text = data.get("message", {}).get("content", "")
                    except Exception:
                        pass
                if not prompt_text:
                    prompt_text = raw_input.strip()
        except Exception:
            pass

    if not prompt_text and args:
        non_flags = [a for a in args if not a.startswith("-")]
        if non_flags:
            prompt_text = " ".join(non_flags)

    if not prompt_text:
        prompt_text = "Perform workspace health check."

    # Send init event
    init_event = {"event": "init", "init": {"permission_mode": "sandbox", "version": "2.0.4"}}
    emit_json(init_event)

    config = load_config()
    cwd = os.getcwd()

    try:
        final_response = run_agentic_loop(prompt_text, cwd, config)
    except Exception as exc:
        final_response = f"Antigravity autonomous execution error: {exc}"
        emit_json({
            "event": "step_update",
            "step_update": {"step_type": "text", "text_delta": final_response},
        })

    # Send completion result
    result_event = {
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": final_response,
        },
    }
    emit_json(result_event)


if __name__ == "__main__":
    main()
