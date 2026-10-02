import os
import math
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
LOCAL_TZ = timezone(timedelta(hours=-3)) # Uruguay / Argentina (UTC-3)

DEFAULT_SOCCER_LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_argentina_primera_division",
    "soccer_brazil_campeonato"
]

def poisson_pmf(k: int, lamb: float) -> float:
    if lamb <= 0:
        return 1.0 if k == 0 else 0.0
    return (math.pow(lamb, k) * math.exp(-lamb)) / math.factorial(k)

def calculate_poisson_probabilities(home_exp: float, away_exp: float):
    scores = {}
    prob_home = 0.0
    prob_draw = 0.0
    prob_away = 0.0

    for h in range(8):
        p_h = poisson_pmf(h, home_exp)
        for a in range(8):
            p_a = poisson_pmf(a, away_exp)
            p_cell = p_h * p_a
            scores[(h, a)] = p_cell

            if h > a:
                prob_home += p_cell
            elif h == a:
                prob_draw += p_cell
            else:
                prob_away += p_cell

    prob_over_1_5 = sum(p for (h, a), p in scores.items() if (h + a) > 1.5)
    prob_over_2_5 = sum(p for (h, a), p in scores.items() if (h + a) > 2.5)
    prob_under_2_5 = 1.0 - prob_over_2_5
    prob_over_3_5 = sum(p for (h, a), p in scores.items() if (h + a) > 3.5)

    return {
        "Victoria Local": round(prob_home * 100, 2),
        "Empate": round(prob_draw * 100, 2),
        "Victoria Visitante": round(prob_away * 100, 2),
        "Over 1.5 Goles": round(prob_over_1_5 * 100, 2),
        "Over 2.5 Goles": round(prob_over_2_5 * 100, 2),
        "Under 2.5 Goles": round(prob_under_2_5 * 100, 2),
        "Over 3.5 Goles": round(prob_over_3_5 * 100, 2)
    }

def calculate_ev(odds: float, win_probability_pct: float) -> float:
    p = win_probability_pct / 100.0
    if odds <= 1.0 or p <= 0:
        return -100.0
    ev = (p * (odds - 1.0) - (1.0 - p)) * 100.0
    return round(ev, 2)

def calculate_kelly_stake(odds: float, win_probability_pct: float, bankroll: float = 1000.0, fraction: float = 0.25) -> dict:
    p = win_probability_pct / 100.0
    b = odds - 1.0
    if b <= 0 or p <= 0:
        return {"stake_pct": 0.0, "amount": 0.0}
    q = 1.0 - p
    f_star = (b * p - q) / b
    if f_star <= 0:
        return {"stake_pct": 0.0, "amount": 0.0}
    
    f_adjusted = f_star * fraction
    stake_pct = min(f_adjusted * 100.0, 5.0)
    amount = round((stake_pct / 100.0) * bankroll, 2)
    return {"stake_pct": round(stake_pct, 2), "amount": amount}

async def fetch_active_soccer_sports(client: httpx.AsyncClient, api_key: str) -> tuple[list[str], str | None]:
    """Obtiene ligas de fútbol activas desde /v4/sports (0 créditos)."""
    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}"
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code == 401:
            return [], "INVALID_KEY"
        if resp.status_code == 429:
            return [], "QUOTA_EXCEEDED"
        if resp.status_code != 200:
            return DEFAULT_SOCCER_LEAGUES, None
        
        sports = resp.json()
        active_soccer = [
            s["key"] for s in sports 
            if s.get("active") is True and (s.get("group") == "Soccer" or s.get("key", "").startswith("soccer_"))
        ]
        return active_soccer if active_soccer else DEFAULT_SOCCER_LEAGUES, None
    except Exception as e:
        logger.error(f"Error al obtener ligas activas: {e}")
        return DEFAULT_SOCCER_LEAGUES, None

async def fetch_odds_for_sport(client: httpx.AsyncClient, sport_key: str, api_key: str) -> tuple[list[dict], str | None]:
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": api_key,
        "regions": "eu,us,uk,au",
        "markets": "h2h,totals",
        "dateFormat": "iso",
        "oddsFormat": "decimal"
    }
    try:
        resp = await client.get(url, params=params, timeout=10.0)
        if resp.status_code == 401:
            return [], "INVALID_KEY"
        if resp.status_code == 429:
            return [], "QUOTA_EXCEEDED"
        if resp.status_code == 200:
            return resp.json(), None
        return [], None
    except Exception as e:
        logger.error(f"Error al obtener cuotas de {sport_key}: {e}")
        return [], None

