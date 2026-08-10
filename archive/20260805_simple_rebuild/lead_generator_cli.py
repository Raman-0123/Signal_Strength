"""Shared selector configuration for the Streamlit lead-generation UI.

The tables in this module are imported by ``app.py``. The optional
``if __name__ == "__main__"`` block remains only as a legacy developer utility;
normal lead harvesting is driven by the Streamlit interface.
"""

import os

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None

# ---------------------------------------------------------
# GEOGRAPHY — Granular cities across India's tech hubs
# ---------------------------------------------------------
LOCATIONS = {
    # Delhi-NCR cluster (each city separate for precise targeting)
    "1":  ["Delhi NCR", "New Delhi", "Delhi"],
    "2":  ["Gurugram", "Gurgaon"],
    "3":  ["Noida", "Greater Noida", "Noida Extension"],
    "4":  ["Faridabad", "Manesar", "Bhiwadi"],
    "5":  ["NCR", "National Capital Region"],

    # Bangalore cluster
    "6":  [
        "Bangalore", "Bengaluru", "Greater Bengaluru Area",
        "Bengaluru Urban", "Bangalore Urban",
    ],
    "7":  ["Koramangala", "HSR Layout", "Indiranagar"],
    "8":  ["Whitefield", "Electronic City", "Bellandur"],

    # Mumbai cluster
    "9":  ["Mumbai", "Navi Mumbai", "Thane"],
    "10": ["BKC", "Bandra Kurla Complex", "Powai", "Andheri"],
    "11": ["Lower Parel", "Worli", "Goregaon"],

    # Hyderabad cluster
    "12": ["Hyderabad"],
    "13": ["HITEC City", "Gachibowli", "Madhapur"],

    # Pune cluster
    "14": ["Pune"],
    "15": ["Hinjewadi", "Baner", "Kharadi", "Viman Nagar"],

    # Other Tier-1 & Tier-2 tech hubs
    "16": ["Chennai", "Chennai OMR", "Sholinganallur"],
    "17": ["Kolkata", "Salt Lake", "New Town Kolkata"],
    "18": ["Ahmedabad", "GIFT City"],
    "19": ["Jaipur", "Jodhpur"],
    "20": ["Chandigarh", "Mohali", "Panchkula"],
    "21": ["Kochi", "Trivandrum", "Thiruvananthapuram"],
    "22": ["Coimbatore", "Madurai"],
    "23": ["Lucknow", "Kanpur"],
    "24": ["Indore", "Bhopal"],
    "25": ["Global", "Pan India", "India"],

    # International tech clusters
    "26": ["Singapore"],
    "27": ["Dubai", "UAE", "Abu Dhabi"],
    "28": ["London", "UK", "United Kingdom"],
    "29": ["San Francisco", "Bay Area", "Silicon Valley", "California"],
    "30": ["New York", "NYC", "NY"],

    # Additional domestic tech nodes
    "31": ["Bhubaneswar", "Orissa", "Odisha"],
    "32": ["Visakhapatnam", "Vizag", "Andhra Pradesh"],
    "33": ["Patna", "Bihar"],
    "34": ["Surat", "Gujarat"],
    "35": ["Vadodara", "Baroda"],
    "36": ["Guwahati", "Assam"],
    "37": ["Goa", "Panaji"],
    "38": ["Dehradun", "Uttarakhand"],
    "39": ["Mysore", "Mysuru"],
}

