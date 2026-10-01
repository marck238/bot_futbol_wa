import httpx
from bs4 import BeautifulSoup
import logging

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

async def get_supermatch_uruguay_odds():
    """
    Extrae gratuitamente los partidos y cuotas del fútbol uruguayo 
    directamente desde la web de Supermatch.
    """
    url = "https://www.supermatch.com.uy/"
    matches = []

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers=HEADERS)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Estructura genérica para parsear los eventos de Uruguay
                # (Supermatch organiza los bloques por liga/deporte)
                events = soup.find_all("div", class_="event-item")  # Ajuste según estructura HTML
                
                for event in events:
                    text = event.get_text()
                    if "Uruguay" in text or "Primera Division" in text or "Segunda" in text:
                        # Extraer equipos y cuotas
                        teams = event.find_all("span", class_="team-name")
                        odds = event.find_all("span", class_="odd-value")
                        
                        if len(teams) >= 2 and len(odds) >= 3:
                            matches.append({
                                "league": "Uruguay Local",
                                "home": teams[0].get_text(strip=True),
                                "away": teams[1].get_text(strip=True),
                                "odds": {
                                    "1": odds[0].get_text(strip=True),
                                    "X": odds[1].get_text(strip=True),
                                    "2": odds[2].get_text(strip=True)
                                }
                            })
        except Exception as e:
            logging.error(f"Error scraping Supermatch: {e}")

    return matches