import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    VERIFY_TOKEN: str = os.getenv("VERIFY_TOKEN", "")
    WHATSAPP_TOKEN: str = os.getenv("WHATSAPP_TOKEN", "")
    PHONE_NUMBER_ID: str = os.getenv("PHONE_NUMBER_ID", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    @classmethod
    def validate(cls):
        """Verifica que no queden variables esenciales vacías."""
        required = ["VERIFY_TOKEN", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID"]
        missing = [var for var in required if not getattr(cls, var)]
        if missing:
            print(f"⚠️  ADVERTENCIA: Faltan definir las siguientes variables en el .env: {', '.join(missing)}")
        else:
            print("✅ Configuración cargada correctamente.")

config = Config()

if __name__ == "__main__":
    config.validate()
