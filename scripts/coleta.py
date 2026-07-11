#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coleta de dados do Meta Ads -> JSONs criptografados em docs/data/.

Roda no GitHub Actions (de hora em hora e sob demanda). NÃO guarda o token
nem a senha no código: os dois vêm de variáveis de ambiente que o Actions
injeta a partir dos GitHub Secrets.

Saídas (todas criptografadas com AES-GCM):
  docs/data/diario.json     -> série diária da conta (90 dias) p/ o gráfico
  docs/data/campanhas.json  -> KPIs + tabela de campanhas, já separados por período
  docs/data/meta.json       -> data/hora da última coleta

Filosofia à prova de falha: se QUALQUER chamada à API falhar, o script aborta
com erro (exit != 0) ANTES de escrever qualquer arquivo. Assim o workflow falha
de forma visível e nunca sobrescreve dados bons com lixo.
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

# Campos de insights pedidos à API.
INSIGHT_FIELDS = ",".join([
    "spend", "impressions", "reach", "frequency", "cpm", "cpc", "ctr",
    "clicks", "inline_link_clicks", "cost_per_inline_link_click",
    "actions", "cost_per_action_type",
])

# action_types que nos interessam.
ACT_CONVERSA = "onsite_conversion.messaging_conversation_started_7d"
ACT_LEAD = "lead"
ACT_LEAD_PIXEL = "offsite_conversion.fb_pixel_lead"


