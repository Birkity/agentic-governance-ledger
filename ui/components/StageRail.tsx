import type { StageState } from "../lib/ledger-data";

interface StageRailProps {
  stages: StageState[];
}

export function StageRail({ stages }: StageRailProps) {
  const currentIndex = stages.findIndex((stage) => stage.status === "current");
  const fallbackIndex = currentIndex >= 0 ? currentIndex : Math.max(stages.findLastIndex((stage) => stage.status === "complete"), 0);
  const focusStage = stages[fallbackIndex] ?? stages[0];
  const completedCount = stages.filter((stage) => stage.status === "complete").length;

  return (
    <section className="panel stack-md">
      <div className="section-header">
        <div>
          <p className="eyebrow">Lifecycle Rail</p>
          <h2>Where the application sits right now</h2>
        </div>
      </div>

      <div className="stage-strip" role="list" aria-label="Application lifecycle stages">
        {stages.map((stage, index) => (
          <div
            key={stage.key}
            className={`stage-pill stage-${stage.status}`}
            role="listitem"
            aria-current={stage.status === "current" ? "step" : undefined}
          >
            <div className="stage-marker">{stage.status === "complete" ? "✓" : index + 1}</div>
            <span className="stage-label">{stage.label}</span>
          </div>
        ))}
      </div>

      {focusStage ? (
        <div className="stage-focus-card">
          <div className="stage-focus-copy">
            <span
              className={`status-pill ${
                focusStage.status === "complete"
                  ? "status-pill-success"
                  : focusStage.status === "current"
                    ? "status-pill-neutral"
                    : "status-pill-warning"
              }`}
            >
              {focusStage.status === "current" ? "Current stage" : focusStage.status === "complete" ? "Completed stage" : "Upcoming stage"}
            </span>
            <h3>{focusStage.label}</h3>
            <p>{focusStage.hint}</p>
          </div>

          <div className="stage-focus-metrics">
            <div>
              <span className="fact-label">Progress</span>
              <strong>
                {Math.min(completedCount + (currentIndex >= 0 ? 1 : 0), stages.length)}/{stages.length}
              </strong>
            </div>
            <div>
              <span className="fact-label">Position</span>
              <strong>{fallbackIndex + 1}</strong>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
