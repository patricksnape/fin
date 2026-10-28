"""Fill in expense categories from what the source already knows about each merchant.

The card portal tags every transaction with a merchant type (the card network's category) and a
coarse category of its own. `rules/categories.yaml` in the data repo maps those to your expense
accounts; the merchant type is tried first, then the coarse one. Your own payee rules (`fin-rule`)
always win, and anything unmapped stays `Expenses:Uncategorized` with `#review`. It is plain code:
no LLM, and nothing but category names is read. Edit the file to change the mapping, then run
`fin categorize` to apply it to transactions that are still uncategorized.
"""

from datetime import date
from pathlib import Path

import yaml
from beancount.core import data

from fin import account_admin, config, labels, ledger
from fin.analytics.recurring import normalize_payee

MAP_FILE = "rules/categories.yaml"
OPENED_ON = date(2000, 1, 1)  # long before any history, so any past transaction fits

# (cadence, necessity) for accounts the default mapping may open
CHART = {
    "Expenses:Food:Groceries": ("regular", "essential"),
    "Expenses:Food:Dining": ("regular", "discretionary"),
    "Expenses:Transport:Public": ("regular", "essential"),
    "Expenses:Transport:Car": ("regular", "essential"),
    "Expenses:Transport:Parking": ("regular", "discretionary"),
    "Expenses:Transport:Taxi": ("regular", "discretionary"),
    "Expenses:Health:Pharmacy": ("regular", "essential"),
    "Expenses:Health:Beauty": ("regular", "discretionary"),
    "Expenses:Pets": ("regular", "essential"),
    "Expenses:Shopping:General": ("one_off", "discretionary"),
    "Expenses:Shopping:Clothing": ("one_off", "discretionary"),
    "Expenses:Shopping:Electronics": ("one_off", "discretionary"),
    "Expenses:Shopping:Home": ("one_off", "discretionary"),
    "Expenses:Shopping:Media": ("one_off", "discretionary"),
    "Expenses:Subscriptions:Streaming": ("recurring", "discretionary"),
    "Expenses:Subscriptions:Software": ("recurring", "discretionary"),
    "Expenses:Entertainment": ("regular", "discretionary"),
    "Expenses:Travel:Flights": ("one_off", "discretionary"),
    "Expenses:Travel:Hotels": ("one_off", "discretionary"),
    "Expenses:Travel:Other": ("one_off", "discretionary"),
    "Expenses:Travel:CarRental": ("one_off", "discretionary"),
    "Expenses:Shopping:Gift": ("one_off", "discretionary"),
}

