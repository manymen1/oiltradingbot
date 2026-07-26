from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlparse

from .types import PlannedSource

# Small curated registry mapping geopolitical actors to official domains and
# news-discovery hooks. Used by the fixture rule analyzer (party extraction)
# and the source-plan builder (official feeds per party). Deliberately
# conservative: unknown actors simply get wire + Google News coverage.

# Wires plus the tier-one broadsheets the market rules treat as the
# resolution standard ("a consensus of credible reporting"). Reuters and AP
# have no public RSS (both 401), so in practice the broadsheets are what we
# actually receive first on our own infrastructure.
WIRE_DOMAINS = [
    "reuters.com",
    "apnews.com",
    "afp.com",
    "nytimes.com",
    "washingtonpost.com",
    "wsj.com",
    "theguardian.com",
    "bbc.com",
]

# actor key -> (aliases for detection, official domains)
ACTORS: dict[str, tuple[list[str], list[str]]] = {
    # war.gov: defense.gov now 301-redirects to war.gov ("Department of War"),
    # so Pentagon announcements arrive on a host that does NOT match
    # "defense.gov" under domain_allowed()'s exact/suffix rule -- the decisive
    # source for the Iran halt-in-offensive markets would have been demoted to
    # alert-only. Both are listed until the rename fully settles.
    "united_states": (["united states", "u.s.", "us ", "washington", "white house", "state department", "pentagon", "department of war"], ["state.gov", "whitehouse.gov", "defense.gov", "war.gov", "centcom.mil"]),
    "iran": (["iran", "tehran", "iranian"], ["mfa.gov.ir"]),
    "israel": (["israel", "jerusalem", "israeli", "idf"], ["gov.il", "mfa.gov.il"]),
    "russia": (["russia", "moscow", "kremlin", "russian"], ["mid.ru", "kremlin.ru"]),
    "ukraine": (["ukraine", "kyiv", "ukrainian"], ["mfa.gov.ua", "president.gov.ua"]),
    "china": (["china", "beijing", "chinese", "prc"], ["fmprc.gov.cn"]),
    "taiwan": (["taiwan", "taipei"], ["mofa.gov.tw"]),
    "north_korea": (["north korea", "pyongyang", "dprk"], []),
    "south_korea": (["south korea", "seoul"], ["mofa.go.kr"]),
    "qatar": (["qatar", "doha", "qatari"], ["mofa.gov.qa"]),
    "oman": (["oman", "muscat", "omani"], ["fm.gov.om"]),
    "saudi_arabia": (["saudi", "riyadh"], ["mofa.gov.sa"]),
    "uae": (["united arab emirates", "abu dhabi", "emirati", "uae"], ["mofaic.gov.ae"]),
    "turkey": (["turkey", "türkiye", "ankara", "turkish"], ["mfa.gov.tr"]),
    "egypt": (["egypt", "cairo", "egyptian"], ["mfa.gov.eg"]),
    "pakistan": (["pakistan", "islamabad", "pakistani"], ["mofa.gov.pk"]),
    "india": (["india", "new delhi", "indian"], ["mea.gov.in"]),
    "switzerland": (["switzerland", "geneva", "bern", "swiss"], ["eda.admin.ch"]),
    "united_kingdom": (["united kingdom", "britain", "london", "british", "uk "], ["gov.uk"]),
    "france": (["france", "paris", "french"], ["diplomatie.gouv.fr"]),
    "germany": (["germany", "berlin", "german"], ["auswaertiges-amt.de"]),
    "european_union": (["european union", "brussels", "eu "], ["europa.eu"]),
    "united_nations": (["united nations", "security council", "un "], ["un.org"]),
    "nato": (["nato"], ["nato.int"]),
    "venezuela": (["venezuela", "caracas"], []),
    "gaza": (["gaza", "hamas"], []),
    "lebanon": (["lebanon", "beirut", "hezbollah"], []),
    "syria": (["syria", "damascus"], []),
    "yemen": (["yemen", "houthi", "sanaa"], []),
    "iraq": (["iraq", "baghdad"], []),
    "afghanistan": (["afghanistan", "kabul", "taliban"], []),
}

# Known mediator actors for diplomacy markets: appearing at all suggests a
# mediator role worth watching even when not a direct party.
MEDIATOR_ACTORS = ["qatar", "oman", "switzerland", "egypt", "turkey", "united_nations"]

