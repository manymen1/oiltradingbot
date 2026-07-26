import "dotenv/config";

import { createSecureClient, OrderSide, OrderType, type SecureClient } from "@polymarket/client";
import type { PrepareMarketOrderRequest } from "@polymarket/client/actions";
import { privateKey } from "@polymarket/client/viem";
import { type ApiKeyCreds } from "@polymarket/bindings/clob";
import { http, type Hex } from "viem";
import { polygon } from "viem/chains";
import type { BookQuote, StrategyConfig, ValuationCandidate } from "./signalTypes.ts";
import { claimCandidateLock, hasLiveAck, isCandidateLocked, lockCandidate, probePath, releaseCandidateLock, writeJson } from "./stateStore.ts";
import { betaProbeMetadata, validatePostedProbeForCandidate } from "./probeValidation.ts";

export type ExecutionResult = {
  posted: boolean;
  skipped?: boolean;
  reason?: string;
  response?: unknown;
};

export async function executeCandidate(
  candidate: ValuationCandidate,
  config: StrategyConfig,
  configHash: string,
): Promise<ExecutionResult> {
  const mode = config.mode;
  if (mode === "off") return { posted: false, skipped: true, reason: "operator_mode_off" };
  const policyBlockers = sourceConfirmedLivePolicyBlockers(candidate, config);
  if (policyBlockers.length > 0) return { posted: false, skipped: true, reason: policyBlockers.join(",") };
  if (!candidate.liveAllowed) {
    return { posted: false, skipped: true, reason: "candidate_alert_or_not_live_allowed" };
  }
  if (mode === "alert_only") return { posted: false, skipped: true, reason: "operator_mode_alert_only" };
  if (mode === "dry_run") return { posted: false, skipped: true, reason: "operator_mode_dry_run" };
  if (!await hasLiveAck(config, configHash)) return { posted: false, skipped: true, reason: "missing_live_config_ack" };
  if (await isCandidateLocked(config, candidate)) return { posted: false, skipped: true, reason: "duplicate_lock" };
  const probe = await validatePostedProbeForCandidate(config, candidate);
  if (!probe.ok) return { posted: false, skipped: true, reason: probe.blockers.join(",") };
  requireMutationAllowed();
  if (!await claimCandidateLock(config, candidate)) return { posted: false, skipped: true, reason: "duplicate_lock" };
  try {
    const response = await placeFakBuy(requiredToken(candidate), candidate.orderUsd, candidate.maxPrice);
    if (isZeroFillResponse(response)) {
      await releaseCandidateLock(config, candidate);
      return { posted: false, skipped: true, reason: "zero_fill", response };
    }
    await lockCandidate(config, candidate, response);
    return { posted: true, response };
  } catch (error) {
    await releaseCandidateLock(config, candidate);
    throw error;
  }
}

export function isZeroFillResponse(response: unknown): boolean {
  if (!response || typeof response !== "object") return false;
  const record = response as Record<string, unknown>;
  return record.zeroFill === true || (record.rejected === true && Number(record.filledShares ?? record.filled_shares ?? 0) <= 0);
}

export function sourceConfirmedLivePolicyBlockers(candidate: ValuationCandidate, config: StrategyConfig): string[] {
  const blockers: string[] = [];
  if (candidate.signalType !== "SOURCE_CONFIRMED_YES") blockers.push("source_confirmed_stale_yes_only_live_policy");
  if (candidate.signalType === "SOURCE_CONFIRMED_YES" && candidate.direction !== "UP") {
    blockers.push("direction_semantics_not_up");
  }
  if (candidate.threshold === undefined || candidate.maxEligibleValuation === undefined) {
    blockers.push("missing_source_confirmation_fields");
  } else if (candidate.maxEligibleValuation < candidate.threshold) {
    blockers.push("source_not_confirmed");
  }
  if (candidate.yesAsk === null || candidate.yesAsk === undefined) {
    blockers.push("missing_yes_ask");
  } else if (candidate.yesAsk > candidate.maxPrice) {
    blockers.push("best_ask_above_cap");
  }
  if (candidate.depthUnderCap === undefined) {
    blockers.push("missing_depth_under_cap");
  } else if (candidate.depthUnderCap < config.minLiquidity) {
    blockers.push("depth_under_taker_cap_below_minimum");
  }
  if (candidate.bookAgeMs === undefined) {
    blockers.push("missing_orderbook_age");
  } else if (candidate.bookAgeMs > config.orderbookMaxAgeMs) {
    blockers.push("orderbook_stale");
  }
  return [...new Set(blockers)];
}

export async function postedProbe(
  tokenId: string,
  quote: BookQuote,
  marketSlug: string,
  config: StrategyConfig,
  price = 0.001,
  amountUsd = 1,
): Promise<ExecutionResult> {
  requireMutationAllowed();
  if (quote.bestAsk !== null && quote.bestAsk <= price) {
    throw new Error(`probe price ${price} would cross best ask ${quote.bestAsk}; refusing posted probe`);
  }
  const response = await placeFakBuy(tokenId, amountUsd, price);
  const result = { posted: true, response };
  const timestamp = new Date().toISOString();
  await writeJson(probePath(config, marketSlug), {
    ok: true,
    timestamp,
    probedAt: timestamp,
    ...betaProbeMetadata(),
    tokenId,
    marketSlug,
    price,
    amountUsd,
    quote,
    response,
  });
  return result;
}

async function placeFakBuy(tokenId: string, amountUsd: number, maxPrice: number): Promise<unknown> {
  const client = await buildClient();
  const request: PrepareMarketOrderRequest = {
    tokenId,
    side: OrderSide.BUY,
    amount: amountUsd,
    maxPrice,
    orderType: OrderType.FAK as OrderType.FAK,
  };
  try {
    return await client.placeMarketOrder(request);
  } catch (error) {
    if (error instanceof Error && /no orders found to match/i.test(error.message)) {
      return { rejected: true, zeroFill: true, reason: error.message };
    }
    throw error;
  }
}

async function buildClient(): Promise<SecureClient> {
  const chainId = Number(process.env.CHAIN_ID ?? "137");
  if (chainId !== 137) throw new Error(`only Polygon mainnet is configured, got CHAIN_ID=${chainId}`);
  return createSecureClient({
    signer: privateKey(requiredHex("PRIVATE_KEY"), {
      chain: polygon,
      transport: http(process.env.POLYGON_RPC_URL ?? process.env.RPC_URL),
    }),
    credentials: {
      key: requiredEnv("CLOB_API_KEY"),
      secret: requiredEnv("CLOB_SECRET"),
      passphrase: requiredEnv("CLOB_PASS_PHRASE"),
    } as ApiKeyCreds,
  });
}

function requiredToken(candidate: ValuationCandidate): string {
  const token = candidate.yesTokenId;
  if (!token) throw new Error("candidate missing YES token id");
  return token;
}

function requireMutationAllowed(): void {
  if (process.env.POLYBOT_TS_BRIDGE_ALLOW_POST !== "1") {
    throw new Error("live beta SDK posting requires POLYBOT_TS_BRIDGE_ALLOW_POST=1");
  }
}

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value?.trim()) throw new Error(`${name} is required`);
  return value.trim();
}

function requiredHex(name: string): Hex {
  const value = requiredEnv(name);
  if (!/^0x[0-9a-fA-F]+$/.test(value)) throw new Error(`${name} must be a hex string`);
  return value as Hex;
}
