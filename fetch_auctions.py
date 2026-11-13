"""
Daily eBay auction fetcher for sports cards.

Finds auctions ending soon that match the watchlist and writes:
  - auctions.json  (full data, for the analyst step)
  - auctions.md    (readable list)

Needs two environment variables (set as GitHub secrets):
  EBAY_CLIENT_ID      -> your eBay app's App ID (Client ID), Production keyset
  EBAY_CLIENT_SECRET  -> your eBay app's Cert ID (Client Secret), Production keyset

Uses only the Python standard library, nothing to install.
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# SETTINGS - edit these to change what gets fetched
# ---------------------------------------------------------------------------

# One search per entry. Use the spelling sellers use in titles.
SEARCH_TERMS = [
    "Messi",
    "Manchester United",
    "Stephen Curry",
    "LeBron James",
    "Luka Doncic",
    "Cooper Flagg",
]

MIN_PRICE = 50          # current bid must be at least this (USD)
MAX_PRICE = None        # e.g. 500 to cap it; None = no cap
HOURS_AHEAD = 24        # only auctions ending within this many hours
MAX_PER_TERM = 600      # safety cap on results per search term
ITEM_COUNTRY = "US"     # only items physically located in this country

# eBay category 261328 = Sports Trading Card Singles (excludes boxes, lots, supplies)
CATEGORY_IDS = "261328"

# Titles containing any of these words are dropped
EXCLUDE_WORDS = [
    "reprint", "rp", "custom", "digital", "lot of", "you pick", "u pick",
    "pick your", "facsimile", "proxy", "break", "mystery", "repack",
]

# Keep only cards that are numbered (/99, /25, 1/1 ...) OR autographed.
# Set either to False to turn that rule off; both False = keep everything.
KEEP_NUMBERED = True
KEEP_AUTOS = True
AUTO_WORDS = ["auto", "autos", "autograph", "autographed", "autographs",
              "signed", "signature", "signatures", "on card auto", "rpa"]

# Only keep cards from these brands (matched in the title).
# Bowman is a Topps brand; remove "bowman" to exclude it.
BRANDS = ["topps", "bowman", "upper deck", "ud", "prizm"]
# "Prizm" is also a parallel name in other Panini sets (Select, Optic, Mosaic...).
# If a title only matches "prizm", it is dropped when one of these appears.
OTHER_PANINI = ["select", "optic", "mosaic", "donruss", "chronicles", "obsidian",
                "hoops", "contenders", "certified", "crown royale", "revolution",
                "spectra", "recon", "flux", "court kings", "immaculate",
                "national treasures", "flawless", "noir", "one and one",
                "one & one", "photogenic", "silhouette", "threads", "kaleidoscope"]

# ---------------------------------------------------------------------------

API = "https://api.ebay.com"
SCOPE = "https://api.ebay.com/oauth/api_scope"


def get_token(client_id: str, client_secret: str) -> str:
    """Application token via client-credentials grant."""
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": SCOPE}
    ).encode()
    req = urllib.request.Request(
        f"{API}/identity/v1/oauth2/token",
        data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def build_filter(now: datetime) -> str:
    end = now + timedelta(hours=HOURS_AHEAD)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    price = f"[{MIN_PRICE}..{MAX_PRICE}]" if MAX_PRICE else f"[{MIN_PRICE}]"
    parts = [
        "buyingOptions:{AUCTION}",
        f"price:{price}",
        "priceCurrency:USD",
        f"itemLocationCountry:{ITEM_COUNTRY}",
        f"itemEndDate:[{now.strftime(fmt)}..{end.strftime(fmt)}]",
    ]
    return ",".join(parts)


def search(token: str, term: str, flt: str) -> list:
    items, offset, limit = [], 0, 200
    while offset < MAX_PER_TERM:
        params = urllib.parse.urlencode({
            "q": term,
            "category_ids": CATEGORY_IDS,
            "filter": flt,
            "sort": "endingSoonest",
            "limit": limit,
            "offset": offset,
        })
        req = urllib.request.Request(
            f"{API}/buy/browse/v1/item_summary/search?{params}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        batch = data.get("itemSummaries", [])
        items.extend(batch)
        if len(batch) < limit or offset + limit >= data.get("total", 0):
            break
        offset += limit
    return items


def excluded(title: str) -> bool:
    words = f" {title.lower()} "
    for w in EXCLUDE_WORDS:
        if f" {w} " in words or (" " in w and w in words):
            return True
    return False


SERIAL_RE = re.compile(r"(?<![\w/])(?:#\s?)?(\d{1,3})?\s?/\s?(\d{1,4})(?![\w/])")


def is_numbered(title: str) -> bool:
    for m in SERIAL_RE.finditer(title):
        num, den = m.group(1), int(m.group(2))
        if num and len(num) == 2 and len(m.group(2)) == 2 and int(num) + 1 == den:
            continue  # looks like a season, e.g. 24/25
        if den >= 1 and (num is None or int(num) <= den):
            return True
    return False


def is_auto(title: str) -> bool:
    words = " " + re.sub(r"[^a-z0-9]+", " ", title.lower()) + " "
    return any(f" {w} " in words for w in AUTO_WORDS)


def brand_ok(title: str) -> bool:
    words = " " + re.sub(r"[^a-z0-9&]+", " ", title.lower()) + " "
    hits = [b for b in BRANDS if f" {b} " in words]
    if not hits:
        return False
    if hits == ["prizm"] and any(f" {o} " in words for o in OTHER_PANINI):
        return False
    return True


def wanted(title: str) -> bool:
    if not brand_ok(title):
        return False
    if not (KEEP_NUMBERED or KEEP_AUTOS):
        return True
    return (KEEP_NUMBERED and is_numbered(title)) or (KEEP_AUTOS and is_auto(title))


def money(obj) -> float | None:
    try:
        return float(obj["value"])
    except (TypeError, KeyError, ValueError):
        return None


def simplify(it: dict, term: str) -> dict:
    seller = it.get("seller", {})
    bid = money(it.get("currentBidPrice")) or money(it.get("price"))
    return {
        "item_id": it.get("itemId"),
        "matched": term,
        "title": it.get("title"),
        "current_bid": bid,
        "bid_count": it.get("bidCount"),
        "ends_utc": it.get("itemEndDate"),
        "seller": seller.get("username"),
        "seller_feedback_pct": seller.get("feedbackPercentage"),
        "seller_feedback_score": seller.get("feedbackScore"),
        "numbered": is_numbered(it.get("title") or ""),
        "auto": is_auto(it.get("title") or ""),
        "condition": it.get("condition"),
        "shipping": money((it.get("shippingOptions") or [{}])[0].get("shippingCost")),
        "url": it.get("itemWebUrl"),
        "image": (it.get("image") or {}).get("imageUrl"),
    }


def write_markdown(rows: list, generated: str) -> None:
    lines = [
        f"# Auctions ending soon ({len(rows)} cards)",
        f"_Generated {generated} UTC. Min bid ${MIN_PRICE}, ending within {HOURS_AHEAD}h._",
        "",
        "| Ends (UTC) | Bid | Bids | Title | Seller |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        ends = (r["ends_utc"] or "")[:16].replace("T", " ")
        bid = f"${r['current_bid']:,.2f}" if r["current_bid"] else "?"
        title = (r["title"] or "").replace("|", "/")
        seller = f"{r['seller']} ({r['seller_feedback_pct']}%)"
        lines.append(f"| {ends} | {bid} | {r['bid_count']} | [{title}]({r['url']}) | {seller} |")
    with open("auctions.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    cid = os.environ.get("EBAY_CLIENT_ID")
    secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        print("Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    token = get_token(cid, secret)
    flt = build_filter(now)

    cutoff = (now + timedelta(hours=HOURS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%S")
    seen, rows = set(), []
    for term in SEARCH_TERMS:
        try:
            found = search(token, term, flt)
        except Exception as e:  # keep going if one search fails
            print(f"Search failed for {term!r}: {e}", file=sys.stderr)
            continue
        kept = 0
        for it in found:
            iid = it.get("itemId")
            title = it.get("title", "")
            ends = (it.get("itemEndDate") or "")[:19]
            if ends and ends > cutoff:
                continue  # double-check the end-time window ourselves
            if iid in seen or excluded(title) or not wanted(title):
                continue
            seen.add(iid)
            rows.append(simplify(it, term))
            kept += 1
        print(f"{term}: {len(found)} found, {kept} kept")

    rows.sort(key=lambda r: r["ends_utc"] or "")
    generated = now.strftime("%Y-%m-%d %H:%M")
    with open("auctions.json", "w", encoding="utf-8") as f:
        json.dump({"generated_utc": generated, "count": len(rows), "items": rows},
                  f, indent=1, ensure_ascii=False)
    write_markdown(rows, generated)
    print(f"Total: {len(rows)} auctions written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
