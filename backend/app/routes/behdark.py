"""Authenticated, bounded HTTP proxy only; no BehDark state in the main app."""
import os
import re
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from app.routes.auth import require_dashboard_access

router=APIRouter(prefix='/api/gui/behdark',tags=['behdark'],dependencies=[Depends(require_dashboard_access)])

@router.api_route('/{path:path}',methods=['GET','POST','PUT'])
async def proxy(path:str,request:Request):
    allowed={'GET':{'snapshot','report.csv','health'},'PUT':{'settings'},'POST':{'capital'}}
    if path not in allowed[request.method] and not (request.method=='POST' and re.fullmatch(r'cancel/\d+',path)):
        raise HTTPException(404)
    base=os.getenv('BEHDARK_URL')
    if not base: raise HTTPException(503,'سرویس مستقل BehDark هنوز راه‌اندازی نشده است.')
    body=await request.body()
    if len(body)>4096: raise HTTPException(413)
    try:
        async with httpx.AsyncClient(timeout=5,trust_env=False) as client:
            r=await client.request(request.method,base.rstrip('/')+'/'+path,content=body,
                                   headers={'Content-Type':'application/json'})
    except httpx.HTTPError:
        raise HTTPException(503,'سرویس BehDark در دسترس نیست؛ بخش‌های دیگر مستقل هستند.')
    headers={k:v for k,v in r.headers.items() if k in ('content-type','content-disposition')}
    return Response(content=r.content,status_code=r.status_code,headers=headers)
