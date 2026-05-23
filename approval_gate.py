import json
import re


class ApprovalGate:
    def __init__(self, cpm_rate: float = 15.0):
        self.cpm_rate = cpm_rate

    def _extract_json(self, raw: str) -> str:
        """Strip markdown fences and find the outermost JSON object."""
        # Remove ```json ... ``` or ``` ... ``` wrappers from LLM output
        raw = re.sub(r'```(?:json)?\s*', '', raw)
        raw = re.sub(r'```', '', raw)
        # Remove trailing commas before ] or }
        raw = re.sub(r',\s*([\]}])', r'\1', raw)
        return raw.strip()

    def _parse(self, cleaned: str) -> dict:
        # Try direct parse first
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Find outermost {} block
        start = cleaned.find('{')
        end   = cleaned.rfind('}') + 1
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
        # Last resort: wrap as competitors list if it looks like a list
        start = cleaned.find('[')
        end   = cleaned.rfind(']') + 1
        if start != -1 and end > start:
            try:
                lst = json.loads(cleaned[start:end])
                return {"competitors": lst}
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from agent output: {cleaned[:200]}")

    def process_final_report(self, raw_output: str) -> str:
        cleaned = self._extract_json(raw_output)
        data    = self._parse(cleaned)

        # Normalise: ensure competitors key exists
        if "competitors" not in data:
            if isinstance(data, list):
                data = {"competitors": data}
            else:
                for v in data.values():
                    if isinstance(v, list):
                        data = {"competitors": v}
                        break
                else:
                    data = {"competitors": []}

        # Deterministic math — overwrite any hallucinated spend/rate
        total_engagement = 0
        total_spend      = 0.0
        brand_calcs      = []

        for comp in data["competitors"]:
            m         = comp.get("metrics", {})
            likes     = int(m.get("likes",    0) or 0)
            comments  = int(m.get("comments", 0) or 0)
            shares    = int(m.get("shares",   0) or 0)
            views     = int(m.get("views",    0) or 0)
            m.update(likes=likes, comments=comments, shares=shares, views=views)
            comp["metrics"] = m

            engagement = likes + comments + shares
            spend      = round((engagement / 1000) * self.cpm_rate, 2)
            eng_rate   = round((engagement / max(views, 1)) * 100, 2)

            comp["estimated_spend_usd"] = spend
            comp["engagement_rate"]     = eng_rate

            total_engagement += engagement
            total_spend      += spend

            # Ensure required fields exist with safe defaults
            comp.setdefault("name",           comp.get("handle", "Unknown"))
            comp.setdefault("handle",         "")
            comp.setdefault("platform",       "Social Media")
            comp.setdefault("sentiment",      "Neutral")
            comp.setdefault("top_posts",      [])
            comp.setdefault("hashtags",       [])
            comp.setdefault("content_themes", [])

            # Sanitise list fields
            for list_field in ("top_posts", "hashtags", "content_themes"):
                val = comp[list_field]
                if not isinstance(val, list):
                    comp[list_field] = [str(val)] if val else []
                else:
                    comp[list_field] = [str(x) for x in val if x]

            brand_calcs.append({
                "brand":       comp.get("name", "?"),
                "likes":       likes,
                "comments":    comments,
                "shares":      shares,
                "views":       views,
                "engagement":  engagement,
                "formula":     f"({likes} likes + {comments} comments + {shares} shares) / 1000 × ${self.cpm_rate} CPM",
                "spend_usd":   spend,
                "eng_rate_pct": eng_rate,
                "eng_rate_formula": f"({engagement} / max({views}, 1)) × 100",
            })

        # Calculation tree / assumptions block
        data["assumptions"] = {
            "cpm_rate_usd":        self.cpm_rate,
            "spend_formula":       "estimated_spend = (likes + comments + shares) / 1000 × CPM",
            "engagement_formula":  "engagement_rate = (likes + comments + shares) / max(views, 1) × 100",
            "engagement_proxy":    "Engagement = likes + comments + shares (shares weighted as earned impressions)",
            "views_note":          "Views treated as reach proxy. If views = 0, engagement rate is capped at 100%.",
            "cpm_note":            f"CPM of ${self.cpm_rate} applied uniformly across all platforms. Adjust in Config.",
            "data_source":         "Web search results via Tavily/DuckDuckGo — estimates only, not official platform data.",
            "total_engagement":    total_engagement,
            "total_spend_usd":     round(total_spend, 2),
            "brand_breakdowns":    brand_calcs,
        }

        # Scrub secrets
        def scrub(obj):
            if isinstance(obj, dict):
                return {
                    k: scrub(v) for k, v in obj.items()
                    if "sk-" not in str(v) and "API_KEY" not in str(v)
                }
            if isinstance(obj, list):
                return [scrub(i) for i in obj]
            return obj

        return json.dumps(scrub(data), indent=2)
