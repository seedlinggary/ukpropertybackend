"""
IDOX planning portal scraper — Layer 2 coverage expansion.

IDOX (Idox Group) powers ~60-70% of UK council planning portals.
Their portals share a standardised URL structure and HTML result format.
This module searches a council's IDOX portal when the user's postcode
matches a known council in the registry.

All requests are live — no data stored in DB.
Timeout per portal: 10s (aggressive; keeps total API time <20s with parallel calls).
"""

import logging
import re
import warnings
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
log = logging.getLogger(__name__)

# ─── Portal registry ──────────────────────────────────────────────────────────
# Keys are the council's admin_district name as returned by postcodes.io,
# OR partial names used by _find_la_entity fuzzy matching.
# Values are the base URL of the IDOX "online-applications" portal.

IDOX_PORTALS: dict[str, str] = {
    # ── London ────────────────────────────────────────────────────────────────
    "London Borough of Barnet":            "https://publicaccess.barnet.gov.uk/online-applications",
    "London Borough of Ealing":            "https://pam.ealing.gov.uk/online-applications",
    "London Borough of Greenwich":         "https://planning.royalgreenwich.gov.uk/online-applications",
    "London Borough of Hackney":           "https://planning.hackney.gov.uk/online-applications",
    "London Borough of Hammersmith and Fulham": "https://public-access.lbhf.gov.uk/online-applications",
    "London Borough of Haringey":          "https://publicaccess.haringey.gov.uk/online-applications",
    "London Borough of Harrow":            "https://planning.harrow.gov.uk/online-applications",
    "London Borough of Havering":          "https://development.havering.gov.uk/online-applications",
    "London Borough of Islington":         "https://publicaccess.islington.gov.uk/online-applications",
    "London Borough of Lambeth":           "https://planning.lambeth.gov.uk/online-applications",
    "London Borough of Lewisham":          "https://planning.lewisham.gov.uk/online-applications",
    "London Borough of Merton":            "https://planning.merton.gov.uk/online-applications",
    "London Borough of Southwark":         "https://planning.southwark.gov.uk/online-applications",
    "London Borough of Sutton":            "https://idoxpa.sutton.gov.uk/online-applications",
    "London Borough of Tower Hamlets":     "https://development.towerhamlets.gov.uk/online-applications",
    "London Borough of Waltham Forest":    "https://planning.walthamforest.gov.uk/online-applications",
    "London Borough of Wandsworth":        "https://planning2.wandsworth.gov.uk/online-applications",
    # ── South East ────────────────────────────────────────────────────────────
    "Brighton and Hove City Council":      "https://planningapps.brighton-hove.gov.uk/online-applications",
    "Canterbury City Council":             "https://planning.canterbury.gov.uk/online-applications",
    "Chichester District Council":         "https://publicaccess.chichester.gov.uk/online-applications",
    "Dover District Council":              "https://planning.dover.gov.uk/online-applications",
    "East Hampshire District Council":     "https://planningpublicaccess.easthants.gov.uk/online-applications",
    "Eastbourne Borough Council":          "https://planning.eastbourne.gov.uk/online-applications",
    "Epsom and Ewell Borough Council":     "https://eplanning.epsom-ewell.gov.uk/online-applications",
    "Guildford Borough Council":           "https://publicaccess.guildford.gov.uk/online-applications",
    "Hastings Borough Council":            "https://publicaccess.hastings.gov.uk/online-applications",
    "Horsham District Council":            "https://www.horsham.gov.uk/planning/search-for-planning-applications",
    "Maidstone Borough Council":           "https://pa.maidstone.gov.uk/online-applications",
    "Medway Council":                      "https://planningapps.medway.gov.uk/online-applications",
    "Oxford City Council":                 "https://publicaccess.oxford.gov.uk/online-applications",
    "Portsmouth City Council":             "https://planningapplications.portsmouth.gov.uk/online-applications",
    "Reading Borough Council":             "https://publicaccess.reading.gov.uk/online-applications",
    "Rother District Council":             "https://planningapps.rother.gov.uk/online-applications",
    "Shepway District Council":            "https://planning.folkestone-hythe.gov.uk/online-applications",
    "Southampton City Council":            "https://planningapplications.southampton.gov.uk/online-applications",
    "Swale Borough Council":               "https://pa.swale.gov.uk/online-applications",
    "Thanet District Council":             "https://planningapps.thanet.gov.uk/online-applications",
    "Tonbridge and Malling Borough Council": "https://publicaccess.tmbc.gov.uk/online-applications",
    "Worthing Borough Council":            "https://planning.adur-worthing.gov.uk/online-applications",
    "Adur District Council":               "https://planning.adur-worthing.gov.uk/online-applications",
    # ── South West ────────────────────────────────────────────────────────────
    "Bristol City Council":                "https://pa.bristol.gov.uk/online-applications",
    "Exeter City Council":                 "https://publicaccess.exeter.gov.uk/online-applications",
    "Plymouth City Council":               "https://planning.plymouth.gov.uk/online-applications",
    "Torbay Council":                      "https://www.torbay.gov.uk/planning/planning-applications/search-for-planning-applications",
    # ── East of England ───────────────────────────────────────────────────────
    "Cambridge City Council":              "https://pa.cambridge.gov.uk/online-applications",
    "Colchester City Council":             "https://publicaccess.colchester.gov.uk/online-applications",
    "Great Yarmouth Borough Council":      "https://planning.great-yarmouth.gov.uk/online-applications",
    "Huntingdonshire District Council":    "https://publicaccess.huntsdc.gov.uk/online-applications",
    "North Norfolk District Council":      "https://www.north-norfolk.gov.uk/online-applications",
    "Peterborough City Council":           "https://publicaccess.peterborough.gov.uk/online-applications",
    "South Cambridgeshire District Council": "https://applications.greatercambridgeplanning.org/online-applications",
    "South Norfolk Council":               "https://planning.south-norfolk.gov.uk/online-applications",
    "Stevenage Borough Council":           "https://publicaccess.stevenage.gov.uk/online-applications",
    # ── East Midlands ─────────────────────────────────────────────────────────
    "Leicester City Council":              "https://planning.leicester.gov.uk/online-applications",
    "Nottingham City Council":             "https://publicaccess.nottinghamcity.gov.uk/online-applications",
    "Derby City Council":                  "https://eplanning.derby.gov.uk/online-applications",
    # ── West Midlands ─────────────────────────────────────────────────────────
    "Birmingham City Council":             "https://publicaccess.birmingham.gov.uk/online-applications",
    "Coventry City Council":               "https://planningapps.coventry.gov.uk/online-applications",
    "Wolverhampton City Council":          "https://planning.wolverhampton.gov.uk/online-applications",
    "Stoke-on-Trent City Council":         "https://planning.stoke.gov.uk/online-applications",
    "Walsall Council":                     "https://planningapps.walsall.gov.uk/online-applications",
    # ── Yorkshire and the Humber ──────────────────────────────────────────────
    "Bradford Council":                    "https://publicaccess.bradford.gov.uk/online-applications",
    "Calderdale Metropolitan Borough Council": "https://publicaccess.calderdale.gov.uk/online-applications",
    "Kingston upon Hull City Council":     "https://planning.hull.gov.uk/online-applications",
    "Leeds City Council":                  "https://publicaccess.leeds.gov.uk/online-applications",
    "Sheffield City Council":              "https://planningapps.sheffield.gov.uk/online-applications",
    "Wakefield Council":                   "https://publicaccess.wakefield.gov.uk/online-applications",
    # ── North West ────────────────────────────────────────────────────────────
    "Liverpool City Council":              "https://planning.liverpool.gov.uk/online-applications",
    "Manchester City Council":             "https://pa.manchester.gov.uk/online-applications",
    "Salford City Council":                "https://publicaccess.salford.gov.uk/online-applications",
    "Stockport Metropolitan Borough Council": "https://publicaccess.stockport.gov.uk/online-applications",
    "Wirral Council":                      "https://planapp.wirral.gov.uk/online-applications",
    # ── North East ────────────────────────────────────────────────────────────
    "Gateshead Council":                   "https://publicaccess.gateshead.gov.uk/online-applications",
    "Newcastle City Council":              "https://publicaccess.newcastle.gov.uk/online-applications",
    "North Tyneside Council":              "https://publicaccess.northtyneside.gov.uk/online-applications",
    "Sunderland City Council":             "https://planningapps.sunderland.gov.uk/online-applications",
    # ── Eastern England ───────────────────────────────────────────────────────
    "St Albans City and District Council": "https://publicaccess.stalbans.gov.uk/online-applications",
    "Spelthorne Borough Council":          "https://publicaccess.spelthorne.gov.uk/online-applications",
    # Eastern England (additional)
    "Broxbourne Borough Council":          "https://publicaccess.broxbourne.gov.uk/online-applications",
    "Hertsmere Borough Council":           "https://publicaccess.hertsmere.gov.uk/online-applications",
    "Three Rivers District Council":       "https://publicaccess.threerivers.gov.uk/online-applications",
    "Watford Borough Council":             "https://publicaccess.watford.gov.uk/online-applications",
    "Welwyn Hatfield Borough Council":     "https://publicaccess.welhat.gov.uk/online-applications",
    "East Hertfordshire District Council": "https://publicaccess.eastherts.gov.uk/online-applications",
    "North Hertfordshire District Council": "https://publicaccess.north-herts.gov.uk/online-applications",
    "Dacorum Borough Council":             "https://publicaccess.dacorum.gov.uk/online-applications",
    "Norwich City Council":                "https://planning.norwich.gov.uk/online-applications",
    "Broadland District Council":          "https://planning.broadland.gov.uk/online-applications",
    "Breckland District Council":          "https://publicaccess.breckland.gov.uk/online-applications",
    "Borough of King's Lynn and West Norfolk": "https://publicaccess.west-norfolk.gov.uk/online-applications",
    "Ipswich Borough Council":             "https://publicaccess.ipswich.gov.uk/online-applications",
    "Babergh District Council":            "https://publicaccess.babergh.gov.uk/online-applications",
    "Mid Suffolk District Council":        "https://publicaccess.midsuffolk.gov.uk/online-applications",
    "East Suffolk Council":                "https://publicaccess.eastsuffolk.gov.uk/online-applications",
    "Braintree District Council":          "https://publicaccess.braintree.gov.uk/online-applications",
    "Chelmsford City Council":             "https://publicaccess.chelmsford.gov.uk/online-applications",
    "Epping Forest District Council":      "https://eplanning.eppingforestdc.gov.uk/online-applications",
    "Harlow District Council":             "https://publicaccess.harlow.gov.uk/online-applications",
    "Uttlesford District Council":         "https://publicaccess.uttlesford.gov.uk/online-applications",
    "Basildon Borough Council":            "https://planning.basildon.gov.uk/online-applications",
    "Brentwood Borough Council":           "https://publicaccess.brentwood.gov.uk/online-applications",
    "Castle Point Borough Council":        "https://publicaccess.castlepoint.gov.uk/online-applications",
    "Maldon District Council":             "https://publicaccess.maldon.gov.uk/online-applications",
    "Rochford District Council":           "https://publicaccess.rochford.gov.uk/online-applications",
    # South East (additional)
    "Basingstoke and Deane Borough Council": "https://planning.basingstoke.gov.uk/online-applications",
    "Fareham Borough Council":             "https://www.fareham.gov.uk/online-applications",
    "Hart District Council":               "https://publicaccess.hart.gov.uk/online-applications",
    "Isle of Wight Council":               "https://planning.iow.gov.uk/online-applications",
    "New Forest District Council":         "https://publicaccess.newforest.gov.uk/online-applications",
    "Rushmoor Borough Council":            "https://planning.rushmoor.gov.uk/online-applications",
    "Test Valley Borough Council":         "https://publicaccess.testvalley.gov.uk/online-applications",
    "Winchester City Council":             "https://publicaccess.winchester.gov.uk/online-applications",
    "Woking Borough Council":              "https://publicaccess.woking.gov.uk/online-applications",
    "Surrey Heath Borough Council":        "https://publicaccess.surreyheath.gov.uk/online-applications",
    "Runnymede Borough Council":           "https://publicaccess.runnymede.gov.uk/online-applications",
    "Elmbridge Borough Council":           "https://publicaccess.elmbridge.gov.uk/online-applications",
    "Mole Valley District Council":        "https://publicaccess.molevalley.gov.uk/online-applications",
    "Tandridge District Council":          "https://publicaccess.tandridge.gov.uk/online-applications",
    "Waverley Borough Council":            "https://publicaccess.waverley.gov.uk/online-applications",
    "Wealden District Council":            "https://publicaccess.wealden.gov.uk/online-applications",
    "Arun District Council":               "https://planning.arun.gov.uk/online-applications",
    "Lewes District Council":              "https://planningpublicaccess.lewes-eastbourne.gov.uk/online-applications",
    "Sevenoaks District Council":          "https://pa.sevenoaks.gov.uk/online-applications",
    "Tunbridge Wells Borough Council":     "https://publicaccess.tunbridgewells.gov.uk/online-applications",
    "Gravesham Borough Council":           "https://planning.gravesham.gov.uk/online-applications",
    "Dartford Borough Council":            "https://planning.dartford.gov.uk/online-applications",
    "Folkestone and Hythe District Council": "https://planning.folkestone-hythe.gov.uk/online-applications",
    "Reigate and Banstead Borough Council": "https://publicaccess.reigate-banstead.gov.uk/online-applications",
    "Crawley Borough Council":             "https://pa.crawley.gov.uk/online-applications",
    "Horsham District Council":            "https://publicaccess.horsham.gov.uk/online-applications",
    "Mid Sussex District Council":         "https://publicaccess.midsussex.gov.uk/online-applications",
    "Gosport Borough Council":             "https://publicaccess.gosport.gov.uk/online-applications",
    "Havant Borough Council":              "https://planningpublicaccess.havant.gov.uk/online-applications",
    "Eastleigh Borough Council":           "https://planning.eastleigh.gov.uk/online-applications",
    "Ashford Borough Council":             "https://planningapps.ashford.gov.uk/online-applications",
    # Berkshire unitaries
    "Slough Borough Council":              "https://planningapps.slough.gov.uk/online-applications",
    "Windsor and Maidenhead Borough Council": "https://publicaccess.rbwm.gov.uk/online-applications",
    "Wokingham Borough Council":           "https://publicaccess.wokingham.gov.uk/online-applications",
    "West Berkshire Council":              "https://publicaccess.westberks.gov.uk/online-applications",
    "Bracknell Forest Borough Council":    "https://planningapps.bracknell-forest.gov.uk/online-applications",
    # Oxfordshire
    "Cherwell District Council":           "https://pa.cherwell.gov.uk/online-applications",
    "South Oxfordshire District Council":  "https://www.southandvale.gov.uk/online-applications",
    "Vale of White Horse District Council": "https://www.southandvale.gov.uk/online-applications",
    "West Oxfordshire District Council":   "https://publicaccess.westoxon.gov.uk/online-applications",
    # South West (additional)
    "Bath and North East Somerset Council": "https://planningonline.bathnes.gov.uk/online-applications",
    "Wiltshire Council":                   "https://planning.wiltshire.gov.uk/online-applications",
    "Swindon Borough Council":             "https://pa.swindon.gov.uk/online-applications",
    "South Gloucestershire Council":       "https://developments.southglos.gov.uk/online-applications",
    "North Somerset Council":              "https://planning.n-somerset.gov.uk/online-applications",
    "Gloucester City Council":             "https://publicaccess.gloucester.gov.uk/online-applications",
    "Cheltenham Borough Council":          "https://planning.cheltenham.gov.uk/online-applications",
    "Stroud District Council":             "https://planning.stroud.gov.uk/online-applications",
    "Cotswold District Council":           "https://publicaccess.cotswold.gov.uk/online-applications",
    "Forest of Dean District Council":     "https://publicaccess.fdean.gov.uk/online-applications",
    "Shropshire Council":                  "https://publicaccess.shropshire.gov.uk/online-applications",
    "Telford and Wrekin Council":          "https://planningapplications.telford.gov.uk/online-applications",
    "Herefordshire Council":               "https://planning.herefordshire.gov.uk/online-applications",
    "Bournemouth Christchurch and Poole Council": "https://planning.bcpcouncil.gov.uk/online-applications",
    "Dorset Council":                      "https://planning.dorset.gov.uk/online-applications",
    "Mendip District Council":             "https://publicaccess.mendip.gov.uk/online-applications",
    "Somerset West and Taunton Council":   "https://publicaccess.somersetwestandtaunton.gov.uk/online-applications",
    "Somerset Council":                    "https://www.somersetcouncil.gov.uk/online-applications",
    "Cornwall Council":                    "https://planning.cornwall.gov.uk/online-applications",
    "Westmorland and Furness Council":     "https://planning.westmorlandandfurness.gov.uk/online-applications",
    "Cumberland Council":                  "https://planning.cumberland.gov.uk/online-applications",
    "Teignbridge District Council":        "https://www.teignbridge.gov.uk/planning",
    "Torridge District Council":           "https://publicaccess.torridge.gov.uk/online-applications",
    "Mid Devon District Council":          "https://publicaccess.middevon.gov.uk/online-applications",
    "East Devon District Council":         "https://eastdevon.gov.uk/planning/planning-applications",
    "Tewkesbury Borough Council":          "https://publicaccess.tewkesbury.gov.uk/online-applications",
    # East Midlands (additional)
    "Charnwood Borough Council":           "https://publicaccess.charnwood.gov.uk/online-applications",
    "Harborough District Council":         "https://publicaccess.harborough.gov.uk/online-applications",
    "Blaby District Council":              "https://publicaccess.blaby.gov.uk/online-applications",
    "Hinckley and Bosworth Borough Council": "https://publicaccess.hinckley-bosworth.gov.uk/online-applications",
    "Melton Borough Council":              "https://publicaccess.melton.gov.uk/online-applications",
    "North West Leicestershire District Council": "https://publicaccess.nwleicestershire.gov.uk/online-applications",
    "Oadby and Wigston Borough Council":   "https://publicaccess.oadby-wigston.gov.uk/online-applications",
    "Rushcliffe Borough Council":          "https://publicaccess.rushcliffe.gov.uk/online-applications",
    "Broxtowe Borough Council":            "https://planning.broxtowe.gov.uk/online-applications",
    "Gedling Borough Council":             "https://publicaccess.gedling.gov.uk/online-applications",
    "Ashfield District Council":           "https://publicaccess.ashfield.gov.uk/online-applications",
    "Mansfield District Council":          "https://publicaccess.mansfield.gov.uk/online-applications",
    "Newark and Sherwood District Council": "https://publicaccess.newark-sherwooddc.gov.uk/online-applications",
    "South Derbyshire District Council":   "https://www.south-derbys.gov.uk/online-applications",
    "Erewash Borough Council":             "https://idoxpa.erewash.gov.uk/online-applications",
    "Amber Valley Borough Council":        "https://publicaccess.ambervalley.gov.uk/online-applications",
    "Bolsover District Council":           "https://publicaccess.bolsover.gov.uk/online-applications",
    "North East Derbyshire District Council": "https://publicaccess.ne-derbyshire.gov.uk/online-applications",
    "Chesterfield Borough Council":        "https://publicaccess.chesterfield.gov.uk/online-applications",
    "Derbyshire Dales District Council":   "https://publicaccess.derbyshiredales.gov.uk/online-applications",
    "High Peak Borough Council":           "https://publicaccess.highpeak.gov.uk/online-applications",
    "West Northamptonshire Council":       "https://wnc.planning-register.co.uk/online-applications",
    "North Northamptonshire Council":      "https://nnc.planning-register.co.uk/online-applications",
    # Lincolnshire
    "Boston Borough Council":              "https://publicaccess.boston.gov.uk/online-applications",
    "East Lindsey District Council":       "https://publicaccess.e-lindsey.gov.uk/online-applications",
    "Lincoln City Council":                "https://planningapps.lincoln.gov.uk/online-applications",
    "North Kesteven District Council":     "https://publicaccess.n-kesteven.gov.uk/online-applications",
    "South Holland District Council":      "https://publicaccess.sholland.gov.uk/online-applications",
    "South Kesteven District Council":     "https://publicaccess.southkesteven.gov.uk/online-applications",
    "West Lindsey District Council":       "https://publicaccess.west-lindsey.gov.uk/online-applications",
    # Bedfordshire
    "Bedford Borough Council":             "https://publicaccess.bedford.gov.uk/online-applications",
    "Central Bedfordshire Council":        "https://www.centralbedfordshire.gov.uk/online-applications",
    "Luton Borough Council":               "https://publicaccess.luton.gov.uk/online-applications",
    # Essex
    "Tendring District Council":           "https://publicaccess.tendringdc.gov.uk/online-applications",
    "Thurrock Council":                    "https://publicaccess.thurrock.gov.uk/online-applications",
    "Southend-on-Sea City Council":        "https://publicaccess.southend.gov.uk/online-applications",
    "Fenland District Council":            "https://publicaccess.fenland.gov.uk/online-applications",
    # West Midlands (additional)
    "Stafford Borough Council":            "https://planning.staffordbc.gov.uk/online-applications",
    "Lichfield District Council":          "https://publicaccess.lichfielddc.gov.uk/online-applications",
    "Cannock Chase District Council":      "https://publicaccess.cannockchasedc.gov.uk/online-applications",
    "Tamworth Borough Council":            "https://publicaccess.tamworth.gov.uk/online-applications",
    "South Staffordshire Council":         "https://publicaccess.sstaffs.gov.uk/online-applications",
    "East Staffordshire Borough Council":  "https://publicaccess.eaststaffordshire.gov.uk/online-applications",
    "Staffordshire Moorlands District Council": "https://publicaccess.staffsmoorlands.gov.uk/online-applications",
    "North Warwickshire Borough Council":  "https://planning.northwarks.gov.uk/online-applications",
    "Rugby Borough Council":               "https://planning.rugby.gov.uk/online-applications",
    "Stratford-on-Avon District Council":  "https://publicaccess.stratford.gov.uk/online-applications",
    "Warwick District Council":            "https://planning.warwickdc.gov.uk/online-applications",
    "Nuneaton and Bedworth Borough Council": "https://publicaccess.nuneatonandbedworth.gov.uk/online-applications",
    "Solihull Metropolitan Borough Council": "https://publicaccess.solihull.gov.uk/online-applications",
    "Bromsgrove District Council":         "https://publicaccess.bromsgrove.gov.uk/online-applications",
    "Redditch Borough Council":            "https://publicaccess.redditch.gov.uk/online-applications",
    "Malvern Hills District Council":      "https://publicaccess.malvernhills.gov.uk/online-applications",
    "Worcester City Council":              "https://publicaccess.worcester.gov.uk/online-applications",
    "Wychavon District Council":           "https://publicaccess.wychavon.gov.uk/online-applications",
    "Wyre Forest District Council":        "https://planning.wyreforest.gov.uk/online-applications",
    "Newcastle-under-Lyme Borough Council": "https://publicaccess.newcastle-staffs.gov.uk/online-applications",
    "Dudley Metropolitan Borough Council": "https://www.dudley.gov.uk/planning",
    "Sandwell Metropolitan Borough Council": "https://publicaccess.sandwell.gov.uk/online-applications",
    # Yorkshire (additional)
    "Barnsley Metropolitan Borough Council": "https://publicaccess.barnsley.gov.uk/online-applications",
    "Rotherham Metropolitan Borough Council": "https://publicaccess.rotherham.gov.uk/online-applications",
    "Kirklees Council":                    "https://publicaccess.kirklees.gov.uk/online-applications",
    "City of York Council":                "https://publicaccess.york.gov.uk/online-applications",
    "East Riding of Yorkshire Council":    "https://newplanningaccess.eastriding.gov.uk/online-applications",
    "North Yorkshire Council":             "https://publicaccess.northyorks.gov.uk/online-applications",
    "Scarborough Borough Council":         "https://publicaccess.scarborough.gov.uk/online-applications",
    "Harrogate Borough Council":           "https://publicaccess.harrogate.gov.uk/online-applications",
    "Ryedale District Council":            "https://publicaccess.ryedale.gov.uk/online-applications",
    "Hambleton District Council":          "https://publicaccess.hambleton.gov.uk/online-applications",
    "Selby District Council":              "https://publicaccess.selby.gov.uk/online-applications",
    "Craven District Council":             "https://publicaccess.cravendc.gov.uk/online-applications",
    "Richmondshire District Council":      "https://publicaccess.richmondshire.gov.uk/online-applications",
    # North West (additional)
    "Bury Metropolitan Borough Council":   "https://publicaccess.bury.gov.uk/online-applications",
    "Rochdale Metropolitan Borough Council": "https://publicaccess.rochdale.gov.uk/online-applications",
    "Tameside Metropolitan Borough Council": "https://publicaccess.tameside.gov.uk/online-applications",
    "Trafford Metropolitan Borough Council": "https://planning.trafford.gov.uk/online-applications",
    "Oldham Metropolitan Borough Council": "https://publicaccess.oldham.gov.uk/online-applications",
    "Bolton Metropolitan Borough Council": "https://publicaccess.bolton.gov.uk/online-applications",
    "Wigan Metropolitan Borough Council":  "https://publicaccess.wigan.gov.uk/online-applications",
    "St Helens Metropolitan Borough Council": "https://publicaccess.sthelens.gov.uk/online-applications",
    "Halton Borough Council":              "https://planning2.halton.gov.uk/online-applications",
    "Warrington Borough Council":          "https://planning.warrington.gov.uk/online-applications",
    "Cheshire East Council":               "https://publicaccess.cheshireeast.gov.uk/online-applications",
    "Cheshire West and Chester Council":   "https://publicaccess.cheshirewestandchester.gov.uk/online-applications",
    "Blackburn with Darwen Borough Council": "https://planning.blackburn.gov.uk/online-applications",
    "Blackpool Council":                   "https://planning.blackpool.gov.uk/online-applications",
    "Preston City Council":                "https://planning.preston.gov.uk/online-applications",
    "Lancaster City Council":              "https://planning.lancaster.gov.uk/online-applications",
    "Ribble Valley Borough Council":       "https://publicaccess.ribblevalley.gov.uk/online-applications",
    "South Ribble Borough Council":        "https://publicaccess.southribble.gov.uk/online-applications",
    "Burnley Borough Council":             "https://planning.burnley.gov.uk/online-applications",
    "Pendle Borough Council":              "https://planning.pendle.gov.uk/online-applications",
    "Rossendale Borough Council":          "https://publicaccess.rossendale.gov.uk/online-applications",
    "Hyndburn Borough Council":            "https://publicaccess.hyndburn.gov.uk/online-applications",
    "Chorley Council":                     "https://planning.chorley.gov.uk/online-applications",
    "West Lancashire Borough Council":     "https://publicaccess.westlancs.gov.uk/online-applications",
    "Fylde Borough Council":               "https://publicaccess.fylde.gov.uk/online-applications",
    "Wyre Council":                        "https://planning.wyre.gov.uk/online-applications",
    "Milton Keynes City Council":          "https://publicaccess.milton-keynes.gov.uk/online-applications",
    "Buckinghamshire Council":             "https://publicaccess.buckinghamshire.gov.uk/online-applications",
    "London Borough of Barking and Dagenham": "https://planning.lbbd.gov.uk/online-applications",
    "Sefton Metropolitan Borough Council": "https://pa.sefton.gov.uk/online-applications",
    "Knowsley Metropolitan Borough Council": "https://publicaccess.knowsley.gov.uk/online-applications",
    # North East (additional)
    "Durham County Council":               "https://publicaccess.durham.gov.uk/online-applications",
    "South Tyneside Council":              "https://planning.southtyneside.gov.uk/online-applications",
    "Middlesbrough Council":               "https://planning.middlesbrough.gov.uk/online-applications",
    "Stockton-on-Tees Borough Council":    "https://publicaccess.stockton.gov.uk/online-applications",
    "Darlington Borough Council":          "https://publicaccess.darlington.gov.uk/online-applications",
    "Hartlepool Borough Council":          "https://publicaccess.hartlepool.gov.uk/online-applications",
    "Redcar and Cleveland Borough Council": "https://planning.redcar-cleveland.gov.uk/online-applications",
    # London (additional)
    "London Borough of Brent":             "https://pa.brent.gov.uk/online-applications",
    "London Borough of Bexley":            "https://pa.bexley.gov.uk/online-applications",
    "London Borough of Bromley":           "https://searchapplications.bromley.gov.uk/online-applications",
    "London Borough of Enfield":           "https://planningandbuildingcontrol.enfield.gov.uk/online-applications",
    "London Borough of Hillingdon":        "https://lpa.hillingdon.gov.uk/online-applications",
    "London Borough of Hounslow":          "https://planning.hounslow.gov.uk/online-applications",
    "London Borough of Kingston upon Thames": "https://publicaccess.kingston.gov.uk/online-applications",
    "London Borough of Newham":            "https://planning.newham.gov.uk/online-applications",
    "London Borough of Redbridge":         "https://planning.redbridge.gov.uk/online-applications",
    "London Borough of Richmond upon Thames": "https://www2.richmond.gov.uk/planningpublicaccess/online-applications",
    "London Borough of Croydon":           "https://publicaccess.croydon.gov.uk/online-applications",
    "Royal Borough of Kensington and Chelsea": "https://publicaccess.rbkc.gov.uk/online-applications",
    "City of Westminster":                 "https://idoxpa.westminster.gov.uk/online-applications",
}

