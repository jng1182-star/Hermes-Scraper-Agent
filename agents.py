import os
from crewai import Agent
from crewai.llm import LLM
from tools.social_search_tool import SocialSearchTool

_OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_OLLAMA_BASE_URL = _OLLAMA_HOST + "/v1"

# ── Force all LLM routing to local Ollama ────────────────────────────────────
# CrewAI's llm_utils.py reads OPENAI_API_BASE / OPENAI_BASE_URL / BASE_URL as
# fallback base_url values and will override our explicit base_url param if
# those env vars point to an ngrok or cloud endpoint.  Clear them and redirect
# everything to 127.0.0.1 before any LLM object is constructed.
for _stale_var in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "BASE_URL", "API_BASE"):
    os.environ.pop(_stale_var, None)
os.environ["OLLAMA_HOST"]    = _OLLAMA_HOST
os.environ["OPENAI_API_BASE"] = _OLLAMA_BASE_URL   # litellm fallback → local Ollama
os.environ["OPENAI_BASE_URL"] = _OLLAMA_BASE_URL
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"    # no phone-home calls


def _make_llm(model_name: str) -> LLM:
    return LLM(
        model=model_name,
        base_url=_OLLAMA_BASE_URL,
        api_key="ollama",
    )


class SocialAgents:
    def __init__(self, depth: str = "deep"):
        self.search_tool = SocialSearchTool()
        # Scraper always uses e4b — it's structured data extraction, not deep reasoning.
        # e4b fits fully in GPU; 26b runs mixed CPU/GPU on this machine and stalls at 600s.
        self.scraper_llm = _make_llm("ollama/gemma4:e4b")
        analyst_model = "ollama/gemma4:26b" if depth == "deep" else "ollama/gemma4:e4b"  # quick is default
        self.llm = _make_llm(analyst_model)

    def scraper_agent(self) -> Agent:
        return Agent(
            role="Social Data Scraper",
            goal=(
                "Retrieve comprehensive raw engagement intelligence for each brand "
                "across Facebook, Instagram, TikTok, and YouTube — covering BOTH paid ads "
                "and organic posts. Extract actual numbers: likes, comments, shares, views, "
                "saves, followers/subscribers, and estimated ad spend signals."
            ),
            backstory=(
                "You are a senior social media intelligence analyst at a top media agency. "
                "You know exactly where to look for paid ad metrics (ad libraries, transparency "
                "centres, agency reports) AND organic performance data (viral trackers, social "
                "listening platforms, influencer databases). "
                "You never return zero-data — if one source is dry you try another angle. "
                "You distinguish clearly between PAID and ORGANIC content signals."
            ),
            tools=[self.search_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def analyst_agent(self) -> Agent:
        return Agent(
            role="Engagement Analyst",
            goal=(
                "Structure the raw intelligence into clean, per-brand, per-platform records "
                "with separate paid and organic metrics. Compute or estimate engagement numbers "
                "from the snippets — never leave metrics as zero if the snippets contain "
                "any quantitative signals (e.g. '2.3M views', '45K likes', '8% ER')."
            ),
            backstory=(
                "You are a data analyst specialising in paid media and organic social benchmarking "
                "for large FMCG and consumer brands. You extract numbers from messy text with "
                "precision. You understand that '2.3M' means 2,300,000 and '45K' means 45,000. "
                "When a snippet says a post got '8% engagement rate' and 'the brand has 500K followers' "
                "you compute: 8% × 500,000 = 40,000 interactions. "
                "You label each data point as paid or organic based on context clues "
                "(e.g. 'sponsored', 'ad library', 'boosted' = paid; 'viral', 'organic', 'UGC' = organic)."
            ),
            llm=self.llm,
            verbose=True,
        )

    def reporter_agent(self) -> Agent:
        return Agent(
            role="Intelligence Reporter",
            goal=(
                "Produce a single, valid JSON report that a Fortune 500 brand manager "
                "can act on immediately. Every competitor entry must have real numbers — "
                "synthesise and estimate if exact figures are absent, but never output zeros "
                "when the context contains quantitative signals."
            ),
            backstory=(
                "You are the lead intelligence reporter at a global media consultancy. "
                "Your deliverables go directly to CMOs and media directors. "
                "You synthesise paid vs organic performance, flag notable campaigns, "
                "and ensure every data field is populated with the best available estimate. "
                "You output clean JSON only — no markdown, no commentary, no code fences."
            ),
            llm=self.llm,
            verbose=True,
        )
