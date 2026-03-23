interface CompactInfoCardProps {
  title: string;
  description?: string;
  children: React.ReactNode;
}

export function CompactInfoCard({ title, description, children }: CompactInfoCardProps) {
  return (
    <section className="panel compact-card stack-md">
      <div className="section-header">
        <div>
          <h2>{title}</h2>
          {description ? <p className="muted-copy">{description}</p> : null}
        </div>
      </div>
      {children}
    </section>
  );
}
