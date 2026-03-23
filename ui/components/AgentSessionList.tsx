import type { AgentSessionCard } from "../lib/ledger-data";
import { formatCurrency, formatDateTime } from "../lib/presenters";

interface AgentSessionListProps {
  sessions: AgentSessionCard[];
}

export function AgentSessionList({ sessions }: AgentSessionListProps) {
  if (sessions.length === 0) {
    return <p className="muted-copy">No agent-session stream was attached to this application.</p>;
  }

  return (
    <div className="session-list">
      {sessions.map((session) => (
        <article key={session.sessionId} className="session-card">
          <div className="timeline-card-top">
            <div>
              <p className="card-kicker">{session.sessionId}</p>
              <h3>{session.agentType}</h3>
            </div>
            <span
              className={`status-pill ${
                session.status === "completed"
                  ? "status-pill-success"
                  : session.status === "failed"
                    ? "status-pill-danger"
                    : "status-pill-warning"
              }`}
            >
              {session.status}
            </span>
          </div>

          <div className="compact-facts compact-facts-three">
            <div>
              <span className="fact-label">Agent</span>
              <strong>{session.agentId ?? "Not recorded"}</strong>
            </div>
            <div>
              <span className="fact-label">Model</span>
              <strong>{session.modelVersion ?? "Not recorded"}</strong>
            </div>
            <div>
              <span className="fact-label">Started</span>
              <strong>{formatDateTime(session.startedAt)}</strong>
            </div>
            <div>
              <span className="fact-label">Completed</span>
              <strong>{formatDateTime(session.completedAt)}</strong>
            </div>
            <div>
              <span className="fact-label">Nodes</span>
              <strong>{session.nodesExecuted}</strong>
            </div>
            <div>
              <span className="fact-label">Tools</span>
              <strong>{session.toolsCalled}</strong>
            </div>
            <div>
              <span className="fact-label">Output events</span>
              <strong>{session.outputEvents}</strong>
            </div>
            <div>
              <span className="fact-label">Cost</span>
              <strong>{session.totalCostUsd === null ? "Not recorded" : formatCurrency(String(session.totalCostUsd))}</strong>
            </div>
          </div>
        </article>
      ))}
    </div>
  );
}