# ---------------------------------------------------------
# ROLES — discrete seniority/persona choices
# ---------------------------------------------------------
# Keep these as separate choices.  The UI used to bundle 8-11 aliases into a
# single selection, which let the first selected family consume the bounded
# query plan before the other personas were searched.
ROLES = {
    "1":  ["CEO", "Chief Executive Officer"],
    "2":  ["Founder", "Co-Founder", "Co Founder"],
    "3":  ["Managing Director", "MD"],
    "4":  ["President"],
    "5":  ["CMO", "Chief Marketing Officer"],
    "6":  ["CTO", "Chief Technology Officer"],
    "7":  ["CIO", "Chief Information Officer"],
    "8":  ["CDO", "Chief Data Officer"],
    "9":  ["Chief Digital Officer"],
    "10": ["VP", "SVP", "AVP", "Vice President", "Senior Vice President", "Assistant Vice President"],
    "11": ["Director"],
    "12": ["Head of Marketing", "Marketing Head", "Country Marketing Head"],
    "13": ["CFO", "Chief Financial Officer", "Head of Finance"],
    "14": ["COO", "Chief Operating Officer", "Head of Operations"],
    "15": ["CISO", "Chief Information Security Officer", "Head of Security"],
    "16": ["CRO", "Chief Revenue Officer", "Chief Sales Officer"],
    "17": ["CPO", "Chief Product Officer", "Head of Product"],
    "18": ["CHRO", "Chief Human Resources Officer", "Chief People Officer"],
    "19": ["Head of Digital Transformation", "VP Digital", "Head of Innovation"],
    "20": ["Head of Engineering", "VP Engineering", "VP Technology"],
    "21": ["Head of Sales", "VP Sales", "SVP Sales"],
    "22": [
        "Head of Talent Acquisition",
        "Talent Acquisition Head",
        "VP Talent Acquisition",
        "Director of Talent Acquisition",
        "Talent Acquisition Director",
        "Senior Director of Talent Acquisition",
        "Sr Director of Talent Acquisition",
        "Global Head of Talent Acquisition",
        "India Head of Talent Acquisition",
        "Regional Head of Talent Acquisition",
        "Head of Recruitment",
        "Talent Acquisition Leader",
    ],
    "23": [
        "Chief Customer Officer",
        "Customer Success",
        "Head of Customer Success",
        "VP Customer Success",
        "Vice President Customer Success",
        "Director Customer Success",
        "Customer Success Director",
        "Customer Experience",
        "Head of Customer Experience",
        "VP Customer Experience",
        "CX Leader",
        "Customer Success Leader",
    ],
}

ROLE_LABELS = {
    "1": "CEO",
    "2": "Founder / Co-Founder",
    "3": "Managing Director",
    "4": "President",
    "5": "CMO",
    "6": "CTO",
    "7": "CIO",
    "8": "CDO (Chief Data Officer)",
    "9": "Chief Digital Officer",
    "10": "VP / SVP / AVP",
    "11": "Director",
    "12": "Head of Marketing",
    "13": "CFO",
    "14": "COO",
    "15": "CISO",
    "16": "CRO / Chief Sales Officer",
    "17": "CPO / Head of Product",
    "18": "CHRO / Chief People Officer",
    "19": "Digital Transformation / Innovation",
    "20": "Engineering / Technology Leadership",
    "21": "Sales Leadership",
    "22": "Talent Acquisition Leadership",
    "23": "Customer Success / CX Leadership",
}

# ---------------------------------------------------------
# INDUSTRIES — discrete sectors for precise targeting
# ---------------------------------------------------------
INDUSTRIES = {
    "1":  ["Information Technology", "IT", "Technology"],
    "2":  ["IT Services", "Technology Services", "Digital Services"],
    "3":  ["Software", "SaaS", "Software as a Service"],
    "4":  ["Enterprise Software", "Enterprise Technology", "Business Software"],
    "5":  ["Cloud Computing", "Cloud", "Data", "Artificial Intelligence", "AI"],
    "6":  ["BFSI", "Banking Financial Services and Insurance", "Financial Services"],
    "7":  ["Banking", "Bank", "Commercial Banking", "Corporate Banking"],
    "8":  ["FinTech", "Payments", "Payment Technology", "Digital Payments"],
    "9":  ["Insurance", "InsurTech", "Life Insurance", "General Insurance"],
    "10": ["NBFC", "Lending", "Consumer Lending", "Business Lending"],
    "11": ["Wealth Management", "Asset Management", "Investment Management", "Capital Markets"],
    "12": ["Manufacturing", "Industrial Manufacturing", "Engineering"],
    "13": ["Automotive", "Electric Vehicle", "EV", "Auto Components"],
    "14": ["Logistics", "Supply Chain", "Warehousing", "Transportation"],
    "15": ["Healthcare", "Hospitals", "HealthTech", "MedTech", "Diagnostics"],
    "16": ["Pharmaceuticals", "Pharma", "Life Sciences", "Biotechnology"],
    "17": ["Retail", "E-Commerce", "D2C", "Marketplace"],
    "18": ["FMCG", "Consumer Goods", "Consumer Products"],
    "19": ["Telecom", "Telecommunications", "5G", "Networks"],
    "20": ["Cybersecurity", "Information Security", "InfoSec", "Data Privacy"],
    "21": ["Global Capability Center", "Global Capability Centre", "GCC", "GIC", "Captive Center"],
    "22": ["Consulting", "Business Process Outsourcing", "BPO", "KPO", "Shared Services"],
    "23": ["EdTech", "Education Technology", "Education", "Learning"],
    "24": ["Real Estate", "PropTech", "Construction", "Infrastructure"],
    "25": ["Media", "Entertainment", "OTT", "AdTech", "Gaming"],
    "26": ["Energy", "Utilities", "CleanTech", "Renewable Energy", "Solar"],
    "27": ["AgriTech", "Agriculture", "FoodTech", "Food Technology"],
    "28": ["Startup", "Unicorn", "Series A", "Series B", "Series C", "Funded"],
    "29": ["MNC", "Multinational", "Fortune 500", "Global Company"],
    "30": ["Government", "Public Sector", "PSU", "Public Administration"],
    "31": ["Travel", "Hospitality", "Travel Technology", "Hotel"],
}

