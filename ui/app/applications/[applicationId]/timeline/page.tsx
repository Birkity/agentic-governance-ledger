import { notFound } from "next/navigation";

import { TimelineExplorer } from "../../../../components/TimelineExplorer";
import { getApplicationDetail } from "../../../../lib/ledger-data";

export const dynamic = "force-dynamic";

interface TimelinePageProps {
  params: Promise<{
    applicationId: string;
  }>;
}

export default async function TimelinePage({ params }: TimelinePageProps) {
  const { applicationId } = await params;
  const detail = await getApplicationDetail(applicationId);
  if (!detail) {
    notFound();
  }

  return <TimelineExplorer timeline={detail.timeline} />;
}
