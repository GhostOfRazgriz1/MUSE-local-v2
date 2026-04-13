"""MUSE entry point — wires all components and starts the server."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path

from muse.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("muse")


async def create_orchestrator(config: Config | None = None):
    """Create and wire all components into an Orchestrator instance."""
    if config is None:
        config = Config()

    config.ensure_dirs()

    # Database initialization
    from muse.db.schema import init_agent_db, init_wal_db

    db = await init_agent_db(str(config.db_path))
    wal_db = await init_wal_db(str(config.wal_db_path))

    # Embedding service
    from muse.memory.embeddings import EmbeddingService

    embedding_service = EmbeddingService(config.memory.embedding_model)

    # Memory
    from muse.memory.repository import MemoryRepository
    from muse.memory.cache import MemoryCache
    from muse.memory.promotion import PromotionManager
    from muse.memory.demotion import DemotionManager
    from muse.memory.encryption import MemoryEncryption

    try:
        memory_enc = MemoryEncryption()
    except Exception as exc:
        logger.warning("Memory encryption disabled: %s", exc)
        memory_enc = None
    memory_repo = MemoryRepository(db, embedding_service, encryption=memory_enc)
    memory_cache = MemoryCache(config.memory.cache_budget_mb)
    promotion_manager = PromotionManager(
        memory_repo, memory_cache, embedding_service,
        config.memory, config.registers,
    )
    demotion_manager = DemotionManager(memory_repo, memory_cache, embedding_service)

    # Permissions
    from muse.permissions.repository import PermissionRepository
    from muse.permissions.trust_budget import TrustBudgetManager
    from muse.permissions.manager import PermissionManager

    permission_repo = PermissionRepository(db)
    trust_budget = TrustBudgetManager(db)
    permission_manager = PermissionManager(permission_repo, trust_budget)

    # LLM Providers — local-only build
    from muse.config import BUILTIN_PROVIDERS
    from muse.providers.local import LocalProvider
    from muse.providers.registry import ProviderRegistry
    from muse.providers.model_router import ModelRouter

    from muse.credentials.vault import CredentialVault
    credential_vault = CredentialVault(db)

    # Local provider is the only provider and the fallback.
    local_def = BUILTIN_PROVIDERS["local"]

    # Check if user has configured a custom address/port via settings
    try:
        import json as _json
        async with db.execute(
            "SELECT value FROM user_settings WHERE key = 'local_server'"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            local_cfg = _json.loads(row[0])
            local_url = f"http://{local_cfg.get('address', 'localhost')}:{local_cfg.get('port', 11434)}/v1"
        else:
            local_url = local_def.base_url
    except Exception:
        local_url = local_def.base_url

    local_prov = LocalProvider(base_url=local_url, name="local")
    registry = ProviderRegistry(fallback=local_prov)
    registry.register("local", local_prov)
    logger.info("Registered local LLM provider: %s", local_url)

    # Custom providers are not used in the local-only build, but the
    # block is kept for compatibility if someone adds one via settings.
    try:
        async with db.execute(
            "SELECT value FROM user_settings WHERE key = 'custom_providers'"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            from muse.providers.openai_compat import OpenAICompatibleProvider
            custom_providers = _json.loads(row[0])
            for cp in custom_providers:
                cp_id = cp["id"]
                stored_key = await credential_vault.retrieve(f"{cp_id}_api_key")
                if not stored_key:
                    continue
                registry.register(
                    cp_id,
                    OpenAICompatibleProvider(
                        name=cp["name"],
                        api_key=stored_key,
                        base_url=cp["base_url"],
                    ),
                )
                logger.info("Loaded custom provider '%s' from vault", cp_id)
    except Exception as e:
        logger.debug("Custom provider loading skipped: %s", e)

    provider = registry

    # Load user's saved default model (falls back to config if not set).
    saved_default_model = config.default_model
    try:
        async with db.execute(
            "SELECT value FROM user_settings WHERE key = 'default_model'"
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            saved_default_model = row[0]
            logger.info("User saved default model: %s", saved_default_model)
    except Exception:
        pass

    # Verify the default model's provider is actually registered.
    # If not, pick the first model from whatever provider IS available.
    default_prefix = saved_default_model.split("/")[0] if "/" in saved_default_model else ""
    if default_prefix and default_prefix not in registry.providers:
        available = list(registry.providers.keys())
        if available:
            picked = available[0]
            try:
                prov_models = await registry.providers[picked].list_models()
                if prov_models:
                    saved_default_model = f"{picked}/{prov_models[0].id}"
                    logger.info(
                        "Configured provider '%s' not available, auto-selected: %s",
                        default_prefix, saved_default_model,
                    )
            except Exception:
                logger.debug("Could not list models from %s for auto-selection", picked)

    # Don't mutate config (frozen dataclass) — pass the resolved model
    # directly to the router.
    model_router = ModelRouter(
        provider, db, saved_default_model,
        vision_model=config.vision_model,
    )

    # OAuth
    from muse.credentials.oauth import OAuthManager, PROVIDERS as OAUTH_PROVIDERS

    oauth_manager = OAuthManager(credential_vault, config, db=db)
    credential_vault.set_oauth_manager(oauth_manager)
    # Register domain → credential mappings for gateway injection
    for prov in OAUTH_PROVIDERS.values():
        for domain in prov.domains:
            credential_vault.register_domain(domain, prov.credential_id)

    # Audit
    from muse.audit.repository import AuditRepository

    audit_repo = AuditRepository(db)

    # WAL
    from muse.wal.log import WriteAheadLog

    wal = WriteAheadLog(wal_db)

    # Skills
    from muse.skills.loader import SkillLoader
    from muse.skills.sandbox import SkillSandbox
    from muse.skills.warm_pool import WarmPool

    skill_loader = SkillLoader(db, config.skills_dir, audit_repo=audit_repo)
    warm_pool = WarmPool(
        pool_size=config.execution.warm_pool_size_standard,
        max_reuse=config.execution.max_reuse_cycles,
    )
    skill_sandbox = SkillSandbox(config.skills_dir, config.ipc_dir, warm_pool)

    # API Gateway
    from muse.gateway.proxy import APIGateway

    gateway = APIGateway(credential_vault, audit_repo, config.gateway)

    # Load first-party skills
    builtin_skills = Path(__file__).parent.parent.parent / "skills"
    if builtin_skills.exists():
        await skill_loader.load_first_party_skills(builtin_skills)

    # MCP connection manager
    from muse.mcp.connection_manager import MCPConnectionManager

    mcp_manager = MCPConnectionManager(db)

    # Orchestrator
    from muse.kernel.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        config=config,
        db=db,
        wal_db=wal_db,
        memory_repo=memory_repo,
        memory_cache=memory_cache,
        embedding_service=embedding_service,
        promotion_manager=promotion_manager,
        demotion_manager=demotion_manager,
        permission_manager=permission_manager,
        trust_budget=trust_budget,
        provider=provider,
        model_router=model_router,
        credential_vault=credential_vault,
        audit_repo=audit_repo,
        wal=wal,
        skill_loader=skill_loader,
        skill_sandbox=skill_sandbox,
        gateway=gateway,
        oauth_manager=oauth_manager,
        mcp_manager=mcp_manager,
    )

    return orchestrator


def _ensure_tls_cert(data_dir: Path) -> tuple[str, str]:
    """Generate a self-signed TLS certificate if one doesn't exist.

    Returns (cert_path, key_path). The certificate is created in the
    data directory and reused across restarts.
    """
    cert_path = data_dir / "tls" / "cert.pem"
    key_path = data_dir / "tls" / "key.pem"

    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    cert_path.parent.mkdir(parents=True, exist_ok=True)

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    logger.info("Generating self-signed TLS certificate...")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "MUSE"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MUSE (self-signed)"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("127.0.0.1"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    # Restrict permissions on the key file (Unix)
    try:
        key_path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass

    logger.info("TLS certificate created at %s", cert_path)
    return str(cert_path), str(key_path)


def main():
    """CLI entry point."""
    import argparse
    import uvicorn
    from muse.api.app import create_app

    parser = argparse.ArgumentParser(description="MUSE")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug tracing (logs to data_dir/logs/)")
    parser.add_argument("--no-tls", action="store_true",
                        help="Disable HTTPS (use plain HTTP)")
    args = parser.parse_args()

    config = Config(debug=args.debug)
    config.ensure_dirs()

    # Initialize debug tracer
    from muse.debug import DebugTracer, set_tracer
    tracer = DebugTracer(enabled=config.debug, logs_dir=config.logs_dir)
    set_tracer(tracer)

    if config.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.info("Debug mode ON — logs at %s", config.logs_dir)

    app = create_app()

    # TLS setup
    ssl_kwargs: dict = {}
    scheme = "http"
    if not args.no_tls:
        try:
            cert_path, key_path = _ensure_tls_cert(config.data_dir)
            ssl_kwargs["ssl_certfile"] = cert_path
            ssl_kwargs["ssl_keyfile"] = key_path
            scheme = "https"
        except Exception as e:
            logger.warning("TLS setup failed, falling back to HTTP: %s", e)

    logger.info(f"Starting MUSE on {scheme}://{config.server.host}:{config.server.port}")
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level="debug" if config.debug else "info",
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
