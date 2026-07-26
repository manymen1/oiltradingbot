export type OperatorMode = "off" | "alert_only" | "dry_run" | "live";

export type EventKind = "threshold" | "ranking";

export type SignalType =
  | "SOURCE_CONFIRMED_YES"
  | "CURVE_MONOTONICITY_YES"
  | "CALENDAR_DOMINANCE_YES"
  | "RANKING_INCONSISTENCY_ALERT"
  | "NPM_DRIFT_MODEL_YES"
  | "NPM_NEAR_BOUNDARY_FORECAST_YES"
  | "NPM_MULTI_DAY_BARRIER_FORECAST_YES"
  | "CURVE_UNDERPRICED_FORECAST_YES"
  | "ORDERBOOK_CONFIRMED_FORECAST_YES"
  | "NO_FORECAST_EDGE"
  | "STALE_SOURCE_ALERT"
  | "NO_ACTION";

export type CandidateStatus = "candidate" | "alert" | "skip" | "no_action";

export type CompanyConfig = {
  name: string;
  npmCompanyId?: string;
  aliases?: string[];
};

export type EventConfig = {
  slug: string;
  kind: EventKind;
  companyName?: string;
  deadlineIso: string;
  marketWindowStartIso?: string;
  ranking?: 1 | 2 | 3;
  mode?: OperatorMode;
};

export type StrategyConfig = {
  mode: OperatorMode;
  pollMs: number;
  npmUpdate: {
    timeZone: string;
    hour: number;
    minute: number;
  };
  automation: {
    taskTimeoutMs: number;
    lockTtlMs: number;
    maxBackoffMs: number;
    alertSink: "file" | "console" | "both" | "none";
  };
  logsDir: string;
  stateDir: string;
  orderbookMaxAgeMs: number;
  maxSpread: number;
  minLiquidity: number;
  globalUsdCap: number;
  perEventUsdCap: number;
  perCompanyUsdCap: number;
  perDeadlineUsdCap: number;
  baseOrderUsd: number;
  defaultMaxYesPrice: number;
  minimumEdge: {
    sourceConfirmed: number;
    curve: number;
    calendar: number;
    drift: number;
  };
  signalMultipliers: Record<SignalType, number>;
  maxYesPriceBySignal: Partial<Record<SignalType, number>>;
  events: EventConfig[];
  companies: CompanyConfig[];
};

export type NpmTapePoint = {
  date: string;
  impliedValuation: number;
  price?: number;
};

export type NpmEvidence = {
  company: string;
  npmCompanyId: string;
  sourceUrl: string;
  latestTapeDate: string;
  latestValuation: number;
  latestPrice?: number;
  maxEligibleValuation?: number;
  maxEligibleDate?: string;
  tape: NpmTapePoint[];
  identityOk: boolean;
  rawHash: string;
};

export type GammaEvent = {
  slug: string;
  title: string;
  description: string;
  resolutionSource?: string;
  markets: Record<string, unknown>[];
  rawHash: string;
};

export type ValuationLeg = {
  eventSlug: string;
  marketSlug: string;
  question: string;
  groupItemTitle?: string;
  eventKind: EventKind;
  company: string;
  deadlineIso: string;
  marketWindowStartIso?: string;
  threshold?: number;
  thresholdText?: string;
  label?: "HIGH" | "LOW" | "RANKING";
  ranking?: 1 | 2 | 3;
  yesTokenId?: string;
  noTokenId?: string;
  conditionId?: string;
  active: boolean;
  closed: boolean;
  acceptingOrders: boolean;
  liquidity: number;
  ruleText: string;
  ruleHash: string;
  ruleFamilyHash: string;
  parseStatus: "ok" | "malformed_threshold" | "unsupported";
  parseReason?: string;
};

export type BookQuote = {
  tokenId: string;
  bestBid: number | null;
  bestAsk: number | null;
  spread: number | null;
  liquidity: number;
  fetchedAt: string;
  bids: Array<{ price: number; size: number }>;
  asks: Array<{ price: number; size: number }>;
};

export type CurvePoint = {
  leg: ValuationLeg;
  yesAsk: number;
};

export type ValuationCandidate = {
  signalType: SignalType;
  status: CandidateStatus;
  company: string;
  eventSlug: string;
  marketSlug: string;
  deadline: string;
  threshold?: number;
  direction?: "UP" | "DOWN" | "UNKNOWN";
  yesTokenId?: string;
  sourceValuation?: number;
  sourceDate?: string;
  maxEligibleValuation?: number;
  maxEligibleDate?: string;
  distancePct?: number;
  yesAsk: number | null;
  bestBid: number | null;
  spread: number | null;
  liquidity: number;
  depthUnderCap?: number;
  bookAgeMs?: number;
  fairPrice: number;
  edge: number;
  confidence: number;
  confidenceScore: number;
  edgeScore: number;
  maxPrice: number;
  orderUsd: number;
  orderTemplate?: {
    tokenId: string;
    side: "BUY";
    outcome: "YES";
    orderType: "FAK";
    amountUsd: number;
    maxPrice: number;
    posted: false;
  };
  liveAllowed: boolean;
  reason: string;
  ruleHash: string;
  pairedMarketSlug?: string;
  pairedYesAsk?: number;
};
