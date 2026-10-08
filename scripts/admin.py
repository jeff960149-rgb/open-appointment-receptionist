"""Run authenticated admin operations using your private .env."""
import argparse
import json
import os

from dotenv import load_dotenv
import httpx


def main():
    load_dotenv()
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('state')
    ai=commands.add_parser('ai'); ai.add_argument('value',choices=['on','off'])
    cap=commands.add_parser('capacity'); cap.add_argument('day'); cap.add_argument('count',type=int)
    for name in ('cancel','callback-complete'):
        sub=commands.add_parser(name);sub.add_argument('id')
    args=parser.parse_args()
    token=os.getenv('ADMIN_TOKEN','')
    if len(token)<24:
        parser.error('ADMIN_TOKEN must be configured with at least 24 characters')
    paths={'state':'/api/admin/state','ai':'/api/admin/ai','capacity':'/api/admin/capacity'}
    path=paths.get(args.command)
    payload={}
    if args.command=='ai':payload={'enabled':args.value=='on'}
    elif args.command=='capacity':payload={'day':args.day,'capacity':args.count}
    elif args.command=='cancel':path='/api/admin/cancel/'+args.id
    elif args.command=='callback-complete':path='/api/admin/callback/'+args.id+'/complete'
    with httpx.Client(timeout=15,headers={'Authorization':'Bearer '+token}) as client:
        url=os.getenv('ADMIN_BASE_URL','http://127.0.0.1:8000').rstrip('/')+path
        response=client.get(url) if args.command=='state' else client.post(url,json=payload)
        response.raise_for_status()
        print(json.dumps(response.json(),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