# Direct publisher RSS endpoints -- minutes faster than Google News indexing,
# which is the dominant latency in the confirmed-entry race. Only feeds with
# stable public URLs are listed; wires without public RSS still go through
# Google News queries.
# VERIFIED 2026-07-20 with scripts/probe_feeds.sh -- every URL here returned
# HTTP 200 AND a non-zero <item> count. The previous two entries
# (state.gov/rss-feed/press-releases, news.un.org) both returned 200 with ZERO
# items: the system believed it had fast official sources and was actually
# receiving nothing, silently leaving Google News (5-15 min indexing lag) as
# the only path. Re-probe before trusting any addition here.
DIRECT_ACTOR_FEEDS: dict[str, list[str]] = {
    "united_states": [
        # Pentagon news + press releases (defense.gov redirects here).
        "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20",
        "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=800&Site=945&max=20",
        # Formal presidential actions (EOs, proclamations, memoranda).
        "https://www.whitehouse.gov/presidential-actions/feed/",
    ],
}

# Ordered by MEASURED freshness (scripts/probe_feeds.sh, 2026-07-20), not by
# reputation. These markets resolve on "a consensus of credible reporting",
# so the trigger is the first credible REPORT -- official government feeds
# measured 2.7 to 5.6 DAYS stale and cannot serve that role. Polling a feed
# only buys reaction time; DEFAULT_AUTO_TRADE_DOMAINS still decides which
# source may authorize a trade.
GENERAL_FAST_FEEDS: list[str] = [
    # 0 min at probe: tier-one broadsheet, own infrastructure.
    "https://www.theguardian.com/world/middleeast/rss",
    # 0 min at probe.
    "https://www.cbsnews.com/latest/rss/world",
    # 7 and 20 min at probe.
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/MiddleEast.xml",
    # 8 min: regional specialist, often first on Gulf/Iran detail.
    "https://www.middleeasteye.net/rss",
    # 10 min: wire agency, fast on Middle East (state-affiliated, so it is a
    # speed source only -- deliberately NOT auto-trade eligible).
    "https://www.aa.com.tr/en/rss/default?cat=middle-east",
    # 29-36 min: broad regional coverage.
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.timesofisrael.com/feed/",
    "https://www.france24.com/en/middle-east/rss",
]


_PUBLISHERS: dict[str, dict[str, object]] = {
    "reuters": {
        "aliases": ["reuters", "reuters.com"],
        "domains": ["reuters.com"],
        "tier": "wire",
        "timestamp_quality": "exact",
    },
    "associated_press": {
        "aliases": ["associated press", "ap", "ap news", "apnews.com"],
        "domains": ["apnews.com"],
        "tier": "wire",
        "timestamp_quality": "exact",
    },
    "afp": {
        "aliases": ["afp", "agence france-presse", "afp.com"],
        "domains": ["afp.com"],
        "tier": "wire",
        "timestamp_quality": "exact",
    },
    "new_york_times": {
        "aliases": ["new york times", "the new york times", "nytimes.com"],
        "domains": ["nytimes.com"],
        "tier": "tier_one_press",
        "timestamp_quality": "exact",
    },
    "washington_post": {
        "aliases": ["washington post", "the washington post", "washingtonpost.com"],
        "domains": ["washingtonpost.com"],
        "tier": "tier_one_press",
        "timestamp_quality": "exact",
    },
    "wall_street_journal": {
        "aliases": ["wall street journal", "the wall street journal", "wsj.com"],
        "domains": ["wsj.com"],
        "tier": "tier_one_press",
        "timestamp_quality": "exact",
    },
    "guardian": {
        "aliases": ["guardian", "the guardian", "theguardian.com"],
        "domains": ["theguardian.com"],
        "tier": "tier_one_press",
        "timestamp_quality": "exact",
    },
    "bbc": {
        "aliases": ["bbc", "bbc news", "bbc.com"],
        "domains": ["bbc.com", "bbc.co.uk"],
        "tier": "tier_one_press",
        "timestamp_quality": "exact",
    },
    "cbs_news": {
        "aliases": ["cbs", "cbs news", "cbsnews.com"],
        "domains": ["cbsnews.com"],
        "tier": "major_press",
        "timestamp_quality": "exact",
    },
    "middle_east_eye": {
        "aliases": ["middle east eye", "middleeasteye.net"],
        "domains": ["middleeasteye.net"],
        "tier": "regional_press",
        "timestamp_quality": "exact",
    },
    "anadolu": {
        "aliases": ["anadolu", "aa", "aa.com.tr"],
        "domains": ["aa.com.tr"],
        "tier": "state_affiliated_press",
        "timestamp_quality": "exact",
    },
    "al_jazeera": {
        "aliases": ["al jazeera", "aljazeera.com"],
        "domains": ["aljazeera.com"],
        "tier": "regional_press",
        "timestamp_quality": "exact",
    },
    "times_of_israel": {
        "aliases": ["times of israel", "timesofisrael.com"],
        "domains": ["timesofisrael.com"],
        "tier": "regional_press",
        "timestamp_quality": "exact",
    },
    "france24": {
        "aliases": ["france 24", "france24.com"],
        "domains": ["france24.com"],
        "tier": "major_press",
        "timestamp_quality": "exact",
    },
}

