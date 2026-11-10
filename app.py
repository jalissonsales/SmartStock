import base64
import csv
import hashlib
import hmac
import html
import importlib.util
import json
import os
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from supabase import create_client


st.set_page_config(page_title="SmartStock AI", layout="wide", menu_items={"Get Help": None, "Report a Bug": None, "About": None})


BASE_DIR = Path(__file__).resolve().parent
UTILS_PATH = BASE_DIR / "utils.py"
CSS_PATH = BASE_DIR / "style.css"
LOGO_PATH = BASE_DIR / "smartstock_ai_logo-removebg-preview.png"
UPLOAD_SCHEMA_VERSION = "2026-04-08-clean-pivot-v2"
USERS_FILE = BASE_DIR / ".users.json"
CHAT_GREETING = {
    "role": "assistant",
    "content": "Envie sua planilha para eu analisar estoque, gerar dashboards e sugerir pedidos inteligentes.",
}


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
PBKDF2_ITERATIONS = 600_000

# Limites de entrada e de sessão (defesa contra abuso e uploads maliciosos)
MAX_UPLOAD_MB = 25
MAX_TOTAL_UPLOAD_MB = 60
MAX_UPLOAD_FILES = 10
MAX_DATA_ROWS = 200_000
MAX_DATA_COLUMNS = 200
ALLOWED_UPLOAD_SUFFIXES = {".csv", ".xlsx"}
MAX_CHAT_QUESTION = 1_000
MAX_CHAT_CONTENT = 8_000
SESSION_IDLE_MINUTES = 120
SESSION_MAX_HOURS = 12
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300


def escape_html(value) -> str:
    """Escapa qualquer valor antes de entrar em um bloco unsafe_allow_html."""
    return html.escape(str(value if value is not None else ""), quote=True)


def sanitize_input(value: str, max_length: int = MAX_CHAT_QUESTION) -> str:
    """Remove caracteres de controle e limita o tamanho de textos vindos do usuário."""
    text = str(value or "")
    text = "".join(
        char for char in text if char in "\n\t" or unicodedata.category(char)[0] != "C"
    )
    return text.strip()[:max_length]


def sanitize_output(value: str, max_length: int = MAX_CHAT_CONTENT) -> str:
    """Remove HTML/imagens/links de respostas da IA para evitar injeção e rastreadores."""
    text = str(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)", r"\1", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text[:max_length].strip()


