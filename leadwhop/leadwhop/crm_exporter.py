"""Stage 5 — CRM-ready export (exact port of the production Salesforce script).

Output columns (exact order):
    FirstName, LastName, Company, Website, Email, Title, Country, Industry,
    Business_Category__c, Function_Picklist__c, Sub_Industry_Form__c,
    Level__c, Market_Positioning__c, Event_Name__c, RecordTypeID

Design notes (mirrors the original):
- TEXT-based picklist values everywhere (not Salesforce record IDs).
- Country is matched to the Salesforce Country picklist text via
  normalize -> alias table (Turkish names & abbreviations) -> exact ->
  difflib fuzzy (cutoff 0.78). Unmatched countries are left EMPTY on
  purpose, to avoid Salesforce import errors from wrong values.
- Industry is re-derived from the Sub-Industry prefix at the end, so the
  two columns can never contradict each other.
- All GPT verdicts are disk-cached (persistent version of the original
  in-memory dictionaries).
"""
from __future__ import annotations

import re
import unicodedata

import difflib
import pandas as pd

from .llm import LLM

# ==========================================================================
# Picklist values (verbatim from production)
# ==========================================================================

FUNCTION_VALUES = [
    "GCA Strategy and Corporate Management",
    "GCA Procurement (Purchasing)",
    "GCA Sales",
    "GCA Marketing",
    "GCA Planning",
    "GCA Production (Manufacturing)",
    "GCA Maintenance",
    "GCA Warehouse & Logistics",
    "GCA Finance",
    "GCA Information Technologies (IT)",
    "GCA Human Resources (HR)",
    "GCA Business Development",
    "Global Procurement Category Manager Glass",
]

INDUSTRY_VALUES = ["Food & Beverage", "Non-Food Related Manufacturing"]

SUB_INDUSTRY_VALUES = [
    "Food & Beverage - Manufacture of beer",
    "Food & Beverage - Manufacture of dairy products",
    "Food & Beverage - Manufacture of non-alcoholic beverages",
    "Food & Beverage - Manufacture of other food products",
    "Food & Beverage - Manufacture of spirits",
    "Food & Beverage - Manufacture of vegetable and animal oils and fats",
    "Food & Beverage - Manufacture of wine",
    "Food & Beverage - Processing and preserving of fruit and vegetables",
    "Food & Beverage - Processing and preserving of meat / seafood",
    "Non-Food Related Manufacturing - Manufacture of chemicals and chemical products (soap, candle, etc.)",
    "Non-Food Related Manufacturing - Manufacture of glass and glass products",
    "Non-Food Related Manufacturing - Other Manufacturing",
    "Non-Food Related Manufacturing - Perfumery & Cosmetics",
]

LEVEL_VALUES = [
    "Board Level", "C-Suite Level", "Upper Managerial Level",
    "Manager Level", "Mid Level", "Entry Level",
]

