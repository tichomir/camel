/**
 * Storybook stories for ChatResponseAnnotator.
 *
 * Written in Component Story Format (CSF) 3.0, compatible with
 * `@storybook/html` (framework-agnostic HTML stories).
 *
 * Stories
 * -------
 * 1. NoUntrustedSources — response from trusted sources only, no badges.
 * 2. SingleBadge — one untrusted tool, one badge.
 * 3. MultipleBadges — two untrusted tools, two badges.
 * 4. PhishingWarningPresent — warning banner with single phishing warning.
 * 5. PhishingWarningMultipleSources — banner with two untrusted sources.
 * 6. MixedProvenanceSingleTool — trusted + untrusted hops, single tool badge.
 * 7. EmptyProvenanceChains — no chains provided, plain response text.
 *
 * Each story renders the annotated HTML via the built-in ANNOTATOR_CSS so
 * the visual result is self-contained in the Storybook canvas.
 *
 * Setup
 * -----
 * Install Storybook in the ui/ directory:
 *   npx storybook@latest init --type html
 *
 * Then run:
 *   npm run storybook
 */

import type { Meta, StoryObj } from "@storybook/html";
import {
  ANNOTATOR_CSS,
  ChatResponseAnnotator,
} from "./ChatResponseAnnotator";
import type { PhishingWarning, ProvenanceChain } from "./types";

// ---------------------------------------------------------------------------
// Story helpers / shared data
// ---------------------------------------------------------------------------

function trustedChain(varName: string): ProvenanceChain {
  return {
    variable_name: varName,
    hops: [
      {
        tool_name: "User literal",
        inner_source: null,
        readers: "Public",
        timestamp: null,
      },
    ],
    is_trusted: true,
  };
}

function untrustedChain(
  varName: string,
  toolName: string,
  innerSource: string | null = null
): ProvenanceChain {
  return {
    variable_name: varName,
    hops: [
      {
        tool_name: toolName,
        inner_source: innerSource,
        readers: "Public",
        timestamp: null,
      },
    ],
    is_trusted: false,
  };
}

function mixedChain(
  varName: string,
  toolName: string
): ProvenanceChain {
  return {
    variable_name: varName,
    hops: [
      {
        tool_name: "User literal",
        inner_source: null,
        readers: "Public",
        timestamp: null,
      },
      {
        tool_name: toolName,
        inner_source: null,
        readers: "Public",
        timestamp: null,
      },
    ],
    is_trusted: false,
  };
}

function makePhishingWarning(
  varName: string,
  toolName: string,
  matchedText = "From: ceo@company.com"
): PhishingWarning {
  return {
    variable_name: varName,
    matched_pattern: "From:\\s*\\S+@\\S+",
    matched_text: matchedText,
    untrusted_sources: [toolName],
    provenance_chain: untrustedChain(varName, toolName),
  };
}

/** Wrap annotated HTML with the bundled stylesheet for self-contained preview. */
function withStyles(html: string): string {
  return `<style>${ANNOTATOR_CSS}</style>\n${html}`;
}

const annotator = new ChatResponseAnnotator();

// ---------------------------------------------------------------------------
// Meta
// ---------------------------------------------------------------------------

const meta: Meta = {
  title: "CaMeL / ChatResponseAnnotator",
  render: (args) => args.html as string,
};

export default meta;

type Story = StoryObj;

// ---------------------------------------------------------------------------
// Story 1 — NoUntrustedSources
// ---------------------------------------------------------------------------

/**
 * All variables in the response originate from trusted sources
 * (`"User literal"` or `"CaMeL"`).  No badges are rendered.
 *
 * This is the baseline / happy-path story demonstrating that trusted responses
 * look identical to a plain response with no visual noise.
 */
