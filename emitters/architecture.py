"""Step 02 architecture capture and consistency check -- Agent 2's (Solution Architect) half of
Step 02, alongside Agent 3's intent and Agent 1's gap analysis (emitters/intent.py).

Same split as intent.py: capture_architecture() records what a human (or the SA agent) decided
-- RTO/RPO, platform binding, per-entity SCD strategy, volume expectations, risks. Persisted to
contracts/architecture/<domain>/architecture.yaml, tracked in git like contracts/intent/ (a
human authored it; it isn't derived from probing a live system).

check_consistency() is the mechanical half, and deliberately narrow: it checks only what can be
verified against a FACT the platform already has, never a judgement call.
  - platform_binding: does the declared target match contracts/platform/<target>.yaml, the one
    this compile actually targets (spec["environment"]["platform"])?
  - entity SCD strategy: does a declared entity's scd_type match what the compiled silver model
    actually specifies (compiled["model"]["dimensions"]/["facts"])? And does that SCD type
    require a platform capability (capabilities.scd2_snapshot) the target actually has?
It does NOT try to judge whether an RTO/RPO is "reasonable" for the described workload -- that
is exactly the kind of semantic call CLAUDE.md rule 4 says not to silently make. RTO/RPO are
recorded as evidence for a human to weigh, not graded.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ARCHITECTURE_DIR = REPO_ROOT / "contracts" / "architecture"


def _architecture_path(domain: str) -> pathlib.Path:
    return ARCHITECTURE_DIR / domain / "architecture.yaml"


def load_architecture(domain: str) -> dict[str, Any] | None:
    path = _architecture_path(domain)
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def capture_architecture(domain: str, client: str, architecture: dict[str, Any], captured_by: str) -> pathlib.Path:
    """Overwrites the architecture record for this domain -- a replace, not a merge, same
    reasoning as capture_intent: a revision should show exactly what was submitted."""
    record = {
        "domain": domain, "client": client,
        "rto": architecture.get("rto", ""),
        "rpo": architecture.get("rpo", ""),
        "platform_binding": architecture.get("platform_binding", ""),
        "layering_rationale": architecture.get("layering_rationale", ""),
        "volume_expectations": architecture.get("volume_expectations", ""),
        "entity_scd": architecture.get("entity_scd") or [],
        "risks": architecture.get("risks") or [],
        "captured_by": captured_by,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    path = _architecture_path(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))
    return path


def _platform_capabilities(target: str) -> dict[str, Any]:
    path = REPO_ROOT / "contracts" / "platform" / f"{target}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()).get("capabilities", {})


def check_consistency(domain: str, spec: dict[str, Any], compiled_model: dict[str, Any]) -> dict[str, Any]:
    """Recomputed fresh, never trusted from a stale file -- called at Freeze (G2) time with the
    SAME spec/compiled_model the freeze is about to act on, exactly like accept_catalogue (G1)
    recomputes gap analysis at approval time rather than trusting an earlier click.

    spec: the compiled intake spec (has spec["environment"]["platform"])
    compiled_model: compiled["model"] from intake_compiler.spec_to_contracts() -- has
        "dimensions" and "facts", each with a "type" field (scd1/scd2/transaction/...)
    """
    architecture = load_architecture(domain)
    if architecture is None:
        return {"domain": domain, "architecture_captured": False, "issues": [], "checked_at":
                 _dt.datetime.now(_dt.timezone.utc).isoformat()}

    issues = []
    actual_target = spec.get("environment", {}).get("platform")
    declared_target = architecture.get("platform_binding")
    if declared_target and actual_target and declared_target != actual_target:
        issues.append({
            "kind": "platform_mismatch",
            "detail": f"architecture declares platform_binding={declared_target!r}, but this "
                      f"compile targets {actual_target!r}",
        })

    entities_by_name = {d["name"]: d for d in compiled_model.get("dimensions", [])}
    entities_by_name.update({f["name"]: f for f in compiled_model.get("facts", [])})
    target_caps = _platform_capabilities(actual_target) if actual_target else {}

    for decl in architecture.get("entity_scd", []):
        name, declared_type = decl.get("entity"), decl.get("scd_type")
        if not name:
            continue
        actual = entities_by_name.get(name)
        if actual is None:
            issues.append({"kind": "unknown_entity",
                           "detail": f"architecture declares SCD strategy for {name!r}, but no "
                                     f"such dimension/fact exists in the compiled model -- stale doc?"})
            continue
        actual_type = actual.get("type")
        if declared_type and actual_type and declared_type != actual_type:
            issues.append({"kind": "scd_mismatch",
                           "detail": f"{name}: architecture declares {declared_type!r}, compiled "
                                     f"model specifies {actual_type!r}"})
        if declared_type == "scd2" and target_caps and not target_caps.get("scd2_snapshot", True):
            issues.append({"kind": "capability_gap",
                           "detail": f"{name}: architecture declares scd2, but platform "
                                     f"{actual_target!r} does not support scd2_snapshot"})

    return {
        "domain": domain, "architecture_captured": True, "issues": issues,
        "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
