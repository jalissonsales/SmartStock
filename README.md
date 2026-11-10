# SmartStock AI

Aplicacao Streamlit para leitura de planilhas, interpretacao automatica de colunas, dashboard operacional, perguntas sobre a base e sugestao de pedido.

## Como executar

1. Crie um ambiente virtual e instale as dependências (recomendado no Windows, evita conflitos com outros projetos):

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

2. Crie `.streamlit/secrets.toml` com a chave da API Gemini:

```toml
GOOGLE_API_KEY = "sua-chave-aqui"
```

> O arquivo antigo `apiKey.txt` não é mais lido pelo app e está no `.gitignore`.

2.1 Configure o Supabase (banco, login e conversas):

**Passo 1:** Execute o script SQL no Supabase
- Abra [Supabase Console](https://app.supabase.com)
- Vá para SQL Editor → Crie uma query nova
- Copie todo o conteúdo de `database_setup.sql` e execute
- Verifique se aparece "Success" (em verde) para todas as queries

**Passo 2:** Configure as credenciais em `.streamlit/secrets.toml`:

```toml
SUPABASE_URL = "https://seu-projeto.supabase.co"
SUPABASE_KEY = "sua-chave-anon-aqui"
APP_URL = "http://localhost:8501"
```

**Como obter as credenciais:**
- Acesse https://app.supabase.com/project/[seu-projeto]/settings/api
- Copie `Project URL` e coloque em `SUPABASE_URL`
- Copie `anon public` e coloque em `SUPABASE_KEY`

**`APP_URL`:** é a URL pública do app. Em produção use `https://seu-dominio.com`
(sem barra no final). Se não definir, o app tenta usar os cabeçalhos HTTP;
em último caso assume `http://localhost:8501`.

**Passo 3:** Em **Authentication → URL Configuration**:
- **Site URL:** `http://localhost:8501` (ou a URL de produção)
- **Redirect URLs:** adicione `http://localhost:8501/**` e a versão de produção
  `https://seu-dominio.com/**`. O `/**` é necessário para os parâmetros
  `?flow=...&state=...` usados no login Google e na recuperação de senha.

**Passo 4 (login com Google):**
1. No [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
   crie um **OAuth Client ID** do tipo *Web application*.
2. Em **Authorized redirect URIs**, cadastre
   `https://SEU-PROJETO.supabase.co/auth/v1/callback`.
3. No Supabase, vá em **Authentication → Providers → Google**, ative o provedor
   e cole o **Client ID** e o **Client Secret**.
4. Em **Authentication → Providers → Email**, mantenha **Confirm email** ativo.
   O link de confirmação leva o usuário de volta ao app para entrar.

**Resultado:** Ao criar uma conta e fazer login (Google ou e-mail/senha), o perfil
do cliente (nome, e-mail, data de cadastro e último acesso) é salvo automaticamente
no Supabase, nas tabelas `profiles` e `login_events`. As conversas com o assistente
também ficam salvas por usuário em `chat_sessions` e `chat_messages` (cada cliente
só acessa as próprias conversas via RLS).

**Recuperação de senha:** disponível em “Esqueci minha senha” na tela de login.
O app envia o link pelo Supabase com PKCE e abre a tela “Definir nova senha”.
O link expira conforme a configuração do Supabase; se expirar, solicite outro.
Como o `code_verifier` fica no processo do servidor, reinícios do servidor ou
múltiplos workers invalidam links pendentes (o app orienta solicitar um novo).


**Segurança (o que já está aplicado):**
- O app usa apenas a chave `anon`/`publishable`; chaves `service_role`/`secret` são detectadas e bloqueadas com aviso.
- RLS habilitado em `profiles`, `login_events`, `chat_sessions` e `chat_messages`: cada usuário só lê/escreve os próprios registros.
- As políticas também validam que o `email` gravado é o mesmo do token (`auth.jwt()`).
- Com o Supabase configurado, o login acontece **somente** no Supabase (sem bypass local e sem credenciais padrão).
- Senhas são gerenciadas pelo Supabase Auth (hash com bcrypt no servidor); novos cadastros exigem no mínimo 12 caracteres.
- O fallback local (somente sem Supabase) usa PBKDF2-SHA256 com 600.000 iterações e salt por usuário.
- Bloqueio temporário após 5 tentativas de login inválidas + mensagens de erro genéricas.
- Login Google e recuperação de senha usam OAuth/PKCE com `state` aleatório, `code_verifier` guardado no servidor e expiração de 10 minutos.
- Sessão expira após 120 minutos de inatividade ou 12 horas de uso contínuo.
- Todo texto vindo do usuário/planilha é escapado antes de entrar em HTML (previne XSS).
- Respostas da IA têm HTML, imagens e links removidos antes da renderização.
- Uploads limitados a 25 MB por arquivo, 60 MB no total, 10 arquivos, 200 colunas e 200.000 linhas.
- Mensagens de erro não exibem URLs, chaves, e-mails ou detalhes internos do banco.
- Prompts da IA tratam planilhas e conversas como dados não confiáveis (proteção contra prompt injection).
- Exportações CSV/Excel neutralizam fórmulas maliciosas (CSV formula injection).
- SDK de IA atual e suportado (`google-genai`); o pacote antigo `google-generativeai` (descontinuado) foi removido.
- Dependências fixadas em `requirements.txt` e verificadas com `pip-audit` (sem vulnerabilidades conhecidas).
- Opções seguras em `.streamlit/config.toml`; `.streamlit/secrets.toml`, `.venv/` e logs estão no `.gitignore`.

**Privacidade:** para responder perguntas e sugerir mapeamentos, uma amostra da planilha e o texto da pergunta são enviados à API do Google Gemini. Não envie dados pessoais sensíveis sem base legal e aviso aos titulares (LGPD).

**Checklist para produção:**
1. Mantenha **Authentication → Providers → Email → Confirm email** ativo.
2. Configure um **SMTP próprio** (Resend, SendGrid, Gmail etc.) em **Authentication → SMTP Settings** (o envio embutido é limitado e só serve para testes).
3. Em **Authentication → URL Configuration**, defina o **Site URL** e as **Redirect URLs** (`https://seu-dominio.com/**`). Defina `APP_URL` no `secrets.toml` com a mesma URL.
4. Ative o provedor **Google** em **Authentication → Providers → Google** e cadastre `https://SEU-PROJETO.supabase.co/auth/v1/callback` no Google Cloud.
5. Revise **Authentication → Rate Limits** e a expiração do JWT (**Authentication → Sessions**).
6. Ative backups do banco (**Database → Backups**; PITR no plano Pro). No plano Free, exporte as tabelas manualmente.
7. Se a chave for exposta, rotacione em **Settings → API Keys** e atualize o `secrets.toml`.
8. Rode o `database_setup.sql` novamente sempre que as políticas RLS mudarem.
9. Em produção, sirva o app via HTTPS e mantenha `SUPABASE_URL`/`GOOGLE_API_KEY` apenas nos secrets da plataforma.
10. Se usar múltiplos workers, os links pendentes de login/recuperação podem cair em outro processo; nesse caso use um único worker ou armazenamento compartilhado.


3. Rode a aplicação com o Python do ambiente virtual:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

> Também funciona ativando o ambiente antes: `.\.venv\Scripts\Activate.ps1` e depois `python -m streamlit run app.py`.

## Publicar no GitHub e gerar uma URL pública

O repositório já está configurado com o remote `https://github.com/jalissonsales/SmartStock.git`.
Para enviar a versão atual:

```powershell
git status
git add .
git commit -m "feat: login Supabase/Google, historico de conversas e UI/UX"
git push origin main
```

> **Nunca** faça commit de `.streamlit/secrets.toml`. Confirme com `git status` que ele
> não aparece. Use `.streamlit/secrets.toml.example` como modelo.

### Opção recomendada: Streamlit Community Cloud (grátis)

1. Acesse <https://share.streamlit.io> e entre com sua conta do GitHub.
2. Clique em **Create app** → **Deploy a public app from GitHub**.
3. Selecione o repositório `jalissonsales/SmartStock`, branch `main` e arquivo `app.py`.
4. Em **Advanced settings**:
   - Escolha **Python 3.13** (ou 3.12).
   - Em **Secrets**, cole o conteúdo abaixo trocando os valores reais:

```toml
GOOGLE_API_KEY = "sua-chave-do-google-ai-studio"
SUPABASE_URL = "https://SEU-PROJETO.supabase.co"
SUPABASE_KEY = "sua-chave-anon-ou-publishable"
APP_URL = "https://SEU-APP.streamlit.app"
```

5. Clique em **Deploy**. O primeiro build leva alguns minutos.
6. Copie a URL gerada (algo como `https://smartstock.streamlit.app`).
7. Volte em **Settings → Secrets**, ajuste `APP_URL` para essa URL (sem barra no final)
   e clique em **Reboot app**.

> No plano gratuito o app hiberna após um período sem acesso; a primeira visita
> depois disso pode demorar ~30 segundos para acordar.

### Se aparecer `ModuleNotFoundError` no Streamlit Cloud

Isso normalmente acontece quando o app foi criado antes de um commit que adicionou
uma dependência nova (por exemplo, `supabase`) ou quando o `requirements.txt` mudou
e o ambiente ainda não foi reinstalado.

1. Na página do app, clique em **Manage app** → menu **⋯** → **Reboot app**.
2. Aguarde o build; o log deve mostrar `Installing dependencies` e a instalação do
   `supabase`.
3. Sempre que alterar o `requirements.txt`, faça um **Reboot app**.
4. A versão do Python é escolhida na criação e **não pode ser alterada depois**.
   Se o log mostrar uma versão incompatível (por exemplo, 3.14 com pacotes antigos),
   exclua o app e crie novamente escolhendo **Python 3.13**.

### Configurar o Supabase para a URL pública

Em **Authentication → URL Configuration**:

- **Site URL:** `https://SEU-APP.streamlit.app`
- **Redirect URLs:** mantenha `http://localhost:8501/**` e adicione:
  - `https://SEU-APP.streamlit.app/**`

O `/**` é necessário para os parâmetros `?flow=google&state=...` e `?code=...`
usados no login Google e na recuperação de senha.

### Configurar o Google Cloud

O **Authorized redirect URI** continua sendo o callback do Supabase:

```text
https://SEU-PROJETO.supabase.co/auth/v1/callback
```

Não troque para a URL do Streamlit Cloud. Opcionalmente, adicione
`https://SEU-APP.streamlit.app` em **Authorized JavaScript origins**.

### Testar em produção

1. Abra a URL pública e faça login com Google e com e-mail/senha.
2. Envie uma planilha, confirme o mapeamento e converse com o assistente.
3. Crie, renomeie e exclua uma conversa; recarregue a página e confirme a persistência.
4. Teste “Esqueci minha senha” e o retorno do link para a URL pública.

### Atualizações futuras

Cada `git push` na branch `main` dispara um novo deploy automático no Streamlit
Community Cloud. Se alterar tabelas ou políticas, rode novamente o
`database_setup.sql` no Supabase.

### Alternativas

- **Render / Railway / Fly.io:** execute
  `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`
  e monte o `secrets.toml` como arquivo secreto no serviço.
- **Servidor próprio:** use HTTPS (Nginx/Caddy como proxy reverso) e mantenha
  `secrets.toml` fora do repositório.

## O que a ferramenta faz

- Le CSV e XLSX (várias planilhas consolidadas em uma única base)
- Detecta encoding e separador de CSV
- Tenta interpretar automaticamente as colunas e permite revisar o mapeamento
- Mostra um fluxo guiado em 3 etapas: upload, mapeamento e análise
- Normaliza a base para perguntas e dashboards
- Responde perguntas numéricas com cálculo local sempre que possível
- Mantém histórico de conversas por usuário, com criar, renomear e excluir
- Gera sugestão de pedido com filtros por produto, fornecedor, status e prioridade
- Exporta dashboard, produtos críticos e pedido sugerido em CSV/Excel
- Login com Google ou conta por e-mail/senha, com recuperação de senha

## Observacoes

- Se a IA nao estiver disponivel, a aplicacao continua funcionando nas analises locais
- O painel `Como a base foi interpretada` ajuda a validar o mapeamento e a base normalizada