# ─── In-memory failure cache ──────────────────────────────────────────────────
# Track portals that recently failed so we don't retry them on every request.
# Format: { base_url → timestamp_of_failure }
import time as _time
_failure_cache: dict[str, float] = {}
_FAILURE_TTL = 300  # 5 minutes before retrying a failed portal

# ─── Scraper ──────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _is_failed(base_url: str) -> bool:
    t = _failure_cache.get(base_url)
    return t is not None and (_time.time() - t) < _FAILURE_TTL


def _mark_failed(base_url: str) -> None:
    _failure_cache[base_url] = _time.time()


def _extract_ref_from_meta(meta_text: str) -> str:
    """Pull application reference from 'Ref. No: 24/01234/FUL·Received:...' text."""
    m = re.search(r"Ref\.\s*No[.:]?\s*([\w/\-]+)", meta_text, re.I)
    return m.group(1).strip() if m else ""


def _extract_date_from_meta(meta_text: str) -> str:
    """Pull validated/decision date from metaInfo text.
    Handles formats: 'Mon 29 Dec 2025', '29/12/2025', '29-12-2025'.
    """
    # "Mon 29 Dec 2025" style (IDOX modern)
    _date_pat = r"(\w{3}\s+\d{1,2}\s+\w{3,9}\s+\d{4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})"
    m = re.search(r"Validated:\s*" + _date_pat, meta_text)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"Received:\s*" + _date_pat, meta_text)
    return m2.group(1).strip() if m2 else ""


