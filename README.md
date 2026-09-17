
# Sport Analyzer V13.2

Telegram sports-analysis bot with a unified dark navy / cyan mobile dashboard.

## V13.2
- Shared Pillow renderer for all graphical bot screens
- Dashboard, menus, match list, selected match, probabilities, goals, simulator, bankroll, performance, sources and help use the same visual system
- 1024x1536 portrait dashboard
- Horizontal probability bars and rounded cards
- Telegram inline keyboards remain functional
- Football-Data.org primary source
- TheSportsDB fallback
- SQLite prediction and bankroll tracking
- API/cache optimizations from V13.1
- No `BufferedInputFile`

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`python bot.py`

Required environment variables:
- `TELEGRAM_TOKEN`
- `FOOTBALL_DATA_KEY`

`RENDER_EXTERNAL_URL` is supplied automatically by Render.
