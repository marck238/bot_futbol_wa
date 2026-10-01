import os
import math
import logging
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

# Importar el scraper del fútbol uruguayo y el calculador dinámico
from src.services.uruguay_scraper import get_supermatch_uruguay_odds
from src.services.poisson_calculator import calculate_dynamic_mus

# Configuración de Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Variables de Entorno
load_dotenv()
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Ligas y Torneos Monitoreados Globalmente en The Odds API
LEAGUES = [
    # --- 🇪🇺 EUROPA OCCIDENTAL Y PRINCIPALES ---
    "soccer_epl",                        # Inglaterra - Premier League
    "soccer_efl_champ",                  # Inglaterra - Championship
    "soccer_fa_cup",                     # Inglaterra - FA Cup
    "soccer_england_league1",            # Inglaterra - League 1
    "soccer_england_league2",            # Inglaterra - League 2
    "soccer_spain_la_liga",              # España - LaLiga
    "soccer_spain_segunda_division",     # España - LaLiga 2
    "soccer_spain_copa_del_rey",         # España - Copa del Rey
    "soccer_italy_serie_a",              # Italia - Serie A
    "soccer_italy_serie_b",              # Italia - Serie B
    "soccer_germany_bundesliga",         # Alemania - Bundesliga
    "soccer_germany_bundesliga2",        # Alemania - 2. Bundesliga
    "soccer_france_ligue_one",           # Francia - Ligue 1
    "soccer_france_ligue_two",           # Francia - Ligue 2
    "soccer_netherlands_eredivisie",     # Países Bajos - Eredivisie
    "soccer_portugal_primeira_liga",     # Portugal - Primeira Liga
    "soccer_belgium_first_div",          # Bélgica - Pro League
    "soccer_turkey_super_league",        # Turquía - Süper Lig

    # --- ❄️ EUROPA NÓRDICA, CENTRAL Y OTRAS (ALTA ESTABILIDAD DE GOLES) ---
    "soccer_australia_aleague",          # Australia - A-League
    "soccer_austria_bundesliga",         # Austria - Bundesliga
    "soccer_denmark_superliga",          # Dinamarca - Superliga
    "soccer_norway_eliteserien",         # Noruega - Eliteserien
    "soccer_sweden_allsvenskan",         # Suecia - Allsvenskan
    "soccer_switzerland_superleague",    # Suiza - Super League
    "soccer_spl",                        # Escocia - Premiership
    "soccer_greece_super_league",        # Grecia - Super League
    "soccer_poland_ekstraklasa",         # Polonia - Ekstraklasa

    # --- 🌎 SUDAMÉRICA Y URUGUAY ---
    "soccer_argentina_primera_division", # Argentina - Liga Profesional
    "soccer_brazil_campeonato",          # Brasil - Brasileirão Série A
    "soccer_brazil_serie_b",              # Brasil - Brasileirão Série B
    "soccer_chile_camp_nacional",        # Chile - Primera División
    "soccer_colombia_primera_a",         # Colombia - Primera A
    "soccer_uruguay_primera_division",   # Uruguay - Primera División
    "soccer_peru_campeonato",            # Perú - Liga 1
    "soccer_ecuador_serie_a",            # Ecuador - Serie A
    "soccer_conmebol_copa_libertadores", # CONMEBOL - Copa Libertadores
    "soccer_conmebol_copa_sudamericana", # CONMEBOL - Copa Sudamericana

    # --- 🇲🇽 🇺🇸 NORTE Y CENTROAMÉRICA ---
    "soccer_usa_mls",                    # EE.UU. / Canadá - MLS
    "soccer_mexico_ligamx",              # México - Liga MX

    # --- 🇸🇦 🇯🇵 ASIA Y ÁFRICA ---
    "soccer_japan_j_league",             # Japón - J1 League
    "soccer_saudi_arabia_pro_league",    # Arabia Saudita - Saudi Pro League
    "soccer_africa_cup_of_nations",      # África - Copa Africana de Naciones

    # --- 🏆 COPAS INTERNACIONALES Y SELECCIONES ---
    "soccer_uefa_champions_league",      # UEFA Champions League
    "soccer_uefa_europa_league",         # UEFA Europa League
    "soccer_uefa_nations_league",        # UEFA Nations League
    "soccer_fifa_world_cup",             # Copa Mundial de la FIFA
    "soccer_fifa_world_cup_qualifiers",  # Clasificatorias al Mundial
    "soccer_international_friendly"      # Amistosos Internacionales / FIFA
]

