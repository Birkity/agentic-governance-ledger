import Link from "next/link";

import type { DocumentPreviewModel, DocumentResource } from "../lib/ledger-data";

interface DocumentPreviewProps {
  applicationId: string;
  documents: DocumentResource[];
  preview: DocumentPreviewModel;
}

function renderCsvTable(rows: string[][] | null) {
  if (!rows || rows.length === 0) {
    return <p className="muted-copy">No CSV preview is available for this document.</p>;
  }

  const [header, ...body] = rows;
  return (
    <div className="table-shell">
      <table>
        <thead>
          <tr>
            {header.map((cell, index) => (
              <th key={`${cell}-${index}`}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={`${rowIndex}-${cellIndex}`}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DocumentPreview({ applicationId, documents, preview }: DocumentPreviewProps) {
  return (
    <section className="panel stack-lg">
      <div className="section-header">
        <div>
          <p className="eyebrow">Evidence Pack</p>
          <h2>Inspect the source documents beside the ledger timeline</h2>
        </div>
      </div>

      {documents.length === 0 ? (
        <p className="muted-copy">No local document bundle was found for this applicant.</p>
      ) : (
        <>
          <div className="document-tabs">
            {documents.map((document) => {
              const isActive = preview.document?.name === document.name;
              return (
                <Link
                  key={document.name}
                  href={`/applications/${applicationId}?doc=${encodeURIComponent(document.name)}`}
                  className={`doc-tab ${isActive ? "doc-tab-active" : ""}`}
                >
                  <span>{document.name}</span>
                  <small>{document.kind.toUpperCase()}</small>
                </Link>
              );
            })}
          </div>

          {preview.document ? (
            <div className="document-frame">
              <div className="document-frame-top">
                <div>
                  <p className="card-kicker">{preview.document.kind.toUpperCase()}</p>
                  <h3>{preview.document.name}</h3>
                </div>
                <a href={preview.document.href} target="_blank" rel="noreferrer" className="ghost-link">
                  Open raw file
                </a>
              </div>

              {preview.document.kind === "pdf" ? (
                <iframe src={preview.document.href} title={preview.document.name} className="pdf-frame" />
              ) : null}

              {preview.document.kind === "csv" ? renderCsvTable(preview.selectedCsvRows) : null}

              {preview.document.kind === "xlsx" ? (
                <div className="xlsx-preview">
                  <p>
                    Workbook preview is download-first here so the interface stays fast. Use the raw file link to open the full
                    spreadsheet locally.
                  </p>
                </div>
              ) : null}

              {preview.document.kind === "other" ? (
                <div className="xlsx-preview">
                  <p>This file type is available for download, but there is no inline renderer in the UI.</p>
                </div>
              ) : null}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
