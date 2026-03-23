import { notFound } from "next/navigation";

import { CompactInfoCard } from "../../../../components/CompactInfoCard";
import { DocumentPreview } from "../../../../components/DocumentPreview";
import { getApplicationDetail } from "../../../../lib/ledger-data";
import { SectionDataList } from "../../../../components/SectionDataList";

export const dynamic = "force-dynamic";

interface EvidencePageProps {
  params: Promise<{
    applicationId: string;
  }>;
  searchParams?: Promise<{
    doc?: string | string[];
  }>;
}

export default async function EvidencePage({ params, searchParams }: EvidencePageProps) {
  const { applicationId } = await params;
  const resolvedSearchParams = searchParams ? await searchParams : undefined;
  const selectedDoc = typeof resolvedSearchParams?.doc === "string" ? resolvedSearchParams.doc : undefined;
  const detail = await getApplicationDetail(applicationId, selectedDoc);
  if (!detail) {
    notFound();
  }

  return (
    <div className="overview-grid">
      <div className="stack-lg">
        <DocumentPreview applicationId={detail.item.applicationId} documents={detail.documents} preview={detail.documentPreview} />
      </div>

      <div className="stack-lg">
        <CompactInfoCard title="Evidence Summary" description="A compact view of the selected document pack.">
          <SectionDataList
            items={[
              { label: "Documents", value: detail.documents.length },
              { label: "Selected", value: detail.documentPreview.document?.name ?? "None selected" },
              { label: "Format", value: detail.documentPreview.document?.kind.toUpperCase() ?? "Not recorded" },
              { label: "Applicant", value: detail.item.applicantId ?? "Not recorded" }
            ]}
            columns={2}
          />
        </CompactInfoCard>
      </div>
    </div>
  );
}