def _parse_status(li: BeautifulSoup) -> str:
    """Extract decision/status from an IDOX result <li>.

    IDOX has several HTML layouts across portal versions:
    - Modern: <div class="badge-status"><div class="value">Approved</div></div>
    - Modern v2: <div class="badge-status"><span>Approved</span></div>
    - Legacy: <span class="status">Approved</span> or <p class="status">
    - Table layout: <td class="Status">Approved</td>
    - Meta paragraph: 'Status: Approved' buried in metaInfo text
    """
    # Modern IDOX: badge-status div with value child
    badge = li.find("div", class_="badge-status")
    if badge:
        for tag in ("div", "span", "p"):
            val = badge.find(tag, class_="value")
            if val:
                return val.get_text(strip=True)
        # No .value child — try the badge text directly
        text = badge.get_text(strip=True)
        if text:
            return text

    # Older / alternate IDOX layouts
    for cls in ("status", "caseStatus", "decision", "Decision", "Status", "CaseStatus"):
        el = li.find(class_=cls)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t

    # Table-based IDOX portals
    for td in li.find_all("td"):
        if td.get("class") and any("status" in c.lower() or "decision" in c.lower()
                                   for c in td.get("class", [])):
            t = td.get_text(strip=True)
            if t:
                return t

    # Last resort: scan metaInfo for "Status:" or "Decision:" label
    meta = li.find(class_="metaInfo") or li.find("p", class_="metaInfo")
    if meta:
        for label in ("Status", "Decision", "Case Status"):
            m = re.search(rf"{label}:\s*([^\|·\n]+)", meta.get_text(" ", strip=True), re.I)
            if m:
                return m.group(1).strip()
    return ""


