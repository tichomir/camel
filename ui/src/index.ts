/**
 * @camel-security/ui — public module index.
 *
 * Re-exports all public types and the ChatResponseAnnotator class so
 * consumers can import from a single entry point:
 *
 * ```ts
 * import { ChatResponseAnnotator, annotate, ANNOTATOR_CSS } from '@camel-security/ui';
 * import type { ProvenanceChain, PhishingWarning, AnnotatedResponse } from '@camel-security/ui';
 * ```
 */

export {
  ChatResponseAnnotator,
  annotate,
  annotate as annotateChatResponse,
  buildSourceBadges,
  collectUntrustedSources,
  renderPhishingWarningBanner,
  renderSourceBadge,
  ANNOTATOR_CSS,
} from "./ChatResponseAnnotator";

export type {
  ChatResponseAnnotatorOptions,
} from "./ChatResponseAnnotator";

export {
  TRUSTED_SOURCES,
} from "./types";

export type {
  AnnotatedResponse,
  AnnotatorInput,
  PhishingWarning,
  ProvenanceChain,
  ProvenanceHop,
  SourceBadge,
} from "./types";
