"""
Datengreifer Backend
=====================
FastAPI-Backend, das drei Scraping-Wege kombiniert:
  1. requests          -> schnelles Laden von statischem HTML
  2. BeautifulSoup     -> Parsen/Extrahieren der Daten aus dem HTML
  3. Selenium          -> Rendern von JavaScript-lastigen Seiten (Login, Infinite Scroll etc.)

Zwei Produktlinien werden hier bedient:
  - /scrape/static   -> "Standard-Tool" (self-service, für einfache Seiten)
  - /scrape/dynamic  -> "Standard-Tool" für JS-Seiten (Selenium)
  - /orders          -> "Individuelle Aufträge" (Kunde beschreibt Bedarf, landet in Warteschlange)

Starten:
    pip install fastapi uvicorn requests beautifulsoup4 selenium pydantic
    uvicorn main:app --reload

Deployment (Empfehlung für den Start, kein eigener Server nötig):
    - Render.com oder Railway.app (kostenloser/günstiger Tier reicht für den Anfang)
    - Selenium braucht einen Chromedriver -> auf Render als "Docker Web Service" deployen
      (Dockerfile-Beispiel unten in den Kommentaren)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="Datengreifer API", version="1.0.0")

# Erlaubt Anfragen von deiner Landingpage (Domain später anpassen)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # in Produktion auf deine echte Domain einschränken
    allow_methods=["*"],
    allow_headers=["*"],
)

ORDERS_FILE = Path("orders.json")


# ---------------------------------------------------------------------------
# 1) STANDARD-TOOL: statisches Scraping (requests + BeautifulSoup)
# ---------------------------------------------------------------------------

class ScrapeStaticRequest(BaseModel):
    url: HttpUrl
    # CSS-Selektoren, die der Nutzer angibt, z.B. {"preis": ".price", "titel": "h1"}
    selectors: dict[str, str]


class ScrapeResult(BaseModel):
    url: str
    data: dict[str, Optional[str]]
    duration_ms: int


@app.post("/scrape/static", response_model=ScrapeResult)
def scrape_static(payload: ScrapeStaticRequest):
    """
    Lädt eine normale (nicht JS-gerenderte) Seite und extrahiert Felder
    per CSS-Selektor. Ideal für: Produktseiten, Preislisten, einfache Kataloge.
    """
    start = time.time()
    try:
        resp = requests.get(
            str(payload.url),
            headers={"User-Agent": "Mozilla/5.0 (compatible; DatengreiferBot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Seite nicht erreichbar: {exc}")

    soup = BeautifulSoup(resp.text, "html.parser")

    data: dict[str, Optional[str]] = {}
    for field_name, css_selector in payload.selectors.items():
        el = soup.select_one(css_selector)
        data[field_name] = el.get_text(strip=True) if el else None

    duration_ms = int((time.time() - start) * 1000)
    return ScrapeResult(url=str(payload.url), data=data, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# 2) STANDARD-TOOL: dynamisches Scraping (Selenium) für JS-lastige Seiten
# ---------------------------------------------------------------------------

class ScrapeDynamicRequest(BaseModel):
    url: HttpUrl
    selectors: dict[str, str]
    wait_seconds: int = 3  # Zeit, die der Seite zum Rendern gegeben wird


@app.post("/scrape/dynamic", response_model=ScrapeResult)
def scrape_dynamic(payload: ScrapeDynamicRequest):
    """
    Nutzt einen headless Chrome-Browser (Selenium), um Seiten zu laden, die
    ihren Inhalt erst per JavaScript nachladen (z.B. Infinite Scroll, SPA-Shops).
    Langsamer als /scrape/static, aber deckt mehr Fälle ab.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    start = time.time()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(str(payload.url))
        time.sleep(payload.wait_seconds)
        soup = BeautifulSoup(driver.page_source, "html.parser")
    finally:
        driver.quit()

    data: dict[str, Optional[str]] = {}
    for field_name, css_selector in payload.selectors.items():
        el = soup.select_one(css_selector)
        data[field_name] = el.get_text(strip=True) if el else None

    duration_ms = int((time.time() - start) * 1000)
    return ScrapeResult(url=str(payload.url), data=data, duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# 3) INDIVIDUELLE AUFTRÄGE: Kunden reichen einen individuellen Bedarf ein
# ---------------------------------------------------------------------------

class OrderRequest(BaseModel):
    name: str
    email: str
    beschreibung: str        # was soll gescraped werden
    ziel_url: Optional[str] = None
    budget: Optional[str] = None


class Order(OrderRequest):
    id: str
    status: str = "neu"
    erstellt_am: float


def _load_orders() -> list[dict]:
    if ORDERS_FILE.exists():
        return json.loads(ORDERS_FILE.read_text())
    return []


def _save_orders(orders: list[dict]) -> None:
    ORDERS_FILE.write_text(json.dumps(orders, indent=2, ensure_ascii=False))


def _send_notification_email(order: dict) -> None:
    """
    Schickt eine E-Mail bei jeder neuen Anfrage - über die Resend-API
    (normale Web-Anfrage, kein SMTP -> läuft zuverlässig auf Render).

    Nötige Umgebungsvariable (in Render unter 'Environment' eintragen):
      RESEND_API_KEY  -> API-Key von resend.com (kostenloses Konto)
      NOTIFY_EMAIL    -> Adresse, an die die Benachrichtigung gehen soll
                         (im kostenlosen Resend-Testmodus: die E-Mail-Adresse,
                         mit der du dich bei Resend registriert hast)

    Ist RESEND_API_KEY nicht gesetzt, wird der Versand einfach übersprungen.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("NOTIFY_EMAIL")

    if not api_key or not recipient:
        return  # E-Mail-Versand nicht konfiguriert -> überspringen

    body_text = (
        f"Neue individuelle Anfrage über Datengreifer:\n\n"
        f"Name: {order['name']}\n"
        f"E-Mail: {order['email']}\n"
        f"Ziel-URL: {order.get('ziel_url') or '-'}\n"
        f"Budget: {order.get('budget') or '-'}\n\n"
        f"Beschreibung:\n{order['beschreibung']}"
    )

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Datengreifer <onboarding@resend.dev>",
                "to": [recipient],
                "subject": f"Neue Anfrage von {order['name']}",
                "text": body_text,
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            print(f"E-Mail-Versand fehlgeschlagen: {resp.status_code} {resp.text}")
    except requests.RequestException as exc:
        # E-Mail-Versand darf niemals die Anfrage selbst zum Scheitern bringen
        print(f"E-Mail-Versand fehlgeschlagen: {exc}")


@app.post("/orders", response_model=Order)
def create_order(payload: OrderRequest):
    """Landet im Kontaktformular deiner Landingpage für Custom-Aufträge."""
    orders = _load_orders()
    order = Order(id=str(uuid.uuid4()), erstellt_am=time.time(), **payload.model_dump())
    orders.append(order.model_dump())
    _save_orders(orders)
    _send_notification_email(order.model_dump())
    return order


@app.get("/orders", response_model=list[Order])
def list_orders():
    """Internes Dashboard: alle eingegangenen Custom-Anfragen."""
    return _load_orders()


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dockerfile-Vorlage für Deployment mit Selenium (als Kommentar zum Kopieren)
# ---------------------------------------------------------------------------
#
# FROM python:3.11-slim
# RUN apt-get update && apt-get install -y wget unzip chromium chromium-driver
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