# --- FUNCIONES DE CÁLCULO MATEMÁTICO ---
def poisson_pmf(k: int, mu: float) -> float:
    """Distribución de Poisson para k goles con media mu."""
    return (math.pow(mu, k) * math.exp(-mu)) / math.factorial(k)

def calculate_over_2_5_prob(mu_home: float, mu_away: float) -> float:
    """Calcula la probabilidad de Over 2.5 goles usando distribución de Poisson."""
    prob_under = 0.0
    under_scores = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2)]
    for h, a in under_scores:
        prob_under += poisson_pmf(h, mu_home) * poisson_pmf(a, mu_away)
    return 1.0 - prob_under

def calculate_kelly_stake(prob: float, odds: float, bankroll_fraction: float = 0.25) -> tuple[float, float]:
    """Calcula el Valor Esperado (EV%) y el Kelly Stake fraccionado."""
    b = odds - 1.0
    if b <= 0:
        return 0.0, 0.0
    
    ev = (prob * odds) - 1.0
    ev_pct = round(ev * 100, 2)
    
    if ev <= 0:
        return ev_pct, 0.0

    f_star = (b * prob - (1.0 - prob)) / b
    kelly = max(0.0, f_star * bankroll_fraction)
    kelly_pct = round(kelly * 100, 2)
    
    return ev_pct, kelly_pct

# --- CONECTOR BASE DE DATOS NEON POSTGRESQL ---
def get_engine():
    db_url = DATABASE_URL or "postgresql+asyncpg://user:pass@localhost/dbname"
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(db_url, echo=False)

