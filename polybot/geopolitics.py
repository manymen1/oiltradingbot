from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="polybot-geopolitics")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_iran_parser = sub.add_parser("inspect-iran")
    inspect_iran_parser.add_argument("--config", required=True)
    inspect_iran_position_parser = sub.add_parser("inspect-iran-position")
    inspect_iran_position_parser.add_argument("--config", required=True)
    preflight_iran_parser = sub.add_parser("preflight-iran")
    preflight_iran_parser.add_argument("--config", required=True)
    preflight_iran_parser.add_argument("--live", action="store_true")
    ack_iran_live_parser = sub.add_parser("ack-iran-live")
    ack_iran_live_parser.add_argument("--config", required=True)
    ack_iran_live_parser.add_argument("--note", default="")
    set_iran_mode_parser = sub.add_parser("set-iran-mode")
    set_iran_mode_parser.add_argument("--config", required=True)
    set_iran_mode_parser.add_argument("--mode", required=True, choices=["off", "alert_only", "dry_run", "live"])
    probe_iran_v2_parser = sub.add_parser("probe-iran-clob-v2")
    probe_iran_v2_parser.add_argument("--config", required=True)
    probe_iran_v2_parser.add_argument("--amount", type=float, default=5.0)
    probe_iran_v2_parser.add_argument("--price", type=float)
    probe_iran_v2_parser.add_argument("--post", action="store_true")
    smoke_iran_classifier_parser = sub.add_parser("smoke-iran-classifier")
    smoke_iran_classifier_parser.add_argument("--config", required=True)
    smoke_iran_classifier_parser.add_argument("--url")
    smoke_iran_classifier_parser.add_argument("--text")
    smoke_iran_classifier_parser.add_argument("--title", default="classifier smoke")
    smoke_iran_classifier_parser.add_argument("--domain", default="reuters.com")
    run_iran_parser = sub.add_parser("run-iran")
    run_iran_parser.add_argument("--config", required=True)
    run_iran_parser.add_argument("--live", action="store_true")

    inspect_location_parser = sub.add_parser("inspect-location")
    inspect_location_parser.add_argument("--config", required=True)
    preflight_location_parser = sub.add_parser("preflight-location")
    preflight_location_parser.add_argument("--config", required=True)
    preflight_location_parser.add_argument("--live", action="store_true")
    ack_location_live_parser = sub.add_parser("ack-location-live")
    ack_location_live_parser.add_argument("--config", required=True)
    ack_location_live_parser.add_argument("--note", default="")
    set_location_mode_parser = sub.add_parser("set-location-mode")
    set_location_mode_parser.add_argument("--config", required=True)
    set_location_mode_parser.add_argument("--mode", required=True, choices=["off", "alert_only", "dry_run", "live"])
    smoke_location_classifier_parser = sub.add_parser("smoke-location-classifier")
    smoke_location_classifier_parser.add_argument("--config", required=True)
    smoke_location_classifier_parser.add_argument("--url")
    smoke_location_classifier_parser.add_argument("--text")
    smoke_location_classifier_parser.add_argument("--title", default="classifier smoke")
    smoke_location_classifier_parser.add_argument("--domain", default="reuters.com")
    run_location_parser = sub.add_parser("run-location-protection")
    run_location_parser.add_argument("--config", required=True)
    run_location_parser.add_argument("--live", action="store_true")
    evaluate_location_parser = sub.add_parser("evaluate-location-forecast")
    evaluate_location_parser.add_argument("--config", required=True)
    evaluate_location_parser.add_argument("--resolved-outcome", required=True)
    evaluate_location_parser.add_argument("--state")

    inspect_binary_parser = sub.add_parser("inspect-binary")
    inspect_binary_parser.add_argument("--config", required=True)
    preflight_binary_parser = sub.add_parser("preflight-binary")
    preflight_binary_parser.add_argument("--config", required=True)
    preflight_binary_parser.add_argument("--live", action="store_true")
    ack_binary_live_parser = sub.add_parser("ack-binary-live")
    ack_binary_live_parser.add_argument("--config", required=True)
    ack_binary_live_parser.add_argument("--note", default="")
    set_binary_mode_parser = sub.add_parser("set-binary-mode")
    set_binary_mode_parser.add_argument("--config", required=True)
    set_binary_mode_parser.add_argument("--mode", required=True, choices=["off", "alert_only", "dry_run", "live"])
    smoke_binary_classifier_parser = sub.add_parser("smoke-binary-classifier")
    smoke_binary_classifier_parser.add_argument("--config", required=True)
    smoke_binary_classifier_parser.add_argument("--url")
    smoke_binary_classifier_parser.add_argument("--text")
    smoke_binary_classifier_parser.add_argument("--title", default="classifier smoke")
    smoke_binary_classifier_parser.add_argument("--domain", default="reuters.com")
    run_binary_parser = sub.add_parser("run-binary")
    run_binary_parser.add_argument("--config", required=True)
    run_binary_parser.add_argument("--live", action="store_true")

    discover_markets_parser = sub.add_parser("discover-markets")
    discover_markets_parser.add_argument("--config", required=True)
    grade_markets_parser = sub.add_parser("grade-markets")
    grade_markets_parser.add_argument("--config", required=True)
    compile_rules_parser = sub.add_parser("compile-rules")
    compile_rules_parser.add_argument("--config", required=True)
    compile_rules_parser.add_argument("--market")
    inspect_rule_parser = sub.add_parser("inspect-rule")
    inspect_rule_parser.add_argument("--config", required=True)
    inspect_rule_parser.add_argument("--market", required=True)
    validate_rule_parser = sub.add_parser("validate-rule")
    validate_rule_parser.add_argument("--spec", required=True)
    consensus_report_parser = sub.add_parser("consensus-report")
    consensus_report_parser.add_argument("--config", required=True)
    consensus_report_parser.add_argument("--market")
    prepare_rule_review_parser = sub.add_parser("prepare-rule-review")
    prepare_rule_review_parser.add_argument("--config", required=True)
    prepare_rule_review_parser.add_argument("--market", required=True)
    prepare_rule_review_parser.add_argument("--pass-sha256", required=True)
    prepare_rule_review_parser.add_argument("--out")
    import_reviewed_rule_parser = sub.add_parser("import-reviewed-rule")
    import_reviewed_rule_parser.add_argument("--config", required=True)
    import_reviewed_rule_parser.add_argument("--market", required=True)
    import_reviewed_rule_parser.add_argument("--spec", required=True)
    import_reviewed_rule_parser.add_argument("--reviewer", required=True)
    import_reviewed_rule_parser.add_argument("--note", required=True)
    import_reviewed_rule_parser.add_argument(
        "--approve-spec-sha256",
        required=True,
    )
    run_rule_market_parser = sub.add_parser("run-rule-market")
    run_rule_market_parser.add_argument("--config", required=True)
    run_rule_market_parser.add_argument("--market", required=True)
    run_rule_market_parser.add_argument("--once", action="store_true")
    run_rule_market_parser.add_argument("--live", action="store_true")
    inspect_rule_market_parser = sub.add_parser("inspect-rule-market")
    inspect_rule_market_parser.add_argument("--config", required=True)
    inspect_rule_market_parser.add_argument("--market", required=True)
    forward_completeness_parser = sub.add_parser(
        "forward-completeness"
    )
    forward_completeness_parser.add_argument("--config", required=True)
    forward_completeness_parser.add_argument("--market", required=True)
    forward_timeline_parser = sub.add_parser("build-forward-timeline")
    forward_timeline_parser.add_argument("--config", required=True)
    forward_timeline_parser.add_argument("--market", required=True)
    forward_timeline_parser.add_argument("--out")
    rotate_forward_parser = sub.add_parser("rotate-forward-recorder")
    rotate_forward_parser.add_argument("--config", required=True)
    rotate_forward_parser.add_argument("--archive-dir")
    plan_sources_parser = sub.add_parser("plan-sources")
    plan_sources_parser.add_argument("--config", required=True)
    plan_sources_parser.add_argument("--market")
    semantic_coverage_parser = sub.add_parser("semantic-coverage")
    semantic_coverage_parser.add_argument("--config", required=True)
    semantic_coverage_parser.add_argument(
        "--all-contexts",
        action="store_true",
    )
    scan_opportunities_parser = sub.add_parser("scan-opportunities")
    scan_opportunities_parser.add_argument("--config", required=True)
    emit_bot_config_parser = sub.add_parser("emit-bot-config")
    emit_bot_config_parser.add_argument("--config", required=True)
    emit_bot_config_parser.add_argument("--market", required=True)
    emit_bot_config_parser.add_argument("--out")
    funnel_report_parser = sub.add_parser("funnel-report")
    funnel_report_parser.add_argument("--config", required=True)
    priority_report_parser = sub.add_parser("priority-report")
    priority_report_parser.add_argument("--config", required=True)
    economics_report_parser = sub.add_parser("economics-report")
    economics_report_parser.add_argument("--config", required=True)
    profit_funnel_parser = sub.add_parser("profit-funnel")
    profit_funnel_parser.add_argument("--config", required=True)
    run_discovery_parser = sub.add_parser("run-discovery")
    run_discovery_parser.add_argument("--config", required=True)
    run_discovery_parser.add_argument("--once", action="store_true")
    reconcile_ledger_parser = sub.add_parser("reconcile-ledger")
    reconcile_ledger_parser.add_argument("--config", required=True)
    calibration_report_parser = sub.add_parser("calibration-report")
    calibration_report_parser.add_argument("--config", required=True)
    record_resolution_parser = sub.add_parser("record-resolution")
    record_resolution_parser.add_argument("--config", required=True)
    record_resolution_parser.add_argument("--market", required=True)
    record_resolution_parser.add_argument("--outcome", required=True)
    record_resolution_parser.add_argument("--resolved", required=True, choices=["yes", "no"])
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--config", required=True)
    replay_parser.add_argument("--articles", required=True)
    replay_parser.add_argument("--limit", type=int, default=0)
    replay_rule_parser = sub.add_parser("replay-rule-market")
    replay_rule_parser.add_argument("--config", required=True)
    replay_rule_parser.add_argument("--market", required=True)
    replay_rule_parser.add_argument("--timeline", required=True)
    replay_rule_parser.add_argument("--out")
    replay_rule_parser.add_argument("--labels")
    replay_rule_parser.add_argument(
        "--dataset-role",
        choices=["development", "frozen_oos", "forward"],
        default="development",
    )
    promotion_report_parser = sub.add_parser("rule-promotion-report")
    promotion_report_parser.add_argument("--config", required=True)
    promotion_report_parser.add_argument("--runs", required=True)
    promotion_report_parser.add_argument("--out")
    fleet_status_parser = sub.add_parser("fleet-status")
    fleet_status_parser.add_argument("--config", required=True)
    eval_classifier_parser = sub.add_parser("eval-classifier")
    eval_classifier_parser.add_argument("--config", required=True)
    eval_classifier_parser.add_argument("--cases", required=True)
    latency_report_parser = sub.add_parser("latency-report")
    latency_report_parser.add_argument("--logs", default="logs")
    latency_report_parser.add_argument("--data", default="data")
    trades_report_parser = sub.add_parser("trades-report")
    trades_report_parser.add_argument("--data", default="data")
    trades_report_parser.add_argument("--ledger")
    run_fleet_parser = sub.add_parser("run-fleet")
    run_fleet_parser.add_argument("--config", required=True)
    run_fleet_parser.add_argument("--live", action="store_true")
    run_fleet_parser.add_argument("--once", action="store_true")
    set_fleet_mode_parser = sub.add_parser("set-fleet-mode")
    set_fleet_mode_parser.add_argument("--mode", required=True, choices=["off", "alert_only", "dry_run", "live"])

    args = parser.parse_args(argv)
    if args.command == "inspect-iran":
        from .iran.runner import inspect_iran_command

        return inspect_iran_command(Path(args.config))
    if args.command == "inspect-iran-position":
        from .iran.runner import inspect_iran_position_command

        return inspect_iran_position_command(Path(args.config))
    if args.command == "preflight-iran":
        from .iran.runner import preflight_iran_command

        return preflight_iran_command(Path(args.config), live_flag=args.live)
    if args.command == "ack-iran-live":
        from .iran.runner import ack_iran_live_command

        return ack_iran_live_command(Path(args.config), note=args.note)
    if args.command == "set-iran-mode":
        from .iran.runner import set_iran_mode_command

        return set_iran_mode_command(Path(args.config), mode=args.mode)
    if args.command == "probe-iran-clob-v2":
        from .iran.runner import probe_iran_clob_v2_command

        return probe_iran_clob_v2_command(Path(args.config), amount=args.amount, post=args.post, price=args.price)
    if args.command == "smoke-iran-classifier":
        from .iran.runner import smoke_iran_classifier_command

        return smoke_iran_classifier_command(Path(args.config), url=args.url, text=args.text, title=args.title, domain=args.domain)
    if args.command == "run-iran":
        from .iran.runner import run_iran_command

        return run_iran_command(Path(args.config), live_flag=args.live)
    if args.command == "inspect-location":
        from .location.runner import inspect_location_command

        return inspect_location_command(Path(args.config))
    if args.command == "preflight-location":
        from .location.runner import preflight_location_command

        return preflight_location_command(Path(args.config), live_flag=args.live)
    if args.command == "ack-location-live":
        from .location.runner import ack_location_live_command

        return ack_location_live_command(Path(args.config), note=args.note)
    if args.command == "set-location-mode":
        from .location.runner import set_location_mode_command

        return set_location_mode_command(Path(args.config), mode=args.mode)
    if args.command == "smoke-location-classifier":
        from .location.runner import smoke_location_classifier_command

        return smoke_location_classifier_command(Path(args.config), url=args.url, text=args.text, title=args.title, domain=args.domain)
    if args.command == "run-location-protection":
        from .location.runner import run_location_command

        return run_location_command(Path(args.config), live_flag=args.live)
    if args.command == "evaluate-location-forecast":
        from .location.calibration import evaluate_forecast_command

        return evaluate_forecast_command(
            Path(args.config),
            args.resolved_outcome,
            state_path=Path(args.state) if args.state else None,
        )
    if args.command == "inspect-binary":
        from .binary.runner import inspect_binary_command

        return inspect_binary_command(Path(args.config))
    if args.command == "preflight-binary":
        from .binary.runner import preflight_binary_command

        return preflight_binary_command(Path(args.config), live_flag=args.live)
    if args.command == "ack-binary-live":
        from .binary.runner import ack_binary_live_command

        return ack_binary_live_command(Path(args.config), note=args.note)
    if args.command == "set-binary-mode":
        from .binary.runner import set_binary_mode_command

        return set_binary_mode_command(Path(args.config), mode=args.mode)
    if args.command == "smoke-binary-classifier":
        from .binary.runner import smoke_binary_classifier_command

        return smoke_binary_classifier_command(Path(args.config), url=args.url, text=args.text, title=args.title, domain=args.domain)
    if args.command == "run-binary":
        from .binary.runner import run_binary_command

        return run_binary_command(Path(args.config), live_flag=args.live)
    if args.command == "discover-markets":
        from .discovery.runner import discover_markets_command

        return discover_markets_command(Path(args.config))
    if args.command == "grade-markets":
        from .discovery.runner import grade_markets_command

        return grade_markets_command(Path(args.config))
    if args.command == "compile-rules":
        from .discovery.runner import compile_rules_command

        return compile_rules_command(
            Path(args.config),
            market_id=args.market,
        )
    if args.command == "inspect-rule":
        from .discovery.runner import inspect_rule_command

        return inspect_rule_command(Path(args.config), args.market)
    if args.command == "validate-rule":
        from .discovery.runner import validate_rule_command

        return validate_rule_command(Path(args.spec))
    if args.command == "prepare-rule-review":
        from .rules.review import prepare_rule_review_command

        return prepare_rule_review_command(
            Path(args.config),
            args.market,
            args.pass_sha256,
            out=Path(args.out) if args.out else None,
        )
    if args.command == "consensus-report":
        from .rules.review import consensus_report_command

        return consensus_report_command(
            Path(args.config),
            market_id=args.market or "",
        )
    if args.command == "import-reviewed-rule":
        from .rules.review import import_reviewed_rule_command

        return import_reviewed_rule_command(
            Path(args.config),
            args.market,
            Path(args.spec),
            reviewer=args.reviewer,
            note=args.note,
            approved_spec_sha256=args.approve_spec_sha256,
        )
    if args.command == "run-rule-market":
        from .rules.runner import run_generic_rule_market_command

        return run_generic_rule_market_command(
            Path(args.config),
            args.market,
            once=args.once,
            live_flag=args.live,
        )
    if args.command == "inspect-rule-market":
        from .rules.runner import inspect_generic_rule_market_command

        return inspect_generic_rule_market_command(
            Path(args.config),
            args.market,
        )
    if args.command == "forward-completeness":
        from .rules.forward import forward_completeness_command

        return forward_completeness_command(
            Path(args.config),
            args.market,
        )
    if args.command == "build-forward-timeline":
        from .rules.forward import build_forward_timeline_command

        return build_forward_timeline_command(
            Path(args.config),
            args.market,
            out=Path(args.out) if args.out else None,
        )
    if args.command == "rotate-forward-recorder":
        from .rules.forward import rotate_forward_recorder_command

        return rotate_forward_recorder_command(
            Path(args.config),
            archive_dir=(
                Path(args.archive_dir) if args.archive_dir else None
            ),
        )
    if args.command == "plan-sources":
        from .discovery.runner import plan_sources_command

        return plan_sources_command(Path(args.config), market_id=args.market)
    if args.command == "semantic-coverage":
        from .discovery.runner import semantic_coverage_command

        return semantic_coverage_command(
            Path(args.config),
            all_contexts=args.all_contexts,
        )
    if args.command == "scan-opportunities":
        from .discovery.runner import scan_opportunities_command

        return scan_opportunities_command(Path(args.config))
    if args.command == "emit-bot-config":
        from .discovery.runner import emit_bot_config_command

        return emit_bot_config_command(Path(args.config), args.market, out=Path(args.out) if args.out else None)
    if args.command == "funnel-report":
        from .discovery.runner import funnel_report_command

        return funnel_report_command(Path(args.config))
    if args.command == "priority-report":
        from .discovery.profit_priority import priority_report_command

        return priority_report_command(Path(args.config))
    if args.command == "economics-report":
        from .discovery.profit_priority import economics_report_command

        return economics_report_command(Path(args.config))
    if args.command == "profit-funnel":
        from .discovery.profit_funnel import profit_funnel_command

        return profit_funnel_command(Path(args.config))
    if args.command == "run-discovery":
        from .discovery.runner import run_discovery_command

        return run_discovery_command(Path(args.config), once=args.once)
    if args.command == "reconcile-ledger":
        from .discovery.runner import reconcile_ledger_command

        return reconcile_ledger_command(Path(args.config))
    if args.command == "calibration-report":
        from .discovery.runner import calibration_report_command

        return calibration_report_command(Path(args.config))
    if args.command == "record-resolution":
        from .discovery.runner import record_resolution_command

        return record_resolution_command(Path(args.config), args.market, args.outcome, args.resolved)
    if args.command == "replay":
        from .replay import replay_articles_command

        return replay_articles_command(Path(args.config), Path(args.articles), limit=args.limit)
    if args.command == "replay-rule-market":
        from .rules.replay import replay_rule_market_command

        return replay_rule_market_command(
            Path(args.config),
            args.market,
            Path(args.timeline),
            out=Path(args.out) if args.out else None,
            dataset_role=args.dataset_role,
            labels_path=Path(args.labels) if args.labels else None,
        )
    if args.command == "rule-promotion-report":
        from .rules.promotion import promotion_report_command

        return promotion_report_command(
            Path(args.config),
            Path(args.runs),
            out=Path(args.out) if args.out else None,
        )
    if args.command == "fleet-status":
        from .discovery.runner import fleet_status_command

        return fleet_status_command(Path(args.config))
    if args.command == "eval-classifier":
        from .evalset import eval_classifier_command

        return eval_classifier_command(Path(args.config), Path(args.cases))
    if args.command == "latency-report":
        from .analysis import latency_report_command

        return latency_report_command(logs_dir=Path(args.logs), data_root=Path(args.data))
    if args.command == "trades-report":
        from .analysis import trades_report_command

        return trades_report_command(data_root=Path(args.data), ledger_path=Path(args.ledger) if args.ledger else None)
    if args.command == "run-fleet":
        from .discovery.fleet import run_fleet_command

        return run_fleet_command(Path(args.config), live=args.live, once=args.once)
    if args.command == "set-fleet-mode":
        from .discovery.fleet import set_fleet_mode_command

        return set_fleet_mode_command(args.mode)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
