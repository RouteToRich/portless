"""Country name → ISO-2, for feeds that publish names, not codes (FATF lists, OECD table, sanctions regimes).

A plain table (no dependency): official short names plus the common variants the feeds use. ``iso2``
returns None for a name it does not know — the caller reports it, never guesses.
"""
from __future__ import annotations

import re
import unicodedata

NAMES: dict[str, str] = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "angola": "AO", "argentina": "AR", "armenia": "AM",
    "australia": "AU", "austria": "AT", "azerbaijan": "AZ", "bahamas": "BS", "the bahamas": "BS", "bahrain": "BH",
    "bangladesh": "BD", "barbados": "BB", "belarus": "BY", "belgium": "BE", "belize": "BZ", "benin": "BJ",
    "bhutan": "BT", "bolivia": "BO", "bosnia and herzegovina": "BA", "botswana": "BW", "brazil": "BR",
    "brunei": "BN", "brunei darussalam": "BN", "bulgaria": "BG", "burkina faso": "BF", "burundi": "BI",
    "cambodia": "KH", "cameroon": "CM", "canada": "CA", "cape verde": "CV", "cabo verde": "CV",
    "central african republic": "CF", "chad": "TD", "chile": "CL", "china": "CN", "colombia": "CO",
    "comoros": "KM", "congo": "CG", "republic of the congo": "CG", "democratic republic of the congo": "CD",
    "democratic republic of congo": "CD", "drc": "CD", "costa rica": "CR", "cote d'ivoire": "CI",
    "cote divoire": "CI", "ivory coast": "CI", "croatia": "HR", "cuba": "CU", "cyprus": "CY", "czechia": "CZ",
    "czech republic": "CZ", "denmark": "DK", "djibouti": "DJ", "dominican republic": "DO", "ecuador": "EC",
    "egypt": "EG", "el salvador": "SV", "equatorial guinea": "GQ", "eritrea": "ER", "estonia": "EE",
    "eswatini": "SZ", "ethiopia": "ET", "fiji": "FJ", "finland": "FI", "france": "FR", "gabon": "GA",
    "gambia": "GM", "the gambia": "GM", "georgia": "GE", "germany": "DE", "ghana": "GH", "gibraltar": "GI",
    "greece": "GR", "guatemala": "GT", "guinea": "GN", "guinea-bissau": "GW", "guyana": "GY", "haiti": "HT",
    "honduras": "HN", "hong kong": "HK", "hungary": "HU", "iceland": "IS", "india": "IN", "indonesia": "ID",
    "iran": "IR", "islamic republic of iran": "IR", "iraq": "IQ", "ireland": "IE", "israel": "IL", "italy": "IT",
    "jamaica": "JM", "japan": "JP", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE", "kosovo": "XK",
    "kuwait": "KW", "kyrgyzstan": "KG", "kyrgyz republic": "KG", "laos": "LA", "lao pdr": "LA",
    "lao people's democratic republic": "LA", "latvia": "LV", "lebanon": "LB", "lesotho": "LS", "liberia": "LR",
    "libya": "LY", "liechtenstein": "LI", "lithuania": "LT", "luxembourg": "LU", "macao": "MO", "madagascar": "MG",
    "malawi": "MW", "malaysia": "MY", "maldives": "MV", "mali": "ML", "malta": "MT", "mauritania": "MR",
    "mauritius": "MU", "mexico": "MX", "moldova": "MD", "monaco": "MC", "mongolia": "MN", "montenegro": "ME",
    "morocco": "MA", "mozambique": "MZ", "myanmar": "MM", "burma": "MM", "namibia": "NA", "nepal": "NP",
    "netherlands": "NL", "the netherlands": "NL", "new zealand": "NZ", "nicaragua": "NI", "niger": "NE",
    "nigeria": "NG", "north korea": "KP", "dprk": "KP", "democratic people's republic of korea": "KP",
    "north macedonia": "MK", "norway": "NO", "oman": "OM", "pakistan": "PK", "palestine": "PS", "panama": "PA",
    "papua new guinea": "PG", "paraguay": "PY", "peru": "PE", "philippines": "PH", "poland": "PL", "portugal": "PT",
    "qatar": "QA", "romania": "RO", "russia": "RU", "russian federation": "RU", "rwanda": "RW",
    "saudi arabia": "SA", "senegal": "SN", "serbia": "RS", "seychelles": "SC", "sierra leone": "SL",
    "singapore": "SG", "slovakia": "SK", "slovenia": "SI", "somalia": "SO", "south africa": "ZA",
    "south korea": "KR", "republic of korea": "KR", "korea": "KR", "south sudan": "SS", "spain": "ES",
    "sri lanka": "LK", "sudan": "SD", "suriname": "SR", "sweden": "SE", "switzerland": "CH", "syria": "SY",
    "syrian arab republic": "SY", "taiwan": "TW", "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH",
    "togo": "TG", "trinidad and tobago": "TT", "tunisia": "TN", "turkey": "TR", "turkiye": "TR",
    "turkmenistan": "TM", "uganda": "UG", "ukraine": "UA", "united arab emirates": "AE", "uae": "AE",
    "united kingdom": "GB", "uk": "GB", "united states": "US", "united states of america": "US", "usa": "US",
    "uruguay": "UY", "uzbekistan": "UZ", "vanuatu": "VU", "venezuela": "VE", "vietnam": "VN", "viet nam": "VN",
    "yemen": "YE", "zambia": "ZM", "zimbabwe": "ZW",
}


def _clean(name: str) -> str:
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    t = re.sub(r"\(.*?\)", " ", t.lower()).replace("’", "'")
    return " ".join(re.sub(r"[^a-z' -]", " ", t).split())


def iso2(name: str | None) -> str | None:
    t = _clean(name or "")
    if len(t) == 2 and t.upper() in set(NAMES.values()):
        return t.upper()
    return NAMES.get(t)


def find_all(text: str) -> list[str]:
    """Every country named in a text (longest names first, each once, in order of first mention)."""
    t = " " + _clean(text) + " "
    hits: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for name in sorted(NAMES, key=len, reverse=True):
        if len(name) <= 3:
            continue  # codes / short aliases are too ambiguous inside free text
        for m in re.finditer(r"(?<=[ ])" + re.escape(name) + r"(?=[ ])", t):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), NAMES[name]))
    out: list[str] = []
    for _, cc in sorted(hits):
        if cc not in out:
            out.append(cc)
    return out
