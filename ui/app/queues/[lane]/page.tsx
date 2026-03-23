import { notFound } from "next/navigation";

import { ApplicationExplorer } from "../../../components/ApplicationExplorer";
import { getDashboardData } from "../../../lib/ledger-data";
import { isQueueLane } from "../../../lib/queues";

export const dynamic = "force-dynamic";

interface QueuePageProps {
  params: Promise<{
    lane: string;
  }>;
}

export default async function QueuePage({ params }: QueuePageProps) {
  const { lane } = await params;
  if (!isQueueLane(lane)) {
    notFound();
  }

  const dashboard = await getDashboardData();

  return (
    <ApplicationExplorer
      applications={dashboard.applications}
      sourceMode={dashboard.mode}
      queue={lane}
    />
  );
}
