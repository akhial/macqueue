import { useEffect, useMemo, useRef, useState } from "react";
import { get } from "./api";
import {
  active,
  appendLogs,
  attention,
  duration,
  elapsed,
  emptyLog,
  filterJobs,
  statusLabel,
} from "./model";
import type { Detail, Filter, Job, LogChunk, Status } from "./model";
import "./App.css";

const clock = (seconds: number | null) =>
  seconds === null
    ? "—"
    : new Date(seconds * 1000).toLocaleString("en-GB", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
const shortId = (id: string) => id.slice(0, 8);
const tabs: { value: Filter; label: string }[] = [
  { value: "all", label: "ALL" },
  { value: "running", label: "RUNNING" },
  { value: "queued", label: "QUEUED" },
  { value: "succeeded", label: "SUCCEEDED" },
  { value: "failed", label: "FAILED" },
  { value: "lost", label: "LOST" },
  { value: "cancelled", label: "CANCELLED" },
  { value: "expired", label: "EXPIRED" },
];

function Badge({ status }: { status: Status }) {
  return (
    <span className={`badge status-${status}`}>
      <span aria-hidden="true">
        {active(status) ? "●" : status === "succeeded" ? "✓" : attention(status) ? "×" : "□"}
      </span>
      {statusLabel(status)}
    </span>
  );
}
function Copy({ value, label = "COPY" }: { value: string; label?: string }) {
  const [state, setState] = useState(label);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return (
    <button
      className="text-button"
      aria-label={label === "COPY" ? "Copy to clipboard" : `Copy ${label}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setState("COPIED");
        } catch {
          setState("COPY FAILED");
        }
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => setState(label), 1600);
      }}
    >
      {state}
    </button>
  );
}

function JobDetail({
  id,
  refresh,
  auto,
  now,
  onClose,
}: {
  id: string;
  refresh: number;
  auto: boolean;
  now: number;
  onClose: () => void;
}) {
  const [job, setJob] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [logError, setLogError] = useState("");
  const [tab, setTab] = useState<"overview" | "logs" | "spec">("overview");
  const [logs, setLogs] = useState(emptyLog);
  const logState = useRef(emptyLog());
  const [follow, setFollow] = useState(true);
  const [logQuery, setLogQuery] = useState("");
  const logView = useRef<HTMLPreElement>(null);
  const specText = useMemo(() => JSON.stringify(job?.spec, null, 2) ?? "", [job?.spec]);
  const resultText = useMemo(() => JSON.stringify(job?.result, null, 2) ?? "", [job?.result]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await get<Detail>(`/jobs/${id}`, controller.signal);
        if (controller.signal.aborted) return;
        setJob(value);
        setError("");
        try {
          for (let page = 0; page < 3; page++) {
            const chunks = await get<LogChunk[]>(
              `/jobs/${id}/logs?after=${logState.current.cursor}`,
              controller.signal,
            );
            if (controller.signal.aborted) return;
            logState.current = appendLogs(logState.current, chunks);
            setLogs(logState.current);
            if (chunks.length < 100) break;
          }
          setLogError("");
        } catch (reason) {
          if (!controller.signal.aborted) setLogError((reason as Error).message);
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError((reason as Error).message);
      } finally {
        if (!controller.signal.aborted && auto) timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, refresh, auto]);
  useEffect(() => {
    if (follow && logView.current) logView.current.scrollTop = logView.current.scrollHeight;
  }, [logs.text, follow, tab]);
  const errorMessage =
    job?.result && (job.result.error ?? job.result.stop_reason ?? job.result.artifact_error);
  const preview = logQuery
    ? logs.text
        .split("\n")
        .filter((line) => line.toLowerCase().includes(logQuery.toLowerCase()))
        .join("\n")
    : logs.text;
  return (
    <aside className="detail" aria-label="Job details">
      <div className="detail-top">
        <span>JOB / {shortId(id)}</span>
        <button onClick={onClose} aria-label="Close job details">
          ×
        </button>
      </div>
      {error && (
        <div className="error-strip" role="status">
          {error}
          {job ? " · STALE" : ""}
        </div>
      )}
      {!job ? (
        <div className="empty">{error ? "DETAILS UNAVAILABLE" : "LOADING JOB…"}</div>
      ) : (
        <>
          <div className="detail-heading">
            <Badge status={job.status} />
            <h2>{job.spec.label || "Untitled job"}</h2>
            <div className="id-line">
              <span title={id}>{id}</span>
              <Copy value={id} label="ID" />
            </div>
          </div>
          <dl className="metadata">
            <div>
              <dt>PROJECT</dt>
              <dd>{job.spec.project}</dd>
            </div>
            <div>
              <dt>WORKER</dt>
              <dd>{job.worker ?? "—"}</dd>
            </div>
            <div>
              <dt>ELAPSED</dt>
              <dd>{duration(elapsed(job, now))}</dd>
            </div>
            <div>
              <dt>QUEUE WAIT</dt>
              <dd>{duration((job.started ?? job.finished ?? now) - job.created)}</dd>
            </div>
          </dl>
          <div className="detail-tabs" role="tablist" aria-label="Job views">
            {(["overview", "logs", "spec"] as const).map((name) => (
              <button
                key={name}
                role="tab"
                aria-selected={tab === name}
                aria-controls={`panel-${name}`}
                id={`tab-${name}`}
                onClick={() => setTab(name)}
              >
                {name.toUpperCase()}
                {name === "logs" && active(job.status) ? " ●" : ""}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === "overview" && (
              <div className="overview">
                {Boolean(errorMessage) && (
                  <div className="failure">
                    <span>STOP / ERROR</span>
                    <pre>
                      {typeof errorMessage === "string"
                        ? errorMessage
                        : JSON.stringify(errorMessage, null, 2)}
                    </pre>
                  </div>
                )}
                <section>
                  <h3>SOURCE</h3>
                  {Object.entries(job.spec.sources).map(([variant, sha]) => (
                    <div className="source-line" key={variant}>
                      <span>{variant}</span>
                      <code title={sha}>{sha.slice(0, 12)}</code>
                      <Copy value={sha} />
                    </div>
                  ))}
                </section>
                <section>
                  <h3>
                    STEPS <span>{job.spec.steps.length}</span>
                  </h3>
                  <ol className="steps">
                    {job.spec.steps.map((step, index) => {
                      const state = job.status === "succeeded" ? "done" : logs.steps[step.id];
                      const failed = state === "running" && !active(job.status);
                      return (
                        <li
                          key={step.id}
                          className={state ? `step-${failed ? "stopped" : state}` : ""}
                        >
                          <span className="step-index">
                            {state === "done"
                              ? "✓"
                              : failed
                                ? "×"
                                : state === "running"
                                  ? "●"
                                  : String(index + 1).padStart(2, "0")}
                          </span>
                          <div>
                            <strong>{step.id}</strong>
                            <span>
                              {step.op}
                              {state === "running" ? (failed ? " / stopped" : " / running") : ""}
                            </span>
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                  {logError && <p className="note">STEP LOG UNAVAILABLE</p>}
                </section>
                <section>
                  <h3>
                    TIMING <span>LOCAL</span>
                  </h3>
                  <dl className="timing">
                    <div>
                      <dt>SUBMITTED</dt>
                      <dd>{clock(job.created)}</dd>
                    </div>
                    <div>
                      <dt>STARTED</dt>
                      <dd>{clock(job.started)}</dd>
                    </div>
                    <div>
                      <dt>FINISHED</dt>
                      <dd>{clock(job.finished)}</dd>
                    </div>
                    <div>
                      <dt>TIMEOUT</dt>
                      <dd>{duration(job.spec.timeout_seconds ?? 14400)}</dd>
                    </div>
                    {job.status === "queued" && (
                      <div>
                        <dt>EXPIRES</dt>
                        <dd>{clock(job.expires)}</dd>
                      </div>
                    )}
                  </dl>
                </section>
                {job.result && (
                  <section>
                    <h3>
                      RESULT <Copy value={resultText} />
                    </h3>
                    <pre className="json result-json">{resultText.slice(0, 131072)}</pre>
                    {resultText.length > 131072 && (
                      <p className="note">PREVIEW TRIMMED · COPY FOR FULL JSON</p>
                    )}
                  </section>
                )}
              </div>
            )}
            {tab === "logs" && (
              <div className="logs-panel">
                <div className="log-tools">
                  <input
                    aria-label="Filter log lines"
                    placeholder="Find in logs…"
                    value={logQuery}
                    onChange={(event) => setLogQuery(event.target.value)}
                  />
                  <button aria-pressed={follow} onClick={() => setFollow(!follow)}>
                    FOLLOW {follow ? "ON" : "OFF"}
                  </button>
                  <Copy value={preview} />
                </div>
                {logError && <div className="error-strip">{logError}</div>}
                <pre className="log-output" ref={logView} tabIndex={0} aria-label="Job log output">
                  {preview || (logQuery ? "NO MATCHES" : "NO LOGS YET")}
                </pre>
                <div className="log-footer">
                  PREVIEW{logs.clipped ? " / OLDER LINES TRIMMED" : ""}
                  <span>FULL LOGS IN ARTIFACT</span>
                </div>
              </div>
            )}
            {tab === "spec" && (
              <div className="spec-panel">
                <div className="section-toolbar">
                  <span>JOB SPEC / JSON</span>
                  <Copy value={specText} />
                </div>
                <pre className="json spec-json" tabIndex={0}>
                  {specText.slice(0, 131072)}
                </pre>
                {specText.length > 131072 && (
                  <p className="note">PREVIEW TRIMMED · COPY FOR FULL JSON</p>
                )}
              </div>
            )}
          </div>
          <div className="artifact">
            {job.has_artifact ? (
              <a className="download" href={`/api/jobs/${id}/artifact`} download={`${id}.tar.gz`}>
                ↓ ARTIFACT .TAR.GZ
              </a>
            ) : (
              <button className="download" disabled>
                NO ARTIFACT
              </button>
            )}
            {job.artifact_sha256 && (
              <div className="artifact-hash">
                <span title={job.artifact_sha256}>SHA256 {job.artifact_sha256.slice(0, 16)}…</span>
                <Copy value={job.artifact_sha256} label="HASH" />
              </div>
            )}
          </div>
        </>
      )}
    </aside>
  );
}

export default function App() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastSync, setLastSync] = useState<number | null>(null);
  const [auto, setAuto] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [activeFirst, setActiveFirst] = useState(true);
  const [selected, setSelected] = useState<string | null>(() =>
    /^#[a-f0-9]{32}$/.test(location.hash) ? location.hash.slice(1) : null,
  );
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await get<Job[]>("/jobs", controller.signal);
        if (controller.signal.aborted) return;
        if (!Array.isArray(value)) throw new Error("INVALID QUEUE RESPONSE");
        setJobs(value);
        setError("");
        setLastSync(Date.now() / 1000);
      } catch (reason) {
        if (!controller.signal.aborted) setError((reason as Error).message);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (auto) timer = setTimeout(poll, 5000);
        }
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [auto, refresh]);
  useEffect(() => {
    function keyboard(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setSelected(null);
        history.replaceState(null, "", location.pathname);
      }
      if (
        event.key === "/" &&
        !["INPUT", "TEXTAREA"].includes((event.target as HTMLElement).tagName)
      ) {
        event.preventDefault();
        input.current?.focus();
      }
    }
    const hash = () =>
      setSelected(/^#[a-f0-9]{32}$/.test(location.hash) ? location.hash.slice(1) : null);
    document.addEventListener("keydown", keyboard);
    window.addEventListener("hashchange", hash);
    return () => {
      document.removeEventListener("keydown", keyboard);
      window.removeEventListener("hashchange", hash);
    };
  }, []);
  function select(id: string | null) {
    setSelected(id);
    history.replaceState(null, "", id ? `#${id}` : location.pathname);
  }
  const priority = (job: Job) => (active(job.status) ? 0 : job.status === "queued" ? 1 : 2);
  const shown = filterJobs(jobs, filter, search).sort(
    (a, b) => (activeFirst ? priority(a) - priority(b) : 0) || b.created - a.created,
  );
  const counts = {
    running: jobs.filter((job) => active(job.status)).length,
    queued: jobs.filter((job) => job.status === "queued").length,
    succeeded: jobs.filter((job) => job.status === "succeeded").length,
    attention: jobs.filter((job) => attention(job.status)).length,
  };
  return (
    <div className="app">
      <header className="masthead">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
            <i />
          </span>
          <h1>MACQUEUE</h1>
          <span className="read-only">READ ONLY</span>
        </div>
        <div className="connection" role="status">
          <span className={error ? "connection-dot disconnected" : "connection-dot"} />
          {loading ? "CONNECTING" : error ? "DISCONNECTED" : "QUEUE CONNECTED"}
        </div>
      </header>
      <main>
        <div className="page-heading">
          <div>
            <span className="eyebrow">DEVBOX → MACBOOK PRO</span>
            <h2>
              Jobs<span className="job-total">/{String(jobs.length).padStart(2, "0")}</span>
            </h2>
          </div>
          <div className="refresh-controls">
            <span className="sync-time">
              {lastSync
                ? `SYNC ${new Date(lastSync * 1000).toLocaleTimeString("en-GB")}`
                : "NO SYNC"}
            </span>
            <button
              className={!auto ? "selected-control" : ""}
              aria-pressed={auto}
              onClick={() => setAuto(!auto)}
            >
              {auto ? "Ⅱ AUTO / 5s" : "▶ AUTO OFF"}
            </button>
            <button onClick={() => setRefresh((value) => value + 1)} aria-label="Refresh jobs">
              ↻ REFRESH
            </button>
          </div>
        </div>
        {error && (
          <div className="connection-error" role="alert">
            <strong>{error}</strong>
            <span>
              {lastSync ? `STALE / ${duration(now - lastSync)}` : "NO DATA"}
              {auto ? " · RETRY 5s" : ""}
            </span>
          </div>
        )}
        <div className="counters" aria-label="Job counts">
          {(
            [
              { key: "running", label: "IN PROGRESS", icon: "●" },
              { key: "queued", label: "QUEUED", icon: "□" },
              { key: "succeeded", label: "COMPLETED", icon: "✓" },
              { key: "attention", label: "FAILED / LOST / EXPIRED", icon: "×" },
            ] as const
          ).map((item) => (
            <button
              key={item.key}
              className={`counter ${item.key === "running" ? "counter-primary" : ""} ${filter === item.key ? "counter-selected" : ""}`}
              aria-pressed={filter === item.key}
              onClick={() => setFilter(filter === item.key ? "all" : item.key)}
            >
              <span className="counter-label">
                {item.label}
                <span>{item.icon}</span>
              </span>
              <span className="counter-value">
                {loading ? "—" : String(counts[item.key]).padStart(2, "0")}
              </span>
            </button>
          ))}
        </div>
        <div className={`workspace ${selected ? "has-detail" : ""}`}>
          <section className="queue" aria-label="Job queue">
            <div className="queue-tools">
              <label className="search">
                <span aria-hidden="true">⌕</span>
                <input
                  ref={input}
                  aria-label="Search jobs"
                  placeholder="Search job, ID, project…"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
                <kbd>/</kbd>
                {search && (
                  <button aria-label="Clear search" onClick={() => setSearch("")}>
                    ×
                  </button>
                )}
              </label>
              <button
                className="sort-button"
                aria-label="Change job order"
                onClick={() => setActiveFirst(!activeFirst)}
              >
                {activeFirst ? "ACTIVE FIRST ↓" : "NEWEST FIRST ↓"}
              </button>
              <span className="shown-count">{shown.length} SHOWN</span>
            </div>
            <div className="filters" aria-label="Filter jobs">
              {tabs.map((item) => (
                <button
                  key={item.value}
                  aria-pressed={filter === item.value}
                  onClick={() => setFilter(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>STATUS</th>
                    <th>JOB</th>
                    <th>SUBMITTED</th>
                    <th>ELAPSED</th>
                    <th>
                      <span className="sr-only">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((job) => (
                    <tr
                      key={job.id}
                      className={selected === job.id ? "selected-row" : ""}
                      onClick={() => select(job.id)}
                    >
                      <td>
                        <Badge status={job.status} />
                      </td>
                      <td className="job-cell">
                        <button
                          className="job-open"
                          aria-label={`Open ${job.label || shortId(job.id)}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            select(job.id);
                          }}
                        >
                          {job.label || "Untitled job"}
                        </button>
                        <div className="job-secondary">
                          <span>{shortId(job.id)}</span>
                          <span>{job.project}</span>
                          <span>
                            {job.steps} {job.steps === 1 ? "STEP" : "STEPS"}
                          </span>
                        </div>
                      </td>
                      <td className="submitted" title={clock(job.created)}>
                        {new Date(job.created * 1000).toLocaleDateString("en-GB", {
                          day: "2-digit",
                          month: "short",
                        })}
                        <small>
                          {new Date(job.created * 1000).toLocaleTimeString("en-GB", {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                        </small>
                      </td>
                      <td className="elapsed">{duration(elapsed(job, now))}</td>
                      <td className="row-arrow" aria-hidden="true">
                        ↗
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!shown.length && (
              <div className="empty">
                <span className="empty-icon">{loading ? "…" : "∅"}</span>
                {loading
                  ? "LOADING QUEUE"
                  : error && !jobs.length
                    ? "QUEUE UNAVAILABLE"
                    : search || filter !== "all"
                      ? "NO MATCHING JOBS"
                      : "NO JOBS"}
                {(search || filter !== "all") && (
                  <button
                    onClick={() => {
                      setSearch("");
                      setFilter("all");
                    }}
                  >
                    CLEAR FILTERS
                  </button>
                )}
              </div>
            )}
            <footer className="table-footer">
              <span>LATEST 100 · {jobs.length} RECEIVED</span>
              <span>LOCAL TIME</span>
            </footer>
          </section>
          {selected && (
            <JobDetail
              key={selected}
              id={selected}
              refresh={refresh}
              auto={auto}
              now={now}
              onClose={() => select(null)}
            />
          )}
        </div>
      </main>
      <footer className="app-footer">
        <span>MACQUEUE / MONITOR</span>
        <span>
          GET ONLY <span aria-hidden="true">↗</span>
        </span>
      </footer>
    </div>
  );
}
