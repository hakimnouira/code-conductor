import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity, ArrowUpRight, Bell, Bot, Camera, Check, CheckCircle2, ChevronDown,
  CircleAlert, Code2, Command, ExternalLink, Eye, FolderGit2, Github,
  Globe, History, KeyRound, Layers, LoaderCircle, Moon, Plus, Power, RefreshCw, Send,
  Settings2, ShieldAlert, ShieldCheck, Sparkles, Square, Sun, Terminal, Trash2, Workflow, X
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
type ConvSummary = { id: string; title: string; updated_at: number; updated_time: string; message_count: number; first_prompt: string };
type ConvDetail = { id: string; title: string; updated_at: number; updated_time: string; message_count: number; messages: { role: string; content: string; step?: number }[]; artifacts: { name: string; size: number; modified_time: string }[] };

// 4-Phase Guided Workflow types
type GuidedStep = { step_id: number; title: string; description: string; target_files: string[]; reference_citation?: string };
type GuidedPipeline = {
  prompt: string;
  status: string;
  phase1_research: {
    query?: string;
    reason?: string;
    repositories: { full_name: string; url: string; score: number; stars: number; license: string; description: string; local_path?: string | null; reasons?: string[] }[];
  };
  phase2_reference?: {
    repo_name: string;
    local_path?: string;
    license?: string;
    key_files: string[];
    design_patterns: string[];
  } | null;
  phase3_plan: GuidedStep[];
  phase4_verification: {
    status: string;
    provenance_report: {
      top_reference?: string;
      license?: string;
      isolation_path?: string;
      patterns_applied: string[];
    };
  };
};

// Browser Session types
type BrowserStatus = {
  connected: boolean;
  endpoint: string;
  browser?: string;
  protocol_version?: string;
  tab_count: number;
  error?: string;
};
type BrowserTab = {
  id: string;
  title: string;
  url: string;
  type: string;
};
type WebModel = {
  id: string;
  name: string;
  url: string;
  input_selector?: string;
  send_selector?: string;
  response_selector?: string;
  description: string;
  is_preset: boolean;
  state?: string;
  diagnostic?: string;
};

const API = "http://127.0.0.1:8765";
const REVERSE_ENGINEER_PROMPT = "Reverse-engineer this repository before making changes. Do not execute project code or install dependencies. First map the architecture, entry points, major modules, data flow, external services, configuration, tests, and build system. Identify the most important files and explain how the pieces work together. Then report risks, missing documentation, likely failure points, and the best next steps. Cite concrete file paths and symbols for every important claim. Start with a concise executive summary, then a component map, request/data flow, test strategy, and prioritized findings.";
const SAMPLE_GUIDED_PROMPT = "Build a beautiful project management dashboard";
const label = (value: string) => value.replace(/_/g, " ");
const complexPrompt = (value: string) => value.trim().length > 240 || /\b(refactor|architecture|implement.*(entire|full|complete)|multiple files|end.to.end|reverse.engineer|migrate|build.*platform|large task|several modules|whole project)\b/i.test(value);
const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const DEFAULT_TOKEN = "5e542c42b89db11fc7a10a2e19fa6e0929fd09d8d5a4c204a5414ead6c42a6be";

