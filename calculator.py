def calculate_zakat(
    ticker: str,
    shares: float,
    market_price: float,
    cri_per_share: float,
    intent: str = "fundamental",
) -> dict:
    """
    Calculate Zakat using the CRI (Cash, Receivables, Inventory) method.

    intent='fundamental' → long-term / passive holding → CRI-per-share is the zakatable base per share
    intent='active'      → trading / flipping intent   → full market price is the zakatable base per share
    """
    intent = (intent or "fundamental").lower().strip()

    if intent == "active":
        zakatable_base = market_price * shares
    else:
        zakatable_base = cri_per_share * shares

    zakat_due = zakatable_base * 0.025

    return {
        "zakatable_base": round(zakatable_base, 2),
        "zakat_due": round(zakat_due, 2),
        "market_value": round(market_price * shares, 2),
    }
