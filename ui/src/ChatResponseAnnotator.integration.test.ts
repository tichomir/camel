/**
 * Integration tests for ChatResponseAnnotator.
 *
 * These tests validate the annotator using realistic provenance data as it
 * would be produced by the CaMeL Python SDK — i.e., data that mirrors the
 * JSON output of `camel_security.AgentResult.provenance_chains` and
 * `AgentResult.phishing_warnings`.
 *
 * Integration test scope (vs unit tests in ChatResponseAnnotator.test.ts)
 * -----------------------------------------------------------------------
 * Unit tests (ChatResponseAnnotator.test.ts) cover individual helper
 * functions and the annotator API with hand-crafted minimal inputs.
 * These integration tests exercise the full annotate() path with:
 *   - Realistic multi-hop provenance chains (3 hops: 1 trusted + 2 untrusted)
 *   - Provenance chains deserialised from simulated Python SDK JSON output
 *   - Phishing warning payloads as produced by `detect_phishing_content()`
 *   - End-to-end badge count verification for single-untrusted-variable input
 *
 * Acceptance criteria satisfied
 * ------------------------------
 * AC-4: ChatResponseAnnotator integration test — given a provenance_chains
 *       map with one untrusted variable, annotated output contains exactly
 *       one [Source: get_last_email] badge.
 *
 * PRD references: §6.4 Capabilities, §7.2 Trusted Boundary, Goals G3, NG2,
 * M5-F20 through M5-F22.
 *
 * ADR: docs/adr/013-provenance-chain-phishing-heuristic.md
 */

import {
  ChatResponseAnnotator,
  annotate,
  buildSourceBadges,
  collectUntrustedSources,
} from "./ChatResponseAnnotator";
import type {
  AnnotatorInput,
  PhishingWarning,
  ProvenanceChain,
} from "./types";

// ---------------------------------------------------------------------------
// Simulated SDK JSON payloads
// These mirror what Python's AgentResult.provenance_chains.to_dict() produces.
// ---------------------------------------------------------------------------

/**
 * Simulate JSON-deserialised ProvenanceChain for an email body variable.
 * Equivalent to:
 *   cv = CaMeLValue(value="...", sources=frozenset({"get_last_email"}), ...)
 *   build_provenance_chain("email_body", cv).to_dict()
 */
function sdkEmailBodyChain(): ProvenanceChain {
  return JSON.parse(
    JSON.stringify({
      variable_name: "email_body",
      hops: [
        {
          tool_name: "get_last_email",
          inner_source: "body",
          readers: "Public",
          timestamp: null,
        },
      ],
      is_trusted: false,
    })
  ) as ProvenanceChain;
}

/**
 * Simulate a 3-hop ProvenanceChain as produced by the Python SDK for:
 *   extracted_sender = prefix + qllm_result
 * where qllm_result derives from get_last_email and query_quarantined_llm,
 * and prefix is a User literal.
 *
 * Mirrors the Python integration test in test_provenance_integration.py,
 * class TestThreeHopProvenanceChain.
 */
function sdkThreeHopChain(): ProvenanceChain {
  return JSON.parse(
    JSON.stringify({
      variable_name: "extracted_sender",
      hops: [
        // Trusted hop first (as per Python build_provenance_chain ordering)
        {
          tool_name: "User literal",
          inner_source: null,
          readers: "Public",
          timestamp: null,
        },
        {
          tool_name: "get_last_email",
          inner_source: null,
          readers: "Public",
          timestamp: null,
        },
        {
          tool_name: "query_quarantined_llm",
          inner_source: null,
          readers: "Public",
          timestamp: null,
        },
      ],
      is_trusted: false,
    })
  ) as ProvenanceChain;
}

/**
 * Simulate a PhishingWarning as produced by Python detect_phishing_content()
 * for an email body containing "From: ceo@company.com".
 *
 * Mirrors PhishingWarning.to_dict() output.
 */