COUNTRY_VALUES = [x.strip() for x in """
Andorra
United Arab Emirates
Afghanistan
Antigua and Barbuda
Anguilla
Albania
Armenia
Angola
Antarctica
Argentina
Austria
Australia
Aruba
Aland Islands
Azerbaijan
Bosnia and Herzegovina
Barbados
Bangladesh
Belgium
Burkina Faso
Bulgaria
Bahrain
Burundi
Benin
Saint Barthélemy
Bermuda
Brunei Darussalam
Bolivia, Plurinational State of
Bonaire, Sint Eustatius and Saba
Brazil
Bahamas
Bhutan
Bouvet Island
Botswana
Belarus
Belize
Canada
Cocos (Keeling) Islands
Congo, the Democratic Republic of the
Central African Republic
Congo
Switzerland
Cote d'Ivoire
Côte d'Ivoire
Cook Islands
Chile
Cameroon
China
Colombia
Costa Rica
Cuba
Cape Verde
Curaçao
Christmas Island
Cyprus
Czech Republic
Germany
Djibouti
Denmark
Dominica
Dominican Republic
Algeria
Ecuador
Estonia
Egypt
Western Sahara
Eritrea
Spain
Ethiopia
Finland
Fiji
Falkland Islands (Malvinas)
Faroe Islands
France
Gabon
United Kingdom
Grenada
Georgia
French Guiana
Guernsey
Ghana
Gibraltar
Greenland
Gambia
Guinea
Guadeloupe
Equatorial Guinea
Greece
South Georgia and the South Sandwich Islands
Guatemala
Guinea-Bissau
Guyana
Heard Island and McDonald Islands
Honduras
Croatia
Haiti
Hungary
Indonesia
Ireland
Israel
Isle of Man
India
British Indian Ocean Territory
Iraq
Iran, Islamic Republic of
Iceland
Italy
Jersey
Jamaica
Jordan
Japan
Kenya
Kyrgyzstan
Cambodia
Kiribati
Comoros
Saint Kitts and Nevis
Korea, Democratic People's Republic of
Korea, Republic of
Kosova
Kuwait
Cayman Islands
Kazakhstan
Lao People's Democratic Republic
Lebanon
Saint Lucia
Liechtenstein
Sri Lanka
Liberia
Lesotho
Lithuania
Luxembourg
Latvia
Libyan Arab Jamahiriya
Morocco
Monaco
Moldova, Republic of
Montenegro
Saint Martin (French part)
Madagascar
Macedonia, the former Yugoslav Republic of
Mali
Myanmar
Mongolia
Macao
Martinique
Mauritania
Montserrat
Malta
Mauritius
Maldives
Malawi
Mexico
Malaysia
Mozambique
Namibia
New Caledonia
Niger
Norfolk Island
Nigeria
Nicaragua
Netherlands
Norway
Nepal
Nauru
Niue
New Zealand
Oman
Panama
Peru
French Polynesia
Papua New Guinea
Philippines
Pakistan
Poland
Saint Pierre and Miquelon
Pitcairn
Puerto Rico
Palestinian Territory, Occupied
Portugal
Paraguay
Qatar
Reunion
Romania
Serbia
Russian Federation
Rwanda
Saudi Arabia
Solomon Islands
Seychelles
Sudan
Sweden
Singapore
Saint Helena, Ascension and Tristan da Cunha
Slovenia
Svalbard and Jan Mayen
Slovakia
Sierra Leone
San Marino
Senegal
Somalia
Suriname
South Sudan
Sao Tome and Principe
El Salvador
Sint Maarten (Dutch part)
Syrian Arab Republic
Swaziland
Turks and Caicos Islands
Chad
French Southern Territories
Togo
Thailand
Tajikistan
Tokelau
Timor-Leste
Turkmenistan
Tunisia
Tonga
Turkey
Trinidad and Tobago
Tuvalu
Chinese Taipei
Tanzania, United Republic of
Ukraine
Uganda
United States
Uruguay
Uzbekistan
Holy See (Vatican City State)
Saint Vincent and the Grenadines
Venezuela, Bolivarian Republic of
Virgin Islands, British
Viet Nam
Vanuatu
Wallis and Futuna
Samoa
Yemen
Mayotte
South Africa
Zambia
Zimbabwe
""".splitlines() if x.strip()]

COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "america": "United States",
    "united states of america": "United States", "abd": "United States",
    "amerika": "United States",
    "amerika birleşik devletleri": "United States",
    "amerikan birleşik devletleri": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom",
    "great britain": "United Kingdom", "britain": "United Kingdom",
    "england": "United Kingdom", "ingiltere": "United Kingdom",
    "birleşik krallık": "United Kingdom",
    "uae": "United Arab Emirates", "u.a.e.": "United Arab Emirates",
    "emirates": "United Arab Emirates", "bae": "United Arab Emirates",
    "birleşik arap emirlikleri": "United Arab Emirates",
    "andorra": "Andorra", "afganistan": "Afghanistan",
    "antigua ve barbuda": "Antigua and Barbuda", "anguilla": "Anguilla",
    "arnavutluk": "Albania", "ermenistan": "Armenia", "angola": "Angola",
    "antarktika": "Antarctica", "arjantin": "Argentina",
    "avusturya": "Austria", "avustralya": "Australia", "aruba": "Aruba",
    "aland adaları": "Aland Islands", "azerbaycan": "Azerbaijan",
    "bosna hersek": "Bosnia and Herzegovina",
    "bosna ve hersek": "Bosnia and Herzegovina", "barbados": "Barbados",
    "bangladeş": "Bangladesh", "belçika": "Belgium",
    "burkina faso": "Burkina Faso", "bulgaristan": "Bulgaria",
    "bahreyn": "Bahrain", "burundi": "Burundi", "benin": "Benin",
    "saint barthelemy": "Saint Barthélemy", "bermuda": "Bermuda",
    "brunei": "Brunei Darussalam",
    "bolivya": "Bolivia, Plurinational State of",
    "bonaire sint eustatius ve saba": "Bonaire, Sint Eustatius and Saba",
    "brezilya": "Brazil", "bahamalar": "Bahamas", "butan": "Bhutan",
    "bouvet adası": "Bouvet Island", "botsvana": "Botswana",
    "belarus": "Belarus", "beyaz rusya": "Belarus", "belize": "Belize",
    "kanada": "Canada", "cocos adaları": "Cocos (Keeling) Islands",
    "kongo demokratik cumhuriyeti": "Congo, the Democratic Republic of the",
    "demokratik kongo cumhuriyeti": "Congo, the Democratic Republic of the",
    "orta afrika cumhuriyeti": "Central African Republic", "kongo": "Congo",
    "isviçre": "Switzerland", "fildişi sahili": "Côte d'Ivoire",
    "cote d'ivoire": "Côte d'Ivoire", "cook adaları": "Cook Islands",
    "şili": "Chile", "kamerun": "Cameroon", "çin": "China",
    "kolombiya": "Colombia", "kosta rika": "Costa Rica", "kuba": "Cuba",
    "cape verde": "Cape Verde", "yeşil burun": "Cape Verde",
    "curaçao": "Curaçao", "curacao": "Curaçao",
    "christmas adası": "Christmas Island", "kıbrıs": "Cyprus",
    "çek cumhuriyeti": "Czech Republic", "çekya": "Czech Republic",
    "almanya": "Germany", "cibuti": "Djibouti", "danimarka": "Denmark",
    "dominika": "Dominica", "dominik cumhuriyeti": "Dominican Republic",
    "cezayir": "Algeria", "ekvador": "Ecuador", "estonya": "Estonia",
    "mısır": "Egypt", "batı sahra": "Western Sahara", "eritre": "Eritrea",
    "ispanya": "Spain", "etiyopya": "Ethiopia", "finlandiya": "Finland",
    "fiji": "Fiji", "falkland adaları": "Falkland Islands (Malvinas)",
    "faroe adaları": "Faroe Islands", "fransa": "France", "gabon": "Gabon",
    "grenada": "Grenada", "gürcistan": "Georgia",
    "fransız guyanası": "French Guiana", "guernsey": "Guernsey",
    "gana": "Ghana", "cebelitarık": "Gibraltar", "grönland": "Greenland",
    "gambiya": "Gambia", "gine": "Guinea", "guadeloupe": "Guadeloupe",
    "ekvator ginesi": "Equatorial Guinea", "yunanistan": "Greece",
    "güney georgia ve güney sandwich adaları":
        "South Georgia and the South Sandwich Islands",
    "guatemala": "Guatemala", "gine bissau": "Guinea-Bissau",
    "guyana": "Guyana",
    "heard adası ve mcdonald adaları": "Heard Island and McDonald Islands",
    "honduras": "Honduras", "hırvatistan": "Croatia", "haiti": "Haiti",
    "macaristan": "Hungary", "endonezya": "Indonesia", "irlanda": "Ireland",
    "israil": "Israel", "man adası": "Isle of Man", "hindistan": "India",
    "britanya hint okyanusu toprakları": "British Indian Ocean Territory",
    "irak": "Iraq", "iran": "Iran, Islamic Republic of",
    "izlanda": "Iceland", "italya": "Italy", "jersey": "Jersey",
    "jamaika": "Jamaica", "ürdün": "Jordan", "japonya": "Japan",
    "kenya": "Kenya", "kırgızistan": "Kyrgyzstan", "kamboçya": "Cambodia",
    "kiribati": "Kiribati", "komorlar": "Comoros",
    "saint kitts ve nevis": "Saint Kitts and Nevis",
    "kuzey kore": "Korea, Democratic People's Republic of",
    "güney kore": "Korea, Republic of", "kore": "Korea, Republic of",
    "kosova": "Kosova", "kuveyt": "Kuwait",
    "cayman adaları": "Cayman Islands", "kazakistan": "Kazakhstan",
    "laos": "Lao People's Democratic Republic", "lübnan": "Lebanon",
    "saint lucia": "Saint Lucia", "lihtenştayn": "Liechtenstein",
    "sri lanka": "Sri Lanka", "liberya": "Liberia", "lesotho": "Lesotho",
    "litvanya": "Lithuania", "lüksemburg": "Luxembourg",
    "letonya": "Latvia", "libya": "Libyan Arab Jamahiriya",
    "fas": "Morocco", "monako": "Monaco",
    "moldova": "Moldova, Republic of", "karadağ": "Montenegro",
    "saint martin": "Saint Martin (French part)",
    "madagaskar": "Madagascar",
    "makedonya": "Macedonia, the former Yugoslav Republic of",
    "kuzey makedonya": "Macedonia, the former Yugoslav Republic of",
    "mali": "Mali", "myanmar": "Myanmar", "burma": "Myanmar",
    "moğolistan": "Mongolia", "makao": "Macao", "martinik": "Martinique",
    "moritanya": "Mauritania", "montserrat": "Montserrat", "malta": "Malta",
    "mauritius": "Mauritius", "maldivler": "Maldives", "malavi": "Malawi",
    "meksika": "Mexico", "malezya": "Malaysia", "mozambik": "Mozambique",
    "namibya": "Namibia", "yeni kaledonya": "New Caledonia",
    "nijer": "Niger", "norfolk adası": "Norfolk Island",
    "nijerya": "Nigeria", "nikaragua": "Nicaragua",
    "hollanda": "Netherlands", "norveç": "Norway", "nepal": "Nepal",
    "nauru": "Nauru", "niue": "Niue", "yeni zelanda": "New Zealand",
    "umman": "Oman", "panama": "Panama", "peru": "Peru",
    "fransız polinezyası": "French Polynesia",
    "papua yeni gine": "Papua New Guinea", "filipinler": "Philippines",
    "pakistan": "Pakistan", "polonya": "Poland",
    "saint pierre ve miquelon": "Saint Pierre and Miquelon",
    "pitcairn": "Pitcairn", "porto riko": "Puerto Rico",
    "filistin": "Palestinian Territory, Occupied", "portekiz": "Portugal",
    "paraguay": "Paraguay", "katar": "Qatar", "reunion": "Reunion",
    "réunion": "Reunion", "romanya": "Romania", "sırbistan": "Serbia",
    "rusya": "Russian Federation",
    "russian federation": "Russian Federation", "ruanda": "Rwanda",
    "suudi arabistan": "Saudi Arabia",
    "solomon adaları": "Solomon Islands", "seyşeller": "Seychelles",
    "sudan": "Sudan", "isveç": "Sweden", "singapur": "Singapore",
    "saint helena": "Saint Helena, Ascension and Tristan da Cunha",
    "slovenya": "Slovenia",
    "svalbard ve jan mayen": "Svalbard and Jan Mayen",
    "slovakya": "Slovakia", "sierra leone": "Sierra Leone",
    "san marino": "San Marino", "senegal": "Senegal", "somali": "Somalia",
    "surinam": "Suriname", "güney sudan": "South Sudan",
    "sao tome ve principe": "Sao Tome and Principe",
    "el salvador": "El Salvador",
    "sint maarten": "Sint Maarten (Dutch part)",
    "suriye": "Syrian Arab Republic", "svaziland": "Swaziland",
    "esvatini": "Swaziland",
    "turks ve caicos adaları": "Turks and Caicos Islands", "çad": "Chad",
    "fransız güney toprakları": "French Southern Territories",
    "togo": "Togo", "tayland": "Thailand", "tacikistan": "Tajikistan",
    "tokelau": "Tokelau", "timor leste": "Timor-Leste",
    "doğu timor": "Timor-Leste", "türkmenistan": "Turkmenistan",
    "tunus": "Tunisia", "tonga": "Tonga", "türkiye": "Turkey",
    "turkiye": "Turkey", "tr": "Turkey",
    "trinidad ve tobago": "Trinidad and Tobago", "tuvalu": "Tuvalu",
    "tayvan": "Chinese Taipei", "çin taypesi": "Chinese Taipei",
    "chinese taipei": "Chinese Taipei",
    "tanzanya": "Tanzania, United Republic of", "ukrayna": "Ukraine",
    "uganda": "Uganda", "uruguay": "Uruguay", "özbekistan": "Uzbekistan",
    "vatikan": "Holy See (Vatican City State)",
    "saint vincent ve grenadinler": "Saint Vincent and the Grenadines",
    "venezuela": "Venezuela, Bolivarian Republic of",
    "ingiliz virgin adaları": "Virgin Islands, British",
    "british virgin islands": "Virgin Islands, British",
    "vietnam": "Viet Nam", "viet nam": "Viet Nam", "vanuatu": "Vanuatu",
    "wallis ve futuna": "Wallis and Futuna", "samoa": "Samoa",
    "yemen": "Yemen", "mayotte": "Mayotte", "güney afrika": "South Africa",
    "zambiya": "Zambia", "zimbabve": "Zimbabwe",
}

