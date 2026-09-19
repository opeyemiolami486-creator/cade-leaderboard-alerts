import httpx
from app import normalize
r=httpx.get('https://cade.market/api/leaderboard?period=day', timeout=20)
r.raise_for_status(); rows=normalize(r.json())
assert len(rows)==10 and rows[0]['predictions'] >= rows[-1]['predictions']
print('CADE_SMOKE_OK', [(x['rank'],x['username'],x['predictions']) for x in rows])
