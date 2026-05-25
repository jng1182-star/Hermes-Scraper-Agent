import json
from datetime import datetime, timezone
from crewai import Task

# Single source of truth for active platforms (H2 fix)
ACTIVE_PLATFORMS = ["facebook", "instagram", "youtube", "tiktok"]

# NOTE: Instagram is modelled from Facebook (Meta Ad Library covers both surfaces).
# The "instagram" key in brand platforms is always populated from Facebook data,
# labelled as "Instagram (modelled from Facebook Page)" in all outputs.

_METHODOLOGY_DISCLAIMER = (
    "All values are directional Share-of-Voice indices (0–100 scale, "
    "Directional / Indexed – Not Actual Spend) reflecting relative advertising "
    "presence within the selected competitive set. These are estimates based on "
    "observable data (ad counts, reach proxies, presence signals) and do not "
    "represent actual spend figures. All indices are calculated within the context "
    "of the selected competitor group and represent share of voice among these "
    "competitors only, not an entire industry or market."
)


class SocialTasks:

    def profile_task(self, agent, params: dict = None) -> Task:
        """Agent 1 — baseline presence signals via official APIs."""
        params      = params or {}
        advertisers = params.get("advertisers", [])
        competitors = params.get("competitors", [])
        platforms   = params.get("platforms", ["YouTube", "Facebook"])
        country     = params.get("country", "")

        all_brands = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_str = ", ".join(all_brands) if all_brands else "all target brands"

        api_input = json.dumps({
            "brands": all_brands,
            "platforms": platforms,
            "country": country or "PH",
        })
        return Task(
            description=(
                f"Collect social media presence signals for these brands: {brands_str}.\n\n"
                f"PLATFORMS: {', '.join(platforms)}\n"
                f"COUNTRY/MARKET: {country or 'Global'}\n\n"
                "STEP 1 — Call the 'Brand API Data Fetcher' tool FIRST with this exact input:\n"
                f"{api_input}\n\n"
                "This tool uses the YouTube Data API v3 and Meta Ad Library API to return "
                "real subscriber counts, video view counts, likes, and declared ad data.\n\n"
                "For Meta Ad Library results, also extract:\n"
                "  - geo_presence: list of countries/regions each brand's ads are targeted at\n"
                "  - ad_start_dates: list of ISO date strings for all active ads "
                "(used to compute Ad Longevity and Creative Velocity signals)\n"
                "These are required fields alongside active_ads_found and impressions_min/max.\n\n"
                "STEP 2 — Return the full JSON output from the Brand API Data Fetcher tool. "
                "Do NOT call the Profile Baseline Scraper tool unless the API tool returns "
                "empty platform_data for every brand. "
                "If an API is unreachable or returns no data, log the issue and proceed "
                "with available signals — do not halt."
            ),
            expected_output=(
                "JSON from the Brand API Data Fetcher tool: per-brand platform data with "
                "presence signals from YouTube Data API v3 (subscribers, avg_views, avg_likes) "
                "and Meta Ad Library API (active_ads_found, impressions_min, impressions_max, "
                "geo_presence, ad_start_dates). "
                "data_source field must show 'youtube_data_api_v3' or 'meta_ad_library_api'."
            ),
            agent=agent,
        )

    def feed_task(self, agent, params: dict = None) -> Task:
        """Agent 2 — paid ad capture across Meta, YouTube, and TikTok."""
        params      = params or {}
        competitors = params.get("competitors", [])
        advertisers = params.get("advertisers", [])
        platforms   = params.get("platforms", ["YouTube", "Facebook"])
        country     = params.get("country", "PH")

        all_brands = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_str = ", ".join(all_brands) if all_brands else "all target brands"

        api_input = json.dumps({
            "brands": all_brands,
            "platforms": platforms,
            "country": country or "PH",
        })
        tiktok_input_example = json.dumps({"brand": "<brand_name>", "country": country or "PH"})
        meta_platforms = [p for p in platforms if p.lower() in ("facebook", "instagram", "youtube")]

        return Task(
            description=(
                f"Collect declared paid advertising signals for these brands: {brands_str}.\n\n"
                f"PLATFORMS: {', '.join(platforms)} + TikTok (always included)\n"
                f"COUNTRY/MARKET: {country or 'Global'}\n\n"
                "STEP 1 — Call the 'Brand API Data Fetcher' tool with this exact input:\n"
                f"{api_input}\n\n"
                "The Meta Ad Library API returns: active ad count, impression ranges "
                "(min/max), and ad creative text per brand.\n"
                "NOTE: The 'facebook' platform key covers both Facebook and Instagram — "
                "Meta Ad Library returns ads running on either surface under the same "
                "advertiser. Do not create a separate 'instagram' key; treat all Meta "
                "ads as part of the 'facebook' SOV bucket.\n\n"
                "STEP 2 — If the API tool returns empty Facebook data (active_ads_found "
                "missing or platform_data has no Facebook entry), call the "
                "'Paid Ad Library Scraper' tool once per brand:\n"
                f"  {{\"brand\": \"<brand_name>\", \"country\": \"{country or 'PH'}\", "
                f"\"platforms\": {json.dumps(meta_platforms)}}}\n\n"
                "STEP 3 — Call the 'TikTok Ad Library Tool' (tiktok_api_tool) for EACH brand:\n"
                f"  {tiktok_input_example}\n"
                "Capture:\n"
                "  - active_tiktok_ads: count of active ads\n"
                "  - tiktok_ad_start_dates: list of ISO date strings for each active ad\n"
                "  - tiktok_impressions_min, tiktok_impressions_max: from TikTok EU Ad Library "
                "unique user reach fields where the brand runs EU campaigns — these are the "
                "most reliable reach proxies TikTok exposes publicly. For non-EU markets "
                "this field will be null; record as missing and assign Low confidence to "
                "the reach bucket signal for TikTok on that brand.\n"
                "  - tiktok_geo_countries: list of countries where ads are running\n"
                "If TikTok API returns no data at all, record zero active ads with "
                "source_quality='search_fallback' and flag confidence as Low for TikTok. "
                "Do not halt — proceed with available signals.\n\n"
                "For ALL platforms, also extract:\n"
                "  - ad_start_dates: list of ISO date strings (for longevity + velocity)\n"
                "  - geo_countries: list of countries where ads are running\n"
                "  - new_ads_last_7d: count of ad IDs with start_date within the last 7 days "
                "(the Creative Velocity signal)\n\n"
                "Return all collected ad signals combined."
            ),
            expected_output=(
                "Per-brand paid ad presence signals from Meta Ad Library API, TikTok Ad Library, "
                "or Playwright scraper fallback: active_ads_found, impressions_min/max, "
                "ad_start_dates, geo_countries, new_ads_last_7d, ad_captions. "
                "Include tiktok_ prefixed equivalents for TikTok platform. "
                "Note which source was used: 'meta_ad_library_api', 'tiktok_api', or "
                "'playwright_scraper'. Note whether TikTok reach data is from EU library "
                "(reliable) or unavailable (null — Low confidence for reach bucket)."
            ),
            agent=agent,
        )

    def extraction_task(self, agent, query, params: dict = None) -> Task:
        """Agent 3 — supplementary search fallback for missing brand/platform signals."""
        params      = params or {}
        date_from   = params.get("date_from") or ""
        date_to     = params.get("date_to")   or ""
        date_range  = params.get("date_range", "Last 30 days")
        platforms   = params.get("platforms",  ["YouTube", "Facebook"])
        post_type   = params.get("post_type",  "both")
        advertisers = params.get("advertisers", [])
        competitors = params.get("competitors", [])
        uploaded    = params.get("uploaded_context", [])
        country     = params.get("country", "")
        markets     = params.get("markets", [country] if country else [])

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

        plat_str = ", ".join(platforms) if platforms else "YouTube, Facebook, TikTok"
        post_scope = {
            "paid":    "Focus ONLY on PAID content: ads, sponsored posts, boosted content, ad library entries.",
            "organic": "Focus ONLY on ORGANIC content: non-sponsored posts, UGC, viral content, creator posts.",
            "both":    "Collect BOTH paid and organic signals. Tag each data point as paid or organic.",
        }.get(post_type, "Collect both paid and organic signals.")

        upload_context = ""
        if uploaded:
            upload_context = "\n\nUPLOADED REFERENCE FILES (read fully — use any relevant context: competitor lists, briefs, brand data, geo targeting, campaign scale, creative themes, market presence signals):\n"
            for uf in uploaded[:10]:
                upload_context += f"\n--- {uf['filename']} ---\n{uf['content'][:8000]}\n"

        all_brands = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_str = ", ".join(all_brands) if all_brands else query
        markets_str = ", ".join(markets) if markets else "all markets"

        return Task(
            description=(
                f"Use the Social Media Intelligence Search tool to research: {query}\n\n"
                f"TARGET BRANDS: {brands_str}\n"
                f"TARGET MARKETS: {markets_str}\n"
                f"PLATFORMS: {plat_str} (include TikTok)\n"
                f"DATE SCOPE: {date_scope}\n"
                f"CONTENT TYPE: {post_scope}\n"
                f"{upload_context}\n\n"
                f"IMPORTANT — MARKET SCOPING: All data collected MUST be scoped to the target "
                f"market(s): {markets_str}. When searching, always append the market name to "
                f"your query (e.g. 'Brand Facebook ads Philippines'). Discard any results that "
                f"clearly relate to other markets. Tag every data point with the market it "
                f"belongs to using a 'market' field.\n\n"
                "Call the search tool ONCE per brand-platform-market combination. "
                "Do not call repeatedly for the same brand-platform combination.\n\n"
                "For EACH brand on EACH platform, extract PRESENCE SIGNALS:\n"
                "1. AD SIGNALS (primary):\n"
                "   - Ad campaign names, creatives, slogans\n"
                "   - Reach tier: impression range bucket from ad library "
                "(report min/max as-is — Low/Med/High/Very High)\n"
                "   - Sponsored post URLs and engagement counts\n"
                "   - ad_start_dates: list of ISO date strings for active ads\n"
                "   - geo_countries: list of countries where ads are running\n"
                "   - new_ads_last_7d: count of ad IDs launched in the last 7 days "
                "(the Creative Velocity signal)\n"
                "   For TikTok: use tiktok_api_tool explicitly if available; "
                "do not rely on search fallback for TikTok if the tool is reachable.\n"
                "2. ORGANIC SIGNALS (secondary corroboration):\n"
                "   - Follower/subscriber count\n"
                "   - Engagement rate (er_pct): parse '8% ER' as 8.0\n"
                "   - Top post captions, hashtags, viral themes\n"
                "   - Likes, comments, shares, views (parse '2.3M' as 2300000, '45K' as 45000)\n\n"
                "For each signal, tag its source_quality as:\n"
                "  'primary_api'     — YouTube API v3, Meta Ad Library, TikTok Ad Library API\n"
                "  'fallback_scraper'— Playwright DOM scrape\n"
                "  'search_fallback' — inferred from web search snippets\n"
                "This tag is required and will be used to assign confidence scores.\n\n"
                "From uploaded reference files, extract all relevant context: competitor brand lists, "
                "geo targeting data, campaign briefs, market presence evidence, creative themes, "
                "ad scale signals, and any brand-specific intelligence. Do NOT extract spend or CPM data."
            ),
            expected_output=(
                "A per-brand, per-platform report with: ad presence signals "
                "(active_ads_found, impressions range, ad_start_dates, geo_countries, "
                "new_ads_last_7d), organic corroboration signals (er_pct, follower count, "
                "top posts, hashtags, themes). Each signal tagged with source_quality. "
                "TikTok signals included where available."
            ),
            agent=agent,
        )

    def analysis_task(self, agent, prior_context: str = None, params: dict = None) -> Task:
        """Agent 4 — compute 6-signal SOV index per brand per platform."""
        params      = params or {}
        advertisers = params.get("advertisers", [])
        competitors = params.get("competitors", [])
        industry    = params.get("industry", "")
        markets     = params.get("markets", [params.get("country", "")])
        all_brands  = list(advertisers) + [c for c in competitors if c not in advertisers]
        brands_seed = ", ".join(f'"{b}"' for b in all_brands) if all_brands else ""
        markets_str = ", ".join(m for m in markets if m) or "Global"

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        prefix = ""
        if prior_context:
            snippet = prior_context[:6000]
            if len(prior_context) > 6000:
                snippet += "\n[... TRUNCATED — earlier data omitted to stay within token limit ...]"
            is_first_party = "AGENT 1:" in snippet or "AGENT 2:" in snippet
            if is_first_party:
                prefix = (
                    "DATA CONTEXT (first-party API/DOM data):\n"
                    "The following data was collected directly from platform APIs and pages "
                    "by Agent 1 (profile baseline) and Agent 2 (feed ad capture). "
                    "Use these values directly — do NOT re-estimate when real signals are present.\n\n"
                    + snippet + "\n\n"
                )
            else:
                prefix = (
                    "[RESUMED FROM CHECKPOINT / SEARCH FALLBACK]\n"
                    "The following data was collected via web search or fallback scraping. "
                    "Treat as lower confidence than primary API values.\n\n"
                    + snippet + "\n\n"
                )

        brands_line = (
            f"\nBRANDS IN THIS SCAN: {brands_seed}\n"
            f"MARKETS: {markets_str}\n"
            f"TODAY'S DATE (for longevity/velocity calculation): {today_str}\n"
        ) if brands_seed else f"\nTODAY'S DATE: {today_str}\n"

        return Task(
            description=(
                prefix +
                brands_line +
                "\nAnalyse the extracted social media data and compute a SHARE-OF-VOICE INDEX "
                "for each brand. Do NOT compute dollar spend values. Do NOT classify ER as a "
                "paid signal. Compute SOV for all platforms with equal rigor.\n\n"
                "INSTAGRAM NOTE: Instagram data is modelled from the associated Facebook Page "
                "(Meta Ad Library covers both surfaces). Copy the facebook signals to an "
                "'instagram' key and add data_source='modelled_from_facebook'. Do NOT fabricate "
                "separate Instagram data.\n\n"
                "TIKTOK SUPPRESSION RULE: After collecting TikTok post data for all brands in "
                "a given market, count the TOTAL number of TikTok posts detected (across all brands). "
                "If total TikTok posts < 2 for a market, set tiktok_suppressed=true for that market "
                "and exclude TikTok from the composite SOV formula (re-weight remaining platforms). "
                "This is per-market: one market may suppress TikTok while another does not.\n\n"
                "For each brand, produce one SOV record per platform "
                "(facebook, instagram [modelled], youtube, tiktok):\n\n"
                "SOV SIGNAL COMPUTATION (normalize each signal 0–100 across all brands):\n\n"
                "1. CREATIVE VOLUME SCORE (weight 30% — updated from 35%):\n"
                "   Input: active_ads_found per brand per platform\n"
                "   Formula: (brand_ads / sum_all_brand_ads_on_platform) × 100\n"
                "   If no ad data: score = 0, mark source_quality='missing'\n\n"
                "2. CREATIVE VELOCITY SCORE (weight 10%):\n"
                "   Input: new_ads_last_7d per brand (ad IDs with start_date within last 7 days)\n"
                "   Formula: (brand_new_ads_7d / sum_all_brand_new_ads_7d_on_platform) × 100\n"
                "   If no start_date data: score = 0, mark source_quality='missing'\n\n"
                "3. AD LONGEVITY SCORE (weight 15%):\n"
                "   Input: ad_start_dates list per brand\n"
                "   Formula: avg days since earliest start_date → normalize by max across brands × 100\n"
                "   If no start dates: score = 0, mark source_quality='missing'\n\n"
                "4. PLATFORM PRESENCE SCORE (weight 15% — replaces Geo Presence):\n"
                "   Input: number of active platforms per brand (max 4: facebook, instagram, youtube, tiktok)\n"
                "   Formula: (brand_active_platform_count / max_active_platform_count) × 100\n"
                "   Store result in 'geo_presence_score' field for schema compatibility.\n\n"
                "5. REACH BUCKET SCORE (weight 15%):\n"
                "   Input: impressions_min, impressions_max\n"
                "   Map range to tier: <1K=1, 1K–10K=2, 10K–100K=3, >100K=4\n"
                "   Normalize: (brand_tier / 4) × 100\n"
                "   If no impression data: score = 0, mark source_quality='missing'\n"
                "   NOTE: For TikTok, reach data only available from EU Ad Library. "
                "If brand has no EU campaigns, assign score=0 and Low confidence for this signal.\n\n"
                "6. ENGAGEMENT CORROBORATION (weight 15% — updated from 10%):\n"
                "   Input: er_pct (engagement rate %)\n"
                "   Formula: (brand_er / category_er_benchmark) × 50 (capped at 100)\n"
                "   Category ER benchmarks — Facebook: General 0.8%, Beauty 1.1%, "
                "Food 1.0%, Finance 0.5%. YouTube: General 2.0%, Beauty 3.0%, Finance 1.5%.\n"
                "   Secondary signal only — NOT a spend proxy.\n\n"
                "COMPOSITE SOV PER PLATFORM (updated formula):\n"
                "   sov_index = (vol×0.30) + (velocity×0.10) + (longevity×0.15) "
                "+ (platform_presence×0.15) + (reach×0.15) + (engagement×0.15)\n"
                "   Round to one decimal. Must be in [0, 100].\n\n"
                "PAID/ORGANIC CLASSIFICATION (per post):\n"
                "ROUTE 1 — Ad Library (primary): If a post_id or ad_id appears in the Meta Ad Library, "
                "YouTube Ads Transparency, or TikTok Commercial Content Library result, classify as "
                "post_classification='Paid (Confirmed)', classification_confidence='High'.\n"
                "ROUTE 2 — Engagement baseline fallback: For posts NOT found in ad libraries:\n"
                "  - Compute the brand's median engagement (likes+comments+shares+views) across "
                "all posts on that platform.\n"
                "  - If a post's engagement > 2× median OR in top 10% of brand posts: classify as "
                "post_classification='Paid (Est.)', classification_confidence='Medium'.\n"
                "  - Otherwise: post_classification='Organic', classification_confidence='High'.\n"
                "Add these fields to each object in top_posts[]. "
                "If no ad library data is available for a market (APAC), Route 2 is the live classifier.\n\n"
                "CONTENT TYPE CLASSIFICATION (per post):\n"
                "For each post in top_posts[], determine content_type:\n"
                "  - 'brand_say': post from the brand's own account with no external person featured\n"
                "  - 'sma': brand post featuring an influencer, creator, or partner (detect via tags, "
                "mentions, or caption references to external persons)\n"
                "  - 'others_say': post where the PUBLISHER is NOT the brand (third-party ad library "
                "result where publisher handle ≠ brand handle). Source: Ad Library 3rd-party "
                "sponsored posts only.\n"
                "Add content_type field to each top_posts[] object.\n\n"
                "KEYWORDS (per brand, per content type):\n"
                "From all post captions, extract top-10 keywords for each content type "
                "(brand_say, sma, others_say). Remove stopwords (the, and, a, to, in, for, is, of, "
                "on, with, at, by, it, be, as, or, an, we, our, you, your, this, that, are, was, "
                "will, have, has, can, all, not). Exclude the brand name itself. "
                "Return as keywords_by_type: {brand_say:[...], sma:[...], others_say:[...]}.\n\n"
                "TIME-GRAIN DATA:\n"
                "If ad_start_dates or post dates are available, emit:\n"
                "  by_month: [{period: 'YYYY-MM', composite_sov: <number>}]\n"
                "  by_week:  [{period: 'YYYY-WNN', composite_sov: <number>}]\n"
                "  by_day:   [{period: 'YYYY-MM-DD', composite_sov: <number>}]\n"
                "Only include periods with at least one data point. If insufficient date data, "
                "emit empty arrays.\n\n"
                "CONFIDENCE — BASE TIER (per brand per platform):\n"
                "   High:   ≥3 signals with source_quality='primary_api' and score > 0\n"
                "   Medium: 2 primary_api signals, or 1 primary_api + 1 fallback_scraper\n"
                "   Low:    ≤1 signal, or only search_fallback\n\n"
                "CROSS-SIGNAL CONSISTENCY CHECK (apply after base tier):\n"
                "   Step 1: Rank all brands on Creative Volume (rank 1 = most ads)\n"
                "   Step 2: Rank all brands on Reach Bucket Score (rank 1 = highest tier)\n"
                "   Step 3: Compute |creative_volume_rank - reach_bucket_rank| per brand\n"
                "   Step 4: If divergence > 2 positions: set consistency_flag = true, "
                "downgrade confidence one tier (High→Medium, Medium→Low)\n"
                "   A brand with consistency_flag = true can NEVER have confidence above Medium.\n\n"
                "COMPOSITE CROSS-PLATFORM SOV:\n"
                "   Default weights: facebook×0.35 + instagram×0.15 + youtube×0.30 + tiktok×0.20\n"
                "   If TikTok suppressed (tiktok_posts < 2): facebook×0.44 + instagram×0.19 + youtube×0.37\n"
                "   If YouTube missing: facebook×0.50 + instagram×0.21 + tiktok×0.29\n"
                "   If Facebook/Instagram missing: youtube×0.60 + tiktok×0.40\n"
                "   composite_confidence = lowest confidence tier across all included platforms.\n\n"
                "EXECUTIVE INSIGHTS (generate after all brand SOVs computed):\n"
                "For each brand, identify up to 2 strategic observations:\n"
                "  - If a brand has high creative_volume but low engagement: note content resonance risk\n"
                "  - If sma posts > 50% of content_type: flag as collaboration-heavy strategy\n"
                "  - If composite_sov declined across time periods: flag declining momentum\n"
                "  - If composite_sov > 40%: flag dominant position\n"
                "Return as insights[]: [{brand: '...', text: '...', icon: '📉|📈|🤝|⚠️|👑'}]\n"
            ),
            expected_output=(
                "A list of brand SOV records: name, markets[], "
                "platforms{facebook|instagram|youtube|tiktok: "
                "{sov_index, sov_label, confidence, consistency_flag, "
                "signals{creative_volume_share, creative_velocity_score, longevity_score, "
                "geo_presence_score, reach_bucket_score, engagement_corroboration}, posts[]}}, "
                "composite_sov, composite_confidence, content_themes, hashtags, "
                "top_posts[{caption, url, platform, likes, views, post_classification, "
                "classification_confidence, content_type}], sentiment, "
                "keywords_by_type{brand_say, sma, others_say}, "
                "by_month[], by_week[], by_day[]. "
                "Plus tiktok_suppressed (bool or {market: bool}), insights[]. "
                "No dollar values."
            ),
            agent=agent,
        )

    def reporting_task(self, agent, prior_context: str = None, params: dict = None) -> Task:
        """Agent 5 — format final SOV JSON report."""
        params      = params or {}
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
                "[RESUMED FROM CHECKPOINT]\n"
                "The analyst already computed the SOV indices. Do NOT re-analyse. "
                f"Format directly into the required JSON schema:\n\n{snippet}\n\n"
            )

        return Task(
            description=(
                prefix +
                f"BRANDS IN THIS SCAN: {brands_seed}\n\n"
                "Format the analysed SOV data into a single valid JSON object. "
                "Output ONLY the JSON — no markdown, no code fences, no extra text.\n\n"
                "Use this exact schema:\n"
                "{\n"
                f'  "methodology_disclaimer": "{_METHODOLOGY_DISCLAIMER}",\n'
                '  "scan_params": {},\n'
                '  "tiktok_suppressed": false,\n'
                '  "insights": [{"brand": "string", "text": "string", "icon": "string"}],\n'
                '  "brands": [\n'
                "    {\n"
                '      "name": "string  ← MUST be one of the brand names listed above",\n'
                '      "markets": ["string ← market name(s) this brand was scanned in"],\n'
                '      "platforms": {\n'
                '        "facebook": {\n'
                '          "sov_index": <number 0–100>,\n'
                '          "sov_label": "<sov_index> (Directional / Indexed – Not Actual Spend)",\n'
                '          "confidence": "High|Medium|Low",\n'
                '          "consistency_flag": false,\n'
                '          "signals": {\n'
                '            "creative_volume_share": <number>,\n'
                '            "creative_velocity_score": <number>,\n'
                '            "longevity_score": <number>,\n'
                '            "geo_presence_score": <number>,\n'
                '            "reach_bucket_score": <number>,\n'
                '            "engagement_corroboration": <number>\n'
                "          },\n"
                '          "posts": [{"post_id":"","post_classification":"Paid (Confirmed)|Paid (Est.)|Organic","classification_confidence":"High|Medium|Low","content_type":"brand_say|sma|others_say"}]\n'
                "        },\n"
                '        "instagram": { "... same structure ... data_source: modelled_from_facebook" },\n'
                '        "youtube": { "... same structure ..." },\n'
                '        "tiktok":  { "... same structure ..." }\n'
                "      },\n"
                '      "composite_sov": <number 0–100>,\n'
                '      "composite_sov_label": "<composite_sov> (Directional / Indexed – Not Actual Spend)",\n'
                '      "composite_confidence": "High|Medium|Low",\n'
                '      "content_themes": ["string"],\n'
                '      "hashtags": ["#string"],\n'
                '      "top_posts": [{"caption":"","url":"","platform":"","likes":0,"views":0,'
                '"post_classification":"Paid (Confirmed)|Paid (Est.)|Organic",'
                '"classification_confidence":"High|Medium|Low","content_type":"brand_say|sma|others_say"}],\n'
                '      "keywords_by_type": {"brand_say":["string"],"sma":["string"],"others_say":["string"]},\n'
                '      "by_month": [{"period":"YYYY-MM","composite_sov":<number>}],\n'
                '      "by_week":  [{"period":"YYYY-WNN","composite_sov":<number>}],\n'
                '      "by_day":   [{"period":"YYYY-MM-DD","composite_sov":<number>}],\n'
                '      "sentiment": "Positive|Neutral|Negative"\n'
                "    }\n"
                "  ],\n"
                '  "category_totals": {\n'
                '    "facebook_total_ads": <integer>,\n'
                '    "youtube_total_videos": <integer>,\n'
                '    "tiktok_total_ads": <integer>,\n'
                '    "scan_date": "<ISO date>"\n'
                "  }\n"
                "}\n\n"
                "RULES:\n"
                "- name MUST be one of the exact brand names listed above\n"
                "- sov_index values across all brands for the same platform SHOULD sum to ~100\n"
                "- composite_sov values across all brands SHOULD sum to ~100\n"
                "- Every sov_index must have a matching sov_label with the directional qualifier\n"
                "- confidence MUST be 'High', 'Medium', or 'Low' only\n"
                "- A brand with consistency_flag = true cannot have confidence above 'Medium'\n"
                "- instagram platform entry MUST copy facebook signals with data_source='modelled_from_facebook'\n"
                "- tiktok platform entry is REQUIRED for every brand (use score=0 + Low confidence "
                "if TikTok data was unavailable). If tiktok_suppressed=true, still include tiktok "
                "entry with score=0 and note='suppressed — fewer than 2 posts detected'\n"
                "- top_posts MUST include post_classification, classification_confidence, content_type\n"
                "- keywords_by_type MUST be present (empty arrays if no post text available)\n"
                "- tiktok_suppressed field MUST be present (false if TikTok data available)\n"
                "- insights[] MUST be present (empty array if no insights generated)\n"
                "- NO dollar amounts, NO paid_signal, NO cpm_used, NO estimated_spend_usd anywhere\n"
                "- methodology_disclaimer must be present exactly as provided above\n"
            ),
            expected_output=(
                "A raw JSON object (no markdown) with methodology_disclaimer, tiktok_suppressed, "
                "insights[], scan_params, brands[] (each with markets[], platforms{facebook,instagram,"
                "youtube,tiktok} containing sov_index, sov_label, confidence, consistency_flag, "
                "signals{6 fields}, posts[{post_classification,classification_confidence,content_type}]; "
                "plus composite_sov, composite_sov_label, composite_confidence, content_themes, "
                "hashtags, top_posts[{post_classification,classification_confidence,content_type}], "
                "keywords_by_type{brand_say,sma,others_say}, by_month[], by_week[], by_day[], "
                "sentiment), and category_totals. No dollar values anywhere."
            ),
            agent=agent,
        )
