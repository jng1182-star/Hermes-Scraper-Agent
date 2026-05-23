import os
from crewai import Agent
from crewai.llm import LLM
from tools.social_search_tool import SocialSearchTool

# Ollama host — CrewAI native Ollama provider reads OLLAMA_HOST env var.
# Append /v1 for OpenAI-compatible endpoint that CrewAI uses.
_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_OLLAMA_BASE_URL = _OLLAMA_HOST + "/v1"

# Ensure env var is set for the native provider's env-var lookup
os.environ["OLLAMA_HOST"] = _OLLAMA_HOST


def _make_llm(model_name: str) -> LLM:
    """Create a CrewAI LLM object pointing at local/remote Ollama."""
    return LLM(
        model=model_name,
        base_url=_OLLAMA_BASE_URL,
        api_key="ollama",          # Ollama doesn't require a real key
    )


class SocialAgents:
    def __init__(self, depth: str = "deep"):
        self.search_tool = SocialSearchTool()
        model = "ollama/gemma4:26b" if depth == "deep" else "ollama/gemma4:e4b"
        self.llm = _make_llm(model)

    def scraper_agent(self) -> Agent:
        return Agent(
            role="Social Data Scraper",
            goal=(
                "Extract raw engagement numbers (likes, comments, shares, views) "
                "for the target brands on specified platforms."
            ),
            backstory=(
                "You are an expert at navigating search results to find specific "
                "social media metrics for brands and their competitors."
            ),
            tools=[self.search_tool],
            llm=self.llm,
            verbose=True,
        )

    def analyst_agent(self) -> Agent:
        return Agent(
            role="Engagement Analyst",
            goal=(
                "Identify and structure raw engagement numbers into a clean, "
                "organised format per brand and platform."
            ),
            backstory=(
                "You are a data specialist. You take messy text and turn it into "
                "structured numbers. You do NOT perform math; you only extract and organise."
            ),
            llm=self.llm,
            verbose=True,
        )

    def reporter_agent(self) -> Agent:
        return Agent(
            role="Intelligence Reporter",
            goal=(
                "Synthesise findings into a structured JSON report with one entry "
                "per competitor brand."
            ),
            backstory=(
                "You are a technical writer. You ensure the final output is a valid, "
                "clean JSON object ready for dashboard consumption."
            ),
            llm=self.llm,
            verbose=True,
        )
