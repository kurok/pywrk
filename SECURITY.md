# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |
| < 1.0   | No        |

## Reporting a Vulnerability

If you discover a security vulnerability in pywrkr, **please do not open a public issue.**

Instead, report it privately:

1. Go to [Security Advisories](https://github.com/kurok/pywrkr/security/advisories)
2. Click **"New draft security advisory"**
3. Provide a clear description of the vulnerability, steps to reproduce, and potential impact

Alternatively, contact the maintainers directly through GitHub.

### What to expect

- **Acknowledgment** within 48 hours
- **Assessment** within 7 days with severity evaluation and timeline
- **Fix release** as soon as practical, depending on severity:
  - Critical: 24-48 hours
  - High: 1-2 weeks
  - Medium/Low: next release cycle
- **Credit** for responsible disclosure (unless you prefer anonymity)

### What qualifies

- Remote code execution
- Command injection via CLI arguments or input files (e.g., scenario files, CSV profiles)
- Arbitrary file read/write
- Dependency vulnerabilities with exploitable impact on pywrkr users

### What does not qualify

- Denial of service against the benchmarking tool itself (it is a load generator by design)
- Issues requiring local access to the machine already running pywrkr
- Vulnerabilities in optional dependencies that don't affect pywrkr's usage

## Security Practices

- **CI scanning**: CodeQL runs on every push and weekly, covering Python and GitHub Actions
- **Least-privilege CI**: All workflows use explicit, minimal `GITHUB_TOKEN` permissions
- **Minimal dependencies**: Only `aiohttp` is required at runtime
- **No secrets in code**: pywrkr does not store or transmit credentials; `--basic-auth` values are only held in memory during the test run
- **Trusted publishing**: PyPI releases use OpenID Connect trusted publishing, no long-lived API tokens

## Responsible Disclosure

When you discover a vulnerability, please:

- Report privately before public disclosure
- Give us reasonable time to patch before revealing publicly
- Only access what is needed to confirm the vulnerability
- Do not disrupt service for other users