_G, _D = "Expenses:Food:Groceries", "Expenses:Food:Dining"
DEFAULT_MAP = {
    "swisscard": {
        "merchant": {
            "GROCERY STORES, SUPERMARKETS": _G,
            "BAKERIES": _G,
            "MISCELLANEOUS FOOD STORES, MARKETS": _G,
            "CANDY, NUT, CONFECTIONERY STORES": _G,
            "EATING PLACES, RESTAURANTS": _D,
            "FAST FOOD RESTAURANTS": _D,
            "CATERERS": _D,
            "AUTOMOBILE PARKING LOTS AND GARAGES": "Expenses:Transport:Parking",
            "LIMOUSINES AND TAXICABS": "Expenses:Transport:Taxi",
            "PASSENGER RAILWAYS": "Expenses:Transport:Public",
            "FUEL DISPENSER, AUTOMATED": "Expenses:Transport:Car",
            "SERVICE STATIONS (W/WITHOUT SERVICES)": "Expenses:Transport:Car",
            "BRIDGE AND ROAD FEES, TOLLS": "Expenses:Transport:Car",
            "DEPARTMENT STORES": "Expenses:Shopping:General",
            "VARIETY STORES": "Expenses:Shopping:General",
            "MISCELLANEOUS GENERAL MERCHANDISE": "Expenses:Shopping:General",
            "CATALOG MERCHANTS": "Expenses:Shopping:General",
            "SECOND HAND STORES": "Expenses:Shopping:General",
            "MISCELLANEOUS AND SPECIALTY RETAIL STORES": "Expenses:Shopping:General",
            "MEN & WOMEN'S CLOTHING": "Expenses:Shopping:Clothing",
            "ACCESSORY AND APPAREL STORES": "Expenses:Shopping:Clothing",
            "CHILDREN'S AND INFANTS WEAR": "Expenses:Shopping:Clothing",
            "WOMEN'S READY TO WEAR": "Expenses:Shopping:Clothing",
            "Electronic Stores": "Expenses:Shopping:Electronics",
            "EQUIPMENT, FURNITURE STORES": "Expenses:Shopping:Home",
            "HOME SUPPLY WAREHOUSE STORES": "Expenses:Shopping:Home",
            "HARDWARE EQUIPMENT AND SUPPLIES": "Expenses:Shopping:Home",
            "BOOKS, NEWSPAPERS, PERIODICALS": "Expenses:Shopping:Media",
            "BOOK STORES": "Expenses:Shopping:Media",
            "DIGITAL GOODS - MEDIA, BOOKS, MOVIES, MUSIC": "Expenses:Shopping:Media",
            "DIGITAL GOODS - MULTI-CATEGORY": "Expenses:Subscriptions:Software",
            "COMPUTER SOFTWARE STORES": "Expenses:Subscriptions:Software",
            "CABLE, SATELLITE, AND OTHER PAY TELEVISION AND RADIO": "Expenses:Subscriptions:Streaming",
            "PET SHOPS, PET FOOD SUPPLIERS": "Expenses:Pets",
            "DRUG STORES and Pharmacies": "Expenses:Health:Pharmacy",
            "COSMETIC STORES": "Expenses:Health:Beauty",
            "BARBER AND BEAUTY SHOPS": "Expenses:Health:Beauty",
            "HEALTH AND BEAUTY SPAS": "Expenses:Health:Beauty",
            "LODGING NOT SPECIFIED": "Expenses:Travel:Hotels",
            "AIR CANADA": "Expenses:Travel:Flights",
        },
        # the portal's coarse category, used when the merchant type is unmapped or missing
        "portal": {
            "GROCERIES": _G,
            "FOOD_AND_DRINK": _D,
            "SHOPPING": "Expenses:Shopping:General",
            "HOUSEHOLD": "Expenses:Shopping:Home",
            "AUTO": "Expenses:Transport:Car",
            "TRAVEL": "Expenses:Travel:Other",
            "HEALTH_AND_BEAUTY": "Expenses:Health:Beauty",
            "ENTERTAINMENT": "Expenses:Entertainment",
        },
    },
    # UBS card: `merchantName` is the merchant type in plain words (some are airline/hotel brands)
    "ubs": {
        "merchant": {
            "Restaurants": _D,
            "Fast-Food Restaurants": _D,
            "Fast Food Restaurant": _D,
            "Caterers": _D,
            "Grocery stores": _G,
            "Bakeries": _G,
            "Package stores - beer": _G,
            "Taxicabs": "Expenses:Transport:Taxi",
            "Parking & Garages": "Expenses:Transport:Parking",
            "Commuter transportation": "Expenses:Transport:Public",
            "Passenger railways": "Expenses:Transport:Public",
            "Bus lines, Tour buses": "Expenses:Transport:Public",
            "Gasoline service stations": "Expenses:Transport:Car",
            "Car component": "Expenses:Transport:Car",
            "Car Rental Company": "Expenses:Travel:CarRental",
            "Rent-a-car": "Expenses:Travel:CarRental",
            "Airlines": "Expenses:Travel:Flights",
            "Swiss International Air Lines": "Expenses:Travel:Flights",
            "British Airways": "Expenses:Travel:Flights",
            "United": "Expenses:Travel:Flights",
            "Air Canada": "Expenses:Travel:Flights",
            "Hotels": "Expenses:Travel:Hotels",
            "Marriott": "Expenses:Travel:Hotels",
            "Courtyard By Marriott": "Expenses:Travel:Hotels",
            "Hilton": "Expenses:Travel:Hotels",
            "Doubletree Hotel": "Expenses:Travel:Hotels",
            "Prince Hotel": "Expenses:Travel:Hotels",
            "Westin Hotels": "Expenses:Travel:Hotels",
            "Premier Travel Inn": "Expenses:Travel:Hotels",
            "Homewood Suites": "Expenses:Travel:Hotels",
            "Aparments": "Expenses:Travel:Hotels",
            "Travel agencies": "Expenses:Travel:Other",
            "Digital goods": "Expenses:Subscriptions:Software",
            "Computer software stores": "Expenses:Subscriptions:Software",
            "Clothing store": "Expenses:Shopping:Clothing",
            "Clothing - sports": "Expenses:Shopping:Clothing",
            "Shoe stores": "Expenses:Shopping:Clothing",
            "Department stores": "Expenses:Shopping:General",
            "Duty free shop": "Expenses:Shopping:General",
            "Secondhand stores": "Expenses:Shopping:General",
            "Games and hobby stores": "Expenses:Shopping:General",
            "Clock or jewelry or watch stores": "Expenses:Shopping:General",
            "Electronics Stores": "Expenses:Shopping:Electronics",
            "Home supply warehouse stores": "Expenses:Shopping:Home",
            "News Dealer & Newsstands": "Expenses:Shopping:Media",
            "Florists": "Expenses:Shopping:Gift",
            "Card or gift or novelty or souvenir shop": "Expenses:Shopping:Gift",
            "Pharmacies": "Expenses:Health:Pharmacy",
            "Barber or beauty shops": "Expenses:Health:Beauty",
            "Pet shops pet foods and supplies stores": "Expenses:Pets",
            "Bands, Orchestras & Music Entertainment": "Expenses:Entertainment",
            "Recreation Services": "Expenses:Entertainment",
            "Tourist Attractions and Exhibits": "Expenses:Entertainment",
            "Public Golf Courses": "Expenses:Entertainment",
            "Amusement park": "Expenses:Entertainment",
            "Theather Production / Ticket Agencies": "Expenses:Entertainment",
            "Cinema": "Expenses:Entertainment",
            "Commercial Sports, Professional Sports Clubs, Athletic Fields, and Sports Promoters": "Expenses:Entertainment",
        },
    },
    # Wise card: the card network's merchant type, cut at 32 characters (Wise's own category is
    # the same text). Medical, government, tax, charity and cash withdrawals are left for review.
    "wise": {
        "merchant": {
            "Eating Places, Restaurants": _D,
            "Fast Food Restaurants": _D,
            "Bars, Cocktail Lounges, Discothe": _D,
            "Caterers": _D,
            "Grocery Stores, Supermarkets": _G,
            "Miscellaneous Food Stores Conven": _G,
            "Candy, Nut, Confectionery Stores": _G,
            "Limousines": "Expenses:Transport:Taxi",
            "Transportation Suburban and Loca": "Expenses:Transport:Public",
            "Passenger Railways": "Expenses:Transport:Public",
            "Bus Lines": "Expenses:Transport:Public",
            "Automobile Parking Lots and Gara": "Expenses:Transport:Parking",
            "Service Stations (with or withou": "Expenses:Transport:Car",
            "Fuel Dispenser, Automated": "Expenses:Transport:Car",
            "Electric Vehicle Charging": "Expenses:Transport:Car",
            "Bridge and Road Fees, Tolls": "Expenses:Transport:Car",
            "Lodging Hotels, Motels, Resorts": "Expenses:Travel:Hotels",
            "Premier Travel Inns": "Expenses:Travel:Hotels",
            "Prince Hotels": "Expenses:Travel:Hotels",
            "Travel Agencies and Tour Operato": "Expenses:Travel:Other",
            "Tap (Portugal) TAP": "Expenses:Travel:Flights",
            "Alaska Airlines Inc. ALASKA A": "Expenses:Travel:Flights",
            "Cable, Satellite, and Other Pay": "Expenses:Subscriptions:Streaming",
            "Computer Software Stores": "Expenses:Subscriptions:Software",
            "5999 R Miscellaneous and Special": "Expenses:Shopping:General",
            "Direct Marketing Catalog Merchan": "Expenses:Shopping:General",
            "Miscellaneous General Merchandis": "Expenses:Shopping:General",
            "Department Stores": "Expenses:Shopping:General",
            "Discount Stores": "Expenses:Shopping:General",
            "Duty Free Stores": "Expenses:Shopping:General",
            "Game, Toy, and Hobby Shops": "Expenses:Shopping:General",
            "Sporting Goods Stores": "Expenses:Shopping:General",
            "Accessory and Apparel Stores Mis": "Expenses:Shopping:Clothing",
            "Men s and Boys  Clothing and Acc": "Expenses:Shopping:Clothing",
            "Men s and Women s Clothing Store": "Expenses:Shopping:Clothing",
            "Women s Accessory and Specialty": "Expenses:Shopping:Clothing",
            "Family Clothing Stores": "Expenses:Shopping:Clothing",
            "Shoe Stores": "Expenses:Shopping:Clothing",
            "Computers, Computer Peripheral E": "Expenses:Shopping:Electronics",
            "Camera and Photographic Supply S": "Expenses:Shopping:Electronics",
            "Equipment, Furniture, and Home F": "Expenses:Shopping:Home",
            "Fabric, Needlework, Piece Goods,": "Expenses:Shopping:Home",
            "News Dealers and Newsstands": "Expenses:Shopping:Media",
            "Book Stores": "Expenses:Shopping:Media",
            "D-Goods:Media,Books,Movies,Music": "Expenses:Shopping:Media",
            "Card, Gift, Novelty, and Souveni": "Expenses:Shopping:Gift",
            "Drug Stores, Pharmacies": "Expenses:Health:Pharmacy",
            "Barber and Beauty Shops": "Expenses:Health:Beauty",
            "Cosmetic Stores": "Expenses:Health:Beauty",
            "Pet Shops Pet Food and Supplies": "Expenses:Pets",
            "Tourist Attractions and Exhibits": "Expenses:Entertainment",
            "Amusement Parks, Carnivals, Circ": "Expenses:Entertainment",
            "Theatrical Producers (except Mot": "Expenses:Entertainment",
            "Motion Picture Theaters": "Expenses:Entertainment",
            "Bowling Alleys": "Expenses:Entertainment",
            "Golf Courses, Public": "Expenses:Entertainment",
            "Clubs Country Clubs, Membership": "Expenses:Entertainment",
        },
    },
}
HEADER = (
    "# Maps a source's own categories to your expense accounts. Merchant type is tried first, then\n"
    "# the portal's coarse category. Edit freely, then run `fin categorize`. Your payee rules win.\n"
    "# llm: true makes every import ask a model about merchants that are still generic (it sees\n"
    "# merchant names and types, never amounts; see `fin categorize --llm --dry-run`).\n"
    "# llm_refine: accounts that are too generic and should be refined by the model.\n"
)


