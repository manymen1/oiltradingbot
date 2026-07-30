#!/usr/bin/env bash
# Probe candidate news feeds for reachability, item count, and FRESHNESS.
#
# Latency is the confirmed-entry strategy's dominant cost, and these markets
# resolve on "a consensus of credible reporting" -- not on government press
# releases. So the feeds that matter are credible outlets publishing on their
# OWN infrastructure (minutes) rather than via aggregator indexing (5-15 min+).
#
# A feed is only worth wiring in if it is live AND fresh: an HTTP 200 proves
# nothing (two feeds in this repo returned 200 with zero items for months).
#
# Usage: bash scripts/probe_feeds.sh [path-to-url-list]
set -u

FEEDS=(
  # --- wires / agencies ---
  "https://www.aa.com.tr/en/rss/default?cat=middle-east"
  "https://apnews.com/index.rss"
  "https://www.reuters.com/world/middle-east/rss"
  # --- major broadsheets (tier-one "credible reporting") ---
  "https://rss.nytimes.com/services/xml/rss/nyt/MiddleEast.xml"
  "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"
  "https://feeds.washingtonpost.com/rss/world"
  "https://www.theguardian.com/world/middleeast/rss"
  # --- fast broadcasters ---
  "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"
  "https://www.aljazeera.com/xml/rss/all.xml"
  "https://feeds.skynews.com/feeds/rss/world.xml"
  "https://www.france24.com/en/middle-east/rss"
  "https://rss.dw.com/rdf/rss-en-world"
  "https://feeds.npr.org/1004/rss.xml"
  "https://www.cbsnews.com/latest/rss/world"
  # --- US politics / policy breaking ---
  "https://api.axios.com/feed/"
  "https://thehill.com/news/feed/"
  "https://rss.politico.com/politics-news.xml"
  # --- regional specialists (often first on Iran/Gulf) ---
  "https://www.timesofisrael.com/feed/"
  "https://www.jpost.com/rss/rssfeedsmiddleeastnews.aspx"
  "https://www.middleeasteye.net/rss"
  "https://english.alarabiya.net/.mrss/en/middle-east.xml"
  # --- official (slower, but authoritative) ---
  "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20"
  "https://www.whitehouse.gov/presidential-actions/feed/"
)

if [ "$#" -ge 1 ] && [ -f "$1" ]; then
  mapfile -t FEEDS < "$1"
fi

now=$(date +%s)
for u in "${FEEDS[@]}"; do
  u="${u%%#*}"
  u="$(printf '%s' "$u" | tr -d '[:space:]')"
  [ -z "$u" ] && continue
  code=$(curl -s -o /tmp/probe.xml -w '%{http_code}' -L --max-time 12 \
    -A 'Mozilla/5.0 (compatible; polybot/1.0)' "$u" 2>/dev/null)
  items=$(grep -c '<item\|<entry' /tmp/probe.xml 2>/dev/null || echo 0)
  raw=$(grep -m1 -o '<pubDate>[^<]*\|<updated>[^<]*\|<published>[^<]*' /tmp/probe.xml 2>/dev/null \
    | head -1 | sed 's/<[a-zA-Z]*>//')
  age="-"
  if [ -n "$raw" ]; then
    ts=$(date -d "$raw" +%s 2>/dev/null || echo "")
    [ -n "$ts" ] && age=$(( (now - ts) / 60 ))
  fi
  status="dead"
  [ "$code" = "200" ] && [ "${items:-0}" -gt 0 ] && status="LIVE"
  printf '%-5s %-4s items=%-4s age=%6s min  %s\n' "$status" "$code" "$items" "$age" "$u"
done
