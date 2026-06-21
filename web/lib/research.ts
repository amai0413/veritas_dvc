// Agentic research: turn a bare claim into REAL retrieved evidence.
//   claim -> Claude writes search queries -> we search the web for result links
//         -> Browserbase scrapes the top pages -> real excerpts for the verdict.
// This is the "connect the two" step a mentor suggested: instead of the user
// pasting one URL, the agent finds the links itself and reads them.
//
// Search uses the DuckDuckGo HTML endpoint (no API key, server-renderable). The
// heavy lifting — getting past bot walls on Reddit/X/news — is done by the
// existing Browserbase scraper in fetchArticle.

import type Anthropic from "@anthropic-ai/sdk";
import { JSDOM } from "jsdom";
import { MODEL } from "./verdict";
import { fetchArticle, type FetchedArticle } from "./fetchArticle";

export type SearchResult = { title: string; url: string };
export type Evidence = FetchedArticle & { query: string; excerpt: string };

const SEARCH_TIMEOUT_MS = 12000;
const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

const QUERY_SCHEMA = {
  type: "object",
  properties: {
    queries: {
      type: "array",
      description: "3-4 web search queries that would surface evidence for AND against the claim.",
      items: { type: "string" },
    },
  },
  required: ["queries"],
  additionalProperties: false,
} as const;

const QUERY_SYSTEM = `You turn a claim into web-search queries that will surface real evidence to fact-check it. Return 3-4 short queries covering different angles: the claim itself, the opposite/debunk angle, an authoritative-source angle, and a community-discussion angle (append "reddit" to one). Keep each query under 8 words. Do not answer the claim — only produce search queries.`;

// Ask Claude for good search queries (with a deterministic fallback).
export async function generateQueries(client: Anthropic, claim: string): Promise<string[]> {
  try {
    const r = await client.messages.create({
      model: MODEL,
      max_tokens: 400,
      output_config: { effort: "low", format: { type: "json_schema", schema: QUERY_SCHEMA } },
      system: QUERY_SYSTEM,
      messages: [{ role: "user", content: `Claim: "${claim}"` }],
    });
    const tb = r.content.find((b) => b.type === "text");
    const raw = tb && tb.type === "text" ? JSON.parse(tb.text) : {};
    const qs = Array.isArray(raw.queries) ? raw.queries.map(String).filter(Boolean) : [];
    if (qs.length) return qs.slice(0, 4);
  } catch {
    /* fall through to heuristic */
  }
  const base = claim.replace(/[?.!]+$/, "");
  return [base, `${base} fact check`, `${base} reddit`, `${base} debunked`];
}

// Decode a DuckDuckGo redirect href (//duckduckgo.com/l/?uddg=<encoded>) to the
// real destination URL; pass real URLs through unchanged.
function decodeDdgHref(href: string): string | null {
  if (!href) return null;
  let h = href.startsWith("//") ? "https:" + href : href;
  try {
    const u = new URL(h, "https://duckduckgo.com");
    if (u.hostname.endsWith("duckduckgo.com") && u.pathname.startsWith("/l/")) {
      const dest = u.searchParams.get("uddg");
      return dest ? decodeURIComponent(dest) : null;
    }
    if (u.protocol === "http:" || u.protocol === "https:") return u.toString();
  } catch {
    /* ignore */
  }
  return null;
}

function parseResults(html: string, limit: number): SearchResult[] {
  const dom = new JSDOM(html);
  const out: SearchResult[] = [];
  const seen = new Set<string>();
  for (const a of Array.from(dom.window.document.querySelectorAll("a.result__a"))) {
    const url = decodeDdgHref(a.getAttribute("href") ?? "");
    if (!url) continue;
    const host = (() => {
      try {
        return new URL(url).hostname;
      } catch {
        return "";
      }
    })();
    if (!host || seen.has(host)) continue; // one result per domain for diversity
    seen.add(host);
    out.push({ title: (a.textContent ?? "").trim() || url, url });
    if (out.length >= limit) break;
  }
  return out;
}

