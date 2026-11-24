"""Finder entry point for the bundled mail client."""

import os
from pathlib import Path

from maildesk.__main__ import main


if __name__ == "__main__":
    # The bundled OpenSSL may have been built with another Mac's CA paths.
    # Use macOS's certificate bundle unless the user selected a trust store.
    ca_file = Path("/etc/ssl/cert.pem")
    if "SSL_CERT_FILE" not in os.environ and "SSL_CERT_DIR" not in os.environ and ca_file.is_file():
        os.environ["SSL_CERT_FILE"] = str(ca_file)
    raise SystemExit(main())
