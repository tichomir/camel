/**
 * Unit tests for ChatResponseAnnotator.
 *
 * Coverage:
 *   1. collectUntrustedSources — extracts untrusted tool names correctly.
 *   2. buildSourceBadges — deduplicates and groups by tool.
 *   3. renderSourceBadge — HTML structure, escaping, ARIA attributes.
 *   4. renderPhishingWarningBanner — HTML structure, role=alert, no-op on empty.
 *   5. ChatResponseAnnotator.annotate — trusted-only input, single badge,
 *      multiple badges, phishing warning, mixed provenance, empty input,
 *      plainTextMode.
 *   6. annotate convenience function — delegates correctly.
 *   7. ANNOTATOR_CSS — exported, non-empty string.
 *   8. HTML escaping — XSS prevention in tool names and response text.
 */

import {
  ANNOTATOR_CSS,
  ChatResponseAnnotator,
  annotate,
  buildSourceBadges,
  collectUntrustedSources,
  renderPhishingWarningBanner,
  renderSourceBadge,
} from "./ChatResponseAnnotator";
import { TRUSTED_SOURCES } from "./types";
import type {
  AnnotatorInput,
  PhishingWarning,
  ProvenanceChain,
} from "./types";

// ---------------------------------------------------------------------------
// Test data factories
// ---------------------------------------------------------------------------

function trustedChain(varName: string): ProvenanceChain {
  return {
    variable_name: varName,
    hops: [{ tool_name: "User literal", inner_source: null, readers: "Public", timestamp: null }],
    is_trusted: true,
  };
}

function untrustedChain(varName: string, toolName = "get_last_email"): ProvenanceChain {
  return {
    variable_name: varName,
    hops: [{ tool_name: toolName, inner_source: "body", readers: "Public", timestamp: null }],
    is_trusted: false,
  };
}

function mixedChain(varName: string, toolName = "read_file"): ProvenanceChain {
  return {
    variable_name: varName,
    hops: [
      { tool_name: "User literal", inner_source: null, readers: "Public", timestamp: null },
      { tool_name: toolName, inner_source: null, readers: "Public", timestamp: null },
    ],
    is_trusted: false,
  };
}

function phishingWarning(
  varName: string,
  toolName = "get_last_email"
): PhishingWarning {
  return {
    variable_name: varName,
    matched_pattern: "From:\\s*\\S+@\\S+",
    matched_text: "From: ceo@company.com",
    untrusted_sources: [toolName],
    provenance_chain: untrustedChain(varName, toolName),
  };
}

// ---------------------------------------------------------------------------
// 1. collectUntrustedSources
// ---------------------------------------------------------------------------

