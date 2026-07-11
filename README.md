# Dashboard Fernanda Magalhães — Meta Ads + Instagram

Dashboard estático que a cliente acessa por um link e vê os resultados das campanhas
dela sozinha. Os dados são coletados da **Meta Marketing API** e da **Instagram Graph
API** de hora em hora por um workflow do **GitHub Actions**, criptografados e publicados
no **GitHub Pages**.

**Link do dashboard:** https://dreamsofc.github.io/dash-fernanda-magalhaes/
(protegido por senha — a `DASH_PASSWORD`).

---

## Como funciona (fluxo dos dados)

```
GitHub Actions (cron de hora em hora + botão manual)
        │
        ▼
scripts/coleta.py  ── Graph API do Meta (anúncios) + Instagram (perfil/seguidores)
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

- O site em `/docs` é **100% estático**: só faz `fetch` dos JSONs locais, nunca chama a
  API a partir do navegador.
- O **token do Meta nunca aparece** no código, no front-end nem nos commits — vive em
  GitHub Secrets e só é usado dentro do Actions.
- O repositório é **público** (para o GitHub Pages funcionar no plano gratuito), por isso
  os JSONs são **criptografados**: sem a senha, são bytes embaralhados.

---

## O que o dashboard mostra

**Seletor de período (dropdown estilo Gerenciador):** Hoje, Ontem, Hoje e ontem,
Últimos 3/7/14/30 dias, Este mês, Mês passado e Máximo (90 dias). As janelas "Últimos N
dias" terminam ontem (igual à Meta), então os números batem com o Gerenciador.

**KPIs de anúncios:** investimento, cadastros (leads), custo por cadastro, visualizações
do site (landing page), custo por visualização, cliques no link, custo por clique,
conversas, CPM, CTR, alcance, impressões — cada um com variação vs. período anterior.

**Distribuição de investimento:** ranking das campanhas por gasto, separando
**campanhas estruturadas** de **publicações turbinadas** (impulsionamentos).

**Tabela em 3 níveis:** Campanhas / Conjuntos / Anúncios — só o que está **ativo e teve
investimento** no período. Clicar numa linha abre um **modal** com o resultado detalhado.

**Instagram (orgânico):** novos seguidores, visitas ao perfil, alcance orgânico e custo
por seguidor (aproximação).

---

## Detalhes que valem lembrar

- **Turbinados:** toda campanha cujo nome contém `Post do Instagram`, `Publicação do
  Instagram`, `Instagram post` ou `Publicação:` é classificada como impulsionamento
  (ver `IMPULS_PADROES` em `scripts/coleta.py`). Ajuste a lista lá se necessário.
- **"Ativo com gasto":** a conta tem centenas de posts impulsionados antigos ainda
  "ativos" mas sem gasto — esses não entram na tabela. Turbinados já encerrados também
  não aparecem na tabela, mas continuam somados no total "Publicações turbinadas".
- **Seguidores por campanha NÃO existem na API.** O Gerenciador mostra "Seguidores no
  Instagram" por campanha na *tela*, mas a Meta não expõe esse número na Marketing API
  (confirmado: nenhuma das ~49 métricas de conversão é de follow). Por isso o número de
  seguidores fica no card do Instagram (do perfil inteiro) e não por campanha.
- **Novos seguidores só nos últimos 30 dias:** a Instagram API só devolve o histórico de
  `follower_count` dos últimos 30 dias — em períodos mais antigos (Mês passado, Máximo,
  e as comparações "vs. anterior" dos períodos longos) esse número aparece como "—".
- **Origem do seguidor** (perfil, explorar, anúncio) não é fornecida por nenhuma API —
  só existe dentro do app do Instagram.

---

## Estrutura do projeto

```
docs/
  index.html            dashboard (fetch + descriptografia + filtros + modal)
  data/                 JSONs criptografados gerados pelo workflow
    diario.json           série diária (90 dias) — alimenta o gráfico
    campanhas.json        por período: KPIs, distribuição, tabelas e Instagram
    meta.json             última coleta + dados do Instagram (usuário, seguidores)
