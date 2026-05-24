import os
from crewai import Agent
from crewai.llm import LLM
from tools.social_search_tool import SocialSearchTool
from tools.profile_scraper import ProfileScraperTool
from tools.feed_scroller import FeedScrollerTool

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
        # Scraper always uses e4b — structured data extraction, not deep reasoning.
        # e4b fits fully in GPU; 26b runs mixed CPU/GPU on this machine and stalls at 600s.
        self.scraper_llm = _make_llm("ollama/gemma4:e4b")
        analyst_model = "ollama/gemma4:26b" if depth == "deep" else "ollama/gemma4:e4b"
        self.llm = _make_llm(analyst_model)

    def profile_agent(self) -> Agent:
        """Agent 1 — scrapes public brand profiles within the date scope."""
        return Agent(
            role="Profile Baseline Scraper",
            goal=(
                "Scrape public brand profile pages on Instagram, Facebook, TikTok, and YouTube "
                "within the specified date scope. Extract actual observed metrics — likes, comments, "
                "views, follower count — for every post published within the date window. "
                "Produce a clean organic baseline per brand per platform."
            ),
            backstory=(
                "You are a specialist in first-party social data collection. "
                "You navigate public brand profiles directly, read actual engagement numbers "
                "from the DOM, and never estimate or infer — you only report what you observe. "
                "You understand that 'last 30 posts' is not good enough; you scope by publish date. "
                "You know that TikTok video IDs encode timestamps and use this to date-filter posts "
                "without visiting every page. You work systematically: collect URLs first, then visit "
                "each in parallel to extract dates and metrics."
            ),
            tools=[self.profile_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def feed_agent(self) -> Agent:
        """Agent 2 — doom scrolls feeds and captures declared paid ads via DOM markers."""
        return Agent(
            role="Feed Ad Capture Agent",
            goal=(
                "Scroll the algorithmic feed for each platform and capture every declared paid "
                "advertisement using strict DOM marker detection. Do NOT flag content as paid "
                "based on engagement numbers — only capture posts where an explicit 'Sponsored', "
                "'Paid partnership', ad badge, or CTA overlay is present in the DOM. "
                "Return structured records: advertiser, creative URL, ad copy, live metrics."
            ),
            backstory=(
                "You are a paid media intelligence specialist trained to identify platform-native "
                "ad declarations. You know the exact DOM markers each platform uses: TikTok's "
                "[data-e2e='ad-badge'], Instagram's 'Sponsored' text node, Facebook's 'Sponsored' "
                "link, YouTube's ad badge near the channel handle. "
                "Your rule is strict: if no explicit DOM marker is present, do not call it an ad. "
                "The engagement-based outlier detection runs downstream — your job is pure observation."
            ),
            tools=[self.feed_tool],
            llm=self.scraper_llm,
            verbose=True,
        )

    def scraper_agent(self) -> Agent:
        """Legacy search-based scraper — used as fallback when DOM scraping yields zero results."""
        return Agent(
            role="Social Data Scraper",
            goal=(
                "Retrieve supplementary engagement intelligence via web search for brands "
                "where direct profile scraping returned no data. Extract the best available "
                "numbers from search snippets: likes, comments, shares, views, followers."
            ),
            backstory=(
                "You are a senior social media intelligence analyst at a top media agency. "
                "You use search intelligence as a fallback when direct scraping is unavailable. "
                "You know the difference between a primary DOM observation and a search snippet "
                "inference — you flag search-derived data as lower confidence in your output. "
                "You never return zero-data — if one source is dry you try another angle."
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
