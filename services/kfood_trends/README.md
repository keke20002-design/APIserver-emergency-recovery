# K-Food Trends

Independent Korean Pantry module for weekly, human-reviewed trend content.

## One-time setup

1. Run `migration.sql` in the Supabase SQL Editor.
2. Configure the variables documented in `.env.example`.
3. Restart the FastAPI process. APScheduler runs the job every Monday at 09:00 Asia/Seoul.

The server reuses the existing Naver Search, Gemini (`gemini-2.5-flash`), Supabase,
logging, scheduler, and `BLOG_ADMIN_ID` / `BLOG_ADMIN_PW` authentication setup.

## Routes

- `GET /api/kfood-trends?period=weekly|monthly|all`
- `GET /api/kfood-trends/{id}`
- `GET /admin/kfood-trends` (review UI)
- `POST /admin/kfood-trends/collect?admin_id=...&admin_pw=...`

Collection uses eight Naver searches by default. The pipe-delimited
`KFOOD_TREND_SEARCH_QUERIES` setting overrides them. Each run stores Naver request
count, Gemini request count, and Gemini prompt/output/total token usage in
`kfood_trend_runs.metrics`.
