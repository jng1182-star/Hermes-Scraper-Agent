import os
from crewai import Agent
from crewai.llm import LLM
from tools.social_search_tool import SocialSearchTool
from tools.profile_scraper import ProfileScraperTool
from tools.feed_scroller import FeedScrollerTool
from tools.api_data_tool import APIDataTool
from tools.paid_adlib_tool import PaidAdLibTool

_OLLAMA_HOST     = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_OLLAMA_BASE_URL = _OLLAMA_HOST + "/v1"

# ── Force all LLM routing to local Ollama ────────────────────────────────────
# CrewAI 1.14.x uses OpenAICompatibleCompletion for Ollama. Its _get_client_params()
# reads OPENAI_API_KEY as fallback when api_key is None, and uses OPENAI_BASE_URL /
# OPENAI_API_BASE as fallback for base_url. If any of these point to real OpenAI
# infrastructure the request goes to api.openai.com → 403 Forbidden.
# Fix: set all three env vars to local Ollama values before LLM objects are constructed.
for _stale_var in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "BASE_URL", "API_BASE"):
    os.environ.pop(_stale_var, None)
os.environ["OLLAMA_HOST"]         = _OLLAMA_HOST
os.environ["OPENAI_API_KEY"]      = "ollama"           # prevent fallback to real OPENAI_API_KEY
os.environ["OPENAI_API_BASE"]     = _OLLAMA_BASE_URL   # litellm/openai SDK base_url fallback
os.environ["OPENAI_BASE_URL"]     = _OLLAMA_BASE_URL   # OpenAI SDK v2 base_url env fallback
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"        # no phone-home calls


def _make_llm(model_name: str) -> LLM:
    return LLM(
        model=model_name,
        base_url=_OLLAMA_BASE_URL,
        api_key="ollama",
    )


