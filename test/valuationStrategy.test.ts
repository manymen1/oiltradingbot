import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import { normalizeConfig } from "../src/valuation/strategy/valuationConfig.ts";
import { parseNpmEvidence, withEligibleMax } from "../src/valuation/strategy/npmValuationSource.ts";
import { parseGammaEvent, parseThreshold, parseValuationLegs } from "../src/valuation/strategy/marketParser.ts";
import { decideThresholdLeg } from "../src/valuation/strategy/valuationDecision.ts";
import { curveMonotonicityCandidates } from "../src/valuation/strategy/curveArbitrage.ts";
import { calendarDominanceCandidates } from "../src/valuation/strategy/calendarArbitrage.ts";
import { rankingAlertCandidates } from "../src/valuation/strategy/rankingSimulator.ts";
import type { BookQuote, CurvePoint, EventConfig, StrategyConfig, ValuationCandidate, ValuationLeg } from "../src/valuation/strategy/signalTypes.ts";
import { applyCaps, liveBlockers } from "../src/valuation/cli.ts";
import { betaProbeMetadata, validatePostedProbeForCandidate } from "../src/valuation/strategy/probeValidation.ts";
import { candidateLockPath, claimCandidateLock, probePath, writeJson } from "../src/valuation/strategy/stateStore.ts";
import { buildMarketAuditRow, monotonicityAudits } from "../src/valuation/strategy/marketAudit.ts";
import { updateFixingWatch } from "../src/valuation/strategy/fixingWatch.ts";
import { buildNpmBarrierForecasts, buildSourceFreshnessSnapshot, monteCarloTouchProbability, pCrossTomorrow, sourceFreshnessMap, tapeStats } from "../src/valuation/strategy/npmBarrierForecast.ts";
import { isPaperOpenTrigger, updateForecastPaperTrades } from "../src/valuation/strategy/forecastPaper.ts";
import { expectedNpmUpdateAt, phaseForNow } from "../src/valuation/strategy/automationSchedule.ts";
import { meaningfulAlerts, runAutomationCycle } from "../src/valuation/strategy/valuationAutomation.ts";
import { acquireAutomationLock, automationHeartbeatPath, writeAutomationHeartbeat } from "../src/valuation/strategy/automationRuntime.ts";
import { buildLadderEntryPlans, ladderDirection } from "../src/valuation/strategy/valuationLadderEntries.ts";
import { STRATEGY_LADDER_PAPER_SIZE_MULTIPLIERS, updateLadderPaperOrders, type LadderPaperOrder } from "../src/valuation/strategy/ladderPaper.ts";
import { discoverValuationUniverse } from "../src/valuation/strategy/valuationUniverseDiscovery.ts";
import { buildDailyReport } from "../src/valuation/strategy/dailyReport.ts";
import { executeCandidate, isZeroFillResponse } from "../src/valuation/strategy/betaExecution.ts";
import { paperPromotionGateBlockers } from "../src/valuation/strategy/promotionGates.ts";

test("config loader applies safe low-risk defaults", () => {
  const config = testConfig();
  assert.equal(config.mode, "alert_only");
  assert.equal(config.pollMs, 30_000);
  assert.deepEqual(config.npmUpdate, {
    timeZone: "America/New_York",
    hour: 13,
    minute: 0,
  });
  assert.equal(config.automation.taskTimeoutMs, 120_000);
  assert.equal(config.automation.lockTtlMs, 600_000);
  assert.equal(config.automation.maxBackoffMs, 600_000);
  assert.equal(config.automation.alertSink, "file");
  assert.equal(config.globalUsdCap, 100);
  assert.equal(config.perEventUsdCap, 50);
  assert.equal(config.perCompanyUsdCap, 50);
  assert.equal(config.perDeadlineUsdCap, 100);
  assert.equal(config.signalMultipliers.SOURCE_CONFIRMED_YES, 1);
  assert.equal(config.signalMultipliers.RANKING_INCONSISTENCY_ALERT, 0);
});

test("threshold parser accepts B/T/M suffixes and rejects malformed dollar amount", () => {
  assert.equal(parseThreshold("Will OpenAI hit $900B by July 31?")?.value, 900_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit HIGH $1.15T by July 31?")?.value, 1_150_000_000_000);
  assert.equal(parseThreshold("Will Perplexity hit $20B by July 31?")?.value, 20_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit LOW $1.0T by July 31?")?.value, 1_000_000_000_000);
  assert.equal(parseThreshold("Will Anthropic hit LOW $800 by July 31?"), null);
});

test("Gamma fixture parser keeps LOW label but relies on rule threshold language", () => {
  const eventConfig = thresholdEvent("Anthropic");
  const event = parseGammaEvent({
    slug: "anthropic-event",
    title: "Will Anthropic's valuation hit by July 31?",
    description: "This market resolves Yes if NPM Price reports Anthropic reaches or exceeds the listed amount.",
    markets: [
      marketFixture("Will Anthropic's valuation hit LOW $900B by July 31?", "low-900b"),
      marketFixture("Will Anthropic's valuation hit LOW $800 by July 31?", "low-800"),
      {
        ...marketFixture("Will Anthropic's valuation hit (LOW) $800 by July 31?", "low-800-group-title"),
        groupItemTitle: "↓$800B",
      },
    ],
  });
  const legs = parseValuationLegs(event, eventConfig);
  assert.equal(legs[0]?.label, "LOW");
  assert.equal(legs[0]?.parseStatus, "ok");
  assert.equal(legs[0]?.threshold, 900_000_000_000);
  assert.equal(legs[1]?.parseStatus, "malformed_threshold");
  assert.equal(legs[2]?.parseStatus, "ok");
  assert.equal(legs[2]?.threshold, 800_000_000_000);
});

test("ranking parser uses group item title for company mapping", () => {
  const event = parseGammaEvent({
    slug: "ranking-event",
    title: "Largest private company end of July?",
    description: "This market resolves to the listed company with the largest private market valuation.",
    markets: [{
      ...marketFixture("Will Epic Games have the highest private market valuation on July 31?", "epic-ranking"),
      groupItemTitle: "Epic Games",
    }],
  });
  const legs = parseValuationLegs(event, {
    slug: "ranking-event",
    kind: "ranking",
    ranking: 1,
    deadlineIso: "2026-08-01T03:59:59Z",
  });
  assert.equal(legs[0]?.company, "Epic Games");
  assert.equal(legs[0]?.label, "RANKING");
});

test("NPM parser stores latest tape and eligible-window max valuation", () => {
  const evidence = parseNpmEvidence({
    company: { name: "Anthropic" },
    latest_tape_d: { date: "2026-07-03", implied_valuation: 1_080_000_000_000, price: 42 },
    tape_d_prices: [
      { date: "2026-06-28", implied_valuation: 1_200_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_105_000_000_000 },
      { date: "2026-07-03", implied_valuation: 1_080_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const windowed = withEligibleMax(evidence, "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  assert.equal(windowed.latestValuation, 1_080_000_000_000);
  assert.equal(windowed.maxEligibleValuation, 1_105_000_000_000);
  assert.equal(windowed.maxEligibleDate, "2026-07-01");
});

test("NPM parser validates endpoint dba_name against configured company", () => {
  const evidence = parseNpmEvidence({
    company: { dba_name: "Epic Games" },
    latest_tape_d: { date: "2026-07-03", implied_valuation: 32_000_000_000 },
  }, { name: "Epic Games", npmCompanyId: "company-epic", aliases: ["Epic"] });
  const mismatch = parseNpmEvidence({
    company: { dba_name: "Kraken" },
    latest_tape_d: { date: "2026-07-03", implied_valuation: 10_000_000_000 },
  }, { name: "Epic Games", npmCompanyId: "company-epic", aliases: ["Epic"] });
  assert.equal(evidence.identityOk, true);
  assert.equal(mismatch.identityOk, false);
});

test("source-confirmed crossed threshold creates BUY YES candidate", () => {
  const config = normalizeConfig({
    mode: "live",
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  assert.equal(candidate.signalType, "SOURCE_CONFIRMED_YES");
  assert.equal(candidate.status, "candidate");
  assert.equal(candidate.direction, "UP");
  assert.equal(candidate.liveAllowed, true);
  assert.equal(candidate.orderUsd > 0, true);
  assert.equal(candidate.distancePct !== undefined && candidate.distancePct < 0.001, true);
  assert.equal(candidate.depthUnderCap !== undefined && candidate.depthUnderCap >= config.minLiquidity, true);
  assert.equal(candidate.bookAgeMs !== undefined, true);
  assert.equal(candidate.confidenceScore, 10);
  assert.equal(candidate.edgeScore > 0, true);
  assert.deepEqual(candidate.orderTemplate, {
    tokenId: "yes-token",
    side: "BUY",
    outcome: "YES",
    orderType: "FAK",
    amountUsd: candidate.orderUsd,
    maxPrice: 0.95,
    posted: false,
  });
});

test("legacy threshold decision alerts ambiguous downside legs instead of trading", () => {
  const config = testConfig();
  const leg = legFixture({
    question: "Will Stripe's valuation fall below $170B by July 31?",
    ruleText: "This market resolves based on whether the valuation falls below the listed amount.",
    threshold: 170_000_000_000,
  });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 171_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  assert.equal(candidate.signalType, "STALE_SOURCE_ALERT");
  assert.equal(candidate.status, "alert");
  assert.equal(candidate.direction, "DOWN");
  assert.equal(candidate.liveAllowed, false);
  assert.equal(candidate.reason, "downside_or_ambiguous_direction_requires_ladder_validation");
});

test("group item down-arrow controls threshold direction", () => {
  const config = testConfig();
  const leg = legFixture({
    question: "Will Stripe's valuation hit $170B by July 31?",
    groupItemTitle: "↓$170B",
    ruleText: "This market resolves Yes if the valuation reaches or exceeds the listed amount.",
    threshold: 170_000_000_000,
  });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 171_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });

  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.01), config);

  assert.equal(candidate.direction, "DOWN");
  assert.equal(candidate.liveAllowed, false);
  assert.equal(candidate.reason, "downside_or_ambiguous_direction_requires_ladder_validation");
});

test("non-crossed threshold no-actions unless drift edge exists", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_050_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.5), config);
  assert.equal(candidate.signalType, "NO_ACTION");
  assert.equal(candidate.liveAllowed, false);
});

test("closed and malformed legs do not trade", () => {
  const config = testConfig();
  const closed = decideThresholdLeg(
    { ...legFixture({ threshold: 1_100_000_000_000 }), closed: true },
    undefined,
    quoteFixture(0.5),
    config,
  );
  const malformed = decideThresholdLeg(
    { ...legFixture({ threshold: undefined }), parseStatus: "malformed_threshold" },
    undefined,
    quoteFixture(0.5),
    config,
  );
  assert.equal(closed.status, "skip");
  assert.equal(malformed.signalType, "STALE_SOURCE_ALERT");
  assert.equal(malformed.liveAllowed, false);
});

test("hard monotonicity violation buys underpriced lower threshold YES", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 900_000_000_000, marketSlug: "lower" });
  const higher = legFixture({ threshold: 950_000_000_000, marketSlug: "higher" });
  const quotes = new Map([
    ["lower", quoteFixture(0.52)],
    ["higher", quoteFixture(0.64)],
  ]);
  const candidates = curveMonotonicityCandidates([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.64 },
  ], quotes, config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "CURVE_MONOTONICITY_YES");
  assert.equal(candidates[0]?.marketSlug, "lower");
});

