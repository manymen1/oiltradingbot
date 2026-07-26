from __future__ import annotations

from pathlib import Path

import pytest

from polybot.binary.config import load_binary_config
from polybot.discovery.config import load_discovery_config
from polybot.iran.config import load_iran_config
from polybot.location.config import load_location_config
from polybot.portfolio import load_portfolio_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _binary_yaml(extra: str = "") -> str:
    return (
        """
market:
  slug: test-market
  deadline_date: "2026-09-30"
  held_side: "YES"
  resolution_rules: Resolves YES on a qualifying event.
"""
        + extra
    )


def _location_yaml(extra: str = "") -> str:
    return (
        """
event:
  slug: test-location
  question: Where is the meeting?
  deadline_date: "2026-09-30"
  held_location: qatar
  resolution_rules: Resolves to the qualifying venue.
outcomes:
  - name: qatar
    label: Qatar
    condition_id: qatar-condition
    yes_token_id: qatar-yes
    no_token_id: qatar-no
  - name: oman
    label: Oman
    condition_id: oman-condition
    yes_token_id: oman-yes
    no_token_id: oman-no
    rotation_target: true
"""
        + extra
    )


def _iran_yaml(extra: str = "") -> str:
    return (
        """
market:
  slug: test-iran
  target_leg: September 30
  held_side: "YES"
  expected_question_contains: September 30
"""
        + extra
    )


def test_duplicate_yaml_key_is_rejected_before_last_value_wins(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _binary_yaml(
            """
execution:
  dry_run: true
  dry_run: false
"""
        ),
    )
    with pytest.raises(ValueError, match="duplicate YAML key 'dry_run'"):
        load_binary_config(path)


def test_unknown_root_and_silently_ignored_nested_keys_are_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unknown keys: allocater"):
        load_discovery_config(
            _write(
                tmp_path,
                """
allocater:
  total_usd: 10
""",
            )
        )
    with pytest.raises(ValueError, match="execution contains unknown keys: dryrun"):
        load_binary_config(
            _write(
                tmp_path,
                _binary_yaml(
                    """
execution:
  dryrun: false
"""
                ),
            )
        )


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf"])
def test_non_finite_risk_caps_fail_closed(tmp_path: Path, value: str) -> None:
    path = _write(
        tmp_path,
        f"""
allocator:
  total_usd: {value}
""",
    )
    with pytest.raises(ValueError, match="allocator.total_usd must be a finite number"):
        load_discovery_config(path)


def test_classifier_pass_and_agreement_invariants_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"classifier\.passes must be at least 1"):
        load_binary_config(
            _write(
                tmp_path,
                _binary_yaml(
                    """
classifier:
  passes: 0
"""
                ),
            )
        )
    with pytest.raises(
        ValueError,
        match=r"classifier\.require_pass_agreement requires passes >= 2",
    ):
        load_binary_config(
            _write(
                tmp_path,
                _binary_yaml(
                    """
classifier:
  passes: 1
  require_pass_agreement: true
"""
                ),
            )
        )


def test_decaying_probability_requires_valid_as_of_metadata(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
opportunity:
  probability_estimates:
    market:
      yes: 0.6
      _decay: true
""",
    )
    with pytest.raises(ValueError, match="_decay requires _as_of"):
        load_discovery_config(path)


def test_quoted_boolean_is_not_truthiness_coerced(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _location_yaml(
            """
execution:
  dry_run: "false"
"""
        ),
    )
    with pytest.raises(ValueError, match=r"execution\.dry_run must be a boolean"):
        load_location_config(path)


def test_location_outcome_token_mappings_must_be_globally_unique(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        _location_yaml().replace("oman-yes", "qatar-yes"),
    )
    with pytest.raises(ValueError, match="token ids must be globally unique"):
        load_location_config(path)


def test_iran_execution_prices_and_trigger_policy_are_validated(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        _iran_yaml(
            """
trigger:
  require_two_sources: true
  trusted_single_source_execution: true
"""
        ),
    )
    with pytest.raises(ValueError, match="require_two_sources conflicts"):
        load_iran_config(path)


def test_portfolio_limits_cannot_be_negative(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
positions:
  - id: protected
    event_slug: event
    held_side: YES
    max_yes_shares_to_sell: -1
""",
    )
    with pytest.raises(
        ValueError,
        match=r"position protected\.max_yes_shares_to_sell must be at least 0",
    ):
        load_portfolio_config(path)


def test_checked_in_active_configs_pass_strict_validation() -> None:
    load_discovery_config(Path("configs/geopolitics/discovery.yaml"))
    load_binary_config(Path("configs/geopolitics/binary-entry.example.yaml"))
    load_location_config(Path("configs/geopolitics/qatar-sept30-yes-protection.yaml"))
    load_iran_config(Path("configs/geopolitics/iran-july17-yes-protection.yaml"))
    load_portfolio_config(Path("configs/geopolitics/positions.example.yaml"))