def _normalise_status(raw: str) -> str:
    """Map raw IDOX status string to our standard decision values."""
    s = raw.lower().strip()
    if not s:
        return "Unknown"
    if any(w in s for w in ("approved", "approve", "grant", "permit", "no objection")):
        return "Approved"
    if any(w in s for w in ("refused", "refuse", "reject", "not permit")):
        return "Refused"
    if any(w in s for w in ("withdrawn", "withdraw")):
        return "Withdrawn"
    if any(w in s for w in ("decided", "determined", "closed", "appeal", "split decision")):
        return "Decided"
    if any(w in s for w in ("pending", "received", "registered", "under consideration",
                             "awaiting", "validation", "no decision", "not yet")):
        return "Pending"
    return raw


def _parse_idox_results(html: str, base_url: str, council_name: str) -> list[dict]:
    """Parse IDOX results HTML into our standard result shape."""
    parsed_url = urlparse(base_url)
    site_root = f"{parsed_url.scheme}://{parsed_url.netloc}"
    soup = BeautifulSoup(html, "lxml")

    # Find the results container
    lis = soup.find_all("li", class_="searchresult")
    results = []

    for li in lis:
        # Proposal / description — the summary link text is the proposal
        summary_a = li.find("a", class_="summaryLink") or li.find("a", href=True)
        if not summary_a:
            continue
        proposal = (
            summary_a.find("div", class_="summaryLinkTextClamp") or summary_a
        ).get_text(strip=True)
        if not proposal:
            continue

        href = summary_a.get("href", "")
        source_url = (site_root + href) if href.startswith("/") else href or None

        # Address
        addr_el = li.find("p", class_="address") or li.find(class_="Address")
        address = addr_el.get_text(strip=True) if addr_el else ""

        # Reference + date from metaInfo
        meta_el = li.find("p", class_="metaInfo") or li.find(class_="metaInfo")
        meta_text = meta_el.get_text(" ", strip=True) if meta_el else ""
        reference = _extract_ref_from_meta(meta_text)
        validated = _extract_date_from_meta(meta_text)

        # Status / decision
        raw_status = _parse_status(li)
        decision = _normalise_status(raw_status)

        results.append({
            "id": f"idox-{council_name}-{reference}" if reference else None,
            "reference": reference,
            "address": address,
            "description": proposal,
            "decision": decision,
            "decision_raw": raw_status,
            "decision_date": validated,
            "council": council_name,
            "is_user_lpa": True,  # IDOX results are always for the user's council
            "application_type": "",
            "application_type_raw": "",
            "source_name": f"{council_name} Planning Portal (IDOX)",
            "source_label": council_name,
            "source_url": source_url,
            "data_freshness": f"Live from {council_name}'s planning portal — applications from last 2 years",
        })

    return results