test("calendar dominance violation buys later deadline YES", () => {
  const config = testConfig();
  const july = legFixture({ threshold: 900_000_000_000, marketSlug: "july", deadlineIso: "2026-08-01T03:59:59Z" });
  const dec = legFixture({ threshold: 900_000_000_000, marketSlug: "dec", deadlineIso: "2027-01-01T04:59:59Z" });
  const quotes = new Map([
    ["july", quoteFixture(0.7)],
    ["dec", quoteFixture(0.58)],
  ]);
  const candidates = calendarDominanceCandidates([
    { leg: july, yesAsk: 0.7 },
    { leg: dec, yesAsk: 0.58 },
  ], quotes, config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "CALENDAR_DOMINANCE_YES");
  assert.equal(candidates[0]?.marketSlug, "dec");
});

test("ranking market inconsistency is alert-only", () => {
  const config = testConfig();
  const leg = rankingLegFixture("Anthropic", 1);
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_200_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidates = rankingAlertCandidates([leg], new Map([["Anthropic", evidence]]), new Map([[leg.marketSlug, quoteFixture(0.4)]]), config);
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0]?.signalType, "RANKING_INCONSISTENCY_ALERT");
  assert.equal(candidates[0]?.liveAllowed, false);
});

test("live blocker audit explains why candidate is not live-eligible", async () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const candidate = decideThresholdLeg(leg, evidence, quoteFixture(0.81), config);
  const blockers = await liveBlockers(candidate, config, "test-config-hash");
  assert.equal(blockers.includes("operator_mode_alert_only"), true);
  assert.equal(blockers.includes("missing_live_config_ack"), true);
  assert.equal(blockers.includes("missing_posted_probe_success"), true);
  assert.equal(blockers.includes("posting_env_not_armed"), true);
});

test("live gate allows only source-confirmed stale YES policy", async () => {
  const config = normalizeConfig({
    ...testConfig(),
    mode: "live",
  });
  const curveCandidate = candidateFixture({
    signalType: "CURVE_MONOTONICITY_YES",
    reason: "bid_backed_curve_repair",
  });
  const blockers = await liveBlockers(curveCandidate, config, "test-config-hash");
  assert.equal(blockers.includes("source_confirmed_stale_yes_only_live_policy"), true);

  const execution = await executeCandidate(curveCandidate, config, "test-config-hash");
  assert.deepEqual(execution, {
    posted: false,
    skipped: true,
    reason: "source_confirmed_stale_yes_only_live_policy",
  });
});

test("zero-fill responses are not accepted as posted execution", () => {
  assert.equal(isZeroFillResponse({ rejected: true, zeroFill: true }), true);
  assert.equal(isZeroFillResponse({ rejected: true, filledShares: 0 }), true);
  assert.equal(isZeroFillResponse({ success: true, filledShares: 10 }), false);
});

test("paper promotion gates block forecast and relative-value live modes", () => {
  assert.deepEqual(paperPromotionGateBlockers("SOURCE_CONFIRMED_YES"), []);
  assert.equal(
    paperPromotionGateBlockers("NPM_MULTI_DAY_BARRIER_FORECAST_YES").includes("paper_promotion_gate_forecast_model_not_satisfied"),
    true,
  );
  assert.equal(
    paperPromotionGateBlockers("NPM_MULTI_DAY_BARRIER_FORECAST_YES").includes("paper_promotion_gate_passive_ladder_maker_not_satisfied"),
    true,
  );
  assert.deepEqual(paperPromotionGateBlockers("CURVE_MONOTONICITY_YES"), [
    "paper_promotion_gate_relative_value_not_live_enabled",
  ]);
});

test("source-confirmed live gate requires depth and fresh orderbook", async () => {
  const config = normalizeConfig({
    ...testConfig(),
    mode: "live",
  });
  const lowDepth = candidateFixture({ depthUnderCap: config.minLiquidity - 1 });
  const lowDepthBlockers = await liveBlockers(lowDepth, config, "test-config-hash");
  assert.equal(lowDepthBlockers.includes("depth_under_taker_cap_below_minimum"), true);
  assert.deepEqual(await executeCandidate(lowDepth, config, "test-config-hash"), {
    posted: false,
    skipped: true,
    reason: "depth_under_taker_cap_below_minimum",
  });

  const staleBook = candidateFixture({ bookAgeMs: config.orderbookMaxAgeMs + 1 });
  const staleBlockers = await liveBlockers(staleBook, config, "test-config-hash");
  assert.equal(staleBlockers.includes("orderbook_stale"), true);
  assert.deepEqual(await executeCandidate(staleBook, config, "test-config-hash"), {
    posted: false,
    skipped: true,
    reason: "orderbook_stale",
  });

  const ambiguousDirection = candidateFixture({ direction: "UNKNOWN" });
  const directionBlockers = await liveBlockers(ambiguousDirection, config, "test-config-hash");
  assert.equal(directionBlockers.includes("direction_semantics_not_up"), true);
  assert.deepEqual(await executeCandidate(ambiguousDirection, config, "test-config-hash"), {
    posted: false,
    skipped: true,
    reason: "direction_semantics_not_up",
  });
});

test("candidate caps enforce global, event, company, and deadline budgets", () => {
  const config = normalizeConfig({
    mode: "live",
    globalUsdCap: 25,
    perEventUsdCap: 15,
    perCompanyUsdCap: 15,
    perDeadlineUsdCap: 25,
    events: [
      { ...thresholdEvent("Anthropic"), slug: "event-a" },
      { ...thresholdEvent("Anthropic"), slug: "event-b" },
      { ...thresholdEvent("OpenAI"), slug: "event-c" },
    ],
    companies: [
      { name: "Anthropic", npmCompanyId: "company-a" },
      { name: "OpenAI", npmCompanyId: "company-b" },
    ],
  });
  const companyBlocked = applyCaps(config, [
    candidateFixture({ eventSlug: "event-a", marketSlug: "a", company: "Anthropic", orderUsd: 10 }),
    candidateFixture({ eventSlug: "event-b", marketSlug: "b", company: "Anthropic", orderUsd: 10 }),
  ], []);
  assert.equal(companyBlocked[0]?.status, "candidate");
  assert.equal(companyBlocked[1]?.status, "skip");
  assert.equal(companyBlocked[1]?.reason, "company_notional_cap_exceeded");

  const deadlineBlocked = applyCaps({ ...config, globalUsdCap: 100, perCompanyUsdCap: 100 }, [
    candidateFixture({ eventSlug: "event-a", marketSlug: "a", company: "Anthropic", orderUsd: 15 }),
    candidateFixture({ eventSlug: "event-c", marketSlug: "c", company: "OpenAI", orderUsd: 15 }),
  ], []);
  assert.equal(deadlineBlocked[1]?.status, "skip");
  assert.equal(deadlineBlocked[1]?.reason, "deadline_notional_cap_exceeded");

  const eventBlocked = applyCaps({ ...config, perCompanyUsdCap: 100, perDeadlineUsdCap: 100 }, [
    candidateFixture({ eventSlug: "event-a", marketSlug: "a", company: "Anthropic", orderUsd: 10 }),
    candidateFixture({ eventSlug: "event-a", marketSlug: "b", company: "Anthropic", orderUsd: 10 }),
  ], []);
  assert.equal(eventBlocked[1]?.reason, "event_notional_cap_exceeded");

  const globalBlocked = applyCaps({ ...config, perEventUsdCap: 100, perCompanyUsdCap: 100, perDeadlineUsdCap: 100 }, [
    candidateFixture({ eventSlug: "event-a", marketSlug: "a", company: "Anthropic", orderUsd: 15 }),
    candidateFixture({ eventSlug: "event-c", marketSlug: "c", company: "OpenAI", orderUsd: 15 }),
  ], []);
  assert.equal(globalBlocked[1]?.reason, "global_notional_cap_exceeded");
});

test("candidate caps count existing locks by company and deadline", () => {
  const config = normalizeConfig({
    mode: "live",
    globalUsdCap: 100,
    perEventUsdCap: 100,
    perCompanyUsdCap: 15,
    perDeadlineUsdCap: 15,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const blockedByCompany = applyCaps(config, [
    candidateFixture({ company: "Anthropic", orderUsd: 10 }),
  ], [{
    eventSlug: "locked-event",
    marketSlug: "locked-market",
    company: "Anthropic",
    deadline: "2026-09-01T03:59:59Z",
    orderUsd: 10,
  }]);
  assert.equal(blockedByCompany[0]?.reason, "company_notional_cap_exceeded");

  const blockedByDeadline = applyCaps({ ...config, perCompanyUsdCap: 100 }, [
    candidateFixture({ company: "Anthropic", deadline: "2026-08-01T03:59:59Z", orderUsd: 10 }),
  ], [{
    eventSlug: "locked-event",
    marketSlug: "locked-market",
    company: "OpenAI",
    deadline: "2026-08-01T03:59:59Z",
    orderUsd: 10,
  }]);
  assert.equal(blockedByDeadline[0]?.reason, "deadline_notional_cap_exceeded");
});

test("candidate lock claim is exclusive before live posting", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-lock-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = candidateFixture();
  assert.equal(await claimCandidateLock(config, candidate), true);
  assert.equal(await claimCandidateLock(config, candidate), false);
  const lock = JSON.parse(await readFile(candidateLockPath(config, candidate), "utf8")) as Record<string, unknown>;
  assert.equal(lock.status, "pending");
  assert.equal(lock.marketSlug, candidate.marketSlug);
});

test("posted probe validation rejects malformed probe files", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-probe-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = decideThresholdLeg(
    legFixture({ threshold: 1_100_000_000_000 }),
    parseNpmEvidence({ latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 } }, { name: "Anthropic", npmCompanyId: "company-a" }),
    quoteFixture(0.81),
    config,
  );
  await writeJson(probePath(config, candidate.marketSlug), {
    ok: true,
    marketSlug: candidate.marketSlug,
    tokenId: "wrong-token",
    timestamp: new Date().toISOString(),
  });
  const validation = await validatePostedProbeForCandidate(config, candidate);
  assert.equal(validation.ok, false);
  assert.equal(validation.blockers.includes("probe_token_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_side_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_order_type_mismatch"), true);
  assert.equal(validation.blockers.includes("probe_sdk_mismatch"), true);
});

test("posted probe validation accepts current beta BUY FAK probe metadata", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-probe-test-"));
  const config = normalizeConfig({
    mode: "live",
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const candidate = decideThresholdLeg(
    legFixture({ threshold: 1_100_000_000_000 }),
    parseNpmEvidence({ latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 } }, { name: "Anthropic", npmCompanyId: "company-a" }),
    quoteFixture(0.81),
    config,
  );
  await writeJson(probePath(config, candidate.marketSlug), {
    ok: true,
    marketSlug: candidate.marketSlug,
    tokenId: candidate.yesTokenId,
    timestamp: new Date().toISOString(),
    ...betaProbeMetadata(),
  });
  const validation = await validatePostedProbeForCandidate(config, candidate);
  assert.equal(validation.ok, true);
  assert.deepEqual(validation.blockers, []);
});

