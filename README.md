# Dashboard Fernanda Magalhães — Meta Ads

Dashboard estático que a cliente acessa por um link e vê os resultados das campanhas
dela sozinha. Os dados são coletados da **Meta Marketing API** de hora em hora por um
workflow do **GitHub Actions**, criptografados e publicados no **GitHub Pages**.

---

## Como funciona (fluxo dos dados)

```
GitHub Actions (cron de hora em hora + botão manual)
        │
        ▼
scripts/coleta.py  ── chama a Graph API do Meta (insights da conta e das campanhas)
        │
        ▼
Criptografa cada JSON com AES-GCM (chave derivada da senha via PBKDF2)
        │
        ▼
Commita em docs/data/*.json  ──►  GitHub Pages serve /docs
        │
        ▼
docs/index.html  ── pede a senha, descriptografa no navegador e mostra o painel
```

- O site em `/docs` é **100% estático**: só faz `fetch` dos JSONs locais, nunca chama
  a API do Meta a partir do navegador.
- O **token do Meta nunca aparece** no código, no front-end nem nos commits. Ele vive
  em GitHub Secrets e só é usado dentro do Actions.
- Como o GitHub Pages é público, os JSONs são **criptografados**. Sem a senha, são bytes
  embaralhados. A cliente digita a senha uma vez e o navegador descriptografa localmente.

---

## Estrutura do projeto

```
docs/
  index.html            layout do dashboard (fetch + descriptografia + filtros)
  data/                 JSONs criptografados gerados pelo workflow
    diario.json           série diária (90 dias) — alimenta o gráfico
    campanhas.json        KPIs + tabela de campanhas, já separados por período
    meta.json             data/hora da última coleta
scripts/
  coleta.py             coleta do Meta + criptografia
  requirements.txt      dependências Python (requests, cryptography)
.github/workflows/
  coleta.yml            agenda (cron) + disparo manual + commit dos dados
.env.example            modelo das variáveis (para rodar local)
```

Enquanto os JSONs reais não existem, o dashboard entra em **modo demo** com dados
fictícios e um aviso discreto de "dados de exemplo".

---

## Secrets (no GitHub: Settings → Secrets and variables → Actions)

| Secret | O que é |
| --- | --- |
| `META_ACCESS_TOKEN` | System User token do Meta (escopo `ads_read`, sem expiração) |
| `META_AD_ACCOUNT_ID` | ID da conta de anúncios, com prefixo `act_` (ex: `act_161375005284674`) |
| `DASH_PASSWORD` | Senha que a cliente usa para abrir o dashboard |

O workflow também precisa de **permissão de escrita**:
Settings → Actions → General → *Workflow permissions* → **Read and write permissions**.

---

## O que a Graph API devolve (referência)

- Endpoint: `GET /v23.0/{META_AD_ACCOUNT_ID}/insights`
- Duas leituras: `level=account` com `time_increment=1` (série diária) e `level=campaign`
  (agregado por período).
- `actions` e `cost_per_action_type` vêm como listas de `{action_type, value}`. O helper
  `get_action(lista, tipo)` devolve `None` quando o tipo não existe, sem quebrar.
- Métricas extraídas: conversas por mensagem
  (`onsite_conversion.messaging_conversation_started_7d`), leads (`lead` e
  `offsite_conversion.fb_pixel_lead`) e cliques no link (`inline_link_clicks`).
- A coleta trata paginação (`paging.next`) e rate limit (retry com backoff exponencial).
- Se a API falhar, o workflow **falha de forma visível** e não sobrescreve dados bons.

---

## Reaproveitar para outro cliente

Este projeto foi feito para ser clonado por cliente. Para uma conta nova:

1. Duplique o repositório (ou crie um novo a partir destes arquivos).
2. Troque os secrets `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID` e `DASH_PASSWORD`.
3. Se quiser, ajuste o nome no cabeçalho de `docs/index.html` (`<h1>` e o título).
4. Ative o GitHub Pages apontando para `/docs` na branch `main`.

Nada de código muda entre clientes — só os secrets e o nome exibido. O ID da conta é
lido de `META_AD_ACCOUNT_ID`; o padrão no `coleta.py` existe apenas como fallback.

---

## Rodar a coleta localmente (opcional, para testar)

```bash
cp .env.example .env      # preencha META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, DASH_PASSWORD
pip install -r scripts/requirements.txt
# carregue o .env no ambiente e rode:
python scripts/coleta.py
```

Os JSONs criptografados aparecem em `docs/data/`. Sirva `docs/` com qualquer servidor
estático (`python -m http.server` dentro de `docs/`) e abra no navegador.

---

## Se o workflow começar a falhar

Abra a aba **Actions** do repositório e clique na execução vermelha para ver o log.
Causas mais comuns:

- **Token expirado ou revogado** → mensagem HTTP 400 com código de erro `190`.
  Gere um novo System User token no Meta e atualize o secret `META_ACCESS_TOKEN`.
- **Sem permissão na conta** → erro `10` ou `200`. Confirme que o System User tem
  acesso à conta de anúncios e o escopo `ads_read`.
- **Rate limit** → códigos `4`, `17`, `32` ou `613`. O script já tenta de novo com
  backoff; se persistir, espere e redispare.
- **ID da conta errado** → confira o secret `META_AD_ACCOUNT_ID` (precisa do `act_`).

Enquanto o problema não é resolvido, o dashboard continua mostrando o **último dado bom**
já commitado — a coleta que falha não apaga o que já estava lá.
