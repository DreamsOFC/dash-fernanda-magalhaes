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

# Teto de linhas por nível/tabela (alto o bastante para não cortar ativos com gasto;
# existe só como trava contra explosão de tamanho). Os maiores gastadores vêm primeiro.
TOP_N = 300

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


class MetaTimeout(MetaError):
    """Subcode 1504018 — a solicitação síncrona expirou (conta grande)."""


TIMEOUT_SUBCODE = 1504018


def _erro_subcode(resp):
    try:
        return resp.json().get("error", {}).get("error_subcode")
    except Exception:
        return None


def graph_get(path, params, max_retries=5):
    """GET numa EDGE (retorna a lista 'data', seguindo paginação)."""
    params = dict(params)
    params["access_token"] = TOKEN
    url = f"{BASE}/{path}"
    rows = []
    while url:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = requests.get(url, params=params, timeout=90)
            except requests.RequestException as e:
                if attempt >= max_retries:
                    raise MetaError(f"Falha de rede em {url}: {e}")
                _backoff(attempt); continue

            if resp.status_code == 200:
                payload = resp.json()
                if "error" in payload:
                    err = payload["error"]
                    if err.get("error_subcode") == TIMEOUT_SUBCODE:
                        raise MetaTimeout("Meta: solicitação expirou (subcode 1504018)")
                    if err.get("code") in (4, 17, 32, 613) and attempt < max_retries:
                        _backoff(attempt); continue
                    raise MetaError(f"Erro da API Meta: {json.dumps(err)[:600]}")
                rows.extend(payload.get("data", []))
                url = payload.get("paging", {}).get("next")
                params = {}  # 'next' já é URL completa
                break

            # Não-200: primeiro checa se é o timeout (pode vir como 400/500)
            if _erro_subcode(resp) == TIMEOUT_SUBCODE:
                raise MetaTimeout("Meta: solicitação expirou (subcode 1504018)")
            if resp.status_code in (429, 500, 502, 503):
                if attempt >= max_retries:
                    raise MetaError(f"HTTP {resp.status_code} persistente em {url}: {resp.text[:400]}")
                _backoff(attempt); continue
            raise MetaError(f"HTTP {resp.status_code} em {url}: {resp.text[:600]}")
    return rows


def graph_get_node(path, params):
    """GET num NÓ (retorna o objeto cru — ex.: status de um job assíncrono)."""
    p = dict(params); p["access_token"] = TOKEN
    resp = requests.get(f"{BASE}/{path}", params=p, timeout=60)
    if resp.status_code != 200:
        raise MetaError(f"HTTP {resp.status_code} em {path}: {resp.text[:300]}")
    return resp.json()


def graph_post(path, params):
    """POST (cria um job de Async Insights)."""
    data = dict(params); data["access_token"] = TOKEN
    try:
        resp = requests.post(f"{BASE}/{path}", data=data, timeout=120)
    except requests.RequestException as e:
        raise MetaError(f"Falha de rede (POST) em {path}: {e}")
    if resp.status_code != 200:
        if _erro_subcode(resp) == TIMEOUT_SUBCODE:
            raise MetaTimeout("Meta: solicitação expirou (subcode 1504018)")
        raise MetaError(f"HTTP {resp.status_code} (POST) em {path}: {resp.text[:400]}")
    return resp.json()


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


def _janela(since, until):
    """Monta o dicionário de uma janela + o período anterior de igual tamanho."""
    L = (until - since).days + 1
    return {"since": since, "until": until,
            "prev_since": since - timedelta(days=L), "prev_until": since - timedelta(days=1)}