test("monotonicity audit distinguishes bid-backed hard violation from ask-only noise", () => {
  const config = testConfig();
  const lower = legFixture({
    threshold: 900_000_000_000,
    marketSlug: "lower",
    ruleHash: "lower-specific-rule-hash",
    ruleFamilyHash: "same-rule-family",
  });
  const higher = legFixture({
    threshold: 950_000_000_000,
    marketSlug: "higher",
    ruleHash: "higher-specific-rule-hash",
    ruleFamilyHash: "same-rule-family",
  });
  const hard = monotonicityAudits([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.7 },
  ], new Map([
    ["lower", quoteFixture(0.52, 0.5)],
    ["higher", quoteFixture(0.7, 0.62)],
  ]), config, new Date("2026-07-05T00:00:01Z"));
  assert.equal(hard[0]?.violationTier, "HARD_CROSS_MARKET_BID_VIOLATION");
  assert.equal(hard[0]?.tradeableBuyOnly, true);

  const soft = monotonicityAudits([
    { leg: lower, yesAsk: 0.52 },
    { leg: higher, yesAsk: 0.7 },
  ], new Map([
    ["lower", quoteFixture(0.52, 0.5)],
    ["higher", quoteFixture(0.7, 0.45)],
  ]), config, new Date("2026-07-05T00:00:01Z"));
  assert.equal(soft[0]?.violationTier, "SOFT_ASK_ONLY_VIOLATION");
  assert.equal(soft[0]?.tradeableBuyOnly, false);
});

test("market audit newly-crossed state ignores pre-window tape points", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    tape_d_prices: [
      { date: "2026-06-28", implied_valuation: 1_200_000_000_000 },
      { date: "2026-06-30", implied_valuation: 1_090_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const row = buildMarketAuditRow({
    leg,
    evidence,
    quote: quoteFixture(0.81, 0.78),
    config,
    now: new Date("2026-07-02T00:00:00Z"),
  });
  assert.equal(row.previousMaxEligibleValuation, 1_090_000_000_000);
  assert.equal(row.state, "NEWLY_CROSSED");
});

test("market audit row classifies source-confirmed stale crossed leg", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_100_000_000_000 });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    tape_d_prices: [
      { date: "2026-06-30", implied_valuation: 1_090_000_000_000 },
      { date: "2026-07-01", implied_valuation: 1_101_000_000_000 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const row = buildMarketAuditRow({
    leg,
    evidence,
    quote: quoteFixture(0.81, 0.78),
    config,
    now: new Date("2026-07-02T00:00:00Z"),
  });
  assert.equal(row.state, "NEWLY_CROSSED");
  assert.equal(row.crossedQuality, "SOURCE_CONFIRMED_AND_STALE");
  assert.equal(row.depthUnderCap > 0, true);
  assert.equal(row.tradeBand === "tradeable" || row.tradeBand === "maybe", true);
});

test("fixing watch records first-seen and later replay observations", () => {
  const row = marketAuditRowFixture({
    state: "NEWLY_CROSSED",
    maxEligibleValuation: 1_101_000_000_000,
    previousMaxEligibleValuation: 1_090_000_000_000,
    yesAsk: 0.81,
  });
  const first = updateFixingWatch(
    [row],
    { generatedAt: "2026-07-01T00:00:00Z", rows: [{ ...row, maxEligibleValuation: 1_090_000_000_000 }] },
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
  );
  assert.equal(first.newCrosses.length, 1);
  assert.equal(first.newCrosses[0]?.observations[0]?.label, "first_seen");
  assert.equal(first.newCrosses[0]?.observations[0]?.fakUnderCapWouldFill, true);

  const later = updateFixingWatch(
    [{ ...row, yesAsk: 0.96, settlementEdge: 0.04, crossedQuality: "SOURCE_CONFIRMED_BUT_ALREADY_PRICED", tradeBand: "alert" }],
    first.snapshot,
    first.state,
    new Date("2026-07-02T00:00:31Z"),
  );
  const tracked = later.state.crosses[first.newCrosses[0]?.key ?? ""];
  assert.equal(tracked?.observations.some((obs) => obs.label === "plus_5s"), true);
  assert.equal(tracked?.observations.some((obs) => obs.label === "plus_30s"), true);
  assert.equal(later.missedEdgeReport[0]?.repricedByLatest, 0.1499999999999999);
});

test("fixing watch requires min liquidity for FAK fillability", () => {
  const row = marketAuditRowFixture({
    state: "NEWLY_CROSSED",
    maxEligibleValuation: 1_101_000_000_000,
    previousMaxEligibleValuation: 1_090_000_000_000,
    yesAsk: 0.81,
    depthUnderCap: 99,
  });
  const update = updateFixingWatch(
    [row],
    { generatedAt: "2026-07-01T00:00:00Z", rows: [{ ...row, maxEligibleValuation: 1_090_000_000_000 }] },
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
    { minLiquidity: 100 },
  );
  const firstSeen = update.newCrosses[0]?.observations[0];
  assert.equal(firstSeen?.staleLiquidity, true);
  assert.equal(firstSeen?.fakUnderCapWouldFill, false);
  assert.equal(update.missedEdgeReport[0]?.firstSeenFakUnderCapWouldFill, false);
});

test("fixing watch baselines first run unless replay is explicit", () => {
  const row = marketAuditRowFixture({ state: "NEWLY_CROSSED" });
  const baseline = updateFixingWatch(
    [row],
    null,
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
  );
  assert.equal(baseline.newCrosses.length, 0);

  const replay = updateFixingWatch(
    [row],
    null,
    { version: 1, updatedAt: "2026-07-01T00:00:00Z", crosses: {} },
    new Date("2026-07-02T00:00:00Z"),
    { replayExisting: true },
  );
  assert.equal(replay.newCrosses.length, 1);
});

test("barrier forecast computes NPM tape stats and crossing probability", () => {
  const evidence = parseNpmEvidence({
    latest_tape_d: { date: "2026-07-04", implied_valuation: 995 },
    tape_d_prices: [
      { date: "2026-07-01", implied_valuation: 950 },
      { date: "2026-07-02", implied_valuation: 970 },
      { date: "2026-07-03", implied_valuation: 985 },
      { date: "2026-07-04", implied_valuation: 995 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" });
  const stats = tapeStats(evidence);
  assert.equal(stats.returnCount, 3);
  assert.equal(stats.recent3DayAvgDailyDrift > 0, true);
  assert.equal(pCrossTomorrow(995, 1000, stats.meanDailyLogReturn, stats.dailyVol) > 0.5, true);
  assert.equal(monteCarloTouchProbability({
    latestValuation: 995,
    threshold: 1000,
    mu: stats.meanDailyLogReturn,
    sigma: stats.dailyVol,
    days: 10,
    paths: 500,
    seed: "forecast-test",
  }) > 0.5, true);
});

test("barrier forecast creates alert-only near-boundary candidate", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "forecast-market" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-04", implied_valuation: 995 },
    tape_d_prices: [
      { date: "2026-07-01", implied_valuation: 950 },
      { date: "2026-07-02", implied_valuation: 970 },
      { date: "2026-07-03", implied_valuation: 985 },
      { date: "2026-07-04", implied_valuation: 995 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const quote = quoteFixture(0.35, 0.33);
  const sourceFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    now: new Date("2026-07-04T12:00:00Z"),
  });
  const row = marketAuditRowFixture({
    marketSlug: "forecast-market",
    threshold: 1_000,
    state: "NEAR_BOUNDARY",
    latestValuation: 995,
    latestDate: "2026-07-04",
    maxEligibleValuation: 995,
    maxEligibleDate: "2026-07-04",
    yesAsk: 0.35,
    yesBid: 0.33,
    depthUnderCap: 250,
  });
  const forecasts = buildNpmBarrierForecasts({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["forecast-market", quote]]),
    marketRows: [row],
    sourceFreshnessByCompany: sourceFreshnessMap(sourceFreshness),
    config,
    now: new Date("2026-07-04T12:00:00Z"),
    simulations: 800,
  });
  assert.equal(forecasts[0]?.signalType, "NPM_MULTI_DAY_BARRIER_FORECAST_YES");
  assert.equal(forecasts[0]?.liveEligible, false);
  assert.equal((forecasts[0]?.edge ?? 0) >= 0.12, true);
});

test("barrier forecast blocks stale source data", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "forecast-market" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-01", implied_valuation: 995 },
    tape_d_prices: [
      { date: "2026-06-29", implied_valuation: 950 },
      { date: "2026-06-30", implied_valuation: 970 },
      { date: "2026-07-01", implied_valuation: 995 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const previousFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    now: new Date("2026-07-02T12:00:00Z"),
  });
  const sourceFreshness = buildSourceFreshnessSnapshot({
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    previous: previousFreshness,
    now: new Date("2026-07-06T12:00:00Z"),
  });
  const forecasts = buildNpmBarrierForecasts({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["forecast-market", quoteFixture(0.35, 0.33)]]),
    marketRows: [marketAuditRowFixture({
      marketSlug: "forecast-market",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      depthUnderCap: 250,
    })],
    sourceFreshnessByCompany: sourceFreshnessMap(sourceFreshness),
    config,
    now: new Date("2026-07-06T12:00:00Z"),
    simulations: 200,
  });
  assert.equal(forecasts[0]?.signalType, "NO_FORECAST_EDGE");
  assert.equal(forecasts[0]?.reason, "stale_endpoint_blocked");
  assert.equal(forecasts[0]?.freshnessState, "STALE_ENDPOINT");
});

test("forecast paper opens loose paper-only watchlist trigger", () => {
  const row = forecastRowFixture({
    signalType: "NO_FORECAST_EDGE",
    distancePct: 0.02,
    modelFairPrice: 0.62,
    pTouchByDeadline: 0.62,
    yesAsk: 0.55,
    edge: 0.07,
    confidenceScore: 0.55,
  });
  assert.equal(isPaperOpenTrigger(row), true);
  const update = updateForecastPaperTrades({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", trades: [] },
    forecasts: [row],
    now: new Date("2026-07-06T17:00:00Z"),
    sizeUsd: 1,
  });
  assert.equal(update.opened.length, 1);
  assert.equal(update.opened[0]?.paperTrigger, "paper_watchlist");
  assert.equal(update.opened[0]?.entryPrice, 0.55);
  assert.equal(update.metrics.totalTrades, 1);
});

test("forecast paper updates after fixing and scores resolved touch", () => {
  const opened = updateForecastPaperTrades({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", trades: [] },
    forecasts: [forecastRowFixture({
      signalType: "NPM_MULTI_DAY_BARRIER_FORECAST_YES",
      latestDate: "2026-07-06",
      latestValuation: 990,
      maxEligibleValuation: 990,
      distancePct: 0.01,
      modelFairPrice: 0.72,
      pTouchByDeadline: 0.72,
      yesAsk: 0.55,
      edge: 0.17,
      confidenceScore: 0.74,
    })],
    now: new Date("2026-07-06T17:00:00Z"),
    sizeUsd: 1,
  });
  const updated = updateForecastPaperTrades({
    previous: opened.state,
    forecasts: [forecastRowFixture({
      latestDate: "2026-07-07",
      latestValuation: 1_005,
      maxEligibleValuation: 1_005,
      distancePct: -0.005,
      yesAsk: 0.93,
    })],
    now: new Date("2026-07-07T17:00:00Z"),
    sizeUsd: 1,
  });
  assert.equal(updated.updated.length, 1);
  assert.equal(updated.updated[0]?.status, "resolved");
  assert.equal(updated.updated[0]?.thresholdTouched, true);
  assert.equal(updated.updated[0]?.finalResolution, true);
  assert.equal(updated.updated[0]?.brierScore, (0.72 - 1) ** 2);
  assert.equal(updated.metrics.resolvedTrades, 1);
  assert.equal(updated.metrics.totalHypotheticalPnl, 0.8182);
});