FINAL_COLUMN_ORDER = [
    "FirstName", "LastName", "Company", "Website", "Email", "Title",
    "Country", "Industry", "Business_Category__c", "Function_Picklist__c",
    "Sub_Industry_Form__c", "Level__c", "Market_Positioning__c",
    "Event_Name__c", "RecordTypeID",
]

# Turkish input column names -> pipeline column names (export-only uploads)
TURKISH_COLUMN_MAP = {
    "Şirket": "Company", "Domain": "Website", "E-posta": "Email",
    "Unvan": "Title", "İsim": "Name", "AI_Notu": "AI_Note", "Ülke": "Country",
}


# ==========================================================================
# Helpers
# ==========================================================================

def normalize_text(text) -> str:
    if pd.isna(text):
        return ""
    text = str(text).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


COUNTRY_NORMALIZED = {normalize_text(c): c for c in COUNTRY_VALUES}
COUNTRY_ALIASES_NORMALIZED = {normalize_text(k): v for k, v in COUNTRY_ALIASES.items()}


def split_name(full_name) -> tuple[str, str]:
    full_name = str(full_name or "").strip()
    if not full_name or full_name in ("-", "nan"):
        return "Bilinmiyor", "Bilinmiyor"
    parts = full_name.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "Bilinmiyor")