class SocialAgents:
    def __init__(self, depth: str = "deep"):
        self.search_tool   = SocialSearchTool()
        self.profile_tool  = ProfileScraperTool()
        self.feed_tool     = FeedScrollerTool()
        self.api_tool      = APIDataTool()
        self.adlib_tool    = PaidAdLibTool()
        # Scraper always uses e4b — structured data extraction, not deep reasoning.
        # e4b fits fully in GPU; 26b runs mixed CPU/GPU on this machine and stalls at 600s.
        self.scraper_llm = _make_llm("ollama/gemma4:e4b")
        analyst_model = "ollama/gemma4:26b" if depth == "deep" else "ollama/gemma4:e4b"
        self.llm = _make_llm(analyst_model)

    def profile_agent(self) -> Agent:
        """Agent 1 — fetches brand profile data via official APIs (YouTube Data API v3, Meta Ad Library)
        with Playwright DOM scraping as fallback."""
        return Agent(
            role="Profile Baseline Scraper",
            goal=(
                "Collect real, structured social media metrics for each brand on Facebook and YouTube. "
                "Use the Brand API Data Fetcher tool first — it calls official APIs (YouTube Data API v3, "
                "Meta Ad Library) and returns exact subscriber counts, view counts, and likes. "
                "If the API tool returns no data for a platform, fall back to the Profile Baseline Scraper tool. "
                "Produce a clean organic baseline per brand per platform with real numbers."
            ),
            backstory=(
                "You are a specialist in social media data collection. Your primary method is official APIs — "
                "YouTube Data API v3 for subscriber counts and video metrics, Meta Ad Library for declared "
                "ad impressions. APIs give you exact, authoritative numbers that no scraper can match. "
                "You fall back to Playwright DOM scraping only when APIs are unavailable. "
                "You never fabricate numbers — you report exactly what the APIs return."
            ),
            tools=[self.api_tool, self.profile_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def feed_agent(self) -> Agent:
        """Agent 2 — paid ad capture via Meta Ad Library API (primary) + Playwright scraper fallback."""
        return Agent(
            role="Feed Ad Capture Agent",
            goal=(
                "Collect declared paid advertising data for each brand on Facebook and YouTube. "
                "Use the Brand API Data Fetcher tool first — it calls the Meta Ad Library API "
                "and returns structured ad counts, impression ranges, and spend ranges. "
                "If the API tool returns empty data for Facebook (Meta API pending approval), "
                "fall back to the Paid Ad Library Scraper tool which scrapes facebook.com/ads/library "
                "directly via headless browser. "
                "Return structured records: advertiser, ad copy, impressions, spend signals."
            ),
            backstory=(
                "You are a paid media intelligence specialist. Your primary source is the Meta Ad "
                "Library API — structured, authoritative, fast. When that API is unavailable or "
                "returns no data, you fall back to headless browser scraping of the Ad Library website. "
                "You never fabricate ad data — you report exactly what the tools return."
            ),
            tools=[self.api_tool, self.adlib_tool, self.feed_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def researcher_agent(self) -> Agent:
        """Agent 3 — profile discovery: find and verify the correct social handles/URLs for
        each brand × market combination before scraping begins."""
        return Agent(
            role="Social Data Researcher",
            goal=(
                "For each brand + advertiser + market combination, identify and verify the correct "
                "official social media profiles and ad library pages across all target platforms. "
                "Use web search to find the exact YouTube channel URL, Facebook Page URL, TikTok "
                "handle, and Instagram handle for each brand in each market. "
                "Verify the profile belongs to the correct brand (match industry context) and "
                "output a structured profile map that the scraper agents will use as their targets."
            ),
            backstory=(
                "You are a senior social media intelligence researcher at a global media agency. "
                "You specialise in brand profile identification — finding the authoritative, "
                "brand-owned social channels versus fan pages or unrelated namesakes. "
                "You search using the advertiser name + brand + market to disambiguate "
                "(e.g. 'Unilever Axe Facebook Philippines official page') and cross-check "
                "profile bios and branding to confirm the match. "
                "You never assume — you verify, and you flag low-confidence matches. "
                "Your output is a clean, structured profile map consumed directly by the scrapers."
            ),
            tools=[self.search_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def scraper_agent(self) -> Agent:
        """Legacy alias kept for checkpoint compatibility — delegates to researcher_agent."""
        return self.researcher_agent()

    def analyst_agent(self) -> Agent:
        return Agent(
            role="Share-of-Voice Analyst",
            goal=(
                "Compute a directional Share-of-Voice index for each brand on each platform "
                "(Facebook, YouTube, TikTok) using six observable proxy signals: "
                "creative volume (total active ads normalized across brands), "
                "creative velocity (new ads launched in last 7 days), "
                "ad longevity (avg days since earliest start_date), "
                "geographic presence (countries targeted), "
                "reach bucket (impression range tier 1–4), "
                "and engagement corroboration (er_pct vs category benchmark). "
                "Normalize each signal, apply weights (35/10/15/15/15/10%), produce a 0–100 "
                "SOV index per brand per platform, apply a cross-signal consistency check "
                "to validate confidence, then compute a composite cross-platform SOV. "
                "Assign confidence (High/Medium/Low) — a brand with conflicting signals "
                "(rank divergence >2 positions on primary signals) can never exceed Medium."
            ),
            backstory=(
                "You are a media intelligence analyst specialising in competitive share-of-voice "
                "measurement for FMCG and consumer brands. You understand that true ad spend data "
                "is not publicly available, so you use observable proxy signals — ad creative "
                "volume and velocity, presence duration, geographic footprint, reach tiers, and "
                "engagement signals — as directional indicators of advertising intensity. "
                "You treat TikTok with equal methodological rigor as Facebook and YouTube. "
                "You produce indexed, normalized SOV scores (0–100) clearly labeled as "
                "directional estimates, not actual spend values. "
                "When signals conflict directionally, you conservatively downgrade confidence "
                "rather than averaging away the discrepancy."
            ),
            llm=self.llm,
            verbose=True,
        )

    def reporter_agent(self) -> Agent:
        return Agent(
            role="SOV Intelligence Reporter",
            goal=(
                "Produce a single valid JSON report containing directional Share-of-Voice "
                "indices for each brand across Facebook, YouTube, and TikTok. "
                "Every brand entry must have a sov_index and sov_label (including the "
                "'(Directional / Indexed – Not Actual Spend)' suffix), confidence level, "
                "consistency_flag, and signal breakdown per platform. "
                "Include the full methodology_disclaimer with competitive set scope caveat. "
                "Never output dollar amounts, CPM values, or spend estimates. "
                "Output clean JSON only — no markdown, no code fences."
            ),
            backstory=(
                "You are the lead intelligence reporter at a global media consultancy. "
                "Your deliverables go directly to CMOs and media directors. "
                "You synthesise relative advertising presence by platform across Facebook, "
                "YouTube, and TikTok, flag brands with low confidence or conflicting signals, "
                "and ensure every SOV index carries the correct directional label. "
                "You output clean JSON only — no markdown, no commentary, no code fences."
            ),
            llm=self.llm,
            verbose=True,
        )