# --- CICLO PRINCIPAL DE ESCANEO ---
async def fetch_odds_and_predict(db=None):
    logging.info("🔄 Iniciando escaneo automático de partidos, noticias y cuotas...")
    all_matches = []

    # 1. ESCANEO LOCAL GRATUITO (URUGUAY VIA SCRAPER SUPERMATCH)
    try:
        logging.info("🇺🇾 Consultando partidos y cuotas locales de Uruguay (Supermatch)...")
        local_uruguay_matches = await get_supermatch_uruguay_odds()
        for match in local_uruguay_matches:
            match_dt = datetime.now(timezone.utc) + timedelta(days=2)
            
            raw_odd = match["odds"].get("Over 2.5", match["odds"].get("1", "1.90"))
            try:
                odd_val = float(str(raw_odd).replace(",", "."))
            except ValueError:
                odd_val = 1.90

            all_matches.append({
                "source": "Supermatch Uruguay",
                "league": match.get("league", "Uruguay Local"),
                "home": match["home"],
                "away": match["away"],
                "datetime": match_dt,
                "market": "Over 2.5 Goles",
                "odds": odd_val,
                "home_stats": match.get("home_stats", {"gf_home_avg": 1.60, "gc_home_avg": 0.90}),
                "away_stats": match.get("away_stats", {"gf_away_avg": 1.20, "gc_away_avg": 1.40}),
                "league_stats": match.get("league_stats", {"league_home_avg": 1.45, "league_away_avg": 1.15})
            })
        logging.info(f"✅ Se obtuvieron {len(local_uruguay_matches)} encuentros de Uruguay.")
    except Exception as e:
        logging.error(f"⚠️ Error al obtener partidos uruguayos vía scraper: {e}")

    # 2. ESCANEO GLOBAL VIA THE ODDS API
    if ODDS_API_KEY:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for league in LEAGUES:
                try:
                    url = f"https://api.the-odds-api.com/v4/sports/{league}/odds"
                    params = {
                        "apiKey": ODDS_API_KEY,
                        "regions": "eu,us",
                        "markets": "totals,h2h",
                        "oddsFormat": "decimal"
                    }
                    resp = await client.get(url, params=params)
                    if resp.status_code != 200:
                        continue
                    
                    data = resp.json()
                    for event in data:
                        home_team = event.get("home_team")
                        away_team = event.get("away_team")
                        commence_time_str = event.get("commence_time")
                        
                        if not commence_time_str:
                            continue

                        dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))

                        best_over_odds = None
                        for bookmaker in event.get("bookmakers", []):
                            for market in bookmaker.get("markets", []):
                                if market.get("key") == "totals":
                                    for outcome in market.get("outcomes", []):
                                        if outcome.get("name") == "Over" and outcome.get("point") == 2.5:
                                            odd_val = float(outcome.get("price", 0))
                                            if best_over_odds is None or odd_val > best_over_odds:
                                                best_over_odds = odd_val
                        
                        if best_over_odds and best_over_odds > 1.20:
                            all_matches.append({
                                "source": "The Odds API",
                                "league": league,
                                "home": home_team,
                                "away": away_team,
                                "datetime": dt,
                                "market": "Over 2.5 Goles",
                                "odds": best_over_odds,
                                "home_stats": {"gf_home_avg": 1.60, "gc_home_avg": 0.90},
                                "away_stats": {"gf_away_avg": 1.20, "gc_away_avg": 1.40},
                                "league_stats": {"league_home_avg": 1.45, "league_away_avg": 1.15}
                            })
                except Exception as e:
                    logging.error(f"Error escaneando liga {league}: {e}")
    else:
        logging.warning("⚠️ ODDS_API_KEY no configurada en .env. Saltando consulta a The Odds API.")

    # 3. CÁLCULO DINÁMICO DE POISSON, EV+, KELLY Y PERSISTENCIA EN NEON POSTGRESQL
    engine = get_engine()
    inserted_count = 0

    try:
        async with engine.begin() as conn:
            for m in all_matches:
                home_stats = m.get("home_stats")
                away_stats = m.get("away_stats")
                league_stats = m.get("league_stats")

                # Cálculo dinámico de mu_home y mu_away
                mu_home, mu_away = calculate_dynamic_mus(home_stats, away_stats, league_stats)

                # Probabilidad Over 2.5 con Poisson
                prob_over_2_5 = calculate_over_2_5_prob(mu_home, mu_away)
                
                # EV% y Kelly Stake
                ev_pct, kelly_stake = calculate_kelly_stake(prob_over_2_5, m["odds"])

                # Filtrar solo oportunidades con EV+ >= 3%
                if ev_pct >= 3.0:
                    match_desc = f"{m['home']} vs {m['away']}"
                    
                    query = text("""
                        INSERT INTO bet_analysis (
                            match_description, recommended_market, odds, ev_percentage, kelly_stake, match_datetime
                        ) VALUES (
                            :desc, :market, :odds, :ev, :kelly, :dt
                        );
                    """)
                    
                    try:
                        await conn.execute(query, {
                            "desc": match_desc,
                            "market": m["market"],
                            "odds": m["odds"],
                            "ev": ev_pct,
                            "kelly": kelly_stake,
                            "dt": m["datetime"]
                        })
                        inserted_count += 1
                    except Exception as ex:
                        logging.error(f"Error al guardar partido {match_desc}: {ex}")
    finally:
        await engine.dispose()

    logging.info(f"🎉 Escaneo automático finalizado. {len(all_matches)} partidos analizados, {inserted_count} guardados con EV+ >= 3%.")

if __name__ == "__main__":
    asyncio.run(fetch_odds_and_predict())