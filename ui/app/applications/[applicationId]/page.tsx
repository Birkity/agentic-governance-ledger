import Link from "next/link";
import { notFound } from "next/navigation";

import { CompactInfoCard } from "../../../components/CompactInfoCard";
import { SectionDataList } from "../../../components/SectionDataList";
import { StageRail } from "../../../components/StageRail";
import { getApplicationDetail } from "../../../lib/ledger-data";
import { formatCurrency, formatDateTime } from "../../../lib/presenters";

export const dynamic = "force-dynamic";

interface ApplicationPageProps {
  params: Promise<{
    applicationId: string;
  }>;
}

export default async function ApplicationPage({ params }: ApplicationPageProps) {
  const { applicationId } = await params;
  const detail = await getApplicationDetail(applicationId);

  if (!detail) {
    notFound();
  }

  return (
    <div className="stack-xl">
      <StageRail stages={detail.stages} />

      <section className="overview-grid">
        <div className="stack-lg">
          <CompactInfoCard title="Current Snapshot" description="The most useful state at a glance.">
            <SectionDataList
              items={[
                { label: "Status", value: detail.item.state.replaceAll("_", " ") },
                { label: "Decision", value: detail.item.decision ?? "Pending" },
                { label: "Compliance", value: detail.item.complianceStatus ?? "In progress" },
                { label: "Risk tier", value: detail.item.riskTier ?? "Not recorded" },
                { label: "Requested", value: formatCurrency(detail.item.requestedAmountUsd) },
                { label: "Approved", value: formatCurrency(detail.item.approvedAmountUsd) }
              ]}
              columns={3}
            />
          </CompactInfoCard>

          <CompactInfoCard title="Quick Links" description="Jump straight into the next useful workspace.">
            <div className="shortcut-grid">
              <Link href={`/applications/${detail.item.applicationId}/timeline`} className="shortcut-card">
                <span className="fact-label">Timeline</span>
                <strong>{detail.timeline.length} events</strong>
                <p>Inspect the durable event log.</p>
              </Link>
              <Link href={`/applications/${detail.item.applicationId}/evidence`} className="shortcut-card">
                <span className="fact-label">Evidence</span>
                <strong>{detail.documents.length} documents</strong>
                <p>Preview PDFs, CSVs, and workbooks.</p>
              </Link>
              <Link href={`/applications/${detail.item.applicationId}/oversight`} className="shortcut-card">
                <span className="fact-label">Oversight</span>
                <strong>{detail.compliance.failedRules.length} failed rules</strong>
                <p>See compliance, lag, and integrity.</p>
              </Link>
              <Link href={`/applications/${detail.item.applicationId}/agents`} className="shortcut-card">
                <span className="fact-label">Agents</span>
                <strong>{detail.agentSessions.length} sessions</strong>
                <p>Review recovered agent activity.</p>
              </Link>
            </div>
          </CompactInfoCard>

          <CompactInfoCard title="Recent Timeline" description="Latest durable events for quick orientation.">
            <div className="mini-timeline">
              {detail.timeline.slice(-6).reverse().map((event) => (
                <div key={event.id} className="mini-timeline-item">
                  <div>
                    <span className="fact-label">{event.streamFamily}</span>
                    <strong>{event.eventType}</strong>
                  </div>
                  <span>{formatDateTime(event.recordedAt)}</span>
                </div>
              ))}
            </div>
          </CompactInfoCard>
        </div>

        <div className="stack-lg">
          <CompactInfoCard title="Applicant Context">
            <SectionDataList
              items={[
                { label: "Applicant", value: detail.company?.companyId ?? detail.item.applicantId ?? "Not recorded" },
                { label: "Industry", value: detail.company?.industry ?? detail.item.industry ?? "Not recorded" },
                { label: "Jurisdiction", value: detail.company?.jurisdiction ?? detail.item.jurisdiction ?? "Not recorded" },
                { label: "Legal type", value: detail.company?.legalType ?? "Not recorded" },
                { label: "Trajectory", value: detail.company?.trajectory ?? "Not recorded" },
                { label: "Risk segment", value: detail.company?.riskSegment ?? "Not recorded" }
              ]}
              columns={2}
            />
          </CompactInfoCard>

          <CompactInfoCard title="Human Review">
            <SectionDataList
              items={[
                { label: "Requested", value: detail.review.requested ? "Yes" : "No" },
                { label: "Completed", value: detail.review.completed ? "Yes" : "No" },
                { label: "Reviewer", value: detail.review.reviewerId ?? "Not recorded" },
                { label: "Override", value: detail.review.override ? "Yes" : "No" },
                { label: "Final decision", value: detail.review.finalDecision ?? "Not recorded" },
                { label: "Reason", value: detail.review.overrideReason ?? "No override reason recorded" }
              ]}
              columns={2}
            />
          </CompactInfoCard>

          <CompactInfoCard title="Oversight Snapshot">
            <SectionDataList
              items={[
                { label: "Verdict", value: detail.compliance.overallVerdict ?? "In progress" },
                { label: "Passed rules", value: detail.compliance.passedRules.length },
                { label: "Failed rules", value: detail.compliance.failedRules.length },
                { label: "Hard blocks", value: detail.compliance.hardBlockRules.join(", ") || "None recorded" },
                {
                  label: "Integrity",
                  value: detail.audit.chainValid === null ? "Not checked yet" : detail.audit.chainValid ? "Healthy" : "Attention"
                },
                { label: "Last check", value: formatDateTime(detail.audit.latestCheckAt) }
              ]}
              columns={2}
            />
          </CompactInfoCard>
        </div>
      </section>
    </div>
  );
}
