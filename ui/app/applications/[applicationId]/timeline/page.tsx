import { notFound } from "next/navigation";

import { TimelineExplorer } from "../../../../components/TimelineExplorer";
import { getApplicationDetail } from "../../../../lib/ledger-data";

export const dynamic = "force-dynamic";

interface TimelinePageProps {
  params: {
    applicationId: string;
  };
}

export default async function TimelinePage({ params }: TimelinePageProps) {
  const detail = await getApplicationDetail(params.applicationId);
  if (!detail) {
    notFound();
  }

  return <TimelineExplorer timeline={detail.timeline} />;
}
