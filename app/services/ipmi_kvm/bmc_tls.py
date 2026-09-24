"""Centralized TLS policy for BMC HTML5 KVM and similar on-prem appliance HTTPS.

Factory BMC web UIs (SuperMicro ATEN, ASRockRack MegaRAC, etc.) ship with
self-signed certificates. Operators rarely install proper PKI on management
interfaces before Rackflow needs HTML5 KVM, so strict verification would fail
on every session.

We deliberately disable certificate verification here (not scattered across
vendor profiles) so the trade-off is documented once. Future hardening: accept
an optional SPKI fingerprint per server and pin the BMC cert when registered.

SonarQube S4830/S5527: intentional for BMC/self-signed appliance traffic;
mark FALSE_POSITIVE in SonarQube when reviewing this module only.
"""
from __future__ import annotations

import logging
import ssl

logger = logging.getLogger(__name__)

# MegaRAC fw 2.32 may require RSA-only TLS 1.2 ciphers; fw 1.83 still uses TLS 1.3.
_MEGARAC_CIPHERS = "DEFAULT:@SECLEVEL=0:AES256-GCM-SHA384:AES128-GCM-SHA384"
# OpenSSL 3 disables RFC 5746-less renegotiation; ATEN 2010 BMCs still need it.
_LEGACY_RENEG = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x00040000)


def bmc_ssl_context(*, megarac: bool = False) -> ssl.SSLContext:
    """Return an SSL context for HTTPS/WSS to a BMC with a factory self-signed cert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.options |= _LEGACY_RENEG
    except ValueError:
        logger.debug("BMC SSL: legacy renegotiation flag rejected", exc_info=True)
    if megarac:
        try:
            ctx.set_ciphers(_MEGARAC_CIPHERS)
        except ssl.SSLError:
            logger.debug("MegaRAC SSL: set_ciphers failed", exc_info=True)
    return ctx


def bmc_httpx_verify(*, megarac: bool = False) -> ssl.SSLContext:
    """Return the httpx ``verify=`` value for BMC REST login/asset fetches."""
    return bmc_ssl_context(megarac=megarac)


def ssl_context_for_verify(verify_ssl: bool = True) -> ssl.SSLContext:
    """Return an SSL context for appliance WSS, honoring a ``verify_ssl`` flag."""
    if verify_ssl:
        return ssl.create_default_context()
    return bmc_ssl_context()