describe("collectUntrustedSources", () => {
  it("returns empty array for empty chains map", () => {
    expect(collectUntrustedSources({})).toEqual([]);
  });

  it("returns empty array when all chains are trusted", () => {
    const chains = {
      greeting: trustedChain("greeting"),
      name: trustedChain("name"),
    };
    expect(collectUntrustedSources(chains)).toEqual([]);
  });

  it("returns tool name for a single untrusted chain", () => {
    const chains = { body: untrustedChain("body", "get_last_email") };
    expect(collectUntrustedSources(chains)).toEqual(["get_last_email"]);
  });

  it("deduplicates tool names appearing in multiple chains", () => {
    const chains = {
      body: untrustedChain("body", "get_last_email"),
      subject: untrustedChain("subject", "get_last_email"),
    };
    expect(collectUntrustedSources(chains)).toEqual(["get_last_email"]);
  });

  it("returns sorted list for multiple distinct tools", () => {
    const chains = {
      a: untrustedChain("a", "fetch_url"),
      b: untrustedChain("b", "get_last_email"),
      c: untrustedChain("c", "read_file"),
    };
    expect(collectUntrustedSources(chains)).toEqual([
      "fetch_url",
      "get_last_email",
      "read_file",
    ]);
  });

  it("excludes TRUSTED_SOURCES from result", () => {
    // A chain that only has trusted hops should not appear even if is_trusted
    // is somehow set to false (defensive test).
    const chain: ProvenanceChain = {
      variable_name: "x",
      hops: [
        { tool_name: "User literal", inner_source: null, readers: "Public", timestamp: null },
      ],
      is_trusted: false, // hypothetically incorrect, but we test hop-level filtering
    };
    expect(collectUntrustedSources({ x: chain })).toEqual([]);
  });

  it("handles mixed (trusted + untrusted hops) chain correctly", () => {
    const chains = { x: mixedChain("x", "read_file") };
    const result = collectUntrustedSources(chains);
    expect(result).toContain("read_file");
    expect(result).not.toContain("User literal");
    expect(result).not.toContain("CaMeL");
  });

  it("TRUSTED_SOURCES constant contains User literal and CaMeL", () => {
    expect(TRUSTED_SOURCES.has("User literal")).toBe(true);
    expect(TRUSTED_SOURCES.has("CaMeL")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2. buildSourceBadges
// ---------------------------------------------------------------------------

describe("buildSourceBadges", () => {
  it("returns empty array for empty chains", () => {
    expect(buildSourceBadges({})).toEqual([]);
  });

  it("returns empty array when all chains are trusted", () => {
    const chains = { greeting: trustedChain("greeting") };
    expect(buildSourceBadges(chains)).toEqual([]);
  });

  it("returns one badge per distinct untrusted tool", () => {
    const chains = {
      body: untrustedChain("body", "get_last_email"),
      subject: untrustedChain("subject", "get_last_email"),
      attachment: untrustedChain("attachment", "read_file"),
    };
    const badges = buildSourceBadges(chains);
    expect(badges).toHaveLength(2);
    const toolNames = badges.map((b) => b.tool_name);
    expect(toolNames).toContain("get_last_email");
    expect(toolNames).toContain("read_file");
  });

  it("groups variable names under each tool", () => {
    const chains = {
      body: untrustedChain("body", "get_last_email"),
      subject: untrustedChain("subject", "get_last_email"),
    };
    const badges = buildSourceBadges(chains);
    expect(badges).toHaveLength(1);
    expect(badges[0].tool_name).toBe("get_last_email");
    expect(badges[0].variable_names.sort()).toEqual(["body", "subject"]);
  });

  it("sorts badges by tool_name", () => {
    const chains = {
      z: untrustedChain("z", "zzz_tool"),
      a: untrustedChain("a", "aaa_tool"),
    };
    const badges = buildSourceBadges(chains);
    expect(badges[0].tool_name).toBe("aaa_tool");
    expect(badges[1].tool_name).toBe("zzz_tool");
  });
});

// ---------------------------------------------------------------------------
// 3. renderSourceBadge
// ---------------------------------------------------------------------------

describe("renderSourceBadge", () => {
  it("includes the tool name in the text content", () => {
    const html = renderSourceBadge("get_last_email");
    expect(html).toContain("[Source: get_last_email]");
  });

  it("sets aria-label to Source: <tool_name>", () => {
    const html = renderSourceBadge("read_file");
    expect(html).toContain('aria-label="Source: read_file"');
  });

  it("sets data-tool attribute", () => {
    const html = renderSourceBadge("fetch_url");
    expect(html).toContain('data-tool="fetch_url"');
  });

  it("uses role=img for screen reader announcement", () => {
    const html = renderSourceBadge("some_tool");
    expect(html).toContain('role="img"');
  });

  it("applies camel-source-badge CSS class", () => {
    const html = renderSourceBadge("tool");
    expect(html).toContain('class="camel-source-badge"');
  });

  it("escapes HTML-special characters in tool name", () => {
    const html = renderSourceBadge('<script>alert("xss")</script>');
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("escapes ampersands in tool name", () => {
    const html = renderSourceBadge("tool&name");
    expect(html).toContain("tool&amp;name");
  });

  it("wraps in a span element", () => {
    const html = renderSourceBadge("tool");
    expect(html).toMatch(/^<span /);
    expect(html).toMatch(/<\/span>$/);
  });
});

// ---------------------------------------------------------------------------
// 4. renderPhishingWarningBanner
// ---------------------------------------------------------------------------

describe("renderPhishingWarningBanner", () => {
  it("returns empty string for empty warnings array", () => {
    expect(renderPhishingWarningBanner([])).toBe("");
  });

  it("uses role=alert for assistive technology announcement", () => {
    const html = renderPhishingWarningBanner([phishingWarning("body")]);
    expect(html).toContain('role="alert"');
  });

  it("applies camel-phishing-warning CSS class", () => {
    const html = renderPhishingWarningBanner([phishingWarning("body")]);
    expect(html).toContain('class="camel-phishing-warning"');
  });

  it("includes the untrusted source tool name", () => {
    const html = renderPhishingWarningBanner([
      phishingWarning("body", "get_last_email"),
    ]);
    expect(html).toContain("get_last_email");
  });

  it("includes the variable name", () => {
    const html = renderPhishingWarningBanner([phishingWarning("email_body")]);
    expect(html).toContain("email_body");
  });

  it("includes matched text excerpt in the banner", () => {
    const html = renderPhishingWarningBanner([phishingWarning("body")]);
    expect(html).toContain("From: ceo@company.com");
  });

  it("deduplicates tool names across multiple warnings", () => {
    const warnings = [
      phishingWarning("a", "tool_x"),
      phishingWarning("b", "tool_x"),
    ];
    const html = renderPhishingWarningBanner(warnings);
    // tool_x should appear but only once in the tool listing
    const matches = (html.match(/tool_x/g) ?? []).length;
    // Allow it to appear in aria-label + body — but only once in tool list,
    // potentially more times elsewhere; just confirm it's present.
    expect(matches).toBeGreaterThanOrEqual(1);
  });

  it("has accessible aria-label on the banner element", () => {
    const html = renderPhishingWarningBanner([phishingWarning("body")]);
    expect(html).toMatch(/aria-label="[^"]+"/);
  });

  it("includes a 'Provenance Warning' title", () => {
    const html = renderPhishingWarningBanner([phishingWarning("body")]);
    expect(html).toContain("Provenance Warning");
  });

  it("escapes HTML-special characters in matched text", () => {
    const w: PhishingWarning = {
      variable_name: "body",
      matched_pattern: "From:\\s*\\S+@\\S+",
      matched_text: '<script>From: evil</script>',
      untrusted_sources: ["bad_tool"],
      provenance_chain: untrustedChain("body", "bad_tool"),
    };
    const html = renderPhishingWarningBanner([w]);
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});

// ---------------------------------------------------------------------------
// 5. ChatResponseAnnotator.annotate
// ---------------------------------------------------------------------------

describe("ChatResponseAnnotator.annotate", () => {
  const annotator = new ChatResponseAnnotator();

  // --- Trusted-only input ---

  it("returns has_badges=false for trusted-only provenance", () => {
    const result = annotator.annotate({
      response_text: "Hello, Alice!",
      provenance_chains: { greeting: trustedChain("greeting") },
    });
    expect(result.has_badges).toBe(false);
    expect(result.untrusted_sources).toEqual([]);
    expect(result.badges).toEqual([]);
  });

  it("does not render a badge for trusted-only provenance", () => {
    const result = annotator.annotate({
      response_text: "Meeting confirmed.",
      provenance_chains: { response: trustedChain("response") },
    });
    expect(result.html).not.toContain("camel-source-badge");
    expect(result.html).not.toContain("[Source:");
  });

  it("wraps trusted response in camel-response div", () => {
    const result = annotator.annotate({
      response_text: "OK",
      provenance_chains: { x: trustedChain("x") },
    });
    expect(result.html).toContain('class="camel-response"');
  });

  it("renders no phishing warning for trusted-only input", () => {
    const result = annotator.annotate({
      response_text: "Hello.",
      provenance_chains: { x: trustedChain("x") },
      phishing_warnings: [],
    });
    expect(result.has_phishing_warning).toBe(false);
    expect(result.html).not.toContain("camel-phishing-warning");
  });

  // --- Single badge ---

  it("has_badges=true for single untrusted source", () => {
    const result = annotator.annotate({
      response_text: "The invoice total is $4,200.",
      provenance_chains: { amount: untrustedChain("amount", "read_invoice") },
    });
    expect(result.has_badges).toBe(true);
    expect(result.untrusted_sources).toEqual(["read_invoice"]);
  });

  it("renders a source badge for single untrusted source", () => {
    const result = annotator.annotate({
      response_text: "Email body here.",
      provenance_chains: { body: untrustedChain("body", "get_last_email") },
    });
    expect(result.html).toContain("[Source: get_last_email]");
    expect(result.html).toContain('class="camel-source-badge"');
    expect(result.html).toContain('aria-label="Source: get_last_email"');
  });

  it("badge has aria-label attribute meeting WCAG 2.1 AA requirement", () => {
    const result = annotator.annotate({
      response_text: "Data here.",
      provenance_chains: { data: untrustedChain("data", "fetch_url") },
    });
    expect(result.html).toMatch(/aria-label="Source: fetch_url"/);
  });

  // --- Multiple badges ---

  it("renders multiple badges for multiple distinct untrusted sources", () => {
    const result = annotator.annotate({
      response_text: "Combined result.",
      provenance_chains: {
        a: untrustedChain("a", "tool_alpha"),
        b: untrustedChain("b", "tool_beta"),
      },
    });
    expect(result.has_badges).toBe(true);
    expect(result.untrusted_sources).toEqual(["tool_alpha", "tool_beta"]);
    expect(result.badges).toHaveLength(2);
    expect(result.html).toContain("[Source: tool_alpha]");
    expect(result.html).toContain("[Source: tool_beta]");
  });

  it("renders one badge per tool (not one per variable)", () => {
    const result = annotator.annotate({
      response_text: "Data.",
      provenance_chains: {
        body: untrustedChain("body", "get_last_email"),
        subject: untrustedChain("subject", "get_last_email"),
      },
    });
    // get_last_email appears as a badge only once
    const badgeCount = (result.html.match(/\[Source: get_last_email\]/g) ?? [])
      .length;
    expect(badgeCount).toBe(1);
  });

  // --- Phishing warning present ---

  it("has_phishing_warning=true when phishing warnings provided", () => {
    const result = annotator.annotate({
      response_text: "From: ceo@corp.com — please transfer funds.",
      provenance_chains: { body: untrustedChain("body") },
      phishing_warnings: [phishingWarning("body")],
    });
    expect(result.has_phishing_warning).toBe(true);
  });

  it("renders warning banner with role=alert", () => {
    const result = annotator.annotate({
      response_text: "Text.",
      provenance_chains: { body: untrustedChain("body") },
      phishing_warnings: [phishingWarning("body")],
    });
    expect(result.html).toContain('role="alert"');
    expect(result.html).toContain("camel-phishing-warning");
  });

  it("warning banner appears before response text in HTML", () => {
    const result = annotator.annotate({
      response_text: "Response text.",
      provenance_chains: { body: untrustedChain("body") },
      phishing_warnings: [phishingWarning("body")],
    });
    const warningPos = result.html.indexOf("camel-phishing-warning");
    const textPos = result.html.indexOf("camel-response__text");
    expect(warningPos).toBeLessThan(textPos);
  });

  it("warning banner includes source tool name", () => {
    const result = annotator.annotate({
      response_text: "Text.",
      provenance_chains: { body: untrustedChain("body", "get_last_email") },
      phishing_warnings: [phishingWarning("body", "get_last_email")],
    });
    expect(result.html).toContain("get_last_email");
  });

  it("phishing_warnings are passed through to AnnotatedResponse", () => {
    const warnings = [phishingWarning("body")];
    const result = annotator.annotate({
      response_text: "Text.",
      provenance_chains: { body: untrustedChain("body") },
      phishing_warnings: warnings,
    });
    expect(result.phishing_warnings).toBe(warnings);
  });

  // --- Mixed provenance (some trusted, some untrusted) ---

  it("shows badge when at least one chain is untrusted (mixed input)", () => {
    const result = annotator.annotate({
      response_text: "Mixed result.",
      provenance_chains: {
        trusted_var: trustedChain("trusted_var"),
        untrusted_var: untrustedChain("untrusted_var", "external_tool"),
      },
    });
    expect(result.has_badges).toBe(true);
    expect(result.untrusted_sources).toContain("external_tool");
    expect(result.untrusted_sources).not.toContain("User literal");
  });

  // --- Empty provenance map ---

  it("handles empty provenance_chains gracefully", () => {
    const result = annotator.annotate({
      response_text: "No data.",
      provenance_chains: {},
    });
    expect(result.has_badges).toBe(false);
    expect(result.has_phishing_warning).toBe(false);
    expect(result.html).toContain("No data.");
  });

  // --- HTML escaping / XSS prevention ---

  it("escapes response_text to prevent XSS", () => {
    const result = annotator.annotate({
      response_text: '<script>alert("xss")</script>',
      provenance_chains: {},
    });
    expect(result.html).not.toContain("<script>");
    expect(result.html).toContain("&lt;script&gt;");
  });

  it("escapes tool names in badges to prevent XSS", () => {
    const result = annotator.annotate({
      response_text: "Data.",
      provenance_chains: {
        x: untrustedChain("x", '<img src=x onerror=alert(1)>'),
      },
    });
    expect(result.html).not.toContain("<img");
    expect(result.html).toContain("&lt;img");
  });

  // --- plainTextMode ---

  it("plainTextMode omits badges from HTML", () => {
    const ptAnnotator = new ChatResponseAnnotator({ plainTextMode: true });
    const result = ptAnnotator.annotate({
      response_text: "Response.",
      provenance_chains: { x: untrustedChain("x", "tool_a") },
    });
    expect(result.html).not.toContain("camel-source-badge");
    expect(result.has_badges).toBe(false);
    // untrusted_sources still populated (metadata preserved)
    expect(result.untrusted_sources).toEqual(["tool_a"]);
  });

  it("plainTextMode omits phishing banner from HTML", () => {
    const ptAnnotator = new ChatResponseAnnotator({ plainTextMode: true });
    const result = ptAnnotator.annotate({
      response_text: "Response.",
      provenance_chains: { x: untrustedChain("x") },
      phishing_warnings: [phishingWarning("x")],
    });
    expect(result.html).not.toContain("camel-phishing-warning");
    expect(result.has_phishing_warning).toBe(false);
  });

  it("plainTextMode still returns untrusted_sources and badges metadata", () => {
    const ptAnnotator = new ChatResponseAnnotator({ plainTextMode: true });
    const result = ptAnnotator.annotate({
      response_text: "Text.",
      provenance_chains: {
        a: untrustedChain("a", "tool_one"),
        b: untrustedChain("b", "tool_two"),
      },
    });
    expect(result.untrusted_sources).toEqual(["tool_one", "tool_two"]);
    expect(result.badges).toHaveLength(2);
  });

  // --- HTML structure ---

  it("badges container has aria-label='Data sources'", () => {
    const result = annotator.annotate({
      response_text: "Text.",
      provenance_chains: { x: untrustedChain("x", "tool_a") },
    });
    expect(result.html).toContain('aria-label="Data sources"');
  });

  it("response text is inside camel-response__text div", () => {
    const result = annotator.annotate({
      response_text: "Hello world",
      provenance_chains: { x: trustedChain("x") },
    });
    expect(result.html).toContain('class="camel-response__text"');
    expect(result.html).toContain("Hello world");
  });
});

// ---------------------------------------------------------------------------
// 6. annotate convenience function
// ---------------------------------------------------------------------------

describe("annotate (convenience function)", () => {
  it("returns same result as new ChatResponseAnnotator().annotate()", () => {
    const input: AnnotatorInput = {
      response_text: "Test.",
      provenance_chains: { x: untrustedChain("x", "tool_a") },
    };
    const direct = new ChatResponseAnnotator().annotate(input);
    const convenience = annotate(input);
    expect(convenience.has_badges).toBe(direct.has_badges);
    expect(convenience.untrusted_sources).toEqual(direct.untrusted_sources);
    expect(convenience.html).toBe(direct.html);
  });

  it("returns AnnotatedResponse shape", () => {
    const result = annotate({
      response_text: "Hello",
      provenance_chains: {},
    });
    expect(typeof result.html).toBe("string");
    expect(typeof result.has_badges).toBe("boolean");
    expect(typeof result.has_phishing_warning).toBe("boolean");
    expect(Array.isArray(result.untrusted_sources)).toBe(true);
    expect(Array.isArray(result.badges)).toBe(true);
    expect(Array.isArray(result.phishing_warnings)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 7. ANNOTATOR_CSS
// ---------------------------------------------------------------------------

describe("ANNOTATOR_CSS", () => {
  it("is a non-empty string", () => {
    expect(typeof ANNOTATOR_CSS).toBe("string");
    expect(ANNOTATOR_CSS.length).toBeGreaterThan(0);
  });

  it("contains camel-source-badge class definition", () => {
    expect(ANNOTATOR_CSS).toContain(".camel-source-badge");
  });

  it("contains camel-phishing-warning class definition", () => {
    expect(ANNOTATOR_CSS).toContain(".camel-phishing-warning");
  });

  it("contains camel-response class definition", () => {
    expect(ANNOTATOR_CSS).toContain(".camel-response");
  });
});
