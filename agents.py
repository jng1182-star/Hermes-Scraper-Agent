import os
from crewai import Agent
from tools.social_search_tool import SocialSearchTool

# Ollama host — override via OLLAMA_HOST env var for remote/cloud setups
_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

LLM_QUICK = f"ollama/gemma4:e4b"
LLM_DEEP  = f"ollama/gemma4:26b"

# Tell litellm (used by CrewAI) where Ollama lives
os.environ.setdefault("OLLAMA_API_BASE", _OLLAMA_HOST)


class SocialAgents:
    def __init__(self, depth: str = "deep"):
        self.search_tool = SocialSearchTool()
        self.llm = LLM_DEEP if depth == "deep" else LLM_QUICK

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
