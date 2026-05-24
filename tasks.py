from crewai import Task


class SocialTasks:

    def profile_task(self, agent, params: dict = None) -> Task:
        """Agent 1 — date-scoped organic baseline scrape across all brand profiles."""
        params     = params or {}
        brands     = params.get("brands", [])
        competitors= params.get("competitors", [])
        advertisers= params.get("advertisers", [])
        platforms  = params.get("platforms", ["YouTube", "Facebook"])
        date_from  = params.get("date_from") or ""
        date_to    = params.get("date_to")   or ""
        date_range = params.get("date_range", "Last 30 days")
        country    = params.get("country", "")

        all_brands = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_str = ", ".join(all_brands) if all_brands else "all target brands"

        date_scope = (
            f"{date_from} to {date_to}" if date_from and date_to else date_range
        )

        return Task(
            description=(
                f"Use the Profile Baseline Scraper tool to collect organic baseline metrics "
                f"for the following brands: {brands_str}.\n\n"
                f"PLATFORMS: {', '.join(platforms)}\n"
                f"DATE SCOPE: {date_scope} — scrape only posts published within this window.\n"
                f"COUNTRY/MARKET: {country or 'Global'}\n\n"
                "Call the tool with this JSON:\n"
                + "{\n"
                + '  "brands": [' + ", ".join('{"name": "' + b + '", "handles": {}}' for b in all_brands) + '],\n'
                + '  "platforms": ' + str(platforms) + ',\n'
                + '  "date_from": "' + date_from + '",\n'
                + '  "date_to": "' + date_to + '",\n'
                + '  "country": "' + country + '"\n'
                + "}\n\n"
                "The tool returns per-brand, per-platform baselines with: "
                "avg_likes, avg_comments, avg_views, avg_er_pct, follower_count, posts_in_scope. "
                "Return the full JSON output from the tool unchanged."
            ),
            expected_output=(
                "JSON from the Profile Baseline Scraper tool: list of baselines per brand per platform, "
                "each with avg_likes, avg_comments, avg_views, avg_er_pct, follower_count, "
                "posts_in_scope, collection_method, data_source."
            ),
            agent=agent,
        )

    def feed_task(self, agent, params: dict = None) -> Task:
        """Agent 2 — doom scroll feed ad capture across all platforms."""
        params     = params or {}
        competitors= params.get("competitors", [])
        advertisers= params.get("advertisers", [])
        platforms  = params.get("platforms", ["YouTube", "Facebook"])
        country    = params.get("country", "")

        all_brands = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_str = ", ".join(all_brands) if all_brands else "all target brands"

        return Task(
            description=(
                f"Use the Feed Doom Scroller tool to capture declared paid advertisements "
                f"for the following brands: {brands_str}.\n\n"
                f"PLATFORMS: {', '.join(platforms)}\n"
                f"COUNTRY/MARKET: {country or 'Global'}\n\n"
                "Call the tool with this JSON:\n"
                "{\n"
                f'  "platforms": {platforms},\n'
                f'  "country": "{country}",\n'
                f'  "brands": {all_brands}\n'
                "}\n\n"
                "IMPORTANT: The tool uses strict DOM-marker detection only. "
                "Do NOT add or infer additional paid posts beyond what the tool returns. "
                "Every ad in the output has been positively identified via an explicit "
                "platform-native ad label or CTA overlay. "
                "Return the full JSON output from the tool unchanged."
            ),
            expected_output=(
                "JSON from the Feed Doom Scroller tool: brand_matched_ads list (each with "
                "platform, paid_signal, advertiser, ad_copy, creative_url, likes, comments, views, "
                "captured_utc, data_source), plus category_ads for unmatched ads and metadata."
            ),
            agent=agent,
        )

    def extraction_task(self, agent, query, params: dict = None) -> Task:
        params     = params or {}
        date_from  = params.get("date_from") or ""
        date_to    = params.get("date_to")   or ""
        date_range = params.get("date_range", "Last 30 days")
        platforms  = params.get("platforms",  ["YouTube", "Facebook"])
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

        plat_str = ", ".join(platforms) if platforms else "YouTube, Facebook"
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

    def analysis_task(self, agent, prior_context: str = None, params: dict = None) -> Task:
        params = params or {}
        advertisers = params.get("advertisers", [])
        competitors = params.get("competitors", [])
        all_brands  = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_seed = ", ".join(f'"{b}"' for b in all_brands) if all_brands else ""

        prefix = ""
        if prior_context:
            snippet = prior_context[:6000]
            if len(prior_context) > 6000:
                snippet += "\n[... TRUNCATED — earlier data omitted to stay within token limit ...]"
            # Detect whether context is first-party DOM data or legacy search snippets
            is_first_party = "AGENT 1:" in snippet or "AGENT 2:" in snippet
            if is_first_party:
                prefix = (
                    "DATA CONTEXT (first-party DOM scrape):\n"
                    "The following data was collected directly from platform pages by Agent 1 "
                    "(profile baseline scraper) and Agent 2 (in-feed ad capture). "
                    "AGENT 1 data = observed organic metrics (actual DOM-read integers). "
                    "AGENT 2 data = confirmed paid ads (explicit DOM label detected). "
                    "Use these values directly — do NOT re-estimate or override with category averages "
                    "when real numbers are present. Only fall back to estimation for missing fields.\n\n"
                    + snippet + "\n\n"
                )
            else:
                prefix = (
                    "[RESUMED FROM CHECKPOINT / SEARCH FALLBACK]\n"
                    "The following data was collected via web search. Metrics may be inferred "
                    "from editorial text — treat as lower confidence than DOM-scraped values. "
                    "Do NOT re-run searches. Analyse this data directly:\n\n"
                    + snippet + "\n\n"
                )
        brands_line = f"\nBRANDS IN THIS SCAN (name field must be one of these — never null or 'Unknown'): {brands_seed}\n" if brands_seed else ""
        return Task(
            description=(
                prefix +
                brands_line +
                "Analyse the extracted social media data and structure it PER COMPETITOR BRAND. "
                "For each brand produce ONE record per platform (or one combined record if data spans platforms):\n\n"
                "- name: brand name (never 'Unknown', never null — must match one of the brands listed above)\n"
                "- handle: @handle if found, else blank\n"
                "- platform: YouTube | Facebook\n"
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
                "- If views are known and platform is YouTube: estimate likes ≈ views × 0.04, comments ≈ views × 0.003\n"
                "- If only 'high engagement' mentioned: use platform median (FB: 0.8%, YT: 2%)\n\n"
                "PAID SIGNAL CLASSIFICATION — required field 'paid_signal' on every record:\n"
                "Step 1: Compute observed ER = (likes + comments + shares + saves) / denominator × 100\n"
                "  - Denominator: views for YouTube; followers for Facebook\n"
                "Step 2: Compare against these 3-month rolling category ER benchmarks:\n"
                "  Facebook: General 0.8%, Beauty 1.1%, Fashion 0.9%, Food 1.0%, Entertainment 1.2%, Finance 0.5%\n"
                "  YouTube: General 2.0%, Beauty 3.0%, Fashion 2.5%, Food 2.5%, Entertainment 3.5%, Finance 1.5%\n"
                "Step 3: Assign paid_signal:\n"
                "  - 'dom_label': a 'Sponsored' or 'Paid partnership' label was found in the content\n"
                "  - 'statistical_outlier': observed ER > 3× the category benchmark above — flag as likely paid even if no DOM label found; also override post_type to 'paid'\n"
                "  - 'declared': content was explicitly called paid/ad in the source data, but no DOM label or ER check applies\n"
                "  - 'organic': no paid signals of any kind detected\n"
                "This field is REQUIRED on every competitor record. Never omit it.\n"
            ),
            expected_output=(
                "A structured list of competitor records, each with: name, handle, platform, post_type, "
                "paid_signal (dom_label|statistical_outlier|declared|organic), "
                "metrics{likes,comments,shares,saves,views,followers}, sentiment, "
                "top_posts[{caption,url,post_type,likes,views}], hashtags[], content_themes[], paid_campaigns[]."
            ),
            agent=agent,
        )

    def reporting_task(self, agent, prior_context: str = None, params: dict = None) -> Task:
        params = params or {}
        advertisers = params.get("advertisers", [])
        competitors = params.get("competitors", [])
        all_brands  = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_seed = ", ".join(f'"{b}"' for b in all_brands) if all_brands else '"(unknown)"'

        prefix = ""
        if prior_context:
            snippet = prior_context[:6000]
            if len(prior_context) > 6000:
                snippet += "\n[... TRUNCATED — earlier data omitted to stay within token limit ...]"
            prefix = (
                f"[RESUMED FROM CHECKPOINT]\n"
                f"The analyst already structured the following data. Do NOT re-analyse. "
                f"Format it directly into the required JSON schema:\n\n{snippet}\n\n"
            )
        return Task(
            description=(
                prefix +
                f"BRANDS IN THIS SCAN (use these exact names — never output name: null or name: 'Unknown'): {brands_seed}\n\n"
                "Format the analysed competitor data into a single valid JSON object. "
                "Output ONLY the JSON — no markdown, no code fences, no extra text.\n\n"
                "Use this exact schema:\n"
                "{\n"
                '  "competitors": [\n'
                "    {\n"
                '      "name": "string  ← MUST be one of the brand names listed above",\n'
                '      "handle": "string",\n'
                '      "platform": "YouTube|Facebook",\n'
                '      "post_type": "paid|organic|both",\n'
                '      "paid_signal": "dom_label|statistical_outlier|declared|organic",\n'
                '      "metrics": {\n'
                '        "likes": integer, "comments": integer, "shares": integer,\n'
                '        "saves": integer, "views": integer, "followers": integer\n'
                "      },\n"
                '      "sentiment": "Positive|Neutral|Negative",\n'
                '      "top_posts": [\n'
                '        {"caption": "str", "url": "str or null", "post_type": "paid|organic", "likes": integer, "views": integer}\n'
                "      ],\n"
                '      "hashtags": ["#string"],\n'
                '      "content_themes": ["string"],\n'
                '      "paid_campaigns": ["string"]\n'
                "    }\n"
                "  ]\n"
                "}\n\n"
                "RULES:\n"
                "- name MUST be one of the exact brand names listed above — never null, never 'Unknown'\n"
                "- Output one record per brand per platform (YouTube + Facebook = 2 records per brand minimum)\n"
                "- metrics: use ONLY real numbers from the data provided. If a metric was not observed, set it to 0.\n"
                "  Do NOT invent or estimate numbers. Zeros are correct when data was not found.\n"
                "  Add a 'data_unavailable': true field on any record where all metrics are zero.\n"
                "- post_type per competitor must be 'paid', 'organic', or 'both'\n"
                "- paid_signal per competitor must be 'dom_label', 'statistical_outlier', 'declared', or 'organic'\n"
                "- top_posts: 1–5 items from real posts found; [] if none found\n"
                "- hashtags: 1–6 items from real posts; [] if none found\n"
                "- content_themes: 1–4 themes inferred from top_posts captions; [] if no posts found\n"
                "- paid_campaigns: list campaign names found; [] if none"
            ),
            expected_output=(
                "A raw JSON object (no markdown) with competitors array. "
                "Each entry: name (from brand list), handle, platform, post_type, paid_signal, metrics{6 real fields}, "
                "data_unavailable (true if all zeros), sentiment, top_posts[], hashtags[], content_themes[], paid_campaigns[]. "
                "Real numbers only — no fabricated estimates."
            ),
            agent=agent,
        )
