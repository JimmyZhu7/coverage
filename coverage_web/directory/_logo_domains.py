# Firm slug -> the firm's own front-door domain. Hand-written; every entry is
# probed for a real logo before it is committed to the database.
DOMAINS = {
    "point72": "point72.com", "janestreet": "janestreet.com",
    "optiver": "optiver.com", "drw": "drw.com", "imc": "imc.com",
    "jump": "jumptrading.com", "hrt": "hudsonrivertrading.com",
    "mizuho": "mizuhogroup.com", "troweprice": "troweprice.com",
    "oliverwyman": "oliverwyman.com", "pimco": "pimco.com",
    "ares": "aresmgmt.com", "raymondjames": "raymondjames.com",
    "capitalgroup": "capitalgroup.com", "vanguard": "vanguard.com",
    "brookfield": "brookfield.com", "carlyle": "carlyle.com",
    "pwc": "pwc.com", "franklintempleton": "franklintempleton.com",
    "fidelityintl": "fidelityinternational.com",
    "alliancebernstein": "alliancebernstein.com", "invesco": "invesco.com",
    "neubergerberman": "nb.com", "oaktree": "oaktreecapital.com",
    "blueowl": "blueowl.com", "apollo": "apollo.com", "virtu": "virtu.com",
    "williamblair": "williamblair.com", "flowtraders": "flowtraders.com",
    "golub": "golubcapital.com", "akuna": "akunacapital.com",
    "baincapital": "baincapital.com", "baird": "rwbaird.com",
    "sixthstreet": "sixthstreet.com", "pipersandler": "pipersandler.com",
    "fiverings": "fiveringsllc.com", "brattle": "brattle.com",
    "generalatlantic": "generalatlantic.com", "schroders": "schroders.com",
    "tpg": "tpg.com", "eqt": "eqtgroup.com", "statestreet": "statestreet.com",
    "wellington": "wellington.com", "solomonpartners": "solomonpartners.com",
    "mangroup": "man.com",
    # Firms whose first domain yielded nothing usable. Each replacement was
    # probed against all three sources before being written here (2026-08-05,
    # after the owner reported specific firms missing or looking wrong).
    "guggenheim": "guggenheiminvestments.com",   # partners.com serves no icon
    "rbc": "jobs.rbc.com",                       # 114px vs 32px on rbc.com
    "tpg": "tpginc.com",                         # 64px clean vs 32px tiled
    "macquarie": "macquarie.com.au",             # 96px vs 32px
    "millennium": "mlp.com",                     # 192px clean
    "clsa": "clsa.com",                          # 48px, resolves now
    "boci": "bocigroup.com",
    "huatai": "htsc.com.cn", "guotaijunan": "gtjai.com",
    # blackrock is DELIBERATELY absent. Its own domains serve nothing above
    # 16px, and the tempting fallback — ishares.com, 32px and clean — is a
    # DIFFERENT BRAND's mark. A wrong logo is worse than a monogram.
    # cicc, franklintempleton, huatai and statestreet are the same story with
    # no near-miss at all: every candidate probed empty. They keep monograms.
}