async function ddgFetchHtml(query: string): Promise<string | null> {
  const endpoint = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SEARCH_TIMEOUT_MS);
  try {
    const res = await fetch(endpoint, {
      signal: controller.signal,
      headers: { "User-Agent": UA, Accept: "text/html" },
    });
    return res.ok ? await res.text() : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Render the DDG results page in a real browser when the direct endpoint is
// rate-limited. Also satisfies "search through Browserbase". Env-gated.
async function ddgViaBrowserbase(query: string): Promise<string | null> {
  if (!process.env.BROWSERBASE_API_KEY || !process.env.BROWSERBASE_PROJECT_ID) return null;
  try {
    const { chromium } = await import("playwright-core");
    const Browserbase = (await import("@browserbasehq/sdk")).default;
    const bb = new Browserbase({ apiKey: process.env.BROWSERBASE_API_KEY });
    const session = await bb.sessions.create({
      projectId: process.env.BROWSERBASE_PROJECT_ID,
      ...(process.env.BROWSERBASE_PROXIES === "true" ? { proxies: true } : {}),
      browserSettings: { solveCaptchas: true },
    });
    const browser = await chromium.connectOverCDP(session.connectUrl);
    try {
      const ctx = browser.contexts()[0] ?? (await browser.newContext());
      const page = ctx.pages()[0] ?? (await ctx.newPage());
      await page.goto(`https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`, {
        waitUntil: "domcontentloaded",
        timeout: 45000,
      });
      await page.waitForTimeout(2000);
      return await page.content();
    } finally {
      await browser.close().catch(() => {});
    }
  } catch {
    return null;
  }
}

// One web search -> result links. Tries the direct HTML endpoint (with one retry
// for transient rate-limits), then falls back to rendering it via Browserbase.
export async function searchLinks(query: string, limit = 4): Promise<SearchResult[]> {
  let html = await ddgFetchHtml(query);
  let results = html ? parseResults(html, limit) : [];
  if (!results.length) {
    await new Promise((r) => setTimeout(r, 600));
    html = await ddgFetchHtml(query);
    results = html ? parseResults(html, limit) : [];
  }
  if (!results.length) {
    html = await ddgViaBrowserbase(query);
    results = html ? parseResults(html, limit) : [];
  }
  return results;
}

// Full pipeline: queries -> search -> dedupe links -> scrape top N pages.
// onStep lets the caller wrap each phase in an Arize span.
export async function gatherEvidence(
  client: Anthropic,
  claim: string,
  opts: {
    maxPages?: number;
    onQueries?: (queries: string[]) => void;
    onSearch?: (query: string, results: SearchResult[]) => void;
    onScrape?: (url: string, ok: boolean) => void;
  } = {},
): Promise<{ queries: string[]; results: SearchResult[]; evidence: Evidence[] }> {
  const maxPages = opts.maxPages ?? 4;
  const queries = await generateQueries(client, claim);
  opts.onQueries?.(queries);

  // Search every query, collect unique links (one per domain).
  const linkByUrl = new Map<string, SearchResult & { query: string }>();
  for (const q of queries) {
    const results = await searchLinks(q, 4);
    opts.onSearch?.(q, results);
    await new Promise((r) => setTimeout(r, 400)); // space out searches (rate-limit friendly)
    for (const r of results) {
      const host = (() => {
        try {
          return new URL(r.url).hostname.replace(/^www\./, "");
        } catch {
          return r.url;
        }
      })();
      if (![...linkByUrl.values()].some((x) => x.url.includes(host))) {
        linkByUrl.set(r.url, { ...r, query: q });
      }
    }
  }

  const results = [...linkByUrl.values()];
  // Scrape the top pages concurrently; keep whatever succeeds.
  const scraped = await Promise.all(
    results.slice(0, maxPages).map(async (r) => {
      try {
        const art = await fetchArticle(r.url);
        opts.onScrape?.(r.url, true);
        return { ...art, query: r.query, excerpt: art.text.slice(0, 1200) } as Evidence;
      } catch {
        opts.onScrape?.(r.url, false);
        return null;
      }
    }),
  );
  const evidence = scraped.filter((e): e is Evidence => e !== null);
  return { queries, results, evidence };
}