test("falls-to legs are never scored as paper wins by reaches-or-exceeds logic", async () => {
  // A "(LOW)" leg resolves YES when the valuation FALLS to the threshold.
  // Scoring maxEligibleValuation >= threshold as a win inverted 23 resolved
  // paper positions and fabricated +$171 of P&L -- the exact number the
  // go/no-go decision reads. Anything not clearly UP must score as unknown.
  const { updateLadderPaperOrders } = await import("../src/valuation/strategy/ladderPaper.ts");
  const downPlan = {
    company: "Anthropic",
    marketSlug: "will-anthropics-valuation-hit-low-1pt0-by-july-31",
    eventSlug: "anthropic-valuation",
    deadline: "2026-07-31T23:59:00Z",
    threshold: 1_000,
    // Valuation is ABOVE the threshold: a falls-to leg has NOT triggered.
    maxEligibleValuation: 1_139,
    direction: "DOWN" as const,
    entryMode: "MAKER_CURVE_REPAIR_BID" as const,
    passiveBidPrice: 0.069,
    yesAsk: 0.12,
    yesBid: 0.05,
    modelFair: 0.89,
    requiredEdge: 0.06,
    paperEligible: true,
    liveEligible: false,
    blockers: [] as string[],
    reason: "bid_backed_curve_repair_passive_bid_paper_only",
  } as never;

  const opened = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans: [downPlan],
    now: new Date("2026-07-06T12:00:00Z"),
    sizeUsd: 1,
  });
  const resolved = updateLadderPaperOrders({
    previous: opened.state,
    plans: [downPlan],
    now: new Date("2026-08-01T12:00:00Z"), // past deadline -> forced resolve
    sizeUsd: 1,
  });
  const order = [...resolved.state.orders].pop();
  if (order && order.status === "resolved") {
    assert.notEqual(
      order.finalResolution,
      true,
      "a falls-to leg whose valuation stayed above the threshold must not resolve YES",
    );
    assert.ok(
      (order.hypotheticalPnl ?? 0) <= 0,
      "an untriggered falls-to leg cannot produce positive paper P&L",
    );
  }
});

test("ladder entry planner creates near-boundary passive maker bid without crossing ask", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "stripe-175" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
    tape_d_prices: [
      { date: "2026-07-05", implied_valuation: 980 },
      { date: "2026-07-06", implied_valuation: 993 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["stripe-175", quoteFixture(0.97, 0.4)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "stripe-175", state: "NEAR_BOUNDARY", yesAsk: 0.97, yesBid: 0.4 })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "stripe-175",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      yesAsk: 0.97,
      yesBid: 0.4,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "MAKER_NEAR_BOUNDARY_BID");
  assert.equal(plans[0]?.sourceConfirmed, false);
  assert.equal(plans[0]?.passiveBidPrice, 0.56);
  assert.equal(plans[0]?.paperEligible, true);
  assert.equal(plans[0]?.liveEligible, false);
});

test("ladder entry planner blocks near-boundary paper bid on low visible liquidity", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "stripe-175" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const lowLiquidityQuote = { ...quoteFixture(0.97, 0.4), liquidity: config.minLiquidity - 1 };
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["stripe-175", lowLiquidityQuote]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "stripe-175", state: "NEAR_BOUNDARY", yesAsk: 0.97, yesBid: 0.4 })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "stripe-175",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      yesAsk: 0.97,
      yesBid: 0.4,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "MAKER_NEAR_BOUNDARY_BID");
  assert.equal(plans[0]?.paperEligible, false);
  assert.equal(plans[0]?.blockers.includes("orderbook_liquidity_below_minimum"), true);
  assert.equal(plans[0]?.reason, "near_boundary_passive_bid_blocked_by_structural_risk");
});

test("ladder entry planner blocks paper entries without YES token mapping", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "stripe-175", yesTokenId: undefined });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["stripe-175", quoteFixture(0.97, 0.4)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "stripe-175", state: "NEAR_BOUNDARY", yesAsk: 0.97, yesBid: 0.4 })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "stripe-175",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      yesAsk: 0.97,
      yesBid: 0.4,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "MAKER_NEAR_BOUNDARY_BID");
  assert.equal(plans[0]?.paperEligible, false);
  assert.equal(plans[0]?.blockers.includes("missing_yes_token"), true);
  assert.equal(plans[0]?.yesTokenId, undefined);
});

test("ladder entry planner annotates market shape and nearest ladder thresholds", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 900, marketSlug: "lower-900" });
  const target = legFixture({ threshold: 1_000, marketSlug: "target-1000" });
  const higher = legFixture({ threshold: 1_100, marketSlug: "higher-1100" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
    tape_d_prices: [
      { date: "2026-07-05", implied_valuation: 980 },
      { date: "2026-07-06", implied_valuation: 993 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [lower, target, higher],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([
      ["lower-900", quoteFixture(0.98, 0.95)],
      ["target-1000", quoteFixture(0.97, 0.4)],
      ["higher-1100", quoteFixture(0.22, 0.18)],
    ]),
    marketRows: [
      marketAuditRowFixture({ marketSlug: "lower-900", state: "PREVIOUSLY_CROSSED", crossedQuality: "SOURCE_CONFIRMED_BUT_ALREADY_PRICED" }),
      marketAuditRowFixture({ marketSlug: "target-1000", state: "NEAR_BOUNDARY", yesAsk: 0.97, yesBid: 0.4 }),
      marketAuditRowFixture({ marketSlug: "higher-1100", state: "UNCROSSED", yesAsk: 0.22, yesBid: 0.18 }),
    ],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "target-1000",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      yesAsk: 0.97,
      yesBid: 0.4,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  const plan = plans.find((item) => item.marketSlug === "target-1000");
  assert.equal(plan?.ladderContext?.marketShape.thresholdCount, 3);
  assert.equal(plan?.ladderContext?.marketShape.minThreshold, 900);
  assert.equal(plan?.ladderContext?.marketShape.maxThreshold, 1_100);
  assert.equal(plan?.ladderContext?.marketShape.currentValuation, 993);
  assert.equal(plan?.ladderContext?.nearestLower?.marketSlug, "lower-900");
  assert.equal(plan?.ladderContext?.nearestLower?.threshold, 900);
  assert.equal(plan?.ladderContext?.nearestUpper?.marketSlug, "target-1000");
  assert.equal(plan?.ladderContext?.nearestUpper?.threshold, 1_000);
  assert.equal(plan?.ladderContext?.nearestUpper?.yesAsk, 0.97);
});

test("ladder entry planner allows source-confirmed taker only for strict stale legs", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "crossed" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 1_010 },
    tape_d_prices: [
      { date: "2026-07-05", implied_valuation: 990 },
      { date: "2026-07-06", implied_valuation: 1_010 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["crossed", quoteFixture(0.81, 0.78)]]),
    marketRows: [marketAuditRowFixture({
      marketSlug: "crossed",
      state: "NEWLY_CROSSED",
      crossedQuality: "SOURCE_CONFIRMED_AND_STALE",
      yesAsk: 0.81,
      yesBid: 0.78,
      depthUnderCap: 100,
      liveBlockers: [],
    })],
    forecasts: [],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "TAKER_SOURCE_CONFIRMED");
  assert.equal(plans[0]?.sourceConfirmed, true);
  assert.equal(plans[0]?.liveEligible, true);
});

test("ladder entry planner blocks source-confirmed taker below depth floor", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "crossed-shallow" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 1_010 },
    tape_d_prices: [
      { date: "2026-07-05", implied_valuation: 990 },
      { date: "2026-07-06", implied_valuation: 1_010 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["crossed-shallow", quoteFixture(0.81, 0.78, { liquidity: config.minLiquidity })]]),
    marketRows: [marketAuditRowFixture({
      marketSlug: "crossed-shallow",
      state: "NEWLY_CROSSED",
      crossedQuality: "SOURCE_CONFIRMED_AND_STALE",
      yesAsk: 0.81,
      yesBid: 0.78,
      depthUnderCap: config.minLiquidity - 1,
      liveBlockers: [],
    })],
    forecasts: [],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "TAKER_SOURCE_CONFIRMED");
  assert.equal(plans[0]?.liveEligible, false);
  assert.equal(plans[0]?.blockers.includes("depth_under_taker_cap_below_minimum"), true);
  assert.equal(plans[0]?.reason, "source_confirmed_but_live_blocked");
});

test("ladder entry planner refuses stale-YES taker when source is not confirmed", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "inconsistent-crossed" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 990 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["inconsistent-crossed", quoteFixture(0.81, 0.78)]]),
    marketRows: [marketAuditRowFixture({
      marketSlug: "inconsistent-crossed",
      state: "NEWLY_CROSSED",
      crossedQuality: "SOURCE_CONFIRMED_AND_STALE",
      yesAsk: 0.81,
      yesBid: 0.78,
      depthUnderCap: config.minLiquidity,
      liveBlockers: [],
    })],
    forecasts: [],
    monotonicity: [],
    config,
  });
  assert.notEqual(plans[0]?.entryMode, "TAKER_SOURCE_CONFIRMED");
  assert.equal(plans[0]?.sourceConfirmed, false);
  assert.equal(plans[0]?.liveEligible, false);
});

test("ladder entry planner uses configured book age for source-confirmed taker", () => {
  const config = { ...testConfig(), orderbookMaxAgeMs: 60_000 };
  const leg = legFixture({ threshold: 1_000, marketSlug: "crossed-slow-book" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 1_010 },
    tape_d_prices: [
      { date: "2026-07-05", implied_valuation: 990 },
      { date: "2026-07-06", implied_valuation: 1_010 },
    ],
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["crossed-slow-book", quoteFixture(0.81, 0.78)]]),
    marketRows: [marketAuditRowFixture({
      marketSlug: "crossed-slow-book",
      state: "NEWLY_CROSSED",
      crossedQuality: "SOURCE_CONFIRMED_AND_STALE",
      yesAsk: 0.81,
      yesBid: 0.78,
      depthUnderCap: config.minLiquidity,
      bookAgeMs: 30_000,
      liveBlockers: [],
    })],
    forecasts: [],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "TAKER_SOURCE_CONFIRMED");
  assert.equal(plans[0]?.liveEligible, true);
  assert.equal(plans[0]?.blockers.includes("orderbook_stale"), false);
});

test("ladder entry planner keeps far optionality as paper-only passive bid", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_200, marketSlug: "far" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 1_000 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["far", quoteFixture(0.12, 0.08)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "far", state: "FAR_ABOVE", yesAsk: 0.12, yesBid: 0.08 })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "far",
      threshold: 1_200,
      state: "FAR_ABOVE",
      latestValuation: 1_000,
      maxEligibleValuation: 1_000,
      distancePct: 0.1667,
      yesAsk: 0.12,
      yesBid: 0.08,
      modelFairPrice: 0.18,
    })],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "MAKER_FAR_OPTIONALITY_BID");
  assert.equal(plans[0]?.passiveBidPrice, 0.078);
  assert.equal(plans[0]?.paperEligible, true);
  assert.equal(plans[0]?.liveEligible, false);
});

