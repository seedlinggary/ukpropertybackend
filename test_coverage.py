import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from routes.property_data import _postcode_from_latlng, _fetch_epc, _fetch_sales, _fetch_planning, _fetch_flood, _fetch_crime

LOCS = [
    ('Mayfair W1',        51.5120, -0.1497),
    ('Hackney E8',        51.5432, -0.0556),
    ('Birmingham B15',    52.4720, -1.9050),
    ('Manchester M20',    53.4142, -2.2134),
    ('Leeds LS6',         53.8175, -1.5670),
    ('Bristol BS8',       51.4566, -2.6090),
    ('Cardiff CF11',      51.4910, -3.2006),
    ('Liverpool L1',      53.4012, -2.9701),
    ('Nottingham NG2',    52.9309, -1.1269),
    ('Newcastle NE1',     54.9740, -1.6140),
]

print('%-20s %-10s %-6s %-5s %-5s %-7s %-4s %-4s %-4s %-5s %-5s %-6s' % (
    'Location','Postcode','EPC','m²','Rooms','Flood','CA','A4','AONB','GB','Crime','Sales'))
print('-'*110)

for name, lat, lng in LOCS:
    pc    = _postcode_from_latlng(lat, lng)
    epc   = _fetch_epc(pc)   if pc else {'found': False}
    sales = _fetch_sales(pc) if pc else {'found': False}
    flood = _fetch_flood(lat, lng)
    plan  = _fetch_planning(lat, lng)
    crime = _fetch_crime(lat, lng)

    rec       = (epc.get('records') or [{}])[0]
    epc_s     = rec.get('currentEnergyEfficiencyBand', '?') if epc.get('found') else 'none'
    area_s    = str(rec.get('totalFloorArea') or rec.get('total-floor-area') or '-') if epc.get('found') else '-'
    rooms_s   = str(rec.get('numberHabitableRooms') or rec.get('number-habitable-rooms') or '-') if epc.get('found') else '-'
    flood_s   = 'Z%d' % flood.get('zone', 1)
    ca_s      = 'Y' if plan.get('conservation_area',{}).get('found') else 'n'
    a4_s      = 'Y' if plan.get('article4',{}).get('found') else 'n'
    aonb_s    = 'Y' if plan.get('aonb',{}).get('found') else 'n'
    gb_s      = 'Y' if plan.get('green_belt',{}).get('found') else 'n'
    crime_s   = str(crime.get('total', '?')) if crime.get('found') else 'err'
    sales_s   = str(len(sales.get('sales',[]))) if sales.get('found') else '0'

    print('%-20s %-10s %-6s %-5s %-5s %-7s %-4s %-4s %-4s %-5s %-5s %-6s' % (
        name, pc or '?', epc_s, area_s, rooms_s, flood_s, ca_s, a4_s, aonb_s, gb_s, crime_s, sales_s))

    # Print top 3 crime categories
    cats = crime.get('categories', [])[:3]
    if cats:
        print('  Crime top 3:', ', '.join(f"{c['label']} ({c['count']})" for c in cats))
    print()
