interface SectionDataListProps {
  items: Array<{
    label: string;
    value: React.ReactNode;
  }>;
  columns?: 2 | 3;
}

export function SectionDataList({ items, columns = 2 }: SectionDataListProps) {
  return (
    <dl className={`data-grid data-grid-${columns}`}>
      {items.map((item) => (
        <div key={item.label} className="data-cell">
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
