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

    # 3) Confere last_30d (deve bater com o Gerenciador) + split turbinados
    print("\n== last_30d (deve bater com o 'Últimos 30 dias' do Gerenciador) ==")
    IMPULS = ("post do instagram", "publicação do instagram", "publicacao do instagram",
              "publicação:", "publicacao:", "instagram post")
    def eh_imp(n):
        n = (n or "").lower(); return any(p in n for p in IMPULS)

    acct = graph_get(f"{AD_ACCOUNT}/insights", {
        "level": "account", "date_preset": "last_30d",
        "fields": "spend,impressions,clicks,inline_link_clicks,cpm", "limit": 10})
    if acct:
        a = acct[0]
        print(f"  spend={a.get('spend')}  impressions={a.get('impressions')}  "
              f"link_clicks={a.get('inline_link_clicks')}  cpm={a.get('cpm')}")

    camp = graph_get(f"{AD_ACCOUNT}/insights", {
        "level": "campaign", "date_preset": "last_30d",
        "fields": "spend,campaign_name", "limit": 500})
    imp_s = camp_s = 0.0; imp_n = camp_n = 0
    tops = []
    for r in camp:
        s = float(r.get("spend") or 0)
        if s <= 0: continue
        nome = r.get("campaign_name")
        tops.append((s, nome, eh_imp(nome)))
        if eh_imp(nome): imp_s += s; imp_n += 1
        else: camp_s += s; camp_n += 1
    tot = imp_s + camp_s or 1
    print(f"  TURBINADOS:  R$ {imp_s:,.2f}  ({imp_s/tot*100:.0f}%)  em {imp_n} campanhas")
    print(f"  ESTRUTURADAS: R$ {camp_s:,.2f}  ({camp_s/tot*100:.0f}%)  em {camp_n} campanhas")
    print("  Top 8 por gasto:")
    for s, nome, imp in sorted(tops, reverse=True)[:8]:
        print(f"    {'[TURBO]' if imp else '[CAMP ]'} R$ {s:>9,.2f}  {(nome or '')[:52]}")

    print("\n== Fim do diagnóstico ==")


if __name__ == "__main__":
    main()
