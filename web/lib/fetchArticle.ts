// Fetch a URL and extract its main article text (the "kill the copy-paste" step).
// Runs server-side only. Mozilla Readability (the Firefox Reader engine) does the
// extraction. Strategy:
//   1. cheap direct fetch of the raw HTML, and
//   2. if that's blocked/empty (Reddit, X, JS-rendered SPAs) and Browserbase is
//      configured, render the page in a real headless browser and extract that.
// Browserbase is env-gated (BROWSERBASE_API_KEY + BROWSERBASE_PROJECT_ID); with no
// keys the behavior is exactly the direct-fetch-only path.

import { JSDOM } from "jsdom";
import { Readability } from "@mozilla/readability";

export type FetchedArticle = {
  title: string;
  text: string;
  url: string; // final URL after redirects
  siteName?: string;
};

export class ArticleFetchError extends Error {}

const FETCH_TIMEOUT_MS = 12000;
const MAX_HTML_BYTES = 5_000_000; // don't ingest huge pages
const MIN_GOOD = 200; // enough text that we trust the direct fetch
const MIN_OK = 100; // bare minimum to attempt analysis
const UA = "Mozilla/5.0 (compatible; VeritasBot/1.0; +https://veritas.demo) Reader";

// Block obvious SSRF targets (private/loopback/link-local hosts, metadata).
function assertPublicHttpUrl(raw: string): URL {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new ArticleFetchError("That doesn't look like a valid URL.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new ArticleFetchError("Only http(s) links are supported.");
  }
  const host = url.hostname.toLowerCase();
  const blocked =
    host === "localhost" ||
    host === "0.0.0.0" ||
    host.endsWith(".local") ||
    host === "metadata.google.internal" ||
    /^127\./.test(host) ||
    /^10\./.test(host) ||
    /^192\.168\./.test(host) ||
    /^169\.254\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host) ||
    host === "::1" ||
    host.startsWith("[");
  if (blocked) {
    throw new ArticleFetchError("That host isn't allowed.");
  }
  return url;
}

// Run Readability over a blob of HTML. Best-effort: returns whatever it found
// (text may be short — the caller decides if that's good enough).
function extractFromHtml(html: string, finalUrl: string): FetchedArticle {
  const dom = new JSDOM(html.slice(0, MAX_HTML_BYTES), { url: finalUrl });
  const doc = dom.window.document;

  let title = doc.title?.trim() ?? "";
  let text = "";
  let siteName: string | undefined;

  try {
    const article = new Readability(doc).parse();
    if (article) {
      title = (article.title || title).trim();
      siteName = article.siteName ?? undefined;
      text = (article.textContent ?? "").replace(/\s+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    }
  } catch {
    // fall through to the body-text fallback
  }

  if (text.length < MIN_GOOD) {
    doc.querySelectorAll("script,style,noscript,nav,header,footer,aside").forEach((el) => el.remove());
    const body = (doc.body?.textContent ?? "").replace(/\s+/g, " ").trim();
    if (body.length > text.length) text = body;
  }

  return { title: title || finalUrl, text, url: finalUrl, siteName };
}

// Cheap direct fetch of raw HTML. Throws on hard failures (blocked, timeout, non-HTML).
async function directFetchHtml(url: URL): Promise<{ html: string; finalUrl: string }> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(url, {
      signal: controller.signal,
      redirect: "follow",
      headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml" },
    });
  } catch (e) {
    throw new ArticleFetchError(
      (e as Error)?.name === "AbortError" ? "The page took too long to load." : "Couldn't reach that URL.",
    );
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) throw new ArticleFetchError(`The page returned HTTP ${res.status}.`);
  const contentType = res.headers.get("content-type") ?? "";
  if (!contentType.includes("html")) throw new ArticleFetchError("That link isn't an HTML page.");
  return { html: await res.text(), finalUrl: res.url || url.toString() };
}

function browserbaseEnabled(): boolean {
  return !!process.env.BROWSERBASE_API_KEY && !!process.env.BROWSERBASE_PROJECT_ID;
}

