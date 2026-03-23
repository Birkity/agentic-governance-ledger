import { notFound } from "next/navigation";

import { AgentSessionList } from "../../../../components/AgentSessionList";
import { CompactInfoCard } from "../../../../components/CompactInfoCard";
import { getApplicationDetail } from "../../../../lib/ledger-data";

export const dynamic = "force-dynamic";

interface AgentsPageProps {
  params: {
    applicationId: string;
  };
}

export default async function AgentsPage({ params }: AgentsPageProps) {
  const detail = await getApplicationDetail(params.applicationId);
  if (!detail) {
    notFound();
  }

  return (
    <CompactInfoCard title="Agent Sessions" description="Recovered execution history for this application.">
      <AgentSessionList sessions={detail.agentSessions} />
    </CompactInfoCard>
  );
}
