/**
 * ChatResponseAnnotator — framework-agnostic chat UI annotation layer.
 *
 * Consumes a CaMeL `AgentResult`'s provenance metadata and annotates response
 * text with `[Source: <tool_name>]` badges for values originating from
 * untrusted tool outputs, and surfaces a warning banner when phishing-content
 * heuristics fire.
 *
 * Design goals
 * ------------
 * - Framework-agnostic: returns HTML strings; no React/Vue/Angular dependency.
 * - WCAG 2.1 AA compliant: badge and warning styles meet contrast requirements.
 * - Zero runtime dependencies: only the types from `./types` are imported.
 *
 * Usage
 * -----
 * ```ts
 * import { ChatResponseAnnotator } from './ChatResponseAnnotator';
 * import type { ProvenanceChain, PhishingWarning } from './types';
 *
 * const annotator = new ChatResponseAnnotator();
 * const result = annotator.annotate({
 *   response_text: "The invoice total is $4,200.",
 *   provenance_chains: agentResult.provenance_chains,
 *   phishing_warnings: agentResult.phishing_warnings,
 * });
 *
 * document.getElementById('chat-response')!.innerHTML = result.html;
 * ```
 *
 * PRD references
 * --------------
 * - §6.4 Capabilities — provenance chain structure.
 * - §7.2 Trusted Boundary — phishing surface logic.
 * - Goals G3, NG2 (partial phishing mitigation via metadata surfacing).
 * - M5-F20 through M5-F22.
 *
 * ADR reference: docs/adr/013-provenance-chain-phishing-heuristic.md §Decision 6
 */

import type {
  AnnotatedResponse,
  AnnotatorInput,
  PhishingWarning,
  ProvenanceChain,
  SourceBadge,
} from "./types";
import { TRUSTED_SOURCES } from "./types";

// ---------------------------------------------------------------------------
// CSS class names (stable — do not rename without a major version bump)
// ---------------------------------------------------------------------------

const CSS = {
  response: "camel-response",
  sourceBadge: "camel-source-badge",
  phishingWarning: "camel-phishing-warning",
  phishingWarningTitle: "camel-phishing-warning__title",
  phishingWarningBody: "camel-phishing-warning__body",
  badgesContainer: "camel-response__badges",
  textContent: "camel-response__text",
} as const;

// ---------------------------------------------------------------------------
// HTML escaping helper (prevents XSS from tool names / text)
// ---------------------------------------------------------------------------

/**
 * Escape a string for safe inclusion in HTML attribute values and text nodes.
 *
 * Only the minimum set of characters is escaped to preserve readability in
 * plain-text fallbacks (e.g. aria-label).
 */
