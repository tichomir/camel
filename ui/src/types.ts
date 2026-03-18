/**
 * TypeScript types mirroring the CaMeL Python SDK provenance data model.
 *
 * These types match the serialised JSON produced by:
 *   - `camel.provenance.ProvenanceHop.to_dict()`
 *   - `camel.provenance.ProvenanceChain.to_dict()`
 *   - `camel.provenance.PhishingWarning.to_dict()`
 *   - `camel_security.AgentResult` (provenance_chains / phishing_warnings fields)
 *
 * Stability guarantee: mirrors the Python SDK v0.6.0 schema.  New optional
 * fields may be added in minor releases; no fields will be removed or renamed
 * without a major-version bump.
 */

// ---------------------------------------------------------------------------
// ProvenanceHop — one origin step in a provenance chain
// ---------------------------------------------------------------------------

/**
 * One origin hop in a {@link ProvenanceChain}.
 *
 * Each hop represents a single source that contributed to a CaMeL variable's
 * final value.  A variable derived from multiple tool outputs has one hop per
 * distinct source in the merged `CaMeLValue.sources` frozenset.
 */
export interface ProvenanceHop {
  /** Origin label — tool identifier (e.g. `"get_last_email"`) or trusted label
   *  (`"User literal"` / `"CaMeL"`). */
  tool_name: string;

  /** Sub-field within the tool's structured output that was extracted (e.g.
   *  `"sender"` for the sender address of an email), or `null` for derived /
   *  composite values. */
  inner_source: string | null;

  /** Authorised audience — sorted list of principal strings (e.g. email
   *  addresses) or the string `"Public"` when the open-readers sentinel
   *  applies. */
  readers: string[] | "Public";

  /** ISO 8601 timestamp recording when this hop was produced, or `null` in
   *  SDK v0.6.0 (reserved for future use). */
  timestamp: string | null;
}

// ---------------------------------------------------------------------------
// ProvenanceChain — full provenance lineage for one variable
// ---------------------------------------------------------------------------

/**
 * Full provenance lineage for a single named CaMeL variable.
 *
 * Returned by `AgentResult.provenance_chains[variableName]` and by
 * `agent.get_provenance(variableName, result)`.
 */
export interface ProvenanceChain {
  /** Name of the variable in the interpreter's variable store. */
  variable_name: string;

  /** Ordered list of origin hops — trusted hops first (alphabetic), then
   *  untrusted hops (alphabetic). */
  hops: ProvenanceHop[];

  /** `true` iff every hop originates from a trusted source (`"User literal"`
   *  or `"CaMeL"`). An empty `hops` array is considered trusted. */
  is_trusted: boolean;
}

// ---------------------------------------------------------------------------
// PhishingWarning — heuristic phishing-content surface detection
// ---------------------------------------------------------------------------

/**
 * Warning emitted when the phishing-content heuristic fires.
 *
 * Produced when a `CaMeLValue` contains text that claims a trusted sender
 * identity (e.g. `From: ceo@example.com`) while originating from an untrusted
 * tool output.  Surfaces in `AgentResult.phishing_warnings`.
 *
 * The warning is **advisory** — it does not block execution.  UIs should
 * display a prominent warning banner to the user.
 */
export interface PhishingWarning {
  /** The variable whose value triggered the heuristic. */
  variable_name: string;

  /** String representation of the matching regular expression. */
  matched_pattern: string;

  /** The substring of the value's text that matched the pattern. */
  matched_text: string;

  /** Subset of the variable's sources that are not in `TRUSTED_SOURCES`. */
  untrusted_sources: string[];

  /** Full provenance chain for the triggering variable, for UI display. */
  provenance_chain: ProvenanceChain;
}

// ---------------------------------------------------------------------------
// Convenience composite — subset of AgentResult needed by the annotator
// ---------------------------------------------------------------------------

/**
 * The subset of `AgentResult` fields consumed by {@link ChatResponseAnnotator}.
 *
 * Callers may pass the full `AgentResult` object (the extra fields are simply
 * ignored) or construct a minimal object satisfying this interface.
 */
export interface AnnotatorInput {
  /** The natural-language response text to annotate. */
  response_text: string;

  /** Provenance chains keyed by variable name, from `AgentResult.provenance_chains`. */
  provenance_chains: Record<string, ProvenanceChain>;

  /** Optional phishing warnings, from `AgentResult.phishing_warnings`. */
  phishing_warnings?: PhishingWarning[];
}

// ---------------------------------------------------------------------------
// AnnotatedResponse — output of ChatResponseAnnotator.annotate()
// ---------------------------------------------------------------------------

/** A single source badge descriptor attached to the annotated response. */
export interface SourceBadge {
  /** Tool name that contributed untrusted data. */
  tool_name: string;

  /** Names of variables originating from this tool. */
  variable_names: string[];
}

/**
 * Structured output of {@link ChatResponseAnnotator.annotate}.
 *
 * Contains both the rendered HTML string and machine-readable metadata so
 * callers can build their own rendering if they prefer to avoid using the
 * pre-built HTML.
 */
export interface AnnotatedResponse {
  /**
   * Annotated HTML string.
   *
   * The response text is wrapped in a `<div class="camel-response">` element.
   * Each untrusted-source badge is rendered as:
   * ```html
   * <span class="camel-source-badge"
   *       aria-label="Source: <tool_name>"
   *       data-tool="<tool_name>">
   *   [Source: <tool_name>]
   * </span>
   * ```
   * When `phishing_warnings` is non-empty, a warning banner is prepended:
   * ```html
   * <div role="alert" class="camel-phishing-warning" aria-label="…">…</div>
   * ```
   */
  html: string;

  /** `true` iff at least one source badge was added. */
  has_badges: boolean;

  /** `true` iff a phishing warning banner was rendered. */
  has_phishing_warning: boolean;

  /** Deduplicated list of untrusted source tool names found across all chains. */
  untrusted_sources: string[];

  /** Structured badge descriptors, one per distinct untrusted tool. */
  badges: SourceBadge[];

  /** Copy of the phishing warnings passed in, for programmatic access. */
  phishing_warnings: PhishingWarning[];
}

// ---------------------------------------------------------------------------
// Trusted-source constant (mirrors Python TRUSTED_SOURCES)
// ---------------------------------------------------------------------------

/**
 * Set of origin labels that CaMeL considers intrinsically trusted.
 *
 * Mirrors `camel.provenance.TRUSTED_SOURCES`.
 */
export const TRUSTED_SOURCES: ReadonlySet<string> = new Set([
  "User literal",
  "CaMeL",
]);
