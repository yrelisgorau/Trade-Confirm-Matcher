"""Reference data and tolerance rules for the trade confirmation matcher.

All counterparties are fictional. Several belong to the same group with more
than one legal entity, which is where real "wrong counterparty" breaks come from:
the trade is booked against the wrong entity of the right group.
"""
from dataclasses import dataclass

OUR_ENTITY = "Example Bank Commodities Ltd"


@dataclass(frozen=True)
class Counterparty:
    code: str
    legal_name: str
    group: str
    aliases: tuple  # how the counterparty's confirmation system may spell its own name


COUNTERPARTIES = [
    Counterparty("ATLAS-SG", "Atlas Petroleum Trading Pte Ltd", "ATLAS",
                 ("ATLAS PETROLEUM TRADING PTE. LTD.", "Atlas Petroleum Trading Pte Limited")),
    Counterparty("ATLAS-UK", "Atlas Petroleum Trading (UK) Ltd", "ATLAS",
                 ("ATLAS PETROLEUM TRADING (UK) LIMITED", "Atlas Petroleum Trading UK Ltd")),
    Counterparty("KESTREL-US", "Kestrel Capital Markets LLC", "KESTREL",
                 ("KESTREL CAPITAL MARKETS, L.L.C.", "Kestrel Cap Mkts LLC")),
    Counterparty("KESTREL-INT", "Kestrel Capital Markets International plc", "KESTREL",
                 ("KESTREL CAPITAL MARKETS INTL PLC", "Kestrel Capital Mkts International PLC")),
    Counterparty("NORTHSEA", "Northsea Energy Trading Ltd", "NORTHSEA",
                 ("NORTHSEA ENERGY TRADING LIMITED", "North Sea Energy Trading Ltd")),
    Counterparty("MERIDIAN", "Meridian Commodities SA", "MERIDIAN",
                 ("MERIDIAN COMMODITIES S.A.", "Meridian Commodities S A")),
    Counterparty("HALVORSEN", "Halvorsen Metals AG", "HALVORSEN",
                 ("HALVORSEN METALS A.G.", "Halvorsen Metals Aktiengesellschaft")),
    Counterparty("ORINOCO", "Orinoco Gas Marketing Inc", "ORINOCO",
                 ("ORINOCO GAS MARKETING, INC.", "Orinoco Gas Mktg Inc")),
    Counterparty("BLUEWATER", "Bluewater Shipping & Trading BV", "BLUEWATER",
                 ("BLUEWATER SHIPPING AND TRADING B.V.", "Bluewater Shipping & Trading B.V.")),
]

# Products: floating index, unit, currency, price tick, realistic price range and
# quantity-per-month choices. 'family' groups indices that could be confused.
PRODUCTS = {
    "BRENT":  dict(index="ICE Brent 1st Line",      unit="BBL",   ccy="USD", tick=0.01,  price=(60, 90),      qty=[5_000, 10_000, 25_000, 50_000, 100_000], family="CRUDE"),
    "WTI":    dict(index="NYMEX WTI 1st Line",      unit="BBL",   ccy="USD", tick=0.01,  price=(55, 85),      qty=[5_000, 10_000, 25_000, 50_000, 100_000], family="CRUDE"),
    "DUBAI":  dict(index="Platts Dubai Monthly Avg", unit="BBL",  ccy="USD", tick=0.01,  price=(58, 88),      qty=[25_000, 50_000, 100_000],                family="CRUDE"),
    "GASOIL": dict(index="ICE Gasoil 1st Line",     unit="MT",    ccy="USD", tick=0.25,  price=(600, 850),    qty=[1_000, 2_000, 5_000, 10_000],            family="PRODUCTS"),
    "HH":     dict(index="NYMEX Henry Hub LD",      unit="MMBTU", ccy="USD", tick=0.001, price=(2.5, 5.0),    qty=[100_000, 250_000, 500_000, 1_000_000],   family="GAS"),
    "TTF":    dict(index="ICE Endex TTF Month Ahead", unit="MWH", ccy="EUR", tick=0.005, price=(25, 50),      qty=[7_200, 14_400, 36_000],                  family="GAS"),
    "COPPER": dict(index="LME Copper 3M",           unit="MT",    ccy="USD", tick=0.5,   price=(8_500, 10_500), qty=[25, 50, 100, 250],                     family="METALS"),
}

# Illustrative FX used only to express impact in USD.
FX_TO_USD = {"USD": 1.0, "EUR": 1.08}

# ---------------------------------------------------------------- tolerance rules
# Price: a confirmation may quote a price rounded to the product's tick, so any
# difference up to PRICE_TOLERANCE_TICKS ticks is treated as a tolerance match.
PRICE_TOLERANCE_TICKS = 1.0
# Quantity: compared after normalising to quantity per calculation period.
# Relative tolerance is for rounding when total quantity is divided by months.
QUANTITY_REL_TOLERANCE = 1e-6
# Dates (trade date, period start/end): exact match required.
DATE_TOLERANCE_DAYS = 0

# Economic (no-reference) matching: fields compared and the minimum number that
# must agree before a confirmation is paired with a booking.
# Unit and currency are blocking keys (must agree before scoring at all).
ECONOMIC_MATCH_FIELDS = ["counterparty", "trade_date", "index", "direction",
                         "period", "quantity", "price"]
ECONOMIC_MATCH_MIN_SCORE = 5

# Missing-confirmation ageing: overdue after this many business days.
CONFIRM_SLA_BDAYS = 2

# Impact thresholds (USD) for prioritising exceptions.
HIGH_IMPACT_USD = 250_000
MEDIUM_IMPACT_USD = 25_000