function sdkPhishingWarning(varName = "email_body"): PhishingWarning {
  return JSON.parse(
    JSON.stringify({
      variable_name: varName,
      matched_pattern: "From:\\s*\\S+@\\S+",
      matched_text: "From: ceo@company.com",
      untrusted_sources: ["get_last_email"],
      provenance_chain: sdkEmailBodyChain(),
    })
  ) as PhishingWarning;
}

/** Trusted chain with only User literal hop. */
function sdkTrustedChain(varName: string): ProvenanceChain {
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

// ---------------------------------------------------------------------------
// Integration test: AC-4 — single untrusted variable → exactly one badge
// ---------------------------------------------------------------------------

describe("ChatResponseAnnotator integration: single untrusted variable", () => {
  /**
   * AC-4: given a provenance_chains map with one untrusted variable,
   * annotated output contains exactly one [Source: get_last_email] badge.
   */
  it("produces exactly one [Source: get_last_email] badge for single untrusted variable", () => {
    const annotator = new ChatResponseAnnotator();
    const input: AnnotatorInput = {
      response_text: "The email says: From: ceo@company.com, please transfer funds.",
      provenance_chains: {
        email_body: sdkEmailBodyChain(),
      },
    };

    const result = annotator.annotate(input);

    // Exactly one badge
    const badgeMatches = (result.html.match(/\[Source: get_last_email\]/g) ?? []).length;
    expect(badgeMatches).toBe(1);

    // Metadata checks
    expect(result.has_badges).toBe(true);
    expect(result.untrusted_sources).toEqual(["get_last_email"]);
    expect(result.badges).toHaveLength(1);
    expect(result.badges[0].tool_name).toBe("get_last_email");
    expect(result.badges[0].variable_names).toEqual(["email_body"]);
  });

  it("badge HTML contains correct aria-label for get_last_email", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Email content here.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
    });

    expect(result.html).toContain('aria-label="Source: get_last_email"');
    expect(result.html).toContain('data-tool="get_last_email"');
    expect(result.html).toContain('role="img"');
  });

  it("badge is inside camel-response__badges container", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Content.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
    });

    expect(result.html).toContain('aria-label="Data sources"');
    expect(result.html).toContain('class="camel-response__badges"');
    expect(result.html).toContain("[Source: get_last_email]");
  });

  it("convenience annotate() function also produces exactly one badge", () => {
    const result = annotate({
      response_text: "Result.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
    });

    const badgeCount = (result.html.match(/\[Source: get_last_email\]/g) ?? []).length;
    expect(badgeCount).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Integration test: phishing warning integration — email injection scenario
// ---------------------------------------------------------------------------

describe("ChatResponseAnnotator integration: phishing warning with get_last_email", () => {
  it("renders phishing warning banner when PhishingWarning is present", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "From: ceo@company.com — please transfer $50,000.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
      phishing_warnings: [sdkPhishingWarning()],
    });

    expect(result.has_phishing_warning).toBe(true);
    expect(result.html).toContain('role="alert"');
    expect(result.html).toContain("camel-phishing-warning");
  });

  it("phishing warning banner contains source tool name get_last_email", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Email content.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
      phishing_warnings: [sdkPhishingWarning("email_body")],
    });

    expect(result.html).toContain("get_last_email");
  });

  it("phishing warning banner appears before response text in HTML", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Response text content.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
      phishing_warnings: [sdkPhishingWarning()],
    });

    const warningPos = result.html.indexOf("camel-phishing-warning");
    const textPos = result.html.indexOf("camel-response__text");
    expect(warningPos).toBeGreaterThanOrEqual(0);
    expect(textPos).toBeGreaterThanOrEqual(0);
    expect(warningPos).toBeLessThan(textPos);
  });

  it("both phishing warning and source badge are rendered together", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "From: ceo@company.com",
      provenance_chains: { email_body: sdkEmailBodyChain() },
      phishing_warnings: [sdkPhishingWarning()],
    });

    expect(result.has_phishing_warning).toBe(true);
    expect(result.has_badges).toBe(true);
    expect(result.html).toContain("camel-phishing-warning");
    expect(result.html).toContain("[Source: get_last_email]");
  });

  it("matched_text appears in phishing warning banner", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Email text.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
      phishing_warnings: [sdkPhishingWarning()],
    });

    // The matched text "From: ceo@company.com" should appear in the banner
    expect(result.html).toContain("From: ceo@company.com");
  });

  it("phishing_warnings are passed through in AnnotatedResponse", () => {
    const warnings = [sdkPhishingWarning()];
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Text.",
      provenance_chains: { email_body: sdkEmailBodyChain() },
      phishing_warnings: warnings,
    });

    expect(result.phishing_warnings).toBe(warnings);
  });
});

