import os
from crewai import Agent
from crewai.llm import LLM
from tools.social_search_tool import SocialSearchTool
from tools.profile_scraper import ProfileScraperTool
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
        self.adlib_tool    = PaidAdLibTool()
        # Scraper always uses e4b — structured data extraction, not deep reasoning.
        # e4b fits fully in GPU; 26b runs mixed CPU/GPU on this machine and stalls at 600s.
        self.scraper_llm = _make_llm("ollama/gemma4:e4b")
        analyst_model = "ollama/gemma4:26b" if depth == "deep" else "ollama/gemma4:e4b"
        self.llm = _make_llm(analyst_model)

    def profile_agent(self) -> Agent:
        """Agent 1 — scrapes public profile pages using the researcher's profile map.
        Collects all posts in scope, computes organic baselines, and flags paid posts
        via DOM labels + ER outlier detection. Geo-unconstrained."""
        return Agent(
            role="Profile Scraper",
            goal=(
                "Using the verified profile map from the researcher, scrape every brand's public "
                "social media profile pages within the user-specified date scope. "
                "For each brand × platform: collect all posts in scope (url, caption, metrics, "
                "publish_date), detect DOM-labelled paid posts (Sponsored / Paid partnership), "
                "compute an organic ER baseline from the clean organic pool, then re-score all "
                "remaining posts — flagging those exceeding 3× the organic baseline ER as "
                "likely_paid (baseline_outlier). "
                "Return organic_posts, paid_posts, and baseline metrics per brand per platform. "
                "Coverage is geo-unconstrained: public profile pages are hit directly."
            ),
            backstory=(
                "You are a specialist in social media content collection and paid signal detection. "
                "The researcher hands you verified URLs and handles — you use those as your exact "
                "scraping targets. Your organic ER baselines and paid post flags are the primary "
                "data source for the analyst's SOV signals. You never fabricate data."
            ),
            tools=[self.profile_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def feed_agent(self) -> Agent:
        """Agent 2 — queries Meta Ad Library, Google Ads Transparency, and TikTok CCL
        for declared paid inventory. No feed scrolling (retired: OOM on Railway).
        Profile scraper covers organic + ER-based paid detection from profile pages."""
        return Agent(
            role="Ad Library Collector",
            goal=(
                "For each brand in the competitive set, query the declared paid ad inventory "
                "across all three ad libraries: Meta Ad Library (covers Facebook + Instagram), "
                "Google Ads Transparency Center (covers YouTube), and TikTok Commercial Content "
                "Library. For each brand × platform: capture active_ads_found, "
                "impressions_min/max, new_ads_last_7d, ad_start_dates, and geo_countries. "
                "Return a structured JSON with ad_library_results keyed by brand name."
            ),
            backstory=(
                "You are a paid media intelligence specialist at a global media consultancy. "
                "You extract declared advertising inventory from public ad transparency libraries — "
                "Meta, Google, and TikTok — without relying on authenticated feeds or algorithmic "
                "surfaces. Your data covers declared paid creative volume and velocity across "
                "Facebook, Instagram, YouTube, and TikTok. You never fabricate ad counts or reach data."
            ),
            tools=[self.adlib_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def researcher_agent(self) -> Agent:
        """Agent 3 — profile discovery: find and verify the correct social handles/URLs for
        each brand × market combination before scraping begins."""
        return Agent(
            role="Social Data Researcher",
            goal=(
                "For each brand + advertiser + market combination, find and verify the official "
                "social media profile URL and handle on each target platform. "
                "Your output is a profile map: one URL/handle per brand × platform × market. "
                "You do NOT collect posts, engagement metrics, ad counts, or spend data — "
                "that is handled downstream by the Profile Scraper and Ad Library Collector."
            ),
            backstory=(
                "You are a senior social media intelligence researcher at a global media agency. "
                "You specialise in brand profile identification — finding the authoritative, "
                "brand-owned social channels versus fan pages or unrelated namesakes. "
                "You search using the advertiser name + brand + market to disambiguate "
                "(e.g. 'Unilever Closeup Facebook Philippines official page') and cross-check "
                "profile names and bios to confirm the match. "
                "You never collect content — you map profiles. "
                "Your output is a clean profile map consumed directly by the scraper agents."
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
