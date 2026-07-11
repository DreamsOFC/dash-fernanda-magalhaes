#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnóstico da conta — NÃO commita nada, NÃO imprime o token.
Serve para descobrir o que a conta realmente retorna antes de reescrever a coleta:
 - quantos objetos ATIVOS existem (campanha / conjunto / anúncio)
 - objetivos das campanhas ativas
 - quais action_types de conversão aparecem nos insights (cadastros, visualização
   de página, follow, cliques etc.) e seus totais nos últimos 30 dias
"""
import json
import os
import sys
import time
import requests

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
AD_ACCOUNT = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


def graph_get(path, params, max_retries=4):
    params = dict(params); params["access_token"] = TOKEN
    url = f"{BASE}/{path}"; rows = []
    while url:
        for attempt in range(1, max_retries + 1):
            r = requests.get(url, params=params, timeout=60)
            if r.status_code in (429, 500, 502, 503) and attempt < max_retries:
                time.sleep(min(2 ** attempt, 30)); continue
            break
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} em {path}: {r.text[:500]}")
        payload = r.json()
        if "error" in payload:
            raise RuntimeError(f"Erro API: {json.dumps(payload['error'])[:500]}")
        rows.extend(payload.get("data", []))
        url = payload.get("paging", {}).get("next"); params = {}
    return rows


def conta_ativos(edge, campos):
    # effective_status=['ACTIVE'] filtra só o que está no ar
    params = {"fields": campos, "effective_status": '["ACTIVE"]', "limit": 200}
    return graph_get(f"{AD_ACCOUNT}/{edge}", params)


def main():
    if not TOKEN or not AD_ACCOUNT:
        print("ERRO: faltam META_ACCESS_TOKEN / META_AD_ACCOUNT_ID", file=sys.stderr)
        sys.exit(1)

    print(f"== Conta {AD_ACCOUNT} | Graph {GRAPH_VERSION} ==\n")

    # 1) Objetos ativos
    camps = conta_ativos("campaigns", "id,name,objective,effective_status")
    adsets = conta_ativos("adsets", "id,name,effective_status")
    ads = conta_ativos("ads", "id,name,effective_status")
    print(f"ATIVOS -> campanhas: {len(camps)} | conjuntos: {len(adsets)} | anúncios: {len(ads)}\n")
    print("Campanhas ativas e objetivos:")
    for c in camps:
        print(f"  - {c.get('name')}  [{c.get('objective')}]")
    print()

    # 2) Quais action_types aparecem (últimos 30 dias, nível conta)
    ins = graph_get(f"{AD_ACCOUNT}/insights", {
        "level": "account", "date_preset": "last_30d",
        "fields": "spend,impressions,reach,clicks,inline_link_clicks,actions,cost_per_action_type",
        "limit": 100,
    })
    print("Métricas base (30d):")
    if ins:
        row = ins[0]
        for k in ("spend", "impressions", "reach", "clicks", "inline_link_clicks"):
            print(f"  {k}: {row.get(k)}")
        print("\naction_types encontrados (nome do Meta -> total no período):")
        for a in row.get("actions", []) or []:
            print(f"  {a.get('action_type')}: {a.get('value')}")
        print("\ncost_per_action_type disponíveis:")
        for a in row.get("cost_per_action_type", []) or []:
            print(f"  {a.get('action_type')}: {a.get('value')}")
    else:
        print("  (sem dados de insights nos últimos 30 dias)")

    print("\n== Fim do diagnóstico ==")


if __name__ == "__main__":
    main()
