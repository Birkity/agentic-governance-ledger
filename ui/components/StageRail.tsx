import type { StageState } from "../lib/ledger-data";

interface StageRailProps {
  stages: StageState[];
}

export function StageRail({ stages }: StageRailProps) {
  return (
    <section className="panel stack-md">
      <div className="section-header">
        <div>
          <p className="eyebrow">Lifecycle Rail</p>
          <h2>Where the application sits right now</h2>
        </div>
      </div>

      <div className="stage-rail">
        {stages.map((stage, index) => (
          <div
            key={stage.key}
            className={`stage-card stage-${stage.status}`}
            aria-current={stage.status === "current" ? "step" : undefined}
          >
            <div className="stage-index">{index + 1}</div>
            <div className="stage-copy">
              <h3>{stage.label}</h3>
              <p>{stage.hint}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