// Detect bot-wall / captcha interstitials so we don't analyze the block page itself.
function looksBlocked(text: string): boolean {
  if (text.length > 1500) return false; // real articles are long; block pages are short
  const t = text.toLowerCase();
  return [
    "blocked by network security",
    "log in to your reddit account",
    "enable javascript and cookies",
    "just a moment",
    "attention required",
    "verify you are human",
    "checking your browser",
    "access denied",
  ].some((p) => t.includes(p));
}

// Render the page in a real Chromium via Browserbase, then extract. Heavy modules
// are dynamically imported so the no-Browserbase path never loads them.
async function fetchViaBrowserbase(url: URL): Promise<FetchedArticle> {
  const { chromium } = await import("playwright-core");
  const Browserbase = (await import("@browserbasehq/sdk")).default;

  const bb = new Browserbase({ apiKey: process.env.BROWSERBASE_API_KEY! });
  let session;
  try {
    session = await bb.sessions.create({
      projectId: process.env.BROWSERBASE_PROJECT_ID!,
      // CAPTCHA solving works on the free plan. Residential proxies (needed to get
      // past hard IP bans like Reddit/X) are paid-only, so they're opt-in via
      // BROWSERBASE_PROXIES=true — enabling them on the free plan returns HTTP 402.
      ...(process.env.BROWSERBASE_PROXIES === "true" ? { proxies: true } : {}),
      browserSettings: { solveCaptchas: true },
    });
  } catch {
    throw new ArticleFetchError("Couldn't start a Browserbase session — check the Browserbase keys.");
  }

  const browser = await chromium.connectOverCDP(session.connectUrl);
  try {
    const context = browser.contexts()[0] ?? (await browser.newContext());
    const page = context.pages()[0] ?? (await context.newPage());
    await page.goto(url.toString(), { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(3500); // let client-side content + captcha solving settle
    const html = await page.content();
    const finalUrl = page.url() || url.toString();
    const article = extractFromHtml(html, finalUrl);
    if (article.text.length < MIN_OK) {
      throw new ArticleFetchError("Even after rendering, couldn't extract readable text from that page.");
    }
    if (looksBlocked(article.text)) {
      throw new ArticleFetchError(
        "That site blocked automated access even through a real browser. Paste the text directly instead.",
      );
    }
    return article;
  } finally {
    await browser.close().catch(() => {});
  }
}

export async function fetchArticle(rawUrl: string): Promise<FetchedArticle> {
  const url = assertPublicHttpUrl(rawUrl);
  const bb = browserbaseEnabled();
  const host = url.hostname.toLowerCase();
  const isJsHeavy = /(^|\.)(reddit|twitter|x|instagram|threads|facebook|tiktok)\.com$/.test(host);

  // Sites that serve an app shell to a plain fetch go straight to the real browser.
  if (isJsHeavy) {
    if (bb) return fetchViaBrowserbase(url);
    throw new ArticleFetchError(
      `${host} blocks plain fetching. Add Browserbase keys to analyze it, or paste the text directly.`,
    );
  }

  // Generic: try the cheap direct fetch first, fall back to a rendered browser.
  let direct: FetchedArticle | null = null;
  try {
    const { html, finalUrl } = await directFetchHtml(url);
    direct = extractFromHtml(html, finalUrl);
  } catch (e) {
    if (!(e instanceof ArticleFetchError)) throw e;
    if (bb) return fetchViaBrowserbase(url); // blocked/timeout → render it
    throw e;
  }

  if (direct.text.length >= MIN_GOOD) return direct;

  // Thin extraction usually means a JS-rendered page — render it if we can.
  if (bb) {
    try {
      return await fetchViaBrowserbase(url);
    } catch {
      // fall through to whatever the direct fetch managed
    }
  }
  if (direct.text.length >= MIN_OK) return direct;

  throw new ArticleFetchError(
    "Couldn't extract readable text — this page may need JavaScript or block bots. Paste the text directly instead.",
  );
}