async def get_upcoming_ev_picks(
    hours: int | None = None,
    days_offset: int | None = None,
    is_weekend: bool = False,
    min_ev_filter: float = 0.0
) -> dict:
    if not ODDS_API_KEY:
        return {"status": "NO_API_KEY", "message": "No se encontró ODDS_API_KEY en las variables de entorno."}

    now = datetime.now(timezone.utc)
    
    # Configuración de rangos de tiempo
    if hours:
        time_start = now - timedelta(minutes=15)
        time_end = now + timedelta(hours=hours)
    elif days_offset is not None:
        now_local = now.astimezone(LOCAL_TZ)
        target_local = now_local + timedelta(days=days_offset)
        time_start_local = target_local.replace(hour=0, minute=0, second=0, microsecond=0)
        time_end_local = target_local.replace(hour=23, minute=59, second=59, microsecond=0)
        time_start = time_start_local.astimezone(timezone.utc)
        time_end = time_end_local.astimezone(timezone.utc)
        if days_offset == 0:
            time_start = max(time_start, now - timedelta(minutes=15))
    elif is_weekend:
        now_local = now.astimezone(LOCAL_TZ)
        days_until_sat = (5 - now_local.weekday()) % 7
        sat_local = (now_local + timedelta(days=days_until_sat)).replace(hour=0, minute=0, second=0, microsecond=0)
        sun_local = (sat_local + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
        time_start = sat_local.astimezone(timezone.utc)
        time_end = sun_local.astimezone(timezone.utc)
    else:
        time_start = now - timedelta(minutes=15)
        time_end = now + timedelta(hours=24)

    all_matches = []
    error_status = None

    async with httpx.AsyncClient() as client:
        active_leagues, err = await fetch_active_soccer_sports(client, ODDS_API_KEY)
        if err:
            return {"status": err, "data": []}

        leagues_to_query = active_leagues[:12]

        tasks = [fetch_odds_for_sport(client, league, ODDS_API_KEY) for league in leagues_to_query]
        results = await asyncio.gather(*tasks)

        for matches, err in results:
            if err in ["INVALID_KEY", "QUOTA_EXCEEDED"]:
                error_status = err
                break
            all_matches.extend(matches)

    if error_status:
        return {"status": error_status, "data": []}

    processed_picks = []

    for match in all_matches:
        commence_time_str = match.get("commence_time")
        if not commence_time_str:
            continue
        try:
            match_dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
        except Exception:
            continue

        if not (time_start <= match_dt <= time_end):
            continue

        home_team = match.get("home_team", "Local")
        away_team = match.get("away_team", "Visitante")
        sport_title = match.get("sport_title", "Fútbol")
        bookmakers = match.get("bookmakers", [])

        if not bookmakers:
            continue

        h2h_odds = {}
        over25_odds = None
        under25_odds = None
        bookmaker_title = "Mercado"

        for bm in bookmakers:
            bookmaker_title = bm.get("title", bookmaker_title)
            for m in bm.get("markets", []):
                if m.get("key") == "h2h" and not h2h_odds:
                    for outcome in m.get("outcomes", []):
                        h2h_odds[outcome["name"]] = outcome["price"]
                elif m.get("key") == "totals" and over25_odds is None:
                    for outcome in m.get("outcomes", []):
                        if outcome.get("point") == 2.5:
                            if outcome.get("name") == "Over":
                                over25_odds = outcome.get("price")
                            elif outcome.get("name") == "Under":
                                under25_odds = outcome.get("price")

        home_odd = h2h_odds.get(home_team)
        away_odd = h2h_odds.get(away_team)
        draw_odd = h2h_odds.get("Draw")

        if home_odd and away_odd:
            home_implied = 1.0 / home_odd
            away_implied = 1.0 / away_odd
            total_implied = home_implied + away_implied
            home_exp = max(0.6, round((home_implied / total_implied) * 2.7, 2))
            away_exp = max(0.5, round((away_implied / total_implied) * 2.3, 2))
        else:
            home_exp = 1.50
            away_exp = 1.10

        probs = calculate_poisson_probabilities(home_exp, away_exp)

        candidates = []
        if home_odd:
            ev = calculate_ev(home_odd, probs["Victoria Local"])
            candidates.append({"market": f"Gana {home_team}", "odd": home_odd, "prob": probs["Victoria Local"], "ev": ev})

        if away_odd:
            ev = calculate_ev(away_odd, probs["Victoria Visitante"])
            candidates.append({"market": f"Gana {away_team}", "odd": away_odd, "prob": probs["Victoria Visitante"], "ev": ev})

        if draw_odd:
            ev = calculate_ev(draw_odd, probs["Empate"])
            candidates.append({"market": "Empate", "odd": draw_odd, "prob": probs["Empate"], "ev": ev})

        if over25_odds:
            ev = calculate_ev(over25_odds, probs["Over 2.5 Goles"])
            candidates.append({"market": "Over 2.5 Goles", "odd": over25_odds, "prob": probs["Over 2.5 Goles"], "ev": ev})

        if under25_odds:
            ev = calculate_ev(under25_odds, probs["Under 2.5 Goles"])
            candidates.append({"market": "Under 2.5 Goles", "odd": under25_odds, "prob": probs["Under 2.5 Goles"], "ev": ev})

        if not candidates:
            continue

        best_pick = max(candidates, key=lambda x: x["ev"])

        if min_ev_filter > 0 and best_pick["ev"] < min_ev_filter:
            continue

        stake_info = calculate_kelly_stake(best_pick["odd"], best_pick["prob"])
        match_time_local = match_dt.astimezone(LOCAL_TZ).strftime("%H:%M hs (%d/%m)")

        processed_picks.append({
            "match": f"{home_team} vs {away_team}",
            "home_team": home_team,
            "away_team": away_team,
            "league": sport_title,
            "time": match_time_local,
            "best_pick": best_pick["market"],
            "odd": best_pick["odd"],
            "prob": best_pick["prob"],
            "ev": best_pick["ev"],
            "stake_pct": stake_info["stake_pct"],
            "stake_amount": stake_info["amount"],
            "bookmaker": bookmaker_title,
            "poisson": probs
        })

    processed_picks.sort(key=lambda x: x["ev"], reverse=True)
    return {"status": "SUCCESS", "data": processed_picks}