def idox_search(
    council_name: str,
    base_url: str,
    keyword: str,
    timeout: int = 10,
) -> list[dict]:
    """
    Search one IDOX portal for applications matching the keyword.
    Returns a list of result dicts in our standard shape, or empty list on failure.
    The keyword is searched across description, address, and reference.
    Date range: last 2 years to limit results.
    """
    if _is_failed(base_url):
        return []

    # Rolling 2-year date window
    from datetime import date, timedelta
    today = date.today()
    two_years_ago = today - timedelta(days=730)
    date_from = two_years_ago.strftime("%d/%m/%Y")
    date_to = today.strftime("%d/%m/%Y")

    try:
        s = requests.Session()
        s.verify = False
        s.headers.update(_HEADERS)

        # Step 1: GET the search form
        r = s.get(f"{base_url}/search.do", params={"action": "simple"}, timeout=timeout)
        if not r.ok:
            log.debug(f"[idox] {council_name} GET {r.status_code}")
            _mark_failed(base_url)
            return []

        soup = BeautifulSoup(r.text, "lxml")
        form = soup.find("form")
        if not form:
            _mark_failed(base_url)
            return []

        # Extract all hidden fields (including CSRF)
        hidden = {
            i["name"]: i.get("value", "")
            for i in form.find_all("input", {"type": "hidden"})
            if i.get("name")
        }

        # Resolve the POST URL — form action is usually an absolute path like
        # "/online-applications/simpleSearchResults.do?action=firstPage"
        form_action = form.get("action", "")
        parsed = urlparse(base_url)
        site_root = f"{parsed.scheme}://{parsed.netloc}"
        if form_action.startswith("/"):
            post_url = site_root + form_action
        elif form_action.startswith("http"):
            post_url = form_action
        else:
            post_url = f"{base_url}/{form_action.lstrip('/')}"

        # Detect keyword field name (modern vs. legacy IDOX)
        uses_simple = "simpleSearchString" in r.text or "simpleSearch" in r.text
        keyword_field = (
            "searchCriteria.simpleSearchString" if uses_simple
            else "searchCriteria.description"
        )

        # Step 2: POST search — filter to decided applications with date range
        post_data = {
            **hidden,
            keyword_field: keyword,
            "searchType": "Application",
            "searchCriteria.caseStatus": "Decided",
            "date(applicationValidatedStart)": date_from,
            "date(applicationValidatedEnd)": date_to,
            "searchCriteria.dateType": "applicationDate",
            "searchCriteria.dateRangeType": "custom",
        }
        if uses_simple:
            post_data["searchCriteria.simpleSearch"] = "true"

        s.headers["Referer"] = f"{base_url}/search.do?action=simple"
        s.headers["Content-Type"] = "application/x-www-form-urlencoded"

        r2 = s.post(post_url, data=post_data, timeout=timeout + 5, allow_redirects=True)
        if not r2.ok:
            log.debug(f"[idox] {council_name} POST {r2.status_code}")
            _mark_failed(base_url)
            return []

        # If "too many results" — progressively narrow the date window.
        # Re-fetch the search form each time to get a fresh CSRF token (some portals
        # invalidate the token after the first POST).
        for _days_back in (365, 180, 90, 30, 14):
            if "too many results" not in r2.text.lower():
                break
            # Refresh CSRF token
            r_fresh = s.get(f"{base_url}/search.do", params={"action": "simple"}, timeout=timeout)
            if r_fresh.ok:
                soup_fresh = BeautifulSoup(r_fresh.text, "lxml")
                form_fresh = soup_fresh.find("form")
                if form_fresh:
                    fresh_hidden = {
                        i["name"]: i.get("value", "")
                        for i in form_fresh.find_all("input", {"type": "hidden"})
                        if i.get("name")
                    }
                    post_data.update(fresh_hidden)
            cutoff = today - timedelta(days=_days_back)
            post_data["date(applicationValidatedStart)"] = cutoff.strftime("%d/%m/%Y")
            r2 = s.post(post_url, data=post_data, timeout=timeout + 5, allow_redirects=True)
            if not r2.ok:
                _mark_failed(base_url)
                return []
        if "too many results" in r2.text.lower():
            log.debug(f"[idox] {council_name} still too many results after 14-day window")
            return []

        def _is_bounce_back(html: str) -> bool:
            """True only when the portal rejected the search and returned the empty form.
            Does NOT fire on legitimate "0 results found" or "too many results" pages.
            """
            s2 = BeautifulSoup(html, "lxml")
            body_text = s2.get_text(" ", strip=True).lower()

            # Explicit "0 results" or "too many results" → legitimate portal response, not a bounce-back
            if re.search(r"(no results|0 result|your search (has )?returned no|no applications found|too many results)", body_text):
                return False

            # h1/h2 is the search form heading (search was not executed)
            h = s2.find("h1") or s2.find("h2")
            heading = h.get_text(strip=True).lower() if h else ""
            if any(phrase in heading for phrase in
                   ("simple search", "search applications", "search for planning applications")):
                # But only a bounce-back if the form keyword field is empty
                kw_input = (
                    s2.find("input", {"name": "searchCriteria.simpleSearchString"}) or
                    s2.find("input", {"name": "searchCriteria.description"})
                )
                if kw_input and not (kw_input.get("value") or "").strip():
                    return True

            return False

        if _is_bounce_back(r2.text):
            # Fallback 1: drop caseStatus filter (some portals don't accept 'Decided')
            pd2 = {k: v for k, v in post_data.items() if k != "searchCriteria.caseStatus"}
            r2 = s.post(post_url, data=pd2, timeout=timeout + 5, allow_redirects=True)

        if r2.ok and _is_bounce_back(r2.text):
            # Fallback 2: drop date range entirely, just keyword search
            pd3 = {
                k: v for k, v in post_data.items()
                if k not in ("date(applicationValidatedStart)", "date(applicationValidatedEnd)",
                             "searchCriteria.dateType", "searchCriteria.dateRangeType",
                             "searchCriteria.caseStatus")
            }
            r2 = s.post(post_url, data=pd3, timeout=timeout + 5, allow_redirects=True)

        if not r2.ok or _is_bounce_back(r2.text):
            log.debug(f"[idox] {council_name} bounced back after all fallbacks")
            return []

        results = _parse_idox_results(r2.text, base_url, council_name)
        log.info(f"[idox] {council_name}: {len(results)} results for keyword={keyword!r}")
        return results[:20]  # cap at 20 per portal

    except requests.exceptions.Timeout:
        log.debug(f"[idox] {council_name} timeout")
        _mark_failed(base_url)
        return []
    except requests.exceptions.ConnectionError as e:
        log.debug(f"[idox] {council_name} connection error: {e}")
        _mark_failed(base_url)
        return []
    except Exception as e:
        log.warning(f"[idox] {council_name} unexpected error: {e}")
        _mark_failed(base_url)
        return []


