from crewai import Task


class SocialTasks:
    def extraction_task(self, agent, query, params: dict = None) -> Task:
        params = params or {}
        date_from  = params.get("date_from") or ""
        date_to    = params.get("date_to")   or ""
        date_range = params.get("date_range", "Last 30 days")
        platforms  = params.get("platforms",  [])

        # Build a precise date scope instruction for the agent
        if date_from and date_to:
            date_scope = (
                f"IMPORTANT: Restrict ALL data you collect to the period {date_from} → {date_to}. "
                "Ignore any posts, campaigns, or metrics outside this window."
            )
        else:
            date_scope = (
                f"IMPORTANT: Restrict ALL data you collect to the period: {date_range}. "
                "Ignore any posts, campaigns, or metrics outside this window."
            )

        platform_scope = ""
        if platforms:
            platform_scope = (
                f"\nFOCUS PLATFORMS: {', '.join(platforms)}. "
                "Prioritise results from these platforms. "
                "Only include other platforms if no data is found on the target platforms."
            )

        return Task(
            description=(
                f"Use the Social Media Intelligence Search tool to research: {query}\n\n"
                f"{date_scope}{platform_scope}\n\n"
                "The tool will return structured JSON with raw snippets per brand and platform. "
                "Your job is to read ALL the raw_snippets carefully and extract:\n"
                "1. ENGAGEMENT METRICS — likes, comments, shares, views (as integers where found)\n"
                "2. POST CONTENT — actual post captions, campaign names, slogans, viral content themes\n"
                "3. HASHTAGS — any #hashtags mentioned in the snippets\n"
                "4. CONTENT THEMES — recurring topics, product names, collaborations, events\n"
                "5. FOLLOWER/FAN counts if mentioned\n"
                "6. PLATFORM — which platform each data point came from\n\n"
                "For each brand, look for evidence of their most engaging content: "
                "what they post about, which campaigns got traction, what their audience responds to. "
                "If a snippet mentions a specific post, campaign, or product launch — extract it. "
                "Return everything you find, organised by brand and platform."
            ),
            expected_output=(
                "A detailed report per brand covering: platform, engagement numbers, "
                "post content samples, hashtags, and content themes — all extracted from the raw snippets."
            ),
            agent=agent,
        )

    def analysis_task(self, agent) -> Task:
        return Task(
            description=(
                "Analyse the extracted social media data and structure it per competitor brand. "
                "For each brand produce:\n"
                "- name: brand name\n"
                "- handle: social media handle (e.g. @brand) if found, else blank\n"
                "- platform: primary platform where data was found\n"
                "- metrics: {likes, comments, shares, views} as integers (use 0 if not found)\n"
                "- sentiment: overall audience sentiment — 'Positive', 'Neutral', or 'Negative' "
                "  (infer from engagement tone, comments, caption language)\n"
                "- top_posts: list of up to 3 post objects. Each object has:\n"
                "    { \"caption\": \"short description of the post\", \"url\": \"direct link to the post if found, else null\" }\n"
                "  Example: { \"caption\": \"Summer campaign featuring athlete collab — 2.1M views\", \"url\": \"https://www.tiktok.com/@brand/video/123\" }\n"
                "  If a URL was visible in the search results, include it. Otherwise set url to null.\n"
                "- hashtags: list of up to 6 hashtags associated with this brand\n"
                "- content_themes: list of up to 4 recurring content themes "
                "  (e.g. ['Athlete endorsement', 'User-generated content', 'Product launch'])\n\n"
                "Assess sentiment from the tone of comments and engagement pattern: "
                "high engagement + positive language = Positive; "
                "low engagement or mixed = Neutral; "
                "complaints, controversy = Negative."
            ),
            expected_output=(
                "A structured list of competitors, each with: name, handle, platform, metrics, "
                "sentiment, top_posts (list of strings), hashtags (list), content_themes (list)."
            ),
            agent=agent,
        )

    def reporting_task(self, agent) -> Task:
        return Task(
            description=(
                "Format the analysed competitor data into a single valid JSON object. "
                "The output must be ONLY the JSON object — no markdown, no code fences, no extra text. "
                "Use this exact schema:\n"
                "{\n"
                '  "competitors": [\n'
                "    {\n"
                '      "name": "string",\n'
                '      "handle": "string",\n'
                '      "platform": "string",\n'
                '      "metrics": {"likes": 0, "comments": 0, "shares": 0, "views": 0},\n'
                '      "sentiment": "Positive|Neutral|Negative",\n'
                '      "top_posts": [{"caption": "string", "url": "string or null"}],\n'
                '      "hashtags": ["#string", "#string"],\n'
                '      "content_themes": ["string", "string"]\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                "Rules:\n"
                "- Every competitor must have ALL fields\n"
                "- top_posts: 1–3 items max. Each item is a JSON object: {\"caption\": \"...\", \"url\": \"https://... or null\"}\n"
                "- hashtags: 1–6 items max, each starting with #\n"
                "- content_themes: 1–4 items max, plain text labels\n"
                "- Use 0 for any unknown numeric value\n"
                "- Use empty list [] if no data found for top_posts / hashtags / content_themes"
            ),
            expected_output=(
                "A raw JSON object (no markdown) with competitors array. "
                "Each entry has: name, handle, platform, metrics{likes,comments,shares,views}, "
                "sentiment, top_posts[{caption,url}], hashtags[], content_themes[]."
            ),
            agent=agent,
        )