def match_country(raw) -> str:
    """Country -> Salesforce picklist TEXT. Unmatched -> '' (safe import)."""
    if pd.isna(raw) or str(raw).strip() == "":
        return ""
    clean = normalize_text(raw)
    if clean in COUNTRY_ALIASES_NORMALIZED:
        return COUNTRY_ALIASES_NORMALIZED[clean]
    if clean in COUNTRY_NORMALIZED:
        return COUNTRY_NORMALIZED[clean]
    all_keys = list(COUNTRY_NORMALIZED) + list(COUNTRY_ALIASES_NORMALIZED)
    close = difflib.get_close_matches(clean, all_keys, n=1, cutoff=0.78)
    if close:
        hit = close[0]
        return COUNTRY_NORMALIZED.get(hit) or COUNTRY_ALIASES_NORMALIZED.get(hit, "")
    return ""


def industry_from_subindustry(sub) -> str:
    if isinstance(sub, str) and sub.startswith("Food & Beverage"):
        return "Food & Beverage"
    return "Non-Food Related Manufacturing"


# ==========================================================================
# Main class
# ==========================================================================

class CRMExporter:
    def __init__(self, llm: LLM, mapping: dict | None = None):
        self.llm = llm
        m = mapping or {}
        self.record_type_id     = m.get("record_type_id", "012WP0000001n6jYAA")
        self.business_category  = m.get("business_category", "Distributor")
        self.market_positioning = m.get("market_positioning", "Strong Player")

    # ---- GPT classifiers (disk-cached, Turkish production prompts) --------

    def business_function(self, title) -> str:
        default = "GCA Strategy and Corporate Management"
        if pd.isna(title) or str(title).strip() in ("", "-"):
            return default
        system = (
            "Sen B2B veri sınıflandırma uzmanısın. "
            "Verilen unvanı aşağıdaki KESİN Function kategorilerinden EN UYGUN olanına ata:\n\n"
            + ", ".join(FUNCTION_VALUES) + "\n\n"
            "REHBER:\n"
            "- CEO, Founder, President, Managing Director gibi unvanlar -> GCA Strategy and Corporate Management\n"
            "- Procurement, Purchasing, Buyer, Sourcing -> GCA Procurement (Purchasing)\n"
            "- Glass Category Manager, Category Manager Glass, Global Procurement Glass -> Global Procurement Category Manager Glass\n"
            "- Sales, Account Manager, Business Development Sales -> GCA Sales\n"
            "- Marketing, Brand, Communications -> GCA Marketing\n"
            "- Planning, Supply Planning, Demand Planning -> GCA Planning\n"
            "- Production, Manufacturing, Plant, Distiller, Brewer, Winemaker, Operations -> GCA Production (Manufacturing)\n"
            "- Maintenance, Engineering Maintenance -> GCA Maintenance\n"
            "- Warehouse, Logistics, Supply Chain, Distribution -> GCA Warehouse & Logistics\n"
            "- Finance, Accounting, Controller -> GCA Finance\n"
            "- IT, Information Technologies, Digital, Data -> GCA Information Technologies (IT)\n"
            "- HR, Human Resources, People -> GCA Human Resources (HR)\n"
            "- Business Development, Partnerships, Growth -> GCA Business Development\n\n"
            "KURALLAR: Sadece kategori adını birebir aynı yaz. Açıklama yapma."
        )
        return self.llm.choose("function", system, f"Unvan: {str(title).strip()}",
                               FUNCTION_VALUES, default)

    def industry(self, ai_note) -> str:
        default = "Non-Food Related Manufacturing"
        if pd.isna(ai_note) or str(ai_note).strip() == "":
            return default
        system = (
            "Üretim açıklamasına bakarak şirketi sadece şu iki Industry değerinden birine ata:\n\n"
            "Food & Beverage\nNon-Food Related Manufacturing\n\n"
            "KURALLAR: Sadece kategori ismini birebir yaz. Açıklama yapma."
        )
        return self.llm.choose("industry", system,
                               f"Açıklama: {str(ai_note).strip()}",
                               INDUSTRY_VALUES, default)

    def sub_industry(self, ai_note) -> str:
        default = "Non-Food Related Manufacturing - Other Manufacturing"
        if pd.isna(ai_note) or str(ai_note).strip() == "":
            return default
        system = (
            "Aşağıdaki üretim açıklamasına bakarak şirketin şu Sub-Industry "
            "değerlerinden HANGİSİNE ait olduğunu belirle:\n\n"
            + "\n".join(SUB_INDUSTRY_VALUES) + "\n\n"
            "REHBER:\n"
            "- Beer, brewery, brewing, ale, lager -> Food & Beverage - Manufacture of beer\n"
            "- Dairy, milk, cheese, yogurt, ice cream -> Food & Beverage - Manufacture of dairy products\n"
            "- Water, juice, soda, soft drink, energy drink, kombucha, tea, coffee drink -> Food & Beverage - Manufacture of non-alcoholic beverages\n"
            "- Sauce, snack, bakery, confectionery, chocolate, canned food, ready meal, general food -> Food & Beverage - Manufacture of other food products\n"
            "- Distillery, spirits, whiskey, whisky, vodka, gin, rum, tequila, liquor, liqueur -> Food & Beverage - Manufacture of spirits\n"
            "- Oil, olive oil, vegetable oil, animal fat -> Food & Beverage - Manufacture of vegetable and animal oils and fats\n"
            "- Wine, winery, vineyard, champagne, sparkling wine -> Food & Beverage - Manufacture of wine\n"
            "- Fruit, vegetable, jam, pickle, tomato paste, preserved vegetables -> Food & Beverage - Processing and preserving of fruit and vegetables\n"
            "- Meat, seafood, fish, poultry, preserved meat -> Food & Beverage - Processing and preserving of meat / seafood\n"
            "- Soap, candle, detergent, chemical products -> Non-Food Related Manufacturing - Manufacture of chemicals and chemical products (soap, candle, etc.)\n"
            "- Glass, glass packaging, bottle manufacturer, jar manufacturer -> Non-Food Related Manufacturing - Manufacture of glass and glass products\n"
            "- Perfume, cosmetics, beauty, skincare, personal care -> Non-Food Related Manufacturing - Perfumery & Cosmetics\n"
            "- Diğer tüm non-food manufacturing -> Non-Food Related Manufacturing - Other Manufacturing\n\n"
            "KURALLAR: Asla açıklama yapma. Sadece seçtiğin kategoriyi birebir yaz."
        )
        return self.llm.choose("sub_industry", system,
                               f"Açıklama: {str(ai_note).strip()}",
                               SUB_INDUSTRY_VALUES, default)

    def seniority(self, title) -> str:
        default = "Mid Level"
        if pd.isna(title) or str(title).strip() in ("", "-"):
            return default
        system = (
            "Sen bir İK uzmanısın. Verilen unvanın şu seviyelerden HANGİSİNE ait olduğunu belirle:\n\n"
            + ", ".join(LEVEL_VALUES) + "\n\n"
            "REHBER:\n"
            "- Board of Directors, Chairman -> Board Level\n"
            "- CEO, CFO, COO, CTO, President, Founder, Owner -> C-Suite Level\n"
            "- VP, Vice President, General Manager, Director, Head of -> Upper Managerial Level\n"
            "- Manager, Supervisor, Lead -> Manager Level\n"
            "- Specialist, Analyst, Coordinator, Senior, Engineer, Distiller, Brewer -> Mid Level\n"
            "- Assistant, Junior, Intern, Trainee -> Entry Level\n\n"
            "KURALLAR: Açıklama yapma. Sadece seçtiğin seviyeyi yaz."
        )
        return self.llm.choose("seniority", system, f"Unvan: {str(title).strip()}",
                               LEVEL_VALUES, default)

    # ---- main --------------------------------------------------------------

    def export(self, df: pd.DataFrame, event_name: str) -> pd.DataFrame:
        # Accept Turkish column names from legacy files
        df = df.rename(columns={k: v for k, v in TURKISH_COLUMN_MAP.items()
                                if k in df.columns})

        out = pd.DataFrame()
        out["Company"] = df.get("Company", "")
        out["Website"] = df.get("Website", "")
        out["Email"]   = df.get("Email", "")
        out["Title"]   = df.get("Title", "")

        names = df.get("Name", pd.Series([""] * len(df))).apply(
            lambda x: pd.Series(split_name(x)))
        out["FirstName"], out["LastName"] = names[0], names[1]

        out["Country"] = df.get("Country", pd.Series([""] * len(df))).apply(match_country)

        ai_note = df.get("AI_Note", pd.Series([""] * len(df)))
        title   = df.get("Title",   pd.Series([""] * len(df)))

        print("   ➤ 1/4 Function eşleştiriliyor...")
        out["Function_Picklist__c"] = title.apply(self.business_function)
        print("   ➤ 2/4 Industry eşleştiriliyor...")
        out["Industry"] = ai_note.apply(self.industry)
        print("   ➤ 3/4 Sub-Industry eşleştiriliyor...")
        out["Sub_Industry_Form__c"] = ai_note.apply(self.sub_industry)
        # Guarantee Industry consistency with Sub-Industry prefix
        out["Industry"] = out["Sub_Industry_Form__c"].apply(industry_from_subindustry)
        print("   ➤ 4/4 Kıdem seviyeleri eşleştiriliyor...")
        out["Level__c"] = title.apply(self.seniority)

        out["Business_Category__c"]  = self.business_category
        out["Market_Positioning__c"] = self.market_positioning
        out["Event_Name__c"]         = event_name
        out["RecordTypeID"]          = self.record_type_id

        return out[FINAL_COLUMN_ORDER]
