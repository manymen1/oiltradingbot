from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polybot.core.execution import PaperTradingAdapter
from polybot.core.fees import FEE_POLICY_VERSION, FeeScheduleSnapshot
from polybot.core.portfolio import PortfolioLink
from polybot.discovery.sources import source_plan_sha256
from polybot.discovery.types import MarketContext, SourcePlan

from .contracts import (
    DECISION_PROOF_SCHEMA_VERSION,
    TRADE_INTENT_SCHEMA_VERSION,
    DecisionProof,
    EvidenceClaim,
    RuleEvaluation,
    RuleSpec,
    TradeIntent,
)


@dataclass(frozen=True)
class ConfirmationPolicy:
    min_edge: float = 0.05
    max_entry_price: float = 0.90
    slippage_buffer: float = 0.01
    resolution_risk_buffer: float = 0.02
    resolution_risk_scale: float = 0.05
    uncertainty_buffer: float = 0.03
    requested_usd: float = 50.0
    exit_price_buffer: float = 0.01
    max_fee_schedule_age_hours: float = 24.0

    def validate(self) -> None:
        values = {
            "min_edge": self.min_edge,
            "max_entry_price": self.max_entry_price,
            "slippage_buffer": self.slippage_buffer,
            "resolution_risk_buffer": self.resolution_risk_buffer,
            "resolution_risk_scale": self.resolution_risk_scale,
            "uncertainty_buffer": self.uncertainty_buffer,
            "requested_usd": self.requested_usd,
            "exit_price_buffer": self.exit_price_buffer,
            "max_fee_schedule_age_hours": self.max_fee_schedule_age_hours,
        }
        for name, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} must be numeric")
            if float(value) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.min_edge > 1 or self.max_entry_price > 1:
            raise ValueError("min_edge and max_entry_price must not exceed 1")
        if self.requested_usd <= 0:
            raise ValueError("requested_usd must be positive")


@dataclass(frozen=True)
class DecisionDraft:
    intent: TradeIntent
    executable_bid: float | None
    executable_ask: float | None
    fee_schedule_status: str
    fee_schedule_sha256: str
    fee_schedule: dict[str, Any]
    fee_buffer: float
    slippage_buffer: float
    resolution_risk_buffer: float
    uncertainty_buffer: float
    minimum_edge: float
    net_edge: float | None


