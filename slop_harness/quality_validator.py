"""Quality validation for slop_harness and simulation generated records.

Validates JSON schema, diversity metrics, duplicate detection, action validity,
and vibe override persistence. Generates quality reports and supports CI threshold checks.

Usage:
    from slop_harness.quality_validator import QualityValidator, QualityReport

    validator = QualityValidator()
    report = validator.validate_batch(records)
    report.assert_thresholds()  # raises QualityThresholdError if metrics fail
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from slop_harness.models import ALL_BPMS, ALL_INSTRUMENTS, ALL_KEYS

logger = logging.getLogger(__name__)

# ── Validation thresholds (overridable via CLI/env or per-call) ──────────────

DEFAULT_THRESHOLDS = {
    "min_bpm_coverage": 0.5,  # at least 50% of BPM values appear
    "min_key_coverage": 0.6,  # at least 60% of keys appear
    "min_instrument_coverage": 0.5,  # at least 50% of instruments appear
    "max_duplicate_ratio": 0.05,  # at most 5% duplicate records
    "min_action_validity": 0.95,  # at least 95% of actions within bounds
    "min_vibe_persistence": 0.0,  # vibe persistence ratio (0 = disabled)
}

# Schema constants
VALID_ACTION_TYPES = {"retain", "add", "remove"}
REQUIRED_RESPONSE_FIELDS = {"master_bpm", "master_key", "actions", "reasoning", "name"}
REQUIRED_ACTION_FIELDS = {"action_type"}
REQUIRED_ADD_FIELDS = {"major_family", "sub_family", "model_id"}


# ── Exceptions ─────────────────────────────────────────────────────────────────


class QualityThresholdError(Exception):
    """Raised when a quality metric falls below its threshold."""

    def __init__(self, failures: list[str]):
        self.failures = failures
        msg = "Quality thresholds failed:\n" + "\n".join(f"  - {f}" for f in failures)
        super().__init__(msg)


class ValidationError(Exception):
    """Raised when a record fails structural validation."""


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Result of validating a single record."""

    record_index: int
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    """Aggregated quality report for a batch of records."""

    total_records: int
    valid_records: int
    invalid_records: int
    per_record_results: list[ValidationResult]

    # Diversity metrics
    bpm_distribution: dict[int, int] = field(default_factory=dict)
    key_distribution: dict[str, int] = field(default_factory=dict)
    instruments_coverage: dict[str, int] = field(default_factory=dict)
    bpm_coverage_ratio: float = 0.0
    key_coverage_ratio: float = 0.0
    instrument_coverage_ratio: float = 0.0

    # Duplicate detection
    duplicate_count: int = 0
    duplicate_ratio: float = 0.0
    unique_record_hashes: int = 0

    # Action validity
    total_actions: int = 0
    invalid_actions: int = 0
    action_validity_ratio: float = 0.0
    out_of_bounds_action_types: Counter = field(default_factory=Counter)

    # Vibe persistence (simulation only)
    vibe_persistence_ratio: float = 0.0
    vibe_transitions: int = 0
    vibe_clears: int = 0

    # Quality gate
    thresholds: dict[str, float] = field(default_factory=dict)
    threshold_failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.threshold_failures) == 0

    def assert_thresholds(self) -> None:
        """Raise QualityThresholdError if any threshold failed."""
        if self.threshold_failures:
            raise QualityThresholdError(self.threshold_failures)

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"Quality Report: {self.valid_records}/{self.total_records} valid ({self.invalid_records} invalid)",
            f"  BPM coverage:  {self.bpm_coverage_ratio:.1%} (distinct BPMS: {len(self.bpm_distribution)})",
            f"  Key coverage:  {self.key_coverage_ratio:.1%} (distinct keys: {len(self.key_distribution)})",
            f"  Instrument coverage: {self.instrument_coverage_ratio:.1%} (distinct: {len(self.instruments_coverage)})",
            f"  Duplicates:     {self.duplicate_count} ({self.duplicate_ratio:.1%})",
            f"  Action validity: {self.action_validity_ratio:.1%} ({self.invalid_actions}/{self.total_actions} invalid)",
        ]
        if self.threshold_failures:
            lines.append("  FAILURES:")
            for f in self.threshold_failures:
                lines.append(f"    - {f}")
        else:
            lines.append("  Thresholds: PASSED")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON reports."""
        return {
            "total_records": self.total_records,
            "valid_records": self.valid_records,
            "invalid_records": self.invalid_records,
            "bpm_distribution": dict(self.bpm_distribution),
            "key_distribution": dict(self.key_distribution),
            "instrument_coverage": dict(self.instruments_coverage),
            "bpm_coverage_ratio": self.bpm_coverage_ratio,
            "key_coverage_ratio": self.key_coverage_ratio,
            "instrument_coverage_ratio": self.instrument_coverage_ratio,
            "duplicate_count": self.duplicate_count,
            "duplicate_ratio": self.duplicate_ratio,
            "unique_record_hashes": self.unique_record_hashes,
            "total_actions": self.total_actions,
            "invalid_actions": self.invalid_actions,
            "action_validity_ratio": self.action_validity_ratio,
            "vibe_persistence_ratio": self.vibe_persistence_ratio,
            "threshold_failures": self.threshold_failures,
            "passed": self.passed,
        }


# ── Validator ───────────────────────────────────────────────────────────────────


class QualityValidator:
    """Validates dataset records for the slop_harness/simulation pipelines."""

    def __init__(self, thresholds: dict[str, float] | None = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_batch(
        self,
        records: list[dict[str, Any]],
        active_stems_per_record: list[list[dict]] | None = None,
    ) -> QualityReport:
        """Validate a batch of records and return a QualityReport.

        Args:
            records: list of {"messages": [...], "response": <JSON string | dict>}
            active_stems_per_record: optional parallel list of active stems for each record
                (used for action bounds checking in simulation)
        """
        per_record: list[ValidationResult] = []
        total = len(records)

        # Track diversity/dedup across batch
        bpm_counter: Counter[int] = Counter()
        key_counter: Counter[str] = Counter()
        instrument_counter: Counter[str] = Counter()
        seen_hashes: set[str] = set()
        duplicate_count = 0

        total_actions = 0
        invalid_actions = 0
        oob_types: Counter[str] = Counter()

        for i, record in enumerate(records):
            result = self._validate_single(record)
            per_record.append(result)

            # Diversity & dedup only for valid records (but count response content)
            response_data = self._extract_response(record)
            if response_data is not None:
                bpm = response_data.get("master_bpm")
                if bpm is not None:
                    bpm_counter[int(bpm)] += 1
                key = response_data.get("master_key")
                if key is not None:
                    key_counter[str(key)] += 1
                for action in response_data.get("actions", []):
                    if action.get("action_type") == "add":
                        sub = action.get("sub_family")
                        if sub is not None:
                            instrument_counter[str(sub)] += 1

                # Duplicate detection via content hash
                h = self._hash_response(response_data)
                if h in seen_hashes:
                    duplicate_count += 1
                else:
                    seen_hashes.add(h)

            # Action validity (with stems for bounds checking)
            stems = active_stems_per_record[i] if active_stems_per_record else None
            action_result = self._validate_actions(response_data, stems)
            total_actions += action_result["total"]
            invalid_actions += action_result["invalid"]
            for t in action_result["oob_types"]:
                oob_types[t] += 1

            # Attach action errors to per-record result
            if action_result["errors"]:
                result.warnings.extend(action_result["errors"])

        # Compute coverage ratios
        bpm_cov = self._coverage_ratio(bpm_counter, ALL_BPMS)
        key_cov = self._coverage_ratio(key_counter, ALL_KEYS)
        inst_cov = self._coverage_ratio(instrument_counter, ALL_INSTRUMENTS)

        action_validity = 1.0 - (invalid_actions / total_actions) if total_actions > 0 else 1.0
        dup_ratio = duplicate_count / total if total > 0 else 0.0

        # Check thresholds (skip checks when batch is empty — nothing to validate)
        if total == 0:
            failures = []
        else:
            failures = self._check_thresholds(
                bpm_coverage=bpm_cov,
                key_coverage=key_cov,
                instrument_coverage=inst_cov,
                duplicate_ratio=dup_ratio,
                action_validity=action_validity,
            )

        valid_count = sum(1 for r in per_record if r.valid)

        return QualityReport(
            total_records=total,
            valid_records=valid_count,
            invalid_records=total - valid_count,
            per_record_results=per_record,
            bpm_distribution=dict(bpm_counter),
            key_distribution=dict(key_counter),
            instruments_coverage=dict(instrument_counter),
            bpm_coverage_ratio=bpm_cov,
            key_coverage_ratio=key_cov,
            instrument_coverage_ratio=inst_cov,
            duplicate_count=duplicate_count,
            duplicate_ratio=dup_ratio,
            unique_record_hashes=len(seen_hashes),
            total_actions=total_actions,
            invalid_actions=invalid_actions,
            action_validity_ratio=action_validity,
            out_of_bounds_action_types=oob_types,
            thresholds=dict(self.thresholds),
            threshold_failures=failures,
        )

    # ── Single record validation ──────────────────────────────────────────────

    def _validate_single(self, record: dict[str, Any]) -> ValidationResult:
        """Validate schema of a single record."""
        errors: list[str] = []
        idx = -1  # caller sets index in loop

        # Check top-level structure
        if not isinstance(record, dict):
            return ValidationResult(record_index=idx, valid=False, errors=["record is not a dict"])

        if "messages" not in record:
            errors.append("missing 'messages' field")
        elif not isinstance(record["messages"], list):
            errors.append("'messages' is not a list")
        elif len(record["messages"]) < 3:
            errors.append(f"expected at least 3 messages, got {len(record['messages'])}")

        if "response" not in record:
            errors.append("missing 'response' field")
        else:
            response = record["response"]
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except json.JSONDecodeError as e:
                    errors.append(f"response is not valid JSON: {e}")

            if isinstance(response, dict):
                for required in REQUIRED_RESPONSE_FIELDS:
                    if required not in response:
                        errors.append(f"response missing '{required}'")
                actions = response.get("actions", [])
                if not isinstance(actions, list):
                    errors.append(f"'actions' is not a list: {type(actions).__name__}")
                elif len(actions) == 0:
                    errors.append("actions array is empty")

        return ValidationResult(record_index=idx, valid=len(errors) == 0, errors=errors)

    # ── Action validation ─────────────────────────────────────────────────────

    def _validate_actions(
        self,
        response: dict[str, Any] | None,
        active_stems: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Validate action bounds and types.

        Returns dict with total, invalid, oob_types, errors.
        """
        result = {"total": 0, "invalid": 0, "oob_types": [], "errors": []}

        if response is None:
            return result

        actions = response.get("actions", [])
        if not isinstance(actions, list):
            return result

        stem_count = len(active_stems) if active_stems else 0

        for action in actions:
            result["total"] += 1
            if not isinstance(action, dict):
                result["invalid"] += 1
                result["errors"].append(f"action is not a dict: {action}")
                continue

            a_type = action.get("action_type")
            if a_type not in VALID_ACTION_TYPES:
                result["invalid"] += 1
                result["oob_types"].append(str(a_type))
                result["errors"].append(f"invalid action_type: {a_type}")
                continue

            # Bounds check for retain/remove
            if a_type in ("retain", "remove"):
                idx = action.get("stem_index")
                if idx is None:
                    result["invalid"] += 1
                    result["errors"].append(f"{a_type} action missing stem_index")
                elif not isinstance(idx, int):
                    result["invalid"] += 1
                    result["errors"].append(f"{a_type} stem_index is not int: {idx}")
                elif stem_count > 0 and (idx < 0 or idx >= stem_count):
                    result["invalid"] += 1
                    result["oob_types"].append(a_type)
                    result["errors"].append(f"{a_type} stem_index {idx} out of bounds (stem_count={stem_count})")

            # Required fields for add
            if a_type == "add":
                for required in REQUIRED_ADD_FIELDS:
                    if required not in action:
                        result["invalid"] += 1
                        result["errors"].append(f"add action missing '{required}'")

        return result

    # ── Vibe persistence validation ───────────────────────────────────────────

    def validate_vibe_persistence(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate vibe override persistence across simulation records.

        Checks that vibes appear in user prompts when set and disappear when cleared.
        Returns {"persistence_ratio": float, "transitions": int, "clears": int}
        """
        transitions = 0
        cleares = 0
        total_checks = 0
        correct = 0

        prev_vibe: str | None = None

        for record in records:
            messages = record.get("messages", [])
            user_msg = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")

            response = self._extract_response(record)
            if response is None:
                continue

            actions = response.get("actions", [])

            # Check if vibe clear happened
            has_clear = False
            for action in actions:
                if action.get("action_type") == "remove":
                    # Removing all stems could be a vibe clear signal
                    pass

            # Detect vibe from user message
            current_vibe = None
            if "OVERRIDE:" in user_msg:
                for line in user_msg.split("\n"):
                    if line.startswith("OVERRIDE:"):
                        current_vibe = line[len("OVERRIDE:") :].strip()
                        break
            elif "vibe_clear_prob" in user_msg:
                has_clear = True

            if prev_vibe is not None and current_vibe is None:
                cleares += 1
            elif prev_vibe is None and current_vibe is not None:
                transitions += 1

            if prev_vibe is not None:
                total_checks += 1
                if prev_vibe in user_msg or has_clear:
                    correct += 1

            prev_vibe = current_vibe

        ratio = correct / total_checks if total_checks > 0 else 1.0
        return {"persistence_ratio": ratio, "transitions": transitions, "clears": cleares}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _extract_response(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Extract and parse response from a record."""
        response = record.get("response")
        if response is None:
            return None
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            try:
                return json.loads(response)
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def _hash_response(self, response: dict[str, Any]) -> str:
        """Create deterministic hash of response content for deduplication."""
        canonical = json.dumps(response, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def _coverage_ratio(
        self,
        counter: Counter,
        all_values: list,
    ) -> float:
        """Compute what fraction of all_values appear in counter."""
        if not all_values:
            return 0.0
        observed = sum(1 for v in all_values if counter[v] > 0 or counter[str(v)] > 0)
        return observed / len(all_values)

    def _check_thresholds(
        self,
        *,
        bpm_coverage: float,
        key_coverage: float,
        instrument_coverage: float,
        duplicate_ratio: float,
        action_validity: float,
    ) -> list[str]:
        """Check all thresholds, return list of failure descriptions."""
        failures: list[str] = []

        if bpm_coverage < self.thresholds.get("min_bpm_coverage", 0):
            failures.append(
                f"BPM coverage {bpm_coverage:.1%} below threshold {self.thresholds['min_bpm_coverage']:.1%}"
            )
        if key_coverage < self.thresholds.get("min_key_coverage", 0):
            failures.append(
                f"Key coverage {key_coverage:.1%} below threshold {self.thresholds['min_key_coverage']:.1%}"
            )
        if instrument_coverage < self.thresholds.get("min_instrument_coverage", 0):
            failures.append(
                f"Instrument coverage {instrument_coverage:.1%} below threshold {self.thresholds['min_instrument_coverage']:.1%}"
            )
        if duplicate_ratio > self.thresholds.get("max_duplicate_ratio", 1.0):
            failures.append(
                f"Duplicate ratio {duplicate_ratio:.1%} exceeds threshold {self.thresholds['max_duplicate_ratio']:.1%}"
            )
        if action_validity < self.thresholds.get("min_action_validity", 0):
            failures.append(
                f"Action validity {action_validity:.1%} below threshold {self.thresholds['min_action_validity']:.1%}"
            )

        return failures