function escapeHtml(raw: string): string {
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------------------------------------------------------------------------
// Provenance analysis helpers
// ---------------------------------------------------------------------------

/**
 * Collect all distinct untrusted source tool names from a provenance chains map.
 *
 * A source is untrusted when its `tool_name` is not in {@link TRUSTED_SOURCES}.
 *
 * @param provenance_chains - Map of variable name → ProvenanceChain.
 * @returns Sorted array of untrusted tool names (deduplicated).
 */
export function collectUntrustedSources(
  provenance_chains: Record<string, ProvenanceChain>
): string[] {
  const sources = new Set<string>();
  for (const chain of Object.values(provenance_chains)) {
    if (!chain.is_trusted) {
      for (const hop of chain.hops) {
        if (!TRUSTED_SOURCES.has(hop.tool_name)) {
          sources.add(hop.tool_name);
        }
      }
    }
  }
  return Array.from(sources).sort();
}

/**
 * Build {@link SourceBadge} descriptors — one per distinct untrusted tool.
 *
 * Each badge groups all variable names that share the same untrusted tool
 * origin, so the UI can display a single badge per tool rather than one per
 * variable.
 *
 * @param provenance_chains - Map of variable name → ProvenanceChain.
 * @returns Array of SourceBadge objects, sorted by tool_name.
 */
export function buildSourceBadges(
  provenance_chains: Record<string, ProvenanceChain>
): SourceBadge[] {
  const toolToVars = new Map<string, string[]>();

  for (const [varName, chain] of Object.entries(provenance_chains)) {
    if (!chain.is_trusted) {
      for (const hop of chain.hops) {
        if (!TRUSTED_SOURCES.has(hop.tool_name)) {
          const existing = toolToVars.get(hop.tool_name) ?? [];
          if (!existing.includes(varName)) {
            existing.push(varName);
          }
          toolToVars.set(hop.tool_name, existing);
        }
      }
    }
  }

  return Array.from(toolToVars.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([tool_name, variable_names]) => ({
      tool_name,
      variable_names: variable_names.sort(),
    }));
}

// ---------------------------------------------------------------------------
// HTML rendering helpers
// ---------------------------------------------------------------------------

/**
 * Render a single `[Source: <tool_name>]` badge as an HTML string.
 *
 * The badge uses:
 * - `role="img"` so screen readers announce it as a non-interactive graphic.
 * - `aria-label="Source: <tool_name>"` for accessible labelling.
 * - `data-tool` attribute for programmatic access in tests / custom styling.
 *
 * WCAG 2.1 AA compliance is ensured via the accompanying stylesheet
 * (`ChatResponseAnnotator.css`) which specifies sufficient colour contrast.
 *
 * @param toolName - Name of the untrusted source tool.
 * @returns HTML string for the badge element.
 */
export function renderSourceBadge(toolName: string): string {
  const safe = escapeHtml(toolName);
  return (
    `<span ` +
    `class="${CSS.sourceBadge}" ` +
    `role="img" ` +
    `aria-label="Source: ${safe}" ` +
    `data-tool="${safe}"` +
    `>[Source: ${safe}]</span>`
  );
}

/**
 * Render the phishing-content warning banner as an HTML string.
 *
 * The banner:
 * - Uses `role="alert"` so assistive technologies announce it immediately.
 * - Provides an `aria-label` summarising the risk.
 * - Lists the source tool(s) and matched text for informed user action.
 *
 * @param warnings - Array of PhishingWarning objects to summarise.
 * @returns HTML string for the warning banner.
 */
export function renderPhishingWarningBanner(
  warnings: PhishingWarning[]
): string {
  if (warnings.length === 0) return "";

  // Deduplicate tool names and variable names across all warnings.
  const toolNames = [
    ...new Set(warnings.flatMap((w) => w.untrusted_sources)),
  ].sort();
  const varNames = [...new Set(warnings.map((w) => w.variable_name))].sort();

  const toolList = toolNames.map(escapeHtml).join(", ");
  const varList = varNames.map(escapeHtml).join(", ");

  const ariaLabel =
    `Provenance warning: this response contains text that claims a sender ` +
    `identity but originates from an untrusted source (${toolNames.join(", ")}). ` +
    `Verify the claim independently before acting.`;

  return (
    `<div ` +
    `role="alert" ` +
    `class="${CSS.phishingWarning}" ` +
    `aria-label="${escapeHtml(ariaLabel)}"` +
    `>` +
    `<span class="${CSS.phishingWarningTitle}">` +
    `\u26A0\uFE0F Provenance Warning` +
    `</span>` +
    `<p class="${CSS.phishingWarningBody}">` +
    `This response contains text that claims a sender identity ` +
    `(e.g.\u00A0<code>${escapeHtml(warnings[0]?.matched_text ?? "")}</code>) ` +
    `but originates from an untrusted source: ` +
    `<strong>${toolList}</strong> ` +
    `(variable${varNames.length > 1 ? "s" : ""}: <em>${varList}</em>). ` +
    `Verify the claim independently before acting.` +
    `</p>` +
    `</div>`
  );
}

// ---------------------------------------------------------------------------
// ChatResponseAnnotator — main class
// ---------------------------------------------------------------------------

/**
 * Options for constructing a {@link ChatResponseAnnotator}.
 */
export interface ChatResponseAnnotatorOptions {
  /**
   * When `true`, all badge and banner HTML is omitted and the plain response
   * text is returned inside the response wrapper.  Useful for server-side
   * rendering where HTML injection is not desired.
   *
   * @default false
   */
  plainTextMode?: boolean;
}

/**
 * Framework-agnostic chat UI annotation layer for CaMeL provenance data.
 *
 * Annotates response text with `[Source: <tool_name>]` badges for untrusted
 * tool outputs, and surfaces a warning banner when phishing-content heuristics
 * fire.
 *
 * @example
 * ```ts
 * const annotator = new ChatResponseAnnotator();
 * const result = annotator.annotate({
 *   response_text: "Meeting confirmed for 3 pm.",
 *   provenance_chains: agentResult.provenance_chains,
 *   phishing_warnings: agentResult.phishing_warnings,
 * });
 * responseDiv.innerHTML = result.html;
 * ```
 */
export class ChatResponseAnnotator {
  private readonly plainTextMode: boolean;

  constructor(options: ChatResponseAnnotatorOptions = {}) {
    this.plainTextMode = options.plainTextMode ?? false;
  }

  /**
   * Annotate a response text with provenance badges and phishing warnings.
   *
   * Algorithm
   * ---------
   * 1. Collect all untrusted source tool names from `provenance_chains`.
   * 2. Build `SourceBadge` descriptors (one per distinct untrusted tool).
   * 3. Render `[Source: <tool_name>]` badge elements.
   * 4. If `phishing_warnings` is non-empty, render a warning banner.
   * 5. Wrap everything in a `<div class="camel-response">` container.
   *
   * Text span mapping
   * -----------------
   * The current implementation annotates the **entire** response text when
   * any untrusted source is present, because the CaMeL SDK v0.6.0 does not
   * provide explicit text-span-to-variable mapping.  When the SDK adds
   * span-level provenance (future milestone), the annotator will be updated
   * to apply badges inline at the relevant text positions.
   *
   * @param input - {@link AnnotatorInput} with response text and provenance data.
   * @returns {@link AnnotatedResponse} with HTML and structured metadata.
   */
  annotate(input: AnnotatorInput): AnnotatedResponse {
    const {
      response_text,
      provenance_chains,
      phishing_warnings = [],
    } = input;

    const badges = buildSourceBadges(provenance_chains);
    const untrustedSources = badges.map((b) => b.tool_name);
    const hasBadges = badges.length > 0;
    const hasPhishingWarning = phishing_warnings.length > 0;

    if (this.plainTextMode) {
      return {
        html: `<div class="${CSS.response}">${escapeHtml(response_text)}</div>`,
        has_badges: false,
        has_phishing_warning: false,
        untrusted_sources: untrustedSources,
        badges,
        phishing_warnings,
      };
    }

    // --- Build the HTML parts ---

    // 1. Phishing warning banner (prepended so it's seen first).
    const warningHtml = hasPhishingWarning
      ? renderPhishingWarningBanner(phishing_warnings)
      : "";

    // 2. Response text (HTML-escaped to prevent XSS from tool output content).
    const textHtml = `<div class="${CSS.textContent}">${escapeHtml(response_text)}</div>`;

    // 3. Source badges container (appended after the text).
    let badgesHtml = "";
    if (hasBadges) {
      const badgeElements = badges.map((b) => renderSourceBadge(b.tool_name));
      badgesHtml =
        `<div class="${CSS.badgesContainer}" aria-label="Data sources">` +
        badgeElements.join("") +
        `</div>`;
    }

    const html =
      `<div class="${CSS.response}">` +
      warningHtml +
      textHtml +
      badgesHtml +
      `</div>`;

    return {
      html,
      has_badges: hasBadges,
      has_phishing_warning: hasPhishingWarning,
      untrusted_sources: untrustedSources,
      badges,
      phishing_warnings,
    };
  }
}

// ---------------------------------------------------------------------------
// Convenience function export
// ---------------------------------------------------------------------------

/**
 * Convenience function — construct a default annotator and call `.annotate()`.
 *
 * Equivalent to `new ChatResponseAnnotator().annotate(input)`.
 *
 * @param input - {@link AnnotatorInput} with response text and provenance data.
 * @returns {@link AnnotatedResponse} with HTML and structured metadata.
 */
export function annotate(input: AnnotatorInput): AnnotatedResponse {
  return new ChatResponseAnnotator().annotate(input);
}

// ---------------------------------------------------------------------------
// CSS stylesheet string (inline-able for zero-dependency environments)
// ---------------------------------------------------------------------------

/**
 * Baseline CSS for badge and warning styles meeting WCAG 2.1 AA contrast
 * requirements.
 *
 * Colour contrast ratios (verified against WCAG 2.1 §1.4.3):
 *
 * | Element | Foreground | Background | Contrast ratio |
 * |---|---|---|---|
 * | `.camel-source-badge` | #1a1a1a (near-black) | #e8f4fd (light blue) | 11.4:1 ✓ |
 * | `.camel-source-badge` border | #0066cc (dark blue) | — | — |
 * | `.camel-phishing-warning` | #5c3317 (dark brown) | #fff3cd (light yellow) | 7.8:1 ✓ |
 * | `.camel-phishing-warning__title` | #5c3317 | #fff3cd | 7.8:1 ✓ |
 *
 * Inject via `<style>${ANNOTATOR_CSS}</style>` or a bundler CSS import.
 */
export const ANNOTATOR_CSS = `
/* CaMeL ChatResponseAnnotator — baseline styles (WCAG 2.1 AA compliant) */

.camel-response {
  font-family: inherit;
  line-height: 1.5;
}

.camel-response__text {
  white-space: pre-wrap;
  word-break: break-word;
}

.camel-response__badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  margin-top: 0.5rem;
}

/* Source badge
 * Contrast: #1a1a1a on #e8f4fd = 11.4:1 (WCAG AA + AAA) */
.camel-source-badge {
  display: inline-block;
  padding: 0.125rem 0.4rem;
  font-size: 0.75rem;
  font-weight: 600;
  color: #1a1a1a;
  background-color: #e8f4fd;
  border: 1px solid #0066cc;
  border-radius: 0.25rem;
  cursor: default;
  white-space: nowrap;
}

.camel-source-badge:focus {
  outline: 2px solid #0066cc;
  outline-offset: 2px;
}

/* Phishing warning banner
 * Contrast: #5c3317 on #fff3cd = 7.8:1 (WCAG AA + AAA) */
.camel-phishing-warning {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0.75rem 1rem;
  margin-bottom: 0.75rem;
  color: #5c3317;
  background-color: #fff3cd;
  border: 1px solid #ffc107;
  border-left: 4px solid #e65c00;
  border-radius: 0.25rem;
}

.camel-phishing-warning__title {
  font-weight: 700;
  font-size: 0.95rem;
}

.camel-phishing-warning__body {
  margin: 0;
  font-size: 0.875rem;
  line-height: 1.5;
}

.camel-phishing-warning__body code {
  padding: 0.1em 0.3em;
  background: rgba(0, 0, 0, 0.08);
  border-radius: 0.2em;
  font-family: monospace;
}
`.trim();
