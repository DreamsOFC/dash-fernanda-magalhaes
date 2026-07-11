#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coleta de dados do Meta Ads -> JSONs criptografados em docs/data/.

Roda no GitHub Actions (de hora em hora e sob demanda). NÃO guarda o token
nem a senha no código: os dois vêm de variáveis de ambiente que o Actions
injeta a partir dos GitHub Secrets.

Saídas (todas criptografadas com AES-GCM):
  docs/data/diario.json     -> série diária da conta (90 dias) p/ o gráfico
  docs/data/campanhas.json  -> KPIs + tabelas (campanha/conjunto/anúncio), por período
  docs/data/meta.json       -> data/hora da última coleta

Regras de negócio:
- Só entram nas tabelas os objetos ATIVOS (effective_status = ACTIVE) que tiveram
  investimento (> 0) no período selecionado. A conta tem centenas de posts
  impulsionados antigos ainda "ativos" mas sem gasto — esses são ignorados.
- Métricas extraídas por linha: cadastros (lead), visualizações da página do site
  (landing_page_view), cliques no link (inline_link_clicks), conversas por mensagem.

Filosofia à prova de falha: se QUALQUER chamada à API falhar, o script aborta
(exit != 0) ANTES de escrever qualquer arquivo — nunca sobrescreve dados bons.
"""

import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# --------------------------------------------------------------------------
# Configuração (tudo trocável por env var — ver README p/ reaproveitar noutra conta)
# --------------------------------------------------------------------------
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
AD_ACCOUNT = os.environ.get("META_AD_ACCOUNT_ID", "act_161375005284674")
TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
PASSWORD = os.environ.get("DASH_PASSWORD", "")
TIMEZONE = os.environ.get("TZ_NAME", "America/Sao_Paulo")

# Fuso de São Paulo (UTC-3, sem horário de verão desde 2019).
SAO_PAULO = timezone(timedelta(hours=-3))

BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "data")

# Quantas linhas no máximo por nível/tabela (os maiores gastadores primeiro).
TOP_N = 50

# Campos de insights.
INSIGHT_FIELDS = ",".join([
    "spend", "impressions", "reach", "frequency", "cpm", "cpc", "ctr",
    "clicks", "inline_link_clicks", "actions",
])

# action_types relevantes (confirmados no diagnóstico da conta).
ACT_CONVERSA = "onsite_conversion.messaging_conversation_started_7d"
ACT_LEAD = "lead"                       # cadastros
ACT_PAGEVIEW = "landing_page_view"      # visualização da página do site
ACT_LINKCLICK = "link_click"


# --------------------------------------------------------------------------
# Helpers de parsing
# --------------------------------------------------------------------------
def get_action(lista, tipo):
    """Retorna o value (float) de um action_type dentro de 'actions'. Retorna None
    quando o tipo não existe — nunca estoura KeyError."""
    if not lista:
        return None
    for item in lista:
        if item.get("action_type") == tipo:
            try:
                return float(item.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def metricas(row):
    """Extrai as métricas de uma linha de insights (qualquer nível)."""
    spend = fnum(row.get("spend"))
    actions = row.get("actions")
    cadastros = int(get_action(actions, ACT_LEAD) or 0)
    pageviews = int(get_action(actions, ACT_PAGEVIEW) or 0)
    conversas = int(get_action(actions, ACT_CONVERSA) or 0)
    linkclicks = int(fnum(row.get("inline_link_clicks")) or (get_action(actions, ACT_LINKCLICK) or 0))
    return {
        "spend": round(spend, 2),
        "cadastros": cadastros,
        "pageViews": pageviews,
        "conversas": conversas,
        "linkClicks": linkclicks,
        "cpm": round(fnum(row.get("cpm")), 2),
        "cpc": round(fnum(row.get("cpc")), 2),
        "ctr": round(fnum(row.get("ctr")), 2),
        "freq": round(fnum(row.get("frequency")), 1),
        "reach": int(fnum(row.get("reach"))),
        "impressions": int(fnum(row.get("impressions"))),
    }


def custo(spend, n):
    return round(spend / n, 2) if n else None


# --------------------------------------------------------------------------
# Camada de rede: paginação + retry com backoff exponencial
# --------------------------------------------------------------------------
class MetaError(RuntimeError):
    pass


def graph_get(path, params, max_retries=5):
    params = dict(params)
    params["access_token"] = TOKEN
    url = f"{BASE}/{path}"
    rows = []
    while url:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = requests.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                if attempt >= max_retries:
                    raise MetaError(f"Falha de rede em {url}: {e}")
                _backoff(attempt); continue
            if resp.status_code in (429, 500, 502, 503):
                if attempt >= max_retries:
                    raise MetaError(f"HTTP {resp.status_code} persistente em {url}: {resp.text[:400]}")
                _backoff(attempt); continue
            if resp.status_code != 200:
                raise MetaError(f"HTTP {resp.status_code} em {url}: {resp.text[:600]}")
            payload = resp.json()
            if "error" in payload:
                err = payload["error"]
                if err.get("code") in (4, 17, 32, 613) and attempt < max_retries:
                    _backoff(attempt); continue
                raise MetaError(f"Erro da API Meta: {json.dumps(err)[:600]}")
            rows.extend(payload.get("data", []))
            url = payload.get("paging", {}).get("next")
            params = {}  # 'next' já é URL completa
            break
    return rows


def _backoff(attempt):
    wait = min(2 ** attempt, 60)
    print(f"  ... aguardando {wait}s (tentativa {attempt})", file=sys.stderr)
    time.sleep(wait)


# --------------------------------------------------------------------------
# Janelas de tempo (horário de São Paulo)
# --------------------------------------------------------------------------
def hoje_sp():
    return datetime.now(SAO_PAULO).date()


def d(date):
    return date.strftime("%Y-%m-%d")


def janelas():
    hoje = hoje_sp()
    j7 = (hoje - timedelta(days=6), hoje)
    p7 = (hoje - timedelta(days=13), hoje - timedelta(days=7))
    j30 = (hoje - timedelta(days=29), hoje)
    p30 = (hoje - timedelta(days=59), hoje - timedelta(days=30))
    ini_mes = hoje.replace(day=1)
    dias_no_mes = (hoje - ini_mes).days
    fim_mes_ant = ini_mes - timedelta(days=1)
    ini_mes_ant = fim_mes_ant.replace(day=1)
    j_mes = (ini_mes, hoje)
    p_mes = (ini_mes_ant, min(ini_mes_ant + timedelta(days=dias_no_mes), fim_mes_ant))
    return {
        "7":   {"atual": j7,   "anterior": p7},
        "30":  {"atual": j30,  "anterior": p30},
        "mes": {"atual": j_mes, "anterior": p_mes},
    }


# --------------------------------------------------------------------------
# Objetos ATIVOS (effective_status = ACTIVE) + metadados
# --------------------------------------------------------------------------
def objetivo_amigavel(obj):
    mapa = {
        "OUTCOME_ENGAGEMENT": "Engajamento", "MESSAGES": "Mensagens",
        "OUTCOME_TRAFFIC": "Tráfego", "LINK_CLICKS": "Tráfego",
        "OUTCOME_AWARENESS": "Reconhecimento", "BRAND_AWARENESS": "Reconhecimento",
        "REACH": "Alcance", "OUTCOME_LEADS": "Cadastros", "LEAD_GENERATION": "Cadastros",
        "OUTCOME_SALES": "Vendas", "CONVERSIONS": "Vendas",
        "VIDEO_VIEWS": "Vídeo", "POST_ENGAGEMENT": "Engajamento",
    }
    return mapa.get(obj, obj.title().replace("_", " ") if obj else "—")


def dias_no_ar(created_time):
    if not created_time:
        return 999
    try:
        dt = datetime.fromisoformat(created_time).date()
        return max((hoje_sp() - dt).days, 0)
    except ValueError:
        return 999


def ativos(edge, campos):
    """Objetos ativos de um nível (campaigns/adsets/ads)."""
    return graph_get(f"{AD_ACCOUNT}/{edge}", {
        "fields": campos, "effective_status": '["ACTIVE"]', "limit": 200,
    })


# --------------------------------------------------------------------------
# Montagem das tabelas por nível (só ativos com gasto no período)
# --------------------------------------------------------------------------
def tabela(rows, id_key, name_key, ativos_meta, subrotulo):
    saida = []
    for r in rows:
        oid = r.get(id_key)
        if oid not in ativos_meta:      # não está ativo -> ignora
            continue
        m = metricas(r)
        if m["spend"] <= 0:             # ativo mas sem gasto -> ignora
            continue
        meta = ativos_meta[oid]
        saida.append({
            "nome": meta.get("nome") or r.get(name_key) or "Sem nome",
            "sub": subrotulo(r, meta),
            "spend": m["spend"],
            "cadastros": m["cadastros"],
            "custoCadastro": custo(m["spend"], m["cadastros"]),
            "conversas": m["conversas"],
            "linkClicks": m["linkClicks"],
            "custoClique": custo(m["spend"], m["linkClicks"]),
            "cpm": m["cpm"], "ctr": m["ctr"], "freq": m["freq"],
            "dias": meta.get("dias", 999),
        })
    saida.sort(key=lambda x: x["spend"], reverse=True)
    return saida[:TOP_N]


def kpis_conta(rows):
    if not rows:
        return {k: 0 for k in ("spend", "cadastros", "pageViews", "conversas",
                               "linkClicks", "cpm", "cpc", "ctr", "freq",
                               "reach", "impressions")} | \
               {"custoCadastro": None, "custoPageView": None,
                "custoClique": None, "custoConversa": None}
    m = metricas(rows[0])
    return {
        "spend": m["spend"],
        "cadastros": m["cadastros"], "custoCadastro": custo(m["spend"], m["cadastros"]),
        "pageViews": m["pageViews"], "custoPageView": custo(m["spend"], m["pageViews"]),
        "linkClicks": m["linkClicks"], "custoClique": custo(m["spend"], m["linkClicks"]),
        "conversas": m["conversas"], "custoConversa": custo(m["spend"], m["conversas"]),
        "cpm": m["cpm"], "cpc": m["cpc"], "ctr": m["ctr"], "freq": m["freq"],
        "reach": m["reach"], "impressions": m["impressions"],
    }


def insights(level, since, until, extra_fields=""):
    fields = INSIGHT_FIELDS + (("," + extra_fields) if extra_fields else "")
    return graph_get(f"{AD_ACCOUNT}/insights", {
        "level": level, "fields": fields,
        "time_range": json.dumps({"since": d(since), "until": d(until)}),
        "limit": 500,
    })


def periodo(janela, meta_camp, meta_adset, meta_ad):
    ai, af = janela["atual"]
    pi, pf = janela["anterior"]

    kpis = kpis_conta(insights("account", ai, af))
    prev_full = kpis_conta(insights("account", pi, pf))
    prev = {k: prev_full[k] for k in
            ("spend", "cadastros", "custoCadastro", "pageViews",
             "custoPageView", "linkClicks", "conversas")}

    camp_rows = insights("campaign", ai, af, "campaign_id,campaign_name")
    adset_rows = insights("adset", ai, af, "adset_id,adset_name,campaign_name")
    ad_rows = insights("ad", ai, af, "ad_id,ad_name,adset_name,campaign_name")

    campanhas = tabela(camp_rows, "campaign_id", "campaign_name", meta_camp,
                       lambda r, m: m.get("objetivo", "—"))
    conjuntos = tabela(adset_rows, "adset_id", "adset_name", meta_adset,
                       lambda r, m: r.get("campaign_name", ""))
    anuncios = tabela(ad_rows, "ad_id", "ad_name", meta_ad,
                      lambda r, m: r.get("campaign_name", ""))

    return {"kpis": kpis, "prev": prev,
            "niveis": {"campanhas": campanhas, "conjuntos": conjuntos, "anuncios": anuncios}}


def serie_diaria():
    hoje = hoje_sp()
    since = hoje - timedelta(days=89)
    rows = graph_get(f"{AD_ACCOUNT}/insights", {
        "level": "account", "fields": INSIGHT_FIELDS,
        "time_range": json.dumps({"since": d(since), "until": d(hoje)}),
        "time_increment": 1, "limit": 500,
    })
    serie = []
    for r in rows:
        m = metricas(r)
        serie.append({
            "date": r.get("date_start"),
            "spend": m["spend"],
            "cadastros": m["cadastros"],
            "linkClicks": m["linkClicks"],
            "pageViews": m["pageViews"],
            "custoCadastro": custo(m["spend"], m["cadastros"]),
        })
    serie.sort(key=lambda x: x["date"] or "")
    return serie


# --------------------------------------------------------------------------
# Criptografia: AES-GCM com chave derivada por PBKDF2 (compatível com WebCrypto)
# --------------------------------------------------------------------------
PBKDF2_ITER = 200_000


def encrypt(obj):
    if not PASSWORD:
        raise MetaError("DASH_PASSWORD não definido — não dá para criptografar.")
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITER).derive(PASSWORD.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, data, None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER,
            "salt": base64.b64encode(salt).decode(),
            "iv": base64.b64encode(iv).decode(),
            "ct": base64.b64encode(ct).decode()}


def escrever(nome, obj):
    caminho = os.path.join(OUT_DIR, nome)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(encrypt(obj), f, ensure_ascii=False)
    print(f"  ✓ {nome} ({os.path.getsize(caminho)} bytes, criptografado)")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    if not TOKEN:
        print("ERRO: variável de ambiente META_ACCESS_TOKEN vazia.", file=sys.stderr)
        sys.exit(1)
    if not PASSWORD:
        print("ERRO: variável de ambiente DASH_PASSWORD vazia.", file=sys.stderr)
        sys.exit(1)

    print(f"Coletando conta {AD_ACCOUNT} via Graph API {GRAPH_VERSION} ...")

    # 1) Metadados dos objetos ativos (nome, dias no ar, objetivo).
    meta_camp = {c["id"]: {"nome": c.get("name"), "objetivo": objetivo_amigavel(c.get("objective", "")),
                           "dias": dias_no_ar(c.get("created_time")), "status": c.get("status")}
                 for c in ativos("campaigns", "id,name,objective,created_time,status")}
    meta_adset = {a["id"]: {"nome": a.get("name"), "dias": dias_no_ar(a.get("created_time"))}
                  for a in ativos("adsets", "id,name,created_time")}
    meta_ad = {a["id"]: {"nome": a.get("name"), "dias": dias_no_ar(a.get("created_time"))}
               for a in ativos("ads", "id,name,created_time")}
    print(f"  ativos -> campanhas: {len(meta_camp)} | conjuntos: {len(meta_adset)} | anúncios: {len(meta_ad)}")

    # 2) Períodos (toda a coleta em memória; erro aqui aborta antes de escrever).
    campanhas_out = {}
    for chave, jan in janelas().items():
        print(f"  período '{chave}' ...")
        campanhas_out[chave] = periodo(jan, meta_camp, meta_adset, meta_ad)

    print("  série diária (90 dias) ...")
    diario = serie_diaria()

    agora = datetime.now(SAO_PAULO)
    meta_out = {
        "updated": agora.isoformat(),
        "updated_label": agora.strftime("%d/%m/%Y às %H:%M"),
        "tz": TIMEZONE, "account": AD_ACCOUNT, "graph_version": GRAPH_VERSION,
        "instagram": False,   # vira True quando a coleta do IG for ligada
    }

    # 3) Escreve tudo criptografado só depois de coletar com sucesso.
    print("Criptografando e gravando ...")
    escrever("diario.json", diario)
    escrever("campanhas.json", campanhas_out)
    escrever("meta.json", meta_out)
    print("Concluído com sucesso.")


if __name__ == "__main__":
    try:
        main()
    except MetaError as e:
        print(f"\nFALHA NA COLETA: {e}", file=sys.stderr)
        sys.exit(1)
