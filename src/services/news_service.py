import httpx
import xml.etree.ElementTree as ET
from urllib.parse import quote

async def get_team_news_modifier(team_name: str) -> float:
    """
    Busca en los RSS de Google News noticias recientes sobre lesiones o bajas del equipo.
    Devuelve un multiplicador para ajustar el promedio de goles (Ej: -0.15 indica baja de rendimiento).
    """
    query = quote(f'"{team_name}" lesionado OR baja OR suspendido OR sancionado')
    rss_url = f"https://news.google.com/rss/search?q={query}&hl=es-419&gl=UY&ceid=UY:es-419"
    
    modifier = 0.0
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(rss_url, timeout=5.0)
            if response.status_code == 200:
                root = ET.fromstring(response.text)
                items = root.findall(".//item")
                
                # Si encuentra 2 o más noticias recientes sobre bajas/lesiones
                if len(items) >= 2:
                    modifier = -0.15  # Penalización del 15% en fuerza ofensiva/defensiva
                    print(f"📰 Bajas o noticias críticas detectadas para {team_name} ({len(items)} noticias). Ajuste: -15%")
                else:
                    print(f"📰 Plantilla sin novedades críticas de bajas para {team_name}.")
        except Exception as e:
            print(f"⚠️ No se pudo consultar noticias RSS para {team_name}: {e}")
            
    return modifier