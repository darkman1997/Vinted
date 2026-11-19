def riga_venditore(extra: dict) -> str:
    """Riga del venditore con la sola valutazione complessiva su 5."""
    if not extra.get("venditore"):
        return ""

    riga = "👤 {}".format(html.escape(str(extra["venditore"])))
    voto = (extra.get("stelle") or "").strip()
    if voto:
        riga += " · ⭐ {}/5".format(html.escape(voto))
    return riga