def periodos_def():
    """Presets estilo Gerenciador de Anúncios (fuso da conta, UTC-3). As janelas
    'Últimos N dias' excluem hoje (igual à Meta); 'Hoje'/'Ontem' são explícitas.
    Cada período traz também o intervalo anterior, de igual tamanho, p/ a variação."""
    hoje = hoje_sp()
    ontem = hoje - timedelta(days=1)
    ini_mes = hoje.replace(day=1)
    fim_mes_ant = ini_mes - timedelta(days=1)
    ini_mes_ant = fim_mes_ant.replace(day=1)

    # Conjunto enxuto p/ a primeira carga estável em conta grande. "Mês passado" e
    # "Máximo (90 dias)" ficaram de fora por serem os mais pesados — reativar depois.
    presets = [
        ("hoje",        "Hoje",             hoje,                        hoje),
        ("ontem",       "Ontem",            ontem,                       ontem),
        ("hoje_ontem",  "Hoje e ontem",     ontem,                       hoje),
        ("3d",          "Últimos 3 dias",   hoje - timedelta(days=3),    ontem),
        ("7d",          "Últimos 7 dias",   hoje - timedelta(days=7),    ontem),
        ("14d",         "Últimos 14 dias",  hoje - timedelta(days=14),   ontem),
        ("30d",         "Últimos 30 dias",  hoje - timedelta(days=30),   ontem),
        ("mes",         "Este mês",         ini_mes,                     hoje),
    ]
    _ = (ini_mes_ant, fim_mes_ant)  # reservado p/ "Mês passado" quando reativar
    return {key: {"label": label, **_janela(since, until)}
            for key, label, since, until in presets}


# Publicações turbinadas: o Meta nomeia automaticamente com estes padrões.
IMPULS_PADROES = (
    "post do instagram", "publicação do instagram", "publicacao do instagram",
    "publicação:", "publicacao:", "instagram post",
)


def eh_impulsionamento(nome):
    n = (nome or "").lower()
    return any(p in n for p in IMPULS_PADROES)


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
def tabela(rows, id_key, name_key, ativos_meta, subrotulo, nome_camp):
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
            "tipo": "imp" if eh_impulsionamento(nome_camp(r, meta)) else "camp",
            "spend": m["spend"],
            "cadastros": m["cadastros"],
            "custoCadastro": custo(m["spend"], m["cadastros"]),
            "conversas": m["conversas"],
            "linkClicks": m["linkClicks"],
            "custoClique": custo(m["spend"], m["linkClicks"]),
            "pageViews": m["pageViews"],
            "cpm": m["cpm"], "ctr": m["ctr"], "freq": m["freq"],
            "reach": m["reach"], "impressions": m["impressions"],
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


def _insights_params(level, since, until, extra_fields, ativos_only):
    fields = INSIGHT_FIELDS + (("," + extra_fields) if extra_fields else "")
    params = {
        "level": level, "fields": fields,
        "time_range": json.dumps({"since": d(since), "until": d(until)}),
        "limit": 500,
    }
    # Reduz a carga no servidor: só objetos ATIVOS (o dashboard só mostra ativos mesmo).
    if ativos_only and level != "account":
        params["filtering"] = json.dumps(
            [{"field": f"{level}.effective_status", "operator": "IN", "value": ["ACTIVE"]}])
    return params


def insights_async(params, rotulo=""):
    """Async Insights API: cria o job, faz poll do status e busca o resultado.
    Caminho oficial da Meta para consultas pesadas (contas grandes)."""
    run = graph_post(f"{AD_ACCOUNT}/insights", params)
    run_id = run.get("report_run_id")
    if not run_id:
        raise MetaError(f"Async sem report_run_id: {json.dumps(run)[:200]}")
    print(f"  [async {rotulo}] job {run_id} criado; aguardando...", file=sys.stderr)
    for _ in range(120):                       # ~6 min de tolerância
        time.sleep(3)
        st = graph_get_node(str(run_id), {"fields": "async_status,async_percent_completion"})
        status = st.get("async_status")
        if status == "Job Completed":
            return graph_get(f"{run_id}/insights", {"limit": 500})
        if status in ("Job Failed", "Job Skipped"):
            raise MetaError(f"Async job {status} ({rotulo})")
    raise MetaError(f"Async job não completou a tempo ({rotulo})")


