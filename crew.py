from crewai import Crew, Process
from agents import SocialAgents
from tasks import SocialTasks


class SocialListeningCrew:
    def __init__(self, query: str, depth: str = "deep", params: dict = None):
        self.query  = query
        self.params = params or {}
        self.agents = SocialAgents(depth=depth)
        self.tasks  = SocialTasks()

    def run(self):
        scraper  = self.agents.scraper_agent()
        analyst  = self.agents.analyst_agent()
        reporter = self.agents.reporter_agent()

        task1 = self.tasks.extraction_task(scraper, self.query, self.params)
        task2 = self.tasks.analysis_task(analyst)
        task3 = self.tasks.reporting_task(reporter)

        crew = Crew(
            agents=[scraper, analyst, reporter],
            tasks=[task1, task2, task3],
            process=Process.sequential,
            verbose=True,
        )
        return crew.kickoff()
