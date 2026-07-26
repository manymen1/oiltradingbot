import { createHash } from "node:crypto";
import type { EventConfig, GammaEvent, ValuationLeg } from "./signalTypes.ts";

const GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events/slug";

export async function fetchGammaEvent(slug: string): Promise<GammaEvent> {
  const response = await fetch(`${GAMMA_EVENT_URL}/${encodeURIComponent(slug)}`, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error(`Gamma fetch failed for ${slug}: ${response.status} ${response.statusText}`);
  const raw = await response.json() as unknown;
  return parseGammaEvent(raw);
}

export function parseGammaEvent(raw: unknown): GammaEvent {
  const record = asRecord(raw);
  const markets = Array.isArray(record.markets) ? record.markets.map(asRecord) : [];
  return {
    slug: requiredString(record.slug, "event.slug"),
    title: stringOr(record.title, ""),
    description: stringOr(record.description, ""),
    resolutionSource: optionalString(record.resolutionSource ?? record.resolution_source),
    markets,
    rawHash: sha256(JSON.stringify(raw)),
  };
}

export function parseValuationLegs(event: GammaEvent, config: EventConfig): ValuationLeg[] {
  return event.markets.map((market) => {
    const question = stringOr(market.question ?? market.title, "");
    const groupItemTitle = optionalString(market.groupItemTitle ?? market.group_item_title);
    const ruleText = [
      event.description,
      event.resolutionSource,
      stringOr(market.description, ""),
      stringOr(market.rules, ""),
      stringOr(market.resolutionSource, ""),
    ].filter(Boolean).join("\n");
    const tokens = parseStringArray(market.clobTokenIds);
    const outcomes = parseStringArray(market.outcomes);
    const yesIndex = outcomes.findIndex((item) => item.toLowerCase() === "yes");
    const noIndex = outcomes.findIndex((item) => item.toLowerCase() === "no");
    const liquidity = numberOr(market.liquidityNum ?? market.liquidity, 0);
    const base = {
      eventSlug: event.slug,
      marketSlug: stringOr(market.slug, sha256(question).slice(0, 12)),
      question,
      groupItemTitle,
      eventKind: config.kind,
      company: config.companyName ?? groupItemTitle ?? inferCompanyFromQuestion(question),
      deadlineIso: config.deadlineIso,
      marketWindowStartIso: config.marketWindowStartIso,
      yesTokenId: yesIndex >= 0 ? tokens[yesIndex] : tokens[0],
      noTokenId: noIndex >= 0 ? tokens[noIndex] : tokens[1],
      conditionId: optionalString(market.conditionId ?? market.condition_id),
      active: boolOr(market.active, true),
      closed: boolOr(market.closed, false),
      acceptingOrders: boolOr(market.acceptingOrders ?? market.accepting_orders, true),
      liquidity,
      ruleText,
      ruleHash: sha256(normalizeRuleText(ruleText || question)),
      ruleFamilyHash: sha256(normalizeRuleFamilyText(ruleText || question)),
    };

    if (config.kind === "ranking") {
      return {
        ...base,
        label: "RANKING",
        ranking: config.ranking,
        parseStatus: "ok" as const,
      };
    }

    const threshold = parseThreshold([question, groupItemTitle, ruleText].filter(Boolean).join("\n"));
    const label = /\bLOW\b/i.test(question) ? "LOW" : /\bHIGH\b/i.test(question) ? "HIGH" : undefined;
    if (!threshold) {
      return {
        ...base,
        label,
        thresholdText: undefined,
        parseStatus: "malformed_threshold" as const,
        parseReason: "threshold_missing_required_B_M_or_T_suffix",
      };
    }
    return {
      ...base,
      label,
      threshold: threshold.value,
      thresholdText: threshold.text,
      parseStatus: ruleSupportsThreshold(ruleText) ? "ok" as const : "unsupported" as const,
      parseReason: ruleSupportsThreshold(ruleText) ? undefined : "rule_text_does_not_confirm_reaches_or_exceeds_threshold",
    };
  });
}

export function parseThreshold(text: string): { value: number; text: string } | null {
  const match = text.match(/\$\s*([0-9]+(?:\.[0-9]+)?)\s*([MBT])\b/i);
  if (!match?.[1] || !match[2]) return null;
  const amount = Number(match[1]);
  const suffix = match[2].toUpperCase();
  if (!Number.isFinite(amount)) return null;
  const multiplier = suffix === "T" ? 1_000_000_000_000 : suffix === "B" ? 1_000_000_000 : 1_000_000;
  return { value: amount * multiplier, text: `$${match[1]}${suffix}` };
}

function ruleSupportsThreshold(ruleText: string): boolean {
  if (!ruleText.trim()) return true;
  const normalized = ruleText.toLowerCase();
  return (
    normalized.includes("reaches or exceeds") ||
    normalized.includes("hit") ||
    normalized.includes("surpass") ||
    normalized.includes("valuation")
  );
}

function inferCompanyFromQuestion(question: string): string {
  const match = question.match(/(?:Will\s+)?(.+?)(?:'s|’s|\s+valuation|\s+be\s+the|\s+have\s+the)/i);
  return match?.[1]?.replace(/^will\s+/i, "").trim() || "unknown";
}

function parseStringArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch {
      return value.split(",").map((item) => item.trim()).filter(Boolean);
    }
  }
  return [];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function numberOr(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function boolOr(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function normalizeRuleText(value: string): string {
  return value.replace(/\s+/g, " ").trim().toLowerCase();
}

function normalizeRuleFamilyText(value: string): string {
  return normalizeRuleText(value)
    .replace(/\$\s*[0-9]+(?:\.[0-9]+)?\s*[mbt]\b/gi, "$k")
    .replace(/\b[0-9]+(?:\.[0-9]+)?\s*(?:million|billion|trillion)\b/gi, "k");
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