def insights(level, since, until, extra_fields="", ativos_only=False):
    """Insights com time_range explícito. Tenta síncrono; se o Meta estourar o tempo
    (subcode 1504018), cai para a Async Insights API."""
    params = _insights_params(level, since, until, extra_fields, ativos_only)
    try:
        return graph_get(f"{AD_ACCOUNT}/insights", params)
    except MetaTimeout:
        rotulo = f"{level} {d(since)}..{d(until)}"
        print(f"  timeout síncrono em {rotulo} -> Async Insights", file=sys.stderr)
        return insights_async(params, rotulo=rotulo)


def split_por_tipo(camp_rows, meta_camp):
    """Reparte o investimento entre publicações turbinadas e campanhas estruturadas.
    Soma TODAS as campanhas com gasto no período (não só as ativas/top-N)."""
    imp_spend = camp_spend = 0.0
    imp_n = camp_n = 0
    for r in camp_rows:
        m = metricas(r)
        if m["spend"] <= 0:
            continue
        meta = meta_camp.get(r.get("campaign_id")) or {}
        nome = meta.get("nome") or r.get("campaign_name")
        if eh_impulsionamento(nome):
            imp_spend += m["spend"]; imp_n += 1
        else:
            camp_spend += m["spend"]; camp_n += 1
    return {"impSpend": round(imp_spend, 2), "campSpend": round(camp_spend, 2),
            "impCount": imp_n, "campCount": camp_n}


def periodo(pdef, meta_camp, meta_adset, meta_ad):
    ai, af = pdef["since"], pdef["until"]
    pi, pf = pdef["prev_since"], pdef["prev_until"]

    kpis = kpis_conta(insights("account", ai, af))
    prev_full = kpis_conta(insights("account", pi, pf))
    prev = {k: prev_full[k] for k in
            ("spend", "cadastros", "custoCadastro", "pageViews",
             "custoPageView", "linkClicks", "conversas")}

    camp_rows = insights("campaign", ai, af, "campaign_id,campaign_name", ativos_only=True)
    adset_rows = insights("adset", ai, af, "adset_id,adset_name,campaign_name", ativos_only=True)
    ad_rows = insights("ad", ai, af, "ad_id,ad_name,adset_name,campaign_name", ativos_only=True)

    campanhas = tabela(camp_rows, "campaign_id", "campaign_name", meta_camp,
                       lambda r, m: m.get("objetivo", "—"),
                       lambda r, m: m.get("nome") or r.get("campaign_name"))
    conjuntos = tabela(adset_rows, "adset_id", "adset_name", meta_adset,
                       lambda r, m: r.get("campaign_name", ""),
                       lambda r, m: r.get("campaign_name"))
    anuncios = tabela(ad_rows, "ad_id", "ad_name", meta_ad,
                      lambda r, m: r.get("campaign_name", ""),
                      lambda r, m: r.get("campaign_name"))

    return {"label": pdef["label"], "since": d(ai), "until": d(af),
            "kpis": kpis, "prev": prev,
            "split": split_por_tipo(camp_rows, meta_camp),
            "niveis": {"campanhas": campanhas, "conjuntos": conjuntos, "anuncios": anuncios}}


DIAS_SERIE = 30   # janela padrão da primeira carga (era 90); aumentar depois se estável.