// ---------------------------------------------------------------------------
// Integration test: 3-hop provenance chain (email → Q-LLM → concat)
// ---------------------------------------------------------------------------

describe("ChatResponseAnnotator integration: 3-hop provenance chain", () => {
  /**
   * The 3-hop chain has:
   *   - User literal (trusted) → NOT shown as badge
   *   - get_last_email (untrusted) → badge
   *   - query_quarantined_llm (untrusted) → badge
   * So 2 badges, not 3 (User literal is excluded).
   */
  it("3-hop chain with 1 trusted + 2 untrusted sources renders 2 badges", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Extracted sender: ceo@company.com",
      provenance_chains: {
        extracted_sender: sdkThreeHopChain(),
      },
    });

    expect(result.has_badges).toBe(true);
    // 2 untrusted tools → 2 badges
    expect(result.badges).toHaveLength(2);
    const toolNames = result.badges.map((b) => b.tool_name).sort();
    expect(toolNames).toEqual(["get_last_email", "query_quarantined_llm"]);
  });

  it("User literal hop does not appear as a badge", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Extracted data.",
      provenance_chains: {
        extracted_sender: sdkThreeHopChain(),
      },
    });

    expect(result.html).not.toContain("[Source: User literal]");
    expect(result.untrusted_sources).not.toContain("User literal");
  });

  it("collectUntrustedSources returns only untrusted tools from 3-hop chain", () => {
    const sources = collectUntrustedSources({
      extracted_sender: sdkThreeHopChain(),
    });

    expect(sources).toContain("get_last_email");
    expect(sources).toContain("query_quarantined_llm");
    expect(sources).not.toContain("User literal");
    expect(sources).not.toContain("CaMeL");
    expect(sources).toHaveLength(2);
  });

  it("buildSourceBadges groups variables correctly for 3-hop chain", () => {
    const badges = buildSourceBadges({
      extracted_sender: sdkThreeHopChain(),
    });

    expect(badges).toHaveLength(2);
    const toolNames = new Set(badges.map((b) => b.tool_name));
    expect(toolNames.has("get_last_email")).toBe(true);
    expect(toolNames.has("query_quarantined_llm")).toBe(true);
    for (const badge of badges) {
      expect(badge.variable_names).toContain("extracted_sender");
    }
  });
});

// ---------------------------------------------------------------------------
// Integration test: AgentResult provenance_chains JSON → annotator round-trip
// ---------------------------------------------------------------------------