_FAST_FEEDS_BY_ORG: dict[str, list[str]] = {
    "guardian": [GENERAL_FAST_FEEDS[0]],
    "cbs_news": [GENERAL_FAST_FEEDS[1]],
    "new_york_times": [GENERAL_FAST_FEEDS[2], GENERAL_FAST_FEEDS[3]],
    "middle_east_eye": [GENERAL_FAST_FEEDS[4]],
    "anadolu": [GENERAL_FAST_FEEDS[5]],
    "al_jazeera": [GENERAL_FAST_FEEDS[6]],
    "times_of_israel": [GENERAL_FAST_FEEDS[7]],
    "france24": [GENERAL_FAST_FEEDS[8]],
}

_CURATED_DIRECT_DOMAINS = {
    str(item).casefold().removeprefix("www.").strip(".")
    for raw in _PUBLISHERS.values()
    for item in raw["domains"]
}
_CURATED_DIRECT_DOMAINS.update(
    str(item).casefold().removeprefix("www.").strip(".")
    for _aliases, domains in ACTORS.values()
    for item in domains
)

# Domains frequently hosting republished copy. They are not inherently a
# second source: evidence extraction must preserve the originating byline.
SYNDICATION_HOSTS = {
    "finance.yahoo.com",
    "news.yahoo.com",
    "aol.com",
    "msn.com",
    "marketscreener.com",
    "swissinfo.ch",
}


def publisher_sources() -> list[PlannedSource]:
    sources: list[PlannedSource] = []
    for organization_id, raw in _PUBLISHERS.items():
        domains = list(raw["domains"])
        for index, domain in enumerate(domains):
            tier = str(raw["tier"])
            sources.append(
                PlannedSource(
                    source_id=(
                        organization_id
                        if index == 0
                        else f"{organization_id}:{domain}"
                    ),
                    organization_id=organization_id,
                    independence_group=organization_id,
                    domain=domain,
                    source_tier=tier,
                    feed_urls=list(_FAST_FEEDS_BY_ORG.get(organization_id, [])),
                    roles=(
                        ["CONFIRMATION", "CONTEXT"]
                        if tier in {"wire", "tier_one_press"}
                        else ["CONTEXT"]
                    ),
                    timestamp_quality=str(raw["timestamp_quality"]),
                )
            )
    return sources


def actor_sources(actors: list[str]) -> list[PlannedSource]:
    sources: list[PlannedSource] = []
    for actor in actors:
        entry = ACTORS.get(actor)
        if entry is None:
            continue
        feeds = DIRECT_ACTOR_FEEDS.get(actor, [])
        for domain in entry[1]:
            matching_feeds = [
                url
                for url in feeds
                if _domain(url) == domain
                or _domain(url).endswith(f".{domain}")
                or domain in {"defense.gov", "war.gov"}
                and _domain(url) in {"defense.gov", "war.gov"}
            ]
            sources.append(
                PlannedSource(
                    source_id=f"official:{actor}:{domain}",
                    organization_id=f"government:{actor}",
                    independence_group=f"government:{actor}",
                    domain=domain,
                    source_tier="official",
                    feed_urls=matching_feeds,
                    roles=["CONFIRMATION", "CONTEXT"],
                    timestamp_quality="exact_or_press_release",
                )
            )
    return sources