def _serie_chunk(since, until):
    """Série diária de um intervalo. Em timeout (1504018), divide pela metade e retenta."""
    try:
        return graph_get(f"{AD_ACCOUNT}/insights", {
            "level": "account", "fields": INSIGHT_FIELDS,
            "time_range": json.dumps({"since": d(since), "until": d(until)}),
            "time_increment": 1, "limit": 500,
        })
    except MetaTimeout:
        if since >= until:
            raise
        meio = since + timedelta(days=(until - since).days // 2)
        print(f"  timeout na série {d(since)}..{d(until)}; dividindo", file=sys.stderr)
        return _serie_chunk(since, meio) + _serie_chunk(meio + timedelta(days=1), until)


def serie_diaria():
    # Incluindo hoje (para presets curtos como "Hoje" terem ponto no gráfico). Buscada
    # em janelas de 7 dias, sequenciais, para não estourar o tempo do Meta.
    hoje = hoje_sp()
    ini = hoje - timedelta(days=DIAS_SERIE - 1)
    brutos = []
    cur = ini
    while cur <= hoje:
        fim = min(cur + timedelta(days=6), hoje)
        brutos.extend(_serie_chunk(cur, fim))
        cur = fim + timedelta(days=1)

    serie = []
    for r in brutos:
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
# Instagram orgânico (perfil + seguidores) — opcional, não aborta a coleta
# --------------------------------------------------------------------------
def ts_sp(date):
    """Meia-noite (São Paulo) da data, em timestamp Unix — usado pelo IG insights."""
    return int(datetime(date.year, date.month, date.day, tzinfo=SAO_PAULO).timestamp())


def instagram_conta():
    """Descobre a conta de Instagram vinculada à Página. Retorna (id, username, seguidores)."""
    pages = graph_get("me/accounts", {
        "fields": "name,instagram_business_account{id,username,followers_count}", "limit": 50})
    for p in pages:
        iga = p.get("instagram_business_account")
        if iga and iga.get("id"):
            return iga["id"], iga.get("username"), int(iga.get("followers_count") or 0)
    return None, None, 0


def ig_novos_seguidores(ig_id, since_d, until_d):
    """Soma de novos seguidores no intervalo. A API do IG exige janela ABAIXO de
    30 dias por chamada, então quebramos em pedaços de até 28 dias."""
    total = 0
    cur = since_d
    while cur <= until_d:
        fim = min(cur + timedelta(days=27), until_d)   # span de até 28 dias (< 30)
        res = graph_get(f"{ig_id}/insights", {
            "metric": "follower_count", "period": "day",
            "since": ts_sp(cur), "until": ts_sp(fim + timedelta(days=1))})
        if res:
            for v in res[0].get("values", []):
                total += int(v.get("value") or 0)
        cur = fim + timedelta(days=1)
    return total


def ig_totais(ig_id, since_d, until_d):
    """Visitas ao perfil, alcance orgânico e toques no link do site no intervalo."""
    out = {"profileViews": 0, "reachOrg": 0, "websiteClicks": 0}
    mapa = {"profile_views": "profileViews", "reach": "reachOrg", "website_clicks": "websiteClicks"}
    try:
        res = graph_get(f"{ig_id}/insights", {
            "metric": "profile_views,reach,website_clicks", "period": "day",
            "metric_type": "total_value",
            "since": ts_sp(since_d), "until": ts_sp(until_d + timedelta(days=1))})
        for m in res:
            k = mapa.get(m.get("name"))
            if k:
                out[k] = int((m.get("total_value") or {}).get("value") or 0)
    except MetaError as e:
        print(f"  (IG totais indisponíveis p/ este intervalo: {str(e)[:120]})", file=sys.stderr)
    return out


def coletar_instagram(ig_id, pdef, spend_periodo):
    def novos(since_d, until_d):
        try:
            return ig_novos_seguidores(ig_id, since_d, until_d)
        except MetaError as e:
            print(f"  (follower_count indisponível: {str(e)[:140]})", file=sys.stderr)
            return None
    n = novos(pdef["since"], pdef["until"])
    prev = novos(pdef["prev_since"], pdef["prev_until"])
    t = ig_totais(ig_id, pdef["since"], pdef["until"])
    return {
        "novosSeguidores": n,
        "prevNovos": prev,
        "custoSeguidor": custo(spend_periodo, n) if n else None,
        "profileViews": t["profileViews"],
        "reachOrg": t["reachOrg"],
        "websiteClicks": t["websiteClicks"],
    }


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


def decrypt(env):
    salt = base64.b64decode(env["salt"])
    iv = base64.b64decode(env["iv"])
    ct = base64.b64decode(env["ct"])
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=int(env.get("iter", PBKDF2_ITER))).derive(PASSWORD.encode("utf-8"))
    return json.loads(AESGCM(key).decrypt(iv, ct, None).decode("utf-8"))


