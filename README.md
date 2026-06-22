# SmartStock AI

Aplicacao Streamlit para leitura de planilhas, interpretacao automatica de colunas, dashboard operacional, perguntas sobre a base e sugestao de pedido.

## Como executar

1. Instale as dependencias:

```bash
pip install -r requirements.txt
```

2. Informe sua chave da API Gemini em `apiKey.txt`:

```txt
SUA_CHAVE_AQUI
```

2.1 (Opcional) Configure Supabase para integração de banco de dados:

**Passo 1:** Execute o script SQL no Supabase
- Abra [Supabase Console](https://app.supabase.com)
- Vá para SQL Editor → Crie uma query nova
- Copie todo o conteúdo de `database_setup.sql` e execute
- Verifique se aparece "Success" (em verde) para todas as queries

**Passo 2:** Configure as credenciais em `.streamlit/secrets.toml`:

```toml
SUPABASE_URL = "https://seu-projeto.supabase.co"
SUPABASE_KEY = "sua-chave-anon-aqui"
```

**Como obter as credenciais:**
- Acesse https://app.supabase.com/project/[seu-projeto]/settings/api
- Copie `Project URL` e coloque em `SUPABASE_URL`
- Copie `anon public` e coloque em `SUPABASE_KEY`

**Resultado:** Ao fazer upload de uma planilha, os dados serão salvos automaticamente no Supabase.


3. Rode a aplicacao:

```bash
streamlit run app.py
```

## O que a ferramenta faz

- Le CSV e XLSX
- Detecta encoding e separador de CSV
- Tenta interpretar automaticamente as colunas
- Permite revisao manual do mapeamento
- Normaliza a base para perguntas e dashboards
- Responde perguntas numericas com calculo local sempre que possivel
- Gera sugestao de pedido quando a base contem informacoes suficientes

## Observacoes

- Se a IA nao estiver disponivel, a aplicacao continua funcionando nas analises locais
- O painel `Como a base foi interpretada` ajuda a validar o mapeamento e a base normalizada