def sanitize_error_message(exc: Exception | str, fallback: str) -> str:
    """Evita vazar URLs, chaves e e-mails em mensagens de erro exibidas na tela."""
    text = str(exc or "").strip()
    if not text:
        return fallback
    text = re.sub(r"https?://\S+", "[endereço removido]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:sb_[A-Za-z0-9_-]+|eyJ[A-Za-z0-9._-]+)\b", "[credencial removida]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[e-mail removido]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240] or fallback


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_export_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Neutraliza fórmulas em CSV/Excel (CSV formula injection)."""
    export_df = df.copy()
    for column in export_df.columns:
        if pd.api.types.is_numeric_dtype(export_df[column]):
            continue
        export_df[column] = export_df[column].map(
            lambda value: "'" + value
            if isinstance(value, str) and value.startswith(FORMULA_PREFIXES)
            else value
        )
    return export_df



def is_valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.match((email or "").strip()))


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt, expected = stored.split("$", 3)
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), salt.encode(), int(iterations)
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(digest.hex(), expected)

    # Compatibilidade com hashes SHA-256 antigos (migrados no próximo login)
    legacy = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(legacy, stored)


# Hash descartável usado para igualar o tempo de resposta quando o e-mail não existe.
_DUMMY_PASSWORD_HASH = _hash_password(secrets.token_urlsafe(24))


def _login_lock_remaining() -> int:
    until = float(st.session_state.get("login_locked_until") or 0)
    return max(0, int(until - time.time()))


def _register_login_failure() -> None:
    attempts = int(st.session_state.get("login_attempts") or 0) + 1
    st.session_state.login_attempts = attempts
    if attempts >= LOGIN_MAX_ATTEMPTS:
        st.session_state.login_locked_until = time.time() + LOGIN_LOCKOUT_SECONDS
        st.session_state.login_attempts = 0


def _reset_login_failures() -> None:
    st.session_state.login_attempts = 0
    st.session_state.login_locked_until = 0


def load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_user(email: str, password: str, name: str) -> None:
    users = load_users()
    users[email.strip().lower()] = {
        "password": _hash_password(password),
        "name": sanitize_input(name, max_length=120) or email,
    }
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def _supabase_key_is_privileged(key: str) -> bool:
    """Detecta chaves que nunca devem rodar no cliente (service_role/secret)."""
    key = (key or "").strip()
    if key.startswith("sb_secret_"):
        return True
    if key.startswith("eyJ") and key.count(".") == 2:
        try:
            payload = key.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
            return str(data.get("role", "")).lower() == "service_role"
        except Exception:
            return False
    return False


def supabase_is_configured() -> bool:
    try:
        url = _normalize_supabase_url(st.secrets.get("SUPABASE_URL", ""))
        key = st.secrets.get("SUPABASE_KEY", "").strip()
    except Exception:
        return False
    return bool(url and key)


def _auth_error_message(error: str) -> str:
    text = (error or "").lower()
    if "email not confirmed" in text:
        return "Confirme seu e-mail antes de entrar. Verifique sua caixa de entrada."
    if "rate limit" in text or "too many requests" in text:
        return "Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente."
    if any(
        token in text
        for token in (
            "failed to establish",
            "connection",
            "timed out",
            "timeout",
            "getaddrinfo",
            "network",
            "name or service not known",
        )
    ):
        return "Não foi possível conectar ao Supabase. Verifique sua internet e as configurações."
    return "E-mail ou senha inválidos."


def _signup_error_message(error: str) -> str:
    text = (error or "").lower()
    if "already registered" in text or "already exists" in text or "user already" in text:
        return "Não foi possível criar a conta com esses dados. Se já tem cadastro, faça login."
    if "rate limit" in text or "too many requests" in text or "too many" in text:
        return "Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente."
    if "password" in text and ("weak" in text or "least" in text or "short" in text):
        return f"A senha não atende aos requisitos mínimos ({PASSWORD_MIN_LENGTH} caracteres)."
    if "email" in text and ("invalid" in text or "valid" in text):
        return "Informe um e-mail válido."
    return sanitize_error_message(error, "Não foi possível criar a conta. Tente novamente.")


def validate_login(email: str, password: str) -> bool:
    if not email or not password:
        return False

    remaining = _login_lock_remaining()
    if remaining > 0:
        st.session_state.login_error = (
            f"Muitas tentativas. Tente novamente em {remaining} segundo(s)."
        )
        return False

    email = email.strip().lower()[:254]
    password = password[:PASSWORD_MAX_LENGTH]

    # Com o Supabase configurado, a autenticação acontece apenas nele.
    # O arquivo local é usado somente como fallback de desenvolvimento.
    if supabase_is_configured():
        sign_in_result, sign_in_error = supabase_sign_in(email, password)
        if sign_in_result is not None:
            user = getattr(sign_in_result, "user", None)
            st.session_state.supabase_user = (
                user.model_dump() if hasattr(user, "model_dump") else None
            )
            st.session_state.login_error = None
            st.session_state.session_started_at = time.time()
            st.session_state.last_activity_at = time.time()
            _reset_login_failures()
            return True
        st.session_state.login_error = _auth_error_message(sign_in_error)
        _register_login_failure()
        return False

    users = load_users()
    user = users.get(email)
    if not user:
        # Comparação descartável para não revelar quais e-mails existem.
        _verify_password(password, _DUMMY_PASSWORD_HASH)
        st.session_state.login_error = "E-mail ou senha inválidos."
        _register_login_failure()
        return False

    if not _verify_password(password, user.get("password", "")):
        st.session_state.login_error = "E-mail ou senha inválidos."
        _register_login_failure()
        return False

    if not str(user.get("password", "")).startswith("pbkdf2_sha256$"):
        save_user(email, password, user.get("name", email))

    st.session_state.supabase_user = None
    st.session_state.login_error = None
    st.session_state.session_started_at = time.time()
    st.session_state.last_activity_at = time.time()
    _reset_login_failures()
    return True


def _normalize_supabase_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/rest/v1", "/auth/v1", "/storage/v1"):
        if url.endswith(suffix):
            url = url[:-len(suffix)].rstrip("/")
    return url


def get_supabase_client():
    cached = st.session_state.get("supabase_client")
    if cached is not None:
        return cached
    try:
        url = _normalize_supabase_url(st.secrets.get("SUPABASE_URL", ""))
        key = st.secrets.get("SUPABASE_KEY", "").strip()
        if not url or not key or _supabase_key_is_privileged(key):
            return None
        client = create_client(url, key)
        st.session_state.supabase_client = client
        return client
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def _probe_supabase(url: str, key: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        f"{url}/rest/v1/profiles?select=id&limit=1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "Supabase recusou a chave de acesso. Confira SUPABASE_KEY no secrets.toml (use a chave anon/publishable)."
        if exc.code == 404:
            return False, "Supabase conectado, mas a tabela 'profiles' não existe. Rode o database_setup.sql no SQL Editor."
        return False, f"Supabase respondeu com erro HTTP {exc.code}. Confira a URL e a chave."
    except Exception as exc:
        detail = sanitize_error_message(exc, "falha de rede")
        return False, f"Não foi possível conectar ao Supabase: {detail}"


def get_supabase_status() -> tuple[bool, str]:
    try:
        url = _normalize_supabase_url(st.secrets.get("SUPABASE_URL", ""))
        key = st.secrets.get("SUPABASE_KEY", "").strip()
    except Exception:
        url = key = ""

    if not url or not key:
        return False, "Supabase não configurado. Defina SUPABASE_URL e SUPABASE_KEY em .streamlit/secrets.toml."

    if _supabase_key_is_privileged(key):
        return False, (
            "SUPABASE_KEY inválida: a chave configurada é privilegiada (service_role/secret). "
            "Use apenas a chave anon/publishable."
        )

    if not url.startswith(("https://", "http://")):
        return False, "SUPABASE_URL inválida. Use a Project URL (ex.: https://xxxx.supabase.co)."

    return _probe_supabase(url, key)


def sync_supabase_profile(client, user, register_login: bool = False) -> None:
    if client is None or user is None:
        return
    try:
        user_id = getattr(user, "id", None)
        if not user_id:
            return

        st.session_state.user_id = str(user_id)

        email = sanitize_input(getattr(user, "email", "") or "", max_length=254)
        metadata = getattr(user, "user_metadata", {}) or {}
        name = sanitize_input(metadata.get("name") or email, max_length=120) or email

        profile = {"id": user_id, "name": name, "email": email}
        if register_login:
            profile["last_login_at"] = datetime.now(timezone.utc).isoformat()

        client.table("profiles").upsert(profile, on_conflict="id").execute()

        if register_login:
            client.table("login_events").insert(
                {"user_id": user_id, "email": email}
            ).execute()

        st.session_state.user_profile = profile
        st.session_state.profile_error = None
    except Exception as exc:
        st.session_state.profile_error = sanitize_error_message(
            exc, "Não foi possível salvar o perfil no Supabase."
        )


def supabase_sign_up(email: str, password: str, name: str):
    if not supabase_is_configured():
        return None, "Supabase não configurado."
    client = get_supabase_client()
    if client is None:
        return None, "Não foi possível inicializar o cliente Supabase. Confira SUPABASE_URL e SUPABASE_KEY."
    email = sanitize_input(email, max_length=254).lower()
    name = sanitize_input(name, max_length=120) or email
    password = str(password or "")[:PASSWORD_MAX_LENGTH]
    try:
        response = client.auth.sign_up(
            {
                "email": email,
                "password": password,
                "options": {"data": {"name": name}},
            }
        )
        if getattr(response, "session", None) is not None:
            sync_supabase_profile(client, response.user)
        return response, None
    except Exception as exc:
        return None, _signup_error_message(str(exc))


def supabase_sign_in(email: str, password: str):
    if not supabase_is_configured():
        return None, "Supabase não configurado."
    client = get_supabase_client()
    if client is None:
        return None, "Não foi possível inicializar o cliente Supabase. Confira SUPABASE_URL e SUPABASE_KEY."
    email = sanitize_input(email, max_length=254).lower()
    password = str(password or "")[:PASSWORD_MAX_LENGTH]
    try:
        response = client.auth.sign_in_with_password({"email": email, "password": password})
        sync_supabase_profile(client, response.user, register_login=True)
        return response, None
    except Exception as exc:
        return None, sanitize_error_message(exc, "Falha na autenticação.")


# Fluxos OAuth/PKCE pendentes (state -> verifier). Ficam no processo do servidor,
# nunca no navegador, e expiram em 10 minutos. Usamos cache_resource porque o
# Streamlit reexecuta o script a cada rerun e variáveis globais seriam perdidas.
_AUTH_PENDING_TTL = 600


@st.cache_resource(show_spinner=False)
def _auth_pending_store() -> dict[str, dict]:
    return {}


def _supabase_credentials() -> tuple[str, str]:
    try:
        url = _normalize_supabase_url(st.secrets.get("SUPABASE_URL", ""))
        key = st.secrets.get("SUPABASE_KEY", "").strip()
    except Exception:
        return "", ""
    return url, key


def _app_base_url() -> str:
    """URL pública usada como redirect do Supabase (configure APP_URL em produção)."""
    try:
        configured = str(st.secrets.get("APP_URL", "")).strip().rstrip("/")
    except Exception:
        configured = ""
    if configured.startswith(("https://", "http://")):
        return configured

    host = ""
    proto = "http"
    try:
        headers = st.context.headers
        host = (
            headers.get("X-Forwarded-Host")
            or headers.get("Host")
            or headers.get("host")
            or ""
        ).strip()
        proto = (
            headers.get("X-Forwarded-Proto")
            or headers.get("x-forwarded-proto")
            or "http"
        ).strip().lower()
    except Exception:
        pass

    # Host vem do cabeçalho HTTP: aceita apenas o formato esperado.
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", host or ""):
        host = "localhost:8501"
    if proto not in ("http", "https"):
        proto = "http"
    return f"{proto}://{host}".rstrip("/")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def _prune_auth_pending() -> None:
    store = _auth_pending_store()
    now = time.time()
    expired = [
        key
        for key, value in store.items()
        if now - float(value.get("created_at", 0)) > _AUTH_PENDING_TTL
    ]
    for key in expired:
        store.pop(key, None)


def _remember_auth_pending(flow: str, verifier: str) -> str:
    _prune_auth_pending()
    state = secrets.token_urlsafe(24)
    _auth_pending_store()[state] = {
        "flow": flow,
        "verifier": verifier,
        "created_at": time.time(),
    }
    return state


def _pop_auth_pending(state: str | None, flow: str | None = None) -> dict | None:
    """Consome o fluxo pendente. O state é obrigatório sempre que enviado."""
    _prune_auth_pending()
    store = _auth_pending_store()
    if state:
        return store.pop(state, None)
    if flow:
        # Alguns provedores removem query params do redirect_to. Nesse caso,
        # aceitamos apenas o fluxo mais recente daquele tipo.
        candidates = [
            (key, value)
            for key, value in store.items()
            if value.get("flow") == flow
        ]
        if candidates:
            key, value = max(candidates, key=lambda item: item[1].get("created_at", 0))
            store.pop(key, None)
            return value
    return None


def _auth_redirect_url(flow: str, state: str) -> str:
    base = _app_base_url()
    separator = "&" if "?" in base else "?"
    return (
        f"{base}{separator}flow={urllib.parse.quote(flow)}"
        f"&state={urllib.parse.quote(state)}"
    )


def build_google_oauth_url() -> str | None:
    """Monta a URL de autorização do Google com PKCE e state anti-CSRF."""
    if not supabase_is_configured():
        return None

    cached = st.session_state.get("google_oauth_url")
    cached_at = st.session_state.get("google_oauth_url_at") or 0
    if cached and time.time() - float(cached_at) < 300:
        return cached

    url, _ = _supabase_credentials()
    if not url:
        return None

    verifier, challenge = _pkce_pair()
    state = _remember_auth_pending("google", verifier)
    query = urllib.parse.urlencode(
        {
            "provider": "google",
            "redirect_to": _auth_redirect_url("google", state),
            "code_challenge": challenge,
            "code_challenge_method": "s256",
            "scopes": "email profile",
        }
    )
    oauth_url = f"{url}/auth/v1/authorize?{query}"
    st.session_state.google_oauth_url = oauth_url
    st.session_state.google_oauth_url_at = time.time()
    return oauth_url


def request_password_reset(email: str) -> tuple[bool, str]:
    """Solicita link de recuperação via Supabase usando PKCE."""
    if not supabase_is_configured():
        return False, "Supabase não configurado. A recuperação de senha exige Supabase."
    url, key = _supabase_credentials()
    if not url or not key:
        return False, "Não foi possível ler as credenciais do Supabase."

    verifier, challenge = _pkce_pair()
    state = _remember_auth_pending("recovery", verifier)
    # A API /recover espera o PKCE no corpo JSON (não na query string).
    query = urllib.parse.urlencode(
        {"redirect_to": _auth_redirect_url("recovery", state)}
    )
    body = json.dumps(
        {
            "email": email,
            "code_challenge": challenge,
            "code_challenge_method": "s256",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/auth/v1/recover?{query}",
        data=body,
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        return True, "Se este e-mail estiver cadastrado, enviaremos um link para redefinir a senha."
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return False, "Muitas solicitações em pouco tempo. Aguarde alguns minutos e tente novamente."
        if exc.code in (400, 404, 422):
            return True, "Se este e-mail estiver cadastrado, enviaremos um link para redefinir a senha."
        return False, "Não foi possível solicitar a recuperação agora. Tente novamente em instantes."
    except Exception:
        return False, "Não foi possível conectar ao Supabase. Verifique sua internet e tente novamente."


def _oauth_error_message(error: str) -> str:
    text = (error or "").lower()
    if "access_denied" in text or "cancel" in text:
        return "Login com Google cancelado."
    if "provider" in text and ("not enabled" in text or "unsupported" in text or "disabled" in text):
        return (
            "O login com Google ainda não está habilitado no Supabase. "
            "Ative o provedor em Authentication > Providers > Google."
        )
    if "redirect" in text and ("url" in text or "uri" in text):
        return (
            "URL de retorno não autorizada. Cadastre a URL do app em "
            "Authentication > URL Configuration no Supabase."
        )
    return sanitize_error_message(error, "Não foi possível concluir o login com Google.")


def _query_param(name: str) -> str | None:
    try:
        value = st.query_params.get(name)
    except Exception:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return str(value) if value else None


def _exchange_error_message(error: Exception | str) -> str:
    text = (error or "").lower()
    if "invalid flow state" in text or "code challenge" in text or "invalid grant" in text:
        return "O link de autenticação expirou ou já foi usado. Solicite um novo."
    if "expired" in text:
        return "O link de autenticação expirou. Solicite um novo."
    return sanitize_error_message(error, "Não foi possível concluir a autenticação. Tente novamente.")


def handle_auth_callback() -> str | None:
    """Processa o retorno do Google ou do link de recuperação de senha."""
    code = _query_param("code")
    error = _query_param("error")
    error_description = _query_param("error_description")
    if not code and not error:
        return None

    flow = _query_param("flow")
    state = _query_param("state")

    # Limpa os parâmetros para o código não ser reutilizado em um rerun.
    try:
        st.query_params.clear()
    except Exception:
        pass

    # Um callback consome o state/verifier. Invalidamos a URL em cache para que
    # uma nova tentativa (após erro ou logout) gere um PKCE novo.
    st.session_state.google_oauth_url = None
    st.session_state.google_oauth_url_at = None

    if error:
        st.session_state.auth_notice = _oauth_error_message(error_description or error)
        st.session_state.auth_view = "login"
        return "error"

    pending = _pop_auth_pending(state, flow)
    if pending is None:
        st.session_state.auth_notice = (
            "O link de autenticação expirou ou já foi usado. Solicite um novo."
        )
        st.session_state.auth_view = "login"
        return "error"

    client = get_supabase_client()
    if client is None:
        st.session_state.auth_notice = (
            "Não foi possível inicializar o Supabase. Confira SUPABASE_URL e SUPABASE_KEY."
        )
        st.session_state.auth_view = "login"
        return "error"

    try:
        response = client.auth.exchange_code_for_session(
            {"auth_code": code, "code_verifier": pending["verifier"]}
        )
    except Exception as exc:
        st.session_state.auth_notice = _exchange_error_message(exc)
        st.session_state.auth_view = "login"
        return "error"

    session = getattr(response, "session", None)
    user = getattr(response, "user", None) or getattr(session, "user", None)
    if session is None or user is None:
        st.session_state.auth_notice = "A autenticação não retornou uma sessão válida."
        st.session_state.auth_view = "login"
        return "error"

    is_recovery = pending.get("flow") == "recovery"

    st.session_state.supabase_user = (
        user.model_dump() if hasattr(user, "model_dump") else None
    )
    st.session_state.session_started_at = time.time()
    st.session_state.last_activity_at = time.time()
    _reset_login_failures()
    sync_supabase_profile(client, user, register_login=not is_recovery)

    if is_recovery:
        st.session_state.authenticated = False
        st.session_state.password_recovery_mode = True
        st.session_state.auth_view = "recovery"
        return "recovery"

    st.session_state.authenticated = True
    st.session_state.password_recovery_mode = False
    st.session_state.auth_view = "login"
    st.session_state.auth_notice = "Login com Google realizado com sucesso."
    return "google"


def _get_user_id() -> str | None:
    user_id = st.session_state.get("user_id")
    if user_id:
        return str(user_id)
    user = st.session_state.get("supabase_user") or {}
    user_id = user.get("id")
    return str(user_id) if user_id else None


def _initials(name: str | None, email: str | None = None) -> str:
    """Gera até duas iniciais para o avatar do usuário."""
    source = (name or email or "?").strip()
    parts = [part for part in re.split(r"\s+", source) if part]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return source[:2].upper()


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_relative_time(value) -> str:
    """Formata datas de conversa de forma humana e curta."""
    parsed = _parse_datetime(value)
    if parsed is None:
        return ""
    now = datetime.now(timezone.utc)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return "agora"
    minutes = seconds // 60
    if minutes < 60:
        return f"há {minutes} min"
    hours = minutes // 60
    if hours < 24 and parsed.date() == now.date():
        return f"hoje, {parsed.astimezone().strftime('%H:%M')}"
    if parsed.date() == (now - timedelta(days=1)).date():
        return "ontem"
    days = (now.date() - parsed.date()).days
    if 1 < days < 7:
        return f"há {days} dias"
    return parsed.astimezone().strftime("%d/%m/%Y")


def group_chat_sessions(sessions: list[dict]) -> list[tuple[str, list[dict]]]:
    """Agrupa conversas por período mantendo a ordem recebida do Supabase."""
    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = (now - timedelta(days=1)).date()
    week_ago = (now - timedelta(days=7)).date()

    groups: dict[str, list[dict]] = {
        "Hoje": [],
        "Ontem": [],
        "Últimos 7 dias": [],
        "Mais antigas": [],
    }
    for session in sessions:
        parsed = _parse_datetime(session.get("updated_at"))
        if parsed is None:
            groups["Mais antigas"].append(session)
            continue
        day = parsed.date()
        if day == today:
            groups["Hoje"].append(session)
        elif day == yesterday:
            groups["Ontem"].append(session)
        elif day >= week_ago:
            groups["Últimos 7 dias"].append(session)
        else:
            groups["Mais antigas"].append(session)
    return [(title, items) for title, items in groups.items() if items]


def filter_chat_sessions(sessions: list[dict], term: str) -> list[dict]:
    term = sanitize_input(term, max_length=80).lower()
    if not term:
        return sessions
    return [
        session
        for session in sessions
        if term in str(session.get("title") or "Nova conversa").lower()
    ]


def has_confirmed_data() -> bool:
    return (
        st.session_state.get("df") is not None
        and bool(st.session_state.get("mapping_confirmed"))
    )


def open_chat_session(session_id: str) -> bool:
    """Abre uma conversa; sem dados, mostra o histórico em modo leitura."""
    if not load_chat_session(session_id):
        return False
    if has_confirmed_data():
        st.session_state.page = "AI Assistant"
        st.session_state.viewing_chat_id = None
    else:
        st.session_state.page = "Chat History"
        st.session_state.viewing_chat_id = session_id
    return True



def chat_persistence_available() -> bool:
    return supabase_is_configured() and _get_user_id() is not None


def get_or_create_chat_session(title: str | None = None) -> str | None:
    session_id = st.session_state.get("chat_session_id")
    if session_id:
        return session_id

    user_id = _get_user_id()
    client = get_supabase_client()
    if not user_id or client is None:
        return None

    try:
        response = (
            client.table("chat_sessions")
            .insert(
                {
                    "user_id": user_id,
                    "title": sanitize_input(title or "Nova conversa", max_length=80),
                }
            )
            .execute()
        )
        if not response.data:
            return None
        st.session_state.chat_session_id = response.data[0]["id"]
        return st.session_state.chat_session_id
    except Exception as exc:
        st.session_state.chat_error = sanitize_error_message(
            exc, "Falha ao criar a conversa no Supabase."
        )
        return None


def save_chat_message(role: str, content: str) -> None:
    if not chat_persistence_available():
        return

    user_id = _get_user_id()
    client = get_supabase_client()
    if not user_id or client is None:
        return

    content = sanitize_output(content, MAX_CHAT_CONTENT)
    if not content:
        return

    try:
        new_session = not st.session_state.get("chat_session_id")
        title = content if role == "user" and new_session else "Nova conversa"
        session_id = get_or_create_chat_session(title=title)
        if not session_id:
            return

        client.table("chat_messages").insert(
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": role,
                "content": content,
            }
        ).execute()
        client.table("chat_sessions").update(
            {"updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", session_id).eq("user_id", user_id).execute()
        invalidate_chat_sessions_cache()
        st.session_state.chat_error = None
    except Exception as exc:
        st.session_state.chat_error = sanitize_error_message(
            exc, "Falha ao salvar a mensagem no Supabase."
        )


def load_persisted_chat() -> None:
    if st.session_state.get("chat_history_loaded"):
        return
    if not chat_persistence_available():
        return

    st.session_state.chat_history_loaded = True
    sessions = list_chat_sessions(force=True)
    if not sessions:
        return
    load_chat_session(sessions[0]["id"])


def invalidate_chat_sessions_cache() -> None:
    st.session_state.chat_sessions_cache = None


def list_chat_sessions(force: bool = False) -> list[dict]:
    if not chat_persistence_available():
        return []

    cached = st.session_state.get("chat_sessions_cache")
    if cached is not None and not force:
        return cached

    user_id = _get_user_id()
    client = get_supabase_client()
    if not user_id or client is None:
        return []

    try:
        response = (
            client.table("chat_sessions")
            .select("id, title, updated_at")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(30)
            .execute()
        )
        sessions = []
        for item in response.data or []:
            sessions.append(
                {
                    "id": item.get("id"),
                    "title": sanitize_input(item.get("title") or "Nova conversa", max_length=80)
                    or "Nova conversa",
                    "updated_at": item.get("updated_at"),
                }
            )
        st.session_state.chat_sessions_cache = sessions
        st.session_state.chat_sessions_error = None
        return sessions
    except Exception as exc:
        st.session_state.chat_sessions_error = sanitize_error_message(
            exc, "Falha ao listar as conversas."
        )
        st.session_state.chat_sessions_cache = []
        return []


def load_chat_session(session_id: str) -> bool:
    if not chat_persistence_available() or not session_id:
        return False

    user_id = _get_user_id()
    client = get_supabase_client()
    if not user_id or client is None:
        return False

    try:
        owned = (
            client.table("chat_sessions")
            .select("id")
            .eq("id", session_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not owned.data:
            st.session_state.chat_error = "Conversa não encontrada."
            return False

        response = (
            client.table("chat_messages")
            .select("role, content, created_at")
            .eq("session_id", session_id)
            .order("created_at")
            .execute()
        )
        history = []
        for item in response.data or []:
            role = item.get("role")
            content = sanitize_output(str(item.get("content", "")), MAX_CHAT_CONTENT)
            if role in ("user", "assistant") and content:
                history.append({"role": role, "content": content})

        st.session_state.chat_session_id = session_id
        st.session_state.chat_history = history or [dict(CHAT_GREETING)]
        st.session_state.chat_history_loaded = True
        st.session_state.last_chat_context = {}
        st.session_state.chat_error = None
        return True
    except Exception as exc:
        st.session_state.chat_error = sanitize_error_message(
            exc, "Falha ao carregar a conversa do Supabase."
        )
        return False


def start_new_chat() -> None:
    st.session_state.chat_session_id = None
    st.session_state.chat_history = [dict(CHAT_GREETING)]
    st.session_state.chat_history_loaded = True
    st.session_state.last_chat_context = {}
    st.session_state.chat_error = None
    st.session_state.confirm_delete_chat = None
    st.session_state.viewing_chat_id = None


def rename_chat_session(session_id: str, title: str) -> bool:
    if not chat_persistence_available() or not session_id:
        return False

    client = get_supabase_client()
    if client is None:
        return False

    new_title = sanitize_input(title, max_length=80) or "Nova conversa"
    user_id = _get_user_id()
    try:
        client.table("chat_sessions").update({"title": new_title}).eq(
            "id", session_id
        ).eq("user_id", user_id).execute()
        invalidate_chat_sessions_cache()
        return True
    except Exception as exc:
        st.session_state.chat_error = sanitize_error_message(
            exc, "Falha ao renomear a conversa."
        )
        return False


def delete_chat_session(session_id: str) -> bool:
    if not chat_persistence_available() or not session_id:
        return False

    client = get_supabase_client()
    if client is None:
        return False

    try:
        user_id = _get_user_id()
        client.table("chat_sessions").delete().eq("id", session_id).eq(
            "user_id", user_id
        ).execute()
        invalidate_chat_sessions_cache()
        if st.session_state.get("chat_session_id") == session_id:
            start_new_chat()
        if st.session_state.get("viewing_chat_id") == session_id:
            st.session_state.viewing_chat_id = None
        return True
    except Exception as exc:
        st.session_state.chat_error = sanitize_error_message(
            exc, "Falha ao excluir a conversa."
        )
        return False


def load_utils_module():
    if not UTILS_PATH.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {UTILS_PATH}")

    spec = importlib.util.spec_from_file_location("smartstock_utils", UTILS_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Nao foi possivel carregar o modulo em {UTILS_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


utils_module = load_utils_module()
analisar_pergunta = utils_module.analisar_pergunta
configurar_gemini = utils_module.configurar_gemini
gerar_pedido_inteligente = utils_module.gerar_pedido_inteligente
responder_pergunta_local = utils_module.responder_pergunta_local
construir_base_normalizada = utils_module.construir_base_normalizada
gerar_resumo_quantitativo = utils_module.gerar_resumo_quantitativo
sugerir_mapeamento_com_ia = utils_module.sugerir_mapeamento_com_ia
inferir_mapeamento_por_perfil = utils_module.inferir_mapeamento_por_perfil
inferir_intencao_pergunta = utils_module.inferir_intencao_pergunta
expandir_pergunta_por_contexto = utils_module.expandir_pergunta_por_contexto
inferir_entidade_da_resposta = utils_module.inferir_entidade_da_resposta


def load_css() -> None:
    if CSS_PATH.exists():
        st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def init_state() -> None:
    defaults = {
        "page": "Dashboard",
        "df": None,
        "uploads": [],
        "column_mapping": {},
        "mapping_confirmed": False,
        "dataset_type": "auto",
        "upload_signature": None,
        "normalized_df": None,
        "mapping_source": "local",
        "last_chat_context": {},
        "pending_chat_question": None,
        "chat_history": [dict(CHAT_GREETING)],
        "chat_session_id": None,
        "chat_history_loaded": False,
        "chat_error": None,
        "chat_sessions_cache": None,
        "chat_sessions_error": None,
        "confirm_delete_chat": None,
        "chat_notice": None,
        "viewing_chat_id": None,
        "chat_search": "",
        "chat_history_search": "",
        "upload_notice": None,
        "pedido_preview": None,
        "pedido_status": None,
        "model_ready": False,
        "last_ai_error": None,
        "last_model_error": None,
        "safety_stock_days": 15,
        "lead_time_days": 7,
        "forecast_horizon_days": 30,
        "authenticated": False,
        "auth_view": "login",
        "auth_notice": None,
        "auth_notice_kind": "info",
        "password_recovery_mode": False,
        "google_oauth_url": None,
        "google_oauth_url_at": None,
        "uploader_key": 0,
        "upload_details": [],
        "supabase_user": None,
        "supabase_client": None,
        "user_profile": None,
        "user_id": None,
        "profile_error": None,
        "login_error": None,
        "login_attempts": 0,
        "login_locked_until": 0,
        "session_started_at": None,
        "last_activity_at": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def logout(reason: str | None = None) -> None:
    """Encerra a sessão no Supabase e limpa todos os dados locais."""
    client = get_supabase_client()
    if client is not None:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    st.session_state.clear()
    if reason:
        st.session_state.login_error = reason
    st.rerun()


def session_expired() -> bool:
    now = time.time()
    started = st.session_state.get("session_started_at")
    last = st.session_state.get("last_activity_at")

    if not started:
        st.session_state.session_started_at = now
        started = now
    if not last:
        st.session_state.last_activity_at = now
        last = now

    if now - float(started) > SESSION_MAX_HOURS * 3600:
        return True
    if now - float(last) > SESSION_IDLE_MINUTES * 60:
        return True

    st.session_state.last_activity_at = now
    return False


def get_api_key() -> str | None:
    try:
        api_key = st.secrets.get("GOOGLE_API_KEY", "").strip()
        if api_key:
            return api_key
    except Exception:
        pass

    env_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if env_key:
        return env_key

    return None


def load_model(api_key: str):
    if not api_key:
        return None, "Nenhuma chave de API foi encontrada. Configure GOOGLE_API_KEY no arquivo .streamlit/secrets.toml."
    try:
        return configurar_gemini(api_key), None
    except Exception as exc:
        return None, sanitize_error_message(
            exc, "Não foi possível carregar o modelo de IA."
        )


def get_model():
    api_key = get_api_key()
    if not api_key:
        return None, "Nenhuma chave de API foi encontrada. Configure GOOGLE_API_KEY no arquivo .streamlit/secrets.toml."
    return load_model(api_key)


 
 


def read_uploaded_files(files) -> tuple[pd.DataFrame | None, list[str], str | None]:
    if not files:
        return None, [], None
    if len(files) > MAX_UPLOAD_FILES:
        return None, [], f"Envie no máximo {MAX_UPLOAD_FILES} arquivos por vez."

    dataframes = []
    names = []
    total_bytes = 0
    max_file_bytes = MAX_UPLOAD_MB * 1024 * 1024
    max_total_bytes = MAX_TOTAL_UPLOAD_MB * 1024 * 1024

    for uploaded in files:
        safe_name = Path(str(uploaded.name)).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            return None, [], f"Formato não suportado: {safe_name}. Envie apenas CSV ou XLSX."

        raw = uploaded.getvalue()
        if not raw:
            return None, [], f"O arquivo {safe_name} está vazio."
        if len(raw) > max_file_bytes:
            return None, [], f"O arquivo {safe_name} excede o limite de {MAX_UPLOAD_MB} MB."
        total_bytes += len(raw)
        if total_bytes > max_total_bytes:
            return None, [], f"O envio total excede o limite de {MAX_TOTAL_UPLOAD_MB} MB."

        try:
            if suffix == ".csv":
                csv_bytes = raw
                last_error = None
                for encoding in ["utf-8", "utf-8-sig", "latin1", "cp1252"]:
                    try:
                        decoded = csv_bytes.decode(encoding)
                        sample = decoded[:4096]
                        try:
                            dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t")
                            sep = dialect.delimiter
                        except csv.Error:
                            first_line = sample.splitlines()[0] if sample.splitlines() else ""
                            sep_scores = {
                                ";": first_line.count(";"),
                                ",": first_line.count(","),
                                "\t": first_line.count("\t"),
                                "|": first_line.count("|"),
                            }

                            sep = max(sep_scores, key=sep_scores.get)
                            if sep_scores[sep] == 0:
                                sep = ","

                        df = pd.read_csv(BytesIO(csv_bytes), encoding=encoding, sep=sep)
                        if len(df.columns) == 1 and sep != ";":
                            df = pd.read_csv(BytesIO(csv_bytes), encoding=encoding, sep=";")
                        break
                    except UnicodeDecodeError as exc:
                        last_error = exc
                else:
                    raise last_error or UnicodeDecodeError("csv", b"", 0, 1, "encoding nao suportado")
            else:
                df = pd.read_excel(BytesIO(raw))
        except MemoryError:
            return None, [], f"O arquivo {safe_name} é grande demais para ser processado."
        except Exception as exc:
            detail = sanitize_error_message(exc, "formato inválido ou arquivo corrompido")
            return None, [], f"Erro ao ler {safe_name}: {detail}"

        if len(df.columns) > MAX_DATA_COLUMNS:
            return None, [], (
                f"O arquivo {safe_name} tem {len(df.columns)} colunas. "
                f"O limite é {MAX_DATA_COLUMNS}."
            )

        df = sanitize_dataframe(df)
        df["Arquivo origem"] = safe_name
        dataframes.append(df)
        names.append(safe_name)

    if not dataframes:
        return None, [], None

    combined = pd.concat(dataframes, ignore_index=True)
    if len(combined) > MAX_DATA_ROWS:
        return None, [], (
            f"A base consolidada tem {len(combined):,} linhas. "
            f"O limite é {MAX_DATA_ROWS:,}."
        ).replace(",", ".")
    return combined, names, None


def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()

    def normalize_label(value: str) -> str:
        text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
        return text.strip().lower()

    renamed_columns = []
    for idx, column in enumerate(cleaned.columns):
        column_name = sanitize_input(str(column), max_length=120)
        column_name = column_name.replace("\n", " ").replace("\t", " ").strip()
        normalized = normalize_label(column_name)

        if normalized == "rotulos de linha":
            renamed_columns.append("Categoria")
            continue

        for prefix in ["soma de ", "media de ", "contagem de ", "count of ", "sum of ", "average of "]:
            if normalized.startswith(prefix):
                column_name = column_name[len(prefix):].strip()
                break

        if not column_name:
            column_name = f"Coluna_{idx + 1}"
        renamed_columns.append(column_name)

    cleaned.columns = renamed_columns

    total_terms = ("total geral", "grand total", "subtotal", "total:")
    remove_mask = pd.Series(False, index=cleaned.index)
    for column in cleaned.columns:
        text_values = cleaned[column].astype(str).map(normalize_label)
        remove_mask = remove_mask | text_values.str.contains("|".join(total_terms), na=False, regex=True)

    cleaned = cleaned.loc[~remove_mask].dropna(how="all").copy()
    return cleaned.reset_index(drop=True)


def get_mapping_suggestions(df: pd.DataFrame) -> dict[str, str | None]:
    _find_column = utils_module._find_column
    return {
        "produto": _find_column(df, ["prod", "item", "descr", "medic", "nome"]),
        "codigo": _find_column(df, ["codigo", "cod", "sku", "ean", "id produto", "id_item"]),
        "vendas": _find_column(df, ["vend", "vendido", "venda", "sale", "demanda", "qt vend", "qtd vend", "quantidade vend", "saida", "giro"]),
        "estoque": _find_column(df, ["estoq", "stock", "saldo", "dispon"]),
        "data": _find_column(df, ["data", "date", "periodo", "mov"]),
        "categoria": _find_column(df, ["categ", "grupo", "linha", "class"]),
        "data_hora": _find_column(df, ["data e hora", "datetime", "hora", "timestamp"]),
        "forma_pagamento": _find_column(df, ["pagamento", "payment", "forma"]),
        "valor": _find_column(df, ["valor", "preco", "preco unitario", "valor unitario", "vlr", "price", "dinheiro", "cart", "receita", "total"]),
        "quantidade": _find_column(df, ["quantidade", "qtd", "qt", "unidades", "volume", "qt vendida", "qtd vendida"]),
        "filial": _find_column(df, ["filial", "loja", "unidade"]),
        "fornecedor": _find_column(df, ["fornecedor", "supplier"]),
    }


def get_active_mapping(df: pd.DataFrame | None) -> dict[str, str | None]:
    if df is None:
        return {}

    suggestions = get_mapping_suggestions(df)
    saved = st.session_state.column_mapping or {}
    merged = {}
    for key, suggested in suggestions.items():
        saved_value = saved.get(key)
        if saved_value in df.columns:
            merged[key] = saved_value
        else:
            merged[key] = suggested
    return merged


def mapping_is_ready(mapping: dict[str, str | None]) -> bool:
    return all(mapping.get(field) for field in ["produto", "vendas", "estoque"])


def get_dataset_requirements(dataset_type: str) -> list[str]:
    requirements = {
        "vendas": ["produto"],
        "estoque": ["produto", "estoque"],
        "misto": ["produto"],
        "financeiro": ["data"],
        "auto": ["produto"],
    }
    return requirements.get(dataset_type, ["produto"])


def get_visible_mapping_fields(dataset_type: str, mapping: dict[str, str | None]) -> list[tuple[str, str]]:
    """Retorna todos os campos mapeáveis, com os obrigatórios marcados e primeiro."""
    field_catalog = {
        "produto": "Coluna de produto",
        "codigo": "Coluna de código",
        "vendas": "Coluna de vendas",
        "estoque": "Coluna de estoque",
        "data": "Coluna de data",
        "categoria": "Coluna de categoria",
        "data_hora": "Coluna de data e hora",
        "forma_pagamento": "Coluna de forma de pagamento",
        "valor": "Coluna de valor",
        "quantidade": "Coluna de quantidade",
        "filial": "Coluna de filial",
        "fornecedor": "Coluna de fornecedor",
    }
    base_fields = {
        "auto": ["produto", "codigo", "vendas", "estoque", "data", "categoria"],
        "vendas": ["produto", "codigo", "vendas", "data", "data_hora", "forma_pagamento", "valor", "quantidade", "categoria", "filial"],
        "estoque": ["produto", "codigo", "estoque", "categoria", "filial", "fornecedor"],
        "misto": ["produto", "codigo", "vendas", "estoque", "data", "data_hora", "categoria", "valor", "quantidade", "filial", "fornecedor"],
        "financeiro": ["data", "data_hora", "forma_pagamento", "valor", "categoria", "filial"],
    }
    required = set(get_dataset_requirements(dataset_type))

    ordered_keys: list[str] = []

    def add(key: str) -> None:
        if key in field_catalog and key not in ordered_keys:
            ordered_keys.append(key)

    # Obrigatórios primeiro, depois os campos típicos do tipo, depois o restante.
    for key in get_dataset_requirements(dataset_type):
        add(key)
    for key in base_fields.get(dataset_type, base_fields["auto"]):
        add(key)
    for key in field_catalog:
        add(key)

    return [
        (key, f"{field_catalog[key]} *" if key in required else field_catalog[key])
        for key in ordered_keys
    ]


def infer_dataset_type(df: pd.DataFrame, mapping: dict[str, str | None]) -> str:
    has_produto = bool(mapping.get("produto"))
    has_estoque = bool(mapping.get("estoque"))
    has_vendas = bool(mapping.get("vendas") or mapping.get("quantidade") or mapping.get("valor"))
    has_pagamento = bool(mapping.get("forma_pagamento"))

    if has_produto and has_estoque and has_vendas:
        return "misto"
    if has_produto and has_estoque:
        return "estoque"
    if has_produto and (has_vendas or has_pagamento):
        return "vendas"
    if has_pagamento or mapping.get("valor"):
        return "financeiro"
    return "auto"


def mapping_is_ready_for_type(mapping: dict[str, str | None], dataset_type: str) -> bool:
    return all(mapping.get(field) for field in get_dataset_requirements(dataset_type))


def choose_primary_sales_column(mapping: dict[str, str | None]) -> str | None:
    return mapping.get("quantidade") or mapping.get("vendas") or mapping.get("valor")


def build_normalized_view(df: pd.DataFrame, mapping: dict[str, str | None]) -> pd.DataFrame:
    return construir_base_normalizada(df, mapping)


def build_kpis(df: pd.DataFrame, mapping: dict[str, str | None], dataset_type: str) -> list[tuple[str, str]]:
    _normalize_numeric = utils_module._normalize_numeric
    normalized = build_normalized_view(df, mapping)
    numeric_cols = [col for col in df.columns if pd.api.types.is_numeric_dtype(df[col])]
    estoque_col = mapping.get("estoque")
    venda_col = mapping.get("vendas")
    produto_col = mapping.get("produto")

    total_itens = df[produto_col].nunique() if produto_col else len(df)
    total_estoque = _normalize_numeric(df[estoque_col]).sum() if estoque_col else None
    total_vendas = _normalize_numeric(df[venda_col]).sum() if venda_col else None
    fallback_sum = _normalize_numeric(df[numeric_cols[0]]).sum() if numeric_cols else None
    total_valor = normalized["valor"].sum() if "valor" in normalized.columns else None
    total_quantidade = normalized["quantidade"].sum() if "quantidade" in normalized.columns else None

    kpis = [
        ("Arquivos lidos", str(len(st.session_state.uploads))),
        ("Produtos monitorados", f"{int(total_itens):,}".replace(",", ".")),
        ("Estoque total", f"{total_estoque:,.0f}".replace(",", ".") if total_estoque is not None else "N/D"),
        ("Venda acumulada", f"{total_vendas:,.0f}".replace(",", ".") if total_vendas is not None else (f"{fallback_sum:,.0f}".replace(",", ".") if fallback_sum is not None else "N/D")),
    ]
    if dataset_type == "vendas":
        kpis[2] = ("Quantidade vendida", f"{total_quantidade:,.0f}".replace(",", ".") if total_quantidade is not None else "N/D")
        kpis[3] = ("Receita total", f"{total_valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if total_valor is not None else "N/D")
    elif dataset_type == "financeiro":
        kpis[1] = ("Lancamentos", f"{len(df):,}".replace(",", "."))
        kpis[2] = ("Receita total", f"{total_valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if total_valor is not None else "N/D")
        kpis[3] = ("Colunas ativas", str(len(df.columns)))
    return kpis


def render_setup_status(model) -> None:
    notices = []
    if get_api_key():
        if model is None:
            notices.append(("warn", f"Chave encontrada, mas o modelo nao carregou: {st.session_state.last_model_error or 'erro nao identificado'}"))
    else:
        notices.append(("warn", "Chave da IA nao encontrada. O app continua funcional nas analises locais, mas as respostas generativas ficam limitadas."))

    if st.session_state.df is not None:
        notices.append(("ok", f"Base carregada com {len(st.session_state.df):,} linha(s).".replace(",", ".")))

    for level, message in notices:
        if level == "ok":
            st.success(message)
        else:
            st.warning(message)


def render_diagnostics(df: pd.DataFrame | None, mapping: dict[str, str | None], dataset_type: str) -> None:
    if df is None:
        return

    normalized_df = st.session_state.normalized_df
    with st.expander("Como a base foi interpretada", expanded=False):
        st.markdown(
            f"""
            <div class="feature-card">
                Tipo da base em uso: <strong>{escape_html(dataset_type.capitalize())}</strong><br>
                Origem do mapeamento: <strong>{'IA' if st.session_state.mapping_source == 'ai' else 'Heuristica'}</strong><br>
                Linhas da base bruta: <strong>{len(df):,}</strong>
            </div>
            """.replace(",", "."),
            unsafe_allow_html=True,
        )

        mapping_rows = []
        for key, value in mapping.items():
            if value:
                mapping_rows.append({"Campo": key, "Coluna detectada": value})
        if mapping_rows:
            st.markdown("<div class='preview-title'>Mapeamento ativo</div>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(mapping_rows), width="stretch", hide_index=True)

        if normalized_df is not None and not normalized_df.empty:
            st.markdown("<div class='preview-title'>Resumo da base normalizada</div>", unsafe_allow_html=True)
            st.caption(gerar_resumo_quantitativo(normalized_df))
            st.dataframe(normalized_df.head(12), width="stretch", height=260)


def render_mapping_panel(df: pd.DataFrame | None) -> dict[str, str | None]:
    if df is None:
        return {}

    mapping = get_active_mapping(df)
    st.session_state.column_mapping = mapping
    st.session_state.normalized_df = construir_base_normalizada(df, mapping)
    auto_type = infer_dataset_type(df, mapping)
    st.session_state.mapping_confirmed = mapping_is_ready_for_type(mapping, auto_type)
    if st.session_state.dataset_type == "auto":
        st.session_state.dataset_type = auto_type
    
    columns = ["", *list(df.columns)]
    confidence_fields = sum(1 for value in mapping.values() if value)
    confidence_label = "Alta" if confidence_fields >= 6 else "Média" if confidence_fields >= 4 else "Baixa"

    st.markdown("<div class='section-label'>Mapeamento das colunas</div>", unsafe_allow_html=True)
    render_file_summary()
    if st.button("🗑️ Remover planilhas e recomeçar", key="clear_from_mapping", width="content"):
        reset_uploads()
        st.rerun()
    st.markdown(
        """
        <div class="feature-card" style="margin-bottom: 1.25rem;">
            Confirme quais colunas representam produto, vendas, estoque, data e categoria.
            Os campos marcados com * são obrigatórios para liberar o dashboard e as sugestões.
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="mapping-summary-card" style="margin-bottom: 1.5rem;">
            <div class="mapping-summary-top">
                <div>
                    <div class="mapping-summary-label">Tipo detectado</div>
                    <div class="mapping-summary-value">{escape_html(auto_type.capitalize())}</div>
                </div>
                <div>
                    <div class="mapping-summary-label">Confiança da leitura</div>
                    <div class="mapping-summary-value">{escape_html(confidence_label)}</div>
                </div>
                <div>
                    <div class="mapping-summary-label">Colunas encontradas</div>
                    <div class="mapping-summary-value">{len(df.columns)}</div>
                </div>
                <div>
                    <div class="mapping-summary-label">Origem da sugestão</div>
                    <div class="mapping-summary-value">{'IA' if st.session_state.mapping_source == 'ai' else 'Heurística'}</div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Usar sugestão automática", width="stretch", key="apply_auto_mapping"):
        st.session_state.column_mapping = mapping
        st.session_state.dataset_type = "auto"
        st.session_state.normalized_df = construir_base_normalizada(df, mapping)
        st.session_state.mapping_confirmed = mapping_is_ready_for_type(mapping, auto_type)
        if st.session_state.mapping_confirmed:
            st.success("Leitura automática aplicada com sucesso.")
            st.rerun()
        else:
            required = ", ".join(get_dataset_requirements(auto_type))
            st.warning(f"A leitura automática foi aplicada, mas ainda faltam campos obrigatórios: {required}.")

    with st.expander("Revisar manualmente", expanded=True):
        with st.form("mapping_form"):
            type_options = {
                "auto": "Detecção automática",
                "vendas": "Base de vendas",
                "estoque": "Base de estoque",
                "misto": "Base mista (vendas + estoque)",
                "financeiro": "Base financeira",
            }
            default_type = st.session_state.dataset_type if st.session_state.dataset_type in type_options else "auto"
            selected_type = st.selectbox(
                "Tipo da base",
                options=list(type_options.keys()),
                index=list(type_options.keys()).index(default_type),
                format_func=lambda key: type_options[key],
            )

            col1, col2, col3 = st.columns(3)
            selected = {}
            preview_type = auto_type if selected_type == "auto" else selected_type
            visible_fields = get_visible_mapping_fields(preview_type, mapping)
            required_fields = get_dataset_requirements(preview_type)
            st.caption(
                "Mapeie as colunas de entrada correspondentes aos conceitos operacionais. "
                "Campos com * são obrigatórios para liberar os relatórios."
            )

            containers = [col1, col2, col3]
            for idx, (field, label) in enumerate(visible_fields):
                container = containers[idx % 3]
                current = mapping.get(field) or ""
                index = columns.index(current) if current in columns else 0
                selected[field] = container.selectbox(label, options=columns, index=index, key=f"map_{field}")

            confirm = st.form_submit_button("Confirmar Mapeamento", width="stretch")

        normalized = {**mapping, **{key: (value or None) for key, value in selected.items()}}
        if confirm:
            st.session_state.column_mapping = normalized
            st.session_state.dataset_type = selected_type
            st.session_state.normalized_df = construir_base_normalizada(df, normalized)
            resolved_type = infer_dataset_type(df, normalized) if selected_type == "auto" else selected_type
            st.session_state.mapping_confirmed = mapping_is_ready_for_type(normalized, resolved_type)
            if st.session_state.mapping_confirmed:
                st.success("Mapeamento confirmado. Redirecionando para o Dashboard...")
                st.rerun()
            else:
                missing = [
                    field
                    for field in get_dataset_requirements(resolved_type)
                    if not normalized.get(field)
                ]
                st.error(
                    "Ainda faltam campos obrigatórios: " + ", ".join(missing) + "."
                    if missing
                    else "Mapeamento incompleto. Revise as colunas selecionadas."
                )
                
    if not st.session_state.column_mapping:
        st.session_state.column_mapping = normalized
        resolved_type = infer_dataset_type(df, normalized)
        st.session_state.dataset_type = "auto"
        st.session_state.mapping_confirmed = mapping_is_ready_for_type(normalized, resolved_type)

    active_mapping = st.session_state.column_mapping
    dataset_type = infer_dataset_type(df, active_mapping) if st.session_state.dataset_type == "auto" else st.session_state.dataset_type
    
    tags = []
    for field, label in [
        ("produto", "Produto"),
        ("codigo", "Código"),
        ("vendas", "Vendas"),
        ("estoque", "Estoque"),
        ("data", "Data"),
        ("categoria", "Categoria"),
    ]:
        value = active_mapping.get(field)
        if value:
            tags.append(
                f"<span class='mapping-tag'><strong>{escape_html(label)}:</strong> "
                f"{escape_html(value)}</span>"
            )
    if tags:
        st.markdown(f"<div class='mapping-tags'>{''.join(tags)}</div>", unsafe_allow_html=True)
        
    return active_mapping


def render_chat_sessions_sidebar() -> None:
    """Lista as conversas do usuário e permite criar, buscar, renomear e excluir."""
    if not chat_persistence_available():
        return

    sessions = list_chat_sessions()
    total = len(sessions)
    st.sidebar.markdown(
        f"<div class='sidebar-section-title'>Conversas"
        f"<span class='sidebar-section-count'>{total}</span></div>",
        unsafe_allow_html=True,
    )

    if st.sidebar.button(
        "＋ Nova conversa", key="sidebar_new_chat", width="stretch"
    ):
        start_new_chat()
        st.session_state.page = "AI Assistant"
        st.rerun()

    if st.session_state.get("chat_notice"):
        st.sidebar.success(st.session_state.chat_notice)
        st.session_state.chat_notice = None

    if st.session_state.get("chat_sessions_error"):
        st.sidebar.caption(
            f"Não foi possível listar as conversas: {st.session_state.chat_sessions_error}"
        )
        return

    if not sessions:
        st.sidebar.markdown(
            "<div class='sidebar-empty'>Suas conversas aparecerão aqui assim que você "
            "enviar a primeira mensagem.</div>",
            unsafe_allow_html=True,
        )
        return

    search = st.sidebar.text_input(
        "Buscar conversas",
        placeholder="🔎 Buscar conversas",
        label_visibility="collapsed",
        key="chat_search",
    )
    filtered = filter_chat_sessions(sessions, search)
    if not filtered:
        st.sidebar.caption("Nenhuma conversa encontrada para esta busca.")
        return

    active_id = st.session_state.get("chat_session_id")
    limit = 30 if search else 10
    shown = 0
    for group_title, items in group_chat_sessions(filtered):
        if shown >= limit:
            break
        st.sidebar.markdown(
            f"<div class='sidebar-conversation-group'>{escape_html(group_title)}</div>",
            unsafe_allow_html=True,
        )
        for session in items:
            if shown >= limit:
                break
            session_id = session.get("id")
            if not session_id:
                continue
            raw_title = str(session.get("title") or "Nova conversa")
            title = raw_title if len(raw_title) <= 30 else raw_title[:29] + "…"
            active = session_id == active_id
            relative = format_relative_time(session.get("updated_at"))
            if st.sidebar.button(
                f"{'●' if active else '💬'} {title}",
                key=f"chat_session_{session_id}",
                width="stretch",
                type="primary" if active else "secondary",
                help=f"Atualizada {relative}" if relative else None,
            ):
                if open_chat_session(session_id):
                    st.rerun()
            shown += 1

    if total > limit or search:
        label = "Ver todas as conversas" if total > limit else "Abrir histórico completo"
        if st.sidebar.button(label, key="open_chat_history", width="stretch"):
            st.session_state.page = "Chat History"
            st.session_state.viewing_chat_id = None
            st.rerun()

    active = next((item for item in sessions if item.get("id") == active_id), None)
    if not active:
        return

    with st.sidebar.expander(
        "⚙️ Gerenciar conversa atual",
        expanded=bool(st.session_state.get("confirm_delete_chat")),
    ):
        new_title = st.text_input(
            "Título da conversa",
            value=active.get("title") or "Nova conversa",
            max_chars=80,
            key=f"rename_chat_{active['id']}",
        )
        action_col1, action_col2 = st.columns(2)
        if action_col1.button("Salvar", key="save_chat_title", width="stretch"):
            if rename_chat_session(active["id"], new_title):
                st.session_state.chat_notice = "Título atualizado."
                st.rerun()
        if action_col2.button(
            "Excluir", key="ask_delete_chat", width="stretch"
        ):
            st.session_state.confirm_delete_chat = active["id"]
            st.rerun()

        if st.session_state.get("confirm_delete_chat") == active["id"]:
            st.warning("Excluir esta conversa? Essa ação não pode ser desfeita.")
            if st.button(
                "Confirmar exclusão",
                key="confirm_delete_chat_btn",
                type="primary",
                width="stretch",
            ):
                if delete_chat_session(active["id"]):
                    st.session_state.chat_notice = "Conversa excluída."
                    st.rerun()
            if st.button(
                "Cancelar", key="cancel_delete_chat", width="stretch"
            ):
                st.session_state.confirm_delete_chat = None
                st.rerun()


def render_sidebar() -> None:
    st.sidebar.markdown(
        """
        <div class="sidebar-brand-box">
            <div class="sidebar-logo-text">SmartStock <span>AI</span></div>
            <div class="sidebar-subtitle">Estoque Inteligente. Compras Inteligentes</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    supabase_ok, supabase_message = get_supabase_status()
    if not supabase_ok:
        st.sidebar.warning(supabase_message)
    if st.session_state.get("profile_error"):
        st.sidebar.warning(f"Falha ao salvar perfil no Supabase: {st.session_state.profile_error}")

    sb_user = st.session_state.get("supabase_user") or {}
    profile = st.session_state.get("user_profile") or {}
    if sb_user or profile:
        user_email = (profile.get("email") or sb_user.get("email") or "").strip()
        user_name = (
            profile.get("name")
            or (sb_user.get("user_metadata") or {}).get("name")
            or user_email
        ).strip()
        provider = str(
            (sb_user.get("app_metadata") or {}).get("provider") or "email"
        ).lower()
        provider_label = "Google" if provider == "google" else "E-mail"
        initials = escape_html(_initials(user_name, user_email))
        st.sidebar.markdown(
            f"""
            <div class="sidebar-user-card">
                <div class="sidebar-user-avatar">{initials}</div>
                <div class="sidebar-user-meta">
                    <div class="sidebar-user-name">{escape_html(user_name)}</div>
                    <div class="sidebar-user-email">{escape_html(user_email)}</div>
                    <span class="sidebar-user-badge">{provider_label}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.sidebar.button(
            "↪ Sair da conta",
            width="stretch",
            key="sidebar_logout",
        ):
            logout()
    elif st.session_state.get("authenticated"):
        st.sidebar.markdown(
            """
            <div class="sidebar-user-card">
                <div class="sidebar-user-avatar">AD</div>
                <div class="sidebar-user-meta">
                    <div class="sidebar-user-name">Administrador</div>
                    <div class="sidebar-user-email">Sessão local</div>
                    <span class="sidebar-user-badge">Local</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.sidebar.button(
            "↪ Sair", width="stretch", key="sidebar_logout"
        ):
            logout()

    options = [
        ("AI Assistant", "💬 Assistente IA"),
        ("Dashboard", "🖥️ Dashboard"),
        ("Purchase Recommendations", "🛍️ Sugestão de Compras"),
    ]
    if chat_persistence_available():
        options.append(("Chat History", "🗂️ Conversas"))

    for option_id, option_label in options:
        active = st.session_state.page == option_id
        if st.sidebar.button(
            option_label,
            key=f"nav_{option_id}",
            width="stretch",
            type="primary" if active else "secondary",
        ):
            st.session_state.page = option_id
            st.session_state.viewing_chat_id = None
            st.rerun()

    render_chat_sessions_sidebar()

    st.sidebar.markdown(
        "<hr style='border-color:#1E293B; margin: 1.25rem 0;'>",
        unsafe_allow_html=True,
    )

    if st.session_state.get("df") is not None:
        uploads = st.session_state.uploads or []
        st.sidebar.markdown(
            f"<div style='font-size:0.8rem;color:#94A3B8;padding:0 0.5rem 0.5rem'>📄 {len(uploads)} arquivo(s) · {len(st.session_state.df):,} linhas</div>",
            unsafe_allow_html=True,
        )
        if st.sidebar.button("＋ Adicionar", key="toggle_extra_upload", width="stretch"):
            st.session_state.show_extra_upload = not st.session_state.get("show_extra_upload", False)
            st.rerun()

        if st.session_state.get("show_extra_upload"):
            st.sidebar.markdown('<div class="extra-upload-area">', unsafe_allow_html=True)
            extra_files = st.file_uploader(
                "Selecione arquivos",
                type=["csv", "xlsx"],
                accept_multiple_files=True,
                label_visibility="collapsed",
                key=f"extra_uploader_{st.session_state.get('uploader_key', 0)}",
            )
            if extra_files:
                with st.spinner("Lendo arquivos..."):
                    extra_combined, extra_names, extra_error = read_uploaded_files(extra_files)
                if extra_error:
                    st.error(extra_error)
                else:
                    new_df = pd.concat([st.session_state.df, extra_combined], ignore_index=True)
                    st.session_state.df = new_df
                    st.session_state.uploads = list(dict.fromkeys(st.session_state.uploads + extra_names))
                    existing_details = st.session_state.get("upload_details") or []
                    st.session_state.upload_details = existing_details + [
                        {"name": uploaded.name, "size": len(uploaded.getvalue())}
                        for uploaded in extra_files
                        if uploaded.name in extra_names
                    ]
                    st.session_state.normalized_df = construir_base_normalizada(new_df, st.session_state.column_mapping)
                    st.session_state.pedido_status = None
                    st.session_state.pedido_preview = None
                    st.session_state.last_chat_context = {}
                    st.session_state.show_extra_upload = False
                    st.session_state.upload_notice = f"{len(extra_names)} arquivo(s) adicionado(s): {', '.join(extra_names)}"
                    st.rerun()
            st.sidebar.markdown('</div>', unsafe_allow_html=True)

        if st.sidebar.button("🔄 Trocar planilha", width="stretch", key="nav_reset"):
            reset_uploads()
            st.rerun()


def render_header(df: pd.DataFrame | None, mapping: dict[str, str | None] | None) -> None:
    if df is not None and mapping and mapping.get("produto"):
        product_col = mapping.get("produto")
        total_products = df[product_col].nunique()
        summary_text = f"Seu inventário está sendo monitorado com {total_products} SKUs ativos."
    else:
        summary_text = "Carregue suas planilhas de inventário para iniciar a análise inteligente de demanda e compras."

    st.markdown(
        f"""
        <div class="hero-panel">
            <div>
                <div class="eyebrow">SmartStock AI</div>
                <h1>Controle estoque, demanda e compras corporativas.</h1>
                <p>{escape_html(summary_text)}</p>
            </div>
            <div class="hero-actions">
                <span>Giro de Estoque</span>
                <span>Previsão de Demanda</span>
                <span>Ordens de Reposição</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def merge_mapping_with_fallback(df: pd.DataFrame, suggested_mapping: dict[str, str | None]) -> dict[str, str | None]:
    profile_mapping = inferir_mapeamento_por_perfil(df).get("mapping", {})
    fallback = get_mapping_suggestions(df)
    merged = {}
    for key in fallback.keys():
        merged[key] = suggested_mapping.get(key) or profile_mapping.get(key) or fallback.get(key)
    return merged


def reset_uploads() -> None:
    """Limpa planilhas e todo o estado derivado do upload."""
    st.session_state.df = None
    st.session_state.uploads = []
    st.session_state.upload_details = []
    st.session_state.upload_signature = None
    st.session_state.column_mapping = {}
    st.session_state.normalized_df = None
    st.session_state.mapping_confirmed = False
    st.session_state.dataset_type = "auto"
    st.session_state.mapping_source = "local"
    st.session_state.page = "Dashboard"
    st.session_state.pedido_preview = None
    st.session_state.pedido_status = None
    st.session_state.last_chat_context = {}
    st.session_state.upload_notice = None
    st.session_state.uploader_key = int(st.session_state.get("uploader_key", 0)) + 1


def render_stepper(current_step: int) -> None:
    steps = [("1", "Upload"), ("2", "Mapeamento"), ("3", "Análise")]
    items = []
    for index, (number, label) in enumerate(steps, start=1):
        state = "done" if index < current_step else "active" if index == current_step else ""
        items.append(
            f"<div class='stepper-item {state}'>"
            f"<span class='stepper-number'>{number}</span>"
            f"<span class='stepper-label'>{escape_html(label)}</span>"
            f"</div>"
        )
    st.markdown(
        "<div class='stepper'>" + "<div class='stepper-line'></div>".join(items) + "</div>",
        unsafe_allow_html=True,
    )


def format_file_size(size_bytes: int) -> str:
    size = float(size_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def render_file_summary() -> None:
    details = st.session_state.get("upload_details") or []
    uploads = st.session_state.get("uploads") or []
    if not uploads:
        return

    if details:
        rows = "".join(
            f"<div class='upload-file-row'><span>📄 {escape_html(item.get('name', ''))}</span>"
            f"<span class='upload-file-size'>{format_file_size(item.get('size', 0))}</span></div>"
            for item in details
        )
    else:
        rows = "".join(
            f"<div class='upload-file-row'><span>📄 {escape_html(name)}</span></div>"
            for name in uploads
        )

    total_rows = len(st.session_state.df) if st.session_state.get("df") is not None else 0
    total_rows_label = f"{total_rows:,}".replace(",", ".")
    st.markdown(
        f"""
        <div class="upload-summary">
            <div class="upload-summary-title">Arquivos ativos</div>
            {rows}
            <div class="upload-summary-total">Total consolidado: <strong>{total_rows_label} linha(s)</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_upload(model=None) -> None:
    render_stepper(1)
    st.markdown(
        """
        <div class="section-label">Importação de Arquivos</div>
        <div class="upload-helper">Selecione planilhas Excel (.xlsx) ou CSV. Limites: 25 MB por arquivo, 10 arquivos, 60 MB no total e 200 mil linhas.</div>
        """,
        unsafe_allow_html=True,
    )

    if st.session_state.get("upload_notice"):
        st.success(st.session_state.upload_notice)
        st.session_state.upload_notice = None

    if not st.session_state.get("uploads"):
        st.markdown(
            """
            <div class="empty-state">
                <div class="empty-state-icon">📊</div>
                <div class="empty-state-title">Comece enviando suas planilhas</div>
                <div class="empty-state-text">
                    Aceitamos CSV e Excel. Você pode enviar várias planilhas juntas:
                    elas serão consolidadas em uma única base para análise.
                </div>
                <div class="empty-state-steps">
                    <span>1. Envie os arquivos</span>
                    <span>2. Confirme as colunas</span>
                    <span>3. Explore o dashboard</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    files = st.file_uploader(
        "Upload de arquivos",
        type=["csv", "xlsx"],
        accept_multiple_files=True,
        label_visibility="collapsed",
        key=f"uploader_{st.session_state.get('uploader_key', 0)}",
    )

    if files:
        current_signature = (UPLOAD_SCHEMA_VERSION, tuple((uploaded.name, len(uploaded.getvalue())) for uploaded in files))
        if st.session_state.upload_signature != current_signature:
            with st.spinner("Lendo e validando arquivos..."):
                combined, names, error = read_uploaded_files(files)
            if error:
                st.error(error)
            else:
                st.session_state.df = combined
                st.session_state.uploads = names
                st.session_state.upload_details = [
                    {"name": uploaded.name, "size": len(uploaded.getvalue())}
                    for uploaded in files
                    if uploaded.name in names
                ]
                st.session_state.upload_signature = current_signature
                st.session_state.pedido_status = None
                st.session_state.pedido_preview = None
                st.session_state.last_chat_context = {}
                ai_result = {"dataset_type": "auto", "mapping": {}, "source": "local"}
                if model is not None:
                    try:
                        with st.spinner("Analisando colunas com IA..."):
                            ai_result = sugerir_mapeamento_com_ia(model, combined)
                    except Exception:
                        ai_result = {"dataset_type": "auto", "mapping": {}, "source": "local"}
                merged_mapping = merge_mapping_with_fallback(combined, ai_result.get("mapping", {}))
                st.session_state.column_mapping = merged_mapping
                st.session_state.normalized_df = construir_base_normalizada(combined, st.session_state.column_mapping)
                detected_type = ai_result.get("dataset_type", "auto")
                if detected_type == "auto":
                    detected_type = infer_dataset_type(combined, st.session_state.column_mapping)
                st.session_state.dataset_type = detected_type if detected_type in ["vendas", "estoque", "misto", "financeiro"] else "auto"
                st.session_state.mapping_source = ai_result.get("source", "local")
                st.session_state.mapping_confirmed = mapping_is_ready_for_type(st.session_state.column_mapping, detected_type)
                st.session_state.upload_notice = (
                    f"{len(names)} arquivo(s) carregado(s): {', '.join(names)}"
                )
                st.rerun()

    if st.session_state.get("uploads"):
        st.markdown("<div class='upload-list-title'>Planilhas carregadas nesta sessão</div>", unsafe_allow_html=True)
        render_file_summary()
        if st.button("🗑️ Remover planilhas", width="stretch", key="clear_uploads"):
            reset_uploads()
            st.rerun()


def render_dashboard_saas(df: pd.DataFrame, mapping: dict, dataset_type: str) -> None:
    import plotly.express as px
    import plotly.graph_objects as go

    render_header(df, mapping)
    
    normalized = st.session_state.get("normalized_df")
    if normalized is None or normalized.empty:
        try:
            normalized = build_normalized_view(df, mapping)
            st.session_state.normalized_df = normalized
        except Exception:
            normalized = None
    if normalized is None or normalized.empty:
        st.warning("Problema ao normalizar a base de dados. Revise o mapeamento das colunas.")
        return
        
    kpis = build_kpis(df, mapping, dataset_type)
    
    cols = st.columns(5)
    
    if "estoque" in normalized.columns and "valor" in normalized.columns:
        inventario_total = float((normalized["estoque"] * normalized["valor"]).sum())
        inventario_label = f"R${inventario_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        inventario_label = "N/D"
        
    tot_products = int(normalized["produto"].nunique()) if "produto" in normalized.columns else len(df)
    vendas_total = float(normalized["quantidade"].sum()) if "quantidade" in normalized.columns else 0
    
    restock_summary = getattr(utils_module, "_build_restock_summary", None)
    if restock_summary:
        summary_df = restock_summary(normalized)
        if not summary_df.empty:
            total_items = len(summary_df)
            healthy_items = len(summary_df[summary_df["Status"] == "Estoque adequado"])
            health_score = int((healthy_items / total_items) * 100) if total_items > 0 else 100
            
            compras_total = float(summary_df["Pedido sugerido"].sum()) if "Pedido sugerido" in summary_df.columns else 0
            if "Ticket medio" in summary_df.columns:
                compras_valor = float((summary_df["Pedido sugerido"] * summary_df["Ticket medio"]).sum())
            else:
                compras_valor = 0.0
            compras_label = f"R${compras_valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        else:
            health_score = 100
            compras_label = "R$0,00"
            compras_total = 0
    else:
        health_score = 100
        compras_label = "N/D"
        compras_total = 0
        
    kpi_definitions = [
        ("Valor do Estoque", inventario_label, "📦 total estocagem", "normal"),
        ("Produtos Ativos", f"{tot_products}", "🏷️ SKUs monitorados", "normal"),
        ("Demanda Acumulada", f"{vendas_total:,.0f}".replace(",", "."), "📈 volume de giros", "normal"),
        ("Saúde do Estoque", f"{health_score}%", "🟢 níveis adequados", "warning" if health_score < 70 else "normal"),
        ("Sugestão de Compra", compras_label, f"🛍️ {compras_total:,.0f} un. estimadas", "alert" if compras_total > 0 else "normal")
    ]
    
    for col, (label, val, sub, state) in zip(cols, kpi_definitions):
        card_class = "metric-card"
        if state == "alert":
            card_class += " alert-kpi"
        elif state == "warning":
            card_class += " warning-kpi"
            
        col.markdown(
            f"""
            <div class="{card_class}">
                <div class="metric-label">{label}</div>
                <div class="metric-value">{val}</div>
                <div class="metric-trend neutral">{sub}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    
    chart_col1, chart_col2 = st.columns([2, 1])
    
    with chart_col1:
        with st.container():
            st.markdown("<div class='preview-title'>Histórico Geral de Vendas</div>", unsafe_allow_html=True)
            st.markdown("<div style='font-size: 0.85rem; color: var(--text-light); margin-bottom: 1rem;'>Visualização temporal do giro acumulado na base de dados.</div>", unsafe_allow_html=True)
            
            if "data" in normalized.columns and "quantidade" in normalized.columns:
                df_timeline = normalized.groupby("data")["quantidade"].sum().reset_index()
                fig_line = px.line(
                    df_timeline,
                    x="data",
                    y="quantidade",
                    labels={"data": "Data", "quantidade": "Quantidade"},
                    color_discrete_sequence=["#10B981"]
                )
                fig_line.update_traces(line=dict(width=2.5))
                fig_line.update_layout(
                    margin=dict(l=10, r=10, t=10, b=30),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=340,
                    xaxis=dict(showgrid=True, gridcolor="#F1F5F9", tickfont=dict(size=11)),
                    yaxis=dict(showgrid=True, gridcolor="#F1F5F9", tickfont=dict(size=11)),
                    hovermode="x unified",
                )
                st.plotly_chart(fig_line, width="stretch", config={"displayModeBar": False})
            else:
                st.info("Colunas de data e quantidade necessárias para renderizar o gráfico histórico.")
        
    with chart_col2:
        with st.container():
            st.markdown("<div class='preview-title'>Distribuição de Status de Reposição</div>", unsafe_allow_html=True)
            st.markdown("<div style='font-size: 0.85rem; color: var(--text-light); margin-bottom: 1rem;'>Proporção de SKUs categorizados por criticidade.</div>", unsafe_allow_html=True)
            
            if restock_summary and not summary_df.empty:
                df_status = summary_df["Status"].value_counts().reset_index()
                df_status.columns = ["Status", "Quantidade"]
                
                color_map = {
                    "Estoque adequado": "#10B981",
                    "Comprar": "#6366F1",
                    "Comprar urgente": "#EF4444",
                    "Estoque parado": "#64748B",
                    "Excesso": "#F59E0B"
                }
                colors = [color_map.get(status, "#CBD5E1") for status in df_status["Status"]]
                
                fig_pie = go.Figure(data=[go.Pie(
                    labels=df_status["Status"],
                    values=df_status["Quantidade"],
                    hole=.45,
                    marker=dict(colors=colors),
                    textinfo="label+percent",
                    textfont=dict(size=12),
                )])
                fig_pie.update_layout(
                    margin=dict(l=10, r=10, t=15, b=10),
                    paper_bgcolor="rgba(0,0,0,0)",
                    height=340,
                    showlegend=False,
                )
                st.plotly_chart(fig_pie, width="stretch", config={"displayModeBar": False})
            else:
                st.info("Mapeamento de estoque e produto necessário para análise de criticidade.")
    
    with st.container():
        st.markdown("<div class='preview-title' style='margin-bottom: 1rem;'>Produtos Críticos Exigindo Reposição Imediata</div>", unsafe_allow_html=True)
        
        if restock_summary and not summary_df.empty:
            urgentes = summary_df[summary_df["Status"] == "Comprar urgente"]
            if urgentes.empty:
                urgentes = summary_df[summary_df["Status"] == "Comprar"].head(8)
                
            if not urgentes.empty:
                visible_cols = [
                    col
                    for col in ["Produto", "Vendas", "Estoque", "Cobertura atual (dias)", "Pedido sugerido", "Prioridade", "Status"]
                    if col in urgentes.columns
                ]
                critical_view = urgentes[visible_cols].round(2)
                st.dataframe(critical_view, width="stretch", hide_index=True)
                st.download_button(
                    "⬇️ Exportar produtos críticos (CSV)",
                    data=sanitize_export_dataframe(critical_view).to_csv(index=False).encode("utf-8-sig"),
                    file_name="produtos_criticos_smartstock.csv",
                    mime="text/csv",
                    key="download_critical_products",
                )
            else:
                st.success("Tudo em ordem! Nenhum produto em situação de compra urgente encontrada.")
        else:
            st.info("Carregue dados de inventário para exibir produtos críticos.")


def render_purchase_recommendations_page(df: pd.DataFrame, mapping: dict) -> None:
    st.markdown(
        "<div class='page-heading'>🛍️ Sugestão de Compras</div>"
        "<div class='page-subtitle'>Lista priorizada de reposição gerada a partir do "
        "estoque atual, do estoque de segurança e do giro de cada produto. Ajuste os "
        "filtros e exporte a lista para o fornecedor.</div>",
        unsafe_allow_html=True,
    )
    
    if not mapping.get("produto") or not mapping.get("estoque"):
        st.warning("É necessário mapear 'produto' e 'estoque' para gerar recomendações de compra.")
        return
        
    try:
        pedido = gerar_pedido_inteligente(df.copy(), mapping)
    except Exception as exc:
        st.error(
            "Erro ao computar as sugestões de compras: "
            + sanitize_error_message(exc, "verifique o mapeamento das colunas.")
        )
        return
        
    if pedido.empty:
        st.info("Nenhum produto necessita de compra baseado no estoque de segurança configurado.")
        return
        
    st.markdown("<div class='section-label'>Filtros e priorização</div>", unsafe_allow_html=True)

    filter_col1, filter_col2, filter_col3 = st.columns([2, 1, 1])
    search_term = filter_col1.text_input(
        "Buscar produto",
        placeholder="Nome ou código do produto",
        key="purchase_search",
    ).strip()

    fornecedores = ["Todos"]
    if "Fornecedor" in pedido.columns:
        fornecedores += sorted(
            {str(value) for value in pedido["Fornecedor"].dropna().tolist()}
        )
    selected_forn = filter_col2.selectbox(
        "Fornecedor", options=fornecedores, key="purchase_supplier"
    )

    status_options = ["Todos"]
    if "Status" in pedido.columns:
        status_options += sorted(
            {str(value) for value in pedido["Status"].dropna().tolist()}
        )
    selected_status = filter_col3.selectbox(
        "Status", options=status_options, key="purchase_status"
    )

    filter_col4, filter_col5 = st.columns([1, 2])
    prioridades = ["Todas", "Alta", "Media", "Baixa"]
    selected_prior = filter_col4.selectbox(
        "Prioridade", options=prioridades, key="purchase_priority"
    )
    sort_option = filter_col5.selectbox(
        "Ordenar por",
        options=[
            "Prioridade e pedido",
            "Maior pedido sugerido",
            "Maior custo estimado",
            "Produto (A-Z)",
        ],
        key="purchase_sort",
    )

    only_with_order = st.checkbox(
        "Mostrar apenas itens com pedido sugerido maior que zero",
        value=True,
        key="purchase_only_order",
    )

    filtered_df = pedido.copy()
    if selected_forn != "Todos" and "Fornecedor" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["Fornecedor"].astype(str) == selected_forn]
    if selected_prior != "Todas" and "Prioridade" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["Prioridade"] == selected_prior]
    if selected_status != "Todos" and "Status" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["Status"] == selected_status]
    if search_term:
        mask = pd.Series(False, index=filtered_df.index)
        for column in ("Produto", "Codigo", "Código"):
            if column in filtered_df.columns:
                mask = mask | filtered_df[column].astype(str).str.contains(
                    search_term, case=False, na=False, regex=False
                )
        filtered_df = filtered_df[mask]
    if only_with_order and "Pedido sugerido" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["Pedido sugerido"] > 0]

    if sort_option == "Maior pedido sugerido" and "Pedido sugerido" in filtered_df.columns:
        filtered_df = filtered_df.sort_values("Pedido sugerido", ascending=False)
    elif sort_option == "Maior custo estimado" and "Valor estimado do pedido" in filtered_df.columns:
        filtered_df = filtered_df.sort_values("Valor estimado do pedido", ascending=False)
    elif sort_option == "Produto (A-Z)" and "Produto" in filtered_df.columns:
        filtered_df = filtered_df.sort_values("Produto", key=lambda s: s.astype(str).str.lower())
    elif "Prioridade" in filtered_df.columns and "Pedido sugerido" in filtered_df.columns:
        priority_order = {"Alta": 0, "Media": 1, "Baixa": 2}
        filtered_df = filtered_df.assign(
            _ordem=filtered_df["Prioridade"].map(priority_order).fillna(3)
        ).sort_values(["_ordem", "Pedido sugerido"], ascending=[True, False]).drop(columns="_ordem")

    total_itens_compra = int((filtered_df["Pedido sugerido"] > 0).sum()) if "Pedido sugerido" in filtered_df.columns else len(filtered_df)
    total_unidades = float(filtered_df["Pedido sugerido"].sum()) if "Pedido sugerido" in filtered_df.columns else 0.0
    
    total_custo = 0.0
    if "Valor estimado do pedido" in filtered_df.columns:
        total_custo = float(filtered_df["Valor estimado do pedido"].sum())
        custo_label = f"R${total_custo:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    else:
        custo_label = "N/D"
        
    cols = st.columns(3)
    cols[0].markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">Itens a Comprar</div>
            <div class="metric-value">{total_itens_compra} SKUs</div>
            <div class="metric-trend neutral">Na seleção atual</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">Unidades Totais</div>
            <div class="metric-value">{total_unidades:,.0f} un.</div>
            <div class="metric-trend neutral">Volume sugerido</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        f"""
        <div class="metric-card alert-kpi">
            <div class="metric-label">Custo Estimado do Pedido</div>
            <div class="metric-value">{custo_label}</div>
            <div class="metric-trend neutral">Com base no ticket médio</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    with st.container():
        st.markdown("<div class='preview-title' style='margin-bottom: 1rem;'>Lista de Sugestões e Justificativas</div>", unsafe_allow_html=True)

        if filtered_df.empty:
            st.markdown(
                """
                <div class="empty-state compact">
                    <div class="empty-state-icon">🔎</div>
                    <div class="empty-state-title">Nenhum item encontrado</div>
                    <div class="empty-state-text">Ajuste a busca ou os filtros para ver outras sugestões de compra.</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            return

        st.caption(f"Exibindo {len(filtered_df)} de {len(pedido)} itens da base de compras.")
        display_df = filtered_df.copy()
        numeric_cols = display_df.select_dtypes(include="number").columns
        display_df[numeric_cols] = display_df[numeric_cols].round(2)

        st.dataframe(display_df, width="stretch", hide_index=True)

        dl1, dl2 = st.columns(2)
        export_df = sanitize_export_dataframe(display_df)
        stamp = datetime.now().strftime("%Y-%m-%d")
        try:
            buffer = BytesIO()
            export_df.to_excel(buffer, index=False, engine="openpyxl")
            dl1.download_button(
                "⬇️ Exportar para Excel",
                data=buffer.getvalue(),
                file_name=f"pedido_sugerido_smartstock_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
        except Exception:
            pass

        csv_data = export_df.to_csv(index=False).encode("utf-8-sig")
        dl2.download_button(
            "⬇️ Exportar para CSV",
            data=csv_data,
            file_name=f"pedido_sugerido_smartstock_{stamp}.csv",
            mime="text/csv",
            width="stretch",
        )


def render_assistant(df: pd.DataFrame | None, mapping: dict[str, str | None], dataset_type: str) -> None:
    st.markdown(
        "<div class='page-heading'>💬 Assistente de IA</div>"
        "<div class='page-subtitle'>Pergunte em linguagem natural sobre ruptura, giro, "
        "excesso de estoque, fornecedores e pedidos sugeridos. As respostas usam a "
        "planilha que você carregou.</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Ver amostra da base analisada pela IA", expanded=False):
        st.dataframe(df.head(10), width="stretch")


def _render_conversation_messages(history: list[dict]) -> None:
    for message in history:
        role = message.get("role") if message.get("role") in ("user", "assistant") else "assistant"
        content = sanitize_output(str(message.get("content", "")), MAX_CHAT_CONTENT)
        if not content:
            continue
        with st.chat_message(role):
            st.markdown(content)


def render_chat_viewer() -> None:
    """Exibe uma conversa salva em modo leitura, mesmo sem planilhas carregadas."""
    session_id = st.session_state.get("viewing_chat_id")
    if not session_id:
        return

    sessions = list_chat_sessions()
    session = next((item for item in sessions if item.get("id") == session_id), None)
    if session is None:
        st.warning("Esta conversa não está mais disponível.")
        st.session_state.viewing_chat_id = None
        return

    if st.session_state.get("chat_session_id") != session_id:
        if not load_chat_session(session_id):
            st.warning(st.session_state.get("chat_error") or "Não foi possível abrir a conversa.")
            st.session_state.viewing_chat_id = None
            return

    title = str(session.get("title") or "Nova conversa")
    relative = format_relative_time(session.get("updated_at"))

    top_left, top_right = st.columns([4, 1])
    with top_left:
        st.markdown(
            f"<div class='page-heading'>💬 {escape_html(title)}</div>"
            f"<div class='page-subtitle'>Conversa salva na sua conta"
            f"{' · ' + escape_html(relative) if relative else ''}.</div>",
            unsafe_allow_html=True,
        )
    with top_right:
        if st.button("← Voltar", width="stretch", key="history_back"):
            st.session_state.viewing_chat_id = None
            st.rerun()

    st.markdown("<div class='chat-readonly-card'>", unsafe_allow_html=True)
    _render_conversation_messages(st.session_state.get("chat_history") or [])
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div style='height:0.75rem'></div>", unsafe_allow_html=True)
    if has_confirmed_data():
        if st.button(
            "Continuar esta conversa no assistente",
            type="primary",
            width="stretch",
            key="history_continue",
        ):
            st.session_state.page = "AI Assistant"
            st.session_state.viewing_chat_id = None
            st.rerun()
    else:
        st.info(
            "Esta é uma conversa salva. Envie e confirme o mapeamento das planilhas "
            "para continuar conversando com o assistente."
        )
        if st.button(
            "📤 Enviar planilhas para continuar",
            type="primary",
            width="stretch",
            key="history_upload",
        ):
            st.session_state.page = "Dashboard"
            st.session_state.viewing_chat_id = None
            st.rerun()


def render_chat_history_page() -> None:
    """Histórico completo de conversas por cliente, acessível a qualquer momento."""
    if not chat_persistence_available():
        st.info(
            "O histórico de conversas fica disponível após entrar com uma conta "
            "conectada ao Supabase."
        )
        return

    if st.session_state.get("viewing_chat_id"):
        render_chat_viewer()
        return

    st.markdown(
        "<div class='page-heading'>🗂️ Suas conversas</div>"
        "<div class='page-subtitle'>Tudo o que você conversou com o assistente fica "
        "salvo na sua conta. Abra, renomeie ou exclua quando quiser.</div>",
        unsafe_allow_html=True,
    )

    confirm_id = st.session_state.get("confirm_delete_chat")
    if confirm_id:
        sessions_for_confirm = list_chat_sessions()
        target = next(
            (item for item in sessions_for_confirm if item.get("id") == confirm_id),
            None,
        )
        target_title = str((target or {}).get("title") or "esta conversa")
        st.warning(
            f"Excluir “{target_title}”? Essa ação não pode ser desfeita."
        )
        confirm_col, cancel_col = st.columns([1, 1])
        if confirm_col.button(
            "Excluir definitivamente",
            type="primary",
            width="stretch",
            key="history_confirm_delete",
        ):
            if delete_chat_session(confirm_id):
                st.session_state.chat_notice = "Conversa excluída."
                st.session_state.confirm_delete_chat = None
                st.rerun()
        if cancel_col.button(
            "Cancelar", width="stretch", key="history_cancel_delete"
        ):
            st.session_state.confirm_delete_chat = None
            st.rerun()

    action_col, search_col = st.columns([1, 2])
    if action_col.button(
        "＋ Nova conversa",
        width="stretch",
        type="primary",
        key="history_new_chat",
    ):
        start_new_chat()
        st.session_state.page = "AI Assistant"
        st.rerun()

    search = search_col.text_input(
        "Buscar conversas",
        placeholder="🔎 Buscar pelo título da conversa",
        label_visibility="collapsed",
        key="chat_history_search",
    )

    if st.session_state.get("chat_notice"):
        st.success(st.session_state.chat_notice)
        st.session_state.chat_notice = None

    sessions = list_chat_sessions()
    if st.session_state.get("chat_sessions_error"):
        st.error(f"Não foi possível carregar as conversas: {st.session_state.chat_sessions_error}")
        return

    if not sessions:
        st.markdown(
            """
            <div class="empty-state">
                <div class="empty-state-icon">🗂️</div>
                <div class="empty-state-title">Nenhuma conversa salva ainda</div>
                <div class="empty-state-text">
                    Envie uma planilha, confirme o mapeamento e faça sua primeira
                    pergunta. A conversa aparecerá aqui automaticamente.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    filtered = filter_chat_sessions(sessions, search)
    if not filtered:
        st.markdown(
            """
            <div class="empty-state compact">
                <div class="empty-state-icon">🔎</div>
                <div class="empty-state-title">Nenhuma conversa encontrada</div>
                <div class="empty-state-text">Tente buscar por outro título.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    st.caption(f"Exibindo {len(filtered)} de {len(sessions)} conversa(s).")

    for group_title, items in group_chat_sessions(filtered):
        st.markdown(
            f"<div class='history-group-title'>{escape_html(group_title)}</div>",
            unsafe_allow_html=True,
        )
        for session in items:
            session_id = session.get("id")
            if not session_id:
                continue
            title = str(session.get("title") or "Nova conversa")
            relative = format_relative_time(session.get("updated_at"))
            active = session_id == st.session_state.get("chat_session_id")
            with st.container(border=True):
                info_col, open_col, menu_col = st.columns([6, 1.2, 0.8])
                with info_col:
                    active_badge = (
                        "<span class='history-active-badge'>Em uso</span>"
                        if active
                        else ""
                    )
                    st.markdown(
                        f"<div class='history-item-title'>{escape_html(title)}"
                        f"{active_badge}</div>"
                        f"<div class='history-item-meta'>💬 Conversa salva"
                        f"{' · ' + escape_html(relative) if relative else ''}</div>",
                        unsafe_allow_html=True,
                    )
                if open_col.button(
                    "Abrir",
                    width="stretch",
                    key=f"history_open_{session_id}",
                ):
                    if open_chat_session(session_id):
                        st.rerun()
                with menu_col.popover(
                    "⋯", width="stretch", help="Renomear ou excluir esta conversa"
                ):
                    new_title = st.text_input(
                        "Novo título",
                        value=title,
                        max_chars=80,
                        key=f"history_rename_{session_id}",
                    )
                    if st.button(
                        "Salvar título",
                        width="stretch",
                        key=f"history_save_{session_id}",
                    ):
                        if rename_chat_session(session_id, new_title):
                            st.session_state.chat_notice = "Título atualizado."
                            st.rerun()
                    if st.button(
                        "Excluir conversa",
                        width="stretch",
                        key=f"history_delete_{session_id}",
                    ):
                        st.session_state.confirm_delete_chat = session_id
                        st.rerun()



def local_fallback_answer(df: pd.DataFrame, question: str, mapping: dict[str, str | None]) -> str:
    _normalize_numeric = utils_module._normalize_numeric
    product_col = mapping.get("produto")
    estoque_col = mapping.get("estoque")
    venda_col = mapping.get("vendas")

    parts = []
    if product_col:
        parts.append(f"Produtos unicos analisados: {df[product_col].nunique()}.")
    if estoque_col:
        estoque_total = _normalize_numeric(df[estoque_col]).sum()
        parts.append(f"Estoque total identificado: {estoque_total:,.0f}.".replace(",", "."))
    if venda_col:
        venda_total = _normalize_numeric(df[venda_col]).sum()
        parts.append(f"Vendas acumuladas na base: {venda_total:,.0f}.".replace(",", "."))
    elif mapping.get("quantidade") or product_col:
        quantity_col = mapping.get("quantidade")
        quantidade_total = _normalize_numeric(df[quantity_col]).sum() if quantity_col else len(df)
        parts.append(f"Movimentação total analisada: {quantidade_total:,.0f}.".replace(",", "."))

    if not parts:
        parts.append("Recebi sua pergunta e li a planilha, mas preciso de pelo menos uma coluna de produto ou data para responder com mais profundidade.")

    parts.append(f"Pergunta recebida: {sanitize_input(question, MAX_CHAT_QUESTION)}")
    parts.append("Se você quiser, eu também posso transformar esses dados em sugestão de pedido na aba Sugestão de Compras.")
    return " ".join(parts)


def render_chat(df: pd.DataFrame | None, model, mapping: dict[str, str | None], dataset_type: str) -> None:
    active_title = "Nova conversa"
    active_id = st.session_state.get("chat_session_id")
    persistence = chat_persistence_available()
    if active_id:
        for session in list_chat_sessions():
            if session.get("id") == active_id:
                active_title = session.get("title") or active_title
                break

    title_col, history_col, new_col = st.columns([5, 1.1, 1.1])
    with title_col:
        st.markdown(
            f"<div class='chat-title'>💬 {escape_html(active_title)}</div>",
            unsafe_allow_html=True,
        )
        if persistence:
            st.markdown(
                "<div class='chat-save-status'><span class='chat-save-dot'></span>"
                "Salvo automaticamente na sua conta</div>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("Nesta sessão as mensagens não são salvas. Entre com uma conta para manter o histórico.")
    with history_col:
        if persistence and st.button(
            "🗂️ Histórico",
            width="stretch",
            key="chat_open_history",
            help="Ver todas as conversas salvas na sua conta",
        ):
            st.session_state.page = "Chat History"
            st.session_state.viewing_chat_id = None
            st.rerun()
    with new_col:
        if st.button(
            "＋ Nova",
            width="stretch",
            key="new_chat_btn",
            help="Começar uma conversa em branco",
        ):
            start_new_chat()
            st.rerun()

    if st.session_state.get("chat_error"):
        st.warning(
            "Não foi possível salvar/carregar a conversa no Supabase: "
            f"{st.session_state.chat_error}"
        )

    history = st.session_state.chat_history or []
    is_new_conversation = (
        not active_id
        and len(history) <= 1
        and (not history or history[0].get("role") == "assistant")
    )
    if is_new_conversation:
        st.markdown(
            """
            <div class="chat-welcome">
                <div class="chat-welcome-icon">🤖</div>
                <div class="chat-welcome-title">Como posso ajudar com seu estoque?</div>
                <div class="chat-welcome-text">
                    Pergunte sobre ruptura, giro, produtos parados, fornecedores
                    ou pedidos sugeridos. Eu analiso a planilha e respondo com os números.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        messages_to_render = []
    else:
        messages_to_render = history

    _render_conversation_messages(messages_to_render)

    if df is not None:
        with st.expander("💡 Sugestões de consulta", expanded=is_new_conversation):
            c1, c2 = st.columns(2)
            with c1:
                if st.button(
                    "🚨 Risco de ruptura",
                    width="stretch",
                    key="q_ruptura",
                    help="Produtos que podem faltar nos próximos dias",
                ):
                    st.session_state.pending_chat_question = "Quais produtos estão em risco de ruptura?"
                if st.button(
                    "🛍️ Lista de compra",
                    width="stretch",
                    key="q_compra",
                    help="Pedido priorizado por urgência e custo",
                ):
                    st.session_state.pending_chat_question = "Me dê a lista de compra priorizada"
            with c2:
                if st.button(
                    "📦 Estoque parado",
                    width="stretch",
                    key="q_parado",
                    help="Itens com excesso ou sem giro",
                ):
                    st.session_state.pending_chat_question = "Quais produtos estão com estoque parado ou excesso?"
                if st.button(
                    "📈 Mais vendido",
                    width="stretch",
                    key="q_top",
                    help="Produto com maior volume de saída",
                ):
                    st.session_state.pending_chat_question = "Qual é o produto mais vendido?"

    placeholder = "Pergunte sobre estoque, ruptura, curva de vendas, fornecedor ou pedido sugerido"
    question = st.chat_input(placeholder, max_chars=MAX_CHAT_QUESTION)
    st.caption("A IA pode cometer erros. Confira os números antes de tomar decisões de compra.")

    if st.session_state.pending_chat_question:
        question = st.session_state.pending_chat_question
        st.session_state.pending_chat_question = None

    question = sanitize_input(question, MAX_CHAT_QUESTION)
    if not question:
        return

    expanded_question = question
    expanded_context = {}
    if st.session_state.normalized_df is not None and not st.session_state.normalized_df.empty:
        expanded_question, expanded_context = expandir_pergunta_por_contexto(
            question,
            st.session_state.normalized_df,
            st.session_state.last_chat_context,
        )

    st.session_state.chat_history.append({"role": "user", "content": question})
    save_chat_message("user", question)
    with st.chat_message("user"):
        st.markdown(question)

    if df is None:
        answer = "Envie uma ou mais planilhas antes de iniciar a análise."
    elif not mapping_is_ready_for_type(mapping, dataset_type):
        required = ", ".join(get_dataset_requirements(dataset_type))
        answer = f"Confirme primeiro o mapeamento de {required} para eu responder com contexto correto."
    else:
        with st.spinner("Analisando seus dados..."):
            local_answer = responder_pergunta_local(df, expanded_question, mapping, st.session_state.normalized_df)
            if local_answer:
                answer = local_answer
                st.session_state.model_ready = True
                st.session_state.last_ai_error = None
            elif model is not None:
                try:
                    answer = analisar_pergunta(model, df, expanded_question, mapping, dataset_type)
                    st.session_state.model_ready = True
                    st.session_state.last_ai_error = None
                except Exception as exc:
                    st.session_state.last_ai_error = sanitize_error_message(
                        exc, "Falha ao consultar o modelo de IA."
                    )
                    answer = local_fallback_answer(df, expanded_question, mapping)
                    st.session_state.model_ready = False
            else:
                api_key = get_api_key()
                if not api_key:
                    st.session_state.last_ai_error = "Nenhuma chave de API foi encontrada. Configure GOOGLE_API_KEY no arquivo .streamlit/secrets.toml."
                else:
                    st.session_state.last_ai_error = "O modelo Gemini não foi carregado. Verifique a chave, dependências e permissão de acesso ao modelo."
                answer = local_fallback_answer(df, expanded_question, mapping)
                st.session_state.model_ready = False

    intent = inferir_intencao_pergunta(expanded_question)
    response_context = inferir_entidade_da_resposta(answer, st.session_state.normalized_df)
    st.session_state.last_chat_context = {
        "intent": intent or expanded_context.get("intent"),
        "product": response_context.get("product") or expanded_context.get("product"),
        "code": response_context.get("code") or expanded_context.get("code"),
        "payment": response_context.get("payment") or expanded_context.get("payment"),
        "period": response_context.get("period") or expanded_context.get("period"),
        "question": expanded_question,
    }

    answer = sanitize_output(str(answer), MAX_CHAT_CONTENT)
    if not answer:
        answer = "Não consegui gerar uma resposta com os dados atuais."

    st.session_state.chat_history.append({"role": "assistant", "content": answer})
    save_chat_message("assistant", answer)
    with st.chat_message("assistant"):
        if expanded_context.get("expanded"):
            st.caption(f"Contexto usado: {expanded_question}")
        st.markdown(answer)
        if st.session_state.last_ai_error:
            st.caption(f"Erro da IA: {st.session_state.last_ai_error}")
    st.rerun()


def show_auth_notice() -> None:
    notice = st.session_state.get("auth_notice")
    if not notice:
        return
    kind = st.session_state.get("auth_notice_kind", "info")
    renderer = {"success": st.success, "warning": st.warning, "error": st.error}.get(
        kind, st.info
    )
    renderer(notice)
    st.session_state.auth_notice = None
    st.session_state.auth_notice_kind = "info"


def render_login() -> None:
    st.markdown(
        "<style>[data-testid=\"stAppViewContainer\"] > .main { padding: 0 !important; }</style>",
        unsafe_allow_html=True,
    )
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.markdown(
            """
            <div class="login-left-panel">
                <div class="login-left-logo">SmartStock <span>AI</span></div>
                <div class="login-left-headline">Inteligência Artificial<br>para Gestão de Estoque</div>
                <p class="login-left-text">Transforme dados em decisões estratégicas com análises preditivas, dashboards inteligentes e sugestões automatizadas de compras.</p>
                <div class="login-left-features">
                    <div class="feature-item"><span class="feature-icon">📊</span> Dashboard Inteligente</div>
                    <div class="feature-item"><span class="feature-icon">🤖</span> Assistente IA</div>
                    <div class="feature-item"><span class="feature-icon">🛒</span> Sugestão de Compras</div>
                </div>
                <div class="login-left-illustration">
                    <div class="ill-chart">
                        <div class="ill-bar" style="height:40px"></div>
                        <div class="ill-bar" style="height:65px"></div>
                        <div class="ill-bar" style="height:30px"></div>
                        <div class="ill-bar" style="height:75px"></div>
                        <div class="ill-bar" style="height:50px"></div>
                        <div class="ill-bar" style="height:60px"></div>
                    </div>
                    <div class="ill-boxes">
                        <div class="ill-box"></div>
                        <div class="ill-box"></div>
                        <div class="ill-box"></div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_right:
        supabase_ok, supabase_message = get_supabase_status()
        if not supabase_ok:
            st.warning(supabase_message)

        if st.session_state.get("password_recovery_mode"):
            render_reset_password()
        elif st.session_state.get("auth_view") == "register":
            render_register_form()
        elif st.session_state.get("auth_view") == "forgot":
            render_forgot_form()
        else:
            render_login_form()


def render_login_form() -> None:
    st.markdown('<div class="login-card-title">Acesse sua conta</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="login-card-subtitle">Entre com seu e-mail e senha.</div>',
        unsafe_allow_html=True,
    )
    show_auth_notice()

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input(
            "E-mail", placeholder="seu@email.com", max_chars=254
        )
        password = st.text_input(
            "Senha",
            type="password",
            placeholder="Sua senha",
            key="login_pass",
            max_chars=PASSWORD_MAX_LENGTH,
        )

        _, forgot_col = st.columns([3, 2])
        with forgot_col:
            st.markdown(
                '<div class="login-forgot-link"><a href="?forgot=1">Esqueci minha senha</a></div>',
                unsafe_allow_html=True,
            )

        submitted = st.form_submit_button(
            "Entrar na Plataforma", width="stretch", type="primary"
        )

    # O link usa query param para não virar um segundo botão de submit
    # (assim o Enter no formulário continua fazendo login).
    if _query_param("forgot"):
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.session_state.auth_view = "forgot"
        st.rerun()

    if submitted:
        if validate_login(username, password):
            st.session_state.authenticated = True
            st.session_state.login_error = None
            st.rerun()
        else:
            st.error(st.session_state.get("login_error") or "E-mail ou senha inválidos.")
            st.session_state.login_error = None
    elif st.session_state.get("login_error"):
        st.warning(st.session_state.login_error)
        st.session_state.login_error = None

    st.markdown(
        '<div class="login-divider">ou continue com</div>',
        unsafe_allow_html=True,
    )

    oauth_url = build_google_oauth_url()
    if oauth_url:
        st.markdown(
            f"""
            <a class="oauth-google-btn" href="{escape_html(oauth_url)}" target="_self" rel="noopener noreferrer">
                <span class="oauth-google-icon">G</span>
                <span>Entrar com Google</span>
            </a>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info("O login com Google fica disponível após configurar o Supabase.")

    st.markdown('<div class="login-footer">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    c1.markdown(
        '<div style="font-size:0.85rem;color:#64748B;padding-top:4px">Ainda não possui conta?</div>',
        unsafe_allow_html=True,
    )
    if c2.button(
        "Criar conta gratuita",
        key="goto_register_btn",
        width="stretch",
    ):
        st.session_state.auth_view = "register"
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def render_forgot_form() -> None:
    st.markdown('<div class="login-card-title">Recuperar senha</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="login-card-subtitle">Informe seu e-mail e enviaremos um link seguro para criar uma nova senha.</div>',
        unsafe_allow_html=True,
    )
    show_auth_notice()

    with st.form("forgot_form", clear_on_submit=False):
        email = st.text_input("E-mail cadastrado", placeholder="seu@email.com", max_chars=254)
        submitted = st.form_submit_button(
            "Enviar link de recuperação", width="stretch"
        )

    if submitted:
        email = sanitize_input(email, max_length=254).lower()
        if not is_valid_email(email):
            st.error("Informe um e-mail válido.")
        else:
            ok, message = request_password_reset(email)
            if ok:
                st.session_state.auth_notice = message
                st.session_state.auth_notice_kind = "success"
                st.session_state.auth_view = "login"
                st.rerun()
            else:
                st.error(message)

    st.markdown('<div class="login-footer">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    c1.markdown(
        '<div style="font-size:0.85rem;color:#64748B;padding-top:4px">Lembrou a senha?</div>',
        unsafe_allow_html=True,
    )
    if c2.button("Voltar para o login", key="back_to_login_btn", width="stretch"):
        st.session_state.auth_view = "login"
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def render_reset_password() -> None:
    st.markdown('<div class="login-card-title">Definir nova senha</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="login-card-subtitle">Você acessou pelo link de recuperação. Crie uma nova senha para sua conta.</div>',
        unsafe_allow_html=True,
    )
    show_auth_notice()

    user = st.session_state.get("supabase_user") or {}
    email = sanitize_input(user.get("email") or "", max_length=254)
    if email:
        st.caption(f"Conta: {email}")

    with st.form("reset_password_form", clear_on_submit=False):
        password = st.text_input(
            "Nova senha",
            type="password",
            placeholder="Crie uma nova senha",
            key="reset_pass",
            max_chars=PASSWORD_MAX_LENGTH,
            help=f"Mínimo de {PASSWORD_MIN_LENGTH} caracteres.",
        )
        confirm = st.text_input(
            "Confirmar nova senha",
            type="password",
            placeholder="Repita a nova senha",
            key="reset_confirm",
            max_chars=PASSWORD_MAX_LENGTH,
        )
        submitted = st.form_submit_button("Salvar nova senha", width="stretch")

    if submitted:
        errors = []
        if not password or len(password) < PASSWORD_MIN_LENGTH:
            errors.append(f"A senha deve ter no mínimo {PASSWORD_MIN_LENGTH} caracteres.")
        elif len(password) > PASSWORD_MAX_LENGTH:
            errors.append(f"A senha deve ter no máximo {PASSWORD_MAX_LENGTH} caracteres.")
        elif email and password.lower() == email.lower():
            errors.append("A senha não pode ser igual ao e-mail.")
        if password != confirm:
            errors.append("As senhas não conferem.")

        if errors:
            for error in errors:
                st.error(error)
        else:
            client = get_supabase_client()
            if client is None:
                st.error("Não foi possível conectar ao Supabase. Tente novamente.")
            else:
                try:
                    client.auth.update_user({"password": password})
                except Exception as exc:
                    st.error(
                        sanitize_error_message(
                            exc, "Não foi possível redefinir a senha. Solicite um novo link."
                        )
                    )
                else:
                    try:
                        client.auth.sign_out()
                    except Exception:
                        pass
                    st.session_state.password_recovery_mode = False
                    st.session_state.authenticated = False
                    st.session_state.supabase_user = None
                    st.session_state.user_id = None
                    st.session_state.user_profile = None
                    st.session_state.auth_view = "login"
                    st.session_state.auth_notice = (
                        "Senha redefinida com sucesso. Entre com a nova senha."
                    )
                    st.session_state.auth_notice_kind = "success"
                    st.rerun()

    if st.button("Cancelar e voltar ao login", key="cancel_reset_btn", width="stretch"):
        client = get_supabase_client()
        if client is not None:
            try:
                client.auth.sign_out()
            except Exception:
                pass
        st.session_state.password_recovery_mode = False
        st.session_state.authenticated = False
        st.session_state.supabase_user = None
        st.session_state.user_id = None
        st.session_state.user_profile = None
        st.session_state.auth_view = "login"
        st.rerun()


def render_register_form() -> None:
    st.markdown('<div class="login-card-title">Criar conta gratuita</div>', unsafe_allow_html=True)
    st.markdown('<div class="login-card-subtitle">Preencha os dados para se cadastrar na plataforma</div>', unsafe_allow_html=True)
    show_auth_notice()

    with st.form("register_form", clear_on_submit=False):
        name = st.text_input("Nome completo", placeholder="Seu nome", max_chars=120)
        email = st.text_input("E-mail", placeholder="seu@email.com", max_chars=254)
        password = st.text_input(
            "Senha", type="password", placeholder="Crie uma senha", key="reg_pass", max_chars=PASSWORD_MAX_LENGTH,
            help=f"Mínimo de {PASSWORD_MIN_LENGTH} caracteres. Evite dados óbvios como nome ou e-mail.",
        )
        confirm = st.text_input(
            "Confirmar senha", type="password", placeholder="Confirme a senha", key="reg_confirm", max_chars=PASSWORD_MAX_LENGTH
        )

        st.markdown('<div class="login-btn-wrapper">', unsafe_allow_html=True)
        submitted = st.form_submit_button("Criar Conta", width="stretch")
        st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        name = sanitize_input(name, max_length=120)
        email = sanitize_input(email, max_length=254).lower()
        errors = []
        if not name:
            errors.append("Nome é obrigatório.")
        if not email:
            errors.append("E-mail é obrigatório.")
        elif not is_valid_email(email):
            errors.append("Informe um e-mail válido.")
        if not password or len(password) < PASSWORD_MIN_LENGTH:
            errors.append(f"Senha deve ter no mínimo {PASSWORD_MIN_LENGTH} caracteres.")
        elif len(password) > PASSWORD_MAX_LENGTH:
            errors.append(f"Senha deve ter no máximo {PASSWORD_MAX_LENGTH} caracteres.")
        elif password.lower() == email:
            errors.append("A senha não pode ser igual ao e-mail.")
        elif name and password.lower() == name.lower():
            errors.append("A senha não pode ser igual ao nome.")
        if password != confirm:
            errors.append("Senhas não conferem.")

        if errors:
            for err in errors:
                st.error(err)
        elif supabase_is_configured():
            sign_up_result, sign_up_error = supabase_sign_up(email, password, name)
            if sign_up_result is None:
                st.error(sign_up_error or "Erro ao criar conta. Tente novamente.")
            else:
                session = getattr(sign_up_result, "session", None)
                user = getattr(sign_up_result, "user", None) or getattr(session, "user", None)
                if session is not None and user is not None:
                    st.session_state.supabase_user = (
                        user.model_dump() if hasattr(user, "model_dump") else None
                    )
                    st.session_state.authenticated = True
                    st.session_state.session_started_at = time.time()
                    st.session_state.last_activity_at = time.time()
                    st.session_state.auth_view = "login"
                    st.rerun()
                else:
                    st.session_state.auth_notice = (
                        "Conta criada! Enviamos um link de confirmação para o seu e-mail. "
                        "Confirme e depois entre na plataforma."
                    )
                    st.session_state.auth_notice_kind = "success"
                    st.session_state.auth_view = "login"
                    st.rerun()
        else:
            # Fallback local apenas quando o Supabase não está configurado.
            if load_users().get(email):
                st.error("E-mail já cadastrado.")
            else:
                save_user(email, password, name)
                st.session_state.auth_notice = "Conta criada com sucesso! Faça o login."
                st.session_state.auth_notice_kind = "success"
                st.session_state.auth_view = "login"
                st.rerun()

    st.markdown('<div class="login-footer">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    c1.markdown('<div style="font-size:0.85rem;color:#64748B;padding-top:4px">Já tem conta?</div>', unsafe_allow_html=True)
    if c2.button("Fazer login", key="goto_login_btn", width="stretch"):
        st.session_state.auth_view = "login"
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def main() -> None:
    load_css()
    init_state()

    handle_auth_callback()

    if not st.session_state.authenticated:
        render_login()
        return

    if session_expired():
        logout("Sua sessão expirou por inatividade. Entre novamente.")

    load_persisted_chat()

    st.markdown(
        "<style>[data-testid=\"stAppViewContainer\"] > .main { padding: 1.5rem 2rem 4rem !important; }"
        "#MainMenu {visibility: hidden;} footer {visibility: hidden;}</style>",
        unsafe_allow_html=True,
    )

    model, model_error = get_model()
    st.session_state.last_model_error = model_error

    if "initialized_saas" not in st.session_state:
        st.session_state.initialized_saas = True

    render_sidebar()

    df = st.session_state.df

    if st.session_state.page == "Chat History":
        if chat_persistence_available():
            if df is not None:
                render_header(df, st.session_state.column_mapping)
            render_chat_history_page()
            return
        st.session_state.page = "Dashboard"

    if df is None:
        render_header(None, None)
        render_upload(model)
        return

    mapping = st.session_state.column_mapping
    
    if not st.session_state.mapping_confirmed:
        render_header(df, mapping)
        render_stepper(2)
        render_mapping_panel(df)
        return

    dataset_type = infer_dataset_type(df, mapping) if st.session_state.dataset_type == "auto" else st.session_state.dataset_type
    page = st.session_state.page
    render_stepper(3)

    if page == "Dashboard":
        render_dashboard_saas(df, mapping, dataset_type)
    elif page == "AI Assistant":
        render_assistant(df, mapping, dataset_type)
        render_chat(df, model, mapping, dataset_type)
    elif page == "Purchase Recommendations":
        render_purchase_recommendations_page(df, mapping)
    else:
        st.session_state.page = "AI Assistant"


if __name__ == "__main__":
    main()