export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">(() => (localStorage.getItem("cc_theme") as "dark" | "light") || "dark");
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
  const [githubResearchEnabled, setGithubResearchEnabled] = useState(() => localStorage.getItem("cc_github_research") !== "false");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>((localStorage.getItem("cc_permission_mode") as PermissionMode) || "safe");
  const [notifications, setNotifications] = useState<NotificationPrefs>(() => {
    try { return { enabled: false, completion: true, action: true, sound: "chime", ...JSON.parse(localStorage.getItem("cc_notifications") || "{}") }; }
    catch { return { enabled: false, completion: true, action: true, sound: "chime" }; }
  });
  const [providers, setProviders] = useState<Provider[]>([]);
  const [providerDraft, setProviderDraft] = useState({ id: "", name: "", base_url: "https://api.example.com/v1", model: "" });
  const [providerKey, setProviderKey] = useState("");
  const [providerBusy, setProviderBusy] = useState(false);

  // Custom Animated Select Dropdown & Smart Add Provider
  const [agentDropdownOpen, setAgentDropdownOpen] = useState(false);
  const [permissionDropdownOpen, setPermissionDropdownOpen] = useState(false);
  const [smartAddOpen, setSmartAddOpen] = useState(false);
  const [smartSite, setSmartSite] = useState("");
  const [smartIsFree, setSmartIsFree] = useState(true);
  const [smartApiKey, setSmartApiKey] = useState("");
  const [smartBusy, setSmartBusy] = useState(false);
  const [smartShowKey, setSmartShowKey] = useState(false);

  // Antigravity History
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyList, setHistoryList] = useState<ConvSummary[]>([]);
  const [selectedConv, setSelectedConv] = useState<ConvDetail | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historySearch, setHistorySearch] = useState("");

  // 4-Phase Guided Workflow
  const [guidedModalOpen, setGuidedModalOpen] = useState(false);
  const [guidedBusy, setGuidedBusy] = useState(false);
  const [guidedPipeline, setGuidedPipeline] = useState<GuidedPipeline | null>(null);

  // Browser Session Control Center
  const [browserModalOpen, setBrowserModalOpen] = useState(false);
  const [browserStatus, setBrowserStatus] = useState<BrowserStatus | null>(null);
  const [browserTabs, setBrowserTabs] = useState<BrowserTab[]>([]);
  const [selectedTabId, setSelectedTabId] = useState<string>("");
  const [browserEndpointDraft, setBrowserEndpointDraft] = useState("http://127.0.0.1:9222");
  const [browserNavUrl, setBrowserNavUrl] = useState("https://google.com");
  const [browserScreenshot, setBrowserScreenshot] = useState<string | null>(null);
  const [browserBusy, setBrowserBusy] = useState(false);
  const [webModels, setWebModels] = useState<WebModel[]>([]);
  const [webModelDraft, setWebModelDraft] = useState({
    id: "",
    name: "",
    url: "https://chatgpt.com",
    input_selector: "",
    send_selector: "",
    response_selector: "",
    description: "",
  });
  const [testModelId, setTestModelId] = useState("web-chatgpt");
  const [testModelPrompt, setTestModelPrompt] = useState("Hello! Explain quantum computing in 1 sentence.");
  const [testModelResult, setTestModelResult] = useState("");
  const [testModelBusy, setTestModelBusy] = useState(false);
  const [cloakLaunching, setCloakLaunching] = useState(false);
  const [browserTabActive, setBrowserTabActive] = useState<"models" | "tabs">("models");
  const [customModelFormOpen, setCustomModelFormOpen] = useState(false);

  const abort = useRef<AbortController | null>(null);
  const latestTaskId = useRef<string | null>(null);
  const responseRef = useRef<HTMLDivElement | null>(null);

  // Close dropdowns on click outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest(".custom-dropdown-container")) {
        setAgentDropdownOpen(false);
        setPermissionDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Synchronize theme to document attribute
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("cc_theme", theme);
  }, [theme]);

  const toggleTheme = () => {
    setTheme(t => (t === "dark" ? "light" : "dark"));
  };

  const request = useCallback(async (path: string, init: RequestInit = {}) => {
    if (!token.trim()) throw new Error("Add the local backend token in Settings.");
    const headers = new Headers(init.headers);
    headers.set("Authorization", "Bearer " + token.trim());
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(base.replace(/\/$/, "") + path, { ...init, headers });
    if (!response.ok) {
      let detail = response.status + " " + response.statusText;
      try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : detail; } catch { /* not json */ }
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
    } catch { /* Audio unavailable */ }
  };

  const notify = (kind: "completion" | "action", title: string, body: string) => {
    if (!notifications.enabled || !notifications[kind]) return;
    playSound(notifications.sound);
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification(title, { body, tag: "code-conductor-" + kind }); } catch { /* Ignore */ }
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
      void fetchBrowserStatus();
    } catch (e) {
      setHealth(null); setAgents([]);
      setError(e instanceof Error ? e.message : "Connection failed");
    }
  }, [base, token, request]);

  useEffect(() => { if (token.trim()) void connect(); }, []);
  useEffect(() => { if (events.length) responseRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [events.length]);

  // -------------------------------------------------------------
  // Guided 4-Phase Workflow Execution
  // -------------------------------------------------------------
  const runGuidedWorkflow = async (targetPrompt = prompt) => {
    const text = targetPrompt.trim() || SAMPLE_GUIDED_PROMPT;
    setGuidedBusy(true); setError(""); setNotice("");
    try {
      setGuidedModalOpen(true);
      const res = await request("/workflow/run", {
        method: "POST",
        body: JSON.stringify({
          prompt: text,
          agent_id: agent,
          workspace_path: workspace || undefined,
          clone_references: true
        })
      }).then(r => r.json()) as GuidedPipeline;
      setGuidedPipeline(res);
      setNotice("Guided Workflow Pipeline completed! Inspect research, reference, and implementation plan.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Guided workflow failed");
    } finally {
      setGuidedBusy(false);
    }
  };

  const applyGuidedPlanToWorkspace = () => {
    if (!guidedPipeline || !guidedPipeline.phase3_plan.length) return;
    const subtasks = guidedPipeline.phase3_plan.map(s => ({
      id: "step-" + s.step_id,
      title: s.title,
      assigned_agent: agent,
      target_files: s.target_files,
      dependencies: s.step_id > 1 ? ["step-" + (s.step_id - 1)] : []
    }));
    const newPlan: Plan = {
      plan_id: "plan-guided-" + Date.now(),
      goal: guidedPipeline.prompt,
      subtasks,
      parallel_batches: subtasks.map(s => [s.id])
    };
    setPlan(newPlan);
    setPrompt(guidedPipeline.prompt);
    setConversationPrompt(guidedPipeline.prompt);
    setGuidedModalOpen(false);
    setNotice("Guided Workflow Plan loaded! Click 'Run plan' to execute.");
  };

  // -------------------------------------------------------------
  // Browser Session Control Center
  // -------------------------------------------------------------
  const fetchBrowserStatus = async () => {
    try {
      const res = await request("/browser/status").then(r => r.json()) as BrowserStatus;
      setBrowserStatus(res);
      if (res.connected) {
        setBrowserEndpointDraft(res.endpoint);
        void fetchBrowserTabs();
      }
    } catch {
      setBrowserStatus(null);
    }
  };

  const fetchBrowserTabs = async () => {
    try {
      const tabs = await request("/browser/tabs").then(r => r.json()) as BrowserTab[];
      setBrowserTabs(tabs);
      if (tabs.length > 0 && !selectedTabId) {
        setSelectedTabId(tabs[0].id);
        setBrowserNavUrl(tabs[0].url || "https://google.com");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load browser tabs");
    }
  };

  const updateBrowserEndpoint = async () => {
    if (!browserEndpointDraft.trim()) return;
    setBrowserBusy(true); setError("");
    try {
      const res = await request("/browser/configure", {
        method: "POST",
        body: JSON.stringify({ endpoint: browserEndpointDraft.trim() })
      }).then(r => r.json());
      setNotice(res.connected ? "Attached to browser at " + res.endpoint : (res.warning || "Endpoint updated"));
      void fetchBrowserStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not configure browser endpoint");
    } finally {
      setBrowserBusy(false);
    }
  };

  const navigateActiveTab = async () => {
    if (!browserNavUrl.trim()) return;
    setBrowserBusy(true); setError("");
    try {
      await request("/browser/navigate", {
        method: "POST",
        body: JSON.stringify({ url: browserNavUrl.trim(), tab_id: selectedTabId || undefined })
      });
      setNotice("Navigated browser tab to: " + browserNavUrl);
      void fetchBrowserTabs();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Navigation failed");
    } finally {
      setBrowserBusy(false);
    }
  };

  const captureScreenshot = async () => {
    setBrowserBusy(true); setError("");
    try {
      const query = selectedTabId ? "?tab_id=" + encodeURIComponent(selectedTabId) : "";
      const res = await request("/browser/screenshot" + query).then(r => r.json());
      if (res.image_data) {
        setBrowserScreenshot(res.image_data);
        setNotice("Captured screenshot from active tab.");
      } else {
        setError("No image data returned from tab.");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Screenshot capture failed");
    } finally {
      setBrowserBusy(false);
    }
  };

  const activateTab = async (tabId: string) => {
    setSelectedTabId(tabId);
    try {
      await request("/browser/tabs/activate/" + encodeURIComponent(tabId), { method: "POST" });
      const found = browserTabs.find(t => t.id === tabId);
      if (found) setBrowserNavUrl(found.url);
    } catch { /* Ignore */ }
  };

  // -------------------------------------------------------------
  // Standard Plan and Task Execution
  // -------------------------------------------------------------
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
        } catch { /* Ignore malformed SSE */ }
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
        god_mode_confirmed: permissionMode === "god", github_research: githubResearchEnabled
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
          if (result.failed) { planFailed = true; executionFailed = true; setNotice("Plan paused because step " + (i + 1) + " failed."); break; }
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
      try { await request("/tasks/" + encodeURIComponent(latestTaskId.current) + "/cancel", { method: "POST" }); } catch { /* abort */ }
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
      setNotice("Repository cloned into " + result.workspace_path + ".");
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
      setNotice("Provider saved in credential store.");
    } catch (e) { setError(e instanceof Error ? e.message : "Provider save failed"); }
    finally { setProviderBusy(false); }
  };

  const removeProvider = async (provider: Provider) => {
    if (!window.confirm("Delete " + provider.name + " and its stored API key?")) return;
    try { await request("/providers/" + encodeURIComponent(provider.id), { method: "DELETE" }); setProviders(old => old.filter(p => p.id !== provider.id)); }
    catch (e) { setError(e instanceof Error ? e.message : "Provider delete failed"); }
  };

  const fetchHistory = async () => {
    setHistoryBusy(true);
    try {
      const list = await request("/antigravity/history").then(r => r.json()) as ConvSummary[];
      setHistoryList(list);
      if (list.length > 0) void openConversation(list[0].id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load Antigravity history");
    } finally { setHistoryBusy(false); }
  };

  const openConversation = async (convId: string) => {
    setHistoryBusy(true);
    try {
      const detail = await request("/antigravity/history/" + encodeURIComponent(convId)).then(r => r.json()) as ConvDetail;
      setSelectedConv(detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load conversation details");
    } finally { setHistoryBusy(false); }
  };

  const loadIntoWorkspace = (detail: ConvDetail) => {
    const lastUser = [...detail.messages].reverse().find(m => m.role === "user");
    if (lastUser) {
      setPrompt(lastUser.content);
      setConversationPrompt(lastUser.content);
    }
    setNotice("Loaded prompt from Antigravity session: " + detail.title);
    setHistoryOpen(false);
  };

  const saveSmartProvider = async () => {
    if (!smartSite.trim() || !smartApiKey.trim() || smartBusy) return;
    setSmartBusy(true); setError(""); setNotice("");
    try {
      const res = await request("/providers/auto-detect", {
        method: "POST",
        body: JSON.stringify({
          site: smartSite.trim(),
          is_free: smartIsFree,
          api_key: smartApiKey.trim(),
        }),
      });
      const data = await res.json();
      setNotice(`✨ Connected ${data.detected.name}! Configured model: ${data.detected.model} (${data.detected.tier.toUpperCase()})`);

      const [agentsRes, providersRes] = await Promise.all([
        request("/agents").then(r => r.json()),
        request("/providers").then(r => r.json()).catch(() => []),
      ]);
      setAgents(agentsRes);
      setProviders(providersRes);

      // Automatically select newly registered agent
      const newAgentId = "custom-" + data.provider.id;
      setAgent(newAgentId);

      setSmartSite("");
      setSmartApiKey("");
      setSmartAddOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Provider detection failed");
    } finally {
      setSmartBusy(false);
    }
  };

  const getAgentBadge = (id: string) => {
    const lower = id.toLowerCase();
    if (lower === "auto") return { text: "Auto", className: "tag-auto" };
    if (lower.startsWith("web-") || lower.includes("cloak")) return { text: "Web API", className: "tag-web" };
    if (lower.includes("groq") || lower.includes("fast")) return { text: "Fast", className: "tag-fast" };
    if (lower.includes("apmix") || lower.includes("free") || lower.includes("tokenharbor")) return { text: "Free", className: "tag-free" };
    if (lower.includes("codecraft") || lower.includes("openai") || lower.includes("paid")) return { text: "Pro", className: "tag-paid" };
    if (lower.includes("local") || lower.includes("ollama")) return { text: "Local", className: "tag-local" };
    if (lower.includes("router")) return { text: "Dual", className: "tag-auto" };
    if (lower.includes("mock")) return { text: "Test", className: "" };
    return { text: "Ready", className: "" };
  };

  const fetchWebModels = async () => {
    try {
      const models = await request("/browser/models").then(r => r.json()) as WebModel[];
      setWebModels(models);
    } catch {
      setWebModels([]);
    }
  };

  const launchCloakBrowser = async (openUrl = "https://chatgpt.com") => {
    setCloakLaunching(true);
    setNotice("🚀 Launching CloakBrowser with persistent user profile...");
    try {
      await request("/browser/launch?url=" + encodeURIComponent(openUrl), { method: "POST" });
      await new Promise(r => setTimeout(r, 2200));
      await Promise.all([fetchBrowserStatus(), fetchWebModels(), fetchBrowserTabs()]);
      setNotice("✨ CloakBrowser connected! Log into your account and cookies stay saved permanently.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to launch CloakBrowser");
    } finally {
      setCloakLaunching(false);
    }
  };

  const openWebModelInCloak = async (model: WebModel) => {
    setBrowserBusy(true);
    try {
      await request("/browser/models/" + encodeURIComponent(model.id) + "/open", { method: "POST" });
      setNotice(`Opened ${model.name} in CloakBrowser. Log into your account!`);
      await Promise.all([fetchBrowserStatus(), fetchBrowserTabs(), fetchWebModels()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to open model tab in browser");
    } finally {
      setBrowserBusy(false);
    }
  };

  const saveCustomWebModel = async () => {
    if (!webModelDraft.name.trim() || !webModelDraft.url.trim()) return;
    setBrowserBusy(true);
    try {
      await request("/browser/models?open_now=true", {
        method: "POST",
        body: JSON.stringify(webModelDraft),
      });
      setNotice(`✨ Web Model '${webModelDraft.name}' registered & opened in CloakBrowser!`);
      setWebModelDraft({ id: "", name: "", url: "https://chatgpt.com", input_selector: "", send_selector: "", response_selector: "", description: "" });
      setCustomModelFormOpen(false);
      await Promise.all([fetchWebModels(), request("/agents").then(r => r.json()).then(setAgents)]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to register web model");
    } finally {
      setBrowserBusy(false);
    }
  };

  const deleteCustomWebModel = async (modelId: string) => {
    if (!window.confirm("Delete this web model from Code Conductor?")) return;
    try {
      await request("/browser/models/" + encodeURIComponent(modelId), { method: "DELETE" });
      await Promise.all([fetchWebModels(), request("/agents").then(r => r.json()).then(setAgents)]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete web model");
    }
  };

  const testWebModelStream = async () => {
    if (!testModelPrompt.trim() || testModelBusy) return;
    setTestModelBusy(true);
    setTestModelResult("");
    try {
      const res = await fetch((base || API) + "/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token.trim(),
        },
        body: JSON.stringify({
          model: testModelId,
          messages: [{ role: "user", content: testModelPrompt.trim() }],
          stream: true,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Request failed" }));
        throw new Error(err.detail || "API test failed");
      }

      const reader = res.body?.getReader();
      const decoder = new TextDecoder("utf-8");
      if (!reader) throw new Error("No readable response stream");

      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || trimmed === "data: [DONE]") continue;
          if (trimmed.startsWith("data: ")) {
            try {
              const data = JSON.parse(trimmed.slice(6));
              const chunk = data.choices?.[0]?.delta?.content || "";
              setTestModelResult(prev => prev + chunk);
            } catch { /* skip */ }
          }
        }
      }
    } catch (e) {
      setTestModelResult(prev => prev + "\n[Error: " + (e instanceof Error ? e.message : "API test error") + "]");
    } finally {
      setTestModelBusy(false);
    }
  };

  const shutdownSystem = async () => {
    if (!window.confirm("Turn off Code Conductor and shut down servers?")) return;
    try {
      await request("/system/shutdown", { method: "POST" });
    } catch { /* Server process exiting */ }
    setNotice("🛑 Code Conductor servers have been safely shut down. You can close this tab.");
    setHealth(null);
  };

  const ready = agents.filter(a => a.state === "ready").length;
  const activeAgent = agents.find(a => a.id === (task?.agent_id || agent));
  const activeAgentDisplay = agent === "auto" ? "Auto agent" : (agents.find(a => a.id === agent)?.name || agent);
  const runningLabel = activeStep >= 0 && plan ? "Step " + (activeStep + 1) + " of " + plan.subtasks.length : "Working";

  return (
    <div className="app-shell" data-theme={theme}>
      <aside className="rail">
        <div className="brand-lockup">
          <div className="brand-symbol"><Sparkles size={20}/></div>
          <div><strong>conductor</strong><span>AI workspace</span></div>
        </div>
        <button className="new-chat" onClick={() => { if (busy) return; setPrompt(""); setConversationPrompt(""); setEvents([]); setPlan(null); setTask(null); setRepo(null); setNotice(""); setError(""); }}>
          <Plus size={16}/> New task <span>⌘ K</span>
        </button>

        <div className="rail-label">WORKSPACE</div>
        <div className="workspace-mini">
          <div className="folder-glyph"><FolderGit2 size={17}/></div>
          <div className="workspace-name">
            <b>{workspace.split(/[\\/]/).filter(Boolean).pop() || "No project open"}</b>
            <span>{repo?.repository_name || (health ? "Local workspace" : "Connect backend")}</span>
          </div>
          <button className="icon-button" title="Import GitHub repository" onClick={() => setRepoOpen(v => !v)}><Github size={15}/></button>
        </div>

        <div className="rail-label">AGENTS <span>{ready}/{agents.length || 0}</span></div>
        <div className="rail-agents">
          {agents.slice(0, 7).map(a => (
            <button key={a.id} className={"rail-agent " + (agent === a.id ? "selected" : "")} onClick={() => setAgent(a.id)}>
              <span className={"agent-dot " + (a.state === "ready" ? "ready" : "")}/>
              <span>{a.name}</span>
              <small>{a.state === "ready" ? "Ready" : label(a.state)}</small>
            </button>
          ))}
          {!agents.length && (
            <button className="rail-agent muted-agent" onClick={() => setSettings(true)}>
              <span className="agent-dot"/><span>Connect an agent</span><small>Setup</small>
            </button>
          )}
        </div>

        <div className="rail-bottom">
          <button className="rail-action" onClick={() => void runGuidedWorkflow()}>
            <Sparkles size={16}/> 4-Phase Guided Flow
          </button>
          <button className="rail-action" onClick={() => { setBrowserModalOpen(true); void fetchBrowserStatus(); void fetchWebModels(); }}>
            <Globe size={16}/> CloakBrowser & Web APIs {browserStatus?.connected && <small>({browserStatus.tab_count})</small>}
          </button>
          <button className="rail-action" onClick={() => { setHistoryOpen(true); void fetchHistory(); }}>
            <History size={16}/> Antigravity Chats
          </button>
          <div className="connection-pill">
            <span className={"connection-dot " + (health ? "ready" : "")}/>
            <div>
              <b>{health ? "Backend online" : "Backend offline"}</b>
              <small>{health ? "localhost · v" + health.version : "Local API connection"}</small>
            </div>
          </div>
          <button className="rail-action highlight-action" onClick={() => setSmartAddOpen(true)}>
            <KeyRound size={16}/> + Add AI Provider
          </button>
          <button className="rail-action shutdown-action" title="Shut down servers and turn off" onClick={() => void shutdownSystem()}>
            <Power size={16}/> Turn Off Conductor
          </button>
          <button className="rail-action" onClick={() => setSettings(true)}><Settings2 size={16}/> Settings</button>
        </div>
      </aside>

      <main className="chat-shell">
        <header className="topbar">
          <div className="top-project">
            <div className="top-project-icon"><Code2 size={16}/></div>
            <div><span>PROJECT</span><b>{workspace.split(/[\\/]/).filter(Boolean).pop() || "New workspace"}</b></div>
            <ChevronDown size={14}/>
          </div>
          <div className="top-actions">
            <button className="status-chip add-api-chip" title="Connect any AI Provider automatically" onClick={() => setSmartAddOpen(true)}>
              <KeyRound size={13}/> + Add API
            </button>
            <button className="icon-button" title={"Toggle theme (current: " + theme + ")"} onClick={toggleTheme}>
              {theme === "dark" ? <Sun size={16}/> : <Moon size={16}/>}
            </button>
            <button className="status-chip" title="CloakBrowser & Web Models Control Center" onClick={() => { setBrowserModalOpen(true); void fetchBrowserStatus(); void fetchWebModels(); }}>
              <Globe size={13}/> {browserStatus?.connected ? "CloakBrowser (" + browserStatus.tab_count + " tabs)" : "CloakBrowser Offline"}
            </button>
            <span className={"status-chip " + (health ? "connected" : "")}><span/> {health ? "Connected" : "Offline"}</span>
            <button className="icon-button" title="Guided 4-Phase Workflow" onClick={() => void runGuidedWorkflow()}><Sparkles size={16}/></button>
            <button className="icon-button" title="Antigravity Chat History" onClick={() => { setHistoryOpen(true); void fetchHistory(); }}><History size={16}/></button>
            <button className="icon-button" title="Toggle task context" onClick={() => setContextOpen(v => !v)}><Activity size={16}/></button>
            <button className="icon-button" title="Settings" onClick={() => setSettings(true)}><Settings2 size={16}/></button>
            <button className="icon-button shutdown-btn" title="Turn Off / Shut Down Conductor" onClick={() => void shutdownSystem()}>
              <Power size={16}/>
            </button>
          </div>
        </header>

        <div className="chat-layout">
          <section className="conversation">
            <div className="conversation-scroll">
              {!task && !events.length && !plan ? (
                <div className="welcome-state">
                  <div className="welcome-orb">
                    <div className="orb-core"><Sparkles size={25}/></div>
                    <span className="orb-ring ring-one"/><span className="orb-ring ring-two"/>
                  </div>
                  <div className="welcome-eyebrow"><span/> GUIDED ENGINEERING STUDIO</div>
                  <h1>What are we <em>building</em><br/>today?</h1>
                  <p>Explore a codebase, run automated multi-phase research, or inspect live browser sessions.<br/>Your agents work with you with complete transparency.</p>
                  <div className="starter-grid">
                    <button onClick={() => void runGuidedWorkflow(SAMPLE_GUIDED_PROMPT)}>
                      <span className="starter-icon violet"><Sparkles size={17}/></span>
                      <b>“Build a beautiful project management dashboard”</b>
                      <small>Run complete 4-Phase Guided Workflow</small>
                      <ArrowUpRight size={14}/>
                    </button>
                    <button onClick={() => setPrompt("Explain the architecture of my current project. Map entry points, major modules, and data flow with file references.")}>
                      <span className="starter-icon blue"><Workflow size={17}/></span>
                      <b>Understand this codebase</b>
                      <small>Architecture and data flow map</small>
                      <ArrowUpRight size={14}/>
                    </button>
                    <button onClick={() => { setRepoOpen(true); setPrompt(REVERSE_ENGINEER_PROMPT); }}>
                      <span className="starter-icon green"><Github size={17}/></span>
                      <b>Reverse-engineer a repo</b>
                      <small>Clone & inspect a GitHub project safely</small>
                      <ArrowUpRight size={14}/>
                    </button>
                    <button onClick={() => { setBrowserModalOpen(true); void fetchBrowserStatus(); }}>
                      <span className="starter-icon amber"><Globe size={17}/></span>
                      <b>Control active browser session</b>
                      <small>Inspect open tabs & capture screenshots</small>
                      <ArrowUpRight size={14}/>
                    </button>
                  </div>
                </div>
              ) : (
                <div className="thread">
                  {conversationPrompt && (
                    <div className="message user-message">
                      <div className="message-avatar user-avatar">YOU</div>
                      <div className="message-content">
                        <div className="message-meta"><b>You</b><span>{clock()}</span></div>
                        <p>{conversationPrompt}</p>
                      </div>
                    </div>
                  )}
                  {plan && (
                    <div className="plan-card">
                      <div className="plan-card-head">
                        <div className="plan-icon"><Workflow size={18}/></div>
                        <div><b>Execution plan</b><span>{plan.subtasks.length} steps · {plan.parallel_batches.length} dependency batches</span></div>
                        <span className="review-badge">REVIEW BEFORE RUN</span>
                      </div>
                      <p>{plan.goal}</p>
                      <div className="plan-step-list">
                        {plan.subtasks.map((step, i) => (
                          <div className={"plan-step " + (activeStep === i ? "current" : activeStep > i ? "finished" : "")} key={step.id}>
                            <div className="step-number">{activeStep > i ? <Check size={14}/> : String(i + 1).padStart(2, "0")}</div>
                            <div className="step-main">
                              <b>{step.title}</b>
                              <span>{step.target_files.join(", ") || "Inspect repository"}{step.dependencies.length ? " · depends on " + step.dependencies.join(", ") : ""}</span>
                            </div>
                            <small>{step.assigned_agent || agent}</small>
                          </div>
                        ))}
                      </div>
                      <div className="plan-card-foot">
                        <span><ShieldCheck size={14}/> Plan is visible before execution</span>
                        <button className="subtle-button" onClick={() => setPlan(null)}>Dismiss</button>
                      </div>
                    </div>
                  )}
                  {(task || events.length > 0) && (
                    <div className="message assistant-message">
                      <div className="message-avatar assistant-avatar"><Sparkles size={16}/></div>
                      <div className="message-content">
                        <div className="message-meta">
                          <b>Conductor</b>
                          <span>{busy ? runningLabel : task?.status === "failed" ? "Stopped with an error" : task?.status === "completed" ? "Finished" : "Response"}</span>
                          <span className="agent-label">{activeAgent?.name || task?.agent_id || agent}</span>
                        </div>
                        {busy && !events.length && (
                          <div className="thinking-row"><span className="thinking-dots"><i/><i/><i/></span><span>{runningLabel} · waiting for the agent response</span></div>
                        )}
                        {events.length > 0 && (
                          <div className="response-body">
                            {events.map((event, i) => {
                              if (event.event_type === "token") return <span key={i}>{event.text}</span>;
                              if (event.event_type === "tool_call") return <div className="event-card tool-event" key={i}><Terminal size={14}/><span>{event.text || "Agent requested a tool"}</span></div>;
                              if (event.event_type === "tool_result") return <div className="event-card" key={i}><CheckCircle2 size={14}/><span>{event.text}</span></div>;
                              if (event.event_type === "error") return <div className="event-card error-event" key={i}><CircleAlert size={14}/><span>{event.text}</span></div>;
                              if (event.event_type === "log") return <div className="event-card" key={i}><Activity size={14}/><span>{event.text}</span></div>;
                              if (event.event_type === "started") return <div className="event-card" key={i}><LoaderCircle size={14}/><span>{event.text || "Task started"}</span></div>;
                              if (event.event_type === "step_update") return <div className="event-card" key={i}><Activity size={14}/><span>{event.text}</span></div>;
                              return null;
                            })}
                          </div>
                        )}
                        {busy && events.length > 0 && <div className="streaming-cursor"><span/> Generating response</div>}
                        {!busy && task?.status === "completed" && (
                          <div className="answer-actions">
                            <span><CheckCircle2 size={14}/> Response complete</span>
                            <button onClick={() => void runTests()}><Terminal size={14}/> Run tests</button>
                            <button onClick={() => { setPrompt("Review the changes from the last task. Inspect the diff and identify regressions or missing tests."); setPlan(null); }}><ShieldCheck size={14}/> Review changes</button>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                  {error && <div className="inline-error"><CircleAlert size={16}/><span>{error}</span><button onClick={() => setError("")}><X size={14}/></button></div>}
                  <div ref={responseRef}/>
                </div>
              )}
            </div>

            {repoOpen && (
              <div className="repo-import-card">
                <div className="repo-import-title">
                  <div className="repo-icon"><Github size={17}/></div>
                  <div><b>Bring a GitHub repository into this workspace</b><span>Shallow clone · inspect first · never auto-run repository code</span></div>
                  <button className="icon-button" onClick={() => setRepoOpen(false)}><X size={15}/></button>
                </div>
                <label>Public GitHub repository URL
                  <input value={repoUrl} onChange={e => setRepoUrl(e.target.value)} placeholder="https://github.com/owner/repository" onKeyDown={e => { if (e.key === "Enter") void importRepository(); }}/>
                </label>
                <div className="repo-import-row">
                  <label>Folder name <input value={repoFolder} onChange={e => setRepoFolder(e.target.value)} placeholder="Use repository name"/></label>
                  <button className="primary-button" disabled={repoBusy || !repoUrl.trim()} onClick={() => void importRepository()}>
                    {repoBusy ? <LoaderCircle className="spin" size={15}/> : <Github size={15}/>} {repoBusy ? "Cloning…" : "Clone & prepare analysis"}
                  </button>
                </div>
                <p className="safety-note"><ShieldCheck size={13}/> Only public github.com URLs are accepted. No install, build, hooks, or scripts are run during import.</p>
              </div>
            )}

            <div className="composer-wrap">
              {notice && <div className="notice-line"><CheckCircle2 size={14}/><span>{notice}</span><button onClick={() => setNotice("")}><X size={13}/></button></div>}
              <div className="composer">
                <textarea
                  value={prompt}
                  onChange={e => { const val = e.target.value; setPrompt(val); if (plan && val.trim() !== plan.goal) setPlan(null); }}
                  onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); void runTask(); } }}
                  placeholder="Ask Conductor to explore, plan, debug, or build something…"
                  rows={3}
                />
                <div className="composer-bottom">
                  <div className="composer-tools">
                    {/* Custom Animated Agent Dropdown */}
                    <div className="custom-dropdown-container">
                      <button
                        type="button"
                        className={"select-pill custom-select-trigger " + (agentDropdownOpen ? "active" : "")}
                        onClick={() => {
                          setAgentDropdownOpen(v => !v);
                          setPermissionDropdownOpen(false);
                        }}
                        aria-label="Select AI agent"
                      >
                        <Bot size={14} className="trigger-icon" />
                        <span>{activeAgentDisplay}</span>
                        <ChevronDown size={12} className={"chevron-icon " + (agentDropdownOpen ? "rotate-180" : "")} />
                      </button>

                      {agentDropdownOpen && (
                        <div className="custom-dropdown-popover agent-popover">
                          <div className="custom-dropdown-head">
                            <div className="dropdown-head-title">
                              <Bot size={13} />
                              <span>Select AI Agent</span>
                            </div>
                            <span className="dropdown-count">{agents.length + 1} available</span>
                          </div>

                          <div className="custom-dropdown-list">
                            {/* Auto Agent Option */}
                            <button
                              type="button"
                              className={"custom-dropdown-item " + (agent === "auto" ? "active" : "")}
                              onClick={() => { setAgent("auto"); setAgentDropdownOpen(false); }}
                            >
                              <div className="item-left">
                                <span className="item-dot ready" />
                                <div className="item-text">
                                  <span className="item-title">Auto agent</span>
                                  <span className="item-sub">Intelligent routing across ready models</span>
                                </div>
                              </div>
                              <div className="item-right">
                                <span className="badge-tag tag-auto">Auto</span>
                                {agent === "auto" && <Check size={13} className="item-check" />}
                              </div>
                            </button>

                            {/* Discovered & Registered Agents */}
                            {agents.map(a => {
                              const b = getAgentBadge(a.id);
                              const isSelected = agent === a.id;
                              return (
                                <button
                                  type="button"
                                  key={a.id}
                                  className={"custom-dropdown-item " + (isSelected ? "active" : "")}
                                  onClick={() => { setAgent(a.id); setAgentDropdownOpen(false); }}
                                >
                                  <div className="item-left">
                                    <span className={"item-dot " + (a.state === "ready" ? "ready" : "standby")} />
                                    <div className="item-text">
                                      <span className="item-title">{a.name}</span>
                                      <span className="item-sub">{a.description || a.id}</span>
                                    </div>
                                  </div>
                                  <div className="item-right">
                                    {b.text && <span className={"badge-tag " + b.className}>{b.text}</span>}
                                    {isSelected && <Check size={13} className="item-check" />}
                                  </div>
                                </button>
                              );
                            })}
                          </div>

                          <div className="custom-dropdown-footer">
                            <button
                              type="button"
                              className="dropdown-add-btn"
                              onClick={() => { setAgentDropdownOpen(false); setSmartAddOpen(true); }}
                            >
                              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                                <Sparkles size={13} />
                                <span>+ Add API / Provider</span>
                              </div>
                              <span className="tag-pill">AI Auto-Detect</span>
                            </button>
                          </div>
                        </div>
                      )}
                    </div>

                    {/* Custom Animated Permission Mode Dropdown */}
                    <div className="custom-dropdown-container">
                      <button
                        type="button"
                        className={"select-pill permission-pill custom-select-trigger " + (permissionMode === "god" ? "danger-mode " : "") + (permissionDropdownOpen ? "active" : "")}
                        onClick={() => {
                          setPermissionDropdownOpen(v => !v);
                          setAgentDropdownOpen(false);
                        }}
                        aria-label="Select permission mode"
                      >
                        {permissionMode === "god" ? <ShieldAlert size={14} className="trigger-icon" /> : <ShieldCheck size={14} className="trigger-icon" />}
                        <span>{permissionMode === "safe" ? "Safe" : permissionMode === "balanced" ? "Balanced" : "God mode"}</span>
                        <ChevronDown size={12} className={"chevron-icon " + (permissionDropdownOpen ? "rotate-180" : "")} />
                      </button>

                      {permissionDropdownOpen && (
                        <div className="custom-dropdown-popover permission-popover">
                          <div className="custom-dropdown-head">
                            <div className="dropdown-head-title">
                              <ShieldCheck size={13} />
                              <span>Permission Guard</span>
                            </div>
                          </div>

                          <div className="custom-dropdown-list">
                            <button
                              type="button"
                              className={"custom-dropdown-item " + (permissionMode === "safe" ? "active" : "")}
                              onClick={() => { setMode("safe"); setPermissionDropdownOpen(false); }}
                            >
                              <div className="item-left">
                                <span className="item-dot ready" />
                                <div className="item-text">
                                  <span className="item-title">Safe mode</span>
                                  <span className="item-sub">Default sandbox & Git guardrails</span>
                                </div>
                              </div>
                              <div className="item-right">
                                <span className="badge-tag tag-free">Recommended</span>
                                {permissionMode === "safe" && <Check size={13} className="item-check" />}
                              </div>
                            </button>

                            <button
                              type="button"
                              className={"custom-dropdown-item " + (permissionMode === "balanced" ? "active" : "")}
                              onClick={() => { setMode("balanced"); setPermissionDropdownOpen(false); }}
                            >
                              <div className="item-left">
                                <span className="item-dot standby" />
                                <div className="item-text">
                                  <span className="item-title">Balanced</span>
                                  <span className="item-sub">Interactive prompt before shell writes</span>
                                </div>
                              </div>
                              <div className="item-right">
                                <span className="badge-tag">Standard</span>
                                {permissionMode === "balanced" && <Check size={13} className="item-check" />}
                              </div>
                            </button>

                            <button
                              type="button"
                              className={"custom-dropdown-item " + (permissionMode === "god" ? "active" : "")}
                              onClick={() => { setMode("god"); setPermissionDropdownOpen(false); }}
                            >
                              <div className="item-left">
                                <span className="item-dot" style={{ background: "var(--red)" }} />
                                <div className="item-text">
                                  <span className="item-title">God mode</span>
                                  <span className="item-sub">Autonomous execution without pauses</span>
                                </div>
                              </div>
                              <div className="item-right">
                                <span className="badge-tag tag-danger">High Risk</span>
                                {permissionMode === "god" && <Check size={13} className="item-check" />}
                              </div>
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                    <button className="tool-pill active" title="Run 4-Phase Guided Workflow" onClick={() => void runGuidedWorkflow()}><Sparkles size={14}/> Guided Flow</button>
                    <button className={"tool-pill " + (githubResearchEnabled ? "research-enabled" : "")} title="Automatically research public GitHub projects" onClick={() => { const next = !githubResearchEnabled; setGithubResearchEnabled(next); localStorage.setItem("cc_github_research", String(next)); }}><Github size={14}/> Research {githubResearchEnabled ? "ON" : "OFF"}</button>
                    <button className="tool-pill" onClick={() => void makePlan()} disabled={!prompt.trim() || planning}><Workflow size={14}/>{planning ? "Planning…" : "Plan task"}</button>
                  </div>
                  <div className="composer-submit">
                    {busy ? <button className="stop-button" onClick={() => void cancel()}><Square size={13}/> Stop</button> : <button className="send-button" disabled={!prompt.trim() || !health} onClick={() => void runTask()}>{plan ? "Run plan" : "Run task"} <Send size={15}/></button>}
                  </div>
                </div>
                <div className="composer-footnote">
                  <span><Command size={11}/> Enter to write · Ctrl/⌘ + Enter to run</span>
                  <span><ShieldCheck size={12}/> Safe sandbox · Git guard protected</span>
                </div>
              </div>
            </div>
          </section>

          {contextOpen && (
            <aside className="context-panel">
              <div className="context-head"><div><span>WORKSPACE</span><b>Task context</b></div><button className="icon-button" onClick={() => setContextOpen(false)}><X size={15}/></button></div>
              <section className="context-section">
                <div className="context-section-title"><FolderGit2 size={15}/> Current project</div>
                <div className="context-project"><div className="context-project-icon"><Code2 size={17}/></div><div><b>{workspace.split(/[\\/]/).filter(Boolean).pop() || "No project selected"}</b><span>{repo?.repository_url || "Local folder"}</span></div></div>
                <label className="field-label">Workspace path<input value={workspace} onChange={e => setWorkspace(e.target.value)} placeholder="C:\Projects\my-app"/></label>
                <button className="outline-button full-button" onClick={() => setRepoOpen(v => !v)}><Github size={14}/> Import GitHub repository</button>
              </section>
              <section className="context-section">
                <div className="context-section-title"><Bot size={15}/> Agent status <button onClick={() => void connect()} title="Refresh agents"><RefreshCw size={12}/></button></div>
                {agents.slice(0, 8).map(a => (
                  <button className="context-agent" key={a.id} onClick={() => setAgent(a.id)}>
                    <span className={"agent-dot " + (a.state === "ready" ? "ready" : "")}/>
                    <span>{a.name}</span>
                    <small>{label(a.state)}</small>
                  </button>
                ))}
                {!agents.length && <p className="context-muted">Connect the backend to discover agents.</p>}
              </section>
              <section className="context-section">
                <div className="context-section-title"><Workflow size={15}/> Progress</div>
                {plan ? (
                  <>
                    <div className="progress-label"><span>{activeStep >= 0 ? "Executing plan" : "Plan ready"}</span><b>{activeStep >= 0 ? Math.min(activeStep + 1, plan.subtasks.length) : 0}/{plan.subtasks.length}</b></div>
                    <div className="progress-track"><span style={{ width: ((activeStep + 1) / plan.subtasks.length * 100) + "%" }}/></div>
                    {plan.subtasks.map((s, i) => (
                      <div className="mini-step" key={s.id}>
                        <span className={"mini-step-dot " + (activeStep > i ? "done" : activeStep === i ? "active" : "")}>{activeStep > i ? <Check size={10}/> : i + 1}</span>
                        <span>{s.title}</span>
                      </div>
                    ))}
                  </>
                ) : <p className="context-muted">Plan a larger task to see the steps, dependencies, and execution progress here.</p>}
              </section>
              <div className="context-bottom"><span className={"connection-dot " + (health ? "ready" : "")}/><span>{health ? "Connected to local backend" : "Backend connection required"}</span></div>
            </aside>
          )}
        </div>
      </main>

      {/* 4-Phase Guided Workflow Modal */}
      {guidedModalOpen && (
        <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setGuidedModalOpen(false); }}>
          <section className="guided-modal" role="dialog" aria-modal="true">
            <div className="guided-head">
              <div>
                <span>GUIDED MULTI-PHASE ENGINE</span>
                <h2>4-Phase Engineering Workflow</h2>
                <p>Research GitHub candidates → Inspect isolated reference → Plan with citations → Build & verify.</p>
              </div>
              <button className="icon-button" onClick={() => setGuidedModalOpen(false)}><X size={17}/></button>
            </div>

            <div className="workflow-stepper">
              <div className={"step-pill " + (guidedPipeline?.phase1_research ? "active" : "")}>
                <div className="step-pill-number">1</div>
                <div className="step-pill-text"><b>AI Research Phase</b><small>Search & rank repositories</small></div>
              </div>
              <div className={"step-pill " + (guidedPipeline?.phase2_reference ? "active" : "")}>
                <div className="step-pill-number">2</div>
                <div className="step-pill-text"><b>Reference Projects</b><small>Isolated clone & analysis</small></div>
              </div>
              <div className={"step-pill " + (guidedPipeline?.phase3_plan ? "active" : "")}>
                <div className="step-pill-number">3</div>
                <div className="step-pill-text"><b>Implementation Plan</b><small>Decompose with citations</small></div>
              </div>
              <div className={"step-pill " + (guidedPipeline?.phase4_verification ? "active" : "")}>
                <div className="step-pill-number">4</div>
                <div className="step-pill-text"><b>Build & Verify</b><small>Attribution & check report</small></div>
              </div>
            </div>

            <div className="guided-body">
              {guidedBusy && (
                <div className="thinking-row" style={{ justifyContent: "center", padding: "40px 0" }}>
                  <LoaderCircle className="spin" size={24}/>
                  <span>Running automated multi-phase pipeline for: "{prompt || SAMPLE_GUIDED_PROMPT}"…</span>
                </div>
              )}

              {guidedPipeline && !guidedBusy && (
                <>
                  {/* Phase 1 */}
                  <div className="phase-card">
                    <div className="phase-title"><Github size={16}/> Phase 1: AI GitHub Research ({guidedPipeline.phase1_research.repositories.length} candidates)</div>
                    <p style={{ margin: 0, fontSize: "11px", color: "var(--muted)" }}>
                      Query: <code>{guidedPipeline.phase1_research.query || "Auto-generated"}</code>
                    </p>
                    <div className="repo-grid">
                      {guidedPipeline.phase1_research.repositories.map(r => (
                        <div className="candidate-card" key={r.full_name}>
                          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                            <b>{r.full_name}</b>
                            <a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--violet)" }}><ExternalLink size={12}/></a>
                          </div>
                          <span>{r.description || "No description provided"}</span>
                          <div className="candidate-badges">
                            <span className="c-badge">{r.stars.toLocaleString()} ★</span>
                            <span className="c-badge" style={{ color: "var(--green)" }}>{r.license || "Open"}</span>
                            <span className="c-badge">Score: {r.score}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Phase 2 */}
                  <div className="phase-card">
                    <div className="phase-title"><Layers size={16}/> Phase 2: Isolated Reference Analysis</div>
                    {guidedPipeline.phase2_reference ? (
                      <div>
                        <p style={{ margin: "0 0 8px", fontSize: "11px", color: "var(--text)" }}>
                          Selected Repository: <b>{guidedPipeline.phase2_reference.repo_name}</b>
                          {guidedPipeline.phase2_reference.local_path && <span style={{ color: "var(--muted)", marginLeft: "8px" }}>(Cloned to: <code>{guidedPipeline.phase2_reference.local_path}</code>)</span>}
                        </p>
                        <div style={{ display: "flex", gap: "6px", flexWrap: "wrap", marginBottom: "8px" }}>
                          {guidedPipeline.phase2_reference.design_patterns.map((pat, idx) => (
                            <span key={idx} className="c-badge" style={{ background: "var(--violet-bg)", color: "var(--violet)" }}>{pat}</span>
                          ))}
                        </div>
                        <div style={{ fontSize: "10px", color: "var(--muted)" }}>
                          Key Structural Files Inspected: {guidedPipeline.phase2_reference.key_files.slice(0, 5).join(", ")}
                        </div>
                      </div>
                    ) : (
                      <p style={{ margin: 0, fontSize: "11px", color: "var(--muted)" }}>
                        No external repository cloned. Proceeding with safe independent architecture.
                      </p>
                    )}
                  </div>

                  {/* Phase 3 */}
                  <div className="phase-card">
                    <div className="phase-title"><Workflow size={16}/> Phase 3: Implementation Plan ({guidedPipeline.phase3_plan.length} steps)</div>
                    <div className="plan-step-list">
                      {guidedPipeline.phase3_plan.map(s => (
                        <div className="plan-step" key={s.step_id}>
                          <div className="step-number">{s.step_id}</div>
                          <div className="step-main">
                            <b>{s.title}</b>
                            <span>{s.description}</span>
                            {s.reference_citation && <small style={{ display: "block", marginTop: "3px", color: "var(--violet)" }}>📌 {s.reference_citation}</small>}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Phase 4 */}
                  <div className="phase-card">
                    <div className="phase-title"><ShieldCheck size={16}/> Phase 4: Build, Verify & Improve Strategy</div>
                    <div className="provenance_report">
                      <div className="provenance-box">
                        <b>Provenance & Reference Attribution</b>
                        <span>Top Reference: {guidedPipeline.phase4_verification.provenance_report.top_reference}</span>
                        <span>License: {guidedPipeline.phase4_verification.provenance_report.license}</span>
                        <span>Design Patterns Applied: {guidedPipeline.phase4_verification.provenance_report.patterns_applied.join(", ")}</span>
                        <span>Safety Guarantees: Sandbox path isolation enforced · Git guard dirty state checkpointed.</span>
                      </div>
                    </div>
                  </div>
                </>
              )}
            </div>

            <div className="modal-actions bottom-actions">
              <button className="outline-button" onClick={() => setGuidedModalOpen(false)}>Close</button>
              <button className="primary-button" disabled={!guidedPipeline || !guidedPipeline.phase3_plan.length} onClick={applyGuidedPlanToWorkspace}>
                Load Plan into Workspace & Execute <ArrowUpRight size={14}/>
              </button>
            </div>
          </section>
        </div>
      )}

      {/* CloakBrowser & Web Models Control Center Modal */}
      {browserModalOpen && (
        <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setBrowserModalOpen(false); }}>
          <section className="browser-modal" role="dialog" aria-modal="true">
            <div className="browser-modal-head">
              <div>
                <span>CLOAKBROWSER & WEB APIS</span>
                <h2>Web Models & CloakBrowser Control Center</h2>
                <p>Turn authenticated web AI chats (ChatGPT, Claude, DeepSeek, LMSYS Arena, Manus) into standard OpenAI-compatible streaming APIs. All credentials and logins stay saved permanently in your local profile.</p>
              </div>
              <button className="icon-button" onClick={() => setBrowserModalOpen(false)}><X size={17}/></button>
            </div>

            <div className="browser-modal-body">
              {/* CloakBrowser Status & 1-Click Launch Banner */}
              <div className="cloak-status-banner">
                <div className="cloak-status-info">
                  <div className={"cloak-status-dot " + (browserStatus?.connected ? "active" : "")}/>
                  <div>
                    <div className="cloak-status-title">
                      {browserStatus?.connected
                        ? `CloakBrowser Connected (${browserStatus.tab_count} open tabs · Port 9222)`
                        : "CloakBrowser Offline (Not Connected)"}
                    </div>
                    <div className="cloak-status-desc">
                      Persistent User Data: <code>D:\work\code-conductor\.cloak_profile</code> (Sessions & logins stay saved)
                    </div>
                  </div>
                </div>
                <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                  <button
                    className="launch-cloak-btn"
                    disabled={cloakLaunching}
                    onClick={() => void launchCloakBrowser()}
                  >
                    {cloakLaunching ? <LoaderCircle className="spin" size={14}/> : <Sparkles size={14}/>}
                    <span>{browserStatus?.connected ? "Re-Open CloakBrowser" : "🚀 Launch CloakBrowser"}</span>
                  </button>
                  <button
                    className="icon-button"
                    onClick={() => { void fetchBrowserStatus(); void fetchWebModels(); void fetchBrowserTabs(); }}
                    title="Refresh status and tabs"
                  >
                    <RefreshCw size={14}/>
                  </button>
                </div>
              </div>

              {/* View Switcher Tabs */}
              <div style={{ display: "flex", gap: "8px", borderBottom: "1px solid var(--line)", paddingBottom: "8px" }}>
                <button
                  className={"subtle-button " + (browserTabActive === "models" ? "active" : "")}
                  style={{
                    fontWeight: 700,
                    fontSize: "12px",
                    color: browserTabActive === "models" ? "var(--violet)" : "var(--muted)",
                    borderBottom: browserTabActive === "models" ? "2px solid var(--violet)" : "none",
                    borderRadius: 0,
                    padding: "4px 10px",
                  }}
                  onClick={() => { setBrowserTabActive("models"); void fetchWebModels(); }}
                >
                  <Bot size={14}/> Web Models as APIs ({webModels.length})
                </button>
                <button
                  className={"subtle-button " + (browserTabActive === "tabs" ? "active" : "")}
                  style={{
                    fontWeight: 700,
                    fontSize: "12px",
                    color: browserTabActive === "tabs" ? "var(--violet)" : "var(--muted)",
                    borderBottom: browserTabActive === "tabs" ? "2px solid var(--violet)" : "none",
                    borderRadius: 0,
                    padding: "4px 10px",
                  }}
                  onClick={() => { setBrowserTabActive("tabs"); void fetchBrowserTabs(); }}
                >
                  <Globe size={14}/> Live Browser Tabs & Screen ({browserTabs.length})
                </button>
              </div>

              {browserTabActive === "models" ? (
                <>
                  {/* Web Models Grid */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
                      <span style={{ fontSize: "11px", fontWeight: 700, color: "var(--text-heading)" }}>
                        CONFIGURED WEB AI PLATFORMS (READY TO USE AS APIS)
                      </span>
                      <button
                        className="outline-button"
                        style={{ fontSize: "11px", padding: "4px 10px" }}
                        onClick={() => setCustomModelFormOpen(v => !v)}
                      >
                        <Plus size={13}/> {customModelFormOpen ? "Close Form" : "Add Custom Web Model"}
                      </button>
                    </div>

                    <div className="web-models-grid">
                      {webModels.map(m => (
                        <div className="web-model-card" key={m.id}>
                          <div className="web-model-card-head">
                            <div>
                              <div className="web-model-title">{m.name}</div>
                              <div className="web-model-url">{m.url}</div>
                            </div>
                            <span className={"web-model-state-badge " + (m.state === "ready" ? "ready" : "")}>
                              {m.state === "ready" ? "Ready" : "On-Demand"}
                            </span>
                          </div>
                          <p className="web-model-desc">{m.description}</p>
                          <div className="web-model-actions">
                            <button
                              className="web-login-btn"
                              title="Open tab in CloakBrowser and sign into your account"
                              onClick={() => void openWebModelInCloak(m)}
                            >
                              <ExternalLink size={12}/> Open & Log In
                            </button>
                            <button
                              className="web-test-btn"
                              title="Test this Web Model API with streaming response"
                              onClick={() => { setTestModelId(m.id); void testWebModelStream(); }}
                            >
                              ⚡ Test API
                            </button>
                            {!m.is_preset && (
                              <button
                                className="icon-button"
                                title="Remove model"
                                onClick={() => void deleteCustomWebModel(m.id)}
                              >
                                <Trash2 size={13}/>
                              </button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Add Custom Web Model Form */}
                  {customModelFormOpen && (
                    <div className="add-web-model-box">
                      <div className="add-web-model-head">
                        <Plus size={15}/> Add Any Custom Web AI Model (e.g. Perplexity, Mistral, Grok)
                      </div>
                      <div className="add-web-model-grid">
                        <label className="settings-field">
                          Display Name
                          <input
                            value={webModelDraft.name}
                            onChange={e => setWebModelDraft({ ...webModelDraft, name: e.target.value })}
                            placeholder="e.g. Perplexity Web"
                          />
                        </label>
                        <label className="settings-field">
                          Web Chat URL
                          <input
                            value={webModelDraft.url}
                            onChange={e => setWebModelDraft({ ...webModelDraft, url: e.target.value })}
                            placeholder="https://www.perplexity.ai"
                          />
                        </label>
                      </div>
                      <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
                        <button className="subtle-button" onClick={() => setCustomModelFormOpen(false)}>Cancel</button>
                        <button
                          className="primary-button"
                          disabled={browserBusy || !webModelDraft.name.trim() || !webModelDraft.url.trim()}
                          onClick={() => void saveCustomWebModel()}
                        >
                          Save & Open in CloakBrowser for Login
                        </button>
                      </div>
                    </div>
                  )}

                  {/* Live Web Model API Tester Playground */}
                  <div className="test-playground-box">
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span style={{ fontSize: "11px", fontWeight: 700, color: "var(--text-heading)", display: "flex", alignItems: "center", gap: "6px" }}>
                        <Terminal size={14} style={{ color: "var(--violet)" }}/> LIVE WEB MODEL API TESTER & STREAM PREVIEW
                      </span>
                      <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                        <span style={{ fontSize: "11px", color: "var(--muted)" }}>Target Model:</span>
                        <select
                          value={testModelId}
                          onChange={e => setTestModelId(e.target.value)}
                          style={{
                            fontSize: "11px",
                            padding: "3px 8px",
                            borderRadius: "6px",
                            background: "var(--panel)",
                            border: "1px solid var(--line)",
                            color: "var(--text)",
                          }}
                        >
                          {webModels.map(m => (
                            <option key={m.id} value={m.id}>{m.name} ({m.id})</option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: "8px" }}>
                      <input
                        value={testModelPrompt}
                        onChange={e => setTestModelPrompt(e.target.value)}
                        placeholder="Type a test prompt to send through CloakBrowser..."
                        style={{ flex: 1 }}
                        onKeyDown={e => { if (e.key === "Enter") void testWebModelStream(); }}
                      />
                      <button
                        className="primary-button"
                        disabled={testModelBusy || !testModelPrompt.trim()}
                        onClick={() => void testWebModelStream()}
                      >
                        {testModelBusy ? <LoaderCircle className="spin" size={13}/> : <Send size={13}/>}
                        <span>{testModelBusy ? "Streaming from Browser…" : "Send Test"}</span>
                      </button>
                    </div>
                    {testModelResult && (
                      <div className="test-playground-result">
                        {testModelResult}
                      </div>
                    )}
                  </div>

                  {/* OpenAI Standard API Access Info */}
                  <div className="api-info-panel">
                    <div className="api-info-head">
                      <div className="api-info-title">
                        <KeyRound size={14}/> STANDARD OPENAI-COMPATIBLE API ENDPOINT
                      </div>
                      <span style={{ fontSize: "10px", color: "var(--muted)" }}>Works with curl, Python openai SDK, VSCode extensions, & Cline</span>
                    </div>
                    <div className="api-code-snippet">
                      curl http://127.0.0.1:8765/v1/chat/completions \<br/>
                      &nbsp;&nbsp;-H "Authorization: Bearer {token}" \<br/>
                      &nbsp;&nbsp;-H "Content-Type: application/json" \<br/>
                      &nbsp;&nbsp;-d '&#123;"model": "{testModelId}", "messages": [&#123;"role": "user", "content": "Hello!"&#125;], "stream": true&#125;'
                    </div>
                  </div>
                </>
              ) : (
                <>
                  {/* Browser Target Config Bar */}
                  <div className="browser-toolbar">
                    <Globe size={16} style={{ color: "var(--violet)" }}/>
                    <span style={{ fontSize: "10px", fontWeight: 700 }}>CDP Endpoint:</span>
                    <input
                      value={browserEndpointDraft}
                      onChange={e => setBrowserEndpointDraft(e.target.value)}
                      placeholder="http://127.0.0.1:9222"
                    />
                    <button className="outline-button" disabled={browserBusy} onClick={() => void updateBrowserEndpoint()}>
                      {browserBusy ? <LoaderCircle className="spin" size={13}/> : "Connect / Change"}
                    </button>
                    <button className="outline-button" onClick={() => void fetchBrowserTabs()} title="Refresh tabs"><RefreshCw size={13}/></button>
                  </div>

                  {/* Open Tabs Selector */}
                  <div className="browser-tabs-row">
                    {browserTabs.map(tab => (
                      <div
                        key={tab.id}
                        className={"b-tab " + (selectedTabId === tab.id ? "active" : "")}
                        onClick={() => void activateTab(tab.id)}
                      >
                        <Eye size={12}/>
                        <span>{tab.title ? (tab.title.length > 25 ? tab.title.slice(0, 25) + "…" : tab.title) : "Tab " + tab.id}</span>
                      </div>
                    ))}
                    {!browserTabs.length && (
                      <span style={{ fontSize: "10px", color: "var(--muted)", padding: "4px 8px" }}>
                        {browserStatus?.connected ? "No open web pages found" : "Browser disconnected. Click 'Launch CloakBrowser' above."}
                      </span>
                    )}
                  </div>

                  {/* Navigation & Action Bar */}
                  <div className="browser-toolbar" style={{ marginTop: 0 }}>
                    <input
                      value={browserNavUrl}
                      onChange={e => setBrowserNavUrl(e.target.value)}
                      placeholder="Enter URL to navigate (e.g. https://google.com)"
                      onKeyDown={e => { if (e.key === "Enter") void navigateActiveTab(); }}
                    />
                    <button className="primary-button" disabled={browserBusy || !browserNavUrl} onClick={() => void navigateActiveTab()}>
                      Navigate
                    </button>
                    <button className="outline-button" disabled={browserBusy} onClick={() => void captureScreenshot()}>
                      <Camera size={13}/> Capture Screen
                    </button>
                  </div>

                  <div className="browser-content">
                    {browserScreenshot ? (
                      <div style={{ textAlign: "center" }}>
                        <img src={browserScreenshot} alt="Browser screenshot" className="screenshot-preview"/>
                      </div>
                    ) : (
                      <div className="history-placeholder">
                        <Globe size={32} style={{ opacity: 0.4 }}/>
                        <span>Active Browser: {browserStatus?.browser || "Not connected"}</span>
                        <small>Click "Capture Screen" to view what CloakBrowser is currently rendering.</small>
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>

            <div className="modal-actions bottom-actions">
              <button className="outline-button" onClick={() => setBrowserModalOpen(false)}>Done</button>
            </div>
          </section>
        </div>
      )}

      {/* Antigravity History Modal */}
      {historyOpen && (
        <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setHistoryOpen(false); }}>
          <section className="history-modal" role="dialog" aria-modal="true">
            <div className="history-head">
              <div>
                <span>LOCAL CHAT ARCHIVE</span>
                <h2>Antigravity Chat Sessions</h2>
                <p>Browse past conversations, transcripts, and artifacts saved in Antigravity IDE.</p>
              </div>
              <button className="icon-button" onClick={() => setHistoryOpen(false)}><X size={17}/></button>
            </div>

            <div className="history-body">
              <aside className="history-sidebar">
                <div className="history-search">
                  <input value={historySearch} onChange={e => setHistorySearch(e.target.value)} placeholder="Search Antigravity chats…"/>
                </div>
                <div className="history-list">
                  {historyList.filter(c => !historySearch || c.title.toLowerCase().includes(historySearch.toLowerCase())).map(c => (
                    <button key={c.id} className={"history-item " + (selectedConv?.id === c.id ? "active" : "")} onClick={() => void openConversation(c.id)}>
                      <b>{c.title || "Untitled Conversation"}</b>
                      <div className="history-meta">
                        <span>{c.updated_time}</span>
                        <em>{c.message_count} msgs</em>
                      </div>
                    </button>
                  ))}
                  {historyList.length === 0 && !historyBusy && (
                    <div className="history-empty">No Antigravity conversations found in ~/.gemini/antigravity-ide/brain.</div>
                  )}
                </div>
              </aside>

              <section className="history-content">
                {selectedConv ? (
                  <>
                    <div className="history-convo-head">
                      <div>
                        <h3>{selectedConv.title}</h3>
                        <span>ID: {selectedConv.id} · {selectedConv.messages.length} turns · Updated {selectedConv.updated_time}</span>
                      </div>
                      <button className="primary-button" onClick={() => loadIntoWorkspace(selectedConv)}>
                        Load into Workspace <ArrowUpRight size={13}/>
                      </button>
                    </div>

                    {selectedConv.artifacts.length > 0 && (
                      <div className="history-artifacts">
                        <small>Generated Artifacts ({selectedConv.artifacts.length}):</small>
                        <div className="artifact-chips">
                          {selectedConv.artifacts.map((art, idx) => (
                            <span className="artifact-chip" key={idx}>{art.name} ({Math.round(art.size / 1024)} KB)</span>
                          ))}
                        </div>
                      </div>
                    )}

                    <div className="history-messages">
                      {selectedConv.messages.map((m, idx) => (
                        <div className={"history-msg " + m.role} key={idx}>
                          <span className="history-msg-role">{m.role.toUpperCase()}</span>
                          <div className="history-msg-body"><pre>{m.content}</pre></div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="history-placeholder">
                    {historyBusy ? <LoaderCircle className="spin" size={24}/> : "Select a conversation from the sidebar to inspect its history."}
                  </div>
                )}
              </section>
            </div>
          </section>
        </div>
      )}

      {/* Smart Add API Provider Modal */}
      {smartAddOpen && (
        <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setSmartAddOpen(false); }}>
          <section className="smart-add-modal" role="dialog" aria-modal="true" aria-labelledby="smart-add-title">
            <div className="smart-add-head">
              <div className="smart-add-title-wrap">
                <div className="smart-add-icon">
                  <Sparkles size={20} />
                </div>
                <div>
                  <h3 id="smart-add-title">Add AI Provider</h3>
                  <p>Just enter where you got your key from and if it's free or paid.<br />The AI configures the endpoint, models, and connection automatically!</p>
                </div>
              </div>
              <button className="icon-button" onClick={() => setSmartAddOpen(false)}><X size={17}/></button>
            </div>

            <div className="smart-add-body">
              {/* Question 1: Where did you get it from? */}
              <label className="smart-field-label">
                <div>Where did you get it from? (Site name or URL)<span className="req">*</span></div>
                <input
                  type="text"
                  className="smart-input"
                  placeholder="e.g. Groq, OpenRouter, DeepSeek, Together, Mistral, APMix, or website URL"
                  value={smartSite}
                  onChange={e => setSmartSite(e.target.value)}
                  autoFocus
                />
                <div className="quick-site-pills">
                  {["Groq", "OpenRouter", "DeepSeek", "Together AI", "Mistral", "APMix", "Cerebras", "Ollama"].map(chip => (
                    <button
                      key={chip}
                      type="button"
                      className={"quick-site-pill " + (smartSite.toLowerCase() === chip.toLowerCase() ? "selected" : "")}
                      onClick={() => setSmartSite(chip)}
                    >
                      {chip}
                    </button>
                  ))}
                </div>
              </label>

              {/* Question 2: Is it Free or Paid? */}
              <div className="smart-field-label">
                <div>Is it a Free or Paid Tier?<span className="req">*</span></div>
                <div className="tier-toggle-grid">
                  <div
                    className={"tier-card " + (smartIsFree ? "selected" : "")}
                    onClick={() => setSmartIsFree(true)}
                  >
                    <div className="tier-card-head">
                      <span className="tier-card-title">🟢 Free Tier</span>
                      <span className="badge-tag tag-free">Zero Cost</span>
                    </div>
                    <p>AI will select fast free models (e.g. Qwen 27B, Llama 3.3 Free, Claude Sonnet Free).</p>
                  </div>

                  <div
                    className={"tier-card " + (!smartIsFree ? "selected" : "")}
                    onClick={() => setSmartIsFree(false)}
                  >
                    <div className="tier-card-head">
                      <span className="tier-card-title">🟣 Paid / Pro Tier</span>
                      <span className="badge-tag tag-paid">Flagship</span>
                    </div>
                    <p>AI will select maximum intelligence flagship models (e.g. Llama 70B, Claude 3.5 Sonnet, Mistral Large).</p>
                  </div>
                </div>
              </div>

              {/* Question 3: API Key */}
              <label className="smart-field-label">
                <div>API Key<span className="req">*</span></div>
                <div className="smart-key-wrap">
                  <input
                    type={smartShowKey ? "text" : "password"}
                    className="smart-input"
                    placeholder="Paste your API key here (e.g. gsk_..., sk-or-..., or bearer token)"
                    value={smartApiKey}
                    onChange={e => setSmartApiKey(e.target.value)}
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    className="smart-key-toggle"
                    onClick={() => setSmartShowKey(v => !v)}
                    title={smartShowKey ? "Hide key" : "Show key"}
                  >
                    <Eye size={15} />
                  </button>
                </div>
              </label>

              <div className="smart-info-note">
                <ShieldCheck size={15} />
                <span>Encrypted & stored securely in your OS credential store (Keyring). Never transmitted to any 3rd-party.</span>
              </div>
            </div>

            <div className="smart-actions">
              <button
                type="button"
                className="outline-button"
                onClick={() => { setSmartAddOpen(false); setSettings(true); }}
                style={{ fontSize: "11px" }}
              >
                Advanced Setup
              </button>
              <div style={{ display: "flex", gap: "8px" }}>
                <button
                  type="button"
                  className="subtle-button"
                  onClick={() => setSmartAddOpen(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="primary-button"
                  disabled={smartBusy || !smartSite.trim() || !smartApiKey.trim()}
                  onClick={() => void saveSmartProvider()}
                >
                  {smartBusy ? <LoaderCircle className="spin" size={14}/> : <Sparkles size={14}/>}
                  <span>{smartBusy ? "Configuring & Connecting…" : "✨ Let AI Configure & Connect"}</span>
                </button>
              </div>
            </div>
          </section>
        </div>
      )}

      {/* Settings Modal */}
      {settings && (
        <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) setSettings(false); }}>
          <section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">
            <div className="settings-modal-head">
              <div>
                <span>CONFIGURATION</span>
                <h2 id="settings-title">Workspace settings</h2>
                <p>Connect your local agents and tune how they work.</p>
              </div>
              <button className="icon-button" onClick={() => setSettings(false)}><X size={17}/></button>
            </div>
            <label className="settings-field">Backend URL<input value={base} onChange={e => setBase(e.target.value)} placeholder="http://127.0.0.1:8765"/></label>
            <label className="settings-field">Local bearer token<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} placeholder="Paste the token from ~/.aihub_token"/></label>
            <div className="settings-callout"><ShieldCheck size={15}/><span>Tokens are stored only in your local session. Provider keys remain in the OS credential store.</span></div>

            <div className="settings-section">
              <div className="settings-section-head"><Bell size={15}/> Notifications</div>
              <label className="check-row"><input type="checkbox" checked={notifications.enabled} onChange={e => saveNotifications({ ...notifications, enabled: e.target.checked })}/> Enable notifications and sounds</label>
              <label className="check-row"><input type="checkbox" checked={notifications.completion} onChange={e => saveNotifications({ ...notifications, completion: e.target.checked })}/> When a task completes</label>
              <label className="check-row"><input type="checkbox" checked={notifications.action} onChange={e => saveNotifications({ ...notifications, action: e.target.checked })}/> When a task needs attention</label>
              <label className="settings-field">Sound<select value={notifications.sound} onChange={e => saveNotifications({ ...notifications, sound: e.target.value })}><option value="chime">Soft chime</option><option value="bell">Bell</option><option value="alert">Alert</option><option value="none">Silent</option></select></label>
              <div className="modal-actions">
                <button className="outline-button" onClick={() => { if ("Notification" in window) void Notification.requestPermission(); }}>Enable browser permission</button>
                <button className="outline-button" onClick={() => playSound(notifications.sound)}>Test sound</button>
              </div>
            </div>

            <div className="settings-section">
              <div className="settings-section-head"><ShieldAlert size={15}/> Permissions</div>
              <p className="settings-help">Safe is default. God mode requires confirmation and can run commands or modify files broadly.</p>
              <label className="settings-field">Default permission mode<select value={permissionMode} onChange={e => setMode(e.target.value as PermissionMode)}><option value="safe">Safe</option><option value="balanced">Balanced</option><option value="god">God mode · high risk</option></select></label>
            </div>

            <div className="settings-section">
              <div className="settings-section-head"><KeyRound size={15}/> Custom OpenAI-compatible provider</div>
              <div className="provider-form">
                <label className="settings-field">Provider ID<input value={providerDraft.id} onChange={e => setProviderDraft({ ...providerDraft, id: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "") })} placeholder="my-provider"/></label>
                <label className="settings-field">Display name<input value={providerDraft.name} onChange={e => setProviderDraft({ ...providerDraft, name: e.target.value })} placeholder="My AI provider"/></label>
                <label className="settings-field">Base URL<input value={providerDraft.base_url} onChange={e => setProviderDraft({ ...providerDraft, base_url: e.target.value })} placeholder="https://api.example.com/v1"/></label>
                <label className="settings-field">Model ID<input value={providerDraft.model} onChange={e => setProviderDraft({ ...providerDraft, model: e.target.value })} placeholder="provider/model"/></label>
                <label className="settings-field span-two">API key(s), one per line<textarea rows={3} autoComplete="off" value={providerKey} onChange={e => setProviderKey(e.target.value)} placeholder="API keys are sent to the local backend and stored in the OS credential store."/></label>
              </div>
              <div className="modal-actions"><button className="primary-button" disabled={providerBusy || !providerDraft.id || !providerDraft.name || !providerDraft.base_url || !providerDraft.model || !providerKey} onClick={() => void saveProvider()}>{providerBusy ? "Saving…" : "Save provider"}</button></div>
              {providers.map(p => (
                <div className="provider-item" key={p.id}>
                  <div><b>{p.name}</b><span>{p.model} · {p.key_count || (p.key_configured ? 1 : 0)} key(s)</span></div>
                  <button className="icon-button" title="Delete provider" onClick={() => void removeProvider(p)}><Trash2 size={14}/></button>
                </div>
              ))}
            </div>

            <div className="modal-actions bottom-actions">
              <button className="outline-button" onClick={() => { setToken(""); sessionStorage.removeItem("cc_token"); setHealth(null); setAgents([]); }}>Clear token</button>
              <button className="primary-button" onClick={() => { setSettings(false); sessionStorage.setItem("cc_api", base); sessionStorage.setItem("cc_token", token.trim()); void connect(); }}>Save & connect <ArrowUpRight size={14}/></button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
