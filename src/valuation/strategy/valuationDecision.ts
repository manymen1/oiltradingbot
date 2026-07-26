import type { BookQuote, NpmEvidence, StrategyConfig, ValuationCandidate, ValuationLeg } from "./signalTypes.ts";
import { allocateCandidate } from "./candidateAllocator.ts";
import { driftCandidate } from "./npmDriftModel.ts";
import { bookAgeMs, depthUnderCap } from "./marketAudit.ts";

export function decideThresholdLeg(
  leg: ValuationLeg,
  evidence: NpmEvidence | undefined,
  quote: BookQuote | undefined,
  config: StrategyConfig,
  locked = false,
): ValuationCandidate {
  const base = candidateBase(leg, evidence, quote, config);
  if (leg.closed || !leg.active) return noAction(base, "SKIP_CLOSED_OR_INACTIVE");
  if (!leg.acceptingOrders) return noAction(base, "SKIP_NOT_ACCEPTING_ORDERS");
  if (leg.parseStatus !== "ok" || leg.threshold === undefined) return alert(base, "STALE_SOURCE_ALERT", "malformed_or_unsupported_threshold_leg", 4);
  if (!evidence) return alert(base, "STALE_SOURCE_ALERT", "missing_npm_evidence_for_company", 4);
  if (!evidence.identityOk) return alert(base, "STALE_SOURCE_ALERT", "npm_company_identity_mismatch", 4);
  if (!quote || quote.bestAsk === null) return noAction(base, "SKIP_MISSING_ORDERBOOK");
  if (quote.spread !== null && quote.spread > config.maxSpread) return noAction(base, "SKIP_WIDE_SPREAD");
  if (quote.liquidity < config.minLiquidity) return noAction(base, "SKIP_LOW_LIQUIDITY");
  if (locked) return noAction(base, "SKIP_DUPLICATE_LOCK");
  if (base.direction !== "UP") return alert(base, "STALE_SOURCE_ALERT", "downside_or_ambiguous_direction_requires_ladder_validation", 7);

  const crossedValue = evidence.maxEligibleValuation ?? evidence.latestValuation;
  const crossedDate = evidence.maxEligibleDate ?? evidence.latestTapeDate;
  if (crossedValue >= leg.threshold) {
    const fairPrice = 0.99;
    const edge = fairPrice - quote.bestAsk;
    return allocateCandidate({
      ...base,
      signalType: "SOURCE_CONFIRMED_YES",
      status: edge >= config.minimumEdge.sourceConfirmed ? "candidate" : "no_action",
      threshold: leg.threshold,
      sourceValuation: evidence.latestValuation,
      sourceDate: evidence.latestTapeDate,
      maxEligibleValuation: crossedValue,
      maxEligibleDate: crossedDate,
      distancePct: distancePct(crossedValue, leg.threshold),
      fairPrice,
      edge,
      confidence: 10,
      reason: edge >= config.minimumEdge.sourceConfirmed
        ? "valuation_reached_or_exceeded_threshold_inside_market_window"
        : "source_confirmed_but_market_price_has_no_remaining_edge",
    }, config);
  }

  const drift = driftCandidate(leg, evidence, quote, config);
  if (drift) return drift;

  return allocateCandidate({
    ...base,
    signalType: "NO_ACTION",
    status: "no_action",
    threshold: leg.threshold,
    sourceValuation: evidence.latestValuation,
    sourceDate: evidence.latestTapeDate,
    maxEligibleValuation: evidence.maxEligibleValuation,
    maxEligibleDate: evidence.maxEligibleDate,
    distancePct: distancePct(evidence.latestValuation, leg.threshold),
    fairPrice: 0,
    edge: 0,
    confidence: 8,
    reason: "valuation_has_not_crossed_threshold_and_no_model_edge",
  }, config);
}

function candidateBase(
  leg: ValuationLeg,
  evidence: NpmEvidence | undefined,
  quote: BookQuote | undefined,
  config: StrategyConfig,
) {
  const sourceConfirmedCap = config.maxYesPriceBySignal.SOURCE_CONFIRMED_YES ?? config.defaultMaxYesPrice;
  return {
    signalType: "NO_ACTION" as const,
    status: "skip" as const,
    company: leg.company,
    eventSlug: leg.eventSlug,
    marketSlug: leg.marketSlug,
    deadline: leg.deadlineIso,
    threshold: leg.threshold,
    direction: thresholdDirection(leg),
    yesTokenId: leg.yesTokenId,
    sourceValuation: evidence?.latestValuation,
    sourceDate: evidence?.latestTapeDate,
    maxEligibleValuation: evidence?.maxEligibleValuation,
    maxEligibleDate: evidence?.maxEligibleDate,
    distancePct: evidence && leg.threshold !== undefined ? distancePct(evidence.latestValuation, leg.threshold) : undefined,
    yesAsk: quote?.bestAsk ?? null,
    bestBid: quote?.bestBid ?? null,
    spread: quote?.spread ?? null,
    liquidity: quote?.liquidity ?? leg.liquidity,
    depthUnderCap: depthUnderCap(quote, sourceConfirmedCap),
    bookAgeMs: bookAgeMs(quote),
    fairPrice: 0,
    edge: 0,
    confidence: 0,
    reason: "not_evaluated",
    ruleHash: leg.ruleHash,
  };
}

function thresholdDirection(leg: ValuationLeg): "UP" | "DOWN" | "UNKNOWN" {
  const text = `${leg.question}\n${leg.groupItemTitle ?? ""}\n${leg.ruleText}`.toLowerCase();
  // "(LOW)" legs and ↓-labeled strikes are falls-to markets even though the
  // question says "hit" and Polymarket's copy-pasted rule boilerplate says
  // "reaches or exceeds". Observed: the Stripe "hit (LOW) $150B" leg parsed
  // as UP against a $173B tape and produced a phantom 97c SOURCE_CONFIRMED
  // candidate on a market the crowd correctly prices at 1c. A label that
  // contradicts the rule text must never resolve toward a trade.
  const hasDownCue = /[↓↘]|\(low\)|hits? a low|low of \$|down|below|less than|at or below|falls? to|drops? to/.test(text);
  const hasConfirmedDownRule = /\(low\)|[↓↘]|hits? a low|at or below|less than or equal|falls? to or below|below the listed amount/.test(text);
  if (hasConfirmedDownRule) return "DOWN";
  if (hasDownCue) return "UNKNOWN";
  if (/reaches or exceeds|exceeds|surpass|hit/.test(text)) return "UP";
  return "UNKNOWN";
}

function noAction(base: ReturnType<typeof candidateBase>, reason: string): ValuationCandidate {
  return { ...base, signalType: "NO_ACTION", status: "skip", reason, confidenceScore: 0, edgeScore: 0, maxPrice: 0, orderUsd: 0, liveAllowed: false };
}

function alert(
  base: ReturnType<typeof candidateBase>,
  signalType: "STALE_SOURCE_ALERT",
  reason: string,
  confidence: number,
): ValuationCandidate {
  return { ...base, signalType, status: "alert", reason, confidence, confidenceScore: confidence, edgeScore: 0, maxPrice: 0, orderUsd: 0, liveAllowed: false };
}

function distancePct(valuation: number, threshold: number): number {
  return Math.abs(valuation - threshold) / threshold;
}
