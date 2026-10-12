import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity, ArrowUpRight, Bell, Bot, Check, CheckCircle2, ChevronDown,
  CircleAlert, Code2, Command, FolderGit2, Github,
  History, KeyRound, LoaderCircle, Plus, RefreshCw, Send,
  Settings2, ShieldAlert, ShieldCheck, Sparkles, Square, Terminal, Trash2, Workflow, X
} from "lucide-react";

type Agent = { id: string; name: string; description: string; state: string; diagnostic?: string; capabilities?: { supported_models?: string[] } };
type Health = { status: string; version: string; workspace: string; active_tasks: number };
type Task = { task_id: string; status: string; agent_id: string; prompt?: string; workspace_path?: string };
type Ev = { task_id?: string; event_type?: string; text?: string; timestamp?: number; data?: Record<string, unknown> };
type Plan = { plan_id: string; goal: string; subtasks: { id: string; title: string; assigned_agent: string; target_files: string[]; dependencies: string[] }[]; parallel_batches: string[][] };
type PermissionMode = "safe" | "balanced" | "god";
type NotificationPrefs = { enabled: boolean; completion: boolean; action: boolean; sound: string };
type Provider = { id: string; name: string; base_url: string; model: string; key_configured: boolean; key_count?: number };
type RepoResult = { repository_url: string; repository_name: string; workspace_path: string; branch: string; cloned: boolean; analysis_safety: string };
const API = "http://127.0.0.1:8765";
const REVERSE_ENGINEER_PROMPT = "Reverse-engineer this repository before making changes. Do not execute project code or install dependencies. First map the architecture, entry points, major modules, data flow, external services, configuration, tests, and build system. Identify the most important files and explain how the pieces work together. Then report risks, missing documentation, likely failure points, and the best next steps. Cite concrete file paths and symbols for every important claim. Start with a concise executive summary, then a component map, request/data flow, test strategy, and prioritized findings.";
const label = (value: string) => value.replace(/_/g, " ");
const complexPrompt = (value: string) => value.trim().length > 240 || /\b(refactor|architecture|implement.*(entire|full|complete)|multiple files|end.to.end|reverse.engineer|migrate|build.*platform|large task|several modules|whole project)\b/i.test(value);
const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const DEFAULT_TOKEN = "5e542c42b89db11fc7a10a2e19fa6e0929fd09d8d5a4c204a5414ead6c42a6be";
export default function App() {
  const [base, setBase] = useState(sessionStorage.getItem("cc_api") || API);
  const [token, setToken] = useState(sessionStorage.getItem("cc_token") || localStorage.getItem("cc_token") || DEFAULT_TOKEN);
  const [workspace, setWorkspace] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agent, setAgent] = useState("auto");
  const [prompt, setPrompt] = useState("");
  const [conversationPrompt, setConversationPrompt] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [activeStep, setActiveStep] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [settings, setSettings] = useState(false);
  const [contextOpen, setContextOpen] = useState(true);
  const [repoOpen, setRepoOpen] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [repoFolder, setRepoFolder] = useState("");
  const [repoBusy, setRepoBusy] = useState(false);
  const [repo, setRepo] = useState<RepoResult | null>(null);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>((localStorage.getItem("cc_permission_mode") as PermissionMode) || "safe");
  const [notifications, setNotifications] = useState<NotificationPrefs>(() => {
    try { return { enabled: false, completion: true, action: true, sound: "chime", ...JSON.parse(localStorage.getItem("cc_notifications") || "{}") }; }
    catch { return { enabled: false, completion: true, action: true, sound: "chime" }; }
  });
  const [providers, setProviders] = useState<Provider[]>([]);
  const [providerDraft, setProviderDraft] = useState({ id: "", name: "", base_url: "https://api.example.com/v1", model: "" });
  const [providerKey, setProviderKey] = useState("");
  const [providerBusy, setProviderBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const latestTaskId = useRef<string | null>(null);
  const responseRef = useRef<HTMLDivElement | null>(null);

  const request = useCallback(async (path: string, init: RequestInit = {}) => {
    if (!token.trim()) throw new Error("Add the local backend token in Settings. It is not pre-filled with a shared default.");
    const headers = new Headers(init.headers);
    headers.set("Authorization", "Bearer " + token.trim());
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(base.replace(/\/$/, "") + path, { ...init, headers });
    if (!response.ok) {
      let detail = response.status + " " + response.statusText;
      try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : detail; } catch { /* response may not be JSON */ }
      throw new Error(detail);
    }
    return response;
  }, [base, token]);

  const playSound = (sound: string) => {
    if (sound === "none") return;
    try {
      const AudioCtx = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const notes = sound === "bell" ? [880, 1175, 880] : sound === "alert" ? [660, 440, 660] : [660, 880];
      let at = ctx.currentTime;
      notes.forEach(hz => {
        const osc = ctx.createOscillator(), gain = ctx.createGain();
        osc.type = sound === "bell" ? "sine" : "triangle";
        osc.frequency.value = hz;
        gain.gain.setValueAtTime(0.0001, at);
        gain.gain.exponentialRampToValueAtTime(0.08, at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.18);
        osc.connect(gain); gain.connect(ctx.destination); osc.start(at); osc.stop(at + 0.2); at += 0.23;
      });
      window.setTimeout(() => void ctx.close(), 1000);
    } catch { /* Audio may be unavailable. */ }
  };
  const notify = (kind: "completion" | "action", title: string, body: string) => {
    if (!notifications.enabled || !notifications[kind]) return;
    playSound(notifications.sound);
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification(title, { body, tag: "code-conductor-" + kind }); } catch { /* Notifications may be unavailable. */ }
    }
  };
  const saveNotifications = (next: NotificationPrefs) => {
    setNotifications(next);
    localStorage.setItem("cc_notifications", JSON.stringify(next));
  };
  const setMode = (next: PermissionMode) => {
    setPermissionMode(next);
    localStorage.setItem("cc_permission_mode", next);
  };
  const connect = useCallback(async () => {
    setError("");
    try {
      const [h, a, p] = await Promise.all([
        request("/health").then(r => r.json()),
        request("/agents").then(r => r.json()),
        request("/providers").then(r => r.json()).catch(() => [])
      ]);
      setHealth(h); setAgents(a); setProviders(p);
      setWorkspace(current => current || h.workspace || "");
      sessionStorage.setItem("cc_api", base);
      sessionStorage.setItem("cc_token", token.trim());
      localStorage.setItem("cc_token", token.trim());
      setNotice("Connected to your local coding workspace.");
    } catch (e) {
      setHealth(null); setAgents([]);
      setError(e instanceof Error ? e.message : "Connection failed");
    }
  }, [base, token, request]);
  useEffect(() => { if (token.trim()) void connect(); }, []); // connect once when a token was explicitly saved
  useEffect(() => { if (events.length) responseRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [events.length]);

  const makePlan = async (text = prompt) => {
    if (!text.trim()) return null;
    setConversationPrompt(text.trim());
    setPlanning(true); setError(""); setNotice("");
    try {
      const next = await request("/orchestrator/plan", {
        method: "POST",
        body: JSON.stringify({ prompt: text.trim(), agent_id: agent, workspace_path: workspace || undefined })
      }).then(r => r.json()) as Plan;
      setPlan(next);
      setNotice("Plan ready · " + next.subtasks.length + " steps in " + next.parallel_batches.length + " dependency batches.");
      return next;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not build task plan");
      return null;
    } finally { setPlanning(false); }
  };

  const streamTask = async (created: Task, signal: AbortSignal) => {
    latestTaskId.current = created.task_id;
    setTask(created);
    const response = await request("/tasks/" + encodeURIComponent(created.task_id) + "/events", { signal });
    if (!response.body) throw new Error("No event stream returned.");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "", taskOutput = "";
    let taskFailed = false;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || "";
      for (const block of blocks) {
        const data = block.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trim()).join("\n");
        if (!data || data === "[DONE]") continue;
        try {
          const event = JSON.parse(data) as Ev;
          setEvents(old => [...old, event]);
          if (event.event_type === "token") taskOutput += event.text || "";
          if (event.event_type === "error") taskFailed = true;
          if (event.event_type === "completed") notify("completion", "Code Conductor · Task finished", "The agent completed a task step.");
          if (event.event_type === "error") notify("action", "Code Conductor · Action needed", event.text || "The task needs attention.");
          if (event.event_type === "needs_action" || ((event.event_type === "tool_call" || event.event_type === "log") && /permission|approval|confirm|waiting|blocked|soft-denied/i.test(event.text || ""))) {
            notify("action", "Code Conductor · Action needed", event.text || "The agent may be waiting for approval.");
          }
          if (event.event_type === "completed" || event.event_type === "error" || event.event_type === "cancelled") {
            setTask(old => old ? { ...old, status: event.event_type === "completed" ? "completed" : event.event_type === "error" ? "failed" : "cancelled" } : old);
          }
        } catch { /* Ignore malformed SSE frames. */ }
      }
    }
    return { output: taskOutput, failed: taskFailed };
  };

  const dispatch = async (taskPrompt: string, selectedAgent: string, signal: AbortSignal) => {
    const created = await request("/tasks", {
      method: "POST",
      body: JSON.stringify({
        prompt: taskPrompt, agent_id: selectedAgent, workspace_path: workspace || undefined,
        stream: true, auto_approve: false, permission_mode: permissionMode,
        god_mode_confirmed: permissionMode === "god"
      })
    }).then(r => r.json()) as Task;
    return streamTask(created, signal);
  };

  const runTask = async () => {
    if (!prompt.trim() || busy) return;
    if (!health) { setError("Connect the local backend before running a task."); setSettings(true); return; }
    if (permissionMode === "god" && !window.confirm("GOD MODE WARNING\n\nThis can approve tool actions and run commands broadly. Continue only with a trusted prompt and reviewed workspace.")) return;
    setError(""); setNotice(""); setBusy(true); setEvents([]); setTask(null); setActiveStep(-1);
    const originalPrompt = prompt.trim();
    let executionFailed = false;
    setConversationPrompt(originalPrompt);
    const controller = new AbortController(); abort.current = controller;
    try {
      let selectedPlan = plan;
      if (!selectedPlan && complexPrompt(originalPrompt)) {
        selectedPlan = await makePlan(originalPrompt);
        if (!selectedPlan) return;
        setNotice("Large task detected. Review the plan, then choose Run plan to execute its steps.");
        return;
      }
      if (selectedPlan && selectedPlan.subtasks.length > 0) {
        const outputs: string[] = [];
        let planFailed = false;
        for (let i = 0; i < selectedPlan.subtasks.length; i++) {
          if (controller.signal.aborted) break;
          const step = selectedPlan.subtasks[i];
          setActiveStep(i);
          setEvents(old => [...old, { event_type: "log", text: "Plan step " + (i + 1) + "/" + selectedPlan.subtasks.length + " · " + step.title, timestamp: Date.now() / 1000 }]);
          const stepAgent = agents.some(a => a.id === step.assigned_agent) ? step.assigned_agent : agent;
          const context = outputs.slice(-2).join("\n\n").slice(-3500);
          const stepPrompt = [
            "OVERALL GOAL:\n" + originalPrompt,
            "CURRENT PLANNED STEP (" + (i + 1) + " of " + selectedPlan.subtasks.length + "):\n" + step.title,
            "TARGET FILES:\n" + (step.target_files.join(", ") || "Inspect the relevant repository files first."),
            "DEPENDENCIES:\n" + (step.dependencies.join(", ") || "No declared dependencies."),
            "WORKSPACE:\n" + (workspace || "Use the workspace supplied by the backend."),
            context ? "RECENT RESULTS FROM EARLIER STEPS:\n" + context : "",
            "Work only on this step, inspect the existing code before editing, respect repository conventions, and report changed files and verification performed. Do not claim tests passed unless you ran them."
          ].filter(Boolean).join("\n\n");
          const result = await dispatch(stepPrompt, stepAgent, controller.signal);
          outputs.push("Step " + (i + 1) + " — " + step.title + ":\n" + result.output.slice(-3500));
          if (result.failed) { planFailed = true; executionFailed = true; setNotice("Plan paused because step " + (i + 1) + " failed. Fix the error, then review the workspace before continuing."); break; }
        }
        if (!controller.signal.aborted && !planFailed) {
          setNotice("Plan execution finished. Review the response and run tests before accepting changes.");
          setTask(old => old ? { ...old, status: "completed" } : old);
        }
      } else {
        setActiveStep(0);
        await dispatch(originalPrompt, agent, controller.signal);
      }
      if (!executionFailed) setPrompt("");
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) setError(e instanceof Error ? e.message : "Task failed");
    } finally { setBusy(false); abort.current = null; latestTaskId.current = null; }
  };

  const cancel = async () => {
    if (latestTaskId.current) {
      try { await request("/tasks/" + encodeURIComponent(latestTaskId.current) + "/cancel", { method: "POST" }); } catch { /* abort below */ }
    }
    abort.current?.abort(); setBusy(false); setNotice("Stop requested.");
  };

  const importRepository = async () => {
    if (!repoUrl.trim() || repoBusy) return;
    if (!health) { setError("Connect the local backend before importing a repository."); setSettings(true); return; }
    setRepoBusy(true); setError(""); setNotice("");
    try {
      const result = await request("/workspace/repository/clone", {
        method: "POST",
        body: JSON.stringify({ repository_url: repoUrl.trim(), parent_path: workspace || health.workspace || undefined, directory_name: repoFolder.trim() || undefined })
      }).then(r => r.json()) as RepoResult;
      setRepo(result); setWorkspace(result.workspace_path); setRepoOpen(false); setRepoUrl(""); setRepoFolder("");
      setConversationPrompt("");
      setPrompt("Reverse-engineer this GitHub repository. " + REVERSE_ENGINEER_PROMPT);
      setPlan(null); setEvents([]);
      setNotice("Repository cloned into " + result.workspace_path + ". Nothing from the repository was executed. Review the workspace, then plan the analysis.");
    } catch (e) { setError(e instanceof Error ? e.message : "Repository import failed"); }
    finally { setRepoBusy(false); }
  };

  const runTests = async () => {
    try {
      const result = await request("/workspace/test/run", {
        method: "POST", body: JSON.stringify({ command_key: "pytest", timeout_seconds: 120, workspace_path: workspace || undefined })
      }).then(r => r.json());
      setNotice(result.passed ? "Tests passed." : "Tests failed with exit code " + result.exit_code + ".");
      setEvents(old => [...old, { event_type: result.passed ? "completed" : "error", text: (result.stdout || "") + (result.stderr || ""), timestamp: Date.now() / 1000 }]);
    } catch (e) { setError(e instanceof Error ? e.message : "Test run failed"); }
  };
  const saveProvider = async () => {
    setProviderBusy(true); setError("");
    try {
      const saved = await request("/providers", { method: "POST", body: JSON.stringify({ ...providerDraft, api_key: providerKey }) }).then(r => r.json()) as Provider;
      setProviders(old => [...old.filter(p => p.id !== saved.id), saved]); setProviderKey("");
      setNotice("Provider saved in the operating system credential store.");
    } catch (e) { setError(e instanceof Error ? e.message : "Provider save failed"); }
    finally { setProviderBusy(false); }
  };
  const removeProvider = async (provider: Provider) => {
    if (!window.confirm("Delete " + provider.name + " and its stored API key?")) return;
    try { await request("/providers/" + encodeURIComponent(provider.id), { method: "DELETE" }); setProviders(old => old.filter(p => p.id !== provider.id)); }
    catch (e) { setError(e instanceof Error ? e.message : "Provider delete failed"); }
  };

  const ready = agents.filter(a => a.state === "ready").length;
  const activeAgent = agents.find(a => a.id === (task?.agent_id || agent));
  const runningLabel = activeStep >= 0 && plan ? "Step " + (activeStep + 1) + " of " + plan.subtasks.length : "Working";
  return <div className="app-shell">
    <aside className="rail">
      <div className="brand-lockup"><div className="brand-symbol"><Sparkles size={20}/></div><div><strong>conductor</strong><span>AI workspace</span></div></div>
      <button className="new-chat" onClick={() => { if (busy) return; setPrompt(""); setConversationPrompt(""); setEvents([]); setPlan(null); setTask(null); setRepo(null); setNotice(""); setError(""); }}><Plus size={16}/> New task <span>⌘ K</span></button>
      <div className="rail-label">WORKSPACE</div>
      <div className="workspace-mini"><div className="folder-glyph"><FolderGit2 size={17}/></div><div className="workspace-name"><b>{workspace.split(/[\\/]/).filter(Boolean).pop() || "No project open"}</b><span>{repo?.repository_name || (health ? "Local workspace" : "Connect backend")}</span></div><button className="icon-button" title="Import GitHub repository" onClick={() => setRepoOpen(v => !v)}><Github size={15}/></button></div>
      <div className="rail-label">AGENTS <span>{ready}/{agents.length || 0}</span></div>
      <div className="rail-agents">{agents.slice(0, 7).map(a => <button key={a.id} className={"rail-agent " + (agent === a.id ? "selected" : "")} onClick={() => setAgent(a.id)}><span className={"agent-dot " + (a.state === "ready" ? "ready" : "")}/><span>{a.name}</span><small>{a.state === "ready" ? "Ready" : label(a.state)}</small></button>)}
        {!agents.length && <button className="rail-agent muted-agent" onClick={() => setSettings(true)}><span className="agent-dot"/><span>Connect an agent</span><small>Setup</small></button>}
      </div>
      <div className="rail-bottom">
        <div className="connection-pill"><span className={"connection-dot " + (health ? "ready" : "")}/><div><b>{health ? "Backend online" : "Backend offline"}</b><small>{health ? "localhost · v" + health.version : "Local API connection"}</small></div></div>
        <button className="rail-action" onClick={() => setSettings(true)}><Settings2 size={16}/> Settings</button>
      </div>
    </aside>

    <main className="chat-shell">
      <header className="topbar">
        <div className="top-project"><div className="top-project-icon"><Code2 size={16}/></div><div><span>PROJECT</span><b>{workspace.split(/[\\/]/).filter(Boolean).pop() || "New workspace"}</b></div><ChevronDown size={14}/></div>
        <div className="top-actions"><span className={"status-chip " + (health ? "connected" : "")}><span/> {health ? "Connected" : "Offline"}</span><button className="icon-button" title="Toggle task context" onClick={() => setContextOpen(v => !v)}><Activity size={16}/></button><button className="icon-button" title="Settings" onClick={() => setSettings(true)}><Settings2 size={16}/></button></div>
      </header>

      <div className="chat-layout">
        <section className="conversation">
          <div className="conversation-scroll">
            {!task && !events.length && !plan ? <div className="welcome-state">
              <div className="welcome-orb"><div className="orb-core"><Sparkles size={25}/></div><span className="orb-ring ring-one"/><span className="orb-ring ring-two"/></div>
              <div className="welcome-eyebrow"><span/> YOUR ENGINEERING PARTNER</div>
              <h1>What are we <em>building</em><br/>today?</h1>
              <p>Explore a codebase, untangle a bug, or ship a feature.<br/>Your agents work with you, not behind a curtain.</p>
              <div className="starter-grid">
                <button onClick={() => setPrompt("Explain the architecture of my current project. Map entry points, major modules, and data flow with file references.")}><span className="starter-icon violet"><Workflow size={17}/></span><b>Understand this codebase</b><small>Architecture and data flow</small><ArrowUpRight size={14}/></button>
                <button onClick={() => { setRepoOpen(true); setPrompt(REVERSE_ENGINEER_PROMPT); }}><span className="starter-icon blue"><Github size={17}/></span><b>Reverse-engineer a repo</b><small>Clone a GitHub project safely</small><ArrowUpRight size={14}/></button>
                <button onClick={() => setPrompt("Investigate a bug in this project. Trace the likely cause through the code, propose a minimal fix, and add or update a regression test.")}><span className="starter-icon amber"><CircleAlert size={17}/></span><b>Investigate a bug</b><small>Trace → fix → verify</small><ArrowUpRight size={14}/></button>
                <button onClick={() => setPrompt("Review this project for security, reliability, and maintainability issues. Prioritize findings by severity and cite exact files and symbols.")}><span className="starter-icon green"><ShieldCheck size={17}/></span><b>Review the project</b><small>Prioritized engineering findings</small><ArrowUpRight size={14}/></button>
              </div>
            </div> : <div className="thread">
              {conversationPrompt && <div className="message user-message"><div className="message-avatar user-avatar">YOU</div><div className="message-content"><div className="message-meta"><b>You</b><span>{clock()}</span></div><p>{conversationPrompt}</p></div></div>}
              {plan && <div className="plan-card"><div className="plan-card-head"><div className="plan-icon"><Workflow size={18}/></div><div><b>Execution plan</b><span>{plan.subtasks.length} steps · {plan.parallel_batches.length} dependency batches</span></div><span className="review-badge">REVIEW BEFORE RUN</span></div><p>{plan.goal}</p><div className="plan-step-list">{plan.subtasks.map((step, i) => <div className={"plan-step " + (activeStep === i ? "current" : activeStep > i ? "finished" : "")} key={step.id}><div className="step-number">{activeStep > i ? <Check size={14}/> : String(i + 1).padStart(2, "0")}</div><div className="step-main"><b>{step.title}</b><span>{step.target_files.join(", ") || "Inspect repository"}{step.dependencies.length ? " · depends on " + step.dependencies.join(", ") : ""}</span></div><small>{step.assigned_agent || agent}</small></div>)}</div><div className="plan-card-foot"><span><ShieldCheck size={14}/> Plan is visible before execution</span><button className="subtle-button" onClick={() => setPlan(null)}>Dismiss</button></div></div>}
              {(task || events.length > 0) && <div className="message assistant-message"><div className="message-avatar assistant-avatar"><Sparkles size={16}/></div><div className="message-content"><div className="message-meta"><b>Conductor</b><span>{busy ? runningLabel : task?.status === "failed" ? "Stopped with an error" : task?.status === "completed" ? "Finished" : "Response"}</span><span className="agent-label">{activeAgent?.name || task?.agent_id || agent}</span></div>
                {busy && !events.length && <div className="thinking-row"><span className="thinking-dots"><i/><i/><i/></span><span>{runningLabel} · waiting for the agent response</span></div>}
                {events.length > 0 && <div className="response-body">{events.map((event, i) => {
                  if (event.event_type === "token") return <span key={i}>{event.text}</span>;
                  if (event.event_type === "tool_call") return <div className="event-card tool-event" key={i}><Terminal size={14}/><span>{event.text || "Agent requested a tool"}</span></div>;
                  if (event.event_type === "tool_result") return <div className="event-card" key={i}><CheckCircle2 size={14}/><span>{event.text}</span></div>;
                  if (event.event_type === "error") return <div className="event-card error-event" key={i}><CircleAlert size={14}/><span>{event.text}</span></div>;
                  if (event.event_type === "log") return <div className="event-card" key={i}><Activity size={14}/><span>{event.text}</span></div>;
                  if (event.event_type === "started") return <div className="event-card" key={i}><LoaderCircle size={14}/><span>{event.text || "Task started"}</span></div>;
                  return null;
                })}</div>}
                {busy && events.length > 0 && <div className="streaming-cursor"><span/> Generating response</div>}
                {!busy && task?.status === "completed" && <div className="answer-actions"><span><CheckCircle2 size={14}/> Response complete</span><button onClick={() => void runTests()}><Terminal size={14}/> Run tests</button><button onClick={() => { setPrompt("Review the changes from the last task. Inspect the diff and identify regressions or missing tests."); setPlan(null); }}><ShieldCheck size={14}/> Review changes</button></div>}
              </div></div>}
              {error && <div className="inline-error"><CircleAlert size={16}/><span>{error}</span><button onClick={() => setError("")}><X size={14}/></button></div>}
              <div ref={responseRef}/>
            </div>}
          </div>

          {repoOpen && <div className="repo-import-card"><div className="repo-import-title"><div className="repo-icon"><Github size={17}/></div><div><b>Bring a GitHub repository into this workspace</b><span>Shallow clone · inspect first · never auto-run repository code</span></div><button className="icon-button" onClick={() => setRepoOpen(false)}><X size={15}/></button></div><label>Public GitHub repository URL<input value={repoUrl} onChange={e => setRepoUrl(e.target.value)} placeholder="https://github.com/owner/repository" onKeyDown={e => { if (e.key === "Enter") void importRepository(); }}/></label><div className="repo-import-row"><label>Folder name <input value={repoFolder} onChange={e => setRepoFolder(e.target.value)} placeholder="Use repository name"/></label><button className="primary-button" disabled={repoBusy || !repoUrl.trim()} onClick={() => void importRepository()}>{repoBusy ? <LoaderCircle className="spin" size={15}/> : <Github size={15}/>} {repoBusy ? "Cloning…" : "Clone & prepare analysis"}</button></div><p className="safety-note"><ShieldCheck size={13}/> Only public github.com URLs are accepted. No install, build, hooks, or scripts are run during import.</p></div>}

          <div className="composer-wrap">
            {notice && <div className="notice-line"><CheckCircle2 size={14}/><span>{notice}</span><button onClick={() => setNotice("")}><X size={13}/></button></div>}
            <div className="composer">
              <textarea value={prompt} onChange={e => { const value = e.target.value; setPrompt(value); if (plan && value.trim() !== plan.goal) setPlan(null); }} onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); void runTask(); } }} placeholder="Ask Conductor to explore, plan, debug, or build something…" rows={3}/>
              <div className="composer-bottom"><div className="composer-tools">
                <label className="select-pill"><Bot size={14}/><select value={agent} onChange={e => setAgent(e.target.value)}><option value="auto">Auto agent</option>{agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select><ChevronDown size={12}/></label>
                <label className={"select-pill permission-pill " + (permissionMode === "god" ? "danger-mode" : "")}><ShieldCheck size={14}/><select aria-label="Permission mode" value={permissionMode} onChange={e => setMode(e.target.value as PermissionMode)}><option value="safe">Safe</option><option value="balanced">Balanced</option><option value="god">God mode</option></select><ChevronDown size={12}/></label>
                <button className="tool-pill" onClick={() => setRepoOpen(v => !v)}><Github size={14}/> GitHub repo</button>
                <button className="tool-pill" onClick={() => void makePlan()} disabled={!prompt.trim() || planning}><Workflow size={14}/>{planning ? "Planning…" : "Plan task"}</button>
              </div><div className="composer-submit">{busy ? <button className="stop-button" onClick={() => void cancel()}><Square size={13}/> Stop</button> : <button className="send-button" disabled={!prompt.trim() || !health} onClick={() => void runTask()}>{plan ? "Run plan" : "Run task"} <Send size={15}/></button>}</div></div>
              <div className="composer-footnote"><span><Command size={11}/> Enter to write · Ctrl/⌘ + Enter to run</span><span><ShieldCheck size={12}/> Safe by default</span></div>
            </div>
          </div>
        </section>

        {contextOpen && <aside className="context-panel">
          <div className="context-head"><div><span>WORKSPACE</span><b>Task context</b></div><button className="icon-button" onClick={() => setContextOpen(false)}><X size={15}/></button></div>
          <section className="context-section"><div className="context-section-title"><FolderGit2 size={15}/> Current project</div><div className="context-project"><div className="context-project-icon"><Code2 size={17}/></div><div><b>{workspace.split(/[\\/]/).filter(Boolean).pop() || "No project selected"}</b><span>{repo?.repository_url || "Local folder"}</span></div></div><label className="field-label">Workspace path<input value={workspace} onChange={e => setWorkspace(e.target.value)} placeholder="C:\Projects\my-app"/></label><button className="outline-button full-button" onClick={() => setRepoOpen(v => !v)}><Github size={14}/> Import GitHub repository</button>
          {repo && <div className="repo-status"><CheckCircle2 size={14}/><div><b>{repo.repository_name} imported</b><span>Branch: {repo.branch}</span><span>Downloaded only · not executed</span></div></div>}</section>
          <section className="context-section"><div className="context-section-title"><Bot size={15}/> Agent status <button onClick={() => void connect()} title="Refresh agents"><RefreshCw size={12}/></button></div>{agents.slice(0, 8).map(a => <button className="context-agent" key={a.id} onClick={() => setAgent(a.id)}><span className={"agent-dot " + (a.state === "ready" ? "ready" : "")}/><span>{a.name}</span><small>{label(a.state)}</small></button>)}{!agents.length && <p className="context-muted">Connect the backend to discover agents.</p>}</section>
          <section className="context-section"><div className="context-section-title"><Workflow size={15}/> Progress</div>{plan ? <><div className="progress-label"><span>{activeStep >= 0 ? "Executing plan" : "Plan ready"}</span><b>{activeStep >= 0 ? Math.min(activeStep + 1, plan.subtasks.length) : 0}/{plan.subtasks.length}</b></div><div className="progress-track"><span style={{ width: ((activeStep + 1) / plan.subtasks.length * 100) + "%" }}/></div>{plan.subtasks.map((s, i) => <div className="mini-step" key={s.id}><span className={"mini-step-dot " + (activeStep > i ? "done" : activeStep === i ? "active" : "")}>{activeStep > i ? <Check size={10}/> : i + 1}</span><span>{s.title}</span></div>)}</> : <p className="context-muted">Plan a larger task to see the steps, dependencies, and execution progress here.</p>}</section>
          <section className="context-section"><div className="context-section-title"><History size={15}/> Latest activity</div>{events.slice(-5).reverse().map((e, i) => <div className="mini-activity" key={i}><span className={"activity-dot " + (e.event_type === "error" ? "bad" : e.event_type === "completed" ? "good" : "")}/><div><b>{label(e.event_type || "event")}</b><span>{(e.text || "").slice(0, 85)}</span></div></div>)}{!events.length && <p className="context-muted">Tool calls, progress, and results will appear here.</p>}</section>
          <div className="context-bottom"><span className={"connection-dot " + (health ? "ready" : "")}/><span>{health ? "Connected to local backend" : "Backend connection required"}</span></div>
        </aside>}
      </div>
    </main>

    {settings && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setSettings(false); }}><section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title"><div className="settings-modal-head"><div><span>CONFIGURATION</span><h2 id="settings-title">Workspace settings</h2><p>Connect your local agents and tune how they work.</p></div><button className="icon-button" onClick={() => setSettings(false)}><X size={17}/></button></div>
      <label className="settings-field">Backend URL<input value={base} onChange={e => setBase(e.target.value)} placeholder="http://127.0.0.1:8765"/></label>
      <label className="settings-field">Local bearer token<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} placeholder="Paste the token from ~/.aihub_token"/></label>
      <div className="settings-callout"><ShieldCheck size={15}/><span>No default token is embedded in the UI. Use the token generated by your local backend. Provider keys remain in the OS credential store.</span></div>
      <div className="settings-section"><div className="settings-section-head"><Bell size={15}/> Notifications</div><label className="check-row"><input type="checkbox" checked={notifications.enabled} onChange={e => saveNotifications({ ...notifications, enabled: e.target.checked })}/> Enable notifications and sounds</label><label className="check-row"><input type="checkbox" checked={notifications.completion} onChange={e => saveNotifications({ ...notifications, completion: e.target.checked })}/> When a task completes</label><label className="check-row"><input type="checkbox" checked={notifications.action} onChange={e => saveNotifications({ ...notifications, action: e.target.checked })}/> When a task needs attention</label><label className="settings-field">Sound<select value={notifications.sound} onChange={e => saveNotifications({ ...notifications, sound: e.target.value })}><option value="chime">Soft chime</option><option value="bell">Bell</option><option value="alert">Alert</option><option value="none">Silent</option></select></label><div className="modal-actions"><button className="outline-button" onClick={() => { if ("Notification" in window) void Notification.requestPermission(); }}>Enable browser permission</button><button className="outline-button" onClick={() => playSound(notifications.sound)}>Test sound</button></div></div>
      <div className="settings-section"><div className="settings-section-head"><ShieldAlert size={15}/> Permissions</div><p className="settings-help">Safe is the default. God mode requires confirmation and can run commands or modify files broadly. Use it only with trusted prompts and a reviewed workspace.</p><label className="settings-field">Default permission mode<select value={permissionMode} onChange={e => setMode(e.target.value as PermissionMode)}><option value="safe">Safe</option><option value="balanced">Balanced</option><option value="god">God mode · high risk</option></select></label></div>
      <div className="settings-section"><div className="settings-section-head"><KeyRound size={15}/> Custom OpenAI-compatible provider</div><p className="settings-help">Text-only /chat/completions endpoints. API keys are stored in the operating system credential store, not browser storage.</p><div className="provider-form"><label className="settings-field">Provider ID<input value={providerDraft.id} onChange={e => setProviderDraft({ ...providerDraft, id: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "") })} placeholder="my-provider"/></label><label className="settings-field">Display name<input value={providerDraft.name} onChange={e => setProviderDraft({ ...providerDraft, name: e.target.value })} placeholder="My AI provider"/></label><label className="settings-field">Base URL<input value={providerDraft.base_url} onChange={e => setProviderDraft({ ...providerDraft, base_url: e.target.value })} placeholder="https://api.example.com/v1"/></label><label className="settings-field">Model ID<input value={providerDraft.model} onChange={e => setProviderDraft({ ...providerDraft, model: e.target.value })} placeholder="provider/model"/></label><label className="settings-field span-two">API key(s), one per line<textarea rows={3} autoComplete="off" value={providerKey} onChange={e => setProviderKey(e.target.value)} placeholder="API keys are sent to the local backend and stored in the OS credential store."/></label></div><div className="modal-actions"><button className="primary-button" disabled={providerBusy || !providerDraft.id || !providerDraft.name || !providerDraft.base_url || !providerDraft.model || !providerKey} onClick={() => void saveProvider()}>{providerBusy ? "Saving…" : "Save provider"}</button></div>{providers.map(p => <div className="provider-item" key={p.id}><div><b>{p.name}</b><span>{p.model} · {p.key_count || (p.key_configured ? 1 : 0)} key(s)</span></div><button className="icon-button" title="Delete provider" onClick={() => void removeProvider(p)}><Trash2 size={14}/></button></div>)}</div>
      <div className="modal-actions bottom-actions"><button className="outline-button" onClick={() => { setToken(""); sessionStorage.removeItem("cc_token"); setHealth(null); setAgents([]); }}>Clear token</button><button className="primary-button" onClick={() => { setSettings(false); sessionStorage.setItem("cc_api", base); sessionStorage.setItem("cc_token", token.trim()); void connect(); }}>Save & connect <ArrowUpRight size={14}/></button></div>
    </section></div>}
  </div>;
}
