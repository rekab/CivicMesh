# Vendored frontend libraries

Third-party JS shipped verbatim into the captive portal. Each file lists
its provenance below — do not edit the file contents (modifications break
the audit trail). To upgrade, replace the file from the upstream source
listed and bump the SHA + date here.

## qrcode-generator.js

- **Upstream**: https://github.com/kazuhikoarase/qrcode-generator (`js/dist/qrcode.js`)
- **Author**: Kazuhiko Arase
- **License**: MIT (preserved as the file's top-of-file header)
- **Commit**: `83b7e8fe3fddd3b0368dbafd6ce56995bd25e3c8` (2025-08-07)
- **Why vendored**: CivicMesh ships offline-first; the captive portal is
  served on a Pi with no internet and zero external CDN dependencies.
- **Used by**: `static/identity.js` to render the bot's contact-add QR in
  the stats sheet (CIV-14 onboarding).
- **API**: global `qrcode(typeNumber, errorCorrectionLevel)`; we use
  `typeNumber=0` (auto-size) with `'M'` (medium error correction).
