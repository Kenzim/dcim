"""BMC TLS helper policy tests."""

import ssl

from app.services.ipmi_kvm.bmc_tls import (
    _MEGARAC_CIPHERS,
    bmc_ssl_context,
    ssl_context_for_verify,
)


def test_bmc_ssl_context_disables_verification():
    ctx = bmc_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_ssl_context_for_verify_honors_flag():
    strict = ssl_context_for_verify(True)
    assert strict.verify_mode != ssl.CERT_NONE
    relaxed = ssl_context_for_verify(False)
    assert relaxed.verify_mode == ssl.CERT_NONE


def test_bmc_ssl_context_megarac_includes_rsa_aes_gcm():
    assert "@SECLEVEL=0" in _MEGARAC_CIPHERS
    assert "AES256-GCM-SHA384" in _MEGARAC_CIPHERS
    ctx = bmc_ssl_context(megarac=True)
    names = {cipher["name"] for cipher in ctx.get_ciphers()}
    assert "AES256-GCM-SHA384" in names