def find_council_portal(council_name: str) -> tuple[str, str] | None:
    """
    Find the IDOX portal for a given council name using fuzzy matching.
    Returns (matched_key, base_url) or None.
    """
    if not council_name:
        return None
    lower = council_name.lower().strip()

    # 1. Exact match
    for key, url in IDOX_PORTALS.items():
        if key.lower() == lower:
            return key, url

    # 2. Partial match — either string contains the other
    for key, url in IDOX_PORTALS.items():
        k_lower = key.lower()
        if lower in k_lower or k_lower in lower:
            return key, url

    # 3. Significant-word overlap (strip noise words)
    _noise = {"borough", "council", "city", "district", "metropolitan", "royal",
               "london", "of", "the", "and", "&"}

    def _words(s: str) -> set[str]:
        return {w for w in re.sub(r"[,.'()]", " ", s.lower()).split() if w not in _noise}

    lower_words = _words(council_name)
    if lower_words:
        for key, url in IDOX_PORTALS.items():
            k_words = _words(key)
            if lower_words & k_words and lower_words <= k_words | lower_words:
                # At least one word overlap and all user words present in key
                if len(lower_words & k_words) >= max(1, len(lower_words) - 1):
                    return key, url

    return None


def council_has_portal(council_name: str) -> bool:
    """Quick check — used by the frontend coverage data."""
    return find_council_portal(council_name) is not None
