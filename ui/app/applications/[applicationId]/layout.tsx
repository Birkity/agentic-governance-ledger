import { notFound } from "next/navigation";

import { ApplicationHeader } from "../../../components/ApplicationHeader";
import { getApplicationDetail } from "../../../lib/ledger-data";

export const dynamic = "force-dynamic";

interface ApplicationLayoutProps {
  children: React.ReactNode;
  params: Promise<{
    applicationId: string;
  }>;
}

export default async function ApplicationLayout({ children, params }: ApplicationLayoutProps) {
  const { applicationId } = await params;
  const detail = await getApplicationDetail(applicationId);
  if (!detail) {
    notFound();
  }

  return (
    <div className="stack-lg">
      <ApplicationHeader detail={detail} />
      {children}
    </div>
  );
}
