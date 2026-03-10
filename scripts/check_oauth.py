#!/usr/bin/env python3
"""检查 OAuth token 数据，帮助诊断 scope 问题"""
import json
import sys
sys.path.insert(0, '/app')

from app.services import redis_client as redis

def main():
    if not redis.available():
        print("Redis not available")
        return
    
    # 检查所有可能的 key
    keys_to_check = [
        'pm-bot:oauth_tokens',
        'default:oauth_tokens', 
        'feishu_oauth_tokens'
    ]
    
    for key in keys_to_check:
        raw = redis.execute('GET', key)
        if raw:
            print(f"\n=== Key: {key} ===")
            try:
                data = json.loads(raw)
                for oid, tok in data.items():
                    print(f"\nUser: {oid}")
                    print(f"  Name: {tok.get('name', 'N/A')}")
                    print(f"  Scope: '{tok.get('scope', 'MISSING')}'")
                    print(f"  All fields: {list(tok.keys())}")
            except Exception as e:
                print(f"  Parse error: {e}")
        else:
            print(f"\n=== Key: {key} - Not found ===")

if __name__ == '__main__':
    main()
