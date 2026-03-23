import { notFound } from "next/navigation";

import { DocumentPreview } from "../../../components/DocumentPreview";
import { StageRail } from "../../../components/StageRail";
import { TimelineExplorer } from "../../../components/TimelineExplorer";
import { getApplicationDetail } from "../../../lib/ledger-data";
import { formatCompactNumber, formatCurrency, formatDateTime } from "../../../lib/presenters";

export const dynamic = "force-dynamic";

interface ApplicationPageProps {
  params: {
    applicationId: string;
  };
  searchParams?: {
    doc?: string | string[];
  };
}

export default async function ApplicationPage({ params, searchParams }: ApplicationPageProps) {
  const selectedDoc = typeof searchParams?.doc === "string" ? searchParams.doc : undefined;
  const detail = await getApplicationDetail(params.applicationId, selectedDoc);

  if (!detail) {
    notFound();
  }

  return (
    <div className="stack-xl">
      <section className="hero-panel hero-panel-detail">
        <div className="hero-copy">
          <p className="eyebrow">{detail.item.applicationId}</p>
          <h1>{detail.item.companyName}</h1>
          <p className="hero-body">
            Follow the application from intake through credit, fraud, compliance, decisioning, audit checks, and any manual
            intervention. The timeline below is built from durable event history, not ad hoc UI state.
          </p>
        </div>

        <div className="hero-metrics">
          <div className="metric-card">
            <span>Requested</span>
            <strong>{formatCurrency(detail.item.requestedAmountUsd)}</strong>
          </div>
          <div className="metric-card">
            <span>Approved</span>
            <strong>{formatCurrency(detail.item.approvedAmountUsd)}</strong>
          </div>
          <div className="metric-card">
            <span>Fraud score</span>
            <strong>{formatCompactNumber(detail.item.fraudScore)}</strong>
          </div>
          <div className="metric-card">
            <span>Last event</span>
            <strong>{formatDateTime(detail.item.lastEventAt)}</strong>
          </div>
        </div>
      </section>

      <StageRail stages={detail.stages} />

      <section className="detail-grid">
        <div className="stack-lg">
          <TimelineExplorer timeline={detail.timeline} />
          <DocumentPreview
            applicationId={detail.item.applicationId}
            documents={detail.documents}
            preview={detail.documentPreview}
          />
        </div>

        <aside className="stack-lg">
          <section className="panel stack-md">
            <div className="section-header">
              <div>
                <p className="eyebrow">Current Snapshot</p>
                <h2>Business state</h2>
              </div>
            </div>

            <dl className="info-list">
              <div>
                <dt>Status</dt>
                <dd>{detail.item.state.replaceAll("_", " ")}</dd>
              </div>
              <div>
                <dt>Decision</dt>
                <dd>{detail.item.decision ?? "Pending"}</dd>
              </div>
              <div>
                <dt>Compliance</dt>
                <dd>{detail.item.complianceStatus ?? "In progress"}</dd>
              </div>
              <div>
                <dt>Risk tier</dt>
                <dd>{detail.item.riskTier ?? "Not recorded"}</dd>
              </div>
              <div>
                <dt>Applicant</dt>
                <dd>{detail.company?.companyId ?? detail.item.applicantId ?? "Not recorded"}</dd>
              </div>
              <div>
                <dt>Jurisdiction</dt>
                <dd>{detail.company?.jurisdiction ?? "Not recorded"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel stack-md">
            <div className="section-header">
              <div>
                <p className="eyebrow">Human Review</p>
                <h2>Manual decision path</h2>
              </div>
            </div>

            <dl className="info-list">
              <div>
                <dt>Requested</dt>
                <dd>{detail.review.requested ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt>Completed</dt>
                <dd>{detail.review.completed ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt>Reviewer</dt>
                <dd>{detail.review.reviewerId ?? "Not recorded"}</dd>
              </div>
              <div>
                <dt>Override</dt>
                <dd>{detail.review.override ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt>Final decision</dt>
                <dd>{detail.review.finalDecision ?? "Not recorded"}</dd>
              </div>
              <div>
                <dt>Reason</dt>
                <dd>{detail.review.overrideReason ?? "No override reason recorded"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel stack-md">
            <div className="section-header">
              <div>
                <p className="eyebrow">Compliance</p>
                <h2>Rule-by-rule trace</h2>
              </div>
            </div>

            <dl className="info-list">
              <div>
                <dt>Verdict</dt>
                <dd>{detail.compliance.overallVerdict ?? "In progress"}</dd>
              </div>
              <div>
                <dt>Regulation set</dt>
                <dd>{detail.compliance.regulationSetVersion ?? "Not recorded"}</dd>
              </div>
              <div>
                <dt>Passed rules</dt>
                <dd>{detail.compliance.passedRules.length}</dd>
              </div>
              <div>
                <dt>Failed rules</dt>
                <dd>{detail.compliance.failedRules.length}</dd>
              </div>
              <div>
                <dt>Hard blocks</dt>
                <dd>{detail.compliance.hardBlockRules.join(", ") || "None recorded"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel stack-md">
            <div className="section-header">
              <div>
                <p className="eyebrow">Audit And Ops</p>
                <h2>Integrity, lag, and concurrency</h2>
              </div>
            </div>

            <dl className="info-list">
              <div>
                <dt>Audit events</dt>
                <dd>{detail.audit.eventCount}</dd>
              </div>
              <div>
                <dt>Chain valid</dt>
                <dd>{detail.audit.chainValid === null ? "Not checked yet" : detail.audit.chainValid ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt>Tamper detected</dt>
                <dd>
                  {detail.audit.tamperDetected === null ? "Not checked yet" : detail.audit.tamperDetected ? "Yes" : "No"}
                </dd>
              </div>
              <div>
                <dt>Latest check</dt>
                <dd>{formatDateTime(detail.audit.latestCheckAt)}</dd>
              </div>
            </dl>

            <details className="json-panel">
              <summary>Projection lag report</summary>
              <pre>{detail.operations.projectionLagReport}</pre>
            </details>

            <details className="json-panel">
              <summary>Concurrency guardrail report</summary>
              <pre>{detail.operations.concurrencyReport}</pre>
            </details>
          </section>

          <section className="panel stack-md">
            <div className="section-header">
              <div>
                <p className="eyebrow">Agent Sessions</p>
                <h2>Recovered execution logs</h2>
              </div>
            </div>

            {detail.agentSessions.length === 0 ? (
              <p className="muted-copy">No agent-session stream was attached to this application.</p>
            ) : (
              <div className="stack-md">
                {detail.agentSessions.map((session) => (
                  <article key={session.sessionId} className="subpanel">
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

                    <dl className="info-list">
                      <div>
                        <dt>Agent</dt>
                        <dd>{session.agentId ?? "Not recorded"}</dd>
                      </div>
                      <div>
                        <dt>Model</dt>
                        <dd>{session.modelVersion ?? "Not recorded"}</dd>
                      </div>
                      <div>
                        <dt>Started</dt>
                        <dd>{formatDateTime(session.startedAt)}</dd>
                      </div>
                      <div>
                        <dt>Completed</dt>
                        <dd>{formatDateTime(session.completedAt)}</dd>
                      </div>
                      <div>
                        <dt>Nodes</dt>
                        <dd>{session.nodesExecuted}</dd>
                      </div>
                      <div>
                        <dt>Tools</dt>
                        <dd>{session.toolsCalled}</dd>
                      </div>
                      <div>
                        <dt>Output events</dt>
                        <dd>{session.outputEvents}</dd>
                      </div>
                    </dl>
                  </article>
                ))}
              </div>
            )}
          </section>
        </aside>
      </section>
    </div>
  );
}