test("ladder entry planner creates adjacent range spread paper candidate", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 1_000, marketSlug: "lower-range" });
  const higher = legFixture({ threshold: 1_100, marketSlug: "higher-range" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 990 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [lower, higher],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([
      ["lower-range", quoteFixture(0.2, 0.18)],
      ["higher-range", quoteFixture(0.8, 0.75)],
    ]),
    noQuotes: new Map([
      ["higher-range", freshQuoteFixture(0.25, 0.2)],
    ]),
    marketRows: [
      marketAuditRowFixture({ marketSlug: "lower-range", state: "NEAR_BOUNDARY", yesAsk: 0.2, yesBid: 0.18 }),
      marketAuditRowFixture({ marketSlug: "higher-range", state: "UNCROSSED", yesAsk: 0.8, yesBid: 0.75 }),
    ],
    forecasts: [
      forecastRowFixture({ company: "Anthropic", marketSlug: "lower-range", threshold: 1_000, pTouchByDeadline: 0.8, modelFairPrice: 0.8 }),
      forecastRowFixture({ company: "Anthropic", marketSlug: "higher-range", threshold: 1_100, pTouchByDeadline: 0.2, modelFairPrice: 0.2 }),
    ],
    monotonicity: [],
    config,
  });
  assert.equal(plans[0]?.entryMode, "RANGE_SPREAD_PAPER");
  assert.equal(plans[0]?.paperEligible, true);
  assert.equal(plans[0]?.liveEligible, false);
  assert.equal(plans[0]?.passiveBidPrice, 0.45);
  assert.equal(plans[0]?.range?.lowerYesTokenId, "yes-token");
  assert.equal(plans[0]?.range?.higherNoTokenId, "no-token");
  assert.equal(plans[0]?.range?.lowerYesAsk, 0.2);
  assert.equal(plans[0]?.range?.higherNoAsk, 0.25);
  assert.equal(plans[0]?.range?.higherNoAskSource, "no_orderbook");
  assert.equal(plans[0]?.range?.modelRangeProbability, 0.6);
});

test("ladder entry planner blocks range spread paper without paired NO token mapping", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 1_000, marketSlug: "lower-range" });
  const higher = legFixture({ threshold: 1_100, marketSlug: "higher-range", noTokenId: undefined });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 990 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [lower, higher],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([
      ["lower-range", quoteFixture(0.2, 0.18)],
      ["higher-range", quoteFixture(0.8, 0.75)],
    ]),
    marketRows: [
      marketAuditRowFixture({ marketSlug: "lower-range", state: "NEAR_BOUNDARY", yesAsk: 0.2, yesBid: 0.18 }),
      marketAuditRowFixture({ marketSlug: "higher-range", state: "UNCROSSED", yesAsk: 0.8, yesBid: 0.75 }),
    ],
    forecasts: [
      forecastRowFixture({ company: "Anthropic", marketSlug: "lower-range", threshold: 1_000, pTouchByDeadline: 0.8, modelFairPrice: 0.8 }),
      forecastRowFixture({ company: "Anthropic", marketSlug: "higher-range", threshold: 1_100, pTouchByDeadline: 0.2, modelFairPrice: 0.2 }),
    ],
    monotonicity: [],
    config,
  });
  const plan = plans.find((item) => item.entryMode === "RANGE_SPREAD_PAPER");
  assert.equal(plan?.paperEligible, false);
  assert.equal(plan?.blockers.includes("paired_missing_no_token"), true);
  assert.equal(plan?.blockers.includes("paired_missing_no_orderbook"), true);
  assert.equal(plan?.range?.higherNoTokenId, undefined);
  assert.equal(plan?.range?.higherNoAskSource, "synthetic_from_yes_bid");
});

test("ladder entry planner blocks curve repair paper when lower leg has structural risk", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 1_000, marketSlug: "lower-curve", closed: true });
  const higher = legFixture({ threshold: 1_100, marketSlug: "higher-curve" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 990 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [lower, higher],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([
      ["lower-curve", quoteFixture(0.4, 0.37)],
      ["higher-curve", quoteFixture(0.68, 0.6)],
    ]),
    marketRows: [
      marketAuditRowFixture({ marketSlug: "lower-curve", state: "NEAR_BOUNDARY", yesAsk: 0.4, yesBid: 0.37 }),
      marketAuditRowFixture({ marketSlug: "higher-curve", state: "UNCROSSED", yesAsk: 0.68, yesBid: 0.6 }),
    ],
    forecasts: [],
    monotonicity: [{
      company: "Anthropic",
      deadline: "2026-08-01T03:59:59Z",
      lowerMarketSlug: "lower-curve",
      higherMarketSlug: "higher-curve",
      lowerThreshold: 1_000,
      higherThreshold: 1_100,
      lowerYesAsk: 0.4,
      lowerYesBid: 0.37,
      higherYesAsk: 0.68,
      higherYesBid: 0.6,
      bidBackedEdge: 0.2,
      midEdge: 0.24,
      askOnlyEdge: 0.28,
      bookAgeMs: 1_000,
      sameRuleHashFamily: true,
      sameDirectionSemantics: true,
      violationTier: "HARD_CROSS_MARKET_BID_VIOLATION",
      tradeableBuyOnly: true,
      reason: "higher_threshold_bid_exceeds_lower_threshold_ask_by_min_edge",
    }],
    config,
  });
  const plan = plans.find((item) => item.entryMode === "MAKER_CURVE_REPAIR_BID");
  assert.equal(plan?.paperEligible, false);
  assert.equal(plan?.blockers.includes("market_not_accepting_orders"), true);
  assert.equal(plan?.reason, "bid_backed_curve_repair_blocked_by_structural_risk");
});

test("ladder entry planner blocks range spread paper when paired leg book is stale", () => {
  const config = testConfig();
  const lower = legFixture({ threshold: 1_000, marketSlug: "lower-range" });
  const higher = legFixture({ threshold: 1_100, marketSlug: "higher-range" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 990 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [lower, higher],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([
      ["lower-range", quoteFixture(0.2, 0.18)],
      ["higher-range", quoteFixture(0.8, 0.75)],
    ]),
    marketRows: [
      marketAuditRowFixture({ marketSlug: "lower-range", state: "NEAR_BOUNDARY", yesAsk: 0.2, yesBid: 0.18 }),
      marketAuditRowFixture({ marketSlug: "higher-range", state: "UNCROSSED", yesAsk: 0.8, yesBid: 0.75, bookAgeMs: config.orderbookMaxAgeMs + 1 }),
    ],
    forecasts: [
      forecastRowFixture({ company: "Anthropic", marketSlug: "lower-range", threshold: 1_000, pTouchByDeadline: 0.8, modelFairPrice: 0.8 }),
      forecastRowFixture({ company: "Anthropic", marketSlug: "higher-range", threshold: 1_100, pTouchByDeadline: 0.2, modelFairPrice: 0.2 }),
    ],
    monotonicity: [],
    config,
  });
  const plan = plans.find((item) => item.entryMode === "RANGE_SPREAD_PAPER");
  assert.equal(plan?.paperEligible, false);
  assert.equal(plan?.blockers.includes("paired_orderbook_stale"), true);
  assert.equal(plan?.reason, "adjacent_threshold_range_spread_blocked_by_structural_risk");
});

test("ladder paper opens passive orders and fills only when ask reaches bid", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "paper-maker" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const baseInput = {
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "paper-maker", state: "NEAR_BOUNDARY" })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "paper-maker",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  };
  const openedPlans = buildLadderEntryPlans({
    ...baseInput,
    quotes: new Map([["paper-maker", quoteFixture(0.97, 0.4)]]),
  });
  const opened = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans: openedPlans,
    now: new Date("2026-07-06T12:00:00Z"),
    sizeUsd: 1,
  });
  assert.equal(opened.opened.length, 1);
  assert.equal(opened.opened[0]?.status, "working");
  assert.equal(opened.opened[0]?.sourceConfirmed, false);
  assert.equal(opened.metrics.filledThisRun, 0);

  const fillPlans = buildLadderEntryPlans({
    ...baseInput,
    quotes: new Map([["paper-maker", quoteFixture(0.55, 0.4)]]),
  });
  const filled = updateLadderPaperOrders({
    previous: opened.state,
    plans: fillPlans,
    now: new Date("2026-07-06T12:05:00Z"),
    sizeUsd: 1,
  });
  assert.equal(filled.filled.length, 1);
  assert.equal(filled.filled[0]?.status, "filled");
  assert.equal(filled.filled[0]?.fillPrice, 0.56);

  const noDuplicate = updateLadderPaperOrders({
    previous: {
      ...filled.state,
      orders: [
        ...filled.state.orders.map((order) => ({ ...order, status: "resolved" as const })),
        ...filled.state.orders.map((order) => ({ ...order, status: "working" as const })),
      ],
    },
    plans: fillPlans,
    now: new Date("2026-07-06T12:10:00Z"),
    sizeUsd: 1,
  });
  assert.equal(noDuplicate.opened.length, 0);
  assert.equal(noDuplicate.state.orders.length, 1);
});

test("ladder paper applies strategy size multipliers by entry mode", () => {
  const config = testConfig();
  const nearLeg = legFixture({ threshold: 1_000, marketSlug: "near-paper" });
  const farLeg = legFixture({ threshold: 1_200, marketSlug: "far-paper" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [nearLeg, farLeg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([
      ["near-paper", quoteFixture(0.97, 0.4)],
      ["far-paper", quoteFixture(0.12, 0.08)],
    ]),
    marketRows: [
      marketAuditRowFixture({ marketSlug: "near-paper", state: "NEAR_BOUNDARY", yesAsk: 0.97, yesBid: 0.4 }),
      marketAuditRowFixture({ marketSlug: "far-paper", state: "FAR_ABOVE", yesAsk: 0.12, yesBid: 0.08 }),
    ],
    forecasts: [
      forecastRowFixture({
        company: "Anthropic",
        marketSlug: "near-paper",
        threshold: 1_000,
        state: "NEAR_BOUNDARY",
        latestValuation: 993,
        maxEligibleValuation: 993,
        distancePct: 0.007,
        modelFairPrice: 0.68,
      }),
      forecastRowFixture({
        company: "Anthropic",
        marketSlug: "far-paper",
        threshold: 1_200,
        state: "FAR_ABOVE",
        latestValuation: 993,
        maxEligibleValuation: 993,
        distancePct: 0.1725,
        yesAsk: 0.12,
        yesBid: 0.08,
        modelFairPrice: 0.18,
      }),
    ],
    monotonicity: [],
    config,
  });
  const opened = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans,
    now: new Date("2026-07-06T12:00:00Z"),
    sizeUsd: 10,
    sizeMultipliers: STRATEGY_LADDER_PAPER_SIZE_MULTIPLIERS,
  });
  const near = opened.opened.find((order) => order.entryMode === "MAKER_NEAR_BOUNDARY_BID");
  const far = opened.opened.find((order) => order.entryMode === "MAKER_FAR_OPTIONALITY_BID");
  assert.equal(near?.sizeUsd, 2.5);
  assert.equal(far?.sizeUsd, 0.5);
  assert.equal(opened.metrics.activeExposureUsd, 3);
});

test("ladder paper enforces company event deadline and global paper caps before opening", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "paper-maker" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const plans = buildLadderEntryPlans({
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["paper-maker", quoteFixture(0.97, 0.4)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "paper-maker", state: "NEAR_BOUNDARY" })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "paper-maker",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  const blocked = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans,
    now: new Date("2026-07-06T12:00:00Z"),
    sizeUsd: 10,
    caps: {
      globalUsdCap: 100,
      perEventUsdCap: 100,
      perCompanyUsdCap: 5,
      perDeadlineUsdCap: 100,
    },
  });
  assert.equal(blocked.opened.length, 0);
  assert.equal(blocked.blocked.length, 1);
  assert.equal(blocked.blocked[0]?.reason, "paper_company_notional_cap_exceeded");
  assert.equal(blocked.metrics.blockedOpenThisRun, 1);
  assert.equal(blocked.metrics.activeExposureUsd, 0);
  assert.deepEqual(blocked.metrics.byCompanyExposureUsd, {});
});