INDUSTRY_LABELS = {
    "1": "IT / Information Technology",
    "2": "IT Services",
    "3": "Software / SaaS",
    "4": "Enterprise Software",
    "5": "Cloud / AI / Data",
    "6": "BFSI",
    "7": "Banking",
    "8": "FinTech / Payments",
    "9": "Insurance / InsurTech",
    "10": "NBFC / Lending",
    "11": "Wealth / Asset Management / Capital Markets",
    "12": "Manufacturing / Engineering",
    "13": "Automotive / EV",
    "14": "Logistics / Supply Chain",
    "15": "Healthcare / Hospitals / MedTech",
    "16": "Pharma / Life Sciences / Biotech",
    "17": "Retail / E-Commerce / D2C",
    "18": "FMCG / Consumer Goods",
    "19": "Telecom / 5G",
    "20": "Cybersecurity / InfoSec",
    "21": "GCC / GIC / Captive Centre",
    "22": "Consulting / BPO / KPO / Shared Services",
    "23": "EdTech / Education",
    "24": "Real Estate / Construction / Infrastructure",
    "25": "Media / Entertainment / Gaming",
    "26": "Energy / Utilities / CleanTech",
    "27": "AgriTech / FoodTech",
    "28": "Startup / Unicorn",
    "29": "MNC / Fortune 500",
    "30": "Government / PSU / Public Sector",
    "31": "Travel / Hospitality",
}

# ---------------------------------------------------------
# SIGNALS — Intelligence-grade event/intent indicators
# POCs most likely to join events/roundtables have these signals
# ---------------------------------------------------------
SIGNALS = {
    # ★ HIGHEST INTENT — Past event participation (strongest predictor)
    "1": ["speaker", "keynote speaker", "keynote", "panelist", "conference speaker",
          "summit speaker", "TEDx", "TEDxTalk", "panel discussion"],

    # ★ HIGH INTENT — Roundtable & leadership forums
    "2": ["roundtable", "CXO roundtable", "leadership summit", "executive roundtable",
          "boardroom", "CXO forum", "leadership forum", "C-suite roundtable"],

    # ★ HIGH INTENT — Industry recognition (proven public figure)
    "3": ["award", "award winner", "40 under 40", "30 under 30", "Forbes",
          "ET 40 under 40", "Economic Times", "Business Today", "CIO100",
          "CMO Asia", "recognised", "felicitated"],

    # ★ HIGH INTENT — Thought leadership (active content creator = likely invitee)
    "4": ["author", "published", "book author", "LinkedIn article", "thought leader",
          "columnist", "contributor", "Forbes contributor", "Harvard Business Review"],

    # ★ MEDIUM INTENT — Advisory & Board roles (senior influencer)
    "5": ["advisory board", "board member", "mentor", "investor", "angel investor",
          "advisor", "independent director", "board of directors"],

    # ★ MEDIUM INTENT — Community & association leadership
    "6": ["NASSCOM", "CII", "FICCI", "TiE", "YPO", "EO Entrepreneurs Organization",
          "industry association", "chapter president", "co-chair"],

    # ★ MEDIUM INTENT — Company growth signals (decision makers with budget)
    "7": ["Series A", "Series B", "Series C", "IPO", "recently funded", "unicorn",
          "expansion", "new market", "global expansion", "hiring"],

    # ★ LOW INTENT — General prospecting (no specific signal)
    "8": ["None - General Prospecting"],

    # ★ GCC-SPECIFIC — leaders visible at capability-centre forums
    "9": ["GCC Roundtable", "Global Capability Center roundtable",
          "Global Capability Centre roundtable", "GCC leadership forum",
          "GCC summit", "GCC conclave"],
}