# --------------------------------------------------------------------------
# Helpers de parsing
# --------------------------------------------------------------------------
def get_action(lista, tipo):
    """Retorna o value (float) de um action_type dentro da lista 'actions' ou
    'cost_per_action_type'. Retorna None quando o tipo não existe — nunca
    estoura KeyError."""
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
    """Converte string/None em float de forma segura (0.0 quando ausente)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def conversas_de(row):
    """Conversas iniciadas por mensagem no registro de insights."""
    n = get_action(row.get("actions"), ACT_CONVERSA)
    return int(n) if n else 0


def leads_de(row):
    """Leads (formulário ou pixel)."""
    a = get_action(row.get("actions"), ACT_LEAD)
    b = get_action(row.get("actions"), ACT_LEAD_PIXEL)
    total = (a or 0) + (b or 0)
    return int(total)


# --------------------------------------------------------------------------
# Camada de rede: paginação + retry com backoff exponencial
# --------------------------------------------------------------------------
class MetaError(RuntimeError):
    pass


def graph_get(path, params, max_retries=5):
    """GET numa URL da Graph API seguindo paging.next e reagindo a rate limit.
    Retorna a lista completa de 'data'. Levanta MetaError em falha definitiva."""
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
                _backoff(attempt)
                continue

            # Rate limit / erro temporário -> backoff e retenta.
            if resp.status_code in (429, 500, 502, 503):
                if attempt >= max_retries:
                    raise MetaError(
                        f"HTTP {resp.status_code} persistente em {url}: {resp.text[:400]}"
                    )
                _backoff(attempt)
                continue

            if resp.status_code != 200:
                # Erro definitivo (token inválido, permissão, campo errado...).
                raise MetaError(
                    f"HTTP {resp.status_code} em {url}: {resp.text[:600]}"
                )

            payload = resp.json()
            if "error" in payload:
                err = payload["error"]
                code = err.get("code")
                # Códigos de rate limit da Meta -> retenta.
                if code in (4, 17, 32, 613) and attempt < max_retries:
                    _backoff(attempt)
                    continue
                raise MetaError(f"Erro da API Meta: {json.dumps(err)[:600]}")

            rows.extend(payload.get("data", []))
            # Próxima página (a URL de 'next' já traz todos os params + token).
            url = payload.get("paging", {}).get("next")
            params = {}  # 'next' é uma URL completa; não reanexar params.
            break

    return rows


def _backoff(attempt):
    wait = min(2 ** attempt, 60)
    print(f"  ... aguardando {wait}s antes de retentar (tentativa {attempt})",
          file=sys.stderr)
    time.sleep(wait)


# --------------------------------------------------------------------------
# Janelas de tempo (em horário de São Paulo)
# --------------------------------------------------------------------------
def hoje_sp():
    return datetime.now(SAO_PAULO).date()


def d(date):
    return date.strftime("%Y-%m-%d")


def janelas():
    """Define os períodos dos chips e o período anterior de cada um (p/ variação)."""
    hoje = hoje_sp()

    # 7 dias (inclui hoje) e os 7 anteriores.
    j7 = (hoje - timedelta(days=6), hoje)
    p7 = (hoje - timedelta(days=13), hoje - timedelta(days=7))

    # 30 dias e os 30 anteriores.
    j30 = (hoje - timedelta(days=29), hoje)
    p30 = (hoje - timedelta(days=59), hoje - timedelta(days=30))

    # Mês atual (do dia 1 até hoje) x mesmo trecho do mês anterior.
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
# Coleta
# --------------------------------------------------------------------------
def insights_conta(since, until, time_increment=None):
    params = {
        "level": "account",
        "fields": INSIGHT_FIELDS,
        "time_range": json.dumps({"since": d(since), "until": d(until)}),
        "limit": 500,
    }
    if time_increment:
        params["time_increment"] = time_increment
    return graph_get(f"{AD_ACCOUNT}/insights", params)


def insights_campanhas(since, until):
    params = {
        "level": "campaign",
        "fields": INSIGHT_FIELDS + ",campaign_name,campaign_id,objective",
        "time_range": json.dumps({"since": d(since), "until": d(until)}),
        "limit": 500,
    }
    return graph_get(f"{AD_ACCOUNT}/insights", params)


def meta_campanhas():
    """Metadados das campanhas (status, objetivo, data de criação p/ 'dias no ar')."""
    params = {"fields": "id,name,status,objective,created_time", "limit": 500}
    rows = graph_get(f"{AD_ACCOUNT}/campaigns", params)
    meta = {}
    hoje = hoje_sp()
    for c in rows:
        criado = c.get("created_time")  # ex: 2026-05-01T10:00:00-0300
        dias = None
        if criado:
            try:
                dt = datetime.fromisoformat(criado).date()
                dias = max((hoje - dt).days, 0)
            except ValueError:
                dias = None
        meta[c["id"]] = {
            "nome": c.get("name", "Sem nome"),
            "status": c.get("status", ""),
            "objetivo": objetivo_amigavel(c.get("objective", "")),
            "dias": dias,
        }
    return meta


def objetivo_amigavel(obj):
    """Traduz o enum de objetivo do Meta para algo legível no dash."""
    mapa = {
        "OUTCOME_ENGAGEMENT": "Mensagens",
        "MESSAGES": "Mensagens",
        "OUTCOME_TRAFFIC": "Tráfego",
        "LINK_CLICKS": "Tráfego",
        "OUTCOME_AWARENESS": "Alcance",
        "BRAND_AWARENESS": "Alcance",
        "REACH": "Alcance",
        "OUTCOME_LEADS": "Cadastros",
        "LEAD_GENERATION": "Cadastros",
        "OUTCOME_SALES": "Vendas",
        "CONVERSIONS": "Vendas",
    }
    return mapa.get(obj, obj.title().replace("_", " ") if obj else "—")


def kpis_de_linha(row):
    """Extrai o bloco de KPIs de uma linha agregada de insights (conta)."""
    spend = fnum(row.get("spend"))
    conversas = conversas_de(row)
    clicks = int(fnum(row.get("inline_link_clicks")))
    return {
        "spend": round(spend, 2),
        "conversas": conversas,
        "custoConversa": round(spend / conversas, 2) if conversas else None,
        "clicks": clicks,
        "cpm": round(fnum(row.get("cpm")), 2),
        "cpc": round(fnum(row.get("cpc")), 2),
        "ctr": round(fnum(row.get("ctr")), 2),
        "freq": round(fnum(row.get("frequency")), 1),
        "reach": int(fnum(row.get("reach"))),
        "impressions": int(fnum(row.get("impressions"))),
    }


ZERO_KPI = {"spend": 0, "conversas": 0, "custoConversa": None, "clicks": 0,
            "cpm": 0, "cpc": 0, "ctr": 0, "freq": 0, "reach": 0, "impressions": 0}


def periodo(janela, meta_camp):
    """Monta o bloco de um período: KPIs da conta, KPIs do período anterior e
    a tabela de campanhas."""
    ai, af = janela["atual"]
    pi, pf = janela["anterior"]

    conta_atual = insights_conta(ai, af)
    conta_ant = insights_conta(pi, pf)

    kpis = kpis_de_linha(conta_atual[0]) if conta_atual else dict(ZERO_KPI)
    prev = kpis_de_linha(conta_ant[0]) if conta_ant else dict(ZERO_KPI)

    camp_rows = insights_campanhas(ai, af)
    campanhas = []
    for r in camp_rows:
        cid = r.get("campaign_id")
        m = meta_camp.get(cid, {})
        spend = fnum(r.get("spend"))
        conv = conversas_de(r)
        campanhas.append({
            "nome": m.get("nome") or r.get("campaign_name", "Sem nome"),
            "objetivo": m.get("objetivo") or objetivo_amigavel(r.get("objective", "")),
            "spend": round(spend, 2),
            "conversas": conv,
            "cpm": round(fnum(r.get("cpm")), 2),
            "ctr": round(fnum(r.get("ctr")), 2),
            "freq": round(fnum(r.get("frequency")), 1),
            "dias": m.get("dias") if m.get("dias") is not None else 999,
            "status": m.get("status", ""),
        })
    # Só campanhas com alguma verba no período, maiores primeiro.
    campanhas = [c for c in campanhas if c["spend"] > 0]
    campanhas.sort(key=lambda c: c["spend"], reverse=True)

    return {"kpis": kpis, "prev": {k: prev[k] for k in
            ("spend", "conversas", "custoConversa", "clicks")},
            "campanhas": campanhas}


def serie_diaria():
    """Série diária da conta nos últimos 90 dias, p/ o gráfico."""
    hoje = hoje_sp()
    since = hoje - timedelta(days=89)
    rows = insights_conta(since, hoje, time_increment=1)
    serie = []
    for r in rows:
        data = r.get("date_start")
        spend = fnum(r.get("spend"))
        conv = conversas_de(r)
        serie.append({
            "date": data,
            "spend": round(spend, 2),
            "conversas": conv,
            "custoConversa": round(spend / conv, 2) if conv else None,
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
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITER)
    key = kdf.derive(PASSWORD.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, data, None)  # tag já vai anexado ao fim
    return {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iter": PBKDF2_ITER,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }


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

    # 1) Toda a coleta acontece PRIMEIRO, em memória. Qualquer erro aborta aqui,
    #    antes de tocar nos arquivos — nunca sobrescreve dados bons com vazio.
    js = janelas()
    meta_camp = meta_campanhas()
    print(f"  {len(meta_camp)} campanhas encontradas na conta.")

    campanhas_out = {}
    for chave, jan in js.items():
        print(f"  período '{chave}' ...")
        campanhas_out[chave] = periodo(jan, meta_camp)

    print("  série diária (90 dias) ...")
    diario = serie_diaria()

    agora = datetime.now(SAO_PAULO)
    meta_out = {
        "updated": agora.isoformat(),
        "updated_label": agora.strftime("%d/%m/%Y às %H:%M"),
        "tz": TIMEZONE,
        "account": AD_ACCOUNT,
        "graph_version": GRAPH_VERSION,
    }

    # 2) Só agora, com tudo em mãos, escreve (criptografado).
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
