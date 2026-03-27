interface CompactInfoCardProps {
  title: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}

export function CompactInfoCard({ title, description, children, className }: CompactInfoCardProps) {
  return (
    <section className={`panel compact-card stack-md${className ? ` ${className}` : ""}`}>
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