test("ladder paper stores non-range deadlines for deadline cap accounting", () => {
  const config = testConfig();
  const firstLeg = legFixture({ threshold: 1_000, marketSlug: "deadline-first" });
  const secondLeg = legFixture({ threshold: 1_005, marketSlug: "deadline-second" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const firstPlans = buildLadderEntryPlans({
    legs: [firstLeg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["deadline-first", quoteFixture(0.97, 0.4)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "deadline-first", state: "NEAR_BOUNDARY" })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "deadline-first",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  const opened = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans: firstPlans,
    now: new Date("2026-07-06T12:00:00Z"),
    sizeUsd: 6,
    caps: {
      globalUsdCap: 100,
      perEventUsdCap: 100,
      perCompanyUsdCap: 100,
      perDeadlineUsdCap: 10,
    },
  });
  assert.equal(opened.opened.length, 1);
  assert.equal(opened.opened[0]?.deadline, firstLeg.deadlineIso);

  const secondPlans = buildLadderEntryPlans({
    legs: [secondLeg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["deadline-second", quoteFixture(0.97, 0.4)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "deadline-second", state: "NEAR_BOUNDARY" })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "deadline-second",
      threshold: 1_005,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.0119,
      modelFairPrice: 0.68,
    })],
    monotonicity: [],
    config,
  });
  const blocked = updateLadderPaperOrders({
    previous: opened.state,
    plans: [...firstPlans, ...secondPlans],
    now: new Date("2026-07-06T12:05:00Z"),
    sizeUsd: 6,
    caps: {
      globalUsdCap: 100,
      perEventUsdCap: 100,
      perCompanyUsdCap: 100,
      perDeadlineUsdCap: 10,
    },
  });
  assert.equal(blocked.opened.length, 0);
  assert.equal(blocked.blocked[0]?.reason, "paper_deadline_notional_cap_exceeded");
  assert.deepEqual(blocked.metrics.byDeadlineExposureUsd, { [firstLeg.deadlineIso]: 6 });
});

test("ladder paper cancels stale passive bids before NPM fixing", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "paper-maker" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const baseInput = {
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "paper-maker", state: "NEAR_BOUNDARY" })],
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "paper-maker",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      modelFairPrice: 0.68,
    })],
    quotes: new Map([["paper-maker", quoteFixture(0.97, 0.4)]]),
    monotonicity: [],
    config,
  };
  const plans = buildLadderEntryPlans(baseInput);
  const opened = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans,
    now: new Date("2026-07-06T16:00:00Z"),
    sizeUsd: 1,
    nextFixingAt: new Date("2026-07-06T17:00:00Z"),
    cancelBeforeFixingMs: 10 * 60_000,
  });
  assert.equal(opened.opened[0]?.status, "working");

  const held = updateLadderPaperOrders({
    previous: opened.state,
    plans,
    now: new Date("2026-07-06T16:45:00Z"),
    sizeUsd: 1,
    nextFixingAt: new Date("2026-07-06T17:00:00Z"),
    cancelBeforeFixingMs: 10 * 60_000,
  });
  assert.equal(held.state.orders[0]?.status, "working");

  const cancelled = updateLadderPaperOrders({
    previous: opened.state,
    plans,
    now: new Date("2026-07-06T16:55:00Z"),
    sizeUsd: 1,
    nextFixingAt: new Date("2026-07-06T17:00:00Z"),
    cancelBeforeFixingMs: 10 * 60_000,
  });
  assert.equal(cancelled.state.orders[0]?.status, "cancelled");
  assert.equal(cancelled.state.orders[0]?.cancelReason, "cancel_before_npm_fixing_model_fair_stale");
  assert.equal(cancelled.opened.length, 0);
});

test("ladder paper cancels and requotes when model fair reprices lower", () => {
  const config = testConfig();
  const leg = legFixture({ threshold: 1_000, marketSlug: "paper-maker" });
  const evidence = withEligibleMax(parseNpmEvidence({
    latest_tape_d: { date: "2026-07-06", implied_valuation: 993 },
  }, { name: "Anthropic", npmCompanyId: "company-a" }), "2026-06-29T00:00:00Z", "2026-08-01T03:59:59Z");
  const baseInput = {
    legs: [leg],
    evidenceByCompany: new Map([["Anthropic", evidence]]),
    quotes: new Map([["paper-maker", quoteFixture(0.97, 0.4)]]),
    marketRows: [marketAuditRowFixture({ marketSlug: "paper-maker", state: "NEAR_BOUNDARY" })],
    monotonicity: [],
    config,
  };
  const highFairPlans = buildLadderEntryPlans({
    ...baseInput,
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "paper-maker",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      modelFairPrice: 0.68,
    })],
  });
  const opened = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [] },
    plans: highFairPlans,
    now: new Date("2026-07-06T12:00:00Z"),
    sizeUsd: 1,
  });
  assert.equal(opened.opened[0]?.passiveBidPrice, 0.56);

  const lowerFairPlans = buildLadderEntryPlans({
    ...baseInput,
    forecasts: [forecastRowFixture({
      company: "Anthropic",
      marketSlug: "paper-maker",
      threshold: 1_000,
      state: "NEAR_BOUNDARY",
      latestValuation: 993,
      maxEligibleValuation: 993,
      distancePct: 0.007,
      modelFairPrice: 0.62,
    })],
  });
  const repriced = updateLadderPaperOrders({
    previous: opened.state,
    plans: lowerFairPlans,
    now: new Date("2026-07-06T12:05:00Z"),
    sizeUsd: 1,
  });
  assert.equal(repriced.updated[0]?.status, "cancelled");
  assert.equal(repriced.updated[0]?.cancelReason, "model_fair_repriced_passive_bid");
  assert.equal(repriced.opened[0]?.status, "working");
  assert.equal(repriced.opened[0]?.passiveBidPrice, 0.5);
});

test("ladder paper proof summarizes passive-fill evidence without enabling live", () => {
  const orders = Array.from({ length: 30 }, (_, index) => ladderPaperOrderFixture({
    id: `near-${index}`,
    hypotheticalPnl: 0.05,
  }));
  const update = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders },
    plans: [],
    now: new Date("2026-07-07T00:00:00Z"),
  });
  assert.equal(update.metrics.proofBeforeLive.currentFilledOrders, 30);
  assert.equal(update.metrics.proofBeforeLive.currentResolvedOrders, 30);
  assert.equal(update.metrics.proofBeforeLive.totalHypotheticalPnl, 1.5);
  assert.equal(update.metrics.proofBeforeLive.readyForManualReview, true);
  assert.equal(update.metrics.proofBeforeLive.readyForLive, false);
  assert.equal(update.metrics.byModeProof[0]?.entryMode, "MAKER_NEAR_BOUNDARY_BID");
  assert.equal(update.metrics.byModeProof[0]?.readyForManualReview, true);

  const stale = updateLadderPaperOrders({
    previous: {
      version: 1,
      updatedAt: "2026-07-06T00:00:00Z",
      orders: [
        ...orders,
        ladderPaperOrderFixture({
          id: "stale-source-cancel",
          status: "cancelled",
          cancelReason: "cancelled_by_blocker:stale_source",
          hypotheticalPnl: null,
        }),
      ],
    },
    plans: [],
    now: new Date("2026-07-07T00:00:00Z"),
  });
  assert.equal(stale.metrics.proofBeforeLive.staleSourceErrorCount, 1);
  assert.equal(stale.metrics.proofBeforeLive.readyForManualReview, false);
});

test("ladder paper live proof excludes range and curve diagnostics", () => {
  const diagnosticOrders = Array.from({ length: 30 }, (_, index) => ladderPaperOrderFixture({
    id: `range-${index}`,
    entryMode: "RANGE_SPREAD_PAPER",
    hypotheticalPnl: 0.1,
  }));
  const curveOrders = Array.from({ length: 30 }, (_, index) => ladderPaperOrderFixture({
    id: `curve-${index}`,
    entryMode: "MAKER_CURVE_REPAIR_BID",
    hypotheticalPnl: 0.1,
  }));
  const update = updateLadderPaperOrders({
    previous: { version: 1, updatedAt: "2026-07-06T00:00:00Z", orders: [...diagnosticOrders, ...curveOrders] },
    plans: [],
    now: new Date("2026-07-07T00:00:00Z"),
  });
  assert.equal(update.metrics.totalHypotheticalPnl, 6);
  assert.equal(update.metrics.proofBeforeLive.currentFilledOrders, 0);
  assert.equal(update.metrics.proofBeforeLive.currentResolvedOrders, 0);
  assert.equal(update.metrics.proofBeforeLive.totalHypotheticalPnl, 0);
  assert.equal(update.metrics.proofBeforeLive.readyForManualReview, false);
  assert.equal(update.metrics.byModeProof.some((row) => row.entryMode === "RANGE_SPREAD_PAPER" && row.readyForManualReview), true);
  assert.equal(update.metrics.byModeProof.some((row) => row.entryMode === "MAKER_CURVE_REPAIR_BID" && row.readyForManualReview), true);
});

test("ladder direction rejects ambiguous down-arrow reaches-or-exceeds legs", () => {
  const leg = legFixture({
    question: "Will Stripe's valuation hit ↓$170B by July 31?",
    ruleText: "This market resolves Yes if NPM reports Stripe reaches or exceeds the listed amount.",
  });
  assert.equal(ladderDirection(leg), "UNKNOWN");
});

