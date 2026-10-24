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

    # ---- hooks used by the pack-agnostic API in app/main.py ----
    @property
    def decision_error(self):
        from packs.health_preauth.actions import DecisionError

        return DecisionError

    def seed(self, rt, history: bool = True) -> dict:
        from packs.health_preauth import actions

        return actions.seed_demo(rt, history)

    def decide(self, rt, case_id: str, role: str, actor: str, action: str, amount=None, reason: str = "") -> dict:
        from packs.health_preauth import actions

        return actions.human_decision(rt, case_id, role, actor, action, amount=amount, reason=reason)

    def case_summary(self, rt, case: dict, role: str) -> dict:
        from packs.health_preauth import actions

        return actions.case_summary(rt, case, role)

    def case_view(self, rt, case: dict, role: str) -> dict:
        from packs.health_preauth import actions

        return actions.case_view(rt, case, role)

    def dashboard(self, rt) -> dict:
        from packs.health_preauth import actions

        return actions.dashboard(rt)

    def mask_outbox(self, rt, rows: list[dict]) -> list[dict]:
        from packs.health_preauth import actions

        out = []
        for row in rows:
            case = rt.cases.get(row["case_id"])
            out.append(actions._mask(row, case["state"].get("intake", {}).get("phi_map", {})) if case else row)
        return out

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