# ---------------------------------------------------------
# SIGNAL INTELLIGENCE SCORING
# Weights for event-attendance probability prediction
# ---------------------------------------------------------
SIGNAL_INTELLIGENCE = {
    "past_speaker":       {"keywords": ["speaker", "keynote", "panelist", "TEDx"], "score": 95},
    "roundtable":         {"keywords": ["roundtable", "CXO roundtable", "boardroom"], "score": 92},
    "award_winner":       {"keywords": ["award", "40 under 40", "Forbes", "recognised"], "score": 88},
    "thought_leader":     {"keywords": ["author", "published", "LinkedIn article", "columnist"], "score": 82},
    "board_advisor":      {"keywords": ["advisory board", "board member", "mentor", "investor"], "score": 78},
    "association_leader": {"keywords": ["NASSCOM", "CII", "FICCI", "TiE", "YPO"], "score": 75},
    "funded_company":     {"keywords": ["Series A", "Series B", "unicorn", "funded"], "score": 65},
    "general":            {"keywords": [], "score": 50},
}

def get_event_probability_score(body: str, title: str, person_name: str = "") -> tuple[int, str]:
    """
    Compute a POC's probability from contextual, attributable evidence.

    Kept as a compatibility wrapper for the company and competitor tabs.  The
    main harvesting engine uses the full assessment, including evidence and
    selected-signal verification.
    Returns (score, signal_name)
    """
    from core.signal_intelligence import assess_signal

    assessment = assess_signal(title, body, person_name=person_name)
    return assessment["score"], assessment["name"]

# ---------------------------------------------------------
# COMPANY_NAMES — Used for local DB fallback
# ---------------------------------------------------------
COMPANY_NAMES = {
    "SaaS/Tech": ["Freshworks", "Postman", "BrowserStack", "Zoho", "Druva", "Innovaccer",
                   "Mindtickle", "HighRadius", "Clevertap", "Darwinbox", "Leadsquared",
                   "Whatfix", "Unacademy", "Razorpay", "Slice", "Jupiter", "Groww"],
    "FinTech/BFSI": ["Razorpay", "Pine Labs", "CRED", "Zerodha", "Groww", "HDFC Bank",
                      "ICICI Bank", "Kotak Mahindra", "Paytm", "PhonePe", "BharatPe",
                      "Axis Bank", "SBI", "Bajaj Finserv", "PolicyBazaar"],
    "Enterprise": ["Infosys", "Wipro", "TCS", "Tech Mahindra", "HCLTech", "LTIMindtree",
                    "Accenture India", "IBM India", "Microsoft India", "SAP India",
                    "Oracle India", "Capgemini", "Cognizant"],
    "E-Commerce": ["Flipkart", "Myntra", "Amazon India", "Meesho", "Nykaa",
                    "Zomato", "Swiggy", "Delhivery", "Blinkit", "BigBasket"],
    "Healthcare": ["Apollo Hospitals", "Narayana Health", "Practo", "PharmEasy",
                    "Tata 1mg", "Fortis", "Max Healthcare", "Medanta", "Aster Hospitals"],
    "Manufacturing": ["Tata Motors", "Mahindra", "Hero MotoCorp", "Maruti Suzuki",
                       "L&T", "Bharat Forge", "Godrej", "Havells", "Voltas"],
}


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def generate_boolean_query(roles, locations, industries, signals):
    role_str = " OR ".join([f'"{r}"' if ' ' in r else r for r in roles])
    loc_str  = " OR ".join([f'"{l}"' if ' ' in l else l for l in locations])
    query = f'site:linkedin.com/in ({role_str}) ({loc_str})'
    if industries:
        ind_str = " OR ".join([f'"{i}"' if ' ' in i else i for i in industries])
        query += f' ({ind_str})'
    if signals and signals[0] != "None - General Prospecting":
        sig_str = " OR ".join([f'"{s}"' if ' ' in s else s for s in signals])
        query += f' ({sig_str})'
    return query