def resolve_source_reference(
    source_ref: str,
    *,
    roles: list[str],
    required: bool,
) -> list[PlannedSource]:
    """Resolve a named rule source to canonical identities.

    Unknown prose is not guessed. A literal domain/URL is still usable as a
    named oracle because its identity is deterministic.
    """

    normalized = " ".join(source_ref.casefold().split())
    domain = _source_ref_domain(source_ref)
    matches: list[PlannedSource] = []
    for item in publisher_sources():
        raw = _PUBLISHERS[item.organization_id]
        aliases = [str(alias).casefold() for alias in raw["aliases"]]
        if normalized in aliases or domain == item.domain:
            matches.append(item)
    for actor, (aliases, domains) in ACTORS.items():
        actor_name_match = (
            normalized == actor.replace("_", " ")
            or normalized in {alias.strip().casefold() for alias in aliases}
        )
        if actor_name_match or domain in domains:
            actor_matches = actor_sources([actor])
            if domain:
                actor_matches = [
                    item for item in actor_matches if item.domain == domain
                ]
            matches.extend(actor_matches)
    if not matches and domain:
        matches.append(
            PlannedSource(
                source_id=f"named:{domain}",
                organization_id=f"named:{domain}",
                independence_group=f"named:{domain}",
                domain=domain,
                source_tier="named_oracle",
                # Unknown rule-text URLs are semantic identities, not an
                # outbound-fetch allowlist. They remain discoverable through
                # aggregators until an operator adds the domain to the
                # curated publisher/actor registry.
                poll_urls=[],
                roles=[],
                timestamp_quality="unknown",
            )
        )
    resolved: list[PlannedSource] = []
    for item in _dedupe_sources(matches):
        poll_urls = (
            list(item.poll_urls)
            if _curated_direct_domain(item.domain)
            else []
        )
        if (
            required
            and "SETTLEMENT" in roles
            and _curated_direct_domain(item.domain)
            and not item.feed_urls
            and not poll_urls
        ):
            literal = source_ref.strip()
            poll_urls = [
                (
                    literal
                    if literal.startswith(("http://", "https://"))
                    else f"https://{item.domain}/"
                )
            ]
        resolved.append(
            replace(
                item,
                poll_urls=sorted(set(poll_urls)),
                roles=sorted(set(item.roles) | set(roles)),
                required=item.required or required,
            )
        )
    return resolved


def _curated_direct_domain(domain: str) -> bool:
    normalized = domain.casefold().removeprefix("www.").strip(".")
    return normalized in _CURATED_DIRECT_DOMAINS


def source_identity(
    domain: str,
    *,
    origin_organization: str = "",
    byline: str = "",
) -> tuple[str, str]:
    """Return (organization, independence group) for corroboration.

    A Reuters article mirrored by Yahoo remains Reuters. Two mirrors therefore
    add one independent source, not two.
    """

    # A mirror may identify its own host as the origin while preserving the
    # wire credit only in the byline. Resolve both fields independently and
    # prefer a recognized credited byline; never let "Yahoo" or "MSN" mask a
    # Reuters/AP/AFP attribution.
    normalized_byline = _normalize_origin(byline)
    if normalized_byline:
        return normalized_byline, normalized_byline
    normalized_origin = _normalize_origin(origin_organization)
    if normalized_origin:
        return normalized_origin, normalized_origin
    normalized_domain = domain.casefold().removeprefix("www.").strip(".")
    for item in publisher_sources():
        if normalized_domain == item.domain or normalized_domain.endswith(
            f".{item.domain}"
        ):
            return item.organization_id, item.independence_group
    if normalized_domain in SYNDICATION_HOSTS:
        return (
            f"syndication_host:{normalized_domain}",
            f"syndication_host:{normalized_domain}",
        )
    for actor_source in actor_sources(list(ACTORS)):
        if normalized_domain == actor_source.domain or normalized_domain.endswith(
            f".{actor_source.domain}"
        ):
            return (
                actor_source.organization_id,
                actor_source.independence_group,
            )
    return normalized_domain, normalized_domain


def independent_source_count(
    evidence: list[dict[str, str]],
) -> int:
    groups: set[str] = set()
    for item in evidence:
        _organization, group = source_identity(
            item.get("domain", ""),
            origin_organization=item.get("origin_organization", ""),
            byline=item.get("byline", ""),
        )
        if group:
            groups.add(group)
    return len(groups)


def _normalize_origin(value: str) -> str:
    import re

    normalized = " ".join(value.casefold().split())
    for organization_id, raw in _PUBLISHERS.items():
        aliases = [str(alias).casefold() for alias in raw["aliases"]]
        for alias in aliases:
            if (
                len(alias) <= 3
                and re.search(rf"\b{re.escape(alias)}\b", normalized)
            ) or (len(alias) > 3 and alias in normalized):
                return organization_id
    return ""


def _source_ref_domain(source_ref: str) -> str:
    text = source_ref.strip()
    if "://" in text:
        return _domain(text)
    for token in text.replace(",", " ").split():
        cleaned = token.strip(".,;:()[]\"'").casefold().removeprefix("www.")
        if "." in cleaned and all(cleaned.split(".")):
            return cleaned
    return ""