def load_map(root: Path) -> dict:
    path = root / MAP_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        defaults = {"llm": False, "llm_refine": ["Expenses:Shopping:General"], **DEFAULT_MAP}
        path.write_text(HEADER + yaml.safe_dump(defaults, sort_keys=False, width=200))
    maps = yaml.safe_load(path.read_text()) or {}
    if missing := {k: v for k, v in DEFAULT_MAP.items() if k not in maps}:
        # a source added since the file was written: append its defaults, keep your edits
        with path.open("a") as f:
            f.write("\n" + yaml.safe_dump(missing, sort_keys=False, width=200))
        maps |= missing
    return maps


def account_for(mapping: dict, meta: dict) -> str | None:
    return mapping.get("merchant", {}).get(meta.get("merchant_category")) or mapping.get(
        "portal", {}
    ).get(meta.get("portal_category"))


def apply_mapping(root: Path, source: str | None = None) -> int:
    """Categorize still-uncategorized transactions from their source categories; returns how many."""
    maps = load_map(root)
    led = ledger.load(config.main_ledger(root))
    rules = labels.read_rules(led)
    wanted: dict[str, str] = {}
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not (e.meta or {}).get("ref"):
            continue
        src = e.meta["ref"].partition(":")[0]
        if (source and src != source) or src not in maps:
            continue
        spent = [p for p in e.postings if p.account.startswith("Expenses:")]
        if len(spent) != 1 or spent[0].account != labels.UNCATEGORIZED:
            continue
        rule = labels.rule_for(
            rules, normalize_payee(e.payee or ""), e.meta.get("merchant_category")
        )
        if rule and "category" in rule:
            continue  # apply_rules fills these in
        if account := account_for(maps[src], e.meta):
            wanted[e.meta["ref"]] = account
    if not wanted:
        return 0
    opened = {e.account for e in led.entries if isinstance(e, data.Open)}
    for account in sorted(set(wanted.values()) - opened):
        cadence, necessity = CHART.get(account, ("regular", "discretionary"))
        account_admin.add_account(
            root, account, cadence=cadence, necessity=necessity, since=OPENED_ON
        )
    return labels.apply(
        root,
        {ref: {"category": a, "categorized": "mapping"} for ref, a in wanted.items()},
        f"categorize: {len(wanted)} transaction(s) from source categories",
    )