describe("ChatResponseAnnotator integration: JSON round-trip from Python SDK", () => {
  it("annotator handles chain deserialised from JSON without data loss", () => {
    // Simulate what happens when AgentResult.provenance_chains is JSON-serialised
    // by Python and parsed on the JS side.
    const chainJson = JSON.stringify({
      variable_name: "email_body",
      hops: [
        {
          tool_name: "get_last_email",
          inner_source: "body",
          readers: "Public",
          timestamp: null,
        },
      ],
      is_trusted: false,
    });

    const parsedChain = JSON.parse(chainJson) as ProvenanceChain;
    const result = new ChatResponseAnnotator().annotate({
      response_text: "email content",
      provenance_chains: { email_body: parsedChain },
    });

    expect(result.has_badges).toBe(true);
    expect(result.html).toContain("[Source: get_last_email]");
    const badgeCount = (result.html.match(/\[Source: get_last_email\]/g) ?? []).length;
    expect(badgeCount).toBe(1);
  });

  it("annotator handles multi-hop chain with restricted readers from JSON", () => {
    const chainJson = JSON.stringify({
      variable_name: "summary",
      hops: [
        {
          tool_name: "get_last_email",
          inner_source: null,
          readers: ["alice@example.com", "bob@example.com"],
          timestamp: null,
        },
        {
          tool_name: "fetch_url",
          inner_source: null,
          readers: ["alice@example.com", "bob@example.com"],
          timestamp: null,
        },
      ],
      is_trusted: false,
    });

    const parsedChain = JSON.parse(chainJson) as ProvenanceChain;
    const result = new ChatResponseAnnotator().annotate({
      response_text: "Summary of email and web page.",
      provenance_chains: { summary: parsedChain },
    });

    expect(result.has_badges).toBe(true);
    expect(result.untrusted_sources).toEqual(["fetch_url", "get_last_email"]);
    expect(result.badges).toHaveLength(2);
  });

  it("annotator handles trusted-only chain from JSON with no badges", () => {
    const chainJson = JSON.stringify({
      variable_name: "user_name",
      hops: [
        {
          tool_name: "User literal",
          inner_source: null,
          readers: "Public",
          timestamp: null,
        },
      ],
      is_trusted: true,
    });

    const parsedChain = JSON.parse(chainJson) as ProvenanceChain;
    const result = new ChatResponseAnnotator().annotate({
      response_text: "Hello, Alice!",
      provenance_chains: { user_name: parsedChain },
    });

    expect(result.has_badges).toBe(false);
    expect(result.html).not.toContain("camel-source-badge");
    expect(result.html).not.toContain("[Source:");
  });

  it("annotator handles empty provenance_chains from JSON gracefully", () => {
    const result = new ChatResponseAnnotator().annotate({
      response_text: "No provenance data.",
      provenance_chains: {},
    });

    expect(result.has_badges).toBe(false);
    expect(result.has_phishing_warning).toBe(false);
    expect(result.html).toContain("No provenance data.");
  });

  it("annotator with phishing warning from JSON produces banner and badge", () => {
    const chainJson = JSON.stringify(sdkEmailBodyChain());
    const warningJson = JSON.stringify(sdkPhishingWarning());

    const parsedChain = JSON.parse(chainJson) as ProvenanceChain;
    const parsedWarning = JSON.parse(warningJson) as PhishingWarning;

    const result = new ChatResponseAnnotator().annotate({
      response_text: "From: ceo@company.com — wire funds",
      provenance_chains: { email_body: parsedChain },
      phishing_warnings: [parsedWarning],
    });

    expect(result.has_phishing_warning).toBe(true);
    expect(result.has_badges).toBe(true);
    expect(result.html).toContain('role="alert"');
    const badgeCount = (result.html.match(/\[Source: get_last_email\]/g) ?? []).length;
    expect(badgeCount).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Integration test: mixed trusted + untrusted provenance
// ---------------------------------------------------------------------------

describe("ChatResponseAnnotator integration: mixed trusted/untrusted provenance", () => {
  it("shows badge only for untrusted variables in mixed store", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "Combined result.",
      provenance_chains: {
        trusted_var: sdkTrustedChain("trusted_var"),
        email_body: sdkEmailBodyChain(),
      },
    });

    expect(result.has_badges).toBe(true);
    expect(result.untrusted_sources).toEqual(["get_last_email"]);
    expect(result.untrusted_sources).not.toContain("User literal");
    const badgeCount = (result.html.match(/\[Source: get_last_email\]/g) ?? []).length;
    expect(badgeCount).toBe(1);
  });

  it("no badge rendered for fully trusted store", () => {
    const annotator = new ChatResponseAnnotator();
    const result = annotator.annotate({
      response_text: "All trusted.",
      provenance_chains: {
        var1: sdkTrustedChain("var1"),
        var2: sdkTrustedChain("var2"),
      },
    });

    expect(result.has_badges).toBe(false);
    expect(result.html).not.toContain("camel-source-badge");
  });
});