def ler_existente(nome):
    """Lê e descriptografa um JSON já commitado, p/ manter dados anteriores quando um
    período novo falha (nunca sobrescrever dado bom com vazio)."""
    caminho = os.path.join(OUT_DIR, nome)
    if not os.path.exists(caminho):
        return None
    try:
        with open(caminho, encoding="utf-8") as f:
            env = json.load(f)
        return decrypt(env)
    except Exception as e:
        print(f"  (não consegui ler {nome} anterior: {str(e)[:120]})", file=sys.stderr)
        return None


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
    # Instagram é opcional: se o token não tiver as permissões, a coleta de
    # anúncios continua normalmente (só não mostra a seção do Instagram).
    ig_id = ig_user = None
    ig_seguidores = 0
    try:
        ig_id, ig_user, ig_seguidores = instagram_conta()
        print(f"  Instagram: @{ig_user} ({ig_seguidores} seguidores)" if ig_id
              else "  Instagram: nenhuma conta vinculada ao token.")
    except MetaError as e:
        print(f"  Instagram indisponível (segue sem ele): {str(e)[:160]}", file=sys.stderr)

    # Dados já publicados — usados como reserva se um período novo falhar.
    old_camp = ler_existente("campanhas.json") or {}
    old_diario = ler_existente("diario.json")

    # 3) Cada período é independente: se um falha, mantém o dado anterior daquele
    #    período em vez de perder tudo.
    campanhas_out = {}
    falhas = []
    for chave, pdef in periodos_def().items():
        print(f"  período '{chave}' ...")
        try:
            p = periodo(pdef, meta_camp, meta_adset, meta_ad)
            if ig_id:
                try:
                    p["ig"] = coletar_instagram(ig_id, pdef, p["kpis"]["spend"])
                except MetaError as e:
                    print(f"  (IG período '{chave}' falhou: {str(e)[:120]})", file=sys.stderr)
            campanhas_out[chave] = p
        except MetaError as e:
            falhas.append(chave)
            print(f"  período '{chave}' FALHOU: {str(e)[:160]}", file=sys.stderr)
            if chave in old_camp:
                campanhas_out[chave] = old_camp[chave]
                print(f"   -> mantido o dado anterior de '{chave}'")

    if not campanhas_out:
        raise MetaError("Nenhum período coletado e sem dados anteriores — abortando "
                        "para não apagar dados bons.")

    print(f"  série diária ({DIAS_SERIE} dias) ...")
    try:
        diario = serie_diaria()
    except MetaError as e:
        print(f"  série diária FALHOU: {str(e)[:160]}", file=sys.stderr)
        diario = old_diario if old_diario else []
        if old_diario:
            print("   -> mantida a série anterior")

    agora = datetime.now(SAO_PAULO)
    meta_out = {
        "updated": agora.isoformat(),
        "updated_label": agora.strftime("%d/%m/%Y às %H:%M"),
        "tz": TIMEZONE, "account": AD_ACCOUNT, "graph_version": GRAPH_VERSION,
        "instagram": bool(ig_id),
        "ig_username": ig_user,
        "seguidores_total": ig_seguidores,
        "periodos_falhos": falhas,
    }

    # 4) Grava (criptografado). campanhas_out nunca está vazio aqui.
    print("Criptografando e gravando ...")
    if diario:                       # não sobrescreve série boa com vazio
        escrever("diario.json", diario)
    else:
        print("  (diario.json mantido — sem dados novos nem anteriores)")
    escrever("campanhas.json", campanhas_out)
    escrever("meta.json", meta_out)
    print(f"Concluído. Períodos coletados: {len(campanhas_out)} | falhos: {falhas or 'nenhum'}")


if __name__ == "__main__":
    try:
        main()
    except MetaError as e:
        print(f"\nFALHA NA COLETA: {e}", file=sys.stderr)
        sys.exit(1)
