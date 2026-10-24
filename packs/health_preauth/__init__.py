"""Health pre-authorization pack: pack.yaml + rules + tools + agents + prompts, plugged into core/."""
from __future__ import annotations

from pathlib import Path

import yaml

from core.orchestrator import Step

HERE = Path(__file__).resolve().parent


class HealthPreauthPack:
    name = "health_preauth"

    def __init__(self) -> None:
        from packs.health_preauth import agents

        self.config = yaml.safe_load((HERE / "pack.yaml").read_text(encoding="utf-8"))
        self.replay_path = HERE / "data" / "replay_cache.json"
        self.pipeline = [
            Step("intake", agents.intake_agent, "intake"),
            Step("policy", agents.policy_agent, "policy"),
            Step("docs", agents.docs_agent, "docs"),
            Step("risk", agents.risk_agent, "risk"),
            Step("decide", agents.decide_step, "orchestrator", always=True),
            Step("explain", agents.explain_step, "orchestrator"),
            Step("act", agents.act_step, "action", always=True),
        ]

    def register_actions(self, rt) -> None:
        from packs.health_preauth import actions

        actions.register(rt)

    def audit_summary(self, step: str, out: dict) -> dict:
        """A few fields per step for the audit chain (full output stays in the case state)."""
        if step == "intake":
            i = out["intake"]
            return {"injection_detected": i["guard"]["detected"], "confidence": i["confidence"], "phi_tokens": len(i["phi_map"])}
        if step == "policy":
            ev = out["policy"]["evaluation"]
            return {"eligible_amount": ev["eligible_amount"], "failed_rules": ev["failed_rules"]}
        if step == "docs":
            return {"missing_docs": out["docs"]["missing_docs"], "cost_deviation_pct": out["docs"]["cost_deviation_pct"]}
        if step == "risk":
            return {"risk_score": out["risk"]["score"]}
        if step == "decide":
            d = out["decision"]
            return {"outcome": d["outcome"], "recommended_amount": d["recommended_amount"]}
        if step == "explain":
            return {"grounded": out["explanation"]["grounded"]}
        return {}


PACK = HealthPreauthPack()
