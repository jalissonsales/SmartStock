import csv
import hashlib
import importlib.util
import json
import os
import unicodedata
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st


st.set_page_config(page_title="SmartStock AI", layout="wide", menu_items={"Get Help": None, "Report a Bug": None, "About": None})


BASE_DIR = Path(__file__).resolve().parent
UTILS_PATH = BASE_DIR / "utils.py"
CSS_PATH = BASE_DIR / "style.css"
LOGO_PATH = BASE_DIR / "smartstock_ai_logo-removebg-preview.png"
UPLOAD_SCHEMA_VERSION = "2026-04-08-clean-pivot-v2"
USERS_FILE = BASE_DIR / ".users.json"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def load_users() -> dict:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_user(email: str, password: str, name: str) -> None:
    users = load_users()
    users[email.strip().lower()] = {"password": _hash_password(password), "name": name}
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def validate_login(email: str, password: str) -> bool:
    if not email or not password:
        return False
    email = email.strip().lower()
    pw_hash = _hash_password(password)

    admin_user = st.secrets.get("LOGIN_USERNAME", "admin").strip().lower()
    admin_pass = st.secrets.get("LOGIN_PASSWORD", "smartstock2024")
    if email == admin_user and password == admin_pass:
        return True

    users = load_users()
    user = users.get(email)
    return user is not None and user["password"] == pw_hash


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
        "chat_history": [
            {
                "role": "assistant",
                "content": "Envie sua planilha para eu analisar estoque, gerar dashboards e sugerir pedidos inteligentes.",
            }
        ],
        "pedido_preview": None,
        "pedido_status": None,
        "model_ready": False,
        "last_ai_error": None,
        "last_model_error": None,
        "safety_stock_days": 15,
        "lead_time_days": 7,
        "forecast_horizon_days": 30,
        "authenticated": False,
        "show_register": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


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
        return None, str(exc)


def get_model():
    api_key = get_api_key()
    if not api_key:
        return None, "Nenhuma chave de API foi encontrada. Configure GOOGLE_API_KEY no arquivo .streamlit/secrets.toml."
    return load_model(api_key)


 
 


def read_uploaded_files(files) -> tuple[pd.DataFrame | None, list[str], str | None]:
    dataframes = []
    names = []

    for uploaded in files:
        suffix = Path(uploaded.name).suffix.lower()
        try:
            if suffix == ".csv":
                csv_bytes = uploaded.getvalue()
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
                excel_bytes = uploaded.getvalue()
                df = pd.read_excel(BytesIO(excel_bytes))
        except Exception as exc:
            return None, [], f"Erro ao ler {uploaded.name}: {exc}"

        df = sanitize_dataframe(df)
        df["Arquivo origem"] = uploaded.name
        dataframes.append(df)
        names.append(uploaded.name)

    if not dataframes:
        return None, [], None

    combined = pd.concat(dataframes, ignore_index=True)
    return combined, names, None