class ConfirmationDecisionEngine:
    """Convert terminal deterministic evaluations into paper-only intents."""

    def __init__(
        self,
        *,
        context: MarketContext,
        spec: RuleSpec,
        adapter: PaperTradingAdapter,
        portfolio: PortfolioLink,
        policy: ConfirmationPolicy,
        execution_enabled: bool = True,
    ) -> None:
        policy.validate()
        spec.validate_context_binding(context)
        self.context = context
        self.spec = spec
        self.adapter = adapter
        self.portfolio = portfolio
        self.policy = policy
        self.execution_enabled = execution_enabled

    def decide(
        self,
        evaluation: RuleEvaluation,
        *,
        created_at: str | None = None,
    ) -> DecisionDraft:
        evaluation.validate_spec_binding(self.spec)
        now = created_at or datetime.now(timezone.utc).isoformat()
        decision_time = _parse_aware(now)
        if not self.execution_enabled:
            return self._no_action(
                evaluation,
                [
                    "rule_family_paper_execution_disabled:"
                    f"{self.spec.semantics.rule_family}"
                ],
                now,
            )
        if evaluation.blockers or not evaluation.terminal:
            return self._no_action(
                evaluation,
                list(evaluation.blockers)
                or [f"nonterminal_evidence:{evaluation.evidence_state}"],
                now,
            )
        binding = next(
            (
                item
                for item in self.spec.outcomes
                if item.name == evaluation.outcome_name
            ),
            None,
        )
        if binding is None:
            return self._no_action(
                evaluation,
                ["evaluation_outcome_not_bound"],
                now,
            )
        fee_schedule, fee_blockers = self._fee_schedule(
            binding.name,
            as_of=decision_time,
        )
        desired_side = (
            "YES"
            if evaluation.evidence_state == "TERMINAL_YES"
            else "NO"
            if evaluation.evidence_state == "TERMINAL_NO"
            else "NONE"
        )
        if desired_side == "NONE":
            return self._no_action(
                evaluation,
                ["terminal_evaluation_has_no_trade_side"],
                now,
            )
        if self.spec.kind == "grouped" and desired_side == "NO":
            # A categorical winner proves other legs false, but buying every
            # losing NO leg would multiply one signal into correlated exposure.
            # Existing wrong-side positions can still be exited.
            desired_entry_disabled = True
        else:
            desired_entry_disabled = False

        position = self.adapter.query_live_position(
            binding.yes_token_id,
            binding.no_token_id,
        )
        desired_shares = (
            position.yes_shares
            if desired_side == "YES"
            else position.no_shares
        )
        opposite_shares = (
            position.no_shares
            if desired_side == "YES"
            else position.yes_shares
        )
        if opposite_shares > 0:
            held_side = "NO" if desired_side == "YES" else "YES"
            token_id = (
                binding.no_token_id
                if held_side == "NO"
                else binding.yes_token_id
            )
            bid = self._bid(binding, held_side)
            intent = TradeIntent.from_dict(
                TradeIntent(
                    schema_version=TRADE_INTENT_SCHEMA_VERSION,
                    market_id=self.spec.market_id,
                    rule_spec_sha256=self.spec.spec_sha256,
                    evaluation_sha256=evaluation.evaluation_sha256,
                    outcome_name=binding.name,
                    action=f"EXIT_{held_side}",
                    side=held_side,
                    token_id=token_id,
                    estimated_probability=0.0,
                    max_price=0.0,
                    allocation_usd=0.0,
                    paper_only=True,
                    blockers=[] if bid is not None else ["exit_quote_unavailable"],
                    created_at=now,
                ).as_dict()
            )
            return DecisionDraft(
                intent=intent,
                executable_bid=bid,
                executable_ask=self._ask(binding, held_side),
                **_fee_fields(fee_schedule),
                fee_buffer=0.0,
                slippage_buffer=self.policy.exit_price_buffer,
                resolution_risk_buffer=0.0,
                uncertainty_buffer=0.0,
                minimum_edge=0.0,
                net_edge=None,
            )
        if desired_shares > 0:
            return self._hold(evaluation, binding.name, desired_side, now)
        if desired_entry_disabled:
            return self._no_action(
                evaluation,
                ["grouped_terminal_no_entry_disabled"],
                now,
            )
        if self._market_has_other_position(binding.name):
            return self._no_action(
                evaluation,
                ["other_outcome_position_open"],
                now,
            )

        ask = self._ask(binding, desired_side)
        bid = self._bid(binding, desired_side)
        blockers: list[str] = list(fee_blockers)
        fee_buffer = (
            fee_schedule.fee_per_share(ask)
            if ask is not None and fee_schedule is not None
            else 0.0
        )
        risk_extra = (
            self.context.rule_analysis.resolution_risk
            * self.policy.resolution_risk_scale
            if self.context.rule_analysis is not None
            else 0.0
        )
        resolution_buffer = self.policy.resolution_risk_buffer + risk_extra
        total_buffers = (
            fee_buffer
            + self.policy.slippage_buffer
            + resolution_buffer
            + self.policy.uncertainty_buffer
        )
        max_price = min(
            self.policy.max_entry_price,
            max(0.0, 1.0 - total_buffers - self.policy.min_edge),
        )
        edge = (
            round(1.0 - ask - total_buffers, 6)
            if ask is not None
            else None
        )
        if ask is None:
            blockers.append("entry_quote_unavailable")
        else:
            if ask > self.policy.max_entry_price:
                blockers.append(f"price_above_cap:{ask}")
            if ask > max_price:
                blockers.append(f"confirmation_edge_below_minimum:{edge}")

        allocation = 0.0
        if not blockers:
            allocation, portfolio_blockers = self.portfolio.allowed(
                self.policy.requested_usd
            )
            blockers.extend(portfolio_blockers)
            if allocation <= 0 and not portfolio_blockers:
                blockers.append("portfolio_allocation_zero")
        action = f"ENTER_{desired_side}" if not blockers else "NO_ACTION"
        side = desired_side if action != "NO_ACTION" else "NONE"
        token_id = (
            binding.yes_token_id
            if desired_side == "YES"
            else binding.no_token_id
        )
        intent = TradeIntent.from_dict(
            TradeIntent(
                schema_version=TRADE_INTENT_SCHEMA_VERSION,
                market_id=self.spec.market_id,
                rule_spec_sha256=self.spec.spec_sha256,
                evaluation_sha256=evaluation.evaluation_sha256,
                outcome_name=binding.name,
                action=action,
                side=side,
                token_id=token_id if action != "NO_ACTION" else "",
                estimated_probability=1.0,
                max_price=max_price,
                allocation_usd=allocation if not blockers else 0.0,
                paper_only=True,
                blockers=sorted(set(blockers)),
                created_at=now,
            ).as_dict()
        )
        return DecisionDraft(
            intent=intent,
            executable_bid=bid,
            executable_ask=ask,
            **_fee_fields(fee_schedule),
            fee_buffer=round(fee_buffer, 6),
            slippage_buffer=self.policy.slippage_buffer,
            resolution_risk_buffer=round(resolution_buffer, 6),
            uncertainty_buffer=self.policy.uncertainty_buffer,
            minimum_edge=self.policy.min_edge,
            net_edge=edge,
        )

    def execute(self, draft: DecisionDraft) -> dict[str, Any]:
        intent = draft.intent
        if not intent.paper_only:
            raise ValueError("generic rules engine accepts paper-only intents")
        if intent.blockers or intent.action in {"NO_ACTION", "HOLD"}:
            return {
                "paper": True,
                "executed": False,
                "reason": (
                    ",".join(intent.blockers)
                    if intent.blockers
                    else intent.action.lower()
                ),
            }
        binding = next(
            item
            for item in self.spec.outcomes
            if item.name == intent.outcome_name
        )
        if intent.action in {"ENTER_YES", "ENTER_NO"}:
            self.portfolio.reserve(intent.allocation_usd)
            result = (
                self.adapter.buy_yes_fak(
                    intent.token_id,
                    intent.allocation_usd,
                    intent.max_price,
                )
                if intent.action == "ENTER_YES"
                else self.adapter.buy_no_fak(
                    intent.token_id,
                    intent.allocation_usd,
                    intent.max_price,
                )
            )
            fill = self.adapter.verify_fill(result, intent.token_id)
            if fill.filled_shares <= 0:
                self.portfolio.settle(None)
                self.portfolio.release()
                return {**dict(result), "executed": False}
            actual_cost = _actual_cost(result)
            self.portfolio.reconcile_entry_basis(
                intent.allocation_usd,
                actual_cost,
            )
            return {**dict(result), "executed": True}

        position = self.adapter.query_live_position(
            binding.yes_token_id,
            binding.no_token_id,
        )
        shares = (
            position.yes_shares
            if intent.action == "EXIT_YES"
            else position.no_shares
        )
        floor = max(
            0.0,
            (draft.executable_bid or 0.0) - self.policy.exit_price_buffer,
        )
        result = (
            self.adapter.sell_yes_fak(intent.token_id, shares, floor)
            if intent.action == "EXIT_YES"
            else self.adapter.sell_no_fak(intent.token_id, shares, floor)
        )
        fill = self.adapter.verify_fill(result, intent.token_id)
        remaining = max(0.0, shares - fill.filled_shares)
        proceeds = float(result.get("proceeds_usd") or 0.0)
        if fill.filled_shares > 0 and remaining <= 1e-9:
            self.portfolio.settle(proceeds)
            self.portfolio.release()
        elif fill.filled_shares > 0:
            self.portfolio.reduce_basis(proceeds)
        return {
            **dict(result),
            "executed": fill.filled_shares > 0,
            "remaining_shares": remaining,
        }

    def proof(
        self,
        *,
        source_plan: SourcePlan,
        evaluation: RuleEvaluation,
        draft: DecisionDraft,
        claims: list[EvidenceClaim],
        execution_result: dict[str, Any],
        created_at: str | None = None,
    ) -> DecisionProof:
        relevant = {
            claim.claim_sha256: claim
            for claim in claims
            if claim.claim_sha256 in evaluation.claim_sha256s
        }
        proof = DecisionProof(
            schema_version=DECISION_PROOF_SCHEMA_VERSION,
            market_id=self.spec.market_id,
            rule_text_sha256=self.spec.rule_text_sha256,
            rule_spec_sha256=self.spec.spec_sha256,
            source_plan_sha256=source_plan_sha256(source_plan),
            evaluation_sha256=evaluation.evaluation_sha256,
            intent_sha256=draft.intent.intent_sha256,
            claim_sha256s=sorted(relevant),
            article_ids=sorted(
                {claim.article_id for claim in relevant.values()}
            ),
            source_domains=sorted(
                {claim.source_domain for claim in relevant.values()}
            ),
            independence_groups=sorted(
                {
                    claim.independence_group
                    for claim in relevant.values()
                }
            ),
            supporting_quotes=sorted(
                {
                    claim.supporting_quote
                    for claim in relevant.values()
                }
            ),
            clauses_satisfied=sorted(
                {
                    clause
                    for claim in relevant.values()
                    for clause in claim.clauses_satisfied
                }
            ),
            clauses_violated=sorted(
                {
                    clause
                    for claim in relevant.values()
                    for clause in claim.clauses_violated
                }
            ),
            outcome_name=draft.intent.outcome_name,
            action=draft.intent.action,
            side=draft.intent.side,
            token_id=draft.intent.token_id,
            estimated_probability=draft.intent.estimated_probability,
            executable_bid=draft.executable_bid,
            executable_ask=draft.executable_ask,
            fee_policy_version=FEE_POLICY_VERSION,
            fee_schedule_status=draft.fee_schedule_status,
            fee_schedule_sha256=draft.fee_schedule_sha256,
            fee_schedule=dict(draft.fee_schedule),
            fee_buffer=draft.fee_buffer,
            slippage_buffer=draft.slippage_buffer,
            resolution_risk_buffer=draft.resolution_risk_buffer,
            uncertainty_buffer=draft.uncertainty_buffer,
            minimum_edge=draft.minimum_edge,
            net_edge=draft.net_edge,
            allocation_usd=draft.intent.allocation_usd,
            paper_only=True,
            blockers=list(draft.intent.blockers),
            executed=bool(execution_result.get("executed")),
            execution_result=dict(execution_result),
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
        )
        return DecisionProof.from_dict(proof.as_dict())

    def _market_has_other_position(self, target_outcome: str) -> bool:
        for item in self.spec.outcomes:
            if item.name == target_outcome:
                continue
            position = self.adapter.query_live_position(
                item.yes_token_id,
                item.no_token_id,
            )
            if position.yes_shares > 0 or position.no_shares > 0:
                return True
        return False

    def _ask(self, binding: Any, side: str) -> float | None:
        return (
            self.adapter.yes_best_ask(binding.yes_token_id)
            if side == "YES"
            else self.adapter.no_best_ask(binding.no_token_id)
        )

    def _bid(self, binding: Any, side: str) -> float | None:
        return (
            self.adapter.yes_best_bid(binding.yes_token_id)
            if side == "YES"
            else self.adapter.no_best_bid(binding.no_token_id)
        )

    def _fee_schedule(
        self,
        outcome_name: str,
        *,
        as_of: datetime,
    ) -> tuple[FeeScheduleSnapshot | None, list[str]]:
        outcome = next(
            (
                item
                for item in self.context.outcomes
                if item.name == outcome_name
            ),
            None,
        )
        if outcome is None:
            return None, ["fee_schedule_outcome_not_bound"]
        schedule = outcome.fee_schedule
        if schedule is None:
            reason = outcome.fee_schedule_error or "metadata_missing"
            return None, [f"fee_schedule_unavailable:{reason}"]
        return schedule, schedule.entry_blockers(
            as_of=as_of,
            max_age_hours=self.policy.max_fee_schedule_age_hours,
        )

    def _fee_schedule_for_proof(
        self,
        outcome_name: str,
    ) -> FeeScheduleSnapshot | None:
        outcome = next(
            (
                item
                for item in self.context.outcomes
                if item.name == outcome_name
            ),
            None,
        )
        return outcome.fee_schedule if outcome is not None else None

    def _no_action(
        self,
        evaluation: RuleEvaluation,
        blockers: list[str],
        now: str,
    ) -> DecisionDraft:
        intent = TradeIntent.from_dict(
            TradeIntent(
                schema_version=TRADE_INTENT_SCHEMA_VERSION,
                market_id=self.spec.market_id,
                rule_spec_sha256=self.spec.spec_sha256,
                evaluation_sha256=evaluation.evaluation_sha256,
                outcome_name=evaluation.outcome_name,
                action="NO_ACTION",
                side="NONE",
                token_id="",
                estimated_probability=(
                    1.0
                    if evaluation.evidence_state == "TERMINAL_YES"
                    else 0.0
                    if evaluation.evidence_state == "TERMINAL_NO"
                    else 0.5
                ),
                max_price=0.0,
                allocation_usd=0.0,
                paper_only=True,
                blockers=sorted(set(blockers)),
                created_at=now,
            ).as_dict()
        )
        return DecisionDraft(
            intent=intent,
            executable_bid=None,
            executable_ask=None,
            **_fee_fields(
                self._fee_schedule_for_proof(evaluation.outcome_name)
            ),
            fee_buffer=0.0,
            slippage_buffer=self.policy.slippage_buffer,
            resolution_risk_buffer=self.policy.resolution_risk_buffer,
            uncertainty_buffer=self.policy.uncertainty_buffer,
            minimum_edge=self.policy.min_edge,
            net_edge=None,
        )

    def _hold(
        self,
        evaluation: RuleEvaluation,
        outcome: str,
        side: str,
        now: str,
    ) -> DecisionDraft:
        intent = TradeIntent.from_dict(
            TradeIntent(
                schema_version=TRADE_INTENT_SCHEMA_VERSION,
                market_id=self.spec.market_id,
                rule_spec_sha256=self.spec.spec_sha256,
                evaluation_sha256=evaluation.evaluation_sha256,
                outcome_name=outcome,
                action="HOLD",
                side=side,
                token_id="",
                estimated_probability=1.0,
                max_price=0.0,
                allocation_usd=0.0,
                paper_only=True,
                blockers=[],
                created_at=now,
            ).as_dict()
        )
        return DecisionDraft(
            intent=intent,
            executable_bid=None,
            executable_ask=None,
            **_fee_fields(self._fee_schedule_for_proof(outcome)),
            fee_buffer=0.0,
            slippage_buffer=0.0,
            resolution_risk_buffer=0.0,
            uncertainty_buffer=0.0,
            minimum_edge=0.0,
            net_edge=None,
        )


def _actual_cost(result: dict[str, Any]) -> float:
    for key in ("total_cost_usd", "gross_cost_usd", "requested_usd"):
        try:
            value = float(result.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    raise ValueError("paper fill is missing actual cost")


def _fee_fields(
    schedule: FeeScheduleSnapshot | None,
) -> dict[str, Any]:
    if schedule is None:
        return {
            "fee_schedule_status": "UNAVAILABLE",
            "fee_schedule_sha256": "",
            "fee_schedule": {},
        }
    return {
        "fee_schedule_status": "VERIFIED",
        "fee_schedule_sha256": schedule.schedule_sha256,
        "fee_schedule": schedule.as_dict(),
    }


def _parse_aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("decision timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("decision timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "ConfirmationDecisionEngine",
    "ConfirmationPolicy",
    "DecisionDraft",
]
