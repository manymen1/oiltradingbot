import { fetchGammaEvent, parseGammaEvent, parseValuationLegs } from "./marketParser.ts";
import { fetchBookQuote } from "./orderbookSource.ts";
import type { EventConfig, GammaEvent, StrategyConfig, ValuationLeg } from "./signalTypes.ts";

const GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events/keyset";
const VALUATION_EVENT_RE = /\bvaluation\b.*\bhit\b.*\bby\b/i;

export type DiscoveredValuationEvent = {
  eventSlug: string;
  title: string;
  company?: string;
  deadlineIso?: string;
  marketWindowStartIso?: string;
  npmCompanyId?: string;
  npmSourceUrl?: string;
  source: "configured_seed" | "gamma_crawl";
  configured: boolean;
  legCount: number;
  thresholdLegCount: number;
  directionCounts: Record<"UP" | "DOWN" | "UNKNOWN", number>;
  quoteIssues: string[];
  markets: Array<{
    marketSlug: string;
    question: string;
    company: string;
    threshold?: number;
    direction: "UP" | "DOWN" | "UNKNOWN";
    yesTokenId?: string;
    noTokenId?: string;
    yesBid: number | null;
    yesAsk: number | null;
    noBid: number | null;
    noAsk: number | null;
    active: boolean;
    closed: boolean;
    acceptingOrders: boolean;
    liquidity: number;
    label?: string;
    ranking?: 1 | 2 | 3;
    ruleText: string;
    ruleHash: string;
    parseStatus: string;
  }>;
};

export type ValuationDiscoveryReport = {
  ok: true;
  generatedAt: string;
  configuredSeedCount: number;
  gammaCrawlEnabled: boolean;
  gammaPagesScanned: number;
  gammaEventsScanned: number;
  gammaCrawlExhausted: boolean;
  maxPagesReached: boolean;
  discoveredEventCount: number;
  accessIssues: string[];
  coverage: {
    configuredEventCount: number;
    configuredThresholdEventCount: number;
    crawlDiscoveredEventCount: number;
    configuredSeedFetchFailures: number;
    eventsWithNpmCompanyId: number;
    eventsWithQuoteIssues: number;
  };
  events: DiscoveredValuationEvent[];
};

export async function discoverValuationUniverse(input: {
  config: StrategyConfig;
  crawlGamma?: boolean;
  maxPages?: number;
  pageSize?: number;
  fetchQuotes?: boolean;
}): Promise<ValuationDiscoveryReport> {
  const configured = new Map(input.config.events.map((event) => [event.slug, event]));
  const events = new Map<string, { event: GammaEvent; source: DiscoveredValuationEvent["source"]; config?: EventConfig }>();
  const accessIssues: string[] = [];

  for (const eventConfig of input.config.events) {
    try {
      const event = await fetchGammaEvent(eventConfig.slug);
      if (isValuationEvent(event) || eventConfig.kind === "threshold" || eventConfig.kind === "ranking") {
        events.set(event.slug, { event, source: "configured_seed", config: eventConfig });
      }
    } catch (error) {
      accessIssues.push(`configured_seed_fetch_failed:${eventConfig.slug}:${errorMessage(error)}`);
    }
  }

  let pagesScanned = 0;
  let eventsScanned = 0;
  let gammaCrawlExhausted = input.crawlGamma === false;
  let maxPagesReached = false;
  if (input.crawlGamma !== false) {
    const maxPages = Math.max(1, input.maxPages ?? 50);
    const pageSize = Math.max(20, Math.min(100, input.pageSize ?? 100));
    let cursor: string | undefined;
    const seenCursors = new Set<string>();
    for (let page = 0; page < maxPages; page += 1) {
      try {
        const result = await fetchGammaEventsPage(cursor, pageSize);
        const rawEvents = result.events;
        pagesScanned += 1;
        eventsScanned += rawEvents.length;
        for (const raw of rawEvents) {
          const event = parseGammaEvent(raw);
          if (!isValuationEvent(event) || events.has(event.slug)) continue;
          events.set(event.slug, { event, source: "gamma_crawl", config: inferEventConfig(event) });
        }
        if (!result.nextCursor) {
          gammaCrawlExhausted = true;
          break;
        }
        if (result.nextCursor === cursor || seenCursors.has(result.nextCursor)) {
          accessIssues.push(`gamma_crawl_repeated_cursor:page_${page}:${result.nextCursor}`);
          break;
        }
        seenCursors.add(result.nextCursor);
        cursor = result.nextCursor;
        if (page === maxPages - 1) maxPagesReached = true;
      } catch (error) {
        accessIssues.push(`gamma_crawl_failed:page_${page}:${errorMessage(error)}`);
        break;
      }
    }
  }

  const discovered = (await Promise.all([...events.values()]
    .map(({ event, source, config }) => discoveredEventRow({
      event,
      config: config ?? inferEventConfig(event),
      source,
      configured: configured.has(event.slug),
      fetchQuotes: input.fetchQuotes !== false,
    }))))
    .sort((left, right) => left.company === right.company
      ? left.eventSlug.localeCompare(right.eventSlug)
      : String(left.company ?? "").localeCompare(String(right.company ?? "")));
  const configuredFailures = accessIssues.filter((issue) => issue.startsWith("configured_seed_fetch_failed")).length;

  return {
    ok: true,
    generatedAt: new Date().toISOString(),
    configuredSeedCount: input.config.events.length,
    gammaCrawlEnabled: input.crawlGamma !== false,
    gammaPagesScanned: pagesScanned,
    gammaEventsScanned: eventsScanned,
    gammaCrawlExhausted,
    maxPagesReached,
    discoveredEventCount: discovered.length,
    accessIssues,
    coverage: {
      configuredEventCount: input.config.events.length,
      configuredThresholdEventCount: input.config.events.filter((event) => event.kind === "threshold").length,
      crawlDiscoveredEventCount: discovered.filter((event) => event.source === "gamma_crawl").length,
      configuredSeedFetchFailures: configuredFailures,
      eventsWithNpmCompanyId: discovered.filter((event) => event.npmCompanyId).length,
      eventsWithQuoteIssues: discovered.filter((event) => event.quoteIssues.length > 0).length,
    },
    events: discovered,
  };
}

