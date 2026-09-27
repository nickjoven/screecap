"""Shared TLS context for talking to the local League client.

Both the LCU (random port) and the Live Client Data API (port 2999) serve
self-signed certificates. We only ever connect to 127.0.0.1, so disabling
verification is appropriate. Centralized here so REST, WebSocket, and
liveclient code all use the same context.
"""
import ssl

INSECURE_LOCALHOST_CTX = ssl.create_default_context()
INSECURE_LOCALHOST_CTX.check_hostname = False
INSECURE_LOCALHOST_CTX.verify_mode = ssl.CERT_NONE
