"""
Scenario Simulator — what-if SoS recalculation under budget weight adjustments.

Use case:
  A client asks: "What happens to my SoS if Brand X doubles their spend?"
  Or: "If we cut our budget by 30%, how much share do we cede?"

The simulator accepts the current report's competitors list plus a dict of
weight multipliers per brand (1.0 = no change, 2.0 = doubled, 0.5 = halved),
then recalculates SoS proportionally across the adjusted competitive set.

Methodology note:
  This is a ceteris paribus (all-else-equal) model: it adjusts the named brand's
  spend proportionally and recalculates shares. It does not model competitive
  response (i.e. it assumes other brands hold spend constant). This is consistent
  with standard media mix scenario planning methodologies (e.g. Nielsen Optimizer,
  Kantar XTEL). Clients should be advised of this assumption.

Usage (Python):
  from core.scenario_sim import simulate_sos
  result = simulate_sos(brands=report["competitors"], adjustments={"Nike": 1.5, "Adidas": 0.7})

Usage (API):
  POST /simulate
  {"adjustments": {"Nike": 1.5, "Adidas": 0.7}}
  → returns competitors array with recalculated sos_pct values
"""


def simulate_sos(brands: list[dict], adjustments: dict[str, float]) -> list[dict]:
    """
    Recalculate SoS for all brands under the given spend weight adjustments.

    Args:
        brands: list of competitor dicts from report["competitors"].
                Each must have "name" and "estimated_spend_usd".
        adjustments: {brand_name: multiplier} — brands not listed default to 1.0.

    Returns:
        New list of competitor dicts with updated "estimated_spend_usd", "sos_pct",
        "scenario_multiplier", and "scenario_note" fields.
        Original SoS values are preserved in "baseline_sos_pct" for comparison.
    """
    import copy
    if not brands:
        return []

    adjusted = []
    for b in brands:
        name       = b.get("name", "Unknown")
        base_spend = float(b.get("estimated_spend_usd") or 0.0)
        multiplier = float(adjustments.get(name, 1.0))
        new_spend  = round(base_spend * multiplier, 2)

        entry = copy.deepcopy(b)  # W7: deepcopy prevents mutating the original report dict
        entry["baseline_sos_pct"]        = b.get("sos_pct", 0.0)
        entry["estimated_spend_usd"]     = new_spend
        entry["scenario_multiplier"]     = multiplier
        entry["scenario_note"] = (
            f"Spend adjusted ×{multiplier:.2f}: ${base_spend:,.2f} → ${new_spend:,.2f}"
            if multiplier != 1.0
            else "No adjustment applied (baseline)."
        )
        adjusted.append(entry)

    total_adjusted_spend = sum(b["estimated_spend_usd"] for b in adjusted)

    for b in adjusted:
        b["sos_pct"] = (
            round(b["estimated_spend_usd"] / total_adjusted_spend * 100, 1)
            if total_adjusted_spend > 0 else 0.0
        )
        b["sos_delta_pct"] = round(b["sos_pct"] - b["baseline_sos_pct"], 1)

    return sorted(adjusted, key=lambda x: x["sos_pct"], reverse=True)


def scenario_summary(brands: list[dict], adjustments: dict[str, float]) -> dict:
    """
    Run simulate_sos and return a structured summary including methodology notes.
    Used by the /simulate API endpoint.
    """
    result = simulate_sos(brands, adjustments)
    total_spend = sum(b["estimated_spend_usd"] for b in result)

    movers = [
        {
            "brand":         b["name"],
            "baseline_sos":  b["baseline_sos_pct"],
            "new_sos":       b["sos_pct"],
            "delta":         b["sos_delta_pct"],
            "multiplier":    b["scenario_multiplier"],
            "note":          b["scenario_note"],
        }
        for b in result
        if b["scenario_multiplier"] != 1.0 or b["sos_delta_pct"] != 0.0
    ]

    return {
        "methodology": (
            "Ceteris paribus (all-else-equal) spend weight adjustment. "
            "Named brands' estimated spend is multiplied by the given factor; "
            "other brands hold constant. SoS is recalculated proportionally. "
            "This model does not simulate competitive response. "
            "Consistent with Nielsen Optimizer and Kantar XTEL scenario planning approaches."
        ),
        "adjustments_applied": adjustments,
        "total_competitive_spend_usd": round(total_spend, 2),
        "movers": movers,
        "competitors": result,
    }
