from urllib.parse import urlparse

from cryptography.fernet import Fernet
from eth_utils import is_address, to_checksum_address
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings
from typing import Literal, Optional


class Settings(BaseSettings):
    # Database settings
    database_url: str
    
    # Redis settings
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    
    # Auth settings
    auth_token_expire_seconds: int = 864000  # 10 days

    # Login brute-force protection (Redis-backed fixed-window counters).
    login_rate_limit_per_ip: int = 20
    login_rate_limit_per_ip_window_seconds: int = 300
    login_rate_limit_per_username: int = 8
    login_rate_limit_per_username_window_seconds: int = 300

    # Admin MCP Streamable HTTP (remote AI). Off by default so operators can
    # mint keys before exposing /mcp.
    mcp_enabled: bool = False
    mcp_rate_limit_per_key: int = 120
    mcp_rate_limit_per_key_window_seconds: int = 60
    mcp_rate_limit_per_ip: int = 240
    mcp_rate_limit_per_ip_window_seconds: int = 60

    # Trust the X-Forwarded-For header when determining the caller's source IP.
    # This must ONLY be enabled when the app sits behind a trusted reverse proxy
    # that sets/overwrites the header; otherwise clients can spoof their identity
    # (e.g. to fetch another server's cloud-init credentials).
    trust_x_forwarded_for: bool = False
    
    # Initial admin (created only when no users exist in DB)
    initial_admin_username: Optional[str] = None
    initial_admin_password: Optional[str] = None
    initial_admin_email: Optional[str] = None
    
    # API settings
    api_title: str = "Rackflow API"
    api_version: str = "1.0.0"

    # When true, disables the unauthenticated /docs, /redoc, and /openapi.json
    # endpoints, which otherwise expose the full API schema (including
    # admin/billing/reseller endpoint shapes) to unauthenticated recon.
    # Defaults to disabling them (secure-by-default); set to False explicitly
    # for local development if interactive API docs are needed.
    disable_public_api_docs: bool = True

    # Reseller payment processing. Stripe is enabled only when a secret key is
    # explicitly supplied; no test/live credentials are embedded in defaults.
    stripe_secret_key: Optional[str] = None
    stripe_publishable_key: Optional[str] = None
    stripe_webhook_secret: Optional[str] = None
    paypal_client_id: Optional[str] = None
    paypal_client_secret: Optional[str] = None
    paypal_webhook_id: Optional[str] = None
    paypal_environment: Literal["sandbox", "live"] = "sandbox"
    # Public browser base used for PayPal's approval return/cancel redirects.
    # No default is provided because it must be an externally reachable URL.
    paypal_return_base_url: Optional[str] = None
    reseller_topup_min_cents: int = 500
    reseller_topup_max_cents: int = 1_000_000

    # Recurring reseller billing. Dedicated workers are the production default;
    # the in-process switches are for single-node/dev only.
    recurring_billing_retry_days: str = "1,3,5"
    recurring_billing_grace_days: int = 7
    recurring_billing_worker_interval_seconds: int = 60
    recurring_billing_worker_lease_seconds: int = 300
    recurring_billing_worker_batch_size: int = 100
    run_recurring_billing_worker: bool = False
    notification_worker_interval_seconds: int = 30
    notification_worker_lease_seconds: int = 120
    notification_worker_batch_size: int = 100
    run_notification_outbox_worker: bool = False

    # Standard-library SMTP delivery for the durable notification outbox.
    smtp_enabled: bool = False
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[SecretStr] = None
    smtp_from: Optional[str] = None
    smtp_starttls: bool = True
    smtp_timeout_seconds: float = 10.0

    # Retail commerce (orders/checkout/client invoices). Always on by default;
    # there is no public storefront — clients use /client after login.
    commerce_retail_enabled: bool = True
    commerce_public_app_url: Optional[str] = None
    commerce_default_currency: str = "USD"
    commerce_invoice_prefix: str = "INV"
    commerce_terms_version: str = "1"
    commerce_registration_mode: Literal["disabled", "invite_only", "open"] = "disabled"
    commerce_checkout_rate_limit_per_account: int = 10
    commerce_checkout_rate_limit_window_seconds: int = 3600

    # Discord account linking (optional).
    discord_client_id: Optional[str] = None
    discord_client_secret: Optional[SecretStr] = None
    discord_redirect_uri: Optional[str] = None

    # Company branding for invoice PDFs.
    company_name: str = "Rackflow"
    company_address: Optional[str] = None
    company_tax_id: Optional[str] = None

    # Non-custodial USDT-on-Ethereum deposits. There are deliberately no RPC,
    # chain, contract, or key defaults: operators must select the network and
    # token explicitly, especially in test environments.
    usdt_rpc_url: Optional[str] = None
    usdt_chain_id: Optional[int] = None
    usdt_contract_address: Optional[str] = None
    usdt_decimals: int = 6
    usdt_confirmations: int = 12
    usdt_scan_interval_seconds: int = 15
    usdt_scan_chunk_size: int = 2_000
    usdt_invoice_expiry_seconds: int = 86_400
    usdt_hd_mnemonic_ciphertext: Optional[SecretStr] = None
    usdt_fernet_key: Optional[SecretStr] = None
    usdt_treasury_address: Optional[str] = None
    usdt_gas_wallet_private_key_ciphertext: Optional[SecretStr] = None
    usdt_gas_reserve_wei: int = 0
    usdt_tolerance_token_units: int = 0
    usdt_rpc_timeout_seconds: float = 10.0
    usdt_rpc_retries: int = 2
    usdt_reorg_recheck_blocks: int = 128
    usdt_sweep_max_attempts: int = 5
    usdt_watcher_lease_seconds: int = 120
    usdt_watcher_enabled: bool = False
    # Run in the API process only for single-node/dev. Production should use
    # the profile-gated dedicated compose worker.
    run_usdt_watcher: bool = False

    @field_validator(
        "usdt_rpc_url",
        "usdt_contract_address",
        "usdt_treasury_address",
        "usdt_hd_mnemonic_ciphertext",
        "usdt_fernet_key",
        "usdt_gas_wallet_private_key_ciphertext",
        "usdt_chain_id",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        mode="before",
    )
    @classmethod
    def _empty_optional_values_are_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_usdt_configuration(self):
        configured = self._validate_usdt_completeness()
        self._validate_usdt_network()
        self._validate_usdt_limits()
        self._validate_usdt_secrets()
        if (self.usdt_watcher_enabled or self.run_usdt_watcher) and not configured:
            raise ValueError("USDT watcher cannot run without complete configuration")
        if self.run_usdt_watcher and not self.usdt_watcher_enabled:
            raise ValueError("RUN_USDT_WATCHER requires USDT_WATCHER_ENABLED")
        return self

    def _validate_usdt_completeness(self) -> bool:
        core = (
            self.usdt_rpc_url,
            self.usdt_chain_id,
            self.usdt_contract_address,
            self.usdt_hd_mnemonic_ciphertext,
            self.usdt_fernet_key,
        )
        configured = any(value is not None for value in core)
        if configured and not all(value is not None for value in core):
            raise ValueError(
                "USDT RPC URL, chain ID, contract, mnemonic ciphertext, and "
                "Fernet key must be configured together"
            )
        return configured

    def _validate_usdt_network(self) -> None:
        if self.usdt_chain_id is not None and self.usdt_chain_id not in {1, 11155111}:
            raise ValueError("USDT_CHAIN_ID must be 1 or 11155111")
        if self.usdt_rpc_url is not None:
            parsed = urlparse(self.usdt_rpc_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("USDT_RPC_URL must be an HTTP(S) URL")
        for field_name in ("usdt_contract_address", "usdt_treasury_address"):
            address = getattr(self, field_name)
            if address is not None:
                if not is_address(address):
                    raise ValueError(f"{field_name.upper()} is not a valid Ethereum address")
                setattr(self, field_name, to_checksum_address(address))

    def _validate_usdt_limits(self) -> None:
        if self.usdt_decimals < 2 or self.usdt_decimals > 18:
            raise ValueError("USDT_DECIMALS must be between 2 and 18")
        positive_fields = (
            "usdt_confirmations",
            "usdt_scan_interval_seconds",
            "usdt_scan_chunk_size",
            "usdt_invoice_expiry_seconds",
            "usdt_rpc_timeout_seconds",
            "usdt_reorg_recheck_blocks",
            "usdt_sweep_max_attempts",
            "usdt_watcher_lease_seconds",
        )
        if any(float(getattr(self, name)) <= 0 for name in positive_fields):
            raise ValueError("USDT timing, confirmation, and chunk values must be positive")
        if self.usdt_scan_chunk_size > 10_000:
            raise ValueError("USDT_SCAN_CHUNK_SIZE must not exceed 10000")
        if self.usdt_rpc_retries < 0 or self.usdt_rpc_retries > 10:
            raise ValueError("USDT_RPC_RETRIES must be between 0 and 10")
        if self.usdt_gas_reserve_wei < 0 or self.usdt_tolerance_token_units < 0:
            raise ValueError("USDT gas reserve and tolerance must be non-negative")

    def _validate_usdt_secrets(self) -> None:
        if self.usdt_fernet_key is not None:
            try:
                Fernet(self.usdt_fernet_key.get_secret_value().encode())
            except (TypeError, ValueError) as exc:
                raise ValueError("USDT_FERNET_KEY is not a valid Fernet key") from exc
        if self.usdt_gas_wallet_private_key_ciphertext is not None:
            if self.usdt_treasury_address is None or self.usdt_fernet_key is None:
                raise ValueError(
                    "Encrypted USDT gas wallet key requires treasury address and Fernet key"
                )

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def paypal_enabled(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def usdt_enabled(self) -> bool:
        return bool(
            self.usdt_watcher_enabled
            and self.usdt_rpc_url
            and self.usdt_chain_id
            and self.usdt_contract_address
            and self.usdt_hd_mnemonic_ciphertext
            and self.usdt_fernet_key
        )

    @property
    def usdt_sweeper_enabled(self) -> bool:
        return bool(
            self.usdt_enabled
            and self.usdt_treasury_address
            and self.usdt_gas_wallet_private_key_ciphertext
        )

    @model_validator(mode="after")
    def _validate_recurring_billing_and_smtp(self):
        try:
            retry_days = tuple(
                int(value.strip())
                for value in self.recurring_billing_retry_days.split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "RECURRING_BILLING_RETRY_DAYS must be comma-separated integers"
            ) from exc
        if (
            not retry_days
            or any(day <= 0 for day in retry_days)
            or tuple(sorted(set(retry_days))) != retry_days
        ):
            raise ValueError(
                "RECURRING_BILLING_RETRY_DAYS must be unique, ascending positive days"
            )
        if self.recurring_billing_grace_days < retry_days[-1]:
            raise ValueError(
                "RECURRING_BILLING_GRACE_DAYS must include the final retry day"
            )
        positive_fields = (
            "recurring_billing_worker_interval_seconds",
            "recurring_billing_worker_lease_seconds",
            "recurring_billing_worker_batch_size",
            "notification_worker_interval_seconds",
            "notification_worker_lease_seconds",
            "notification_worker_batch_size",
            "smtp_timeout_seconds",
        )
        if any(float(getattr(self, name)) <= 0 for name in positive_fields):
            raise ValueError("Recurring billing, notification, and SMTP limits must be positive")
        if self.recurring_billing_worker_batch_size > 1000:
            raise ValueError("RECURRING_BILLING_WORKER_BATCH_SIZE must not exceed 1000")
        if self.notification_worker_batch_size > 1000:
            raise ValueError("NOTIFICATION_WORKER_BATCH_SIZE must not exceed 1000")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together")
        if self.smtp_enabled and not (self.smtp_host and self.smtp_from):
            raise ValueError("SMTP_ENABLED requires SMTP_HOST and SMTP_FROM")
        return self

    @property
    def recurring_billing_retry_day_offsets(self) -> tuple[int, ...]:
        return tuple(
            int(value.strip())
            for value in self.recurring_billing_retry_days.split(",")
            if value.strip()
        )

    # VM deployment jobs: run the worker loop inside the API process. Off by
    # default -- production runs a separate `deployment-worker` container so the
    # API stays enqueue-only and horizontally scalable. Enable for single-node
    # or dev convenience only.
    run_deployment_worker: bool = False
    deployment_worker_interval_seconds: int = 3
    deployment_worker_lease_ttl_seconds: int = 60
    
    # Static files settings
    static_files_path: str = "./frontend"

    # Optional runner URLs (separate containers). When set, app calls these APIs instead of running subprocesses.
    dhcp_runner_url: Optional[str] = None
    tftp_runner_url: Optional[str] = None
    # Legacy: single URL for combined dhcp+tftp runner (paths /dhcp/* and /tftp/*)
    dhcp_tftp_service_url: Optional[str] = None
    
    # For per-location service instances: encrypt stored API keys so backend can call runners.
    # Must be a base64-encoded 32-byte key. Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    service_instance_encryption_key: Optional[str] = None
    # When True, creating/updating a service instance API key without a configured
    # encryption key is rejected. Defaults to True (secure by default) so that any
    # deployment path -- not just the specific docker-compose.yml wiring -- fails
    # closed instead of silently persisting plaintext runner API keys. Local
    # dev/tests that intentionally exercise the no-key/plaintext path must opt out
    # explicitly via REQUIRE_SERVICE_INSTANCE_ENCRYPTION=false.
    require_service_instance_encryption: bool = True

    # End-user IPMI reverse proxy (subdomain + ticket).
    # Public base domain used to build per-server BMC proxy URLs, e.g. "ipmi.rackflow.com"
    # produces https://{server.uuid}.ipmi.rackflow.com. When unset, launch URLs cannot be
    # built and the mint endpoints return 409.
    ipmi_proxy_public_base: Optional[str] = None
    # URL scheme and optional port for the proxy subdomain. Production defaults to
    # https on the standard port; dev/self-hosted setups can use http + a custom
    # port (e.g. http://{uuid}.ipmi.lan:9082).
    ipmi_proxy_scheme: str = "https"
    ipmi_proxy_port: Optional[int] = None
    # Shared secret the ipmi_proxy_runner edge presents to the runner API (Bearer).
    ipmi_proxy_runner_api_key: Optional[str] = None
    # One-time launch ticket lifetime (seconds). Kept short: the ticket is only used
    # for the browser -> edge handoff, then exchanged for a session cookie.
    ipmi_ticket_ttl_seconds: int = 60
    # Edge session cookie lifetime (seconds) after a ticket is redeemed.
    ipmi_session_ttl_seconds: int = 7200

    # Admin "sign in as" impersonation session lifetime (seconds). Kept short;
    # the admin mints a fresh one each time they click "Sign in as".
    impersonation_session_ttl_seconds: int = 900
    # One-time billing portal SSO redeem token lifetime (seconds). Only used
    # for the browser handoff from the billing platform to /api/client/sso/redeem.
    client_sso_ticket_ttl_seconds: int = 90

    # Public site root for RackFlow admin/service links in Proxmox VM notes
    # (e.g. "https://rackflow.example.com"). Falls back to public_app_url when unset.
    public_base_url: Optional[str] = None

    # VM guest VNC console (Proxmox vncproxy + WebSocket bridge, in-app --
    # not the IPMI reverse proxy). Base URL Rackflow itself is reachable at,
    # used to build the WHMCS popup launch URL (e.g. "https://rackflow.example.com").
    # Required for the billing vnc-ticket endpoint; unset -> 409.
    public_app_url: Optional[str] = None
    # One-time launch ticket lifetime (seconds) -- browser -> /vnc handoff only
    # (mirrors ipmi_ticket_ttl_seconds / client_sso_ticket_ttl_seconds).
    vm_vnc_launch_ttl_seconds: int = 60
    # WS session token lifetime (seconds) -- reusable while the console tab/
    # modal is open, so a brief network blip doesn't force a full re-mint.
    # The token is passed as a `?token=` WS query param (browsers can't set
    # WS headers), so it can land in proxy/access logs; kept to 1 hour rather
    # than longer to bound how long a log-leaked token stays replayable.
    vm_vnc_session_ttl_seconds: int = 3600

    # IPMI HTML5 KVM (BMC IVTP bridge, in-app — not the IPMI web-UI proxy).
    # Launch tickets hand the browser to /kvm; WS sessions authorize /api/kvm/ws.
    ipmi_kvm_launch_ttl_seconds: int = 60
    ipmi_kvm_session_ttl_seconds: int = 3600

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

