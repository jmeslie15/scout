import { Badge } from './Badge'

/**
 * The evidence-strength key.
 *
 * Rendered next to every findings list so an operator reading a finding has the
 * ranking in front of them: MACHINE outranks INSPECTION, which outranks
 * AUTHORITATIVE, which outranks INFERENCE. Inference is at the bottom on
 * purpose — an AI claim is useful and never sufficient.
 */
export function EvidenceStrengthLegend() {
  return (
    <aside className="legend" aria-label="Evidence strength key">
      <h4 className="legend__title">How to read evidence</h4>
      <ul className="legend__list">
        <li>
          <Badge tone="deterministic" description="Rank 3 of 4. Deterministic execution.">
            MACHINE
          </Badge>
          <span>
            Tool run, test run, git diff, GitHub check, or stored artifact. Confidence ceiling 1.00.
          </span>
        </li>
        <li>
          <Badge tone="verified" description="Rank 2 of 4. Direct observation.">
            INSPECTION
          </Badge>
          <span>Direct observation of a file or repository structure. Confidence ceiling 0.95.</span>
        </li>
        <li>
          <Badge tone="human" description="Rank 1 of 4. A human answer or approved decision.">
            AUTHORITATIVE
          </Badge>
          <span>Human answer or approved engineering decision. Confidence ceiling 0.95.</span>
        </li>
        <li>
          <Badge tone="ai" description="Rank 0 of 4. An AI claim.">
            INFERENCE
          </Badge>
          <span>An AI claim. Confidence ceiling 0.65, and never sufficient on its own.</span>
        </li>
      </ul>
      <p className="legend__note">
        A finding is a <strong>verified fact</strong> only when its provenance is DETERMINISTIC, at
        least one cited reference is MACHINE strength, and its claimed confidence did not have to be
        capped. A finding with no evidence at all is forced to require human review.
      </p>
    </aside>
  )
}
