from crewai import Task


class SocialTasks:
    def extraction_task(self, agent, query, params: dict = None) -> Task:
        params     = params or {}
        date_from  = params.get("date_from") or ""
        date_to    = params.get("date_to")   or ""
        date_range = params.get("date_range", "Last 30 days")
        platforms  = params.get("platforms",  ["TikTok", "Instagram", "YouTube", "Facebook"])
        post_type  = params.get("post_type",  "both")
        advertisers= params.get("advertisers", [])
        competitors = params.get("competitors", [])
        uploaded   = params.get("uploaded_context", [])

        if date_from and date_to:
            date_scope = (
                f"RESTRICT ALL DATA to {date_from} → {date_to}. "
                "Ignore posts, campaigns, or metrics outside this window."
            )
        else:
            date_scope = (
                f"RESTRICT ALL DATA to: {date_range}. "
                "Ignore posts, campaigns, or metrics outside this window."
            )

        plat_str = ", ".join(platforms) if platforms else "TikTok, Instagram, YouTube, Facebook"
        post_scope = {
            "paid":    "Focus ONLY on PAID content: ads, sponsored posts, boosted content, ad library entries.",
            "organic": "Focus ONLY on ORGANIC content: non-sponsored posts, UGC, viral content, creator posts.",
            "both":    "Collect BOTH paid (ads, sponsored, boosted) AND organic (UGC, viral, creator) content. Tag each data point as paid or organic.",
        }.get(post_type, "Collect both paid and organic content.")

        # Uploaded file context
        upload_context = ""
        if uploaded:
            upload_context = "\n\nUPLOADED REFERENCE FILES (use this data to inform your search):\n"
            for uf in uploaded[:5]:  # cap at 5 files
                upload_context += f"\n--- {uf['filename']} ---\n{uf['content'][:2000]}\n"

        all_brands = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_str = ", ".join(all_brands) if all_brands else query

        return Task(
            description=(
                f"Use the Social Media Intelligence Search tool to research: {query}\n\n"
                f"TARGET BRANDS: {brands_str}\n"
                f"PLATFORMS: {plat_str} (ONLY these four platforms)\n"
                f"DATE SCOPE: {date_scope}\n"
                f"CONTENT TYPE: {post_scope}\n"
                f"{upload_context}\n\n"
                "Call the search tool ONCE per brand-platform pair — one focused query covers paid and organic. "
                "Do not call the tool repeatedly for the same brand-platform combination.\n\n"
                "For EACH brand on EACH platform, extract:\n"
                "1. PAID METRICS (if applicable):\n"
                "   - Ad campaign names, creatives, slogans\n"
                "   - Estimated impressions/reach from ad library\n"
                "   - CPM/CPC signals if visible\n"
                "   - Sponsored post URLs and engagement\n"
                "2. ORGANIC METRICS:\n"
                "   - Likes, comments, shares, saves, views (as integers — parse '2.3M' as 2300000)\n"
                "   - Follower/subscriber count\n"
                "   - Top organic post captions and hashtags\n"
                "   - Viral content themes\n"
                "3. For BOTH: note which entries are paid vs organic\n\n"
                "IMPORTANT: Parse ALL numbers from snippets. '2.3M views' = 2300000. "
                "'45K likes' = 45000. '8% ER' = engagement rate 8.0. "
                "Never return zeros when the snippets contain quantitative signals. "
                "If a snippet gives a range (e.g. '10K–50K'), use the midpoint (30000)."
            ),
            expected_output=(
                "A detailed per-brand, per-platform report with: paid campaign data, "
                "organic engagement numbers (parsed as integers), follower counts, "
                "top post content, hashtags, and content themes. Each metric labelled paid/organic."
            ),
            agent=agent,
        )

    def analysis_task(self, agent, prior_context: str = None) -> Task:
        prefix = ""
        if prior_context:
            # Truncate to avoid token overflow; enough for the analyst to work from
            snippet = prior_context[:6000]
            prefix = (
                f"[RESUMED FROM CHECKPOINT]\n"
                f"The scraper already collected the following data. Do NOT re-run searches. "
                f"Analyse this data directly:\n\n{snippet}\n\n"
            )
        return Task(
            description=(
                prefix +
                "Analyse the extracted social media data and structure it PER COMPETITOR BRAND. "
                "For each brand produce ONE record per platform (or one combined record if data spans platforms):\n\n"
                "- name: brand name (never 'Unknown')\n"
                "- handle: @handle if found, else blank\n"
                "- platform: TikTok | Instagram | YouTube | Facebook\n"
                "- post_type: 'paid' | 'organic' | 'both'\n"
                "- metrics: {\n"
                "    likes: integer,\n"
                "    comments: integer,\n"
                "    shares: integer,\n"
                "    saves: integer,\n"
                "    views: integer,\n"
                "    followers: integer  (subscriber count for YouTube, page followers for Facebook)\n"
                "  }\n"
                "  NOTE: Parse shorthand — '2.3M' = 2300000, '45K' = 45000, '8% of 500K followers' → likes≈40000\n"
                "  Use your best estimate based on available data. DO NOT use 0 when signals exist.\n"
                "- sentiment: 'Positive' | 'Neutral' | 'Negative' — infer from engagement tone\n"
                "- top_posts: list of up to 5 objects:\n"
                "    {caption: str, url: str|null, post_type: 'paid'|'organic', likes: int, views: int}\n"
                "- hashtags: list of up to 6 hashtags\n"
                "- content_themes: list of up to 4 themes\n"
                "- paid_campaigns: list of notable paid campaign names (empty list if none found)\n\n"
                "Engagement inference rules:\n"
                "- If ER% and followers are known: interactions = ER% × followers / 100\n"
                "- If views are known and platform is TikTok/YouTube: estimate likes ≈ views × 0.05, comments ≈ views × 0.005\n"
                "- If only 'high engagement' mentioned: use platform median (TikTok: 5% ER, IG: 1.5%, FB: 0.8%, YT: 2%)\n"
            ),
            expected_output=(
                "A structured list of competitor records, each with: name, handle, platform, post_type, "
                "metrics{likes,comments,shares,saves,views,followers}, sentiment, "
                "top_posts[{caption,url,post_type,likes,views}], hashtags[], content_themes[], paid_campaigns[]."
            ),
            agent=agent,
        )

    def reporting_task(self, agent, prior_context: str = None) -> Task:
        prefix = ""
        if prior_context:
            snippet = prior_context[:6000]
            prefix = (
                f"[RESUMED FROM CHECKPOINT]\n"
                f"The analyst already structured the following data. Do NOT re-analyse. "
                f"Format it directly into the required JSON schema:\n\n{snippet}\n\n"
            )
        return Task(
            description=(
                prefix +
                "Format the analysed competitor data into a single valid JSON object. "
                "Output ONLY the JSON — no markdown, no code fences, no extra text.\n\n"
                "Use this exact schema:\n"
                "{\n"
                '  "competitors": [\n'
                "    {\n"
                '      "name": "string",\n'
                '      "handle": "string",\n'
                '      "platform": "TikTok|Instagram|YouTube|Facebook",\n'
                '      "post_type": "paid|organic|both",\n'
                '      "metrics": {\n'
                '        "likes": 0, "comments": 0, "shares": 0,\n'
                '        "saves": 0, "views": 0, "followers": 0\n'
                "      },\n"
                '      "sentiment": "Positive|Neutral|Negative",\n'
                '      "top_posts": [\n'
                '        {"caption": "str", "url": "str or null", "post_type": "paid|organic", "likes": 0, "views": 0}\n'
                "      ],\n"
                '      "hashtags": ["#string"],\n'
                '      "content_themes": ["string"],\n'
                '      "paid_campaigns": ["string"]\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                "RULES:\n"
                "- Every competitor MUST have ALL fields\n"
                "- metrics: use best available estimate — never all-zeros if data was found\n"
                "- post_type per competitor must be 'paid', 'organic', or 'both'\n"
                "- top_posts: 1–5 items; each a JSON object with caption, url (or null), post_type, likes, views\n"
                "- hashtags: 1–6 items starting with #\n"
                "- content_themes: 1–4 plain text labels\n"
                "- paid_campaigns: list campaign names found; [] if none"
            ),
            expected_output=(
                "A raw JSON object (no markdown) with competitors array. "
                "Each entry: name, handle, platform, post_type, metrics{6 fields}, "
                "sentiment, top_posts[{caption,url,post_type,likes,views}], "
                "hashtags[], content_themes[], paid_campaigns[]."
            ),
            agent=agent,
        )
