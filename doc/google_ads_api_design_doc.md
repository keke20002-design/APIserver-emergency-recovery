# App Monetization Dashboard — Google Ads API Design Document

## 1. Overview

We are an indie mobile app developer/publisher operating several Android
apps monetized through Google AdMob, with user acquisition run through
Google Ads. This document describes an internal tool, the **App
Monetization Dashboard**, built to combine our own Google Ads spend data
with AdMob revenue data and Firebase/GA4 analytics into a single
reporting view.

The goal is to answer, per app / country / campaign:

- Are we spending more on Google Ads than we earn back through AdMob?
- Which campaigns should we scale, optimize, or stop?
- What CPI can we afford given our measured user LTV?

## 2. Architecture

```
Next.js Dashboard (internal, read-only UI)
        |
        v
Python/FastAPI backend (APIserver)
        |
        +--> Google Ads API   (campaign performance: impressions, clicks,
        |                      cost, conversions, app installs, by country)
        +--> AdMob API        (ad unit revenue, impressions, eCPM)
        +--> GA4 Data API     (retention, active users)
        |
        v
PostgreSQL (Supabase) - daily aggregated tables, read by the dashboard
```

A backend job calls the Google Ads API on a schedule (daily, re-syncing
the last 7 days to absorb late-arriving data corrections), stores the
results in our own database, and the dashboard reads from that database.
The dashboard never calls the Google Ads API directly from the browser.

## 3. Google Ads API usage

- **Access pattern**: read-only reporting via `GoogleAdsService.SearchStream`
  using Google Ads Query Language (GAQL).
- **Resources queried**: `geographic_view` (campaign performance segmented
  by date and country) and `geo_target_constant` (to resolve country
  criterion IDs to ISO country codes).
- **Fields used**: `segments.date`, `campaign.id`, `campaign.name`,
  `geographic_view.country_criterion_id`, `customer.currency_code`,
  `metrics.impressions`, `metrics.clicks`, `metrics.cost_micros`,
  `metrics.conversions`, `metrics.biddable_app_install_conversions`.
- **We do not** create, modify, or delete campaigns, ad groups, ads, or
  bids through the API. No write operations are performed.
- **We do not** use App Conversion Tracking or Remarketing APIs.

## 4. Authentication

- OAuth 2.0 "Desktop app" client, with a refresh token obtained once via
  an interactive consent flow run by the account owner.
- The Google Ads manager (MCC) account ID is supplied as
  `login-customer-id` to access the linked client account.
- Credentials (developer token, OAuth client id/secret, refresh token)
  are stored only in server-side environment variables, never in client
  code or the dashboard frontend.

## 5. Users

- **Internal only.** The dashboard is used exclusively by the business
  owner to make their own advertising budget decisions. It is not
  distributed to external clients or the general public, and no other
  party has access to the underlying Google Ads data through this tool.

## 6. Data handling

- Data pulled from the Google Ads API is stored in our own
  Supabase/PostgreSQL database, scoped to our own advertiser account(s)
  only.
- No data is shared with, or resold to, third parties.