def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()

    def normalize_label(value: str) -> str:
        text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
        return text.strip().lower()

    renamed_columns = []
    for idx, column in enumerate(cleaned.columns):
        column_name = str(column).strip()
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
    field_catalog = {
        "produto": "Coluna de produto",
        "codigo": "Coluna de codigo",
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
        "auto": ["produto", "codigo", "data", "data_hora", "categoria", "valor"],
        "vendas": ["produto", "codigo", "data", "data_hora", "forma_pagamento", "valor", "quantidade", "categoria", "filial"],
        "estoque": ["produto", "codigo", "estoque", "categoria", "filial", "fornecedor"],
        "misto": ["produto", "codigo", "vendas", "estoque", "data", "data_hora", "categoria", "valor", "quantidade", "filial", "fornecedor"],
        "financeiro": ["data", "data_hora", "forma_pagamento", "valor", "categoria", "filial"],
    }

    ordered_keys = []
    for key in base_fields.get(dataset_type, base_fields["auto"]):
        if mapping.get(key) and key not in ordered_keys:
            ordered_keys.append(key)
    for key, value in mapping.items():
        if value and key in field_catalog and key not in ordered_keys:
            ordered_keys.append(key)

    return [(key, field_catalog[key]) for key in ordered_keys if mapping.get(key)]


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
                Tipo da base em uso: <strong>{dataset_type.capitalize()}</strong><br>
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
            st.dataframe(pd.DataFrame(mapping_rows), use_container_width=True, hide_index=True)

        if normalized_df is not None and not normalized_df.empty:
            st.markdown("<div class='preview-title'>Resumo da base normalizada</div>", unsafe_allow_html=True)
            st.caption(gerar_resumo_quantitativo(normalized_df))
            st.dataframe(normalized_df.head(12), use_container_width=True, height=260)


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
    st.markdown(
        """
        <div class="feature-card" style="margin-bottom: 1.25rem;">
            Confirme quais colunas representam produto, vendas, estoque, data e categoria.
            Produto, vendas e estoque são obrigatórios para liberar os relatórios de simulação e o dashboard.
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
                    <div class="mapping-summary-value">{auto_type.capitalize()}</div>
                </div>
                <div>
                    <div class="mapping-summary-label">Confiança da leitura</div>
                    <div class="mapping-summary-value">{confidence_label}</div>
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
    if st.button("Usar sugestão automática", use_container_width=True, key="apply_auto_mapping"):
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
            st.caption("Mapeie as colunas de entrada correspondentes aos conceitos operacionais.")

            containers = [col1, col2, col3]
            for idx, (field, label) in enumerate(visible_fields):
                container = containers[idx % 3]
                current = mapping.get(field) or ""
                index = columns.index(current) if current in columns else 0
                selected[field] = container.selectbox(label, options=columns, index=index, key=f"map_{field}")

            confirm = st.form_submit_button("Confirmar Mapeamento", use_container_width=True)

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
                required = ", ".join(get_dataset_requirements(resolved_type))
                st.warning(f"Mapeie pelo menos estes campos para continuar: {required}.")
                
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
            tags.append(f"<span class='mapping-tag'><strong>{label}:</strong> {value}</span>")
    if tags:
        st.markdown(f"<div class='mapping-tags'>{''.join(tags)}</div>", unsafe_allow_html=True)
        
    return active_mapping


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

    options = [
        ("AI Assistant", "💬 Assistente IA"),
        ("Dashboard", "🖥️ Dashboard"),
        ("Purchase Recommendations", "🛍️ Sugestão de Compras"),
    ]

    for option_id, option_label in options:
        active = st.session_state.page == option_id
        if st.sidebar.button(
            option_label,
            key=f"nav_{option_id}",
            use_container_width=True,
            type="primary" if active else "secondary",
        ):
            st.session_state.page = option_id
            st.rerun()

    st.sidebar.markdown("<hr style='border-color:#1E293B; margin: 1.5rem 0;'>", unsafe_allow_html=True)

    if st.sidebar.button("🚪 Logout", use_container_width=True, key="nav_logout"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()

    st.sidebar.markdown("<hr style='border-color:#1E293B; margin: 1.5rem 0;'>", unsafe_allow_html=True)

    if st.session_state.get("df") is not None:
        uploads = st.session_state.uploads or []
        st.sidebar.markdown(
            f"<div style='font-size:0.8rem;color:#94A3B8;padding:0 0.5rem 0.5rem'>📄 {len(uploads)} arquivo(s) · {len(st.session_state.df):,} linhas</div>",
            unsafe_allow_html=True,
        )
        if st.sidebar.button("＋ Adicionar", key="toggle_extra_upload", use_container_width=True):
            st.session_state.show_extra_upload = not st.session_state.get("show_extra_upload", False)
            st.rerun()

        if st.session_state.get("show_extra_upload"):
            st.sidebar.markdown('<div class="extra-upload-area">', unsafe_allow_html=True)
            extra_files = st.file_uploader(
                "Selecione arquivos",
                type=["csv", "xlsx"],
                accept_multiple_files=True,
                label_visibility="collapsed",
                key="extra_uploader",
            )
            if extra_files:
                extra_combined, extra_names, extra_error = read_uploaded_files(extra_files)
                if extra_error:
                    st.error(extra_error)
                else:
                    new_df = pd.concat([st.session_state.df, extra_combined], ignore_index=True)
                    st.session_state.df = new_df
                    st.session_state.uploads = list(dict.fromkeys(st.session_state.uploads + extra_names))
                    st.session_state.normalized_df = construir_base_normalizada(new_df, st.session_state.column_mapping)
                    st.session_state.pedido_status = None
                    st.session_state.pedido_preview = None
                    st.session_state.last_chat_context = {}
                    st.session_state.show_extra_upload = False
                    st.success(f"{len(extra_names)} arquivo(s) adicionado(s): {', '.join(extra_names)}")
                    st.rerun()
            st.sidebar.markdown('</div>', unsafe_allow_html=True)

        if st.sidebar.button("🔄 Trocar planilha", use_container_width=True, key="nav_reset"):
            st.session_state.df = None
            st.session_state.uploads = []
            st.session_state.column_mapping = {}
            st.session_state.normalized_df = None
            st.session_state.mapping_confirmed = False
            st.session_state.page = "Dashboard"
            st.session_state.pedido_preview = None
            st.session_state.pedido_status = None
            st.session_state.last_chat_context = {}
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
                <p>{summary_text}</p>
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


def render_upload(model=None) -> None:
    st.markdown(
        """
        <div class="section-label">Importação de Arquivos</div>
        <div class="upload-helper">Selecione planilhas Excel, ou arquivos CSV.</div>
        """,
        unsafe_allow_html=True,
    )
    files = st.file_uploader(
        "Upload de arquivos",
        type=["csv", "xlsx"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )

    if files:
        current_signature = (UPLOAD_SCHEMA_VERSION, tuple((uploaded.name, len(uploaded.getvalue())) for uploaded in files))
        if st.session_state.upload_signature != current_signature:
            combined, names, error = read_uploaded_files(files)
            if error:
                st.error(error)
            else:
                st.session_state.df = combined
                st.session_state.uploads = names
                st.session_state.upload_signature = current_signature
                st.session_state.pedido_status = None
                st.session_state.pedido_preview = None
                st.session_state.last_chat_context = {}
                ai_result = {"dataset_type": "auto", "mapping": {}, "source": "local"}
                if model is not None:
                    try:
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
                st.success(f"{len(names)} arquivo(s) carregado(s): {', '.join(names)}")
                st.rerun()
        elif st.session_state.uploads and st.session_state.df is not None:
            st.info(
                f"{len(st.session_state.uploads)} arquivo(s) consolidados em {len(st.session_state.df):,} linha(s)."
            )

    if st.session_state.uploads:
        st.markdown(
            f"""
            <div class="upload-summary">
                <strong>Arquivos ativos:</strong> {", ".join(st.session_state.uploads)}<br>
                <strong>Total de linhas consolidadas:</strong> {len(st.session_state.df):,}
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_dashboard_saas(df: pd.DataFrame, mapping: dict, dataset_type: str) -> None:
    import plotly.express as px
    import plotly.graph_objects as go

    render_header(df, mapping)
    
    normalized = st.session_state.normalized_df
    if normalized is None or normalized.empty:
        st.warning("Problema ao normalizar a base de dados.")
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
                st.plotly_chart(fig_line, use_container_width=True, config={"displayModeBar": False})
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
                st.plotly_chart(fig_pie, use_container_width=True, config={"displayModeBar": False})
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
                st.dataframe(urgentes[visible_cols].round(2), use_container_width=True, hide_index=True)
            else:
                st.success("Tudo em ordem! Nenhum produto em situação de compra urgente encontrada.")
        else:
            st.info("Carregue dados de inventário para exibir produtos críticos.")


def render_purchase_recommendations_page(df: pd.DataFrame, mapping: dict) -> None:
    st.markdown("<div class='section-label'>Sugestão de Compras (Procurement)</div>", unsafe_allow_html=True)
    st.markdown(
        """
        <div class="feature-card" style="margin-bottom: 1.5rem;">
            Sugestões de compra geradas automaticamente com base no Estoque Alvo parametrizado.
        </div>
        """,
        unsafe_allow_html=True,
    )
    
    if not mapping.get("produto") or not mapping.get("estoque"):
        st.warning("É necessário mapear 'produto' e 'estoque' para gerar recomendações de compra.")
        return
        
    try:
        pedido = gerar_pedido_inteligente(df.copy(), mapping)
    except Exception as exc:
        st.error(f"Erro ao computar as sugestões de compras: {exc}")
        return
        
    if pedido.empty:
        st.info("Nenhum produto necessita de compra baseado no estoque de segurança configurado.")
        return
        
    filters_col1, filters_col2 = st.columns(2)
    
    fornecedores = ["Todos"]
    if "Fornecedor" in pedido.columns:
        fornecedores += sorted(pedido["Fornecedor"].dropna().unique().tolist())
        
    selected_forn = filters_col1.selectbox("Filtrar por Fornecedor", options=fornecedores)
    
    prioridades = ["Todas", "Alta", "Media", "Baixa"]
    selected_prior = filters_col2.selectbox("Filtrar por Prioridade", options=prioridades)
    
    filtered_df = pedido.copy()
    if selected_forn != "Todos" and "Fornecedor" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["Fornecedor"] == selected_forn]
    if selected_prior != "Todas" and "Prioridade" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["Prioridade"] == selected_prior]
        
    total_itens_compra = int((filtered_df["Pedido sugerido"] > 0).sum())
    total_unidades = float(filtered_df["Pedido sugerido"].sum())
    
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
        
        display_df = filtered_df.copy()
        numeric_cols = display_df.select_dtypes(include="number").columns
        display_df[numeric_cols] = display_df[numeric_cols].round(2)
        
        st.dataframe(display_df, use_container_width=True, hide_index=True)
        
        dl1, dl2 = st.columns(2)
        try:
            buffer = BytesIO()
            display_df.to_excel(buffer, index=False, engine="openpyxl")
            dl1.download_button(
                "Exportar para Excel",
                data=buffer.getvalue(),
                file_name="pedido_sugerido_smartstock.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except Exception:
            pass
            
        csv_data = display_df.to_csv(index=False).encode("utf-8-sig")
        dl2.download_button(
            "Exportar para CSV",
            data=csv_data,
            file_name="pedido_sugerido_smartstock.csv",
            mime="text/csv",
            use_container_width=True,
        )


def render_assistant(df: pd.DataFrame | None, mapping: dict[str, str | None], dataset_type: str) -> None:
    st.markdown("<div class='section-label'>Assistente Conversacional de IA</div>", unsafe_allow_html=True)
    st.markdown(
        """
        <div class="feature-card" style="margin-bottom: 1.5rem;">
            O assistente analisa seus dados de inventário estruturados em tempo real.
            Você pode pedir análises de venda, risco de ruptura ou sugestão de pedidos.
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Ver amostra da base analisada pela IA", expanded=False):
        st.dataframe(df.head(10), use_container_width=True)


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

    parts.append(f"Pergunta recebida: {question}")
    parts.append("Se você quiser, eu também posso transformar esses dados em sugestão de pedido na aba Sugestão de Compras.")
    return " ".join(parts)


def render_chat(df: pd.DataFrame | None, model, mapping: dict[str, str | None], dataset_type: str) -> None:
    for message in st.session_state.chat_history:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if df is not None:
        st.markdown("<div style='font-size:0.85rem;font-weight:600;color:#64748B;margin-bottom:0.5rem'>Sugestões de consulta:</div>", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🚨 risco de ruptura", use_container_width=True, key="q_ruptura"):
                st.session_state.pending_chat_question = "Quais produtos estão em risco de ruptura?"
            if st.button("🛍️ lista de compra", use_container_width=True, key="q_compra"):
                st.session_state.pending_chat_question = "Me dê a lista de compra priorizada"
        with c2:
            if st.button("📦 estoque parado", use_container_width=True, key="q_parado"):
                st.session_state.pending_chat_question = "Quais produtos estão com estoque parado ou excesso?"
            if st.button("📈 mais vendido", use_container_width=True, key="q_top"):
                st.session_state.pending_chat_question = "Qual é o produto mais vendido?"

    placeholder = "Pergunte sobre estoque, ruptura, curva de vendas, fornecedor ou pedido sugerido"
    question = st.chat_input(placeholder)

    if st.session_state.pending_chat_question:
        question = st.session_state.pending_chat_question
        st.session_state.pending_chat_question = None

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
                    st.session_state.last_ai_error = str(exc)
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

    st.session_state.chat_history.append({"role": "assistant", "content": answer})
    with st.chat_message("assistant"):
        if expanded_context.get("expanded"):
            st.caption(f"Contexto usado: {expanded_question}")
        st.markdown(answer)
        if st.session_state.last_ai_error:
            st.caption(f"Erro da IA: {st.session_state.last_ai_error}")
    st.rerun()


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
        if st.session_state.show_register:
            render_register_form()
        else:
            render_login_form()


def render_login_form() -> None:
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("E-mail", placeholder="seu@email.com")
        show_pw = st.checkbox("Mostrar senha")
        pw_type = "default" if show_pw else "password"
        password = st.text_input("Senha", type=pw_type, placeholder="Sua senha", key="login_pass")

        col1, col2 = st.columns([1, 1])
        with col1:
            st.checkbox("Lembrar de mim", value=True)
        with col2:
            st.markdown(
                '<div class="login-forgot-link"><a href="#">Esqueci minha senha</a></div>',
                unsafe_allow_html=True,
            )

        st.markdown('<div class="login-btn-wrapper">', unsafe_allow_html=True)
        submitted = st.form_submit_button("Entrar na Plataforma", use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        if validate_login(username, password):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("E-mail ou senha inválidos.")

    st.markdown(
        """
        <div class="login-divider">ou continue com</div>
        <div class="social-btns">
            <button class="social-btn" onclick="alert('Em breve disponível')" type="button">
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>
                Entrar com Google
            </button>
            <button class="social-btn" onclick="alert('Em breve disponível')" type="button">
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none"><path d="M11.5 2c1.1 0 2.1.4 2.9 1.1l.1.1c.7.7 1.1 1.7 1.1 2.8 0 1.1-.4 2.1-1.1 2.9l-.1.1c-.8.7-1.8 1.1-2.9 1.1s-2.1-.4-2.9-1.1l-.1-.1C7.9 9.1 7.5 8.1 7.5 7s.4-2.1 1.1-2.9l.1-.1C9.4 2.4 10.4 2 11.5 2zM4 22v-3.5c0-1.1.4-2.1 1.1-2.9l.1-.1c.8-.7 1.8-1.1 2.9-1.1h7c1.1 0 2.1.4 2.9 1.1l.1.1c.7.8 1.1 1.8 1.1 2.9V22H4z" fill="#00A4EF"/></svg>
                Entrar com Microsoft
            </button>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="login-footer">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    c1.markdown('<div style="font-size:0.85rem;color:#64748B;padding-top:4px">Ainda não possui conta?</div>', unsafe_allow_html=True)
    if c2.button("Criar conta gratuita", key="goto_register_btn", use_container_width=True):
        st.session_state.show_register = True
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def render_register_form() -> None:
    st.markdown('<div class="login-card-title">Criar conta gratuita</div>', unsafe_allow_html=True)
    st.markdown('<div class="login-card-subtitle">Preencha os dados para se cadastrar na plataforma</div>', unsafe_allow_html=True)

    with st.form("register_form", clear_on_submit=False):
        name = st.text_input("Nome completo", placeholder="Seu nome")
        email = st.text_input("E-mail", placeholder="seu@email.com")
        reg_show_pw = st.checkbox("Mostrar senha")
        reg_pw_type = "default" if reg_show_pw else "password"
        password = st.text_input("Senha", type=reg_pw_type, placeholder="Crie uma senha", key="reg_pass")
        confirm = st.text_input("Confirmar senha", type=reg_pw_type, placeholder="Confirme a senha", key="reg_confirm")

        st.markdown('<div class="login-btn-wrapper">', unsafe_allow_html=True)
        submitted = st.form_submit_button("Criar Conta", use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        errors = []
        if not name:
            errors.append("Nome é obrigatório.")
        if not email:
            errors.append("E-mail é obrigatório.")
        if not password or len(password) < 6:
            errors.append("Senha deve ter no mínimo 6 caracteres.")
        if password != confirm:
            errors.append("Senhas não conferem.")
        if load_users().get(email.strip().lower()):
            errors.append("E-mail já cadastrado.")

        if errors:
            for err in errors:
                st.error(err)
        else:
            save_user(email, password, name)
            st.success("Conta criada com sucesso! Faça o login.")
            st.session_state.show_register = False
            st.rerun()

    st.markdown('<div class="login-footer">', unsafe_allow_html=True)
    c1, c2 = st.columns([1, 1])
    c1.markdown('<div style="font-size:0.85rem;color:#64748B;padding-top:4px">Já tem conta?</div>', unsafe_allow_html=True)
    if c2.button("Fazer login", key="goto_login_btn", use_container_width=True):
        st.session_state.show_register = False
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)


def main() -> None:
    load_css()
    init_state()

    if not st.session_state.authenticated:
        render_login()
        return

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

    if df is None:
        render_header(None, None)
        render_upload(model)
        return

    mapping = st.session_state.column_mapping
    
    if not st.session_state.mapping_confirmed:
        render_header(df, mapping)
        render_mapping_panel(df)
        return

    dataset_type = infer_dataset_type(df, mapping) if st.session_state.dataset_type == "auto" else st.session_state.dataset_type
    page = st.session_state.page

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