async function fetchGammaEventsPage(
  afterCursor: string | undefined,
  limit: number,
): Promise<{ events: unknown[]; nextCursor?: string }> {
  const params = new URLSearchParams({
    active: "true",
    closed: "false",
    archived: "false",
    limit: String(limit),
  });
  if (afterCursor) params.set("after_cursor", afterCursor);
  const url = `${GAMMA_EVENTS_URL}?${params.toString()}`;
  const response = await fetch(url, {
    headers: {
      accept: "application/json",
      "user-agent": "Mozilla/5.0 valuation-ladder-bot",
    },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const raw = await response.json() as unknown;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("malformed keyset response");
  }
  const page = raw as { events?: unknown; next_cursor?: unknown };
  if (!Array.isArray(page.events)) {
    throw new Error("malformed keyset events");
  }
  if (page.next_cursor !== undefined && typeof page.next_cursor !== "string") {
    throw new Error("malformed keyset next_cursor");
  }
  return {
    events: page.events,
    nextCursor: page.next_cursor || undefined,
  };
}

async function discoveredEventRow(input: {
  event: GammaEvent;
  config: EventConfig;
  source: DiscoveredValuationEvent["source"];
  configured: boolean;
  fetchQuotes: boolean;
}): Promise<DiscoveredValuationEvent> {
  const { event, config, source, configured } = input;
  const legs = parseValuationLegs(event, config);
  const thresholdLegs = legs.filter((leg) => leg.threshold !== undefined);
  const quotes = input.fetchQuotes ? await quoteLegs(legs) : new Map<string, QuotePair>();
  const quoteIssues = [...quotes.values()].flatMap((quote) => quote.issues);
  const directionCounts = thresholdLegs.reduce<Record<"UP" | "DOWN" | "UNKNOWN", number>>((counts, leg) => {
    counts[directionFromLeg(leg)] += 1;
    return counts;
  }, { UP: 0, DOWN: 0, UNKNOWN: 0 });
  return {
    eventSlug: event.slug,
    title: event.title,
    company: config.companyName,
    deadlineIso: config.deadlineIso,
    marketWindowStartIso: config.marketWindowStartIso,
    npmCompanyId: extractNpmCompanyId(event),
    npmSourceUrl: event.resolutionSource,
    source,
    configured,
    legCount: legs.length,
    thresholdLegCount: thresholdLegs.length,
    directionCounts,
    quoteIssues,
    markets: legs.map((leg) => ({
      marketSlug: leg.marketSlug,
      question: leg.question,
      company: leg.company,
      threshold: leg.threshold,
      direction: directionFromLeg(leg),
      yesTokenId: leg.yesTokenId,
      noTokenId: leg.noTokenId,
      yesBid: quotes.get(leg.marketSlug)?.yesBid ?? null,
      yesAsk: quotes.get(leg.marketSlug)?.yesAsk ?? null,
      noBid: quotes.get(leg.marketSlug)?.noBid ?? null,
      noAsk: quotes.get(leg.marketSlug)?.noAsk ?? null,
      active: leg.active,
      closed: leg.closed,
      acceptingOrders: leg.acceptingOrders,
      liquidity: leg.liquidity,
      label: leg.label,
      ranking: leg.ranking,
      ruleText: leg.ruleText,
      ruleHash: leg.ruleHash,
      parseStatus: leg.parseStatus,
    })),
  };
}

type QuotePair = {
  yesBid: number | null;
  yesAsk: number | null;
  noBid: number | null;
  noAsk: number | null;
  issues: string[];
};

async function quoteLegs(legs: ValuationLeg[]): Promise<Map<string, QuotePair>> {
  const result = new Map<string, QuotePair>();
  await Promise.all(legs.map(async (leg) => {
    const pair: QuotePair = { yesBid: null, yesAsk: null, noBid: null, noAsk: null, issues: [] };
    if (leg.yesTokenId) {
      try {
        const quote = await fetchBookQuote(leg.yesTokenId);
        pair.yesBid = quote.bestBid;
        pair.yesAsk = quote.bestAsk;
      } catch (error) {
        pair.issues.push(`${leg.marketSlug}:yes_quote_failed:${errorMessage(error)}`);
      }
    }
    if (leg.noTokenId) {
      try {
        const quote = await fetchBookQuote(leg.noTokenId);
        pair.noBid = quote.bestBid;
        pair.noAsk = quote.bestAsk;
      } catch (error) {
        pair.issues.push(`${leg.marketSlug}:no_quote_failed:${errorMessage(error)}`);
      }
    }
    result.set(leg.marketSlug, pair);
  }));
  return result;
}

function inferEventConfig(event: GammaEvent): EventConfig {
  return {
    slug: event.slug,
    kind: "threshold",
    companyName: inferCompany(event.title),
    deadlineIso: inferDeadlineIso(event.title) ?? new Date(Date.now() + 30 * 86_400_000).toISOString(),
    marketWindowStartIso: undefined,
  };
}

function isValuationEvent(event: GammaEvent): boolean {
  const text = `${event.title}\n${event.description}`;
  return VALUATION_EVENT_RE.test(text);
}

function extractNpmCompanyId(event: GammaEvent): string | undefined {
  return event.resolutionSource?.match(/company-[a-f0-9-]+/)?.[0];
}

function inferCompany(title: string): string | undefined {
  const match = title.match(/Will\s+(.+?)(?:'s|’s)\s+valuation\s+hit/i);
  return match?.[1]?.trim();
}

function inferDeadlineIso(title: string): string | undefined {
  const match = title.match(/\bby\s+([A-Z][a-z]+)\s+([0-9]{1,2})(?:,\s*([0-9]{4}))?/);
  if (!match?.[1] || !match[2]) return undefined;
  const year = Number(match[3] ?? new Date().getUTCFullYear());
  const month = monthIndex(match[1]);
  const day = Number(match[2]);
  if (month === undefined || !Number.isFinite(day)) return undefined;
  return new Date(Date.UTC(year, month, day + 1, 3, 59, 59, 0)).toISOString();
}

function monthIndex(value: string): number | undefined {
  const index = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"].indexOf(value.toLowerCase());
  return index >= 0 ? index : undefined;
}

function directionFromLeg(leg: ValuationLeg): "UP" | "DOWN" | "UNKNOWN" {
  const text = `${leg.question}\n${leg.ruleText}`.toLowerCase();
  // Downside markers FIRST: "(LOW)" legs and ↓-labeled strikes are
  // falls-to markets even though their question says "hit" and Polymarket's
  // copy-pasted rule boilerplate says "reaches or exceeds". Observed: the
  // Stripe "hit (LOW) $150B" leg parsed as UP against a $173B tape and
  // produced a phantom 97c edge on a market the crowd correctly prices at
  // 1c. Label/rule-text conflicts must never resolve toward a trade.
  if (/\(low\)|[↓↘]|hits? a low|falls? to|drops? to|declines? to/.test(text)) {
    return "DOWN";
  }
  if (/down|below|less than/.test(text)) {
    return /at or below|less than or equal|falls? to or below|below the listed amount/.test(text) ? "DOWN" : "UNKNOWN";
  }
  if (/reaches or exceeds|exceeds|surpass|hit/.test(text)) return "UP";
  return "UNKNOWN";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message.replace(/\s+/g, " ") : String(error);
}
