import Link from "next/link";

export default function NotFound() {
  return (
    <section className="hero-panel">
      <div className="hero-copy">
        <p className="eyebrow">Application Not Found</p>
        <h1>No matching ledger view was found</h1>
        <p className="hero-body">
          The application id you requested is not available in the live ledger or the current seed world. Return to the
          dashboard and select a tracked application to inspect its history, evidence, and review path.
        </p>
      </div>

      <div className="panel stack-md">
        <p className="muted-copy">Try returning to the main workbench and choosing an application card from the current data set.</p>
        <Link href="/" className="ghost-link">
          Back to dashboard
        </Link>
      </div>
    </section>
  );
}
