import { notFound } from "next/navigation";

import { CompactInfoCard } from "../../../../components/CompactInfoCard";
import { SectionDataList } from "../../../../components/SectionDataList";
import { getApplicationDetail } from "../../../../lib/ledger-data";
import { formatDateTime } from "../../../../lib/presenters";

export const dynamic = "force-dynamic";

interface OversightPageProps {
  params: Promise<{
    applicationId: string;
  }>;
}

export default async function OversightPage({ params }: OversightPageProps) {
  const { applicationId } = await params;
  const detail = await getApplicationDetail(applicationId);
  if (!detail) {
    notFound();
  }

  return (
    <div className="overview-grid">
      <div className="stack-lg">
        <CompactInfoCard title="Compliance">
          <SectionDataList
            items={[
              { label: "Verdict", value: detail.compliance.overallVerdict ?? "In progress" },
              { label: "Regulation set", value: detail.compliance.regulationSetVersion ?? "Not recorded" },
              { label: "Passed rules", value: detail.compliance.passedRules.length },
              { label: "Failed rules", value: detail.compliance.failedRules.length },
              { label: "Noted rules", value: detail.compliance.notedRules.length },
              { label: "Hard blocks", value: detail.compliance.hardBlockRules.join(", ") || "None recorded" }
            ]}
            columns={2}
          />

          <details className="json-panel">
            <summary>Snapshot history</summary>
            <pre>{JSON.stringify(detail.compliance.snapshots, null, 2)}</pre>
          </details>
        </CompactInfoCard>

        <CompactInfoCard title="Audit Integrity">
          <SectionDataList
            items={[
              { label: "Audit events", value: detail.audit.eventCount },
              {
                label: "Chain valid",
                value: detail.audit.chainValid === null ? "Not checked yet" : detail.audit.chainValid ? "Yes" : "No"
              },
              {
                label: "Tamper detected",
                value: detail.audit.tamperDetected === null ? "Not checked yet" : detail.audit.tamperDetected ? "Yes" : "No"
              },
              { label: "Latest check", value: formatDateTime(detail.audit.latestCheckAt) },
              { label: "Integrity hash", value: detail.audit.latestIntegrityHash ?? "Not recorded" }
            ]}
            columns={2}
          />
        </CompactInfoCard>
      </div>

      <div className="stack-lg">
        <CompactInfoCard title="Projection Health">
          <details className="json-panel" open>
            <summary>Projection lag report</summary>
            <pre>{detail.operations.projectionLagReport}</pre>
          </details>
        </CompactInfoCard>

        <CompactInfoCard title="Concurrency Guardrail">
          <details className="json-panel" open>
            <summary>OCC report</summary>
            <pre>{detail.operations.concurrencyReport}</pre>
          </details>
        </CompactInfoCard>
      </div>
    </div>
  );
}