scripts/
  coleta.py             coleta (Meta + Instagram) + criptografia
  requirements.txt      dependências Python (requests, cryptography)
.github/workflows/
  coleta.yml            agenda (cron) + disparo manual + commit dos dados
.env.example            modelo das variáveis (para rodar local)
```

Enquanto os JSONs reais não existem, o dashboard entra em **modo demo** com dados
fictícios e um aviso discreto de "dados de exemplo".

### Formato dos dados (após descriptografar)

- `campanhas.json` = objeto com uma chave por período (`hoje`, `ontem`, `hoje_ontem`,
  `3d`, `7d`, `14d`, `30d`, `mes`, `mes_passado`, `max`). Cada período tem:
  `{ label, since, until, kpis, prev, split, niveis: {campanhas, conjuntos, anuncios}, ig }`.
- Cada linha de nível: `{ nome, sub, tipo (camp|imp), spend, cadastros, custoCadastro,
  conversas, linkClicks, custoClique, pageViews, cpm, ctr, freq, reach, impressions, dias }`.
- `ig` (por período): `{ novosSeguidores, prevNovos, custoSeguidor, profileViews,
  reachOrg, websiteClicks }`.

---

## Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | O que é |
| --- | --- |
| `META_ACCESS_TOKEN` | System User token do Meta. Escopos: `ads_read`, `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement`, `pages_show_list`. Sem expiração. |
| `META_AD_ACCOUNT_ID` | ID da conta de anúncios, com prefixo `act_` (ex: `act_161375005284674`) |
| `DASH_PASSWORD` | Senha que a cliente usa para abrir o dashboard |

O workflow também precisa de **permissão de escrita**:
Settings → Actions → General → *Workflow permissions* → **Read and write permissions**.

> **Segurança:** cadastre os secrets sensíveis pelo prompt oculto do `gh`, nunca na linha
> de comando. Ex.: `gh secret set META_ACCESS_TOKEN -R DreamsOFC/dash-fernanda-magalhaes`
> e cole o valor quando aparecer `? Paste your secret:` (não fica no histórico do terminal).

---

## Reaproveitar para outro cliente

Este projeto foi feito para ser clonado por cliente:

1. Duplique o repositório (ou crie um novo a partir destes arquivos).
2. Troque os secrets `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID` e `DASH_PASSWORD`.
3. Ajuste o nome no cabeçalho de `docs/index.html` (`<h1>` e o `<title>`).
4. Ative o GitHub Pages apontando para `/docs` na branch `main`.

Nenhum código muda entre clientes — só os secrets e o nome exibido. O ID da conta é lido
de `META_AD_ACCOUNT_ID`; o padrão no `coleta.py` existe apenas como fallback.

---

## Rodar a coleta localmente (opcional, para testar)

```bash
cp .env.example .env      # preencha os 3 valores
pip install -r scripts/requirements.txt
# carregue o .env no ambiente e rode:
python scripts/coleta.py
```

Os JSONs criptografados aparecem em `docs/data/`. Sirva `docs/` com qualquer servidor
estático (`python -m http.server` dentro de `docs/`) e abra no navegador.

---

## Se o workflow começar a falhar

Abra a aba **Actions** → clique na execução **vermelha** → veja o passo "Coletar dados".
Causas mais comuns (a mensagem traz o **código de erro** do Meta):

- **Token expirado ou revogado** → erro `190`. Gere um novo System User token e atualize
  `META_ACCESS_TOKEN`.
- **Sem permissão / escopo faltando** → erro `10`, `100` ou `200`. Confirme que o System
  User tem acesso à conta de anúncios (e à Página/Instagram) e os escopos listados acima.
- **Rate limit** → códigos `4`, `17`, `32` ou `613`. O script já tenta de novo com
  backoff; se persistir, espere e redispare.
- **ID da conta errado** → confira `META_AD_ACCOUNT_ID` (precisa do `act_`).
- **Instagram indisponível** → a coleta de anúncios continua normal; só a seção do
  Instagram some. Verifique os escopos de Instagram no token.

A coleta é **à prova de falha**: se qualquer chamada falhar, o script aborta antes de
escrever — o dashboard continua mostrando o **último dado bom** já commitado.