def _domain(url: str) -> str:
    return urlparse(url).netloc.casefold().removeprefix("www.")


def _dedupe_sources(sources: list[PlannedSource]) -> list[PlannedSource]:
    by_key: dict[tuple[str, str], PlannedSource] = {}
    for item in sources:
        key = (item.organization_id, item.domain)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue
        by_key[key] = replace(
            existing,
            feed_urls=sorted(set(existing.feed_urls) | set(item.feed_urls)),
            poll_urls=sorted(set(existing.poll_urls) | set(item.poll_urls)),
            roles=sorted(set(existing.roles) | set(item.roles)),
            required=existing.required or item.required,
        )
    return sorted(
        by_key.values(),
        key=lambda item: (item.organization_id, item.domain),
    )


def direct_feeds(actors: list[str]) -> list[str]:
    feeds: list[str] = []
    for actor in actors:
        feeds.extend(DIRECT_ACTOR_FEEDS.get(actor, []))
    return feeds

# Decisive-event vocabulary by broad market family; the fixture analyzer uses
# these to seed escalate keywords when the rules match the family.
EVENT_FAMILIES: dict[str, list[str]] = {
    "talks": ["talks", "negotiations", "meeting", "summit", "round", "dialogue", "convene", "delegation"],
    "ceasefire": ["ceasefire", "truce", "cessation of hostilities", "armistice"],
    "strike": ["strike", "attack", "missile", "drone", "bomb", "airstrike"],
    "sanctions": ["sanction", "sanctions", "embargo", "export controls"],
    "election": ["election", "vote", "ballot", "runoff", "inaugurat"],
    "agreement": ["agreement", "deal", "treaty", "accord", "sign"],
    "leadership": ["resign", "impeach", "coup", "oust", "successor", "steps down"],
}


# Broad region buckets for the portfolio's second correlation dimension:
# different party sets in one theater still move together on contagion.
ACTOR_REGIONS: dict[str, str] = {
    "united_states": "north_america",
    "iran": "middle_east", "israel": "middle_east", "qatar": "middle_east",
    "oman": "middle_east", "saudi_arabia": "middle_east", "uae": "middle_east",
    "egypt": "middle_east", "gaza": "middle_east", "lebanon": "middle_east",
    "syria": "middle_east", "yemen": "middle_east", "iraq": "middle_east",
    "turkey": "middle_east",
    "russia": "eastern_europe", "ukraine": "eastern_europe",
    "china": "east_asia", "taiwan": "east_asia", "north_korea": "east_asia", "south_korea": "east_asia",
    "pakistan": "south_asia", "india": "south_asia", "afghanistan": "south_asia",
    "united_kingdom": "western_europe", "france": "western_europe",
    "germany": "western_europe", "switzerland": "western_europe", "european_union": "western_europe",
    "venezuela": "south_america",
}


def region_of(actors: list[str]) -> str:
    """Majority region of the deciding actors (global institutions and the US
    are weighted last so 'us + iran' lands in middle_east, not a tie)."""
    from collections import Counter

    weighted = [ACTOR_REGIONS[a] for a in actors if a in ACTOR_REGIONS and a not in ("united_states", "united_nations", "nato")]
    if not weighted:
        weighted = [ACTOR_REGIONS[a] for a in actors if a in ACTOR_REGIONS]
    if not weighted:
        return "global"
    return Counter(weighted).most_common(1)[0][0]


def detect_actors(text: str) -> list[str]:
    lowered = text.lower()
    found = [actor for actor, (aliases, _domains) in ACTORS.items() if any(alias in lowered for alias in aliases)]
    return sorted(found)


def detect_event_families(text: str) -> list[str]:
    lowered = text.lower()
    return sorted(family for family, terms in EVENT_FAMILIES.items() if any(term in lowered for term in terms))


def official_domains(actors: list[str]) -> list[str]:
    domains: list[str] = []
    for actor in actors:
        entry = ACTORS.get(actor)
        if entry:
            domains.extend(entry[1])
    return sorted(set(domains))


def google_news_rss(query: str) -> str:
    from urllib.parse import quote_plus

    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"


def bing_news_rss(query: str) -> str:
    from urllib.parse import quote_plus

    # Second aggregator on separate (Microsoft) infrastructure. A degraded
    # route to Google must not blind discovery: an ISP peering fault toward
    # Google timed out every news.google.com feed for days while the rest of
    # the internet stayed reachable, leaving Al Jazeera as the only live feed.
    return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