export const NoUntrustedSources: Story = {
  name: "No Untrusted Sources",
  args: (() => {
    const result = annotator.annotate({
      response_text:
        "Good morning! Your meeting with Alice is confirmed for 3:00 PM today.",
      provenance_chains: {
        greeting: trustedChain("greeting"),
        meeting_time: trustedChain("meeting_time"),
        attendee: trustedChain("attendee"),
      },
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "All variables originate from trusted sources. No source badges or warnings are rendered.",
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Story 2 — SingleBadge
// ---------------------------------------------------------------------------

/**
 * One untrusted tool source — a `[Source: get_last_email]` badge is rendered
 * below the response text.  Demonstrates single-badge rendering and the
 * light-blue badge style.
 */
export const SingleBadge: Story = {
  name: "Single Source Badge",
  args: (() => {
    const result = annotator.annotate({
      response_text:
        "The email subject is: \"Q1 Budget Review — Action Required\". " +
        "It was sent by finance@company.com.",
      provenance_chains: {
        email_subject: untrustedChain("email_subject", "get_last_email", "subject"),
        email_sender: untrustedChain("email_sender", "get_last_email", "sender"),
      },
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "Response contains data from `get_last_email`. A single source badge " +
          "is displayed below the response text.",
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Story 3 — MultipleBadges
// ---------------------------------------------------------------------------

/**
 * Two distinct untrusted tool sources — badges for both `get_last_email` and
 * `read_file` are rendered.  Demonstrates multi-badge layout and grouping.
 */
export const MultipleBadges: Story = {
  name: "Multiple Source Badges",
  args: (() => {
    const result = annotator.annotate({
      response_text:
        "The invoice (from the attached PDF) references the vendor " +
        "mentioned in the latest email. Total: $12,450.00. " +
        "Please confirm with the finance team before approving.",
      provenance_chains: {
        invoice_total: untrustedChain("invoice_total", "read_file", "content"),
        vendor_name: untrustedChain("vendor_name", "get_last_email", "body"),
        user_instruction: trustedChain("user_instruction"),
      },
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "Response contains data from `read_file` and `get_last_email`. " +
          "Two source badges are shown — one per distinct tool.",
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Story 4 — PhishingWarningPresent
// ---------------------------------------------------------------------------

/**
 * A phishing-content heuristic has fired: the response contains text claiming
 * a trusted sender identity while the data originated from `get_last_email`.
 *
 * A yellow warning banner is prepended above the response text, with
 * `role="alert"` so screen readers announce it immediately.
 */
export const PhishingWarningPresent: Story = {
  name: "Phishing Warning",
  args: (() => {
    const result = annotator.annotate({
      response_text:
        "From: ceo@company.com — Hi, this is urgent. Please wire $50,000 to " +
        "account 123-456-789 immediately. Do not tell anyone.",
      provenance_chains: {
        email_body: untrustedChain("email_body", "get_last_email", "body"),
      },
      phishing_warnings: [
        makePhishingWarning("email_body", "get_last_email", "From: ceo@company.com"),
      ],
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "The phishing-content heuristic fired on `email_body` (matched " +
          "`From: ceo@company.com` while the data came from `get_last_email`). " +
          "A yellow warning banner with `role=alert` is prepended to the response.",
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Story 5 — PhishingWarningMultipleSources
// ---------------------------------------------------------------------------

/**
 * Phishing warning fires with two distinct untrusted source tools.  Demonstrates
 * that the banner deduplicates and lists both tool names.
 */
export const PhishingWarningMultipleSources: Story = {
  name: "Phishing Warning — Multiple Sources",
  args: (() => {
    const result = annotator.annotate({
      response_text:
        "I am Alice, your assistant. Message from Bob: please update your " +
        "credentials via the link below.",
      provenance_chains: {
        identity_claim: untrustedChain("identity_claim", "get_last_email", "body"),
        link_text: untrustedChain("link_text", "fetch_url", "content"),
      },
      phishing_warnings: [
        makePhishingWarning("identity_claim", "get_last_email", "I am Alice"),
        {
          variable_name: "link_text",
          matched_pattern: "\\bMessage\\s+from\\s+\\w+",
          matched_text: "Message from Bob",
          untrusted_sources: ["fetch_url"],
          provenance_chain: untrustedChain("link_text", "fetch_url", "content"),
        },
      ],
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "Two phishing patterns fired across two untrusted tools (`get_last_email` " +
          "and `fetch_url`). The banner lists both tool names and both variable names.",
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Story 6 — MixedProvenanceSingleTool
// ---------------------------------------------------------------------------

/**
 * A variable has both trusted (`"User literal"`) and untrusted (`read_file`)
 * hops — the chain is therefore untrusted.  A single badge for `read_file`
 * is rendered; no badge for `"User literal"`.
 */
export const MixedProvenanceSingleTool: Story = {
  name: "Mixed Provenance — Single Tool Badge",
  args: (() => {
    const result = annotator.annotate({
      response_text:
        "The report summary (combining your notes and the uploaded document) " +
        "is ready for review.",
      provenance_chains: {
        summary: mixedChain("summary", "read_file"),
        user_note: trustedChain("user_note"),
      },
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "`summary` has both `User literal` and `read_file` in its provenance. " +
          "The chain is untrusted, so a badge for `read_file` is shown. " +
          "`user_note` is fully trusted — no badge.",
      },
    },
  },
};

// ---------------------------------------------------------------------------
// Story 7 — EmptyProvenanceChains
// ---------------------------------------------------------------------------

/**
 * No provenance chains are provided (e.g. the agent run failed or the
 * `provenance_chains` field is empty).  The annotator renders the plain
 * response text with no badges or warnings.
 */
export const EmptyProvenanceChains: Story = {
  name: "Empty Provenance Chains",
  args: (() => {
    const result = annotator.annotate({
      response_text: "Task completed successfully.",
      provenance_chains: {},
    });
    return { html: withStyles(result.html) };
  })(),
  parameters: {
    docs: {
      description: {
        story:
          "No provenance chains provided. The response is rendered as-is " +
          "with no badges or warnings.",
      },
    },
  },
};
