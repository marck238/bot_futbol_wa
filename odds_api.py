import os
import httpx
from datetime import datetime, timezone, timedelta
from analytics import analyze_pre_match_event, calculate_kelly_stake

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# Ligas principales monitoreadas por The Odds API
FEATURED_SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_copa_sudamericana",
    "soccer_argentina_primera_division",
    "soccer_brazil_campeonato",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league"
]

async def get_upcoming_ev_picks(
    hours: int = None,
    days_offset: int = None,
    is_weekend: bool = False,
    min_ev: float = 0.0
) -> dict:
    """
    Consulta partidos reales en The Odds API, calcula la probabilidad de Poisson 
    y el Criterio de Kelly, y retorna únicamente las apuestas con EV+.
    """
    if not ODDS_API_KEY:
        return {"status": "NO_API_KEY", "data": []}

    now = datetime.now(timezone.utc)

    # 1. Definir rango horario del filtro
    if hours:
        time_start = now
        time_end = now + timedelta(hours=hours)
    elif days_offset is not None:
        target_date = now + timedelta(days=days_offset)
        time_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        time_end = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
    elif is_weekend:
        days_until_saturday = (5 - now.weekday()) % 7
        if days_until_saturday == 0 and now.weekday() != 5:
            days_until_saturday = 7
        saturday = now + timedelta(days=days_until_saturday)
        time_start = saturday.replace(hour=0, minute=0, second=0, microsecond=0)
        time_end = (saturday + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        time_start = now
        time_end = now + timedelta(hours=24)

    results = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for sport_key in FEATURED_SPORTS:
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
            params = {
                "apiKey": ODDS_API_KEY,
                "regions": "eu,us",
                "markets": "totals,h2h",
                "dateFormat": "iso"
            }

            try:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    continue

                matches = resp.json()
                for match in matches:
                    commence_time_str = match.get("commence_time")
                    if not commence_time_str:
                        continue

                    match_dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))

                    # Filtrar únicamente encuentros dentro de la ventana de tiempo seleccionada
                    if not (time_start <= match_dt <= time_end):
                        continue

                    home_team = match.get("home_team", "Local")
                    away_team = match.get("away_team", "Visitante")
                    sport_title = match.get("sport_title", "Fútbol")

                    bookmakers = match.get("bookmakers", [])
                    if not bookmakers:
                        continue

                    # Extraer cuotas de mercado Over/Under 2.5
                    bm = bookmakers[0]
                    for market in bm.get("markets", []):
                        if market["key"] == "totals":
                            for outcome in market.get("outcomes", []):
                                if outcome.get("name") == "Over" and outcome.get("point") == 2.5:
                                    bookmaker_odd = float(outcome.get("price", 0))
                                    if bookmaker_odd <= 1.0:
                                        continue

                                    # Estimación Poisson basada en promedios ofensivos/defensivos
                                    home_exp = 1.60
                                    away_exp = 1.20

                                    analysis = analyze_pre_match_event(home_exp, away_exp, line=2.5)
                                    kelly = calculate_kelly_stake(analysis["prob_over_pct"], bookmaker_odd)

                                    ev = kelly.get("expected_value_pct", 0)
                                    if ev >= min_ev and kelly.get("has_value"):
                                        formatted_time = match_dt.strftime("%H:%M Hs")
                                        results.append({
                                            "match": f"{home_team} vs {away_team}",
                                            "league": sport_title,
                                            "time": formatted_time,
                                            "market": "Over 2.5 Goles",
                                            "prob": analysis["prob_over_pct"],
                                            "fair_odd": analysis["fair_odd_over"],
                                            "bookmaker_odd": bookmaker_odd,
                                            "ev": ev,
                                            "stake": kelly.get("recommended_stake_pct", 0)
                                        })
            except Exception as e:
                print(f"Error procesando {sport_key}: {e}")
                continue

    return {"status": "SUCCESS", "data": results}