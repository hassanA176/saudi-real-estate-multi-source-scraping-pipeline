# Unified Real-Estate Database Schema

All collectors write to one SQLite database and use the `source` column to keep source records isolated.

## `listings`

```sql
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_listing_id TEXT,
    property_type TEXT,
    purpose TEXT,
    listing_url TEXT NOT NULL,
    title TEXT,
    price INTEGER,
    price_text TEXT,
    rent_period TEXT,
    area INTEGER,
    area_text TEXT,
    region TEXT,
    city TEXT,
    sub_region TEXT,
    district TEXT,
    street TEXT,
    building_number TEXT,
    postal_code TEXT,
    additional_number TEXT,
    location_link TEXT,
    phone TEXT,
    whatsapp_url TEXT,
    description TEXT,
    details_json TEXT,
    features_json TEXT,
    images_json TEXT,
    videos_json TEXT,
    advertiser_json TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    is_active INTEGER DEFAULT 1,
    UNIQUE(source, source_listing_id),
    UNIQUE(source, listing_url)
);
```

Source-specific details are stored in JSON text columns so a single schema can absorb different fields without discarding data.

## `scraper_state`

Tracks first collection and recurring-cycle timestamps per source.

```sql
CREATE TABLE IF NOT EXISTS scraper_state (
    source TEXT PRIMARY KEY,
    initial_collection_done INTEGER DEFAULT 0,
    last_cycle_started_at TEXT,
    last_cycle_finished_at TEXT
);
```

## Google Maps operational tables

The Maps collector adds `gm_`-prefixed tables for discovered links, query status, scroll diagnostics, and restart checkpoints. These are operational state tables and do not alter records belonging to other sources.

## Deduplication

Two source-scoped uniqueness rules prevent the same listing from being inserted repeatedly while allowing coincident IDs across unrelated websites.

For Google Maps, the Google place identifier becomes `source_listing_id`. Existing places are touched via `last_seen_at`; details are reopened only when new, failed, or due for a targeted retry.