test("valuation discovery stores rule text, executable quotes, and crawl coverage", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url.includes("/events/slug/test-event")) {
      return jsonResponse({
        slug: "test-event",
        title: "Will Stripe's valuation hit by July 31?",
        description: "This market resolves Yes if NPM reaches or exceeds the listed amount.",
        resolutionSource: "https://forgeglobal.com/insights/companies/company-6edded11-6786-4392-9695-3cce6fda0de0",
        markets: [marketFixture("Will Stripe's valuation hit $175B by July 31?", "stripe-175")],
      });
    }
    if (url.includes("/events/slug/ranking-event")) {
      return jsonResponse({
        slug: "ranking-event",
        title: "Largest private company end of July?",
        description: "This market resolves to the listed company with the largest private market valuation.",
        markets: [marketFixture("Will Epic Games have the highest private market valuation on July 31?", "epic-ranking")],
      });
    }
    if (url.includes("gamma-api.polymarket.com/events?")) {
      return jsonResponse(Array.from({ length: 20 }, (_, index) => ({
        slug: `non-valuation-${index}`,
        title: `Non valuation event ${index}`,
        description: "Unrelated market",
        markets: [],
      })));
    }
    if (url.includes("token_id=yes-token")) {
      return jsonResponse({
        bids: [{ price: "0.56", size: "10" }],
        asks: [{ price: "0.79", size: "10" }],
      });
    }
    if (url.includes("token_id=no-token")) {
      return jsonResponse({
        bids: [{ price: "0.21", size: "10" }],
        asks: [{ price: "0.44", size: "10" }],
      });
    }
    return new Response("not found", { status: 404, statusText: "Not Found" });
  }) as typeof fetch;
  try {
    const report = await discoverValuationUniverse({
      config: normalizeConfig({
        events: [
          thresholdEvent("Stripe"),
          {
            slug: "ranking-event",
            kind: "ranking",
            ranking: 1,
            deadlineIso: "2026-08-01T03:59:59Z",
            marketWindowStartIso: "2026-06-30T00:00:00Z",
          },
        ],
        companies: [
          { name: "Stripe", npmCompanyId: "company-6edded11-6786-4392-9695-3cce6fda0de0" },
          { name: "Epic Games", npmCompanyId: "company-625e5f47-7ff7-45c4-be95-0305665164bd" },
        ],
      }),
      crawlGamma: true,
      maxPages: 1,
      pageSize: 20,
    });
    assert.equal(report.discoveredEventCount, 2);
    assert.equal(report.gammaPagesScanned, 1);
    assert.equal(report.gammaEventsScanned, 20);
    assert.equal(report.gammaCrawlExhausted, false);
    assert.equal(report.maxPagesReached, true);
    assert.equal(report.coverage.configuredEventCount, 2);
    assert.equal(report.coverage.configuredThresholdEventCount, 1);
    assert.equal(report.coverage.configuredSeedFetchFailures, 0);
    assert.equal(report.coverage.eventsWithNpmCompanyId, 1);
    assert.equal(report.coverage.eventsWithQuoteIssues, 0);
    const threshold = report.events.find((event) => event.eventSlug === "test-event");
    const ranking = report.events.find((event) => event.eventSlug === "ranking-event");
    assert.equal(threshold?.npmCompanyId, "company-6edded11-6786-4392-9695-3cce6fda0de0");
    assert.equal(threshold?.npmSourceUrl?.includes("forgeglobal.com"), true);
    assert.equal(threshold?.markets[0]?.ruleText.includes("reaches or exceeds"), true);
    assert.equal(threshold?.markets[0]?.yesBid, 0.56);
    assert.equal(threshold?.markets[0]?.yesAsk, 0.79);
    assert.equal(threshold?.markets[0]?.noBid, 0.21);
    assert.equal(threshold?.markets[0]?.noAsk, 0.44);
    assert.equal(ranking?.markets[0]?.label, "RANKING");
    assert.equal(ranking?.markets[0]?.ranking, 1);
    assert.equal(ranking?.markets[0]?.company, "Epic Games");
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("daily report exposes discovery coverage and crawl completeness", () => {
  const report = buildDailyReport({
    generatedAt: "2026-07-06T00:00:00Z",
    discovery: {
      discoveredEventCount: 5,
      coverage: {
        configuredEventCount: 7,
        configuredThresholdEventCount: 5,
        crawlDiscoveredEventCount: 0,
        configuredSeedFetchFailures: 0,
        eventsWithNpmCompanyId: 5,
        eventsWithQuoteIssues: 1,
      },
      gammaPagesScanned: 10,
      gammaEventsScanned: 1000,
      gammaCrawlExhausted: false,
      maxPagesReached: true,
      accessIssues: ["quote_failed"],
    },
  });
  const discovery = report.discovery as Record<string, unknown>;
  assert.equal(discovery.discoveredEventCount, 5);
  assert.equal(discovery.gammaPagesScanned, 10);
  assert.equal(discovery.gammaEventsScanned, 1000);
  assert.equal(discovery.gammaCrawlExhausted, false);
  assert.equal(discovery.maxPagesReached, true);
  assert.deepEqual((discovery.coverage as Record<string, unknown>).eventsWithQuoteIssues, 1);
  assert.deepEqual(discovery.accessIssues, ["quote_failed"]);
});

test("daily report preserves ladder source-confirmed state and activation triggers", () => {
  const report = buildDailyReport({
    generatedAt: "2026-07-06T00:00:00Z",
    entryAudit: {
      summary: { liveEligibleCount: 0 },
      actionablePlans: [{
        company: "Stripe",
        marketSlug: "stripe-175",
        threshold: 175_000_000_000,
        entryMode: "MAKER_NEAR_BOUNDARY_BID",
        direction: "UP",
        sourceConfirmed: false,
        distancePct: 0.006,
        yesAsk: 0.97,
        yesBid: 0.4,
        modelFair: 0.68,
        passiveBidPrice: 0.56,
        paperEligible: true,
        liveEligible: false,
        activation: {
          forecastActiveAt: 172_375_000_000,
          sourceConfirmedAt: 175_000_000_000,
          alertIfAskBelow: 0.94,
        },
        blockers: [],
        reason: "near_boundary_passive_bid_paper_only",
      }],
    },
    ladderPaper: {
      baseSizeUsd: 10,
      sizeMultipliers: {
        MAKER_NEAR_BOUNDARY_BID: 0.25,
        MAKER_FAR_OPTIONALITY_BID: 0.05,
      },
      summary: {},
      workingOrders: [{
        company: "Stripe",
        eventSlug: "stripe-event",
        marketSlug: "stripe-175",
        threshold: 175_000_000_000,
        deadline: "2026-08-01T03:59:59Z",
        entryMode: "MAKER_NEAR_BOUNDARY_BID",
        sourceConfirmed: false,
        passiveBidPrice: 0.56,
        modelFair: 0.68,
        requiredEdge: 0.12,
        sizeUsd: 2.5,
        status: "working",
        reason: "near_boundary_passive_bid_paper_only",
      }],
      blocked: [{
        company: "Stripe",
        eventSlug: "stripe-event",
        marketSlug: "stripe-180",
        deadline: "2026-08-01T03:59:59Z",
        entryMode: "MAKER_NEAR_BOUNDARY_BID",
        reason: "paper_deadline_notional_cap_exceeded",
        sizeUsd: 2.5,
        usedUsd: 10,
        capUsd: 10,
      }],
    },
  });
  const ladderEntries = report.ladderEntries as Record<string, unknown>;
  const plans = ladderEntries.actionablePlans as Record<string, unknown>[];
  assert.equal(plans[0]?.sourceConfirmed, false);
  assert.deepEqual(plans[0]?.activation, {
    forecastActiveAt: 172_375_000_000,
    sourceConfirmedAt: 175_000_000_000,
    alertIfAskBelow: 0.94,
  });
  const ladderPaper = report.ladderPaper as Record<string, unknown>;
  assert.equal(ladderPaper.baseSizeUsd, 10);
  assert.deepEqual(ladderPaper.sizeMultipliers, {
    MAKER_NEAR_BOUNDARY_BID: 0.25,
    MAKER_FAR_OPTIONALITY_BID: 0.05,
  });
  const workingOrders = ladderPaper.workingOrders as Record<string, unknown>[];
  assert.equal(workingOrders[0]?.sourceConfirmed, false);
  assert.equal(workingOrders[0]?.eventSlug, "stripe-event");
  assert.equal(workingOrders[0]?.deadline, "2026-08-01T03:59:59Z");
  assert.equal(workingOrders[0]?.sizeUsd, 2.5);
  assert.equal(workingOrders[0]?.requiredEdge, 0.12);
  const blocked = ladderPaper.blocked as Record<string, unknown>[];
  assert.equal(blocked[0]?.reason, "paper_deadline_notional_cap_exceeded");
  assert.equal(blocked[0]?.sizeUsd, 2.5);
  assert.equal(blocked[0]?.usedUsd, 10);
  assert.equal(blocked[0]?.capUsd, 10);
});

test("automation schedule resolves expected NPM fixing phases", () => {
  const expected = new Date("2026-07-06T17:00:00Z");
  assert.equal(phaseForNow(new Date("2026-07-06T16:15:00Z"), expected), "PRE_FIXING_PREP");
  assert.equal(phaseForNow(new Date("2026-07-06T16:55:00Z"), expected), "FIXING_WINDOW");
  assert.equal(phaseForNow(new Date("2026-07-06T17:30:00Z"), expected), "POST_FIXING_REVIEW");
  assert.equal(phaseForNow(new Date("2026-07-06T20:00:00Z"), expected), "LOW_FREQUENCY_MONITOR");
  assert.equal(expectedNpmUpdateAt(new Date("2026-07-06T18:30:00Z")).toISOString(), "2026-07-07T17:00:00.000Z");
  assert.equal(expectedNpmUpdateAt(new Date("2026-01-06T12:00:00Z")).toISOString(), "2026-01-06T18:00:00.000Z");
});

test("automation dry run lists phase tasks without execution", async () => {
  const cycle = await runAutomationCycle({
    now: new Date("2026-07-06T17:00:00Z"),
    dryRun: true,
    runTask: async () => {
      throw new Error("should not run");
    },
  });
  assert.equal(cycle.phase, "FIXING_WINDOW");
  assert.deepEqual(cycle.tasks, ["fixing-watch", "market-audit-strict", "entry-audit", "ladder-paper", "forecast-paper"]);
  assert.equal(cycle.results.every((result) => result.dryRun === true), true);
});

test("automation cycle marks task timeout as failed alert", async () => {
  const cycle = await runAutomationCycle({
    now: new Date("2026-07-06T17:00:00Z"),
    taskTimeoutMs: 1,
    runTask: async () => new Promise((resolve) => setTimeout(() => resolve({ ok: true }), 20)),
  });
  assert.equal(cycle.ok, false);
  assert.equal(cycle.results[0]?.ok, false);
  assert.equal(cycle.results[0]?.timedOut, true);
  assert.equal(cycle.alerts[0]?.type, "TASK_FAILED");
});

test("automation entry alerts include row details, ask-cap drops, and ambiguous downside", () => {
  const alerts = meaningfulAlerts([{
    task: "entry-audit",
    ok: true,
    result: {
      summary: {
        strictSourceConfirmedTakerCount: 1,
        nearBoundaryPassiveBidCount: 1,
        rangeSpreadPaperCount: 1,
      },
      actionablePlans: [{
        company: "Stripe",
        eventSlug: "stripe-event",
        marketSlug: "stripe-175",
        threshold: 175_000_000_000,
        direction: "UP",
        entryMode: "MAKER_NEAR_BOUNDARY_BID",
        distancePct: 0.006,
        yesAsk: 0.54,
        yesBid: 0.4,
        passiveBidPrice: 0.56,
        modelFair: 0.68,
        blockers: [],
        reason: "near_boundary_passive_bid_paper_only",
      }, {
        company: "Stripe",
        eventSlug: "stripe-event",
        marketSlug: "stripe-range",
        pairedMarketSlug: "stripe-180",
        threshold: 175_000_000_000,
        direction: "UP",
        entryMode: "RANGE_SPREAD_PAPER",
        yesAsk: 0.2,
        passiveBidPrice: 0.45,
        modelFair: 0.6,
        blockers: [],
        reason: "adjacent_threshold_range_spread_paper_only",
      }],
      plans: [{
        company: "Anthropic",
        eventSlug: "anthropic-event",
        marketSlug: "anthropic-crossed",
        threshold: 1_100_000_000_000,
        direction: "UP",
        entryMode: "TAKER_SOURCE_CONFIRMED",
        yesAsk: 0.81,
        maxTakerPrice: 0.94,
        modelFair: 1,
        liveEligible: true,
        blockers: [],
        reason: "source_confirmed_stale_yes_taker",
      }, {
        company: "Anthropic",
        eventSlug: "anthropic-event",
        marketSlug: "anthropic-blocked-crossed",
        threshold: 1_200_000_000_000,
        direction: "UP",
        entryMode: "TAKER_SOURCE_CONFIRMED",
        yesAsk: 0.81,
        maxTakerPrice: 0.94,
        modelFair: 1,
        liveEligible: false,
        blockers: ["depth_under_taker_cap_below_minimum"],
        reason: "source_confirmed_but_live_blocked",
      }, {
        company: "Stripe",
        eventSlug: "stripe-event",
        marketSlug: "stripe-down",
        threshold: 170_000_000_000,
        direction: "UNKNOWN",
        entryMode: "WATCH_ONLY",
        distancePct: 0.01,
        yesAsk: 0.4,
        modelFair: 0,
        blockers: ["direction_semantics_unknown"],
        reason: "watch_ladder_leg_no_entry",
      }],
    },
  }]);
  const types = alerts.map((alert) => alert.type);
  assert.equal(types.includes("SOURCE_CONFIRMED_STALE_YES_PLAN"), true);
  assert.equal(types.includes("NEAR_BOUNDARY_PASSIVE_BID_PLAN"), true);
  assert.equal(types.includes("RANGE_SPREAD_PAPER_PLAN"), true);
  assert.equal(types.includes("ASK_BELOW_ENTRY_CAP"), true);
  assert.equal(types.includes("DOWNSIDE_SEMANTICS_AMBIGUOUS"), true);
  const askAlert = alerts.find((alert) => alert.type === "ASK_BELOW_ENTRY_CAP") as Record<string, unknown>;
  assert.equal(Array.isArray(askAlert.rows), true);
  assert.equal((askAlert.rows as Record<string, unknown>[])[0]?.marketSlug, "stripe-175");
  const sourceAlert = alerts.find((alert) => alert.type === "SOURCE_CONFIRMED_STALE_YES_PLAN") as Record<string, unknown>;
  assert.equal(sourceAlert.count, 1);
  assert.equal((sourceAlert.rows as Record<string, unknown>[]).some((row) => row.marketSlug === "anthropic-blocked-crossed"), false);
  const downsideAlert = alerts.find((alert) => alert.type === "DOWNSIDE_SEMANTICS_AMBIGUOUS") as Record<string, unknown>;
  assert.equal((downsideAlert.rows as Record<string, unknown>[])[0]?.marketSlug, "stripe-down");
});

test("automation runtime lock prevents overlapping instances and writes heartbeat", async () => {
  const stateDir = await mkdtemp(join(tmpdir(), "valuation-auto-test-"));
  const config = normalizeConfig({
    stateDir,
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
  const lock = await acquireAutomationLock(config);
  await assert.rejects(() => acquireAutomationLock(config), /already running/);
  await writeAutomationHeartbeat(config, { phase: "FIXING_WINDOW", ok: true });
  const heartbeat = JSON.parse(await readFile(automationHeartbeatPath(config), "utf8")) as Record<string, unknown>;
  assert.equal(heartbeat.phase, "FIXING_WINDOW");
  assert.equal(heartbeat.ok, true);
  await lock.release();
  const nextLock = await acquireAutomationLock(config);
  await nextLock.release();
});

function testConfig(): StrategyConfig {
  return normalizeConfig({
    events: [thresholdEvent("Anthropic")],
    companies: [{ name: "Anthropic", npmCompanyId: "company-a" }],
  });
}

function thresholdEvent(companyName: string): EventConfig {
  return {
    slug: "test-event",
    kind: "threshold",
    companyName,
    deadlineIso: "2026-08-01T03:59:59Z",
    marketWindowStartIso: "2026-06-29T00:00:00Z",
  };
}

function marketFixture(question: string, slug: string): Record<string, unknown> {
  return {
    question,
    slug,
    outcomes: "[\"Yes\",\"No\"]",
    clobTokenIds: "[\"yes-token\",\"no-token\"]",
    active: true,
    closed: false,
    acceptingOrders: true,
    liquidityNum: 500,
  };
}

function legFixture(overrides: Partial<ValuationLeg>): ValuationLeg {
  return {
    eventSlug: "event",
    marketSlug: overrides.marketSlug ?? "market",
    question: "Will Anthropic's valuation hit $1.1T by July 31?",
    eventKind: "threshold",
    company: "Anthropic",
    deadlineIso: overrides.deadlineIso ?? "2026-08-01T03:59:59Z",
    marketWindowStartIso: "2026-06-29T00:00:00Z",
    threshold: overrides.threshold,
    thresholdText: "$1.1T",
    yesTokenId: "yes-token",
    noTokenId: "no-token",
    active: true,
    closed: false,
    acceptingOrders: true,
    liquidity: 500,
    ruleText: "reaches or exceeds the listed amount",
    ruleHash: "rule-hash",
    ruleFamilyHash: "rule-family-hash",
    parseStatus: "ok",
    ...overrides,
  };
}

function rankingLegFixture(company: string, ranking: 1 | 2 | 3): ValuationLeg {
  return {
    ...legFixture({ threshold: undefined, marketSlug: `${company}-ranking` }),
    eventKind: "ranking",
    company,
    label: "RANKING",
    ranking,
    parseStatus: "ok",
  };
}

function quoteFixture(bestAsk: number, bestBid = Math.max(0, bestAsk - 0.03), overrides: Partial<BookQuote> = {}): BookQuote {
  return {
    tokenId: "yes-token",
    bestBid,
    bestAsk,
    spread: Math.max(0, bestAsk - bestBid),
    liquidity: 500,
    fetchedAt: "2026-07-05T00:00:00Z",
    bids: [{ price: bestBid, size: 100 }],
    asks: [
      { price: bestAsk, size: 100 },
      { price: Math.min(0.99, bestAsk + 0.02), size: 200 },
    ],
    ...overrides,
  };
}

function freshQuoteFixture(bestAsk: number, bestBid = Math.max(0, bestAsk - 0.03)): BookQuote {
  return {
    ...quoteFixture(bestAsk, bestBid),
    fetchedAt: new Date().toISOString(),
  };
}

function candidateFixture(overrides: Partial<ValuationCandidate> = {}): ValuationCandidate {
  return {
    signalType: "SOURCE_CONFIRMED_YES",
    status: "candidate",
    company: "Anthropic",
    eventSlug: "event",
    marketSlug: "market",
    deadline: "2026-08-01T03:59:59Z",
    threshold: 1_000,
    direction: "UP",
    yesTokenId: "yes-token",
    sourceValuation: 1_010,
    sourceDate: "2026-07-06",
    maxEligibleValuation: 1_010,
    maxEligibleDate: "2026-07-06",
    distancePct: -0.01,
    yesAsk: 0.81,
    bestBid: 0.78,
    spread: 0.03,
    liquidity: 500,
    depthUnderCap: 250,
    bookAgeMs: 1_000,
    fairPrice: 1,
    edge: 0.19,
    confidence: 10,
    confidenceScore: 10,
    edgeScore: 19,
    maxPrice: 0.94,
    orderUsd: 10,
    orderTemplate: {
      tokenId: "yes-token",
      side: "BUY",
      outcome: "YES",
      orderType: "FAK",
      amountUsd: 10,
      maxPrice: 0.94,
      posted: false,
    },
    liveAllowed: true,
    reason: "source_confirmed",
    ruleHash: "rule-hash",
    ...overrides,
  };
}

function ladderPaperOrderFixture(overrides: Partial<LadderPaperOrder> = {}): LadderPaperOrder {
  return {
    id: "paper-order",
    company: "Anthropic",
    eventSlug: "event",
    marketSlug: "paper-maker",
    threshold: 1_000,
    deadline: "2026-08-01T03:59:59Z",
    entryMode: "MAKER_NEAR_BOUNDARY_BID",
    openedAt: "2026-07-06T00:00:00Z",
    sourceDate: "2026-07-06",
    currentValuation: 1_010,
    maxEligibleValuation: 1_010,
    distancePct: -0.01,
    passiveBidPrice: 0.56,
    modelFair: 0.68,
    requiredEdge: 0.12,
    sizeUsd: 1,
    status: "resolved",
    filledAt: "2026-07-06T00:05:00Z",
    fillPrice: 0.56,
    currentMarkPrice: 1,
    finalResolution: true,
    hypotheticalPnl: 0.05,
    cancelReason: null,
    reason: "near_boundary_passive_bid_paper_only",
    ...overrides,
  };
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function marketAuditRowFixture(overrides: Partial<ReturnType<typeof buildMarketAuditRow>> = {}): ReturnType<typeof buildMarketAuditRow> {
  return {
    company: "Anthropic",
    eventSlug: "event",
    marketSlug: "market",
    threshold: 1_100_000_000_000,
    deadline: "2026-08-01T03:59:59Z",
    label: "HIGH",
    state: "NEWLY_CROSSED",
    crossedQuality: "SOURCE_CONFIRMED_AND_STALE",
    latestValuation: 1_101_000_000_000,
    latestDate: "2026-07-01",
    maxEligibleValuation: 1_101_000_000_000,
    maxEligibleDate: "2026-07-01",
    previousMaxEligibleValuation: 1_090_000_000_000,
    sourceDateAgeHours: 24,
    yesAsk: 0.81,
    yesBid: 0.78,
    settlementEdge: 0.19,
    distancePct: 0.001,
    depthUnderCap: 100,
    bookAgeMs: 1_000,
    ruleConfidence: 10,
    tradeScore: 100,
    tradeBand: "tradeable",
    liveBlockers: [],
    reason: "source_confirmed_and_stale",
    ...overrides,
  };
}

function forecastRowFixture(overrides: Partial<ReturnType<typeof buildNpmBarrierForecasts>[number]> = {}): ReturnType<typeof buildNpmBarrierForecasts>[number] {
  return {
    company: "OpenAI",
    eventSlug: "event",
    marketSlug: "forecast-market",
    threshold: 1_000,
    deadline: "2026-08-01T03:59:59Z",
    state: "UNCROSSED",
    latestValuation: 980,
    latestDate: "2026-07-06",
    maxEligibleValuation: 980,
    maxEligibleDate: "2026-07-06",
    distancePct: 0.02,
    daysRemaining: 25,
    sourceDateAgeHours: 12,
    freshnessState: "MISSED_EXPECTED_UPDATE",
    expectedNextUpdateAt: "2026-07-07T12:00:00Z",
    lastSuccessfulFetchAt: "2026-07-06T17:00:00Z",
    endpointChangedSinceLastFetch: false,
    tapeAdvancedSinceLastFetch: false,
    carryForwardLikely: false,
    dailyDrift: 0.002,
    medianDailyDrift: 0.002,
    recent3DayAvgDailyDrift: 0.002,
    recent7DayAvgDailyDrift: 0.002,
    dailyVol: 0.005,
    maxDailyMove: 0.01,
    pCrossTomorrow: 0.2,
    pTouchByDeadline: 0.62,
    yesAsk: 0.55,
    yesBid: 0.52,
    modelFairPrice: 0.62,
    edge: 0.07,
    confidenceScore: 0.55,
    depthUnderCap: 250,
    signalType: "NO_FORECAST_EDGE",
    liveEligible: false,
    reason: "forecast_edge_below_minimum",
    needed: ["modelFairPrice - yesAsk must be >= 0.12"],
    paperTrade: {
      forecastTime: "2026-07-06T17:00:00Z",
      entryPrice: 0.55,
      nextNpmFixingResult: null,
      thresholdTouched: null,
      marketPriceAfterFixing: null,
      finalResolution: null,
      hypotheticalPnl: null,
    },
    ...overrides,
  };
}
