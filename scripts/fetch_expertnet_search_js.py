#!/usr/bin/env python3
from pathlib import Path
import re
import requests

out=Path('florida-expertnet-js')
out.mkdir(exist_ok=True)
for name in ['search.js','global.js']:
    url=f'https://expertnet.org/scripts/{name}'
    r=requests.get(url,timeout=60,headers={'User-Agent':'Mozilla/5.0'})
    r.raise_for_status()
    (out/name).write_text(r.text,encoding='utf-8')
    print('\n###',name,'bytes',len(r.text))
    for line in r.text.splitlines():
        if re.search(r'ajax|\.load\(|getJSON|\.post\(|\.get\(|fuseaction|searchResults|dataTable|serverSide|url\s*:',line,re.I):
            print(line[:2000])
